#!/usr/bin/env python3
"""gateway — 固定入口的轮换代理网关。

    你的程序 ──HTTP :8080──▶ gateway ──▶ 池中某个上游代理 ──▶ 目标站点
    你的程序 ─SOCKS5 :1080─▶ gateway ──▶ 池中某个上游代理 ──▶ 目标站点

存在的意义：代理池里的 IP 一直在变，业务侧不想每次都去调 API 换地址。
gateway 提供一个稳定的本地入口，每条连接自动从池子里挑一个可用上游转发出去，
失败自动换下一个。

上游来源（可叠加，见 GATEWAY_SOURCE）：
  redis   直读 proxy_pool 的库（DB 0 的 hash），默认来源。少一次 HTTP 往返，
          且能拿到 latency 用于优先选快的上游
  api     走 proxy_pool 的 HTTP API（GET {PROXY_API}/all/）。数据与 redis 来源相同，
          适合 gateway 与 proxy_pool 不在同一台 Redis 旁边的部署
  static  GATEWAY_STATIC_PROXIES 里手工指定的固定列表。proxy_pool 只采 HTTP 代理，
          想接入自备的 SOCKS 上游就用这个

支持的上游协议：http / https（HTTP CONNECT 隧道）、socks5 / socks5h、socks4 / socks4a。

⚠️ 安全提示
    默认不开启鉴权。一旦把 8080 / 1080 暴露到公网，就是一个任何人可用的开放代理，
    会被立刻滥用。只在可信网络内使用，或务必设置 GATEWAY_USER / GATEWAY_PASS。

环境变量
--------
GATEWAY_ENABLED         1/0，默认 1
GATEWAY_SOURCE          上游来源，逗号分隔，默认 "redis"
GATEWAY_HTTP_PORT       HTTP 代理监听端口，默认 8080；设为 0 关闭
GATEWAY_SOCKS_PORT      SOCKS5 监听端口，默认 1080；设为 0 关闭
GATEWAY_BIND            监听地址，默认 0.0.0.0
GATEWAY_USER            客户端鉴权用户名（留空 = 不鉴权）
GATEWAY_PASS            客户端鉴权密码
GATEWAY_REFRESH         上游列表刷新间隔秒数，默认 60
GATEWAY_TOP_N           候选池保留的最快上游数量，默认 200
GATEWAY_POOL_SCAN       每轮从 Redis 扫描的成员上限，默认 1000
GATEWAY_MAX_RETRIES     单次请求最多尝试几个上游，默认 3
GATEWAY_DIAL_TIMEOUT    连接上游 + 建隧道的超时秒数，默认 10
GATEWAY_IDLE_TIMEOUT    隧道空闲多久后断开，默认 120
GATEWAY_FAIL_THRESHOLD  连续失败几次后临时拉黑，默认 2
GATEWAY_COOLDOWN        拉黑时长秒数，默认 300
GATEWAY_STATIC_PROXIES  固定上游，格式 "host:port:proto,host:port:proto"
PROXY_API               proxy_pool API 根地址，source 含 api 时必需
REDIS_HOST/PORT         Redis 连接，默认 redis / 6379
PROXY_POOL_DB           proxy_pool 的库号，默认 0
TABLE_NAME              proxy_pool 的 hash 名，默认 use_proxy
"""

import base64
import json
import os
import random
import selectors
import socket
import socketserver
import struct
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# proxy_pool 在 Redis 里的 hash 名，与 setting.py 的 TABLE_NAME 对应
TABLE_NAME = os.environ.get("TABLE_NAME", "use_proxy")

# 未知延迟按最差处理，保证有实测数据的上游优先被选中
UNKNOWN_LATENCY = 9999
MAX_HEAD_BYTES = 64 * 1024
RELAY_CHUNK = 64 * 1024
BAD_HOSTS = {"0.0.0.0", "127.0.0.1", "localhost", "::1"}

HTTP_PROTOS = {"http", "https"}
SOCKS5_PROTOS = {"socks5", "socks5h"}
SOCKS4_PROTOS = {"socks4", "socks4a"}


def _env_int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


ENABLED = os.environ.get("GATEWAY_ENABLED", "1").strip().lower() not in ("0", "false", "no", "")
SOURCES = [s.strip().lower() for s in os.environ.get("GATEWAY_SOURCE", "redis").split(",") if s.strip()]
HTTP_PORT = _env_int("GATEWAY_HTTP_PORT", 8080)
SOCKS_PORT = _env_int("GATEWAY_SOCKS_PORT", 1080)
BIND = os.environ.get("GATEWAY_BIND", "0.0.0.0")
AUTH_USER = os.environ.get("GATEWAY_USER", "")
AUTH_PASS = os.environ.get("GATEWAY_PASS", "")
REFRESH = max(10, _env_int("GATEWAY_REFRESH", 60))
TOP_N = max(1, _env_int("GATEWAY_TOP_N", 200))
POOL_SCAN = max(1, _env_int("GATEWAY_POOL_SCAN", 1000))
MAX_RETRIES = max(1, _env_int("GATEWAY_MAX_RETRIES", 3))
DIAL_TIMEOUT = max(1, _env_int("GATEWAY_DIAL_TIMEOUT", 10))
IDLE_TIMEOUT = max(5, _env_int("GATEWAY_IDLE_TIMEOUT", 120))
FAIL_THRESHOLD = max(1, _env_int("GATEWAY_FAIL_THRESHOLD", 2))
COOLDOWN = max(10, _env_int("GATEWAY_COOLDOWN", 300))
STATIC_PROXIES = os.environ.get("GATEWAY_STATIC_PROXIES", "")
PROXY_API = os.environ.get("PROXY_API", "").strip().rstrip("/")
REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT = _env_int("REDIS_PORT", 6379)
PROXY_POOL_DB = _env_int("PROXY_POOL_DB", 0)


def log(msg):
    print(f"[gateway] {msg}", flush=True)


# ────────────────────────── 上游池 ──────────────────────────

class Upstream:
    __slots__ = ("host", "port", "proto", "latency")

    def __init__(self, host, port, proto, latency=UNKNOWN_LATENCY):
        self.host = host
        self.port = port
        self.proto = proto
        self.latency = latency

    @property
    def key(self):
        return f"{self.host}:{self.port}"

    def __repr__(self):
        return f"{self.proto}://{self.host}:{self.port}({self.latency}ms)"


def _normalize_proto(raw):
    """把各种写法归一到 dialer 认识的协议名，认不出来就当 http。"""
    p = (raw or "").strip().lower()
    if p in SOCKS5_PROTOS:
        return "socks5"
    if p in SOCKS4_PROTOS:
        return "socks4"
    if p in HTTP_PROTOS:
        return "http"
    return "http"


def _valid_endpoint(host, port):
    return bool(host) and host not in BAD_HOSTS and 1 <= port <= 65535


def _parse_latency(raw):
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return UNKNOWN_LATENCY
    return value if value > 0 else UNKNOWN_LATENCY


class UpstreamPool:
    def __init__(self):
        self._lock = threading.Lock()
        self._items = []
        self._fails = {}
        self._cooldown = {}
        self._redis = None

    # ---- 各来源加载 ----

    def _redis_client(self):
        if self._redis is None:
            import redis  # 延迟导入：source 不含 redis 时无需该依赖

            self._redis = redis.Redis(
                host=REDIS_HOST, port=REDIS_PORT, db=PROXY_POOL_DB,
                decode_responses=True, socket_connect_timeout=5, socket_timeout=5,
            )
        return self._redis

    def _load_redis(self):
        """直读 proxy_pool 的库（DB 0 的 hash，field=ip:port，value=Proxy.to_json）。

        比走 api 来源少一次 HTTP 往返，而且能拿到 latency 用于择优。
        POOL_SCAN 只是一道安全上限：proxy_pool 的 MAX_FAIL_COUNT 默认为 0，
        失败即删，池子里基本都是可用代理，量级不大。
        """
        r = self._redis_client()
        out = []
        for field, value in r.hscan_iter(TABLE_NAME, count=1000):
            try:
                data = json.loads(value)
            except (TypeError, ValueError):
                continue
            proxy = str(data.get("proxy") or field or "")
            host, _, port = proxy.rpartition(":")
            if not port.isdigit():
                continue
            port = int(port)
            if not _valid_endpoint(host, port):
                continue
            # proxy_pool 只收 HTTP 代理（它的校验器验不了 socks），
            # https 位表示该代理支持 CONNECT，对拨号方式而言仍是 http 类上游
            out.append(Upstream(host, port, "http", _parse_latency(data.get("latency"))))
            if len(out) >= POOL_SCAN:
                break
        return out

    def _load_api(self):
        if not PROXY_API:
            return []
        req = urllib.request.Request(f"{PROXY_API}/all/",
                                     headers={"User-Agent": "proxy-pool-gateway/1.0"})
        # 这个 ProxyHandler({}) 不能删。PROXY_API 指向的是本机（同容器内的
        # proxy_pool），而宿主机若配了 docker 代理，Docker 会把 HTTP_PROXY 注入容器，
        # 用默认 opener 的话这个内部请求会被送去外部代理而失败。
        # 靠代码禁用比靠 NO_PROXY 可靠：Docker 会同时注入大小写两个变量，
        # 只覆盖其中一个时另一个会被优先采纳（所以本项目不再设 NO_PROXY）。
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=DIAL_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
        if not isinstance(payload, list):
            return []

        out = []
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            host, _, port = str(entry.get("proxy") or "").rpartition(":")
            if not port.isdigit():
                continue
            port = int(port)
            if not _valid_endpoint(host, port):
                continue
            # proxy_pool 只存 HTTP 代理，https=True 表示支持 CONNECT
            out.append(Upstream(host, port, "http", UNKNOWN_LATENCY))
        return out

    def _load_static(self):
        out = []
        for item in STATIC_PROXIES.split(","):
            item = item.strip()
            if not item:
                continue
            parts = item.split(":")
            if len(parts) < 2:
                continue
            host, port_raw = parts[0], parts[1]
            proto = parts[2] if len(parts) > 2 else "http"
            if not port_raw.isdigit():
                continue
            port = int(port_raw)
            if not _valid_endpoint(host, port):
                continue
            out.append(Upstream(host, port, _normalize_proto(proto), UNKNOWN_LATENCY))
        return out

    # ---- 刷新 / 选取 / 反馈 ----

    def refresh(self):
        loaders = {"redis": self._load_redis, "api": self._load_api, "static": self._load_static}
        merged = {}
        for name in SOURCES:
            loader = loaders.get(name)
            if loader is None:
                log(f"未知的 GATEWAY_SOURCE 项：{name}（已忽略）")
                continue
            try:
                for up in loader():
                    prev = merged.get(up.key)
                    # 同一 endpoint 出现在多个来源时，保留延迟更低（信息更确切）的那条
                    if prev is None or up.latency < prev.latency:
                        merged[up.key] = up
            except Exception as exc:  # noqa: BLE001 - 单个来源失败不能影响其他来源
                log(f"来源 {name} 加载失败: {exc.__class__.__name__}: {exc}")

        items = sorted(merged.values(), key=lambda u: u.latency)[:TOP_N]
        with self._lock:
            self._items = items
            live = set(merged)
            # 已经不在池子里的 endpoint，其失败计数也一并清掉，避免无限增长
            self._fails = {k: v for k, v in self._fails.items() if k in live}
            self._cooldown = {k: v for k, v in self._cooldown.items() if k in live}
        return len(items)

    def pick(self, exclude=()):
        now = time.time()
        with self._lock:
            usable = [u for u in self._items
                      if u.key not in exclude and self._cooldown.get(u.key, 0) <= now]
            if not usable:
                # 全在冷却中，说明池子普遍不可用，此时无视冷却做一次兜底尝试
                usable = [u for u in self._items if u.key not in exclude]
        return random.choice(usable) if usable else None

    def report(self, up, ok):
        with self._lock:
            if ok:
                self._fails.pop(up.key, None)
                self._cooldown.pop(up.key, None)
                return
            fails = self._fails.get(up.key, 0) + 1
            self._fails[up.key] = fails
            if fails >= FAIL_THRESHOLD:
                self._cooldown[up.key] = time.time() + COOLDOWN

    def stats(self):
        now = time.time()
        with self._lock:
            return len(self._items), sum(1 for t in self._cooldown.values() if t > now)


POOL = UpstreamPool()


# ────────────────────────── socket 小工具 ──────────────────────────

def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise OSError("connection closed while reading")
        buf += chunk
    return buf


def _read_head(sock):
    """读到 \\r\\n\\r\\n 为止，返回 (头部, 多读到的剩余字节)。"""
    buf = b""
    while b"\r\n\r\n" not in buf:
        if len(buf) > MAX_HEAD_BYTES:
            raise OSError("response head too large")
        chunk = sock.recv(RELAY_CHUNK)
        if not chunk:
            raise OSError("connection closed while reading head")
        buf += chunk
    head, _, leftover = buf.partition(b"\r\n\r\n")
    return head, leftover


def _encode_host(host):
    try:
        return host.encode("idna")
    except (UnicodeError, ValueError):
        return host.encode("ascii", "ignore")


def split_hostport(authority, default_port):
    """解析 host:port，兼容 IPv6 的 [::1]:443 写法。"""
    authority = authority.strip()
    if authority.startswith("["):
        host, sep, rest = authority[1:].partition("]")
        if sep and rest.startswith(":") and rest[1:].isdigit():
            return host, int(rest[1:])
        return host, default_port
    host, sep, port = authority.rpartition(":")
    if sep and port.isdigit():
        return host, int(port)
    return authority, default_port


def relay(client, upstream):
    """双向转发，任一端关闭或空闲超时就结束。"""
    for sock in (client, upstream):
        sock.settimeout(None)
    sel = selectors.DefaultSelector()
    try:
        sel.register(client, selectors.EVENT_READ, upstream)
        sel.register(upstream, selectors.EVENT_READ, client)
        while True:
            events = sel.select(timeout=IDLE_TIMEOUT)
            if not events:
                return  # 空闲超时
            for key, _mask in events:
                try:
                    data = key.fileobj.recv(RELAY_CHUNK)
                except OSError:
                    return
                if not data:
                    return
                try:
                    key.data.sendall(data)
                except OSError:
                    return
    finally:
        sel.close()


# ────────────────────────── 上游拨号 ──────────────────────────

def _dial_http(up, host, port):
    sock = socket.create_connection((up.host, up.port), timeout=DIAL_TIMEOUT)
    try:
        sock.settimeout(DIAL_TIMEOUT)
        target = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        sock.sendall((f"CONNECT {target} HTTP/1.1\r\n"
                      f"Host: {target}\r\n"
                      f"User-Agent: proxy-pool-gateway/1.0\r\n"
                      f"Proxy-Connection: Keep-Alive\r\n\r\n").encode())
        head, leftover = _read_head(sock)
        status = head.split(b"\r\n", 1)[0]
        fields = status.split(None, 2)
        if len(fields) < 2 or fields[1] != b"200":
            raise OSError(f"CONNECT 被拒: {status[:120].decode('latin1', 'replace')}")
        return sock, leftover
    except Exception:
        sock.close()
        raise


def _dial_socks5(up, host, port):
    sock = socket.create_connection((up.host, up.port), timeout=DIAL_TIMEOUT)
    try:
        sock.settimeout(DIAL_TIMEOUT)
        sock.sendall(b"\x05\x01\x00")  # 只声明"无鉴权"
        rep = _recv_exact(sock, 2)
        if rep[0] != 0x05 or rep[1] != 0x00:
            raise OSError("socks5 握手失败（上游要求鉴权？）")

        try:
            addr = b"\x01" + socket.inet_aton(host)  # ATYP=IPv4
        except OSError:
            try:
                addr = b"\x04" + socket.inet_pton(socket.AF_INET6, host)  # ATYP=IPv6
            except OSError:
                encoded = _encode_host(host)  # ATYP=域名
                if not 0 < len(encoded) <= 255:
                    raise OSError("socks5 域名长度非法")
                addr = b"\x03" + bytes([len(encoded)]) + encoded

        sock.sendall(b"\x05\x01\x00" + addr + struct.pack(">H", port))
        rep = _recv_exact(sock, 4)
        if rep[1] != 0x00:
            raise OSError(f"socks5 连接失败 rep=0x{rep[1]:02x}")

        atyp = rep[3]
        if atyp == 0x01:
            _recv_exact(sock, 4 + 2)
        elif atyp == 0x03:
            _recv_exact(sock, _recv_exact(sock, 1)[0] + 2)
        elif atyp == 0x04:
            _recv_exact(sock, 16 + 2)
        else:
            raise OSError(f"socks5 返回未知 ATYP=0x{atyp:02x}")
        return sock, b""
    except Exception:
        sock.close()
        raise


def _dial_socks4(up, host, port):
    sock = socket.create_connection((up.host, up.port), timeout=DIAL_TIMEOUT)
    try:
        sock.settimeout(DIAL_TIMEOUT)
        try:
            packed, trailer = socket.inet_aton(host), b""
        except OSError:
            # SOCKS4a：IP 填 0.0.0.1，域名以 NUL 结尾附在请求末尾
            packed, trailer = b"\x00\x00\x00\x01", _encode_host(host) + b"\x00"
        sock.sendall(b"\x04\x01" + struct.pack(">H", port) + packed + b"\x00" + trailer)
        rep = _recv_exact(sock, 8)
        if rep[1] != 0x5A:
            raise OSError(f"socks4 连接失败 rep=0x{rep[1]:02x}")
        return sock, b""
    except Exception:
        sock.close()
        raise


DIALERS = {"http": _dial_http, "socks5": _dial_socks5, "socks4": _dial_socks4}


def open_tunnel(host, port):
    """挑上游并建立到 host:port 的隧道，失败自动换下一个。

    返回 (socket, 上游多读到的剩余字节, Upstream)。全部失败则抛最后一次的异常。
    """
    tried = set()
    last_error = None
    for _ in range(MAX_RETRIES):
        up = POOL.pick(exclude=tried)
        if up is None:
            break
        tried.add(up.key)
        dialer = DIALERS.get(up.proto, _dial_http)
        try:
            sock, leftover = dialer(up, host, port)
        except Exception as exc:  # noqa: BLE001 - 换下一个上游重试
            POOL.report(up, ok=False)
            last_error = exc
            continue
        POOL.report(up, ok=True)
        return sock, leftover, up

    if last_error is None:
        last_error = OSError("上游池为空，暂无可用代理")
    raise last_error


def _forward_http_via(up, host, port, request_bytes):
    """把已构造好的 HTTP 请求发给上游。

    上游是 HTTP 代理时用 absolute-URI 直发（兼容性最好，无需上游放开 CONNECT 80）；
    上游是 SOCKS 时先打隧道再发 origin-form。请求字节由调用方按需构造。
    """
    dialer = DIALERS.get(up.proto, _dial_http)
    if up.proto == "http":
        sock = socket.create_connection((up.host, up.port), timeout=DIAL_TIMEOUT)
        leftover = b""
    else:
        sock, leftover = dialer(up, host, port)
    try:
        sock.settimeout(DIAL_TIMEOUT)
        sock.sendall(request_bytes)
        return sock, leftover
    except Exception:
        sock.close()
        raise


# ────────────────────────── HTTP 代理入口 ──────────────────────────

HOP_BY_HOP = {
    "proxy-connection", "connection", "keep-alive", "proxy-authorization",
    "proxy-authenticate", "te", "trailer", "upgrade",
}

_EXPECTED_AUTH = ("Basic " + base64.b64encode(f"{AUTH_USER}:{AUTH_PASS}".encode()).decode()) if AUTH_USER else ""


class HttpProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "proxy-pool-gateway"
    sys_version = ""

    def log_message(self, fmt, *args):
        pass  # 走我们自己的 log()

    # ---- 鉴权 ----
    def _authorized(self):
        if not _EXPECTED_AUTH:
            return True
        if self.headers.get("Proxy-Authorization", "") == _EXPECTED_AUTH:
            return True
        body = b"proxy authentication required\n"
        self.send_response(407)
        self.send_header("Proxy-Authenticate", 'Basic realm="proxy-pool-gateway"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True
        return False

    def _fail(self, code, message):
        body = f"{message}\n".encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass
        self.close_connection = True

    # ---- CONNECT：HTTPS 隧道 ----
    def do_CONNECT(self):
        if not self._authorized():
            return
        host, port = split_hostport(self.path, 443)
        if not host:
            return self._fail(400, "bad CONNECT target")

        try:
            upstream, leftover, up = open_tunnel(host, port)
        except Exception as exc:  # noqa: BLE001
            log(f"CONNECT {host}:{port} 失败: {exc}")
            return self._fail(502, f"no usable upstream proxy: {exc}")

        try:
            self.connection.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            if leftover:
                self.connection.sendall(leftover)
            relay(self.connection, upstream)
        except OSError:
            pass
        finally:
            upstream.close()
            self.close_connection = True

    # ---- 普通 HTTP 请求 ----
    def _read_body(self):
        length = self.headers.get("Content-Length")
        if length and length.isdigit():
            return self.rfile.read(int(length))
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            # 原样读出 chunked 数据（含长度行），转发时保持 Transfer-Encoding 不变
            body = b""
            while True:
                line = self.rfile.readline()
                if not line:
                    break
                body += line
                size_field = line.strip().split(b";")[0]
                try:
                    size = int(size_field, 16)
                except ValueError:
                    break
                if size == 0:
                    body += self.rfile.readline()  # 结束的空行
                    break
                body += self.rfile.read(size)
                body += self.rfile.readline()  # chunk 后的 CRLF
            return body
        return b""

    def _build_request(self, absolute_uri, host, port, origin_form):
        target = absolute_uri if not origin_form else (origin_form or "/")
        lines = [f"{self.command} {target} HTTP/1.1"]
        seen_host = False
        for name, value in self.headers.items():
            if name.lower() in HOP_BY_HOP:
                continue
            if name.lower() == "host":
                seen_host = True
            lines.append(f"{name}: {value}")
        if not seen_host:
            lines.append(f"Host: {host}" if port in (80, None) else f"Host: {host}:{port}")
        # 强制单次请求即关闭，省掉上游连接复用带来的状态管理
        lines.append("Connection: close")
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1", "replace")

    def _forward(self):
        if not self._authorized():
            return
        if not self.path.lower().startswith("http://"):
            # 非绝对 URI 说明不是把我们当代理在用（例如直接浏览器访问 8080）
            return self._fail(400, "this port is an HTTP proxy, not a web server; "
                                   "configure it as http_proxy and use absolute URIs")

        rest = self.path[len("http://"):]
        authority, sep, path_qs = rest.partition("/")
        host, port = split_hostport(authority, 80)
        origin_form = ("/" + path_qs) if sep else "/"
        if not host:
            return self._fail(400, "bad request target")

        try:
            body = self._read_body()
        except OSError:
            return self._fail(400, "failed to read request body")

        tried = set()
        last_error = None
        for _ in range(MAX_RETRIES):
            up = POOL.pick(exclude=tried)
            if up is None:
                break
            tried.add(up.key)
            # HTTP 上游用 absolute-URI 直发；SOCKS 上游打隧道后用 origin-form
            request_bytes = self._build_request(self.path, host, port,
                                                None if up.proto == "http" else origin_form)
            try:
                upstream, leftover = _forward_http_via(up, host, port, request_bytes + body)
            except Exception as exc:  # noqa: BLE001
                POOL.report(up, ok=False)
                last_error = exc
                continue

            POOL.report(up, ok=True)
            try:
                if leftover:
                    self.connection.sendall(leftover)
                # 上游响应原样回吐，不经 BaseHTTPRequestHandler 二次加工
                upstream.settimeout(IDLE_TIMEOUT)
                while True:
                    chunk = upstream.recv(RELAY_CHUNK)
                    if not chunk:
                        break
                    self.connection.sendall(chunk)
            except OSError as exc:
                last_error = exc
            finally:
                upstream.close()
                self.close_connection = True
            return

        log(f"{self.command} {host}:{port} 失败: {last_error}")
        self._fail(502, f"no usable upstream proxy: {last_error or 'pool empty'}")

    do_GET = _forward
    do_POST = _forward
    do_PUT = _forward
    do_DELETE = _forward
    do_HEAD = _forward
    do_PATCH = _forward
    do_OPTIONS = _forward


# ────────────────────────── SOCKS5 入口 ──────────────────────────

class Socks5Handler(socketserver.BaseRequestHandler):
    def handle(self):
        sock = self.request
        try:
            sock.settimeout(DIAL_TIMEOUT)
            if not self._negotiate(sock):
                return
            target = self._read_request(sock)
            if target is None:
                return
            host, port = target
        except Exception:  # noqa: BLE001 - 畸形客户端不能把处理线程打挂
            return

        try:
            upstream, leftover, up = open_tunnel(host, port)
        except Exception as exc:  # noqa: BLE001
            log(f"SOCKS5 {host}:{port} 失败: {exc}")
            self._reply(sock, 0x05)  # 0x05 = connection refused
            return

        try:
            self._reply(sock, 0x00)
            if leftover:
                sock.sendall(leftover)
            relay(sock, upstream)
        except OSError:
            pass
        finally:
            upstream.close()

    def _negotiate(self, sock):
        header = _recv_exact(sock, 2)
        if header[0] != 0x05:
            return False
        methods = set(_recv_exact(sock, header[1])) if header[1] else set()

        if AUTH_USER:
            if 0x02 not in methods:
                sock.sendall(b"\x05\xff")
                return False
            sock.sendall(b"\x05\x02")
            if _recv_exact(sock, 1)[0] != 0x01:
                return False
            user = _recv_exact(sock, _recv_exact(sock, 1)[0]).decode("utf-8", "replace")
            password = _recv_exact(sock, _recv_exact(sock, 1)[0]).decode("utf-8", "replace")
            if user != AUTH_USER or password != AUTH_PASS:
                sock.sendall(b"\x01\x01")
                return False
            sock.sendall(b"\x01\x00")
            return True

        if 0x00 not in methods:
            sock.sendall(b"\x05\xff")
            return False
        sock.sendall(b"\x05\x00")
        return True

    def _read_request(self, sock):
        header = _recv_exact(sock, 4)
        if header[0] != 0x05:
            return None
        if header[1] != 0x01:  # 只支持 CONNECT，不支持 BIND / UDP ASSOCIATE
            self._reply(sock, 0x07)
            return None

        atyp = header[3]
        if atyp == 0x01:
            host = socket.inet_ntoa(_recv_exact(sock, 4))
        elif atyp == 0x03:
            # 注意：这里不能用 "idna" 编解码器。Python 的 idna codec 只支持
            # errors='strict'，传 'replace' 会直接抛 UnicodeError。
            # SOCKS5 传过来的域名本身就是 ASCII（可能已是 punycode），
            # 按 utf-8 解出来即可，需要 IDNA 编码时交给 _encode_host()。
            host = _recv_exact(sock, _recv_exact(sock, 1)[0]).decode("utf-8", "replace")
        elif atyp == 0x04:
            host = socket.inet_ntop(socket.AF_INET6, _recv_exact(sock, 16))
        else:
            self._reply(sock, 0x08)
            return None

        port = struct.unpack(">H", _recv_exact(sock, 2))[0]
        if not host:
            self._reply(sock, 0x01)
            return None
        return host, port

    @staticmethod
    def _reply(sock, code):
        # BND.ADDR / BND.PORT 填 0，客户端一般不校验
        try:
            sock.sendall(b"\x05" + bytes([code]) + b"\x00\x01" + b"\x00" * 4 + b"\x00\x00")
        except OSError:
            pass


class ThreadedSocksServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


# ────────────────────────── 启动 ──────────────────────────

def _refresh_loop():
    while True:
        try:
            count = POOL.refresh()
            total, cooling = POOL.stats()
            log(f"上游刷新: 候选={count} 冷却中={cooling} (来源: {','.join(SOURCES)})")
            if total == 0:
                log("候选池为空 — 代理池还在预热，或 GATEWAY_SOURCE / PROXY_API 配置有误")
        except Exception as exc:  # noqa: BLE001 - 刷新线程必须常驻
            log(f"刷新失败: {exc.__class__.__name__}: {exc}")
        time.sleep(REFRESH)


def start():
    """把网关的监听和后台刷新都起成守护线程后立即返回。

    拆出这个函数是为了让 web.py 能在同一个进程里托管网关 —— 网关本来就全是
    线程模型，单独占一个进程只是多一份开销。返回已启动的 server 列表，
    调用方想关的时候自己 shutdown；不关也无所谓，都是 daemon 线程。
    """
    if not ENABLED:
        log("GATEWAY_ENABLED=0，网关不启用")
        return []
    if HTTP_PORT <= 0 and SOCKS_PORT <= 0:
        log("HTTP 与 SOCKS 端口都被关闭，网关无事可做")
        return []

    if not AUTH_USER:
        log("⚠️  未设置 GATEWAY_USER/GATEWAY_PASS —— 这是一个无鉴权的开放代理，"
            "切勿把端口暴露到公网")

    # 先同步刷一次，避免刚起来就因为池子是空的而全部 502
    try:
        POOL.refresh()
    except Exception as exc:  # noqa: BLE001
        log(f"首次刷新失败（后台会继续重试）: {exc}")
    threading.Thread(target=_refresh_loop, daemon=True).start()

    servers = []
    if HTTP_PORT > 0:
        http_server = ThreadingHTTPServer((BIND, HTTP_PORT), HttpProxyHandler)
        http_server.daemon_threads = True
        servers.append(("HTTP", http_server))
    if SOCKS_PORT > 0:
        servers.append(("SOCKS5", ThreadedSocksServer((BIND, SOCKS_PORT), Socks5Handler)))

    for name, server in servers:
        log(f"{name} 监听 {BIND}:{server.server_address[1]}")
        threading.Thread(target=server.serve_forever, daemon=True).start()
    return servers


def main():
    """单独运行网关时的入口（容器里是由 web.py 托管的，不走这里）。"""
    servers = start()
    if not servers:
        return 0
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        for _name, server in servers:
            server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
