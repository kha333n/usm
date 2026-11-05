#!/usr/bin/env python3
"""Test the guard's desktop mode on the real desktop, touching only test apps (scope names with 'usmtest').
Run as root while hogs run in app-usmtest-a.scope (big, incompressible) and app-usmtest-b.scope (with a
silent pw-play). Steps: find apps, audio detection, pause A (its memory pushed to swap), fake 'A in front'
-> A must run again at once and its memory come back. Usage: guard-focus-test.py UID HOG_A_PID"""
import argparse, importlib.util, os, sys, time
uid, pid_a = int(sys.argv[1]), int(sys.argv[2])
sys.path.insert(0, "/usr/local/lib/usm")
spec = importlib.util.spec_from_file_location("guard", "/usr/local/lib/usm/usm-guard.py")
g = importlib.util.module_from_spec(spec); spec.loader.exec_module(g)
scratch = os.environ.get("SCRATCH", "/tmp")
a = argparse.Namespace(desktop=True, exclude="", min_app_mb=64, protect_desktop="1G", high_ticks=0, apps=None,
                       protect=[], mode="full", apps_high="auto", swap_dir=scratch, log="/dev/stdout",
                       log_dir=None, keep_days=1, max_mb=10)
G = g.Guard(a)
G.swap_busy = True                       # this test instance never adds swap files
orig = G.eligible
G.eligible = lambda app: orig(app) if "usmtest" in app.name else "not-a-test-app"
G.psi_now = 0.0
G.find_user(); G.discover()
apps = {x.name: x for x in G.apps.values()}
print("apps found:", ", ".join(f"{n}({x.stat('memory.current') >> 20}MB, {len(x.paths)} scope(s))" for n, x in sorted(apps.items())))
chrome = [x for x in G.apps.values() if "chrome" in x.name.lower()]
if chrome:
    print("chrome grouped as one app:", [os.path.basename(p) for p in chrome[0].paths])
A, B = apps["usmtest-a"], apps["usmtest-b"]
aud = G.audio_pids()
print("processes with a running sound stream:", sorted(aud), "-> B has sound:", bool(set(B.pids()) & aud))
print("pick among B only:", G.pick([B], "memory"))
victim, notes = G.pick(list(G.apps.values()), "memory")
print("pick among all running (test apps only allowed):", victim.name if victim else None)
sw = lambda: {l.split()[0]: int(l.split()[3]) >> 10 for l in open("/proc/swaps").read().splitlines()[1:]}
vram = lambda: next((v for k, v in sw().items() if k.startswith("/dev/ublkb")), 0)
print(f"before: A in RAM {A.stat('memory.current') >> 20} MB, in swap {A.stat('memory.swap.current') >> 20} MB; VRAM tier used {vram()} MB")
t = time.monotonic()
G.freeze(A, "test")
if A.reclaimer:
    A.reclaimer.join(120)
print(f"paused + pushed out in {time.monotonic() - t:.1f} s: A in RAM {A.stat('memory.current') >> 20} MB, in swap "
      f"{A.stat('memory.swap.current') >> 20} MB; VRAM tier used {vram()} MB; frozen={open(A.path + '/cgroup.freeze').read().strip()}")
# the user "switches" to A
f = f"/run/user/{uid}/usm-desktop"
with open(f, "w") as fh:
    fh.write(f"t={int(time.time())}\nfocus_pid={pid_a}\nfocus_app=usmtest-a\nidle_ms=0\nlag_max_ms=0\n")
os.chown(f, uid, uid)
t = time.monotonic()
G.update_focus()
if G.focus_key in G.apps and G.apps[G.focus_key].frozen:
    G.thaw_focus(G.apps[G.focus_key])
print(f"in front -> running again after {1000 * (time.monotonic() - t):.0f} ms: frozen={open(A.path + '/cgroup.freeze').read().strip()}")
for i in range(20):
    time.sleep(0.5)
    if A.stat("memory.swap.current") < (32 << 20):
        break
print(f"after {0.5 * (i + 1):.1f} s: A in RAM {A.stat('memory.current') >> 20} MB, in swap {A.stat('memory.swap.current') >> 20} MB (prefetched)")
os.unlink(f)
G.update_focus()
print("eligible checks:", {n: orig(x) for n, x in sorted(apps.items())})
