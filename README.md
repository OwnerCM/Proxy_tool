# Proxy Tool — 支持 arm64 的代理池 + 可视化看板 + 轮换网关

以 [jhao104/proxy_pool](https://github.com/jhao104/proxy_pool) 为数据平面，
把 [abclq/proxy-pool-dashboard](https://github.com/abclq/proxy-pool-dashboard) 的展示层接上，
再加一个固定入口的轮换代理网关，打成**一个镜像、一个容器**，
由 GitHub Actions 产出 **linux/amd64 + linux/arm64** 双架构镜像并发布到 GHCR。

Apple Silicon、树莓派、AWS Graviton 上都能直接跑。

---

## 为什么上游的 arm64 跑不了

**问题不在 Dockerfile，在 workflow。** 上游 `docker-image-latest.yml` 是这样写的：

```yaml
- uses: docker/build-push-action@v2
  with:
    context: .
    push: true
    tags: jhao104/proxy_pool:latest
```

没有 `platforms:`，没有 `setup-buildx-action`，也没有 QEMU。默认就只在 amd64 runner 上
产出一个 amd64 manifest，arm64 机器 `docker pull` 时报
`no matching manifest for linux/arm64/v8`。

顺带两个相关的坑：镜像 `ENTRYPOINT` 用 `bash`，但 `python:3.10-alpine` 里没有 bash
（这就是很多人要在 compose 里覆盖 `entrypoint` 的原因）；而**覆盖成 `sh` 同样不行**，
因为 Debian 的 `/bin/sh` 是 dash，也不支持 `proxy_pool.sh` 里的 `[[ ]]`。
本项目不再使用 `proxy_pool.sh`，这个坑直接消失。

## 怎么解决的

两个架构各自在**原生 runner** 上构建，按 digest 分别推送，最后合并成一个 manifest list：

```
             ┌─ ubuntu-latest    ─ linux/amd64 ─┐
push/tag ───►│                                   ├─► imagetools create ─► 一个多架构 tag
             └─ ubuntu-24.04-arm ─ linux/arm64 ─┘         └─► 断言 manifest 真含两个架构
```

- **用原生 arm64 runner，不用 QEMU。** GitHub 的 arm64 托管 runner 已于 2025-08
  [对公开仓库正式可用且免费](https://github.blog/changelog/2025-08-07-arm64-hosted-runners-for-public-repositories-are-now-generally-available/)。
  私有仓库把 workflow 里 arm64 那行的 runner 换成 `ubuntu-latest` 并加一步
  `docker/setup-qemu-action@v3` 即可。
- **基础镜像 alpine → `python:3.10-slim-bookworm`。** 依赖里只有 `lxml` 是 C 扩展，
  它在 PyPI 上有 amd64/arm64 的 manylinux wheel，走 glibc 完全不需要现场编译。
- **流水线自带验收。** 合并 manifest 后会 `imagetools inspect` 并断言其中同时存在
  `linux/amd64` 和 `linux/arm64`，缺一个就让构建失败。

---

## 快速开始

```bash
git clone <你的仓库地址> && cd Proxy_tool
docker compose pull && docker compose up -d
```

`docker-compose.yml` 里只写了 `image` 没写 `build`，是纯部署用的 —— `up` 只会拉镜像，
不会在拉不到时悄悄改成本地构建。镜像地址写死为 `ghcr.io/ownercm/proxy-tool:latest`，
换人用的话改这一行。

改了代码想在本地验证，显式构建成同名镜像再起：

```bash
docker build -t ghcr.io/ownercm/proxy-tool:latest .
docker compose up -d
```

| 地址 | 用途 |
| --- | --- |
| http://localhost:5050 | 可视化看板 |
| http://localhost:5010 | proxy_pool 原生 API |
| http://localhost:5010/get/ | 取一个代理 |
| `http://localhost:8080` | **轮换代理入口（HTTP）** |
| `socks5://localhost:1080` | **轮换代理入口（SOCKS5）** |

首次启动后采集器才开始跑，大约 10–30 分钟池子才有规模。

### 发布镜像

1. 仓库推到 GitHub
2. Settings → Actions → General → Workflow permissions 选 **Read and write permissions**
3. push 到 `main` 即自动构建，产出 `ghcr.io/<你的用户名>/proxy-tool:latest`

---

## 架构

**职责单一，不重复。** 这一点是踩过坑才定下来的 —— 见下文「为什么不是简单合并」。

```
┌─ proxy-redis ────────┐        ┌─ proxy-tool（单容器，3 个进程）──────────────┐
│  redis:7-alpine      │        │  tini                                        │
│                      │        │   └── docker/supervisor.py                   │
│  DB 0  代理数据       │◄───────┤        ├── api      :5010  原生 API           │
│  DB 1  geo 缓存       │        │        ├── schedule        采集 + 验证        │
└──────────────────────┘        │        └── serve    :5050  看板               │
                                │                     :8080  网关(HTTP)        │
                                │                     :1080  网关(SOCKS5)      │
                                └──────────────────────────────────────────────┘

三个进程都是阻塞式的，合不了：`api` 是 gunicorn（自己还 fork 4 个 worker），
`schedule` 是 APScheduler 的 BlockingScheduler。看板和网关都是线程模型，
已经合并进 `serve` 一个进程。
```

| 层 | 谁负责 | 说明 |
| --- | --- | --- |
| 数据平面 | proxy_pool | **唯一**写代理数据的地方：采集、验证、淘汰、原生 API |
| 采集源 | proxy_pool 的 24 个 fetcher | 原有 14 个 + 移植进来的 10 个 GitHub 列表源 |
| 展示 | `web/web.py` | 只读 DB 0，不写任何代理数据 |
| 出口 | `web/gateway.py` | 固定入口，逐请求轮换上游；由 web 进程托管 |

Redis 的 DB 0 存代理数据（proxy_pool 的 `use_proxy` hash），DB 1 只被 `geo.py`
用作地理信息缓存，不是第二个数据平面。

### 为什么不是简单合并

一开始我把两个项目并排跑、用一个 bridge 把数据从 proxy_pool 复制给看板。
后来核实发现这么做几乎没有收益：

**proxy-pool-dashboard 不是 proxy_pool 的前端，而是一套完整独立的代理池** ——
它有自己的采集器（23 源）、验证器、存储 schema、API 和 UI。它对 proxy_pool 的引用
只有 `backend.py` 里一处 `jhao_map()`，整段包在 `try/except` 里，读不到就返回空；
而且只给已存在于自己库里的代理补三个字段，从不新增代理。

更致命的是**质量策略冲突**：proxy_pool 没有延迟门槛，只要 10s 内有响应就留；
而看板的验证器会把 **≥500ms 的直接删掉**。所以从 proxy_pool 复制过去的代理，
30 秒内绝大部分就被清掉了，proxy_pool 那 10 秒超时的验证工作也全是白做的。

（同一作者还有第三个仓库 `proxy-pool-tools`，README 写着「完整代理池 =
proxy_pool + dashboard + 本工具集」，但那个工具集全程用 SQLite、
`DB_PATH` 甚至硬编码成作者本机路径，与前两者的 Redis 完全不通。）

所以现在的做法是：**只保留一套数据平面**（proxy_pool，成熟、有测试、MIT 许可），
把看板降级为纯展示层，把对方最有价值的资产（GitHub 代理列表源）移植进
proxy_pool 的 fetcher 框架。看板自带的采集器、验证器、质量统计、bridge 全部删除。

### 进程管理

容器里用 `docker/supervisor.py` 统一管这 3 个进程，而不是沿用上游各自的启动方式：

- proxy_pool 的 `proxy_pool.sh` 只是拉起 `proxyPool.py server` 和 `schedule`，
  这里直接调这两个 click 子命令，脚本和它的 bash 依赖都不需要了。
- 看板原来的看门狗 `dashboard.py` 已随其采集/验证进程一并删除。

它做三件事：

- 给每个进程的输出加 `[名字]` 前缀再转发到容器 stdout
- SIGTERM/SIGINT 时优雅停机，连子进程派生的孙进程一起清掉
- 启动前自检脚本和离线库是否就位，缺了立刻报错而不是运行时静默降级

**容错取向是 crash-only**：任一进程退出就结束整个容器，由 Docker 的 restart 策略
统一重启（自带指数退避），容器内部不做单进程重启和退避。这样少掉一整套
退避/计数/稳定期判定的逻辑；更重要的是进程反复挂会直接表现为容器反复重启，
在监控上可见，而不是被"容器内部悄悄重启、对外仍然 healthy"掩盖。
代价是一个进程挂会连带重启另两个 —— 状态全在外部 Redis 里，代价是几秒钟中断。

```bash
docker compose logs -f proxy-tool
docker compose logs -f proxy-tool | grep '^\[gateway\]'   # 只看网关
```

---

## gateway —— 固定入口的轮换代理

代理池里的 IP 一直在变，业务侧不想每次都去调 API 换地址。没有网关时每个调用方都得
自己写一遍取用—重试—上报：

```python
proxy = requests.get("http://proxy-tool:5010/get/").json()["proxy"]
try:
    r = requests.get(url, proxies={"http": f"http://{proxy}"}, timeout=10)
except Exception:
    requests.get(f"http://proxy-tool:5010/delete/?proxy={proxy}")
    # 重试逻辑自己写…
```

有网关时地址固定不变，业务代码零改动：

```bash
export http_proxy=http://proxy-tool:8080
curl https://target.com

curl -x socks5h://proxy-tool:1080 https://httpbin.org/ip
```

- 入口协议：HTTP（含 `CONNECT` 隧道，所以 HTTPS 也走得通）、SOCKS5
- 选取策略：按实测延迟排序取最快的一批，逐请求随机轮换；延迟未知的排在最后
- 失败处理：自动换下一个上游，连续失败达阈值后临时拉黑（默认 2 次 / 冷却 300s）
- 上游来源默认直读 Redis（比走 API 少一跳，且能拿到延迟）

**适用与不适用：** 它适合无状态的批量请求，以及那些你改不了代码的工具
（curl、浏览器、只支持 SOCKS 的客户端）。如果需要「同一个会话固定用同一个出口 IP」
（维持登录态、购物车、分页会话），那就直接调 `/get/` 拿一个代理在该会话里一直用、
绕过网关 —— 两种用法并行不冲突。

另外要清楚它的代价：所有流量过一个进程，是吞吐瓶颈也是单点；它也隐藏了实际出口 IP，
排查问题时不够直观。

> ### ⚠️ 安全提示
> `GATEWAY_USER` / `GATEWAY_PASS` 默认为空，此时 8080 和 1080 是**无鉴权的开放代理**。
> 一旦这两个端口能被公网访问到，会在很短时间内被扫描并滥用，流量算在你头上。
>
> 只在可信网络内使用，或者在 compose 里给 `proxy-tool` 加上这两个环境变量。
> 不需要网关就设 `SVC_GATEWAY=0` 并删掉对应的端口映射。

---

## 配置

默认值全部写在镜像的 `ENV` 里，开箱可用。需要调整时在 compose 的 `proxy-tool`
服务下加 `environment:` 即可。

### 服务开关

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `SVC_PROXY_POOL` | `1` | 采集 + 验证 + 原生 API |
| `SVC_WEB` | `1` | 展示层 |
| `SVC_GATEWAY` | `1` | 8080 / 1080 轮换代理 |

### 采集与验证吞吐（重要）

移植进来的 GitHub 列表源加起来有一万多条，而且绝大多数是死的。
proxy_pool 的调度是**每 5 分钟采集一轮、每 2 分钟全量复检**，
若不限流，单轮验证要跑一个多小时，远超调度间隔，任务会不断堆叠直到把池子拖死。

所以每个 GitHub 源每轮只随机取 `GITHUB_FETCH_LIMIT` 条（默认 150，10 个源共约 1500 条）。
随机采样能让多轮下来覆盖整个列表，而不是每次都盯着开头几条。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `GITHUB_FETCH_LIMIT` | `150` | 每个 GitHub 源每轮的取样上限，`0` = 不限 |
| `PROXY_CHECK_THREADS` | `20` | 校验线程数（上游写死 20，本项目改为可配） |
| `VERIFY_TIMEOUT` | `10` | 单个代理的校验超时（秒），调小能显著提升吞吐 |

粗算：1500 条 × 均摊 3s ÷ 20 线程 ≈ 225s，刚好落在 5 分钟的采集间隔内。
想放大源规模就同时调大前两项，或把 `VERIFY_TIMEOUT` 降到 5。
日志里 `ProxyCheck` 迟迟不 `complete` 就是没跑完的信号。

### Redis

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `REDIS_HOST` | `redis` | 等于 compose 里的服务名 |
| `REDIS_PORT` | `6379` | |
| `REDIS_PASSWORD` | 空 | 有则用于拼 `DB_CONN` |
| `PROXY_POOL_DB` | `0` | 代理数据所在库（DB 1 是 geo 缓存） |
| `DB_CONN` | 由上面几项推导 | 一般不用手动设 |

### 网关

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `GATEWAY_USER` / `GATEWAY_PASS` | 空 | 客户端鉴权。**留空 = 开放代理** |
| `GATEWAY_SOURCE` | `redis` | 上游来源，可选 `redis` / `api` / `static` |
| `GATEWAY_TOP_N` | `200` | 候选池保留最快的 N 个 |
| `GATEWAY_MAX_RETRIES` | `3` | 单次请求最多尝试几个上游 |
| `GATEWAY_FAIL_THRESHOLD` | `2` | 连续失败几次后拉黑 |
| `GATEWAY_COOLDOWN` | `300` | 拉黑时长（秒） |

完整清单见 `web/gateway.py` 与 `web/web.py` 的模块头注释。
proxy_pool 侧沿用上游的环境变量覆盖机制（`handler/configHandler.py`）。

---

## 与上游的差异

### 构建与打包

| 改动 | 原因 |
| --- | --- |
| workflow 重写为多架构，合并成一个文件发 GHCR | 上游只出 amd64（**本项目要解决的核心问题**） |
| 合并为单镜像单容器 | 部署简化 |
| 基础镜像 alpine → Debian slim | glibc/manylinux 有现成 aarch64 wheel，跨架构免编译 |
| 不再使用 `proxy_pool.sh` | 直接调 `proxyPool.py` 的子命令，摆脱 bash 依赖 |
| 多阶段构建、非 root（`10001:10001`）运行、内置 `HEALTHCHECK` | 体积、权限、编排 |

### 对 proxy_pool 的改动

| 文件 | 改动 | 原因 |
| --- | --- | --- |
| `helper/proxy.py`、`helper/check.py` | 新增 `latency` 字段并在校验时计时 | 上游完全不记录延迟，导致看板的延迟列/排序/筛选和网关的择优都无从实现。校验本身就是一次真实代理请求，顺手计时即可 |
| `fetcher/sources/github_lists.py` | 新增 10 个 GitHub 列表源 | 移植 dashboard/tools 最有价值的资产到成熟框架里。只收 HTTP/HTTPS 源：proxy_pool 的校验器验不了 SOCKS，导进来只会全部失败被删 |
| `setting.py`、`handler/configHandler.py`、`helper/check.py` | 校验线程数改为可配 | 上游写死 20，源规模变大后需要能调 |

上游列表里 `mertguvencli/http-proxy-list` 与 `mmpx12/proxy-list` 的 raw 路径实测已 404，
未收录。

### 对 dashboard 的改动

删除了 9 个文件（`backend.py`、`frontend.py`、`validator.py`、`quality.py`、
`new_fetcher.py`、`searcher.py`、`util.py`、`dashboard.py`、`bridge.py`）
和 10MB 的 `data/ip2region.xdb` —— 它们构成的那套采集/验证/存储与 proxy_pool 完全重复。
目录体积从 14MB 降到 3.5MB（目录也随之从 `dashboard/` 更名为 `web/`，因为它现在只承载展示层和网关）。

保留 `geo.py`（GeoIP，含离线库 `data/ipdb.bin`）与 `static/`（前端 SPA），
新增 `web.py`（只读展示层，同时托管静态文件）和 `gateway.py`（轮换网关）。

顺带修掉一个上游缺陷：`searcher.py` 里 `import ip2region.util as util` 引用了一个
仓库中并不存在的包，导致 ip2region 离线兜底**从未生效**过。随着 `new_fetcher.py`
一并删除，这条死链路和它带来的模块同名冲突都不存在了。

前端两处已不成立的描述也已修正：状态栏不再宣称「仅展示 <500ms」，
协议筛选不再列出 proxy_pool 根本不会产出的 socks4/socks5。

---

## 目录结构

```
.
├── .github/workflows/docker_publish.yml   # 唯一的 workflow：测试 → 多架构构建 → 发布 GHCR
├── Dockerfile                  # 合并镜像
├── docker-compose.yml          # 两个容器：redis + proxy-tool
├── docker/
│   ├── supervisor.py           # 容器内进程管理 + 健康检查（--healthcheck）
│   └── selfcheck.py            # 构建期自检，Dockerfile 与 CI 共用
├── api/ db/ fetcher/ handler/ helper/ util/    # proxy_pool 源码
│   └── fetcher/sources/github_lists.py         #   └─ 新增的 GitHub 列表源
├── proxyPool.py  setting.py  requirements.txt
├── tests/  pyproject.toml  requirements-test.txt   # proxy_pool 测试（248 例）
└── web/
    ├── web.py                  # 只读展示层（自研）
    ├── gateway.py              # 轮换代理网关（自研，由 web 进程托管）
    ├── geo.py                  # GeoIP（上游）
    ├── data/ipdb.bin           # 离线 GeoIP 库，运行必需
    └── static/                 # 前端 SPA（上游）
```

---

## 本地校验

```bash
hadolint Dockerfile
actionlint
docker compose config -q

pip install -r requirements.txt -r requirements-test.txt
pytest -q

python docker/selfcheck.py collisions
python docker/selfcheck.py proxy_pool
cd web && python ../docker/selfcheck.py web
```

---

## 许可与出处

- **proxy_pool** — MIT License，Copyright (c) 2017 J_hao104。见 [`LICENSE`](LICENSE)。
- **proxy-pool-dashboard** — 上游仓库**未声明许可证**，默认即保留所有权利。
  本项目保留其 `geo.py` 与 `static/`。自用没问题；若要公开分发或商用，
  建议先联系原作者确认授权。
- `web/data/ipdb.bin` 源自 DB-IP Country Lite，适用其原始许可条款。
- GitHub 代理列表源的 URL 清单参考了 `abclq/proxy-pool-tools`。

本项目新增的 `web.py`、`gateway.py`、`docker/` 下的脚本、`github_lists.py`、
workflow 与容器化配置遵循 MIT。

## 免责声明

公开代理来源不可控，请勿用于任何违法用途，也不要传输敏感数据 —— 中间节点对流量是可见的。
`gateway` 默认不鉴权，暴露到公网前请务必阅读上面的安全提示。
