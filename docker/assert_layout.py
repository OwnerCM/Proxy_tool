#!/usr/bin/env python3
"""构建期自检 —— 由 Dockerfile 与 CI 共同调用，也可以在本地直接跑。

检查的是"改了代码之后还成立吗"这类前提，出问题时让构建立刻失败，
而不是等到运行时才静默降级（ip2region 那条链路就是这么被藏了很久的）。

    python docker/assert_layout.py proxy_pool   # 在 proxy_pool 源码目录下运行
    cd dashboard && python ../docker/assert_layout.py web
    python docker/assert_layout.py collisions   # 在仓库根目录运行

断言都相对当前工作目录，不写死绝对路径，因此本地和镜像里跑的是同一套逻辑。
"""

import os
import sys


def fail(msg):
    print(f"❌ 自检失败: {msg}", file=sys.stderr)
    raise SystemExit(1)


def check_proxy_pool():
    """proxy_pool 侧：CLI 可用、采集源能被发现、latency 字段在。"""
    from helper.proxy import Proxy

    if "latency" not in Proxy("1.2.3.4:8080").to_dict:
        fail("Proxy.to_dict 缺少 latency 字段 —— 看板的延迟列与 gateway 择优会失效")

    from handler.configHandler import ConfigHandler
    from helper.fetch import _discover_fetchers

    fetchers = _discover_fetchers(ConfigHandler().fetcherExclude)
    names = [c.name for c in fetchers]
    github = [n for n in names if n.startswith("github-")]
    if not github:
        fail("没有发现任何 github-* 采集源，fetcher/sources/github_lists.py 可能已失效")
    print(f"✅ proxy_pool: {len(names)} 个采集源（含 {len(github)} 个 GitHub 源），"
          f"Proxy 含 latency 字段")


def check_web():
    """展示层：静态资源与离线 GeoIP 就绪、模块可导入。"""
    for rel in ("static/index.html", "static/app.js", "data/ipdb.bin"):
        if not os.path.exists(rel):
            fail(f"缺少 {rel}")

    import geo

    if geo._local_lookup("8.8.8.8") != "US":
        fail("离线 GeoIP 库解析异常（8.8.8.8 应为 US）—— data/ipdb.bin 可能损坏")

    import gateway
    import web

    if not hasattr(gateway, "start"):
        fail("gateway 缺少 start()，web.py 无法在同进程托管网关")
    if not hasattr(web, "Handler"):
        fail("web 缺少 Handler")

    print("✅ web: 静态资源与离线 GeoIP 就绪，web/gateway 模块可导入")


def check_collisions():
    """镜像里两份代码放在同一目录，顶层模块名不能撞。

    原本 dashboard 有个 ip2region 的 util.py，和 proxy_pool 的 util/ 包同名，
    放一起会互相遮蔽而且是**静默**的（表现只是地理定位悄悄失效）。
    那个文件已随采集器一并删除，这里把"不许再撞"这条固化成断言，
    以后往任一侧加文件时会立刻发现。
    """
    proxy_pool = {"api", "db", "fetcher", "handler", "helper", "util",
                  "proxyPool", "setting"}
    web_dir = "dashboard"
    if not os.path.isdir(web_dir):
        fail(f"找不到 {web_dir}/，请在仓库根目录运行")

    web_side = set()
    for entry in os.listdir(web_dir):
        if entry.endswith(".py"):
            web_side.add(entry[:-3])
        elif os.path.isdir(os.path.join(web_dir, entry)) and not entry.startswith("__"):
            web_side.add(entry)

    clash = proxy_pool & web_side
    if clash:
        fail(f"顶层模块名冲突: {', '.join(sorted(clash))} —— "
             f"镜像里两份代码同目录，同名模块会互相遮蔽")
    print(f"✅ 无模块名冲突（proxy_pool {len(proxy_pool)} 项 vs "
          f"{web_dir}/ {len(web_side)} 项）")


def main():
    targets = {"proxy_pool": check_proxy_pool, "web": check_web,
               "collisions": check_collisions}
    if len(sys.argv) != 2 or sys.argv[1] not in targets:
        print(__doc__)
        return 2
    sys.path.insert(0, os.getcwd())
    os.environ.setdefault("GATEWAY_ENABLED", "0")
    targets[sys.argv[1]]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
