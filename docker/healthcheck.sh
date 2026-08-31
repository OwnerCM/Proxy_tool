#!/bin/sh
# 容器健康检查。只检查被启用的服务，避免关掉某个服务后健康检查恒失败。
set -eu

enabled() {
    # $1=变量值 $2=默认值(1/0)；空值取默认
    v="${1:-}"
    [ -z "$v" ] && v="$2"
    case "$(printf '%s' "$v" | tr '[:upper:]' '[:lower:]')" in
        0|false|no|off) return 1 ;;
        *) return 0 ;;
    esac
}

if enabled "${SVC_PROXY_POOL:-}" 1; then
    curl -fsS --max-time 5 "http://127.0.0.1:${PORT:-5010}/count/" >/dev/null \
        || { echo "proxy_pool API 无响应"; exit 1; }
fi

if enabled "${SVC_WEB:-${SVC_DASHBOARD:-}}" 1; then
    curl -fsS --max-time 10 "http://127.0.0.1:${WEB_PORT:-5050}/api/stats" >/dev/null \
        || { echo "展示层无响应"; exit 1; }
fi

exit 0
