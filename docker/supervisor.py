#!/usr/bin/env python3
"""容器内进程管理器 —— proxy_pool + dashboard 合并镜像里，tini 之下的总管。

职责划分
--------
proxy_pool 是唯一的数据平面（采集 + 验证 + 存储 + 原生 API，数据在 Redis DB 0），
web 是只读展示层，gateway 是固定入口的轮换出口。三者不重复。

上游各自的启动方式在合并后都不适用：

* proxy_pool 的 `proxy_pool.sh` 是个 bash 脚本（用了 `[[ ]]` 等 bash 专有语法），
  而它做的事只是拉起 `proxyPool.py server` 和 `proxyPool.py schedule` 两个进程。
  这里直接调这两个子命令，脚本和它带来的 bash 依赖一并不需要了。
* 看板原来自带看门狗和一整套采集/验证进程，与 proxy_pool 完全重复且质量策略冲突，
  已全部移除，只留下展示层。

Redis 不在这里管 —— 它是独立容器，见 docker-compose.yml。

做的事
------
1. 按环境变量决定启动哪些服务（见 build_services）
2. 统一给各进程输出打上 `[名字]` 前缀再转发到容器 stdout
   —— 否则几个进程的日志混成一团没法排查
3. 子进程崩溃后指数退避重启（2s→4s→…→60s 上限，稳定 120s 后重置）
4. 收到 SIGTERM/SIGINT 时优雅停掉所有子进程（含它们派生的孙进程）
5. 启动前做前置检查：脚本在不在、离线库在不在
   —— 更新过代码后如果文件被改名/挪走，这里立刻报出来，而不是运行时静默降级

环境变量
--------
SVC_PROXY_POOL / SVC_WEB / SVC_GATEWAY
                    各服务开关，1/0，默认全开
REDIS_HOST/PORT     Redis 位置，默认 redis:6379（等于 compose 里的服务名）
REDIS_PASSWORD      Redis 密码，有则用于拼 DB_CONN
PROXY_POOL_DB       proxy_pool 的库号，默认 0（DB 1 现在只放 geo 缓存）
DB_CONN             proxy_pool 的库地址；不设置则由上面几项推导
PROXY_API           proxy_pool 的 API 地址，gateway 的 api 来源会用
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
    web_on = truthy(env.get("SVC_WEB", env.get("SVC_DASHBOARD")), default=True)
    gateway_on = truthy(env.get("SVC_GATEWAY", env.get("GATEWAY_ENABLED")), default=True)
    # gateway.py 自己也会看 GATEWAY_ENABLED，若为 0 会立刻正常退出。
    # 这里把它对齐成 supervisor 的决定，避免"supervisor 要起、gateway 自己不干"
    # 造成的无限重启循环。开关的唯一真相来源是 SVC_GATEWAY。
    env["GATEWAY_ENABLED"] = "1" if gateway_on else "0"

    return [
        # 数据平面：采集 + 验证 + 原生 API，是全系统唯一写代理数据的地方
        Service("pp-api", [py, "proxyPool.py", "server"], PP_DIR, pool_on,
                requires=["proxyPool.py"]),
        Service("pp-sched", [py, "proxyPool.py", "schedule"], PP_DIR, pool_on,
                requires=["proxyPool.py"]),

        # 展示层：只读 DB 0
        Service("web", [py, "-u", "web.py"], APP_DIR, web_on,
                requires=["web.py", "geo.py", "static/index.html"]),

        # 出口：固定入口的轮换代理
        Service("gateway", [py, "-u", "gateway.py"], APP_DIR, gateway_on,
                requires=["gateway.py"]),
    ]


def resolve_env():
    """补齐派生的环境变量，让上游代码不需要任何修改就能拿到正确配置。"""
    env = dict(os.environ)

    host = env.get("REDIS_HOST", "").strip() or "redis"
    port = env.get("REDIS_PORT", "").strip() or "6379"

    # 显式写回：geo.py 里 REDIS_HOST 的默认值是 "proxy-redis"，和本项目的服务名不一致，
    # 所以统一喂准确的值，不依赖任何模块自己的默认值。
    env["REDIS_HOST"] = host
    env["REDIS_PORT"] = port
    env.setdefault("PROXY_POOL_DB", "0")

    # proxy_pool 的代理数据在 DB 0；DB 1 只被 geo.py 用作地理缓存
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

    if any(s.enabled and s.name == "web" for s in services):
        # 离线 GeoIP 库缺失只会让地理定位降级，不影响主流程，所以只告警不拦截
        ipdb = os.path.join(APP_DIR, "data", "ipdb.bin")
        if not os.path.exists(ipdb):
            log(f"⚠️  {ipdb} 不存在，离线 GeoIP 将不可用，国家分组会退化为未知")

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
