# -*- coding: utf-8 -*-
"""
-------------------------------------------------
   File Name：     github_lists.py
   Description :   GitHub 上的公开代理列表源（每个仓库一个 fetcher）
-------------------------------------------------
"""

import os
import random

from fetcher.baseFetcher import BaseFetcher
from handler.logHandler import LogHandler
from util.webRequest import WebRequest

logger = LogHandler("fetcher")

# ── 为什么要限流 ──
# 下面这些列表加起来有一万多条，而且绝大多数是死的。proxy_pool 的调度是每 5 分钟
# 采集一轮、每 2 分钟全量复检一轮，校验线程默认 20 个、单个超时 10s。
# 若把一万多条一次性灌进校验队列，单轮要跑一个多小时，远超调度间隔，
# APScheduler 的任务会不断堆叠（max_instances=10），最终把池子拖死。
#
# 所以每个源每轮只随机取 GITHUB_FETCH_LIMIT 条。随机采样的好处是多轮下来能把
# 整个列表都覆盖到，而不是每次都盯着开头那几条。
#
# 粗略的吞吐account：单源 150 条 × 10 个源 = 1500 条/轮，
# 免费代理里约六成会立刻连接失败、三成会耗到超时，均摊约 3s，
# 1500 × 3 / 20 线程 ≈ 225s，刚好落在 5 分钟的采集间隔内。
# 想放大就同时调 GITHUB_FETCH_LIMIT 与 PROXY_CHECK_THREADS，
# 或把 setting.py 里的 VERIFY_TIMEOUT 从 10 降到 5。
FETCH_LIMIT = int(os.environ.get("GITHUB_FETCH_LIMIT", "150") or 150)

# 仓库 -> raw 列表 URL。
# 只收 HTTP/HTTPS 列表：proxy_pool 的校验器用 requests 的 http/https proxies 参数，
# 验不了 SOCKS，导进来只会全部失败然后被删掉，纯属浪费校验预算。
# 代理是 http 还是 https 由 proxy_pool 自己校验后写入 https 字段，
# 所以这里不需要（也不应该）沿用源站标注的协议。
SOURCES = {
    "thespeedx": [
        "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    ],
    "jetkai": [
        "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-http.txt",
        "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-https.txt",
    ],
    "shiftytr": [
        "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt",
        "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/https.txt",
    ],
    "sunny9577": [
        "https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/proxies.txt",
    ],
    "themiralay": [
        "https://raw.githubusercontent.com/themiralay/Proxy-List-World/master/data.txt",
    ],
    "aliilapro": [
        "https://raw.githubusercontent.com/ALIILAPRO/Proxy/main/http.txt",
    ],
    "rdavydov": [
        "https://raw.githubusercontent.com/rdavydov/proxy-list/main/proxies/http.txt",
    ],
    "vakhov": [
        "https://raw.githubusercontent.com/vakhov/fresh-proxy-list/master/http.txt",
    ],
    "monosans": [
        "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    ],
    "roosterkid": [
        "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS.txt",
    ],
}

# 实测已失效、故未收录（2026-08 复核）：
#   mertguvencli/http-proxy-list  -> raw 路径 404
#   mmpx12/proxy-list             -> raw 路径 404
# 另外 jetkai 的 proxies.txt 是 http/https/socks 的合集，与上面两条 http/https
# 专用列表重复且含 socks，故不收。


class _GithubListFetcher(BaseFetcher):
    """GitHub 代理列表源基类。

    name 留空，_discover_fetchers 会跳过它（它只检查 name 非空的类），
    真正被加载的是文件末尾按 SOURCES 生成的那些子类。
    """

    name = ""
    urls = []

    def fetch(self):
        collected = []
        for url in self.urls:
            try:
                # retry_time=1：WebRequest 默认失败重试 3 次、每次间隔 5s，
                # 十来个 URL 叠加起来会让采集轮次拖很久，这里不值得等
                text = WebRequest().get(url, timeout=15, retry_time=1,
                                        retry_interval=1).text
                collected.extend(self.parseProxiesFromText(text))
            except Exception as e:
                logger.error("ProxyFetch - %s: %s - %s" % (self.name, url, e))

        # 同一仓库的多个列表之间会有重叠，先去重再采样
        unique = list(dict.fromkeys(collected))
        if FETCH_LIMIT > 0 and len(unique) > FETCH_LIMIT:
            unique = random.sample(unique, FETCH_LIMIT)
        for proxy in unique:
            yield proxy


def _make_fetcher(repo, urls):
    class_name = "Github%sFetcher" % repo.capitalize()
    return class_name, type(class_name, (_GithubListFetcher,), {
        "name": "github-%s" % repo,
        "url": "https://github.com/",
        "enabled": True,
        "urls": urls,
        "__doc__": "GitHub 代理列表: %s" % repo,
    })


# 每个仓库生成一个独立的 fetcher 类，好处有两个：
#   1. source 字段能记到具体仓库，便于回头评估哪个源产出高
#   2. proxy_pool 给每个 fetcher 分配一个线程，天然并行
for _repo, _urls in SOURCES.items():
    _name, _cls = _make_fetcher(_repo, _urls)
    globals()[_name] = _cls
del _repo, _urls, _name, _cls
