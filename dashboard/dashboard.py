import subprocess, os, sys, time, signal

PROCS = {}    # name -> {"proc": Popen, "restarts": int, "last_restart": float}
BACKOFF_BASE = 2     # seconds
BACKOFF_MAX = 60     # cap at 1 minute
BACKOFF_WINDOW = 120 # reset backoff after 2 min stable

def start(name, args):
    p = subprocess.Popen(args, start_new_session=True)
    PROCS[name] = {"proc": p, "restarts": 0, "last_restart": time.time()}
    print(f"[dashboard] started {name} (pid={p.pid})")

def cleanup():
    for name, info in PROCS.items():
        p = info["proc"]
        try:
            p.terminate()
            p.wait(timeout=5)
        except:
            try: p.kill()
            except: pass
    print("[dashboard] all children terminated")

signal.signal(signal.SIGTERM, lambda *a: (cleanup(), sys.exit(0)))
signal.signal(signal.SIGINT,  lambda *a: (cleanup(), sys.exit(0)))

os.chdir('/app')
start("validator", [sys.executable, '-u', 'validator.py'])
start("backend",   [sys.executable, 'backend.py'])
start("frontend",  [sys.executable, 'frontend.py'])
start("quality",   [sys.executable, '-u', 'quality.py'])
print('[dashboard] running: frontend :5050, backend :5051, validator bg, quality bg')

while True:
    try:
        # Poll each child
        for name, info in list(PROCS.items()):
            p = info["proc"]
            ret = p.poll()
            if ret is not None:
                print(f"[dashboard] {name} exited (code={ret}), restarting...")
                del PROCS[name]

                # Backoff: exponential, bounded, reset after window
                restart_count = info["restarts"] + 1
                elapsed_since_last = time.time() - info["last_restart"]
                if elapsed_since_last > BACKOFF_WINDOW:
                    restart_count = 0  # reset - process was stable

                delay = min(BACKOFF_MAX, BACKOFF_BASE * (2 ** restart_count))
                print(f"[dashboard] backoff {delay:.0f}s before restart (attempt #{restart_count})")
                time.sleep(delay)

                # Reap any zombie children
                try:
                    while True:
                        wpid, _ = os.waitpid(-1, os.WNOHANG)
                        if wpid == 0:
                            break
                except ChildProcessError:
                    pass

                start(name, p.args)
                PROCS[name]["restarts"] = restart_count

        time.sleep(1)
    except KeyboardInterrupt:
        break

cleanup()
