#!/usr/bin/env python3
"""Deterministic test of the guard's desktop rules: fake pressure and app activity, real pause/resume of small
test processes (app-usmtest-p*). Run as root with the usm-guard service OFF. Usage: guard-policy-test.py UID
Checks: no pause before 3 s of overload; busiest eligible app chosen (idle / recent / in front skipped); pause
ends at 20 s under lasting pressure; ends early once settled (>= 5 s); cooldown; at most 2 paused; disk busy
-> no pause; no focus info -> only would-freeze."""
import argparse, importlib.util, os, pwd, subprocess, sys, time

UID = int(sys.argv[1])
USER = pwd.getpwuid(UID).pw_name
SCR = os.environ.get("SCRATCH", "/tmp")
sys.path.insert(0, "/usr/local/lib/usm")
spec = importlib.util.spec_from_file_location("guard", "/usr/local/lib/usm/usm-guard.py")
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)
ENV = [f"XDG_RUNTIME_DIR=/run/user/{UID}", f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{UID}/bus"]
NAMES = ["pa", "pb", "pc", "pd", "pe"]


def user(*cmd, **kw):
    return subprocess.run(["runuser", "-u", USER, "--", "env", *ENV, *cmd], capture_output=True, text=True, **kw)


for n in NAMES:
    user("systemctl", "--user", "stop", f"app-usmtest-{n}.scope")
procs = {}
for n in NAMES:
    procs[n] = subprocess.Popen(["runuser", "-u", USER, "--", "env", *ENV, "systemd-run", "--user", "--scope",
                                 "--slice=app.slice", f"--unit=app-usmtest-{n}.scope", "sleep", "600"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(2)

clock = [1000.0]
g.time.monotonic = lambda: clock[0]          # the test drives time
fake_focus = {"focus_pid": "0", "lag_max_ms": "0", "age": 0.0}
g.read_desktop = lambda uid: dict(fake_focus)
a = argparse.Namespace(desktop=True, exclude="", min_app_mb=0, protect_desktop="1G", high_ticks=0, trigger=0,
                       pause_max=20.0, cooldown=15.0, recent=300.0, apps=None, protect=[], mode="full",
                       apps_high="auto", swap_dir=SCR, log=f"{SCR}/guard-policy.csv", log_dir=None, keep_days=1, max_mb=10)
G = g.Guard(a)
G.swap_busy = True
G.apply_protection = lambda: None
G.audio_pids = lambda: set()
orig = G.eligible
G.eligible = lambda app: orig(app) if app.name.startswith("usmtest-p") else "not-a-test-app"
G.find_user()
P = {"apps": 0.0, "sess": 0.0}
G.psi.sample = lambda: P["apps"]
G.io_psi.sample = lambda: 0.0
G.sess_psi.sample = lambda: P["sess"]
disk = [False]
G.disk_busy = lambda: disk[0]
activity = {"pa": 5000, "pb": 3000, "pc": 0, "pd": 9000, "pe": 2000}     # faults/s; pc idle
g.App.activity = lambda self, now: setattr(self, "flt_rate", activity.get(self.name.replace("usmtest-", ""), 0))
st = {}
ev = []
G.log = lambda action, app="", detail="": ev.append((clock[0] - 1000, action, app.replace("usmtest-", ""), detail)) \
    if action in ("freeze", "thaw", "disk-busy", "would-freeze", "overload-nothing-to-pause") else None
G.discover()
apps = {x.name.replace("usmtest-", ""): x for x in G.apps.values() if x.name.startswith("usmtest-p")}
assert len(apps) == 5, apps.keys()
G.focus_valid = True


maxp = [0]


def ticks(seconds):
    for _ in range(int(seconds / g.TICK)):
        G.tick_desktop(st)
        maxp[0] = max(maxp[0], sum(1 for x in apps.values() if x.frozen))
        clock[0] += g.TICK


def front(name):
    p = int(open(f"{apps[name].path}/cgroup.procs").read().split()[0])
    fake_focus["focus_pid"] = str(p)


res = []
check = lambda name, cond, info="": res.append((name, bool(cond), info))
front("pe"); ticks(1)                    # pe was in front -> "recently used"
front("pa")                              # pa is in front now
P["apps"] = 0.5
ticks(2.5)
check("no pause before 3 s of overload", not any(e[1] == "freeze" for e in ev))
ticks(1)
f1 = [e for e in ev if e[1] == "freeze"]
check("busiest eligible app paused first (pd)", f1 and f1[0][2] == "pd", f1)
ticks(30)                                # pressure stays: pd ends at 20 s; second app (pb) may be paused meanwhile
fr = [e for e in ev if e[1] == "freeze"]
th = [e for e in ev if e[1] == "thaw"]
pd_th = next((e for e in th if e[2] == "pd"), None)
check("pause ends at the 20 s limit under lasting pressure", pd_th and "time-limit" in pd_th[3] and 19.5 <= pd_th[0] - f1[0][0] <= 21, pd_th)
check("never paused: in front (pa), recent (pe), idle (pc)", not {e[2] for e in fr} & {"pa", "pe", "pc"}, sorted({e[2] for e in fr}))
check("at most 2 paused at once", maxp[0] <= 2, maxp[0])
again = [e for e in fr if e[2] == "pd"]
check("cooldown: pd not paused again within 15 s", len(again) < 2 or again[1][0] - pd_th[0] >= 15, [e[0] for e in again])
P["apps"] = 0.0
ticks(25)                                # calm; all cooldowns pass
check("nothing paused when calm", not any(x.frozen for x in apps.values()))
P["apps"] = 0.5
ticks(10)                                # a pause starts at ~3 s and is held 7 s
P["apps"] = 0.0
t_calm = clock[0] - 1000
ticks(3)
th2 = [e for e in ev if e[1] == "thaw" and "settled" in e[3] and e[0] >= t_calm]
check("pause ends early (2 s) once pressure settled", th2 and th2[0][0] - t_calm <= 2.6, th2[:1])
ticks(25)
n = len([e for e in ev if e[1] == "freeze"])
disk[0] = True
P["apps"] = 0.5
ticks(6)
check("disk is the bottleneck: no pause, logged", len([e for e in ev if e[1] == "freeze"]) == n and any(e[1] == "disk-busy" for e in ev))
disk[0] = False
P["apps"] = 0.0
ticks(40)                                # cooldowns pass
G.focus_valid = False
G.update_focus = lambda: None
P["apps"] = 0.5
n = len([e for e in ev if e[1] == "freeze"])
ticks(5)
check("no focus info: only would-freeze", len([e for e in ev if e[1] == "freeze"]) == n and any(e[1] == "would-freeze" for e in ev))
for x in apps.values():
    if x.frozen:
        x.set_frozen(0)
for n in NAMES:
    user("systemctl", "--user", "stop", f"app-usmtest-{n}.scope")
print("events:")
for e in ev:
    print(f"  {e[0]:6.1f}s {e[1]:26s} {e[2]:4s} {e[3][:100]}")
print("RESULTS")
for name, okk, info in res:
    print(f"  {'PASS' if okk else 'FAIL'} {name} {info if not okk else ''}")
