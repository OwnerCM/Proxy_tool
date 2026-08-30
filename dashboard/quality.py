#!/usr/bin/env python3
"""
IP 质量检测 + 风险评分 — 对代理池做深度检测，过滤高风险 IP。

检测项：
  1. HTTP/HTTPS 连通性 — 通过代理请求 httpbin.org/ip
  2. 匿名级别 — X-Forwarded-For / Via 等泄露头
  3. DNS 黑名单 — Spamhaus/Barracuda/SORBS 反查
  4. IP 类型 — 机房(datacenter) vs 住宅(residential) vs 移动
  5. 内容劫持 — 广告/JS 注入检测
  6. 延迟 — 响应时间

风险评分规则：
  - 机房 IP -25 分（高滥用风险）
  - DNSBL 在黑名单 -30 分
  - 透明代理 -30 分
  - 内容劫持 -20 分

结果写回 Redis：quality_score / anonymity / risk_level / ip_type / quality_checked_at
"""

import json, time, sys, os, re, socket, ssl
import urllib.request
import urllib.error
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import redis

# ── 配置 ──
REDIS_HOST = os.environ.get("REDIS_HOST", "proxy-redis")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
POOL = redis.ConnectionPool(
    host=REDIS_HOST, port=REDIS_PORT, db=1,
    max_connections=40,
    socket_connect_timeout=5, socket_timeout=5,
    decode_responses=True,
)
R = redis.Redis(connection_pool=POOL)

KEY_POOL = "proxies:pool"
PFX_PROXY = "proxy:"
CHECK_INTERVAL = 120       # 每 2 分钟一轮
QUALITY_THREADS = 30       # 并发数
QUALITY_TIMEOUT = 8        # 单次检测超时
TEST_URL = "http://httpbin.org/ip"
TEST_URL_HTTPS = "https://httpbin.org/ip"
MIN_QUALITY_SCORE = 50     # 低于此分删除（含风险扣分）

# ── 匿名级别定义 ──
ANON_ELITE = "elite"       # 不泄露任何头
ANON_ANONYMOUS = "anon"    # 泄露 Via 但无真实 IP
ANON_TRANSPARENT = "trans" # 泄露真实 IP（X-Forwarded-For 等）

# 广告/劫持特征
INJECT_PATTERNS = [
    rb'<script[^>]*src=["\']?http',   # 注入的 script 标签
    rb'adsbygoogle',
    rb'popup',
    rb'popunder',
    rb'alert\(',
    rb'document\.write',
    rb'<!-- ad -->',
    rb'<iframe[^>]*src=',
]

# ── DNSBL 黑名单服务器 ──
DNSBL_SERVERS = [
    "zen.spamhaus.org",          # Spamhaus 综合
    "b.barracudacentral.org",    # Barracuda
    "dnsbl.sorbs.net",           # SORBS
    "bl.spamcop.net",            # SpamCop
    "all.s5h.net",               # S5h
]

# ── IP 类型 ──
IP_TYPE_DC = "datacenter"
IP_TYPE_RESIDENTIAL = "residential"
IP_TYPE_MOBILE = "mobile"
IP_TYPE_UNKNOWN = "unknown"

# ── 风险等级 ──
RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"

# ── ip-api.com 速率控制 (免费 45 req/min → 留余量 35) ──
_ip_api_lock = threading.Lock()
_ip_api_last_req = 0.0
IP_API_MIN_INTERVAL = 60.0 / 35  # ~1.7s per request


def _rate_limit_ip_api():
    """阻塞直到可以发送下一个 ip-api 请求"""
    global _ip_api_last_req
    with _ip_api_lock:
        now = time.time()
        wait = _ip_api_last_req + IP_API_MIN_INTERVAL - now
        if wait > 0:
            time.sleep(wait)
        _ip_api_last_req = time.time()


def _reverse_ip(ip):
    """反转 IP 用于 DNSBL 查询：1.2.3.4 → 4.3.2.1"""
    parts = ip.split(".")
    if len(parts) != 4:
        return None
    return ".".join(reversed(parts))


def check_dnsbl(ip, timeout=3):
    """
    DNS 黑名单查询。返回 (listed_count, listed_servers)
    DNSBL 原理：查询 <reversed-ip>.<dnsbl-server>，有 A 记录 = 在黑名单
    """
    rev = _reverse_ip(ip)
    if not rev:
        return 0, []

    listed = []
    for server in DNSBL_SERVERS:
        query = f"{rev}.{server}"
        try:
            socket.setdefaulttimeout(timeout)
            result = socket.getaddrinfo(query, 0, socket.AF_INET, socket.SOCK_STREAM)
            if result:
                listed.append(server)
        except (socket.gaierror, socket.timeout, OSError):
            pass

    return len(listed), listed


def check_ip_type(ip, timeout=5):
    """
    判断 IP 类型：机房/住宅/移动。
    优先用 Redis geo:{ip} 缓存（isp/org 等字段），不命中才走 ip-api.com API。
    """
    # 1. 先查 Redis 缓存 geo:{ip}（支持 JSON 字符串和 hash 两种格式）
    try:
        geo_raw = R.get(f"geo:{ip}")
    except Exception:
        geo_raw = None
    
    geo = {}
    if geo_raw:
        try:
            geo = json.loads(geo_raw)
        except (json.JSONDecodeError, TypeError):
            # 可能是 hash 类型，回退
            try:
                geo = R.hgetall(f"geo:{ip}")
            except Exception:
                geo = {}
    if geo and geo.get("isp"):
        isp = geo.get("isp", "")
        org = geo.get("org", "")
        as_name = geo.get("as", "")
        is_hosting = geo.get("hosting") == "true"
        is_mobile = geo.get("mobile") == "true"
        
        if is_mobile:
            return IP_TYPE_MOBILE, {"isp": isp, "org": org, "as": as_name, "source": "redis"}
        if is_hosting:
            return IP_TYPE_DC, {"isp": isp, "org": org, "as": as_name, "source": "redis"}
        
        dc_keywords = [
            "amazon", "google", "microsoft", "azure", "digitalocean",
            "vultr", "linode", "hetzner", "ovh", "tencent", "alibaba",
            "aliyun", "oracle", "cloudflare", "fastly", "akamai",
            "choopa", "psychz", "colocrossing", "datacamp",
            "hosting", "data center", "datacenter", "dedicated",
            "rackspace", "softlayer", "leaseweb", "online.net",
        ]
        combined = f"{isp} {org} {as_name}".lower()
        if any(kw in combined for kw in dc_keywords):
            return IP_TYPE_DC, {"isp": isp, "org": org, "as": as_name, "source": "redis"}
        return IP_TYPE_RESIDENTIAL, {"isp": isp, "org": org, "as": as_name, "source": "redis"}

    # 2. 回退：Redis 无缓存 → 调 ip-api API（有速率限制）
    _rate_limit_ip_api()
    try:
        url = f"http://ip-api.com/json/{ip}?fields=isp,org,as,mobile,proxy,hosting"
        req = urllib.request.Request(url, headers={"User-Agent": "proxy-pool-quality/1.0"})
        resp = urllib.request.urlopen(req, timeout=timeout)
        data = json.loads(resp.read())

        if data.get("status") != "success":
            return IP_TYPE_UNKNOWN, {}

        is_hosting = data.get("hosting", False)
        is_mobile = data.get("mobile", False)
        isp = data.get("isp", "")
        org = data.get("org", "")
        as_name = data.get("as", "")

        if is_mobile:
            ip_type = IP_TYPE_MOBILE
        elif is_hosting:
            ip_type = IP_TYPE_DC
        else:
            dc_keywords = [
                "amazon", "google", "microsoft", "azure", "digitalocean",
                "vultr", "linode", "hetzner", "ovh", "tencent", "alibaba",
                "aliyun", "oracle", "cloudflare", "fastly", "akamai",
                "choopa", "psychz", "colocrossing", "datacamp",
                "hosting", "data center", "datacenter", "dedicated",
                "rackspace", "softlayer", "leaseweb", "online.net",
            ]
            combined = f"{isp} {org} {as_name}".lower()
            ip_type = IP_TYPE_DC if any(kw in combined for kw in dc_keywords) else IP_TYPE_RESIDENTIAL

        return ip_type, {"isp": isp, "org": org, "as": as_name,
                         "hosting": is_hosting, "mobile": is_mobile,
                         "is_datacenter": ip_type == IP_TYPE_DC, "source": "ip-api"}

    except Exception as e:
        return IP_TYPE_UNKNOWN, {"error": str(e)[:100], "source": "error"}


def assess_risk_ip(ip, timeout=5):
    """
    综合风险评估：DNSBL + IP类型。
    返回 {"risk_level": str, "risk_score_deduction": int, "dnsbl_count": int, "ip_type": str, "details": dict}
    """
    result = {"risk_level": RISK_LOW, "risk_score_deduction": 0,
              "dnsbl_count": 0, "dnsbl_listed": [], "ip_type": IP_TYPE_UNKNOWN,
              "details": {}}

    # ── DNSBL 检查 ──
    dnsbl_count, dnsbl_listed = check_dnsbl(ip, timeout=timeout)
    result["dnsbl_count"] = dnsbl_count
    result["dnsbl_listed"] = dnsbl_listed

    if dnsbl_count >= 2:
        result["risk_score_deduction"] += 30
        result["risk_level"] = RISK_HIGH
    elif dnsbl_count == 1:
        result["risk_score_deduction"] += 15
        result["risk_level"] = RISK_MEDIUM

    # ── IP 类型检测 ──
    ip_type, details = check_ip_type(ip, timeout=timeout)
    result["ip_type"] = ip_type
    result["details"] = details

    if ip_type == IP_TYPE_DC:
        result["risk_score_deduction"] += 25
        if result["risk_level"] == RISK_LOW:
            result["risk_level"] = RISK_MEDIUM
    elif ip_type == IP_TYPE_MOBILE:
        result["risk_score_deduction"] -= 10  # 移动 IP 加分（低风险）
        result["risk_level"] = RISK_LOW

    return result


def _proxy_handler(protocol, ip, port):
    """构建 urllib ProxyHandler"""
    proxy_url = f"{protocol}://{ip}:{port}"
    return urllib.request.ProxyHandler({protocol: proxy_url})


def check_http(ip, port, protocol="http", timeout=QUALITY_TIMEOUT):
    """
    通过代理请求 httpbin.org/ip，返回 (ok, detail_dict)
    """
    result = {
        "ip": ip, "port": port, "protocol": protocol,
        "ok": False, "anonymity": None, "latency_ms": 0,
        "returned_ip": None, "leak_headers": [],
        "status_code": 0, "body_size": 0, "injected": False,
        "error": None,
    }

    test_url = TEST_URL_HTTPS if protocol == "https" else TEST_URL

    try:
        proxy = _proxy_handler(protocol, ip, port)
        opener = urllib.request.build_opener(proxy)

        t0 = time.time()
        req = urllib.request.Request(test_url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
        })
        resp = opener.open(req, timeout=timeout)
        result["latency_ms"] = round((time.time() - t0) * 1000)
        result["status_code"] = resp.code

        if resp.code != 200:
            result["error"] = f"HTTP {resp.code}"
            return result

        body = resp.read()
        result["body_size"] = len(body)

        # ── 检查内容劫持 ──
        for pattern in INJECT_PATTERNS:
            if re.search(pattern, body):
                result["injected"] = True
                break

        # ── 解析返回的 IP ──
        try:
            data = json.loads(body)
            result["returned_ip"] = data.get("origin", "").split(",")[0].strip()
        except json.JSONDecodeError:
            result["error"] = "invalid JSON"
            return result

        # ── 检查泄露头 ──
        leak_test_url = "http://httpbin.org/headers"
        req2 = urllib.request.Request(leak_test_url, headers={
            "User-Agent": "Mozilla/5.0",
        })
        try:
            resp2 = opener.open(req2, timeout=timeout)
            headers_data = json.loads(resp2.read())
            resp_headers = headers_data.get("headers", {})

            # 检查常见代理泄露头
            leak_keys = ["X-Forwarded-For", "X-Real-Ip", "X-Real-IP",
                         "X-Client-Ip", "X-Forwarded", "Forwarded",
                         "X-Forwarded-Proto", "X-Forwarded-Host",
                         "Cf-Connecting-Ip", "True-Client-Ip"]
            found_leaks = []
            for key, val in resp_headers.items():
                if key.lower().replace("-", "") in [k.lower().replace("-", "") for k in leak_keys]:
                    found_leaks.append(f"{key}: {val}")

            result["leak_headers"] = found_leaks

            # 判定匿名级别
            if any("forwarded-for" in h.lower() or "real-ip" in h.lower() or "client-ip" in h.lower() or "connecting-ip" in h.lower() or "true-client-ip" in h.lower()
                   for h in found_leaks):
                result["anonymity"] = ANON_TRANSPARENT
            elif found_leaks:
                result["anonymity"] = ANON_ANONYMOUS
            else:
                result["anonymity"] = ANON_ELITE
        except Exception:
            result["anonymity"] = ANON_ANONYMOUS  # 无法检测，保守估计

        result["ok"] = True

    except urllib.error.URLError as e:
        result["error"] = f"URLError: {e.reason}"
    except (socket.timeout, TimeoutError):
        result["error"] = "timeout"
    except ssl.SSLError as e:
        result["error"] = f"SSL: {e}"
    except Exception as e:
        result["error"] = str(e)[:120]

    return result


def score_proxy(check_result, risk=None):
    """根据检测结果 + 风险评估打分 (0-100)"""
    if not check_result["ok"]:
        return 0

    score = 0

    # 连通性 (30分)
    score += 30

    # 匿名级别 (30分)
    anon = check_result["anonymity"]
    if anon == ANON_ELITE:
        score += 30
    elif anon == ANON_ANONYMOUS:
        score += 15
    else:  # transparent
        score += 0

    # 延迟 (20分): <500ms 满分，500-2000ms 递减，>2000ms 0分
    lat = check_result["latency_ms"]
    if lat < 500:
        score += 20
    elif lat < 1000:
        score += 15
    elif lat < 2000:
        score += 10
    elif lat < 4000:
        score += 5

    # 内容纯净 (20分)
    if not check_result["injected"]:
        score += 20

    # HTTPS 加成
    if check_result["protocol"] == "https":
        score = min(100, score + 5)

    # 返回 IP 匹配性 (减分)
    returned_ip = check_result.get("returned_ip", "")
    if returned_ip and returned_ip != check_result["ip"]:
        score -= 20

    # ── 风险评估扣分 ──
    if risk:
        score -= risk.get("risk_score_deduction", 0)

    return max(0, min(100, score))


def check_single(proxy_key):
    """检测单个代理。返回结果 dict 或 None（跳过时）"""
    # 优先读 proxy:ip:port key（真正存数据的地方），ZSET key 没有 ip/port
    data_key = f"proxy:{proxy_key}"
    try:
        hd = R.hgetall(data_key)
    except Exception:
        hd = {}

    if not hd:
        # 回退到 ZSET key 本身
        data_key = proxy_key
        try:
            hd = R.hgetall(data_key)
        except Exception:
            hd = {}

    if not hd:
        # 最后尝试从 key 中提取 IP 和 port
        if "proxy:" in proxy_key:
            raw = proxy_key.replace("proxy:", "")
        else:
            raw = proxy_key
        parts = raw.rsplit(":", 1)
        if len(parts) == 2:
            hd = {"ip": parts[0], "port": parts[1], "protocol": "http"}
        else:
            return None

    ip = hd.get("ip", "")
    port = hd.get("port", "")
    protocol = hd.get("protocol", "http")

    if not ip or not port:
        return None

    # 只测 HTTP/HTTPS
    if protocol not in ("http", "https"):
        return None

    # 已经检测过且分数够的跳过
    existing_score = hd.get("quality_score", "")
    if existing_score and existing_score.isdigit():
        if int(existing_score) >= MIN_QUALITY_SCORE:
            return None  # 已合格，跳过

    result = check_http(ip, port, protocol)

    # 如果 HTTP 失败但协议是 https，试试 http
    if not result["ok"] and protocol == "https":
        result2 = check_http(ip, port, "http")
        if result2["ok"]:
            result = result2
            result["protocol"] = "https->http"

    # ── 风险评估（DNSBL + IP类型）──
    risk = {}
    if result["ok"]:
        risk = assess_risk_ip(ip)
    else:
        # 连通都不过，直接高风险
        risk = {"risk_level": RISK_HIGH, "risk_score_deduction": 50,
                "dnsbl_count": 0, "ip_type": IP_TYPE_UNKNOWN}

    score = score_proxy(result, risk)

    # 写回 Redis
    try:
        R.hset(data_key, mapping={
            "quality_score": str(score),
            "anonymity": result["anonymity"] or "unknown",
            "risk_level": risk.get("risk_level", RISK_HIGH),
            "ip_type": risk.get("ip_type", IP_TYPE_UNKNOWN),
            "dnsbl_count": str(risk.get("dnsbl_count", 0)),
            "quality_checked_at": str(int(time.time())),
        })
    except Exception:
        pass

    # 低分直接删
    if score < MIN_QUALITY_SCORE:
        try:
            R.zrem(KEY_POOL, proxy_key)
            R.delete(data_key)
        except Exception:
            pass

    return {"key": proxy_key, "score": score, "anonymity": result["anonymity"],
            "latency": result["latency_ms"], "error": result["error"],
            "risk_level": risk.get("risk_level"), "ip_type": risk.get("ip_type"),
            "dnsbl_count": risk.get("dnsbl_count")}


def run_once():
    """一轮检测"""
    # 只测已验证代理 (validator_ok=1) + 未检测过质量的
    all_keys = R.zrangebyscore(KEY_POOL, 1, 100)
    if not all_keys:
        print(f"[quality] pool empty, nothing to check")
        return

    # 过滤：必须有 validator_ok 或 latency 或 success_rate
    need_check = []
    skipped = 0
    batch_size = 500  # 每轮最多测 500 个
    for k in all_keys:
        if len(need_check) >= batch_size:
            break
        # 优先读 proxy:IP:PORT key（新数据都在这里）
        alt_key = f"proxy:{k}"
        try:
            hd = R.hgetall(alt_key)
            if not hd:
                hd = R.hgetall(k)
        except Exception:
            try:
                hd = R.hgetall(k)
            except Exception:
                continue
        if not hd:
            continue
        qs = hd.get("quality_score", "")
        if (not qs or not qs.isdigit() or int(qs) < MIN_QUALITY_SCORE):
            need_check.append(k)
        else:
            skipped += 1

    if not need_check:
        print(f"[quality] all {skipped} proxies already checked, skip")
        return

    print(f"[quality] pool={len(all_keys)}, need_check={len(need_check)} (skipped={skipped}), checking...")

    checked = 0
    passed = 0
    failed = 0
    elite = 0
    low_risk = 0
    dc_count = 0

    with ThreadPoolExecutor(max_workers=QUALITY_THREADS) as ex:
        futures = {ex.submit(check_single, k): k for k in need_check}
        for future in as_completed(futures):
            r = future.result()
            if r is None:
                continue
            checked += 1
            if r["score"] >= MIN_QUALITY_SCORE:
                passed += 1
            else:
                failed += 1
            if r["anonymity"] == "elite":
                elite += 1
            if r.get("risk_level") == RISK_LOW:
                low_risk += 1
            if r.get("ip_type") == IP_TYPE_DC:
                dc_count += 1

    pct = (passed / checked * 100) if checked else 0
    print(f"[quality] done: checked={checked} passed={passed}({pct:.0f}%) "
          f"failed={failed} elite={elite} low_risk={low_risk} dc={dc_count} pool={len(all_keys)}")

    # ── 批量清理已验证的死代理（有 quality_score=0 但没被 check_single 自动删的）──
    if failed > 0:
        _purge_dead_proxies(all_keys[:500])


def _purge_dead_proxies(keys_sample):
    """清理池中的死代理：quality_score=0 且 latency 为空或 >5000ms"""
    dead = 0
    for k in keys_sample:
        try:
            hd = R.hgetall(f"proxy:{k}")
            if not hd:
                hd = R.hgetall(k)
        except Exception:
            continue
        qs = hd.get("quality_score", "")
        if qs == "0":
            try:
                R.zrem(KEY_POOL, k)
                R.delete(f"proxy:{k}", k)
                dead += 1
            except Exception:
                pass
    if dead > 0:
        print(f"[quality] purged {dead} dead proxies")


def main():
    print(f"[quality] starting, interval={CHECK_INTERVAL}s, "
          f"threads={QUALITY_THREADS}, min_score={MIN_QUALITY_SCORE}")
    while True:
        try:
            run_once()
        except Exception as e:
            print(f"[quality] error: {e}")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
