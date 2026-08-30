# Docker 部署

!!! warning "本页是上游 proxy_pool 单仓时期的文档，已不适用于本仓库"

    本仓库把 proxy_pool 与 dashboard 合并，并改为自建**多架构镜像（含 arm64）**。
    下文提到的 `jhao104/proxy_pool:latest` 只有 amd64，在 arm64 机器上会报
    `no matching manifest for linux/arm64/v8`。

    正确做法见仓库根目录的 `README.md`：

    ```bash
    docker compose up -d --build          # 本地构建
    # 或在 .env 里配好 PROXY_POOL_IMAGE / DASHBOARD_IMAGE 后
    docker compose pull && docker compose up -d
    ```

    本页仅作为上游行为的参考保留。

## 使用 Docker 镜像

拉取并运行 Docker 镜像：

```console
docker pull jhao104/proxy_pool:latest

docker run --env DB_CONN=redis://:password@ip:port/0 -p 5010:5010 jhao104/proxy_pool:latest
```

`DB_CONN` 环境变量会覆盖 `setting.py` 中的数据库连接配置。

## 使用 docker-compose

项目根目录下的 `docker-compose.yml` 定义了 ProxyPool 和 Redis 两个服务：

```yaml
version: '2'
services:
  proxy_pool:
    build: .
    container_name: proxy_pool
    ports:
      - "5010:5010"
    links:
      - proxy_redis
    environment:
      DB_CONN: "redis://@proxy_redis:6379/0"
  proxy_redis:
    image: "redis"
    container_name: proxy_redis
```

启动：

```console
docker-compose up -d
```

## 容器环境注意事项

在 Docker 容器中，建议使用前台模式启动服务：

```console
./proxy_pool.sh start --fg
```

Dockerfile 中的 ENTRYPOINT 配置：

```dockerfile
ENTRYPOINT ["tini", "--", "bash", "proxy_pool.sh", "start", "--fg"]
```