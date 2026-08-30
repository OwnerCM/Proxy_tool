# Proxy Tool — 支持 arm64 的代理池 + 可视化看板 + 轮换网关

把 [jhao104/proxy_pool](https://github.com/jhao104/proxy_pool)（代理采集与校验）和
[abclq/proxy-pool-dashboard](https://github.com/abclq/proxy-pool-dashboard)（可视化看板）
合并成**一个镜像、一个容器**，由 GitHub Actions 产出 **linux/amd64 + linux/arm64**
双架构镜像并发布到 GHCR。

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
本项目不再使用 `proxy_pool.sh`，这个坑直接消失（见下文）。

## 怎么解决的

两个架构各自在**原生 runner** 上构建，按 digest 分别推送，最后合并成一个 manifest list：

```
             ┌─ ubuntu-latest    ─ linux/amd64 ─┐
push/tag ───►│                                   ├─► imagetools create ─► 一个多架构 tag
             └─ ubuntu-24.04-arm ─ linux/arm64 ─┘         └─► 断言 manifest 真含两个架构
```

- **用原生 arm64 runner，不用 QEMU。** GitHub 的 arm64 托管 runner 已于 2025-08
  [对公开仓库正式可用且免费](https://github.blog/changelog/2025-08-07-arm64-hosted-runners-for-public-repositories-are-now-generally-available/)。
  QEMU 模拟慢且偶发失败。
  私有仓库拿不到免费 arm runner 时，把仓库变量 `ARM_RUNNER` 设成 `ubuntu-latest`，
  workflow 里的 QEMU 步骤会自动启用，不用改任何代码。
- **基础镜像 alpine → `python:3.10-slim-bookworm`。** 依赖里只有 `lxml` 是 C 扩展，
  它在 PyPI 上有 amd64/arm64 的 manylinux wheel，走 glibc 完全不需要现场编译。
- **流水线自带验收。** 合并 manifest 后会 `imagetools inspect` 并断言其中同时存在
  `linux/amd64` 和 `linux/arm64`，缺一个就让构建失败 —— 不靠"构建成功"去假定架构支持。

---

## 快速开始

```bash
git clone <你的仓库地址> && cd Proxy_tool
docker compose up -d --build
```

要用 CI 产出的镜像，先把 `docker-compose.yml` 里的 `OWNER` 换成你的 GitHub 用户名，
然后 `docker compose pull && docker compose up -d`。

| 地址 | 用途 |
| --- | --- |
| http://localhost:5050 | 可视化看板 |
| http://localhost:5050/api/proxies | 看板 API（按国家/城市/协议/延迟筛选） |
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

两个容器：Redis 一个，proxy_pool + dashboard 合并成的应用一个。

```
┌─ proxy-redis ────────┐        ┌─ proxy-tool（单容器，8 个进程）──────────────┐
│  redis:7-alpine      │        │  tini                                        │
│                      │        │   └── docker/supervisor.py                   │
│  DB 0 ← proxy_pool   │◄───────┤        ├── pp-api      :5010  proxy_pool API │
│  DB 1 ← dashboard    │        │        ├── pp-sched           抓取 + 校验     │
└──────────────────────┘        │        ├── dash-web    :5050  看板 + 反代     │
                                │        ├── dash-api    :5051  看板内部 API    │
                                │        ├── validator          验证 / 淘汰     │
                                │        ├── quality            质量统计        │
                                │        ├── bridge             导入代理        │
                                │        └── gateway    :8080   HTTP 代理入口   │
                                │                       :1080   SOCKS5 入口     │
                                └──────────────────────────────────────────────┘
```

两套系统共用一个 Redis 但**分库**：proxy_pool 用 DB 0，看板用 DB 1，互不干扰。

### 镜像内部布局（和仓库布局刻意不同）

| 镜像路径 | 内容 | 为什么必须是这个位置 |
| --- | --- | --- |
| `/app` | 仓库的 `dashboard/` | 上游 `backend.py` 第 9 行写死了 `sys.path.insert(0, "/app")` |
| `/opt/proxy_pool` | 仓库根目录的 proxy_pool 源码 | **不能和 dashboard 同目录** |
| `/opt/proxy-tool` | 进程管理器与检查脚本 | — |

那条"不能同目录"是这个合并项目最大的暗坑：两边都有一个叫 `util` 的顶层模块
（proxy_pool 是 `util/` 包，dashboard 是 ip2region 的 `util.py`），放一起会互相覆盖，
而且是**静默**覆盖 —— 表现只是地理定位悄悄失效，很难发现。
`docker/assert_layout.py` 在构建期把这条约束钉死：布局一旦被破坏，构建直接失败。

proxy_pool 源码里没有任何绝对路径，所以它可以自由重定位。

### 进程管理

容器里用 `docker/supervisor.py` 统一管这 8 个进程，而不是沿用上游各自的启动方式：

- proxy_pool 的 `proxy_pool.sh` 只是拉起 `proxyPool.py server` 和 `schedule` 两个进程，
  这里直接调这两个 click 子命令，脚本和它的 bash 依赖都不需要了。
- dashboard 的 `dashboard.py` 是它自己的看门狗，只管它那 4 个进程。合并后由
  supervisor 统一接管；`dashboard.py` 保留在仓库里（单独跑看板时仍可用），容器不会用到。

supervisor 提供的额外好处：

- 给每个进程的输出加 `[名字]` 前缀再转发到容器 stdout ——
  否则 8 个进程的日志混在一起没法排查
- 崩溃后指数退避重启（2s→4s→…→60s 上限，稳定 120s 后重置）
- SIGTERM/SIGINT 时优雅停机，连子进程派生的孙进程一起清掉
- 启动前自检脚本和离线库是否就位，缺了立刻报错而不是运行时静默降级

看日志与状态：

```bash
docker compose logs -f proxy-tool
docker compose logs -f proxy-tool | grep '^\[gateway\]'   # 只看网关
```

---

## 两个自研组件

### bridge —— 让 proxy_pool 的代理出现在看板里

看板的 `backend.py` 本来就会读 proxy_pool 在 DB 0 的 `use_proxy` 哈希做字段补全
（`jhao_map()`），但那只对**已经存在于 DB 1 池子里**的成员生效。
也就是说 proxy_pool 抓到的代理默认根本进不了看板。

`bridge.py` 补的就是这一段：周期性 `GET {PROXY_API}/all/`，把新代理写进 DB 1，
之后交给看板自己的 validator 去验证、定位、淘汰。它只插入不存在的条目，
**不会覆盖** validator 已写好的 latency / country / 信用分，也不会调用 proxy_pool 的
`/delete/`，因此对上游池子完全只读。

> 看板的 validator 策略是"只留 <500ms"，所以从 proxy_pool 导入的慢代理会在下一轮
> 验证时被清掉。这是看板的既有设计，不是 bug。

### gateway —— 固定入口的轮换代理

代理池里的 IP 一直在变，业务侧不想每次都去调 API 换地址。gateway 提供一个稳定入口，
每条连接自动从池子里挑一个可用上游转发，失败自动换下一个：

```bash
curl -x http://localhost:8080  https://httpbin.org/ip
curl -x socks5h://localhost:1080 https://httpbin.org/ip

export http_proxy=http://localhost:8080 https_proxy=http://localhost:8080
```

- 入口协议：HTTP（含 `CONNECT` 隧道，所以 HTTPS 也走得通）、SOCKS5
- 上游协议：http / https / socks4 / socks4a / socks5 / socks5h
- 选取策略：按实测延迟排序取最快的一批，逐请求随机轮换
- 失败处理：连续失败达阈值后临时拉黑（默认 2 次 / 冷却 300s），不写回上游池子

> ### ⚠️ 安全提示
> `GATEWAY_USER` / `GATEWAY_PASS` 默认为空，此时 8080 和 1080 是**无鉴权的开放代理**。
> 一旦这两个端口能被公网访问到，会在很短时间内被扫描并滥用，流量算在你头上。
>
> 只在可信网络内使用，或者在 compose 里给 `proxy-tool` 加上这两个环境变量。
> 不需要网关就设 `SVC_GATEWAY=0` 并删掉对应的端口映射。

---

## 配置

`DB_CONN`、`PROXY_API`、`PYTHONUNBUFFERED`、`REDIS_HOST` 这些默认值全部写在镜像的
`ENV` 里，开箱可用 —— Redis 的默认地址就是 `redis`，正好等于 compose 里的服务名。
需要调整时在 compose 的 `proxy-tool` 服务下加 `environment:` 即可。

compose 里显式声明的只有三件必要的事，都不是可调参数：

| 配置 | 为什么必须有 |
| --- | --- |
| `NO_PROXY` / `no_proxy` | 宿主机 `~/.docker/config.json` 若配了 `proxies`（为拉镜像走代理，很常见），Docker 会自动把 `HTTP_PROXY` 注入容器。而看板的 `frontend.py` 是用 urllib 默认 opener 把 `/api/*` 反代到 `127.0.0.1:5051` 的，届时这个内部请求会被送去外部代理，看板 API 直接 502。大小写两份都要给，因为 urllib 和 requests 读的变量名大小写不一致 |
| `logging` 轮转 | 容器里 8 个进程一直在打日志（validator 每 30s 一轮），不轮转会吃满磁盘 |
| `stop_grace_period: 30s` | `docker stop` 默认只等 10s。supervisor 停机上界约 `STOP_TIMEOUT + 进程数×1s`；实测最坏情况（8 个子进程全部忽略 SIGTERM）为 10.8s，30s 有充足余量 |

另外 compose 没有显式声明 `networks` —— compose 会自动建一个项目专属网络，
功能与手写 `proxy-net` 完全等价。别的 compose 项目要接进来的话：

```yaml
networks:
  default:
    name: proxy-tool_default
    external: true
```

### 服务开关

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `SVC_PROXY_POOL` | `1` | proxy_pool 的 API 与调度进程 |
| `SVC_DASHBOARD` | `1` | 看板的 4 个进程 |
| `SVC_BRIDGE` | `1` | 代理导入（`PROXY_API` 为空时自动不启用） |
| `SVC_GATEWAY` | `1` | 8080 / 1080 轮换代理 |

### Redis

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `REDIS_HOST` | `redis` | 等于 compose 里的服务名 |
| `REDIS_PORT` | `6379` | |
| `REDIS_PASSWORD` | 空 | 有则用于拼 `DB_CONN` |
| `REDIS_DB` | `1` | 看板用的库号（proxy_pool 固定用 0） |
| `DB_CONN` | 由上面几项推导 | proxy_pool 的库地址，一般不用手动设 |

### 网关

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `GATEWAY_USER` / `GATEWAY_PASS` | 空 | 客户端鉴权。**留空 = 开放代理** |
| `GATEWAY_SOURCE` | `redis,api` | 上游来源，可选 `redis` / `api` / `static` |
| `GATEWAY_TOP_N` | `200` | 候选池保留最快的 N 个 |
| `GATEWAY_MAX_RETRIES` | `3` | 单次请求最多尝试几个上游 |
| `GATEWAY_FAIL_THRESHOLD` | `2` | 连续失败几次后拉黑 |
| `GATEWAY_COOLDOWN` | `300` | 拉黑时长（秒） |

完整清单见 `dashboard/gateway.py` 与 `dashboard/bridge.py` 的模块头注释。

proxy_pool 沿用上游的环境变量覆盖机制（`handler/configHandler.py`），
`HTTP_URL`、`VERIFY_TIMEOUT`、`POOL_SIZE_MIN` 等都可覆盖 `setting.py` 里的同名配置。

---

## 与上游的差异

### 构建与打包

| 改动 | 原因 |
| --- | --- |
| workflow 重写为多架构，合并成一个文件发 GHCR | 上游只出 amd64（**本项目要解决的核心问题**） |
| 两个项目合并为单镜像单容器 | 部署简化 |
| 基础镜像 alpine → Debian slim | glibc/manylinux 有现成 aarch64 wheel，跨架构免编译 |
| 不再使用 `proxy_pool.sh` | 直接调 `proxyPool.py` 的子命令，摆脱 bash 依赖 |
| 改为多阶段构建 | 编译器只留在 builder 阶段 |
| 以非 root（`10001:10001`）运行 | 最小权限 |
| 加 `HEALTHCHECK` | 让 compose 的 `depends_on: service_healthy` 可用 |

### 对上游源码的修改

**proxy_pool 的源码零修改。** dashboard 改了 3 个文件：

| 文件 | 改动 | 影响 |
| --- | --- | --- |
| `dashboard/searcher.py` | `import ip2region.util as util` → `import util` | 仓库里并没有 `ip2region` 这个包，原样保留会让 `import searcher` 抛 `ModuleNotFoundError`，被 `new_fetcher.py` 的 try/except 吞掉 —— ip2region 离线兜底**从未真正生效**过 |
| `dashboard/new_fetcher.py` | xdb 路径 `/app/ip2region.xdb` → 用 `__file__` 拼出 `data/ip2region.xdb` | 同上，上游写的是一个不存在的路径 |
| `dashboard/requirements.txt` | `redis>=5.0` → `redis==5.2.1` | 锁版本，保证多架构构建可复现 |

修复后已实测：`114.114.114.114` → `中国|江苏省|南京市|0|CN`。
这条链路现在由 `docker/assert_layout.py` 在构建期和 CI 里各验证一次，不会再悄悄坏掉。

另外删掉了上游 dashboard 的 `server.py`（一个 11 行的临时静态文件服务，与本项目无关）
和两份各自的 `Dockerfile` / `docker-compose.yml`（已被合并方案取代）。

### 新增

- `dashboard/bridge.py` — proxy_pool → 看板的代理导入
- `dashboard/gateway.py` — HTTP / SOCKS5 轮换代理入口
- `docker/supervisor.py` — 容器内 8 进程管理
- `docker/assert_layout.py` — 布局断言（构建期 + CI）
- `docker/healthcheck.sh` — 按服务开关做健康检查

---

## 目录结构

```
.
├── .github/workflows/ci.yml    # 唯一的 workflow：测试 → 多架构构建 → 发布 GHCR
├── Dockerfile                  # 合并镜像
├── docker-compose.yml          # 两个容器：redis + proxy-tool
├── docker/
│   ├── supervisor.py           # 容器内进程管理
│   ├── assert_layout.py        # 布局断言，Dockerfile 与 CI 共用
│   └── healthcheck.sh
├── api/ db/ fetcher/ handler/ helper/ util/    # proxy_pool 源码（零修改）
├── proxyPool.py  setting.py  requirements.txt
├── proxy_pool.sh               # 上游的启动脚本，容器不用，保留供本地直接运行
├── tests/                      # proxy_pool 测试（248 例）
├── docs/                       # proxy_pool 的 mkdocs 文档
└── dashboard/
    ├── frontend.py backend.py validator.py quality.py geo.py new_fetcher.py
    ├── searcher.py util.py     # ip2region 离线库读取（Apache-2.0）
    ├── dashboard.py            # 上游自带的看门狗，容器不用
    ├── bridge.py gateway.py    # 自研
    ├── data/                   # 离线 GeoIP 库，运行必需，勿加入 .gitignore
    └── static/                 # 看板前端
```

---

## 本地校验

推之前可以先自查：

```bash
hadolint Dockerfile
shellcheck docker/healthcheck.sh
actionlint
docker compose config -q

pip install -r requirements.txt -r requirements-test.txt -r dashboard/requirements.txt
pytest -q

cd dashboard && python ../docker/assert_layout.py dashboard && cd ..
python docker/assert_layout.py proxy_pool
```

---

## 许可与出处

- **proxy_pool** — MIT License，Copyright (c) 2017 J_hao104。见 [`LICENSE`](LICENSE)。
- **proxy-pool-dashboard** — 上游仓库**未声明许可证**，默认即保留所有权利。
  自用没问题；若要公开分发或商用，建议先联系原作者确认授权。
- **ip2region**（`dashboard/searcher.py`、`dashboard/util.py`）— Apache-2.0，
  Copyright 2022 The Ip2Region Authors。
- `dashboard/data/ipdb.bin` 源自 DB-IP Country Lite，`dashboard/data/ip2region.xdb`
  源自 ip2region，各自适用其原始许可条款。

本项目新增的 `bridge.py`、`gateway.py`、`docker/` 下的脚本、workflow 与容器化配置遵循 MIT。

## 免责声明

公开代理来源不可控，请勿用于任何违法用途，也不要传输敏感数据 —— 中间节点对流量是可见的。
`gateway` 默认不鉴权，暴露到公网前请务必阅读上面的安全提示。
