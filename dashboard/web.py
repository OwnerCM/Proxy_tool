#!/usr/bin/env python3
"""展示层 —— 静态页面 + 3 个只读 API，数据源是 proxy_pool 的 Redis DB 0。

设计要点
--------
* **只读**。所有采集、验证、淘汰都由 proxy_pool 负责，这里一个字都不往代理数据里写。
  历史上这个看板自带一整套采集器和验证器，与 proxy_pool 完全重复且质量策略冲突
  （proxy_pool 保留 10s 内可用的，看板则删掉 ≥500ms 的），已全部移除。

* **DB 0 是唯一数据源**，格式为 proxy_pool 的 hash：field 是 "ip:port"，
  value 是 Proxy.to_json。DB 1 现在只用来放 geo.py 的地理缓存，不再是数据平面。

* **渲染路径绝不同步联网**。geo.resolve() 对 CN IP 会同步调在线 API（超时 12s），
  一页 50 行就能把请求拖死。所以这里只用离线库 + Redis 缓存，未命中的 IP 丢进
  geo:queue，由 geo.start_filler() 的后台线程慢慢补。

* 快照整体缓存 SNAPSHOT_TTL 秒。DB 0 是单个 hash，hgetall 上万条不便每请求都做一次。

环境变量
--------
REDIS_HOST / REDIS_PORT   Redis 位置，默认 redis:6379
PROXY_POOL_DB             proxy_pool 用的库号，默认 0
TABLE_NAME                proxy_pool 的 hash 名，默认 use_proxy（与 setting.py 同名配置对应）
WEB_PORT                  监听端口，默认 5050
WEB_BIND                  监听地址，默认 0.0.0.0
SNAPSHOT_TTL              快照缓存秒数，默认 60
"""

import json
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import redis

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import geo  # noqa: E402  依赖 sys.path 先就位

REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379") or 6379)
PROXY_POOL_DB = int(os.environ.get("PROXY_POOL_DB", "0") or 0)
TABLE_NAME = os.environ.get("TABLE_NAME", "use_proxy")
WEB_PORT = int(os.environ.get("WEB_PORT", "5050") or 5050)
WEB_BIND = os.environ.get("WEB_BIND", "0.0.0.0")
SNAPSHOT_TTL = int(os.environ.get("SNAPSHOT_TTL", "60") or 60)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MAX_LIMIT = 200
DEFAULT_LIMIT = 50
BAD_HOSTS = {"0.0.0.0", "127.0.0.1", "localhost", "::1"}
UNKNOWN = "?"

MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8", ".svg": "image/svg+xml",
        ".png": "image/png", ".ico": "image/x-icon", ".json": "application/json"}

_pool = redis.ConnectionPool(host=REDIS_HOST, port=REDIS_PORT, db=PROXY_POOL_DB,
                             decode_responses=True, socket_timeout=5,
                             socket_connect_timeout=3)
_redis = redis.Redis(connection_pool=_pool)

_snapshot = {"at": 0.0, "rows": [], "by_country": {}}
_snapshot_lock = threading.Lock()


def log(msg):
    print(f"[web] {msg}", flush=True)


# ────────────────────────── 数据读取 ──────────────────────────

def _country_of(ip, region):
    """确定国家代码。

    优先用 proxy_pool 自己解析的 region —— 它调 api.ip.sb 拿的就是 country_code，
    校验阶段已经填好了，不用重复解析。拿不到时退到本地离线库。
    """
    code = (region or "").strip().upper()
    if len(code) == 2 and code.isascii() and code.isalpha():
        return code
    try:
        code = geo.resolve_region(ip)          # 离线优先，不联网
        if code and code != "ZZ" and len(code) == 2:
            return code
    except Exception:
        pass
    return UNKNOWN


def _location_of(ip, country_code):
    """城市/地区文字。只读缓存，未命中就排队让后台补，绝不在这里联网。"""
    try:
        data = geo._cached(ip)
        if data and not data.get("_placeholder"):
            loc = geo._format_location(data)
            if loc:
                return loc
        geo._enqueue_ip(ip)
    except Exception:
        pass
    return geo.COUNTRY_CODE.get(country_code, country_code)


def _build_snapshot():
    """把 DB 0 整个读出来，转成展示需要的形状。"""
    rows = []
    for field, value in _redis.hscan_iter(TABLE_NAME, count=1000):
        try:
            d = json.loads(value)
        except (TypeError, ValueError):
            continue
        proxy = str(d.get("proxy") or field or "")
        ip, _, port = proxy.rpartition(":")
        if not ip or ip in BAD_HOSTS or not port.isdigit():
            continue

        country = _country_of(ip, d.get("region"))
        try:
            delay = int(float(d.get("latency") or 0))
        except (TypeError, ValueError):
            delay = 0

        rows.append({
            "ip": ip,
            "port": port,
            # proxy_pool 只有 https 布尔位，没有 socks —— 它的校验器也验不了 socks
            "protocol": "https" if d.get("https") else "http",
            "delay": max(0, delay),
            "country": country,
            "last_check": d.get("last_time") or "-",
            "source": d.get("source") or UNKNOWN,
        })

    by_country = {}
    for row in rows:
        by_country.setdefault(row["country"], []).append(row)
    return rows, by_country


def snapshot():
    now = time.time()
    with _snapshot_lock:
        fresh = _snapshot["rows"] and now - _snapshot["at"] < SNAPSHOT_TTL
        if fresh:
            return _snapshot["rows"], _snapshot["by_country"]
    try:
        rows, by_country = _build_snapshot()
    except redis.RedisError as exc:
        log(f"读取 Redis 失败: {exc}")
        with _snapshot_lock:
            return _snapshot["rows"], _snapshot["by_country"]
    with _snapshot_lock:
        _snapshot.update(at=now, rows=rows, by_country=by_country)
        return rows, by_country


# ────────────────────────── 查询逻辑 ──────────────────────────

def _first(params, key, default=""):
    values = params.get(key)
    return values[0].strip() if values and values[0] is not None else default


def _as_int(raw, default, low=None, high=None):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if low is not None:
        value = max(low, value)
    if high is not None:
        value = min(high, value)
    return value


def query_country(code, params):
    _, by_country = snapshot()
    rows = list(by_country.get(code, []))

    protocol = _first(params, "protocol").lower()
    if protocol:
        rows = [r for r in rows if r["protocol"] == protocol]

    max_delay = _as_int(_first(params, "delay"), 0, low=0)
    if max_delay > 0:
        # delay=0 表示还没测出延迟，按"未知"处理，不参与延迟筛选
        rows = [r for r in rows if 0 < r["delay"] <= max_delay]

    search = _first(params, "search")
    if search:
        rows = [r for r in rows if search in r["ip"]]

    # location 筛选依赖地理文字，只对当前候选集解析（都是缓存读取，不联网）
    location_kw = _first(params, "location")
    if location_kw:
        rows = [r for r in rows
                if location_kw in _location_of(r["ip"], r["country"])]

    asc = _first(params, "asc", "1") == "1"
    if _first(params, "sort", "delay") == "delay":
        # 主键把"延迟未知"(0)一律压到最后，次键才是真正的升/降序，
        # 否则降序时一堆 0 会顶在最前面
        rows.sort(key=lambda r: (r["delay"] <= 0, r["delay"] if asc else -r["delay"]))
    else:
        rows.sort(key=lambda r: r["last_check"], reverse=not asc)

    limit = _as_int(_first(params, "limit"), DEFAULT_LIMIT, low=1, high=MAX_LIMIT)
    total = len(rows)
    pages = max(1, (total + limit - 1) // limit)
    page = _as_int(_first(params, "page"), 1, low=1, high=pages)
    window = rows[(page - 1) * limit: page * limit]

    proxies = [{
        "ip": r["ip"], "port": r["port"], "protocol": r["protocol"],
        "delay": r["delay"], "last_check": r["last_check"],
        "location": _location_of(r["ip"], r["country"]),
    } for r in window]

    return {"total_matched": total, "page": page, "pages": pages, "proxies": proxies}


def query_stats():
    rows, by_country = snapshot()
    return {"total": len(rows),
            "regions": {code: len(items) for code, items in by_country.items()}}


def query_countries():
    _, by_country = snapshot()
    countries = [{
        "code": code,
        "name": geo.COUNTRY_CODE.get(code, "未知" if code == UNKNOWN else code),
        "count": len(items),
    } for code, items in by_country.items()]
    countries.sort(key=lambda c: (-c["count"], c["code"]))
    return {"countries": countries}


# ────────────────────────── HTTP ──────────────────────────

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "proxy-tool-web"
    sys_version = ""

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, content_type):
        if isinstance(body, str):
            body = body.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, payload, code=200):
        self._send(code, json.dumps(payload, ensure_ascii=False),
                   "application/json; charset=utf-8")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        params = urllib.parse.parse_qs(parsed.query)

        try:
            if path == "/api/stats":
                return self._json(query_stats())
            if path == "/api/countries":
                return self._json(query_countries())
            if path.startswith("/api/country/"):
                code = urllib.parse.unquote(path[len("/api/country/"):])
                return self._json(query_country(code, params))
            if path.startswith("/api/"):
                return self._json({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001 - 单个请求出错不该打挂服务
            log(f"{path} 处理失败: {exc.__class__.__name__}: {exc}")
            return self._json({"error": str(exc)}, 500)

        return self._serve_static(path)

    def _serve_static(self, path):
        rel = "index.html" if path == "/" else path.lstrip("/")
        target = os.path.realpath(os.path.join(STATIC_DIR, rel))
        root = os.path.realpath(STATIC_DIR)
        # 防路径穿越
        if target != root and not target.startswith(root + os.sep):
            return self._send(403, b"forbidden", "text/plain; charset=utf-8")
        if not os.path.isfile(target):
            # SPA 兜底：非静态资源的路径一律回首页
            target = os.path.join(root, "index.html")
            if not os.path.isfile(target):
                return self._send(404, b"not found", "text/plain; charset=utf-8")
        with open(target, "rb") as fp:
            body = fp.read()
        ext = os.path.splitext(target)[1].lower()
        self._send(200, body, MIME.get(ext, "application/octet-stream"))


def main():
    log(f"数据源 redis://{REDIS_HOST}:{REDIS_PORT}/{PROXY_POOL_DB} hash={TABLE_NAME}")
    if not os.path.isfile(os.path.join(STATIC_DIR, "index.html")):
        log(f"⚠️  {STATIC_DIR}/index.html 不存在，页面将无法访问")

    # geo 的后台填充线程：把 geo:queue 里的 IP 慢慢做在线解析写进缓存。
    # 渲染路径只读缓存，所以在线查询的延迟不会打到请求上。
    try:
        geo.start_filler()
        log("geo 后台填充线程已启动")
    except Exception as exc:  # noqa: BLE001
        log(f"geo 后台填充线程启动失败（地理信息会退化为离线库）: {exc}")

    server = ThreadingHTTPServer((WEB_BIND, WEB_PORT), Handler)
    server.daemon_threads = True
    log(f"监听 {WEB_BIND}:{WEB_PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
