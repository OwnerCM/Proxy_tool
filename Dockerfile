# syntax=docker/dockerfile:1.7
#
# 合并镜像：proxy_pool（数据平面）+ 展示层 + 轮换网关，一个镜像跑一个容器。
# Redis 是独立容器，见 docker-compose.yml。
# 多架构：linux/amd64 + linux/arm64
#
# 全部代码都放在 /app，两份源码不做目录隔离 —— 顶层模块名没有交集，
# 构建期由 assert_layout.py collisions 把"不许再撞"这条固化成断言。
# （历史原因：dashboard 曾有个 ip2region 的 util.py 与 proxy_pool 的 util/ 包同名，
#   会互相静默遮蔽，所以当初分了目录；那个文件已随采集器一并删除。）
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
# 两份 requirements 一起装：proxy_pool 声明 redis>=4.2.0、展示层声明 redis==5.2.1，
# pip 解析后取 5.2.1，无冲突（已用 --dry-run 验证）。
COPY requirements.txt ./requirements.txt
COPY dashboard/requirements.txt ./requirements-web.txt
RUN pip install -r requirements.txt -r requirements-web.txt

# ══════════ runtime ══════════
FROM python:3.10-slim-bookworm

# 只保留本地构建时有意义的标签。revision / version / source 这几个由 CI 里的
# docker/metadata-action 自动注入（且会覆盖这里的同名值），不用在这儿重复维护。
LABEL org.opencontainers.image.title="proxy-tool" \
      org.opencontainers.image.description="代理池 + 可视化看板 + 轮换代理网关，单容器多架构镜像（支持 arm64）" \
      org.opencontainers.image.licenses="MIT"

# tini -> PID 1，负责信号转发与回收孤儿进程
# curl -> HEALTHCHECK
# libxml2/libxslt1.1 -> 仅在 lxml 退化为源码编译时才需要的动态库（很小，作为兜底）
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
    APP_DIR=/app

# ── 代码就位：两份源码同放 /app ──
WORKDIR /app
COPY api/ ./api/
COPY db/ ./db/
COPY fetcher/ ./fetcher/
COPY handler/ ./handler/
COPY helper/ ./helper/
COPY util/ ./util/
COPY proxyPool.py setting.py ./
# 显式列举而不是 COPY dashboard/ ./ —— 后者会让 dashboard/requirements.txt
# 覆盖 proxy_pool 的同名文件，虽然运行时用不到但很容易看错
COPY dashboard/web.py dashboard/gateway.py dashboard/geo.py ./
COPY dashboard/static/ ./static/
COPY dashboard/data/ ./data/
COPY docker/supervisor.py docker/healthcheck.sh docker/assert_layout.py /opt/proxy-tool/

# ── 构建期自检 ──
# 改了代码后如果前提不成立（模块名撞了、采集源没被发现、Proxy 少了 latency 字段、
# 离线 GeoIP 库损坏、静态资源缺失），构建直接失败，
# 而不是等到运行时才静默降级 —— ip2region 那条链路就是这么被藏了很久的。
RUN set -eux; \
    python proxyPool.py --help > /dev/null; \
    python /opt/proxy-tool/assert_layout.py proxy_pool; \
    python /opt/proxy-tool/assert_layout.py web; \
    python -m compileall -q /app /opt/proxy-tool

# ── 运行账户与可写目录 ──
# /app/log : 上游 handler/logHandler.py 在 import 时就会创建并写入该目录
# 显式建组：useradd 自动分配的 GID 不保证等于 UID，而下面 USER 用的是固定数字。
RUN groupadd --gid 10001 app \
    && useradd --create-home --shell /usr/sbin/nologin --uid 10001 --gid 10001 app \
    && mkdir -p /app/log \
    && chmod +x /opt/proxy-tool/healthcheck.sh \
    && chown -R 10001:10001 /app /opt/proxy-tool

USER 10001:10001

# ── 默认配置 ──
# 全部通过 ENV 提供，好处是 docker exec 进去手动跑脚本时也能拿到一致的配置。
#
# REDIS_HOST 默认取 "redis"，正好等于 docker-compose.yml 里 Redis 的服务名，
# 所以 compose 那边不用为此写 environment。
# 独立 docker run 时按需覆盖：-e REDIS_HOST=<你的 redis 地址>
#
# PROXY_POOL_DB=0 是代理数据所在的库；DB 1 只被 geo.py 用作地理缓存。
# PROXY_API 指向 127.0.0.1 是对的 —— proxy_pool 和展示层同在一个容器里。
#
# NO_PROXY 是必须的：容器里一旦存在 HTTP_PROXY（Docker 会把宿主机
# ~/.docker/config.json 里的 proxies 自动注入），访问本机 Redis 与 proxy_pool
# 的请求就会被送去外部代理而失败。大小写两份都给：urllib 与 requests
# 读取的变量名大小写不一致。
ENV REDIS_HOST=redis \
    REDIS_PORT=6379 \
    PROXY_POOL_DB=0 \
    PROXY_API=http://127.0.0.1:5010 \
    NO_PROXY=localhost,127.0.0.1,::1,redis,proxy-redis,proxy-tool \
    no_proxy=localhost,127.0.0.1,::1,redis,proxy-redis,proxy-tool

# 刻意不在这里设置 DB_CONN（proxy_pool 的库地址）。
# 它由 supervisor.py 从 REDIS_HOST/REDIS_PORT/REDIS_PASSWORD 推导后传给子进程；
# 在这里写死会盖掉推导结果，换 Redis 地址时就会连不上。

# 5010 proxy_pool 原生 API / 5050 看板 / 8080 网关(HTTP) / 1080 网关(SOCKS5)
EXPOSE 5010 5050 8080 1080

# HEALTHCHECK 必须用 shell 形式（脚本内部有条件判断）
# hadolint ignore=DL3025
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD /opt/proxy-tool/healthcheck.sh

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "/opt/proxy-tool/supervisor.py"]
