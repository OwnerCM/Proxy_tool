# syntax=docker/dockerfile:1.7
#
# 合并镜像：proxy_pool（数据平面）+ 展示层 + 轮换网关，一个镜像跑一个容器。
# Redis 是独立容器，见 docker-compose.yml。
# 多架构：linux/amd64 + linux/arm64
#
# 全部代码都放在 /app，两份源码不做目录隔离 —— 顶层模块名没有交集，
# 构建期由 selfcheck.py collisions 把"不许再撞"这条固化成断言。
# （历史原因：web/ 曾有个 ip2region 的 util.py 与 proxy_pool 的 util/ 包同名，
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
# 只有一份依赖清单。展示层唯一的额外依赖是 redis，已并入根 requirements.txt
# 并锁定版本（原来 web/ 下那份是双镜像时代的遗留，已删）。
COPY requirements.txt ./
RUN pip install -r requirements.txt

# ══════════ runtime ══════════
FROM python:3.10-slim-bookworm

# 只保留本地构建时有意义的标签。revision / version / source 这几个由 CI 里的
# docker/metadata-action 自动注入（且会覆盖这里的同名值），不用在这儿重复维护。
LABEL org.opencontainers.image.title="proxy-tool" \
      org.opencontainers.image.description="代理池 + 可视化看板 + 轮换代理网关，单容器多架构镜像（支持 arm64）" \
      org.opencontainers.image.licenses="MIT"

# tini   -> PID 1，负责信号转发与回收孤儿进程
# curl   -> 手工排查用（健康检查已改为 supervisor.py --healthcheck，不依赖它）
# procps -> 提供 ps/top。基础镜像没有它，而这个容器里跑着好几个进程，
#           没有 ps 就只能看 docker stats 的聚合值，定位不到是哪个进程在吃 CPU
# libxml2/libxslt1.1 -> 仅在 lxml 退化为源码编译时才需要的动态库（很小，作为兜底）
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tini \
        curl \
        procps \
        tzdata \
        libxml2 \
        libxslt1.1 \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=Asia/Shanghai
RUN ln -snf "/usr/share/zoneinfo/$TZ" /etc/localtime && echo "$TZ" > /etc/timezone

COPY --from=builder /opt/venv /opt/venv

# MALLOC_ARENA_MAX：glibc 默认给每个进程开到 8×CPU核数 个 malloc arena，每个 arena
# 都有独立的空闲链表，多线程程序（这里每个进程都有若干工作线程）会因此虚增几十 MB
# RSS —— 内存并没有真的在用，只是没还给系统。压到 2 个对吞吐没有实质影响，
# 这类线程数不多、以网络 I/O 为主的服务尤其如此。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    APP_DIR=/app \
    MALLOC_ARENA_MAX=2

# ── 代码就位：两份源码同放 /app ──
WORKDIR /app
COPY api/ ./api/
COPY db/ ./db/
COPY fetcher/ ./fetcher/
COPY handler/ ./handler/
COPY helper/ ./helper/
COPY util/ ./util/
COPY proxyPool.py setting.py ./
COPY web/ ./
COPY docker/supervisor.py docker/selfcheck.py /opt/proxy-tool/

# ── 构建期自检 ──
# 改了代码后如果前提不成立（模块名撞了、采集源没被发现、Proxy 少了 latency 字段、
# 离线 GeoIP 库损坏、静态资源缺失），构建直接失败，
# 而不是等到运行时才静默降级 —— ip2region 那条链路就是这么被藏了很久的。
RUN set -eux; \
    python proxyPool.py --help > /dev/null; \
    python /opt/proxy-tool/selfcheck.py proxy_pool; \
    python /opt/proxy-tool/selfcheck.py web; \
    python -m compileall -q /app /opt/proxy-tool

# ── 运行账户与可写目录 ──
# /app/log : 上游 handler/logHandler.py 在 import 时就会创建并写入该目录
# 显式建组：useradd 自动分配的 GID 不保证等于 UID，而下面 USER 用的是固定数字。
RUN groupadd --gid 10001 app \
    && useradd --create-home --shell /usr/sbin/nologin --uid 10001 --gid 10001 app \
    && mkdir -p /app/log \
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
# 刻意不设 NO_PROXY。容器里访问本机的 HTTP 调用只有两处（gateway 读 proxy_pool
# 的 /all/、supervisor 的健康检查），它们都在代码里用 ProxyHandler({}) 显式禁用了
# 代理 —— 这比 NO_PROXY 可靠：Docker 注入代理配置时会同时写大小写两个变量，
# 只覆盖其中一个的话，另一个（Docker 的值）会被 urllib 和 requests 优先采纳。
ENV REDIS_HOST=redis \
    REDIS_PORT=6379 \
    PROXY_POOL_DB=0 \
    PROXY_API=http://127.0.0.1:5010

# 刻意不在这里设置 DB_CONN（proxy_pool 的库地址）。
# 它由 supervisor.py 从 REDIS_HOST/REDIS_PORT/REDIS_PASSWORD 推导后传给子进程；
# 在这里写死会盖掉推导结果，换 Redis 地址时就会连不上。

# 5010 proxy_pool 原生 API / 5050 看板 / 8080 网关(HTTP) / 1080 网关(SOCKS5)
EXPOSE 5010 5050 8080 1080

# 健康检查复用 supervisor 的 SVC_* 判定，只探测被启用的服务。
# interval 用 60s 而非 30s：探 /api/stats 会触发展示层重建全量快照，
# 频率过高等于自己给自己加负载。
HEALTHCHECK --interval=60s --timeout=15s --start-period=60s --retries=3 \
    CMD ["python", "/opt/proxy-tool/supervisor.py", "--healthcheck"]

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "/opt/proxy-tool/supervisor.py"]
