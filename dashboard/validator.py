#!/usr/bin/env python3
"""验证引擎 — 只留 S 级 (<500ms)。直连测试，慢的直接删。"""

import redis, time, socket, threading, sys, os, datetime, json, struct
from concurrent.futures import ThreadPoolExecutor, as_completed

KEY_POOL = "proxies:pool"
PFX_PROXY = "proxy:"
CHECK_INTERVAL = 30
HARVEST_INTERVAL = 1800
VALIDATE_THREADS = 50
VALIDATE_TIMEOUT = 2
CREDIT_MAX = 50
SUBMIT_CHUNK = 1000
S_LATENCY_MAX = 500  # 只留 <500ms

POOL = redis.ConnectionPool(
    host=os.environ.get("REDIS_HOST", "proxy-redis"), port=6379, db=1,
    max_connections=VALIDATE_THREADS * 4,
    socket_connect_timeout=5, socket_timeout=5,
    decode_responses=True,
)
REDIS = redis.Redis(connection_pool=POOL)

# ── GeoIP ──
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import geo as geo_resolver
except Exception:
    geo_resolver = None

def resolve_geo_and_store(ip, proxy_key, force=False):
    if not geo_resolver:
        return "?", "?"
    try:
        data = geo_resolver.resolve_and_store(ip, proxy_key=proxy_key, force=force)
        if data:
            return data.get("countryCode", "?") or "?", geo_resolver.resolve(ip)
        return geo_resolver.resolve_region(ip), geo_resolver.resolve(ip)
    except Exception:
        return "?", "?"

# ── 信用分 Lua ──
CREDIT_SCRIPT = REDIS.register_script("""
    local s = tonumber(redis.call('ZINCRBY', KEYS[1], ARGV[1], ARGV[2]))
    if not s then return 0 end
    if s < 0 then
        redis.call('ZREM', KEYS[1], ARGV[2])
        redis.call('DEL', KEYS[2] .. ARGV[2])
        return 1
    end
    if s > tonumber(ARGV[3]) then
        redis.call('ZADD', KEYS[1], ARGV[3], ARGV[2])
    end
    return 0
""")

def credit_add(proxy, delta):
    return CREDIT_SCRIPT(keys=[KEY_POOL, PFX_PROXY], args=[delta, proxy, CREDIT_MAX]) == 1

def proxy_count():
    return REDIS.zcard(KEY_POOL) or 0

# ── 探活 ──
TARGET_HOST = "www.qq.com"
TARGET_IP = "103.7.30.123"

def http_test(ip, port, timeout=VALIDATE_TIMEOUT):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            t0 = time.time()
            s.connect((ip, int(port)))
            s.send(f"GET http://{TARGET_HOST}/ HTTP/1.1\r\nHost: {TARGET_HOST}\r\nConnection: close\r\n\r\n".encode())
            resp = b""
            while True:
                try:
                    chunk = s.recv(4096)
                    if not chunk: break
                    resp += chunk
                except Exception: break
            lat = int((time.time() - t0) * 1000)
            if resp and (b"HTTP/" in resp or b"html" in resp.lower() or len(resp) > 200):
                return min(lat, 9999)
    except Exception:
        pass
    return None

def socks4_test(ip, port):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(VALIDATE_TIMEOUT)
            t0 = time.time()
            s.connect((ip, int(port)))
            target_bytes = socket.inet_aton(TARGET_IP)
            req = b"\x04\x01" + struct.pack(">H", 80) + target_bytes + b"\x00"
            s.send(req)
            resp = s.recv(8)
            lat = int((time.time() - t0) * 1000)
            if len(resp) >= 2 and resp[1] == 0x5a:
                return min(lat, 9999)
    except Exception:
        pass
    return None

def socks5_test(ip, port):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(VALIDATE_TIMEOUT)
            t0 = time.time()
            s.connect((ip, int(port)))
            s.send(b"\x05\x01\x00")
            resp = s.recv(2)
            if len(resp) < 2 or resp[0] != 5 or resp[1] != 0:
                return None
            target_bytes = socket.inet_aton(TARGET_IP)
            req = b"\x05\x01\x00\x01" + target_bytes + struct.pack(">H", 80)
            s.send(req)
            resp = s.recv(10)
            lat = int((time.time() - t0) * 1000)
            if len(resp) >= 2 and resp[0] == 5 and resp[1] == 0:
                return min(lat, 9999)
    except Exception:
        pass
    return None

def _remove_proxy(proxy_str):
    REDIS.zrem(KEY_POOL, proxy_str)
    REDIS.delete(f"{PFX_PROXY}{proxy_str}")

# ── 验证单条 ──
def validate_one(proxy_str, meta):
    if not meta: return ("skipped", None)
    ip = meta.get("ip", "")
    port_str = meta.get("port", "")
    proto = (meta.get("protocol") or "?").lower()
    if not ip: ip = proxy_str.split(":")[0]
    if not port_str:
        parts = proxy_str.split(":")
        port_str = parts[1] if len(parts) > 1 else "0"
    try: port = int(port_str)
    except Exception: return ("skipped", None)

    # ── 协议自动探测 ──
    proto_changed = False
    if proto == "?":
        lat = http_test(ip, port)
        if lat is not None: proto = "http"; proto_changed = True
        else:
            lat = socks5_test(ip, port)
            if lat is not None: proto = "socks5"; proto_changed = True
            else:
                lat = socks4_test(ip, port)
                if lat is not None: proto = "socks4"; proto_changed = True
    elif proto.startswith("socks5"):
        lat = socks5_test(ip, port)
    elif proto.startswith("socks4"):
        lat = socks4_test(ip, port)
    else:
        lat = http_test(ip, port)
        if lat is None:
            lat2 = socks5_test(ip, port)
            if lat2 is not None: lat = lat2; proto = "socks5"; proto_changed = True
            else:
                lat2_2 = socks4_test(ip, port)
                if lat2_2 is not None: lat = lat2_2; proto = "socks4"; proto_changed = True

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if lat is not None:
        if lat < S_LATENCY_MAX:
            # ✅ S 级 — 直接留
            country_cc, city = resolve_geo_and_store(ip, proxy_str, force=False)
            hset_args = {
                "latency": str(lat), "last_check": now, "success_rate": "100",
                "country": country_cc, "location": city
            }
            if proto_changed: hset_args["protocol"] = proto
            REDIS.hset(f"{PFX_PROXY}{proxy_str}", mapping=hset_args)
            credit_add(proxy_str, 5)
            return ("ok_s", "S")
        else:
            # ❌ >= 500ms: 直接删除
            _remove_proxy(proxy_str)
            return ("removed", None)
    else:
        # 直连失败: 直接删除
        if geo_resolver:
            try: geo_resolver.resolve(ip)
            except Exception: pass
        _remove_proxy(proxy_str)
        return ("removed", None)

# ── 批量验证 ──
def validate_all(executor):
    proxies = REDIS.zrange(KEY_POOL, 0, -1)
    total = len(proxies)
    if total == 0:
        print("[validate] pool empty"); return

    results = {"ok_s": 0, "fail": 0, "removed": 0,
               "slow": 0, "skipped": 0, "S": 0}
    now_ts = time.time()
    to_check = []; skipped = 0

    for i in range(0, total, SUBMIT_CHUNK):
        chunk = proxies[i:i + SUBMIT_CHUNK]
        pipe = REDIS.pipeline(transaction=False)
        for p in chunk: pipe.hgetall(f"{PFX_PROXY}{p}")
        metas = pipe.execute()
        for p, meta in zip(chunk, metas):
            if not meta: to_check.append((p, meta)); continue
            lc = meta.get("last_check", "")
            lat_str = meta.get("latency", "")
            if lat_str and lat_str not in ("9999", "", "0") and lc:
                try:
                    ts = datetime.datetime.strptime(lc, "%Y-%m-%d %H:%M:%S").timestamp()
                    if float(lat_str) < S_LATENCY_MAX and now_ts - ts < 300:
                        skipped += 1; continue
                except Exception: pass
            to_check.append((p, meta))

    results["skipped"] = skipped
    print(f"[validate] {total} total → skip={skipped} check={len(to_check)}")
    if not to_check: return

    futures = {executor.submit(validate_one, p, meta): p for p, meta in to_check}
    checked = 0
    for f in as_completed(futures):
        try:
            status, grade = f.result(); checked += 1
            if status == "ok_s": results["ok_s"] += 1; results["S"] += 1
            elif status == "removed": results["removed"] += 1
            elif status == "slow": results["slow"] += 1
            elif status == "fail": results["fail"] += 1
        except Exception: results["fail"] += 1

    print(f"[validate] checked={checked} skip={skipped} "
          f"S_direct={results['ok_s']} "
          f"fail={results['fail']} removed={results['removed']} "
          f"slow={results['slow']} total_S={results['S']}")

# ── 采集 ──
def harvest_new_proxies():
    before = proxy_count()
    try:
        fetcher_path = os.path.join(os.path.dirname(__file__), "new_fetcher.py")
        if not os.path.exists(fetcher_path): return 0
        import subprocess
        result = subprocess.run(
            [sys.executable, fetcher_path], capture_output=True, text=True, timeout=600,
            env={**os.environ, "REDIS_HOST": os.environ.get("REDIS_HOST", "proxy-redis")}
        )
        if result.returncode != 0: print(f"[harvest] exit={result.returncode}")
        if result.stdout:
            for line in result.stdout.strip().split("\n")[-5:]: print(f"[harvest] {line}")
        if result.stderr:
            for line in result.stderr.strip().split("\n")[-3:]: print(f"[harvest:err] {line}")
        after = proxy_count()
        return max(0, after - before)
    except Exception as e:
        print(f"[harvest] error: {e}"); return 0

def _harvest_wrapper():
    try:
        added = harvest_new_proxies()
        print(f"[engine] harvest done: +{added}")
    except Exception as e:
        print(f"[engine] harvest error: {e}")

# ── 主循环 ──
def main():
    print(f"[engine] start — pool={proxy_count()} (S<{S_LATENCY_MAX}ms only)")
    last_harvest = time.time()
    harvest_thread = None
    executor = ThreadPoolExecutor(max_workers=VALIDATE_THREADS, thread_name_prefix="val")
    try:
        while True:
            try:
                now = time.time()
                if now - last_harvest > HARVEST_INTERVAL:
                    if harvest_thread is None or not harvest_thread.is_alive():
                        harvest_thread = threading.Thread(target=_harvest_wrapper, daemon=True)
                        harvest_thread.start()
                        last_harvest = now
                t0 = time.time()
                validate_all(executor)
                elapsed = time.time() - t0
                sleep_for = max(0, CHECK_INTERVAL - elapsed)
                print(f"[engine] round={elapsed:.1f}s sleep={sleep_for:.1f}s")
                if sleep_for > 0: time.sleep(sleep_for)
            except Exception as e:
                print(f"[engine] error: {e}")
                time.sleep(CHECK_INTERVAL)
    finally:
        executor.shutdown(wait=True)
        if harvest_thread and harvest_thread.is_alive():
            harvest_thread.join(timeout=5)

if __name__ == "__main__":
    main()
