#!/usr/bin/env python3
"""容器内进程管理器 —— proxy_pool + dashboard 合并镜像里，tini 之下的总管。

为什么需要它
------------
两个项目合并成一个镜像后，容器里要同时跑 8 个进程，而上游各自的启动方式
在合并后都不再适用：

* proxy_pool 的 `proxy_pool.sh` 是个 bash 脚本（用了 `[[ ]]` 等 bash 专有语法），
  而它做的事只是拉起 `proxyPool.py server` 和 `proxyPool.py schedule` 两个进程。
  这里直接调这两个子命令，脚本和它带来的 bash 依赖一并不需要了。
* dashboard 的 `dashboard.py` 是它自己的看门狗，只管它自己那 4 个子进程。
  合并后由本模块统一接管；dashboard.py 保留在仓库里（单独跑看板时仍可用），
  但容器不会用到它。

Redis 不在这里管 —— 它是独立容器，见 docker-compose.yml。

做的事
------
1. 按环境变量决定启动哪些服务（见 build_services）
2. 统一给各进程输出打上 `[名字]` 前缀再转发到容器 stdout
   —— 这是单容器方案最需要的东西，否则 8 个进程的日志混成一团没法排查
3. 子进程崩溃后指数退避重启（2s→4s→…→60s 上限，稳定 120s 后重置）
4. 收到 SIGTERM/SIGINT 时优雅停掉所有子进程（含它们派生的孙进程）
5. 启动前做前置检查：脚本在不在、离线库在不在
   —— 更新过代码后如果文件被改名/挪走，这里立刻报出来，而不是运行时静默降级

目录布局
--------
dashboard 必须落在 /app：上游 backend.py 里硬编码了 `sys.path.insert(0, "/app")`。
proxy_pool 必须落在别处（/opt/proxy_pool）：两边都有名为 `util` 的顶层模块
（proxy_pool 是 util/ 包，dashboard 是 ip2region 的 util.py），
放同一目录会互相覆盖，且 ip2region 会静默失效。

环境变量
--------
SVC_PROXY_POOL / SVC_DASHBOARD / SVC_BRIDGE / SVC_GATEWAY
                    各服务开关，1/0，默认全开
REDIS_HOST/PORT     Redis 位置，默认 redis:6379（等于 compose 里的服务名）
REDIS_PASSWORD      Redis 密码，有则用于拼 DB_CONN
REDIS_DB            dashboard 用的库号，默认 1（proxy_pool 固定用 0）
DB_CONN             proxy_pool 的库地址；不设置则由上面几项推导
PROXY_API           proxy_pool 的 API 地址，bridge/gateway 会用；留空则不启用 bridge
APP_DIR / PP_DIR    两个代码目录，默认 /app 与 /opt/proxy_pool（改它们便于本地测试）
STOP_TIMEOUT        优雅停止的等待秒数，默认 10
REDIS_WAIT          启动时等 Redis 就绪的秒数，默认 30
"""

import os
import signal
import socket
import subprocess
import sys
import threading
import time

APP_DIR = os.environ.get("APP_DIR", "/app")
PP_DIR = os.environ.get("PP_DIR", "/opt/proxy_pool")
STOP_TIMEOUT = int(os.environ.get("STOP_TIMEOUT", "10") or 10)
REDIS_WAIT = int(os.environ.get("REDIS_WAIT", "30") or 30)

BACKOFF_BASE = 2
BACKOFF_MAX = 60
STABLE_AFTER = 120

_print_lock = threading.Lock()
_shutting_down = threading.Event()


def log(msg):
    with _print_lock:
        sys.stdout.write(f"[supervisor] {msg}\n")
        sys.stdout.flush()


def truthy(value, default=True):
    if value is None or value == "":
        return default
    return value.strip().lower() not in ("0", "false", "no", "off")


class Service:
    def __init__(self, name, argv, cwd, enabled, requires=()):
        self.name = name
        self.argv = argv
        self.cwd = cwd
        self.enabled = enabled
        self.requires = requires          # 启动前必须存在的文件（相对 cwd）
        self.proc = None
        self.restarts = 0
        self.started_at = 0.0

    def missing_files(self):
        return [f for f in self.requires
                if not os.path.exists(os.path.join(self.cwd, f))]


def build_services(env):
    py = sys.executable

    pool_on = truthy(env.get("SVC_PROXY_POOL"), default=True)
    dash_on = truthy(env.get("SVC_DASHBOARD"), default=True)
    # bridge 只有在配了 PROXY_API 时才有意义
    bridge_on = truthy(env.get("SVC_BRIDGE"), default=True) and bool(env.get("PROXY_API", "").strip())
    gateway_on = truthy(env.get("SVC_GATEWAY", env.get("GATEWAY_ENABLED")), default=True)
    # gateway.py 自己也会看 GATEWAY_ENABLED，若为 0 会立刻正常退出。
    # 这里把它对齐成 supervisor 的决定，避免"supervisor 要起、gateway 自己不干"
    # 造成的无限重启循环。开关的唯一真相来源是 SVC_GATEWAY。
    env["GATEWAY_ENABLED"] = "1" if gateway_on else "0"

    return [
        Service("pp-api", [py, "proxyPool.py", "server"], PP_DIR, pool_on,
                requires=["proxyPool.py"]),
        Service("pp-sched", [py, "proxyPool.py", "schedule"], PP_DIR, pool_on,
                requires=["proxyPool.py"]),

        Service("dash-api", [py, "-u", "backend.py"], APP_DIR, dash_on,
                requires=["backend.py", "geo.py"]),
        Service("dash-web", [py, "-u", "frontend.py"], APP_DIR, dash_on,
                requires=["frontend.py", "static/index.html"]),
        Service("validator", [py, "-u", "validator.py"], APP_DIR, dash_on,
                requires=["validator.py", "new_fetcher.py"]),
        Service("quality", [py, "-u", "quality.py"], APP_DIR, dash_on,
                requires=["quality.py"]),

        Service("bridge", [py, "-u", "bridge.py"], APP_DIR, bridge_on,
                requires=["bridge.py"]),
        Service("gateway", [py, "-u", "gateway.py"], APP_DIR, gateway_on,
                requires=["gateway.py"]),
    ]


def resolve_env():
    """补齐派生的环境变量，让上游代码不需要任何修改就能拿到正确配置。"""
    env = dict(os.environ)

    host = env.get("REDIS_HOST", "").strip() or "redis"
    port = env.get("REDIS_PORT", "").strip() or "6379"

    # 显式写回：dashboard 各模块里 REDIS_HOST 的默认值并不一致
    # （new_fetcher.py 写 "redis"，validator/backend/quality/geo 写 "proxy-redis"），
    # 所以这里统一喂准确的值，不依赖任何模块自己的默认值。
    env["REDIS_HOST"] = host
    env["REDIS_PORT"] = port
    env.setdefault("REDIS_DB", "1")

    # proxy_pool 用 DB 0，dashboard 用 DB 1，共用一个实例但互不干扰
    if not env.get("DB_CONN", "").strip():
        password = env.get("REDIS_PASSWORD", "").strip()
        auth = f":{password}@" if password else "@"
        env["DB_CONN"] = f"redis://{auth}{host}:{port}/0"

    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


def pump_output(name, stream):
    """把子进程输出逐行加前缀转发到容器 stdout。"""
    prefix = f"[{name}] "
    try:
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip("\n")
            with _print_lock:
                sys.stdout.write(prefix + line + "\n")
                sys.stdout.flush()
    except (ValueError, OSError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


def spawn(svc, env):
    svc.proc = subprocess.Popen(
        svc.argv,
        cwd=svc.cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        # 独立会话：子进程自己再派生的孙进程（validator 会拉起 new_fetcher）
        # 能被整组一起干掉，不会留下孤儿
        start_new_session=True,
    )
    svc.started_at = time.time()
    threading.Thread(target=pump_output, args=(svc.name, svc.proc.stdout),
                     daemon=True).start()
    log(f"{svc.name} 已启动 (pid={svc.proc.pid})")


def signal_group(proc, sig):
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError):
        try:
            proc.send_signal(sig)
        except ProcessLookupError:
            pass


def wait_for_tcp(host, port, timeout):
    """等某个 TCP 端口可连接。用于等 Redis 就绪，避免子进程一上来就连不上库、
    白白进入退避重启。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, int(port)), 1):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def preflight(services, env):
    """启动前自检。手动同步上游后如果结构变了，这里立刻报错而不是静默降级。"""
    problems = []

    for svc in services:
        if not svc.enabled:
            continue
        if not os.path.isdir(svc.cwd):
            problems.append(f"{svc.name}: 目录不存在 {svc.cwd}")
            continue
        missing = svc.missing_files()
        if missing:
            problems.append(f"{svc.name}: {svc.cwd} 下缺少 {', '.join(missing)}")

    if problems:
        for p in problems:
            log(f"❌ 前置检查失败 —— {p}")
        log("代码目录结构可能已变化（比如刚更新过上游代码），请检查 README 的目录说明")
        return False

    if any(s.enabled and s.name == "validator" for s in services):
        # 离线 GeoIP 库缺失只会让地理定位降级，不影响主流程，所以只告警不拦截
        for rel, desc in (("data/ip2region.xdb", "ip2region 离线库"),
                          ("data/ipdb.bin", "DB-IP 离线库")):
            if not os.path.exists(os.path.join(APP_DIR, rel)):
                log(f"⚠️  {APP_DIR}/{rel} 不存在，{desc}兜底将不可用")

    if any(s.enabled and s.name == "gateway" for s in services):
        if not env.get("GATEWAY_USER", "").strip():
            log("⚠️  网关未设置 GATEWAY_USER/GATEWAY_PASS —— 这是无鉴权的开放代理，"
                "切勿把 8080/1080 暴露到公网")

    return True


def shutdown(services):
    _shutting_down.set()
    running = [s for s in services if s.proc and s.proc.poll() is None]
    if not running:
        return
    log(f"正在停止 {len(running)} 个进程…")
    for svc in running:
        signal_group(svc.proc, signal.SIGTERM)

    # 停机总时长必须有上界，且要小于 compose 的 stop_grace_period，
    # 否则我们还没收完尾就被 Docker 用 SIGKILL 打断。
    # 上界 ≈ STOP_TIMEOUT + 进程数 × 1s。
    deadline = time.time() + STOP_TIMEOUT
    for svc in running:
        remaining = max(0.1, deadline - time.time())
        try:
            svc.proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            log(f"{svc.name} 未在 {STOP_TIMEOUT}s 内退出，强制结束")
            signal_group(svc.proc, signal.SIGKILL)
            try:
                svc.proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
    log("全部进程已停止")


def main():
    env = resolve_env()
    services = build_services(env)

    enabled = [s for s in services if s.enabled]
    skipped = [s.name for s in services if not s.enabled]
    log(f"启用: {', '.join(s.name for s in enabled) or '(无)'}")
    if skipped:
        log(f"跳过: {', '.join(skipped)}")
    if not enabled:
        log("没有任何服务被启用，退出")
        return 1

    if not preflight(services, env):
        return 2

    signal.signal(signal.SIGTERM, lambda *_: shutdown(services) or sys.exit(0))
    signal.signal(signal.SIGINT, lambda *_: shutdown(services) or sys.exit(0))

    # 等 Redis 就绪再拉起子进程。compose 的 depends_on 已经保证了启动顺序，
    # 但 Redis 重启、或用 docker run 单独起容器时仍可能没就绪，
    # 这里等一下能避免一堆子进程同时连不上库、白白进入退避重启。
    host, port = env["REDIS_HOST"], env["REDIS_PORT"]
    if wait_for_tcp(host, port, REDIS_WAIT):
        log(f"Redis {host}:{port} 可连接")
    else:
        log(f"⚠️  {REDIS_WAIT}s 内连不上 Redis {host}:{port}，仍继续启动"
            f"（子进程会自行重试）")

    for svc in enabled:
        spawn(svc, env)

    log("全部服务已拉起，进入监控循环")

    try:
        while not _shutting_down.is_set():
            for svc in enabled:
                if svc.proc is None:
                    continue
                code = svc.proc.poll()
                if code is None:
                    continue
                if _shutting_down.is_set():
                    break

                uptime = time.time() - svc.started_at
                if uptime > STABLE_AFTER:
                    svc.restarts = 0      # 之前跑得很稳，退避计数归零
                svc.restarts += 1
                delay = min(BACKOFF_MAX, BACKOFF_BASE * (2 ** (svc.restarts - 1)))
                log(f"{svc.name} 退出 (code={code}, 已运行 {uptime:.0f}s)，"
                    f"{delay}s 后重启（第 {svc.restarts} 次）")

                if _shutting_down.wait(delay):
                    break
                spawn(svc, env)
            _shutting_down.wait(1)
    finally:
        shutdown(services)
    return 0


if __name__ == "__main__":
    sys.exit(main())
