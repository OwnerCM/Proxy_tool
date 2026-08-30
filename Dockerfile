# syntax=docker/dockerfile:1.7
#
# 合并镜像：proxy_pool + dashboard + 内置 Redis，一个镜像跑一个容器。
# 多架构：linux/amd64 + linux/arm64
#
# ── 镜像内部布局（刻意与仓库布局不同，原因如下）──
#
#   /app              ← 仓库的 dashboard/
#         上游 dashboard/backend.py 第 9 行写死了 sys.path.insert(0, "/app")，
#         所以 dashboard 必须正好落在 /app，否则它自己的模块会找不到。
#
#   /opt/proxy_pool   ← 仓库根目录的 proxy_pool 源码
#         不能和 dashboard 放同一个目录：两边都有名为 util 的顶层模块
#         （proxy_pool 是 util/ 包，dashboard 是 ip2region 的 util.py），
#         同目录会互相覆盖，且 ip2region 会静默失效。
#         proxy_pool 源码里没有任何绝对路径，可以自由重定位。
#         下面有构建期断言来锁死这条约束。
#
#   /opt/proxy-tool   ← 进程管理器与健康检查脚本
#   /data             ← 内置 Redis 的落盘目录
#
# ── 与上游 jhao104/proxy_pool 镜像的差异 ──
#   1. 基础镜像 python:3.10-alpine → python:3.10-slim-bookworm。
#      alpine 走 musl，C 扩展缺 musllinux wheel 时要现场编译；Debian slim 走
#      glibc/manylinux，lxml 在 amd64/arm64 上都有预编译 wheel，跨架构免编译。
#   2. 不使用 proxy_pool.sh。它是个 bash 脚本（用了 [[ ]]），而它做的事就是拉起
#      proxyPool.py 的 server 和 schedule 两个子命令 —— 这里由 supervisor.py 直接调用，
#      bash 依赖和 alpine/dash 不兼容的问题一并消失。
#   3. Python 版本锁在 3.10：lxml 4.9.2 没有 cp312/cp313 的 wheel。

# ══════════ builder：只负责装依赖 ══════════
FROM python:3.10-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# 正常情况下所有依赖都有 amd64/arm64 wheel，用不到编译器。
# 留着是为了兜底：万一某个架构缺 wheel，构建会退化为源码编译而不是直接失败。
# 不锁 apt 版本（DL3008）：Debian 归档会清理旧版本，锁死会让镜像过段时间构建失败。
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        gcc \
        libxml2-dev \
        libxslt1-dev \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /tmp/build
# 两份 requirements 直接一起装：proxy_pool 声明 redis>=4.2.0、dashboard 声明
# redis==5.2.1，pip 解析后取 5.2.1，无冲突（已用 --dry-run 验证）。
COPY requirements.txt ./requirements.txt
COPY dashboard/requirements.txt ./requirements-dashboard.txt
RUN pip install -r requirements.txt -r requirements-dashboard.txt

# ══════════ runtime ══════════
FROM python:3.10-slim-bookworm

ARG VCS_REF=unknown
ARG BUILD_VERSION=dev

LABEL org.opencontainers.image.title="proxy-tool" \
      org.opencontainers.image.description="代理池 + 可视化看板 + 轮换代理网关，单容器多架构镜像（支持 arm64）" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="${BUILD_VERSION}"

# tini -> PID 1，负责信号转发与回收孤儿进程
# curl -> HEALTHCHECK
# libxml2/libxslt1.1 -> 仅在 lxml 退化为源码编译时才需要的动态库（很小，作为兜底）
# Redis 不装在这个镜像里，它作为独立容器运行（见 docker-compose.yml）
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tini \
        curl \
        tzdata \
        libxml2 \
        libxslt1.1 \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=Asia/Shanghai
RUN ln -snf "/usr/share/zoneinfo/$TZ" /etc/localtime && echo "$TZ" > /etc/timezone

COPY --from=builder /opt/venv /opt/venv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    APP_DIR=/app \
    PP_DIR=/opt/proxy_pool

# ── 代码就位 ──
COPY dashboard/ /app/
COPY docker/supervisor.py docker/healthcheck.sh docker/assert_layout.py /opt/proxy-tool/

WORKDIR /opt/proxy_pool
COPY api/ ./api/
COPY db/ ./db/
COPY fetcher/ ./fetcher/
COPY handler/ ./handler/
COPY helper/ ./helper/
COPY util/ ./util/
COPY proxyPool.py setting.py ./

# ── 构建期断言：把上面那些布局约束真正锁死 ──
# 手动更新上游代码后，如果结构变了导致这些前提不成立，构建会直接失败，
# 而不是等到运行时才静默降级（ip2region 兜底失效就是这么被藏了很久的）。
#
# 这里必须用 cd 而不是 WORKDIR（DL3003）：断言的核心就是"同一个 python 在不同 cwd 下
# import util 会解析到不同文件"，需要在一条 RUN 里切换两次目录，WORKDIR 做不到。
# hadolint ignore=DL3003
RUN set -eux; \
    cd /opt/proxy_pool; \
    python proxyPool.py --help > /dev/null; \
    python /opt/proxy-tool/assert_layout.py proxy_pool; \
    cd /app; \
    python /opt/proxy-tool/assert_layout.py dashboard; \
    python -m compileall -q /app /opt/proxy_pool /opt/proxy-tool

# ── 运行账户与可写目录 ──
# /opt/proxy_pool/log : 上游 handler/logHandler.py 在 import 时就会创建并写入该目录
# 显式建组：useradd 自动分配的 GID 不保证等于 UID，而下面 USER 用的是固定数字。
RUN groupadd --gid 10001 app \
    && useradd --create-home --shell /usr/sbin/nologin --uid 10001 --gid 10001 app \
    && mkdir -p /opt/proxy_pool/log \
    && chmod +x /opt/proxy-tool/healthcheck.sh \
    && chown -R 10001:10001 /app /opt/proxy_pool /opt/proxy-tool

USER 10001:10001

# ── 默认配置 ──
# 全部通过 ENV 提供，好处是 docker exec 进去手动跑脚本时也能拿到一致的配置，
# 不必依赖各上游模块里五花八门的默认值（dashboard 各模块的默认值并不一致）。
# REDIS_HOST 默认取 "redis"，正好等于 docker-compose.yml 里 Redis 的服务名，
# 所以 compose 那边一行 environment 都不用写。
# 独立 docker run 时按需覆盖：-e REDIS_HOST=<你的 redis 地址>
#
# PROXY_API 指向 127.0.0.1 是对的 —— proxy_pool 和看板现在同在一个容器里。
# NO_PROXY 是必须的：看板的 frontend.py 用 urllib 默认 opener 把 /api/* 反代到
# 127.0.0.1:5051，一旦容器里存在 HTTP_PROXY（Docker 会把宿主机 ~/.docker/config.json
# 里的 proxies 自动注入容器），这个内部请求就会被送去外部代理，看板 API 直接 502。
# 大小写两份都给：urllib 与 requests 读取的变量名大小写不一致。
ENV REDIS_HOST=redis \
    REDIS_PORT=6379 \
    REDIS_DB=1 \
    PROXY_API=http://127.0.0.1:5010 \
    NO_PROXY=localhost,127.0.0.1,::1,redis,proxy-redis,proxy-tool \
    no_proxy=localhost,127.0.0.1,::1,redis,proxy-redis,proxy-tool

# 刻意不在这里设置 DB_CONN（proxy_pool 的库地址）。
# 它由 supervisor.py 从 REDIS_HOST/REDIS_PORT/REDIS_PASSWORD 推导后传给子进程；
# 在这里写死会盖掉推导结果，换 Redis 地址时就会连不上。

# 5010 proxy_pool API / 5050 看板 / 8080 网关(HTTP) / 1080 网关(SOCKS5)
# 5051 是看板内部 API，6379 是内置 Redis，都只监听回环，不对外暴露
EXPOSE 5010 5050 8080 1080

# HEALTHCHECK 必须用 shell 形式（脚本内部有条件判断）
# hadolint ignore=DL3025
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD /opt/proxy-tool/healthcheck.sh

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "/opt/proxy-tool/supervisor.py"]
