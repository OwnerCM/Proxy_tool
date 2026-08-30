# Proxy Pool Dashboard

[![Docker](https://img.shields.io/badge/Docker-✓-2496ED)](https://docker.com)
[![Python](https://img.shields.io/badge/Python-3.13-blue)](https://python.org)
[![Redis](https://img.shields.io/badge/Redis-7-red)](https://redis.io)

高质量代理池采集/验证/Geo定位/展示一体化系统。**只保留 <500ms 代理**，慢代理直接删除，不做分层。

## ✨ 核心功能

| 功能 | 说明 |
|------|------|
| 采集 | **24** 源（ProxyScrape / GitHub 代理列表 / 快代理 / 89ip 等），采集阶段筛选：无端口+协议的丢弃，TCP 连通性预检；`fetch_github_multi()` 泛型多协议采集器消除重复代码 |
| 验证 | 50 线程并发 TCP+HTTP 验证，**<500ms 保留，≥500ms 直接删除**（无 S/A/B/C/D 分层） |
| GeoIP | **在线优先**：7 个 API 轮询（ip-api / ip9 / freeipapi / ipwhois / ipinfo / ipapi / ip2location）+ 代理池轮换防限流；离线 DB-IP 库兜底 |
| 国内城市 | CN IP 专项：geoworker 每 60s 批量在线查询，返回中文省份+城市 |
| 海外复测 | 海外代理走代理链二次验证，确保延迟数据真实 | 海外服务器无需走代理
| 前端 | SPA 单页应用：首页国旗+中文国名+节点数 → 点进钻取页筛选（协议/城市/延迟/搜索）→ 窗口分页 |
| 进程守护 | dashboard.py 看门狗，子进程崩溃指数退避重启，稳定 2 分钟后重置 |

## 🏗 架构

```
                    ┌─────────────┐
                    │  dashboard  │  PID 1 看门狗
                    └──┬──┬──┬────┘
          ┌────────────┘  │  └────────────┐
          ▼               ▼               ▼
  ┌───────────┐   ┌───────────┐   ┌───────────┐
  │ validator │   │  backend  │   │ frontend  │
  │  30s/轮   │   │  :5051    │   │  :5050    │
  │ 验证+清理  │   │ API+缓存  │   │ 静态+反代 │
  └─────┬─────┘   └─────┬─────┘   └───────────┘
        │               │
        ▼               ▼
  ┌─────────────────────────────┐
  │          Redis (DB 1)       │
  │  proxies:pool (ZSET 信用分) │
  │  proxy:{ip}:{port} (Hash)   │
  │  geo:{ip} (Geo缓存 7天TTL)  │
  └──────────┬──────────────────┘
             │
    ┌────────┴────────┐
    │   geo.py        │
    │ 在线API(7个)     │
    │ +离线DB-IP兜底   │
    │ +代理池轮换      │
    └────────┬────────┘
             │
    ┌────────┴────────┐
    │  new_fetcher.py  │
    │ 20+源 并发采集    │
    │ TCP预检 + 去重   │
    └─────────────────┘
```

## 📦 快速开始

```bash
git clone https://github.com/abclq/proxy-pool-dashboard.git
cd proxy-pool-dashboard
docker-compose up -d
```

| 服务 | 地址 |
|------|------|
| 🖥 Dashboard | http://localhost:5050 |
| 📡 API | http://localhost:5050/api/proxies |
| 📊 统计 | http://localhost:5050/api/stats |

## 🔌 API

### GET `/api/proxies`

所有代理均为 S 级（<500ms），不需要 grade 参数。

| 参数 | 说明 |
|------|------|
| `country` | 国家代码：`CN` 中国 / `US` 美国 / `!CN` 海外，默认全部 |
| `location` | 城市/地区关键词匹配（中文），如 `location=杭州` |
| `protocol` | `http` / `https` / `socks4` / `socks5` |
| `delay` | 延迟上限（ms），如 `delay=500` |
| `search` | IP 模糊搜索 |
| `sort` | `delay` / `credit`，默认 `delay` |
| `order` | `asc` / `desc`，默认 `asc` |
| `page` / `limit` | 分页，默认 page=1 limit=50，最大 limit=200 |

```json
{
  "total": 25000, "total_matched": 2074,
  "proxies": [{
    "ip": "123.45.67.89", "port": "8080",
    "protocol": "http", "delay": 234,
    "country": "CN", "location": "浙江 杭州",
    "credit": 35, "source": "proxyscrape"
  }]
}
```

### GET `/api/stats`

```json
{
  "total": 25894,
  "grades": {"s": 25894},
  "regions": {"CN": 12000, "US": 3000, "JP": 800, …}
}
```

### GET `/api/country/{code}`

钻取指定国家的所有代理，参数同 `/api/proxies`。

## 🗺 GeoIP 管线（在线优先）

1. **Redis 缓存**（7 天 TTL）—— 命中直接返回，零延迟
2. **在线 API 轮询**（7 个）—— ip-api → ip9 → freeipapi → ipwhois → ipinfo → ipapi → ip2location
3. **代理池轮换**—— API 返回 403/429 时，从 Redis 代理池取 HTTP 代理重新请求
4. **离线 DB-IP**—— 兜底，355K 条 IP 段，内存二分查找，3.6μs/次
5. **CN 专项**—— 每分钟批量在线查询国内 IP 城市信息，显示中文省份+城市

## 📂 代码结构

```
proxy-pool-dashboard/
├── dashboard.py        # PID 1 看门狗：启动3子进程 + 崩溃指数退避重启
├── new_fetcher.py      # 采集引擎：24 源并发拉取，TCP预检，去重入库；fetch_github_multi() 泛型多协议采集
├── validator.py        # 验证引擎：50线程，<500ms保留，≥500ms删除，海外走代理复测
├── geo.py              # GeoIP：7在线API + 离线DB-IP + Redis缓存 + 代理轮换
├── backend.py          # API服务：多线程 HTTP，索引缓存，中文城市名显示
├── frontend.py         # 静态文件 + /api 反代到 backend:5051
├── data/
│   ├── ipdb.bin        # 离线 IP 二进制库（DB-IP Country Lite）
│   └── ip2region.xdb   # ip2region 离线库（备用）
├── static/
│   ├── index.html      # SPA 入口
│   ├── app.js          # 前端逻辑：createElement 防 XSS，AbortController 防竞态
│   └── style.css       # GitHub 暗色主题
├── docker-compose.yml
├── Dockerfile
└── requirements.txt    # 仅 redis>=5.0
```

## 🔧 技术要点

- **严格阈值**：<500ms 保留，≥500ms 调用 `_remove_proxy()` 从 ZSET + Hash 双删，不做分层
- **XSS 防护**：前端全部用 `createElement()` + `textContent`，无 `innerHTML` 注入点
- **防竞态**：快速切换筛选时 AbortController 取消旧请求，避免结果错乱
- **窗口分页**：10k+ 代理时最多显示 7 个页码按钮
- **索引缓存**：`all_proxies()` 60s TTL，`stats` 900s TTL，避免每次全量 hgetall
- **多线程 HTTP**：ThreadingMixIn + daemon_threads，请求不互相阻塞
- **启动预热**：backend 启动时同步建立索引缓存
- **进程守护**：dashboard.py 指数退避重启（2s→4s→8s→…→60s 上限），稳定 120s 后重置
- **Redis 连接池**：backend 用 connection_pool（线程安全），validator 50线程*4连接
- **国内城市纯净度**：`_format_location()` + `location_display()` 双重过滤英文省份名和垃圾词汇（阿里/腾讯/百度/电信/联通/云等）

## 🚀 部署说明

### Docker Compose（标准）

```bash
docker-compose up -d
```

### 生产部署（自定义端口/网络）

`docker-compose.yml` 默认暴露 5050。如需改端口，修改 `ports` 映射：

```yaml
ports:
  - "8080:5050"
```

首次启动后，采集引擎自动运行，约 10-30 分钟后代理池达到可用规模。

### 代理验证周期

- 验证器每 30s 一轮，对未验证的新代理做 TCP+HTTP 测试
- 已验证代理每 300s 重检一次
- 新源采集每 30 分钟一次
- CN IP 城市信息每分钟在线查询
- Geo 离线库每月自动更新
