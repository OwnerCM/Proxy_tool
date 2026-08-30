#!/usr/bin/env python3
"""bridge — 把 proxy_pool 抓到的代理导入 dashboard 的 Redis(DB 1)。

背景
----
dashboard 的 backend.py 里有 ``jhao_map()``，会去读 proxy_pool 的 DB 0
``use_proxy`` 哈希做字段补全，但它只对**已经存在于 DB 1 ``proxies:pool``**
的成员生效。也就是说 proxy_pool 抓到的代理默认不会出现在看板里。
bridge 就是补这一段：周期性从 proxy_pool 的 HTTP API 拉取代理列表，
把新代理写进 DB 1，之后交给 dashboard 自己的 validator 去验证/定位/淘汰。

行为约定
--------
* 只插入 DB 1 中**尚不存在**的代理，绝不覆盖 validator 已经写好的
  latency / country / location 等字段。
* 不做验证。延迟、地理位置一律留给 validator 和 geo 模块。
* 只读 proxy_pool（GET /all/），不会调用它的 /delete/ 接口，
  因此不会影响 proxy_pool 自身的池子。

环境变量
--------
PROXY_API           proxy_pool 的 API 根地址，如 http://proxy-pool:5010
                    留空则本模块直接退出（不算错误）
REDIS_HOST          默认 redis（compose 里 Redis 的服务名）
REDIS_PORT          默认 6379
REDIS_DB            默认 1（dashboard 的库）
BRIDGE_INTERVAL     同步间隔秒数，默认 60
BRIDGE_CREDIT       新代理的初始信用分，默认 20（与 new_fetcher.py 一致）
BRIDGE_TIMEOUT      调用 proxy_pool API 的超时秒数，默认 15
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

import redis

KEY_POOL = "proxies:pool"
PFX_PROXY = "proxy:"
SOURCE_TAG = "proxy_pool"

PROXY_API = os.environ.get("PROXY_API", "").strip().rstrip("/")
REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
REDIS_DB = int(os.environ.get("REDIS_DB", "1"))
INTERVAL = max(10, int(os.environ.get("BRIDGE_INTERVAL", "60")))
CREDIT = int(os.environ.get("BRIDGE_CREDIT", "20"))
TIMEOUT = int(os.environ.get("BRIDGE_TIMEOUT", "15"))

BAD_HOSTS = {"0.0.0.0", "127.0.0.1", "localhost", "::1"}


def log(msg):
    print(f"[bridge] {msg}", flush=True)


def _redis():
    return redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        db=REDIS_DB,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
    )


def fetch_from_proxy_pool():
    """GET {PROXY_API}/all/ ，返回 [{proxy, https, region, source}, ...]。"""
    url = f"{PROXY_API}/all/"
    req = urllib.request.Request(url, headers={"User-Agent": "proxy-pool-bridge/1.0"})
    # 显式用不带代理的 opener，避免容器里 HTTP_PROXY 之类的变量把请求绕出去
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=TIMEOUT) as resp:
        payload = json.loads(resp.read().decode("utf-8", "replace"))
    if not isinstance(payload, list):
        raise ValueError(f"unexpected payload type: {type(payload).__name__}")
    return payload


def normalize(entry):
    """proxy_pool 的 Proxy.to_dict -> dashboard 的 hash 字段。

    返回 (proxy_str, mapping)，无法识别时返回 (None, None)。
    """
    if not isinstance(entry, dict):
        return None, None
    proxy_str = str(entry.get("proxy") or "").strip()
    if ":" not in proxy_str:
        return None, None
    host, _, port = proxy_str.rpartition(":")
    if not host or host in BAD_HOSTS or not port.isdigit():
        return None, None
    if not 1 <= int(port) <= 65535:
        return None, None

    # proxy_pool 不记录 socks，只区分是否支持 https（即是否支持 CONNECT）
    protocol = "https" if entry.get("https") else "http"

    upstream_source = str(entry.get("source") or "").strip()
    source = f"{SOURCE_TAG}/{upstream_source}" if upstream_source else SOURCE_TAG

    return proxy_str, {
        "ip": host,
        "port": port,
        "protocol": protocol,
        "source": source,
        # 地理位置只做透传，country 留空交给 geo.py 解析，避免写入脏值
        "location": str(entry.get("region") or "").strip(),
        "country": "",
        "latency": "0",
    }


def sync_once(r):
    """执行一轮同步，返回 (拉取总数, 新增数)。"""
    entries = fetch_from_proxy_pool()

    candidates = {}
    for entry in entries:
        proxy_str, mapping = normalize(entry)
        if proxy_str:
            candidates[proxy_str] = mapping
    if not candidates:
        return len(entries), 0

    keys = list(candidates)
    pipe = r.pipeline(transaction=False)
    for proxy_str in keys:
        pipe.exists(f"{PFX_PROXY}{proxy_str}")
    already = pipe.execute()

    fresh = [k for k, exists in zip(keys, already) if not exists]
    if not fresh:
        return len(entries), 0

    pipe = r.pipeline(transaction=False)
    for proxy_str in fresh:
        pipe.zadd(KEY_POOL, {proxy_str: CREDIT})
        pipe.hset(f"{PFX_PROXY}{proxy_str}", mapping=candidates[proxy_str])
    pipe.execute()

    return len(entries), len(fresh)


def main():
    once = "--once" in sys.argv[1:]

    if not PROXY_API:
        log("PROXY_API 未设置，bridge 不启用（这是正常的，不是错误）")
        return 0

    log(f"start — api={PROXY_API} redis={REDIS_HOST}:{REDIS_PORT}/{REDIS_DB} interval={INTERVAL}s")
    r = _redis()

    while True:
        started = time.time()
        try:
            total, added = sync_once(r)
            log(f"synced: fetched={total} new={added} pool={r.zcard(KEY_POOL)} "
                f"({time.time() - started:.1f}s)")
        except urllib.error.URLError as exc:
            log(f"proxy_pool API 不可达: {exc.reason}")
        except redis.RedisError as exc:
            log(f"redis 异常: {exc}")
        except Exception as exc:  # noqa: BLE001 - 守护进程不能因单轮失败退出
            log(f"同步失败: {exc.__class__.__name__}: {exc}")

        if once:
            return 0
        time.sleep(max(1, INTERVAL - (time.time() - started)))


if __name__ == "__main__":
    sys.exit(main())
