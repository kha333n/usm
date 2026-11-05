#!/usr/bin/env python3
"""Scenario test of the guard's desktop policy on the real desktop, touching only test apps (app-usmtest-*).
Run as root with the usm-guard service OFF. Usage: guard-desktop-test.py UID
Starts (as UID) four hogs in app scopes: front + busy + recent (all touching memory), idle (big, does nothing);
drives Guard.tick_desktop every 0.5 s with a fake "app in front" and checks:
  1. only `busy` is paused under overload (front: in front; recent: in front a minute ago; idle: does nothing)
  2. a pause ends at the latest after PAUSE_MAX; the cooldown keeps it from being paused again at once
  3. switching to a paused app lets it run at once
  4. disk is the bottleneck -> no pause
  5. desktop lag while paused but memory calm -> everything runs (safety brake)
  6. all hog data intact."""
import argparse, importlib.util, os, pwd, signal, subprocess, sys, time

UID = int(sys.argv[1])
USER = pwd.getpwuid(UID).pw_name
HERE = os.path.dirname(os.path.realpath(__file__))
SCR = os.environ.get("SCRATCH", "/tmp")
sys.path.insert(0, "/usr/local/lib/usm")
spec = importlib.util.spec_from_file_location("guard", "/usr/local/lib/usm/usm-guard.py")
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)

ENV = {"XDG_RUNTIME_DIR": f"/run/user/{UID}", "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{UID}/bus"}


def as_user(*cmd, **kw):
    return subprocess.Popen(["runuser", "-u", USER, "--", "env", *[f"{k}={v}" for k, v in ENV.items()], *cmd], **kw)


def start_hog(name, mb, touch_s):
    out = f"{SCR}/hog-{name}.out"
    if os.path.exists(out):
        os.unlink(out)
    as_user("systemd-run", "--user", "--scope", "--slice=app.slice", f"--unit=app-usmtest-{name}.scope",
            "-E", f"TOUCH_S={touch_s}", f"{HERE}/hog", str(mb), "mixed",
            stdout=open(out, "w"), stderr=subprocess.STDOUT)
    for _ in range(600):
        txt = open(out).read() if os.path.exists(out) else ""
        if "filled" in txt:
            return int(txt.split("hog ")[1].split(":")[0])
        time.sleep(0.2)
    raise SystemExit(f"hog {name} did not start: {txt}")


def stop_all():
    for n in ("front", "busy", "recent", "idle"):
        subprocess.run(["runuser", "-u", USER, "--", "env", *[f"{k}={v}" for k, v in ENV.items()],
                        "systemctl", "--user", "stop", f"app-usmtest-{n}.scope"], capture_output=True)


avail = int(next(l for l in open("/proc/meminfo") if l.startswith("MemAvailable")).split()[1]) >> 10
each = max(1500, (avail + 6000 - 2000) // 3)
print(f"RAM available {avail} MB: 3 touching hogs x {each} MB + 1 idle hog 2000 MB")
stop_all()
pids = {"idle": start_hog("idle", 2000, 0)}
for n in ("front", "busy", "recent"):
    pids[n] = start_hog(n, each, 100)

fake = {"focus_pid": str(pids["recent"]), "lag_max_ms": "0", "age": 0.0}
g.read_desktop = lambda uid: dict(fake, t=str(int(time.time())))
a = argparse.Namespace(desktop=True, exclude="", min_app_mb=64, protect_desktop="1G", high_ticks=0, trigger=0,
                       pause_max=20.0, cooldown=15.0, recent=300.0, apps=None, protect=[], mode="full",
                       apps_high="auto", swap_dir=SCR, log=f"{SCR}/guard-test.csv", log_dir=None, keep_days=1, max_mb=10)
G = g.Guard(a)
G.swap_busy = True                                  # this test instance adds no swap files
G.apply_protection = lambda: None                   # leave the real protection settings alone
orig_eligible = G.eligible
G.eligible = lambda app: orig_eligible(app) if app.name.startswith("usmtest") else "not-a-test-app"
G.psi_now = 0.0
G.find_user()
st = {}
events = []
log0 = G.log


def log(action, app="", detail=""):
    if action in ("freeze", "thaw", "disk-busy", "overload-nothing-to-pause", "would-freeze"):
        events.append((round(time.monotonic() - T0, 1), action, app, detail))
        print(f"  {time.monotonic() - T0:6.1f}s {action:26s} {app:16s} {detail[:110]}")
    log0(action, app, detail)


G.log = log
T0 = time.monotonic()


def run(seconds, every=None):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        G.tick_desktop(st)
        if every:
            every()
        time.sleep(g.TICK)


print("phase 1: 'recent' in front for 4 s, then 'front' in front; all three start touching memory")
run(4)
fake["focus_pid"] = str(pids["front"])
for n in ("front", "busy", "recent"):
    os.kill(pids[n], signal.SIGUSR2)
psi_seen = []
run(45, every=lambda: psi_seen.append(G.psi_now))
print(f"  apps waiting for memory: max {max(psi_seen):.2f}, avg {sum(psi_seen) / len(psi_seen):.2f}")

print("phase 2: switch to 'busy' while it is paused (wait up to 30 s for a pause)")
busy = next(x for x in G.apps.values() if x.name == "usmtest-busy")
t_switch = None
end = time.monotonic() + 30
while time.monotonic() < end:
    G.tick_desktop(st)
    if busy.frozen:
        fake["focus_pid"] = str(pids["busy"])
        t_switch = time.monotonic() - T0
        G.tick_desktop(st)
        break
    time.sleep(g.TICK)
fake["focus_pid"] = str(pids["front"])

print("phase 3: the disk is the bottleneck (simulated) - no new pause for 12 s")
G.disk_busy = lambda: True
n_before = sum(1 for e in events if e[1] == "freeze")
run(12)
n_disk = sum(1 for e in events if e[1] == "freeze") - n_before
del G.disk_busy

print("phase 4: wait for the touching to end, then safety brake: pause 'idle' by hand, report desktop lag")
for n in ("front", "busy", "recent"):
    out = f"{SCR}/hog-{n}.out"
    for _ in range(300):
        if "touch:" in open(out).read():
            break
        run(1)
run(4)
idle = next(x for x in G.apps.values() if x.name == "usmtest-idle")
G.pause(idle, "test", time.monotonic())
fake["lag_max_ms"] = "1500"
run(1.5)
fake["lag_max_ms"] = "0"
braked = not idle.frozen

for a in G.apps.values():                            # whatever happens: nothing stays paused
    if a.frozen:
        a.set_frozen(0)
print("verify data")
for n, pid in pids.items():
    os.kill(pid, signal.SIGUSR1)
time.sleep(1)
checks = {}
for n in pids:
    out = f"{SCR}/hog-{n}.out"
    for _ in range(300):
        txt = open(out).read()
        if "verify:" in txt:
            break
        time.sleep(0.5)
    checks[n] = txt.split("verify:")[-1].strip().splitlines()[0] if "verify:" in txt else "no answer"
    touch = [l for l in txt.splitlines() if l.startswith("touch:")]
    if touch:
        print(f"  {n}: {touch[-1]}")
stop_all()

freezes = [e for e in events if e[1] == "freeze"]
thaws = [e for e in events if e[1] == "thaw"]
paused_names = {e[2] for e in freezes if e[3].startswith("overload")}
ok = lambda c: "PASS" if c else "FAIL"
print("\nRESULTS")
print(f"  {ok(paused_names == {'usmtest-busy'})} paused because of overload: {sorted(paused_names)}"
      f" ({len(freezes)} pauses) - expected only usmtest-busy (+ idle by hand in phase 4)")
durations = []
for f in freezes:
    t = next((th for th in thaws if th[2] == f[2] and th[0] >= f[0]), None)
    if t:
        durations.append(t[0] - f[0])
print(f"  {ok(durations and max(durations) <= 21.5)} pause lengths {[round(d, 1) for d in durations]} s - at most 20 s")
cool = [freezes[i + 1][0] - next(th[0] for th in thaws if th[2] == freezes[i][2] and th[0] >= freezes[i][0])
        for i in range(len(freezes) - 1) if freezes[i + 1][2] == freezes[i][2] == "usmtest-busy"
        and any(th[2] == freezes[i][2] and th[0] >= freezes[i][0] for th in thaws)]
print(f"  {ok(all(c >= 14.5 for c in cool))} gaps before pausing busy again {[round(c, 1) for c in cool]} s - cooldown 15 s in this test")
infront = [th for th in thaws if "in-front" in th[3]]
print(f"  {ok(t_switch and infront and abs(infront[0][0] - t_switch) < 1.1)} switched to busy at {t_switch} s -> "
      f"{'ran again at ' + str(infront[0][0]) + ' s' if infront else 'NOT let run'}")
print(f"  {ok(n_disk == 0)} disk busy: {n_disk} pauses (expected 0); logged: {any(e[1] == 'disk-busy' for e in events)}")
print(f"  {ok(braked)} safety brake let the paused app run: {braked}")
print(f"  {ok(all(c.endswith(' 0 bad') for c in checks.values()))} data: {checks}")
