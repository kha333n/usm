#!/usr/bin/env python3
"""usm-tool: the read-only parts of the `usm` command (no root needed).

  status            everything at a glance
  dash [MINUTES]    live view, refreshed every second, with charts of the last MINUTES (5; q to quit)
  report [SINCE]    what happened: SINCE = hours (24, the default), a day (2026-11-05) or 'all'
  mark [NOTE]       note that the PC felt slow or hung just now - goes into the logs for tuning
  log [N]           the last N (30) events
Settings: usm config (usm_config.py); machine check: usm check (usm_check.py).
"""
import csv
import glob
import gzip
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from usm_dash import LOG_DIR, conf, events, guard_state, log_files, open_text, swap_label, tail_events  # noqa: E402
from usm_log import desktop_user, read_desktop  # noqa: E402

LIB = os.path.dirname(os.path.realpath(__file__))
def read(path, default=""):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return default


def sh(*cmd, timeout=10):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def unit_state(unit):
    return sh("systemctl", "is-active", unit).strip(), sh("systemctl", "is-enabled", unit).strip()


def meminfo():
    m = {}
    for line in read("/proc/meminfo").splitlines():
        k, v = line.split(":")
        m[k] = int(v.split()[0]) >> 10
    return m


def swaps():
    out = []
    for line in read("/proc/swaps").splitlines()[1:]:
        name, kind, size, used, prio = line.split()
        out.append((name, int(size) >> 10, int(used) >> 10, int(prio)))
    return out


def psi_avg(path):
    out = {}
    for line in read(path).splitlines():
        parts = line.split()
        out[parts[0]] = float(parts[1].split("=")[1])
    return out


def gib(mb):
    return f"{mb / 1024:.1f}"


# ---------------------------------------------------------------------------------- status
def status():
    print(f"USM {read(os.path.join(LIB, 'VERSION'), '?').strip()} status   {time.strftime('%Y-%m-%d %H:%M:%S')}")
    act, en = unit_state("usm-vram.service")
    tier = sh(f"{LIB}/usm-vramd-ctl", "status").strip() or "?"
    boot = {"enabled": "on", "disabled": "off"}.get(en, en)
    print(f"VRAM tier:  {tier.splitlines()[0]}   (at boot: {boot})")
    for line in tier.splitlines()[1:]:
        print(f"           {line}")
    act, en = unit_state("usm-guard.service")
    gs = guard_state()
    boot = {"enabled": "on", "disabled": "off"}.get(en, en)
    if act != "active":
        print(f"Guard:      not running   (at boot: {boot})")
    else:
        mode = gs.get("mode", "?")
        text = {"full": "on - when memory runs out, briefly pauses a busy background app "
                        f"(max {conf().get('GUARD_PAUSE_MAX_S', '20')} s)",
                "observe": "observe only - GNOME extension usm-desktop not active (log out and in once); "
                           "logs what it would pause",
                "protect": "protect only - keeps the desktop in RAM, never pauses apps",
                "waiting": "waiting for a desktop user to log in"}.get(mode, mode)
        print(f"Guard:      {text}   (at boot: {boot})")
        if gs.get("focus"):
            print(f"            in front: {gs['focus']}")
        if gs.get("frozen_apps"):
            print(f"            PAUSED now: {', '.join(gs['frozen_apps'])} ({gs.get('frozen_mb', 0)} MiB) - "
                  f"switch to an app to let it run again")
        else:
            print("            paused now: none")
    act, en = unit_state("usm-logger.service")
    size = sum(os.path.getsize(p) for p in glob.glob(f"{LOG_DIR}/*") if os.path.isfile(p)) >> 20
    days = sorted({os.path.basename(p).split("-", 1)[1][:10] for p in glob.glob(f"{LOG_DIR}/samples-*")})
    print(f"Logger:     {'running' if act == 'active' else 'NOT running'}; {LOG_DIR}: {size} MiB"
          f"{', since ' + days[0] if days else ''}")
    mi = meminfo()
    print(f"Memory:     RAM {gib(mi['MemAvailable'])} of {gib(mi['MemTotal'])} GiB available", end="")
    if mi.get("Zswap"):
        print(f"; zswap: {gib(mi['Zswapped'])} GiB of pages compressed into {gib(mi['Zswap'])} GiB of RAM", end="")
    print()
    for name, size, used, prio in swaps():
        print(f"            swap {swap_label(name):22s} {gib(used):>5} of {gib(size):>5} GiB used  (priority {prio})")
    p = psi_avg("/proc/pressure/memory")
    u = desktop_user()
    dk = read_desktop(u[0]) if u else {}
    lag = f"; desktop lag (last 2 s) {dk.get('lag_max_ms', '?')} ms" if dk else ""
    print(f"Stalls:     waiting for memory (last 10 s): some {p.get('some', 0):.1f}%, all {p.get('full', 0):.1f}%{lag}")
    print("More:       usm app or usm dash (live) | usm report (what happened) | usm mark (it felt slow just now)")


# ---------------------------------------------------------------------------------- dash
def dash(minutes=5):
    import usm_dash
    usm_dash.run(minutes, version=read(os.path.join(LIB, "VERSION"), "?").strip())


# ---------------------------------------------------------------------------------- logs
def rows(name, t0, t1):
    for p in log_files(name, t0, t1):
        try:
            with open_text(p) as f:
                for r in csv.DictReader(f):
                    try:
                        t = float(r["t"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if t0 <= t <= t1:
                        yield t, r
        except (OSError, EOFError, gzip.BadGzipFile):
            continue


def num(r, k):
    try:
        return float(r[k])
    except (KeyError, TypeError, ValueError):
        return None


def marks(t0, t1):
    out = sh("journalctl", "-q", "--no-pager", "-t", "usm-mark", "-o", "short-unix",
             "--since", f"@{int(t0)}", "--until", f"@{int(t1)}")
    res = []
    for line in out.splitlines():
        try:
            ts, rest = line.split(" ", 1)
            res.append((float(ts), rest.split(": ", 1)[-1]))
        except ValueError:
            pass
    return res


def report(arg="24"):
    now = time.time()
    if arg == "all":
        t0, label = 0, "everything logged"
    elif len(arg) == 10 and arg[4] == "-":
        t0 = time.mktime(time.strptime(arg, "%Y-%m-%d"))
        now, label = t0 + 86400, arg
    else:
        t0, label = now - float(arg) * 3600, f"last {arg} h"
    t1 = now
    n = 0
    first = last = None
    secs = {"on": 0.0, "spilled": 0.0, "off": 0.0}
    guard_modes = {}
    avail_min, low_ram_s = None, 0.0
    peak = {"vram_swap_used_mb": 0, "disk_swap_used_mb": 0, "zswap_mb": 0, "zswapped_mb": 0, "gpu_others_mb": 0}
    tot = {"mem_some_ms": 0, "mem_full_ms": 0, "sess_mem_full_ms": 0, "apps_mem_some_ms": 0, "pswpin": 0,
           "pswpout": 0, "majflt": 0, "oom_kill": 0}
    lag_n = {200: 0, 1000: 0, 5000: 0}
    lag_max, logger_late = 0, 0
    vram_lat, disk_lat = [], []
    prev_t = None
    for t, r in rows("samples", t0, t1):
        dt = min(t - prev_t, 5.0) if prev_t else 1.0
        prev_t = t
        n += 1
        first = first or t
        last = t
        secs[r.get("tier", "off")] = secs.get(r.get("tier", "off"), 0) + dt
        g = r.get("guard") or "off"
        guard_modes[g] = guard_modes.get(g, 0) + dt
        a = num(r, "avail_mb")
        if a is not None:
            avail_min = a if avail_min is None else min(avail_min, a)
            if a < 500:
                low_ram_s += dt
        for k in peak:
            v = num(r, k)
            if v is not None:
                peak[k] = max(peak[k], v)
        for k in tot:
            tot[k] += num(r, k) or 0
        lag = num(r, "shell_lag_ms")
        if lag is not None:
            lag_max = max(lag_max, lag)
            for th in lag_n:
                if lag >= th:
                    lag_n[th] += 1
        if (num(r, "lag_ms") or 0) >= 1000:
            logger_late += 1
        for col, lst in (("vram_rlat_us", vram_lat), ("disk_rlat_us", disk_lat)):
            v = num(r, col)
            if v is not None:
                lst.append(v)
    print(f"USM report: {label}")
    if not n:
        print(f"  no samples in {LOG_DIR} for this time")
        return
    pg = os.sysconf("SC_PAGE_SIZE")
    span = last - first
    print(f"  data: {time.strftime('%Y-%m-%d %H:%M', time.localtime(first))} to "
          f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(last))} ({span / 3600:.1f} h, {n} samples)")
    print(f"  VRAM tier: on {secs.get('on', 0) / 3600:.1f} h, released (data on disk) {secs.get('spilled', 0) / 3600:.1f} h,"
          f" off {secs.get('off', 0) / 3600:.1f} h")
    print("  guard: " + ", ".join(f"{k} {v / 3600:.1f} h" for k, v in sorted(guard_modes.items(), key=lambda x: -x[1])))
    print(f"  RAM: lowest available {avail_min:.0f} MiB; below 500 MiB for {low_ram_s / 60:.1f} min")
    print(f"  swap peak: VRAM tier {gib(peak['vram_swap_used_mb'])} GiB, disk swap {gib(peak['disk_swap_used_mb'])} GiB,"
          f" zswap {gib(peak['zswapped_mb'])} GiB in {gib(peak['zswap_mb'])} GiB of RAM")
    print(f"  paging: swap-in {tot['pswpin'] * pg / 2**30:.1f} GiB, swap-out {tot['pswpout'] * pg / 2**30:.1f} GiB,"
          f" {tot['majflt']:.0f} major faults")

    def med(lst, q=0.5):
        return sorted(lst)[min(len(lst) - 1, int(q * len(lst)))] if lst else None
    if vram_lat:
        print(f"  VRAM tier read latency per request: median {med(vram_lat):.0f} us, 99% of seconds below {med(vram_lat, .99):.0f} us")
    if disk_lat:
        print(f"  disk read latency per request: median {med(disk_lat):.0f} us, 99% of seconds below {med(disk_lat, .99):.0f} us")
    print(f"  apps waited for memory {tot['apps_mem_some_ms'] / 1000:.0f} s in total; everything waited at once "
          f"{tot['mem_full_ms'] / 1000:.0f} s; the desktop shell waited {tot['sess_mem_full_ms'] / 1000:.1f} s")
    if lag_max or any(lag_n.values()):
        print(f"  desktop lag: worst {lag_max / 1000:.1f} s; seconds with lag >= 0.2 s: {lag_n[200]}, >= 1 s: {lag_n[1000]},"
              f" >= 5 s: {lag_n[5000]}")
    else:
        print("  desktop lag: not measured (GNOME extension usm-desktop not active)")
    if logger_late:
        print(f"  the logger itself ran >= 1 s late {logger_late} times (system stalled)")
    if tot["oom_kill"]:
        print(f"  OOM KILLS: {tot['oom_kill']:.0f}")
    if peak["gpu_others_mb"]:
        print(f"  other programs used up to {peak['gpu_others_mb']:.0f} MiB of GPU memory")
    ev = events(t0, t1)
    stalls = [line for _, line in ev if " stall from " in line]
    other = [line for _, line in ev if " stall from " not in line and "logger st" not in line]
    print(f"\n  stall episodes: {len(stalls)}")
    for line in stalls[-15:]:
        print(f"    {line}")
    if other:
        print(f"\n  events ({len(other)}):")
        for line in other[-25:]:
            print(f"    {line}")
    # guard
    acts, paused_s, open_freeze, would = {}, {}, {}, {}
    for t, r in rows("guard", t0, t1):
        a, app = r.get("action", ""), r.get("app", "")
        acts[a] = acts.get(a, 0) + 1
        if a == "freeze":
            open_freeze.setdefault(app, t)
        elif a == "thaw" and app in open_freeze:
            paused_s[app] = paused_s.get(app, 0) + t - open_freeze.pop(app)
        elif a == "would-freeze":
            would[app] = would.get(app, 0) + 1
    if acts:
        print("\n  guard actions: " + ", ".join(f"{k} {v}" for k, v in sorted(acts.items(), key=lambda x: -x[1])))
        for app, s in sorted(paused_s.items(), key=lambda x: -x[1])[:10]:
            print(f"    {app}: paused {s / 60:.1f} min in total")
        for app, c in sorted(would.items(), key=lambda x: -x[1])[:10]:
            print(f"    would have paused {app} {c} times (observe mode)")
    # apps
    peak_app = {}
    for t, r in rows("apps", t0, t1):
        if r.get("kind") in ("app", "system", "session"):
            v = (num(r, "mem_mb") or 0) + (num(r, "swap_mb") or 0)
            k = r.get("name", "?")
            if v > peak_app.get(k, (0, ""))[0]:
                peak_app[k] = (v, r.get("kind"))
    if peak_app:
        print("\n  biggest (RAM + swap, peak): " + ", ".join(
            f"{k} {gib(v)} GiB" for k, (v, kind) in sorted(peak_app.items(), key=lambda x: -x[1][0])[:8]))
    mk = marks(t0, t1)
    if mk:
        print(f"\n  your marks ({len(mk)}):")
        samples = list(rows("samples", min(t for t, _ in mk) - 60, max(t for t, _ in mk) + 5))
        for t, note in mk:
            near = [r for ts, r in samples if t - 60 <= ts <= t]
            if near:
                worst = max(near, key=lambda r: num(r, "mem_some_ms") or 0)
                ctx = (f"last minute: RAM avail min {min(num(r, 'avail_mb') or 0 for r in near):.0f} MiB, memory waits up to "
                       f"{(num(worst, 'mem_some_ms') or 0) / 10:.0f}% of a second, desktop lag up to "
                       f"{max(num(r, 'shell_lag_ms') or 0 for r in near) / 1000:.1f} s, in front: {near[-1].get('focus_app') or '?'}")
            else:
                ctx = "no samples around it"
            print(f"    {time.strftime('%m-%d %H:%M:%S', time.localtime(t))} \"{note}\" - {ctx}")


def mark(note):
    note = note or "felt slow"
    subprocess.run(["logger", "-t", "usm-mark", "--", note])
    p = psi_avg("/proc/pressure/memory")
    mi = meminfo()
    print(f"marked {time.strftime('%H:%M:%S')}: \"{note}\" (RAM available {mi['MemAvailable']} MiB, waiting for memory"
          f" {p.get('some', 0):.0f}% of the last 10 s). Shows up in: usm report")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    args = sys.argv[2:]
    if cmd == "status":
        status()
    elif cmd == "dash":
        try:
            minutes = float(args[0]) if args else 5
        except ValueError:
            minutes = 0
        if not 1 <= minutes <= 60:
            print("usm dash [MINUTES]: charts of the last 1 to 60 minutes (default 5)")
            sys.exit(2)
        dash(minutes)
    elif cmd == "report":
        report(args[0] if args else "24")
    elif cmd == "mark":
        mark(" ".join(args))
    elif cmd == "log":
        for line in tail_events(int(args[0]) if args else 30):
            print(line)
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
