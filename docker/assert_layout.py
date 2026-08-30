#!/usr/bin/env python3
"""构建期布局断言 —— 由 Dockerfile 调用，也可以在本地直接跑。

合并镜像最容易出错的地方是 proxy_pool 和 dashboard 都有一个叫 `util` 的顶层模块：
proxy_pool 的是 util/ 包，dashboard 的是 ip2region 的 util.py。
两者一旦落在同一目录或同一 sys.path 上，就会互相覆盖 —— 而且是**静默**覆盖，
表现只是 ip2region 地理定位悄悄失效，很难发现。

所以镜像把它们分开放（dashboard 在 /app，proxy_pool 在 /opt/proxy_pool），
并用这个脚本在构建期把这条约束钉死：将来更新代码后如果布局被破坏，
构建会直接失败，而不是等到线上才发现定位数据不对。

断言都相对当前工作目录，不写死绝对路径，这样本地模拟镜像布局时能跑同一套逻辑：

    cd <dashboard 目录>  && python docker/assert_layout.py dashboard
    cd <proxy_pool 目录> && python docker/assert_layout.py proxy_pool

用法：assert_layout.py {proxy_pool|dashboard}
"""

import os
import sys


def fail(msg):
    print(f"❌ 布局断言失败: {msg}", file=sys.stderr)
    raise SystemExit(1)


def resolved(path):
    return os.path.realpath(path)


def check_util_is_local(expect_package):
    """`import util` 必须解析到当前目录下的那一个。"""
    import util

    path = resolved(util.__file__)
    cwd = resolved(os.getcwd())
    if not path.startswith(cwd + os.sep):
        fail(f"import util 解析到了 {path}，它不在当前目录 {cwd} 下 —— "
             f"说明 proxy_pool 与 dashboard 的 util 发生了串台")

    is_package = os.path.basename(path) == "__init__.py"
    if expect_package and not is_package:
        fail(f"这里的 util 应该是 proxy_pool 的 util/ 包，实际是 {path}")
    if not expect_package and is_package:
        fail(f"这里的 util 应该是 dashboard 的 ip2region util.py，实际是 {path}")
    return path


def check_proxy_pool():
    path = check_util_is_local(expect_package=True)
    print(f"✅ proxy_pool: util -> {path}")


def check_dashboard():
    path = check_util_is_local(expect_package=False)
    print(f"✅ dashboard: util -> {path}")

    # ip2region 离线库真查一次。上游这条链路本来是坏的
    # （searcher.py 里 import 了一个不存在的 ip2region 包），修好后必须保持可用。
    import searcher
    import util

    xdb = os.path.join("data", "ip2region.xdb")
    if not os.path.exists(xdb):
        fail(f"离线库 {xdb} 不存在")
    result = searcher.new_with_file_only(util.IPv4, xdb).search("114.114.114.114")
    if "CN" not in result:
        fail(f"ip2region 查询结果不对: {result}")
    print(f"✅ dashboard: ip2region 可用 -> {result}")

    # 所有会被 supervisor 拉起的模块都得能导入
    modules = ["geo", "backend", "frontend", "validator", "quality",
               "new_fetcher", "bridge", "gateway"]
    os.environ.setdefault("GATEWAY_ENABLED", "0")
    for name in modules:
        __import__(name)
    print(f"✅ dashboard: {len(modules)} 个模块均可导入")


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("proxy_pool", "dashboard"):
        print(__doc__)
        return 2
    sys.path.insert(0, os.getcwd())
    {"proxy_pool": check_proxy_pool, "dashboard": check_dashboard}[sys.argv[1]]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
