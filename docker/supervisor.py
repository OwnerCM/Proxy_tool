#!/usr/bin/env python3
"""容器内进程管理器 —— tini 之下管三个进程。

为什么还需要它
--------------
一个容器里必须跑三个进程，而这三个都是阻塞式的、合不了：

    api        gunicorn 跑 Flask（gunicorn 自己还会 fork 4 个 worker）
    schedule   APScheduler 的 BlockingScheduler
    serve      看板 + 网关（这两个都是线程模型，已合并进同一进程）

所以需要有人负责启动它们、把日志汇聚到容器 stdout、并在退出时收拾干净。
用自己写的而不是 supervisord，是因为不想为此引入一个外部依赖 + 配置文件，
而且这样能在本地直接跑测试。

设计取向：crash-only
--------------------
任一进程退出就结束整个容器，由 Docker 的 restart 策略统一重启，
本模块不做单进程重启和退避。理由：

* 简单。少掉一整套退避/计数/稳定期判定的逻辑。
* 可观测。进程反复挂会表现为容器反复重启，你的监控本来就看这一层；
  而"容器内部悄悄重启子进程、对外仍然 healthy"是会掩盖问题的。
* 代价可接受。状态全在外部 Redis 里，重启一次就是几秒钟的中断。
  Docker 的 restart 策略自带指数退避，崩溃循环不会打满 CPU。

做的事
------
1. 按 SVC_* 决定启动哪些进程
2. 给各进程输出加 `[名字]` 前缀再转发到容器 stdout（否则几个进程的日志混成一团）
3. 启动前检查脚本在不在 —— 改了代码后文件被改名/挪走时立刻报错，而非运行时静默降级
4. 任一进程退出 → 停掉其余进程 → 以非零码退出，交给 Docker 重启
5. 收到 SIGTERM/SIGINT → 优雅停掉全部进程（含它们派生的孙进程）

环境变量
--------
SVC_PROXY_POOL / SVC_WEB / SVC_GATEWAY   各服务开关，1/0，默认全开
                                         （SVC_GATEWAY 由 serve 进程内部读取）
REDIS_HOST / REDIS_PORT / REDIS_PASSWORD Redis 位置，默认 redis:6379
PROXY_POOL_DB                            proxy_pool 的库号，默认 0
DB_CONN                                  不设置则由上面几项推导
APP_DIR                                  代码目录，默认 /app（改它便于本地测试）
STOP_TIMEOUT                             优雅停止的等待秒数，默认 10
REDIS_WAIT                               启动时等 Redis 就绪的秒数，默认 30
"""

import os
import signal
import socket
import subprocess
import sys
import threading
import time

APP_DIR = os.environ.get("APP_DIR", "/app")
STOP_TIMEOUT = int(os.environ.get("STOP_TIMEOUT", "10") or 10)
REDIS_WAIT = int(os.environ.get("REDIS_WAIT", "30") or 30)

_print_lock = threading.Lock()
_stopping = threading.Event()


def log(msg):
    with _print_lock:
        sys.stdout.write(f"[supervisor] {msg}\n")
        sys.stdout.flush()


def truthy(value, default=True):
    if value is None or value == "":
        return default
    return value.strip().lower() not in ("0", "false", "no", "off")


class Service:
    def __init__(self, name, argv, requires):
        self.name = name
        self.argv = argv
        self.requires = requires   # 启动前必须存在的文件（相对 APP_DIR）
        self.proc = None


def build_services(env):
    py = sys.executable
    pool_on = truthy(env.get("SVC_PROXY_POOL"))
    # 网关是否启用由 serve 进程内部按 SVC_GATEWAY 判断，
    # 所以只要看板或网关任一开启，serve 就得起
    web_on = truthy(env.get("SVC_WEB", env.get("SVC_DASHBOARD")))
    gateway_on = truthy(env.get("SVC_GATEWAY", env.get("GATEWAY_ENABLED")))

    services = []
    if pool_on:
        services.append(Service("api", [py, "proxyPool.py", "server"], ["proxyPool.py"]))
        services.append(Service("schedule", [py, "proxyPool.py", "schedule"], ["proxyPool.py"]))
    if web_on or gateway_on:
        services.append(Service("serve", [py, "-u", "web.py"],
                                ["web.py", "geo.py", "static/index.html"]))
    return services


def resolve_env():
    """补齐派生的环境变量，让各模块不必依赖自己那些不一致的默认值。"""
    env = dict(os.environ)
    host = env.get("REDIS_HOST", "").strip() or "redis"
    port = env.get("REDIS_PORT", "").strip() or "6379"
    env["REDIS_HOST"] = host
    env["REDIS_PORT"] = port
    env.setdefault("PROXY_POOL_DB", "0")

    # 代理数据在 DB 0；DB 1 只被 geo.py 用作地理缓存
    if not env.get("DB_CONN", "").strip():
        password = env.get("REDIS_PASSWORD", "").strip()
        auth = f":{password}@" if password else "@"
        env["DB_CONN"] = f"redis://{auth}{host}:{port}/0"

    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


def pump(name, stream):
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
        svc.argv, cwd=APP_DIR, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        # 独立会话：子进程再派生的孙进程（gunicorn 的 worker 等）
        # 能被整组一起干掉，不会留下孤儿
        start_new_session=True,
    )
    threading.Thread(target=pump, args=(svc.name, svc.proc.stdout), daemon=True).start()
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
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, int(port)), 1):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def preflight(services):
    problems = []
    if not os.path.isdir(APP_DIR):
        problems.append(f"代码目录不存在: {APP_DIR}")
    else:
        for svc in services:
            missing = [f for f in svc.requires
                       if not os.path.exists(os.path.join(APP_DIR, f))]
            if missing:
                problems.append(f"{svc.name}: {APP_DIR} 下缺少 {', '.join(missing)}")
    if problems:
        for p in problems:
            log(f"❌ 前置检查失败 —— {p}")
        return False

    ipdb = os.path.join(APP_DIR, "data", "ipdb.bin")
    if any(s.name == "serve" for s in services) and not os.path.exists(ipdb):
        # 只影响地理定位，不拦截启动
        log(f"⚠️  {ipdb} 不存在，离线 GeoIP 将不可用，国家分组会退化为未知")
    return True


def stop_all(services):
    _stopping.set()
    running = [s for s in services if s.proc and s.proc.poll() is None]
    if not running:
        return
    log(f"正在停止 {len(running)} 个进程…")
    for svc in running:
        signal_group(svc.proc, signal.SIGTERM)

    # 停机总时长要有上界且小于 compose 的 stop_grace_period，
    # 否则还没收完尾就被 Docker 用 SIGKILL 打断
    deadline = time.time() + STOP_TIMEOUT
    for svc in running:
        try:
            svc.proc.wait(timeout=max(0.1, deadline - time.time()))
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
    if not services:
        log("没有任何服务被启用，退出")
        return 1
    log("启用: " + ", ".join(s.name for s in services))

    if not preflight(services):
        return 2

    def on_signal(_sig, _frm):
        stop_all(services)
        sys.exit(0)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    # 等 Redis 就绪。compose 的 depends_on 已经保证了顺序，但 Redis 重启或用
    # docker run 单独起容器时仍可能没就绪，等一下能少一次无谓的容器重启。
    host, port = env["REDIS_HOST"], env["REDIS_PORT"]
    if wait_for_tcp(host, port, REDIS_WAIT):
        log(f"Redis {host}:{port} 可连接")
    else:
        log(f"⚠️  {REDIS_WAIT}s 内连不上 Redis {host}:{port}，仍继续启动")

    for svc in services:
        spawn(svc, env)
    log("全部服务已拉起")

    # crash-only：任一进程退出就结束容器，交给 Docker 的 restart 策略重启
    try:
        while not _stopping.is_set():
            for svc in services:
                code = svc.proc.poll()
                if code is None:
                    continue
                log(f"❌ {svc.name} 退出 (code={code})，"
                    f"按 crash-only 策略结束容器，由 Docker 重启")
                stop_all(services)
                return code or 1
            _stopping.wait(1)
    finally:
        stop_all(services)
    return 0


if __name__ == "__main__":
    sys.exit(main())
