#!/usr/bin/env python3
"""usm-logger: record what memory, swap, the VRAM tier, the guard and the desktop do, for tuning.

Only reads (procfs, sysfs, cgroups, NVML); changes nothing. Files in LOG_DIR (default /var/log/usm),
one per day, gzipped after the day, kept LOG_KEEP_DAYS (30) / LOG_MAX_MB (1024) in total:
  samples-DAY.csv  every LOG_INTERVAL (1) s: RAM, stalls (PSI), paging, swap per tier, disk and VRAM-device
                   latency, VRAM tier state, GPU, focused app, desktop (GNOME Shell) lag, guard state
  apps-DAY.csv     every 10 s: memory, swap, faults and stalls of each app and of big services
  events-DAY.log   things worth reading: stall episodes, sleep/wake, swap changes, OOM kills, own stalls
Summaries: usm report.
"""
import ctypes
import glob
import json
import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from usm_log import DailyLog, app_name, desktop_user, read_desktop, sd_notify  # noqa: E402

LOG_DIR = os.environ.get("LOG_DIR", "/var/log/usm")
INTERVAL = float(os.environ.get("LOG_INTERVAL", "1") or 1)
KEEP_DAYS = int(os.environ.get("LOG_KEEP_DAYS", "30") or 30)
MAX_MB = int(os.environ.get("LOG_MAX_MB", "300") or 300)
APPS_EVERY = 10.0
GPU_EVERY = 15.0
STATS = "/run/usm-vramd.stats"
GUARD_STATE = "/run/usm-guard.state"
PG = os.sysconf("SC_PAGE_SIZE")

libc = ctypes.CDLL(None, use_errno=True)


def read(path, default=""):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return default


def meminfo():
    m = {}
    for line in read("/proc/meminfo").splitlines():
        k, v = line.split(":")
        m[k] = int(v.split()[0]) >> 10     # MiB
    return m


def vmstat():
    out = {}
    for line in read("/proc/vmstat").splitlines():
        k, v = line.split()
        out[k] = int(v)
    out["allocstall"] = sum(v for k, v in out.items() if k.startswith("allocstall_"))
    return out


def psi(path):
    """(some_total_us, full_total_us, some_avg10, full_avg10); zeros if missing."""
    some = full = 0
    s10 = f10 = 0.0
    for line in read(path).splitlines():
        parts = dict(p.split("=") for p in line.split()[1:])
        if line.startswith("some"):
            some, s10 = int(parts["total"]), float(parts["avg10"])
        elif line.startswith("full"):
            full, f10 = int(parts["total"]), float(parts["avg10"])
    return some, full, s10, f10


def blkstat(dev):
    f = read(f"/sys/block/{dev}/stat").split()
    if len(f) < 11:
        return None
    v = [int(x) for x in f]
    return {"rio": v[0], "rsec": v[2], "rms": v[3], "wio": v[4], "wsec": v[6], "wms": v[7], "infl": v[8],
            "dio": v[11] if len(v) > 11 else 0}


def swaps():
    """[(name, kind, size_mb, used_mb, prio)]"""
    out = []
    for line in read("/proc/swaps").splitlines()[1:]:
        name, kind, size, used, prio = line.split()
        out.append((name, kind, int(size) >> 10, int(used) >> 10, int(prio)))
    return out


def disk_of(path, kind):
    """Block device name (whole disk) holding a swap file/partition."""
    try:
        st = os.stat(path)
        dev = st.st_rdev if kind == "partition" else st.st_dev
        sysdev = os.path.realpath(f"/sys/dev/block/{os.major(dev)}:{os.minor(dev)}")
        if os.path.exists(f"{sysdev}/partition"):
            sysdev = os.path.dirname(sysdev)
        return os.path.basename(sysdev)
    except OSError:
        return None


def cpustat():
    f = read("/proc/stat").splitlines()[0].split()[1:]
    v = [int(x) for x in f]
    idle, iowait = v[3], v[4]
    return sum(v[:8]), idle, iowait


def procs_state():
    run = blocked = 0
    for line in read("/proc/stat").splitlines():
        if line.startswith("procs_running"):
            run = int(line.split()[1])
        elif line.startswith("procs_blocked"):
            blocked = int(line.split()[1])
    return run, blocked


def power():
    ac, bat = "", ""
    for p in glob.glob("/sys/class/power_supply/*"):
        t = read(f"{p}/type").strip()
        if t == "Mains":
            ac = read(f"{p}/online").strip()
        elif t == "Battery":
            bat = read(f"{p}/capacity").strip()
    return ac, bat


def gpu_pci():
    for d in glob.glob("/sys/bus/pci/devices/*"):
        if read(f"{d}/vendor").strip() == "0x10de" and read(f"{d}/class").startswith(("0x0300", "0x0302")):
            return d
    return None


class NVML:
    """GPU memory/use via NVML, queried only while the GPU is awake anyway (a query would wake it)."""

    class Mem(ctypes.Structure):
        _fields_ = [("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong), ("used", ctypes.c_ulonglong)]

    class Util(ctypes.Structure):
        _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]

    class Proc(ctypes.Structure):
        _fields_ = [("pid", ctypes.c_uint), ("used", ctypes.c_ulonglong), ("gi", ctypes.c_uint), ("ci", ctypes.c_uint)]

    def __init__(self):
        try:
            self.lib = ctypes.CDLL("libnvidia-ml.so.1")
        except OSError:
            self.lib = None
        self.open = False

    def sample(self, keep_open=False):
        """(used_mb, util_pct, watts, {pid: used_mb}) or None. keep_open: stay initialised (cheap next time;
        only while the GPU is held awake anyway, i.e. the VRAM tier is on)."""
        if not self.lib:
            return None
        if not self.open:
            if self.lib.nvmlInit_v2() != 0:
                return None
            self.open = True
        try:
            h = ctypes.c_void_p()
            if self.lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(h)) != 0:
                return None
            m, u, w = self.Mem(), self.Util(), ctypes.c_uint()
            self.lib.nvmlDeviceGetMemoryInfo(h, ctypes.byref(m))
            util = u.gpu if self.lib.nvmlDeviceGetUtilizationRates(h, ctypes.byref(u)) == 0 else -1
            watts = w.value / 1000 if self.lib.nvmlDeviceGetPowerUsage(h, ctypes.byref(w)) == 0 else -1
            procs = {}
            for fn in ("nvmlDeviceGetComputeRunningProcesses_v3", "nvmlDeviceGetGraphicsRunningProcesses_v3"):
                n = ctypes.c_uint(64)
                arr = (self.Proc * 64)()
                try:
                    if getattr(self.lib, fn)(h, ctypes.byref(n), arr) == 0:
                        for i in range(n.value):
                            used = arr[i].used if arr[i].used < (1 << 60) else 0   # "not available"
                            procs[arr[i].pid] = max(procs.get(arr[i].pid, 0), used >> 20)
                except AttributeError:
                    pass
            return m.used >> 20, util, watts, procs
        finally:
            if not keep_open:
                self.lib.nvmlShutdown()
                self.open = False


def comm(pid):
    return read(f"/proc/{pid}/comm").strip() or str(pid)


def cg_int(path, name):
    try:
        return int(read(f"{path}/{name}") or 0)
    except ValueError:
        return 0


def cg_stat(path):
    out = {}
    for line in read(f"{path}/memory.stat").splitlines():
        k, _, v = line.partition(" ")
        if k in ("anon", "file", "zswap", "pgmajfault", "shmem"):
            out[k] = int(v)
    return out


SAMPLE_COLS = (
    "t,lag_ms,avail_mb,free_mb,anon_mb,file_mb,shmem_mb,dirty_mb,writeback_mb,slab_mb,swapcached_mb,"
    "zswap_mb,zswapped_mb,swap_used_mb,vram_swap_used_mb,disk_swap_used_mb,"
    "mem_some_ms,mem_full_ms,io_some_ms,io_full_ms,cpu_some_ms,apps_mem_some_ms,apps_mem_full_ms,"
    "sess_mem_some_ms,sess_mem_full_ms,mem_some_avg10,mem_full_avg10,"
    "pswpin,pswpout,majflt,refault_anon,refault_file,scan_kswapd,scan_direct,steal_kswapd,steal_direct,"
    "allocstall,zswpin,zswpout,zswpwb,oom_kill,"
    "vram_rps,vram_wps,vram_dps,vram_rlat_us,vram_wlat_us,disk_rps,disk_wps,disk_rmbps,disk_wmbps,"
    "disk_rlat_us,disk_wlat_us,cpu_pct,iowait_pct,running,blocked,load1,"
    "tier,tier_in_vram_mb,tier_on_disk_mb,tier_live_mb,tier_comp_pct,tier_refused,tier_errors,"
    "gpu_rt,gpu_used_mb,gpu_util,gpu_w,gpu_others_mb,gpu_top,"
    "focus_app,idle_s,shell_lag_ms,desk_age_s,guard,frozen,frozen_mb,ac,bat")
APPS_COLS = "t,name,cgroup,kind,mem_mb,anon_mb,file_mb,swap_mb,zswap_mb,majflt,some_ms,full_ms,frozen,focused,read_mb,write_mb"


class Episode:
    """A stretch of time with heavy memory stalls or desktop lag, summarised into one event line."""

    def __init__(self, t):
        self.start, self.last_bad, self.n = t, t, 0
        self.some = self.full = self.peak_full = self.sess_full = 0.0
        self.lag_max = 0
        self.swpin = self.swpout = self.majflt = 0
        self.avail_min = 1 << 30
        self.focus, self.frozen = set(), 0

    def add(self, t, r):
        self.n += 1
        self.some += r["mem_some_ms"]
        self.full += r["mem_full_ms"]
        self.sess_full += r["sess_mem_full_ms"]
        self.peak_full = max(self.peak_full, r["mem_full_ms"] / max(r["dt"] * 10, 1e-3))
        self.lag_max = max(self.lag_max, r["shell_lag_ms"] or 0)
        self.swpin += r["pswpin"]
        self.swpout += r["pswpout"]
        self.majflt += r["majflt"]
        self.avail_min = min(self.avail_min, r["avail_mb"])
        if r["focus_app"]:
            self.focus.add(r["focus_app"])
        self.frozen = max(self.frozen, r["frozen"] or 0)

    def text(self, t):
        mb = lambda pages: pages * PG >> 20
        return (f"stall from {time.strftime('%H:%M:%S', time.localtime(self.start))} for {t - self.start:.0f} s: "
                f"apps waited on memory {self.some / 1000:.1f} s (all at once {self.full / 1000:.1f} s, peak "
                f"{self.peak_full:.0f}% of a second); desktop shell stalled {self.sess_full / 1000:.1f} s, longest "
                f"desktop lag {self.lag_max} ms; swap-in {mb(self.swpin)} MiB, swap-out {mb(self.swpout)} MiB, "
                f"{self.majflt} major faults; RAM available min {self.avail_min} MiB; in front: "
                f"{','.join(sorted(self.focus)) or '?'}; guard paused up to {self.frozen} app(s)")


class Logger:
    def __init__(self):
        self.samples = DailyLog(LOG_DIR, "samples", "csv", SAMPLE_COLS, KEEP_DAYS, MAX_MB)
        self.apps = DailyLog(LOG_DIR, "apps", "csv", APPS_COLS, KEEP_DAYS, MAX_MB)
        self.events = DailyLog(LOG_DIR, "events", "log", None, KEEP_DAYS, MAX_MB)
        self.nvml = NVML()
        self.gpu = gpu_pci()
        self.prev = {}
        self.cg_prev = {}
        self.user = None
        self.user_t = -1e9
        self.gpu_t = -1e9
        self.gpu_row = ("", "", "", "", "", "")
        self.gpu_hogs = set()
        self.apps_t = -1e9
        self.swaps_prev = None
        self.tier_prev = None
        self.episode = None
        self.stop = False

    def event(self, text):
        self.events.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}")

    def delta(self, key, value):
        old = self.prev.get(key)
        self.prev[key] = value
        return 0 if old is None or value < old else value - old

    # --- one sample --------------------------------------------------------------------
    def sample(self, t, dt, lag_ms):
        mi = meminfo()
        vs = vmstat()
        r = {"t": f"{t:.3f}", "lag_ms": lag_ms, "dt": dt}
        r["avail_mb"], r["free_mb"] = mi["MemAvailable"], mi["MemFree"]
        r["anon_mb"] = mi.get("AnonPages", 0)
        r["file_mb"] = mi.get("Active(file)", 0) + mi.get("Inactive(file)", 0)
        r["shmem_mb"], r["dirty_mb"], r["writeback_mb"] = mi.get("Shmem", 0), mi.get("Dirty", 0), mi.get("Writeback", 0)
        r["slab_mb"] = mi.get("SReclaimable", 0) + mi.get("SUnreclaim", 0)
        r["swapcached_mb"], r["zswap_mb"], r["zswapped_mb"] = mi.get("SwapCached", 0), mi.get("Zswap", 0), mi.get("Zswapped", 0)
        r["swap_used_mb"] = mi["SwapTotal"] - mi["SwapFree"]
        sw = swaps()
        vdev, ddev, best = None, None, None
        vram_used = disk_used = 0
        for name, kind, size, used, prio in sw:
            if name.startswith("/dev/ublkb"):
                vdev, vram_used = os.path.basename(name), vram_used + used
            else:
                disk_used += used
                if best is None or prio > best:
                    best, ddev = prio, disk_of(name, kind)
        r["vram_swap_used_mb"], r["disk_swap_used_mb"] = vram_used, disk_used
        for key, path in (("mem", "/proc/pressure/memory"), ("io", "/proc/pressure/io"), ("cpu", "/proc/pressure/cpu")):
            some, full, s10, f10 = psi(path)
            r[f"{key}_some_ms"] = self.delta(f"psi_{key}_some", some) // 1000
            if key != "cpu":
                r[f"{key}_full_ms"] = self.delta(f"psi_{key}_full", full) // 1000
            if key == "mem":
                r["mem_some_avg10"], r["mem_full_avg10"] = s10, f10
        if t - self.user_t > 30:
            self.user, self.user_t = desktop_user(), t
        for key, sub in (("apps", "app.slice"), ("sess", "session.slice")):
            if self.user:
                some, full, _, _ = psi(f"{self.user[1]}/{sub}/memory.pressure")
                r[f"{key}_mem_some_ms"] = self.delta(f"{key}_some", some) // 1000
                r[f"{key}_mem_full_ms"] = self.delta(f"{key}_full", full) // 1000
            else:
                r[f"{key}_mem_some_ms"] = r[f"{key}_mem_full_ms"] = 0
        for col, key in (("pswpin", "pswpin"), ("pswpout", "pswpout"), ("majflt", "pgmajfault"),
                         ("refault_anon", "workingset_refault_anon"), ("refault_file", "workingset_refault_file"),
                         ("scan_kswapd", "pgscan_kswapd"), ("scan_direct", "pgscan_direct"),
                         ("steal_kswapd", "pgsteal_kswapd"), ("steal_direct", "pgsteal_direct"),
                         ("allocstall", "allocstall"), ("zswpin", "zswpin"), ("zswpout", "zswpout"),
                         ("zswpwb", "zswpwb"), ("oom_kill", "oom_kill")):
            r[col] = self.delta(f"vm_{key}", vs.get(key, 0))
        for pre, dev in (("vram", vdev), ("disk", ddev)):
            b = blkstat(dev) if dev else None
            if b is None:
                for c in ("rps", "wps", "dps", "rlat_us", "wlat_us", "rmbps", "wmbps"):
                    r[f"{pre}_{c}"] = ""
                continue
            d = {k: self.delta(f"blk_{dev}_{k}", v) for k, v in b.items() if k != "infl"}
            r[f"{pre}_rps"], r[f"{pre}_wps"], r[f"{pre}_dps"] = (round(d["rio"] / dt), round(d["wio"] / dt),
                                                               round(d["dio"] / dt))
            r[f"{pre}_rlat_us"] = round(d["rms"] * 1000 / d["rio"]) if d["rio"] else ""
            r[f"{pre}_wlat_us"] = round(d["wms"] * 1000 / d["wio"]) if d["wio"] else ""
            r[f"{pre}_rmbps"] = round(d["rsec"] * 512 / dt / 2**20, 1)
            r[f"{pre}_wmbps"] = round(d["wsec"] * 512 / dt / 2**20, 1)
        tot, idle, iow = cpustat()
        dtot = self.delta("cpu_tot", tot)
        didle, diow = self.delta("cpu_idle", idle), self.delta("cpu_iow", iow)
        r["cpu_pct"] = round(100 * (dtot - didle - diow) / dtot) if dtot else ""
        r["iowait_pct"] = round(100 * diow / dtot) if dtot else ""
        r["running"], r["blocked"] = procs_state()
        r["load1"] = read("/proc/loadavg").split()[0]
        # VRAM tier
        st = dict(kv.split("=") for kv in read(STATS).split() if "=" in kv) if vdev else {}
        r["tier"] = "off" if not vdev else ("on" if st.get("vram_held", "1") == "1" else "spilled")
        for col, key in (("tier_in_vram_mb", "in_vram_mb"), ("tier_on_disk_mb", "on_disk_mb"),
                         ("tier_live_mb", "live_mb"), ("tier_comp_pct", "comp_pct"),
                         ("tier_refused", "refused"), ("tier_errors", "errors")):
            r[col] = st.get(key, "")
        # GPU
        if t - self.gpu_t >= GPU_EVERY:
            self.gpu_t = t
            rt = read(f"{self.gpu}/power/runtime_status").strip() if self.gpu else ""
            row = (rt, "", "", "", "", "")
            if rt == "active":
                g = self.nvml.sample(keep_open=r["tier"] == "on")
                if g:
                    used, util, watts, procs = g
                    mine = {"usm-vramd", "gnome-shell", "Xwayland"}
                    others = {p: mb for p, mb in procs.items() if comm(p) not in mine}
                    top = max(others, key=others.get, default=None)
                    row = (rt, used, util, round(watts, 1), sum(others.values()),
                           f"{comm(top)}:{others[top]}" if top else "")
                    hogs = {comm(p) for p, mb in others.items() if mb >= 512}
                    for h in hogs - self.gpu_hogs:
                        self.event(f"GPU memory: {h} uses {max(mb for p, mb in others.items() if comm(p) == h)} MiB"
                                   f" (VRAM tier {r['tier']})")
                    self.gpu_hogs = hogs
            self.gpu_row = row
        (r["gpu_rt"], r["gpu_used_mb"], r["gpu_util"], r["gpu_w"], r["gpu_others_mb"], r["gpu_top"]) = self.gpu_row
        # desktop (GNOME helper extension) and guard
        dk = read_desktop(self.user[0]) if self.user else {}
        r["focus_app"] = dk.get("focus_app", "").replace(",", ";")
        r["idle_s"] = round(int(dk["idle_ms"]) / 1000) if dk.get("idle_ms", "").isdigit() else ""
        r["shell_lag_ms"] = int(dk["lag_max_ms"]) if dk.get("lag_max_ms", "").isdigit() else ""
        r["desk_age_s"] = round(dk["age"], 1) if dk else ""
        try:
            gs = json.loads(read(GUARD_STATE) or "{}")
        except ValueError:
            gs = {}
        fresh = gs and time.time() - gs.get("t", 0) < 10
        r["guard"] = gs.get("mode", "") if fresh else "off"
        r["frozen"] = len(gs.get("frozen_apps", [])) if fresh else 0
        r["frozen_mb"] = gs.get("frozen_mb", 0) if fresh else 0
        r["ac"], r["bat"] = power()
        self.samples.write(",".join(str(r[c]) for c in SAMPLE_COLS.split(",")))
        self.watch(t, r, sw, mi)
        if t - self.apps_t >= APPS_EVERY:
            self.apps_sample(t, gs if fresh else {}, dk)
            self.apps_t = t

    # --- events ------------------------------------------------------------------------
    def watch(self, t, r, sw, mi):
        names = {s[0]: s for s in sw}
        if self.swaps_prev is not None:
            for n in names.keys() - self.swaps_prev.keys():
                self.event(f"swap on: {n} {names[n][2]} MiB priority {names[n][4]}")
            for n in self.swaps_prev.keys() - names.keys():
                self.event(f"swap off: {n}")
        self.swaps_prev = names
        if self.tier_prev is not None and r["tier"] != self.tier_prev:
            self.event(f"VRAM tier: {self.tier_prev} -> {r['tier']}")
        self.tier_prev = r["tier"]
        if r["oom_kill"]:
            self.event(f"OOM killer killed {r['oom_kill']} process(es) (details: journalctl -k -g oom)")
        if r["lag_ms"] >= 1000:
            self.event(f"the logger itself ran {r['lag_ms'] / 1000:.1f} s late (system stalled)")
        if r["tier_refused"] not in ("", "0") and r["tier_refused"] != self.prev.get("refused_seen"):
            self.event(f"VRAM tier refused {r['tier_refused']} writes so far (full)")
            self.prev["refused_seen"] = r["tier_refused"]
        dt = r["dt"]
        bad = (r["mem_full_ms"] >= 100 * dt or r["mem_some_ms"] >= 500 * dt or r["sess_mem_full_ms"] >= 100 * dt
               or (r["shell_lag_ms"] or 0) >= 500)
        if bad:
            if self.episode is None:
                self.episode = Episode(t)
            self.episode.last_bad = t
        if self.episode:
            self.episode.add(t, r)
            if t - self.episode.last_bad >= 10:
                if self.episode.last_bad > self.episode.start or self.episode.full >= 200 or self.episode.lag_max >= 500:
                    self.event(self.episode.text(self.episode.last_bad + dt))
                self.episode = None

    def apps_sample(self, t, gs, dk):
        if not self.user:
            return
        uid, svc = self.user
        frozen = set(gs.get("frozen", []))
        focus = ""
        if dk.get("focus_pid", "").isdigit():
            focus = os.path.basename(read(f"/proc/{dk['focus_pid']}/cgroup").strip().rpartition("/")[2])
        rows = []
        targets = []
        for sub, kind, min_mb in (("app.slice", "app", 16), ("session.slice", "session", 32), ("background.slice", "background", 32)):
            base = f"{svc}/{sub}"
            targets.append((base, kind + "-total", 0))
            for c in glob.glob(f"{base}/*/"):
                targets.append((c.rstrip("/"), kind, min_mb))
        targets.append(("/sys/fs/cgroup/system.slice", "system-total", 0))
        for c in glob.glob("/sys/fs/cgroup/system.slice/*/"):
            targets.append((c.rstrip("/"), "system", 64))
        seen = set()
        for path, kind, min_mb in targets:
            cur, swap = cg_int(path, "memory.current"), cg_int(path, "memory.swap.current")
            if (cur + swap) >> 20 < min_mb:
                continue
            seen.add(path)
            st = cg_stat(path)
            some, full, _, _ = psi(f"{path}/memory.pressure")
            rb = wb = 0
            for line in read(f"{path}/io.stat").splitlines():     # disk bytes read/written, all devices
                for kv in line.split()[1:]:
                    k, _, v = kv.partition("=")
                    if k == "rbytes":
                        rb += int(v)
                    elif k == "wbytes":
                        wb += int(v)
            prev = self.cg_prev.get(path)
            self.cg_prev[path] = (st.get("pgmajfault", 0), some, full, rb, wb)
            if prev:
                majflt, dsome, dfull = (max(0, st.get("pgmajfault", 0) - prev[0]), max(0, some - prev[1]) // 1000,
                                        max(0, full - prev[2]) // 1000)
                drd, dwr = max(0, rb - prev[3]) >> 20, max(0, wb - prev[4]) >> 20
            else:
                majflt = dsome = dfull = drd = dwr = ""
            base = os.path.basename(path)
            name = app_name(base) if kind == "app" else base
            rows.append(f"{t:.0f},{name.replace(',', ';')},{base.replace(',', ';')},{kind},{cur >> 20},"
                        f"{st.get('anon', 0) >> 20},{st.get('file', 0) >> 20},{swap >> 20},{st.get('zswap', 0) >> 20},"
                        f"{majflt},{dsome},{dfull},{int(base in frozen)},{int(bool(focus) and focus == base)},{drd},{dwr}")
        for p in list(self.cg_prev):
            if p not in seen and not os.path.exists(p):
                del self.cg_prev[p]
        for row in rows:
            self.apps.write(row)

    # --- main loop ---------------------------------------------------------------------
    def run(self):
        sd_notify("READY=1")
        self.event(f"logger start (every {INTERVAL:g} s, {LOG_DIR}, keep {KEEP_DAYS} days / {MAX_MB} MiB)")
        mono, boot = time.monotonic(), time.clock_gettime(time.CLOCK_BOOTTIME)
        nxt = mono
        last = None
        while not self.stop:
            nxt += INTERVAL
            delay = nxt - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            now_m, now_b = time.monotonic(), time.clock_gettime(time.CLOCK_BOOTTIME)
            slept = (now_b - boot) - (now_m - mono)       # time in suspend/hibernate (monotonic stops there)
            if slept > 2:
                self.event(f"woke up after {slept:.0f} s asleep")
                self.prev.clear()                          # counters from before the sleep: no deltas
                self.cg_prev.clear()
                self.episode = None
                nxt = now_m
            mono, boot = now_m, now_b
            lag = max(0, round((now_m - nxt) * 1000))
            if lag > 5000:
                nxt = now_m                                # do not try to catch up on missed samples
            dt = (now_m - last) if last else INTERVAL
            last = now_m
            try:
                self.sample(time.time(), max(dt, 1e-3), lag)
            except Exception as e:                         # keep logging whatever happens
                if time.monotonic() - self.prev.get("err_t", -1e9) > 60:
                    self.event(f"logger error: {type(e).__name__}: {e}")
                    self.prev["err_t"] = time.monotonic()
            sd_notify("WATCHDOG=1")
        self.event("logger stop")
        for log in (self.samples, self.apps, self.events):
            log.close()


def main():
    libc.mlockall(7)                  # MCL_CURRENT | MCL_FUTURE | MCL_ONFAULT: never swapped out
    try:
        with open("/proc/self/oom_score_adj", "w") as f:
            f.write("-900")
    except OSError:
        pass
    lg = Logger()
    signal.signal(signal.SIGTERM, lambda *_: setattr(lg, "stop", True))
    signal.signal(signal.SIGINT, lambda *_: setattr(lg, "stop", True))
    lg.run()


if __name__ == "__main__":
    main()
