#!/usr/bin/env python3
"""usm-guard (prototype): keep Linux responsive under memory overload without killing apps.

Runs as root. Every child cgroup of --apps is treated as one app (on a desktop these
would be the per-app scopes systemd already creates). Works with unmodified apps.

Every TICK seconds it does three things:
  1. protect   Keep memory.low on the --protect cgroups (desktop, session, the app the
               user is working in), so the kernel takes memory from background apps first.
  2. load ctl  If memory pressure stays high, freeze one more background app and push its
               memory to swap in one bulk pass. Frozen and running apps trade places every
               SLICE seconds, so every app keeps making progress. A frozen app is let back
               in early only when its memory fits; it is prefetched in bulk before thawing.
  3. capacity  If swap is nearly full, add a temporary swap file so the OOM killer does
               not have to kill anything; remove it again when it is no longer needed.

Usage:
  usm-guard.py --apps /sys/fs/cgroup/usm-apps --protect /sys/fs/cgroup/usm-ui=192M \\
               [--protect CG=SIZE ...] [--mode full|protect] [--swap-dir DIR] [--log FILE]
  usm-guard.py --desktop [--exclude wezterm,zoom,...] [--log-dir /var/log/usm]
Desktop mode (a GNOME session): the apps are the desktop user's app scopes (one app = all scopes of one
launch, e.g. Chrome's two). Only when apps (or the desktop itself) really wait for memory for 3 s does it
pause the busiest background app (most page faults / memory growth) - briefly: until the pressure has
settled, at most 20 s, then that app has a 2 min cooldown. Never paused: the app in front and apps that were
in front in the last 5 min (from the usm-desktop GNOME extension; a paused app runs again the moment you
switch to it), idle apps (pausing them relieves nothing), apps playing or recording sound, apps matching
--exclude (terminals, IDEs: their programs often serve the app in front), small apps, desktop services. Not
when the disk is the bottleneck (pausing does not fix a busy SSD). If the desktop lags while an app is
paused but memory is calm, everything runs again at once. Without the extension it only logs "would-freeze".
"""
import argparse
import ctypes
import glob
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "tools"))
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from usm_log import DailyLog, app_name, desktop_user, read_desktop, sd_notify  # noqa: E402

TICK = 0.5
# Pressure is measured on the managed apps' cgroup: share of time at least one app is
# stalled waiting for memory ("some"). System-wide "full" misses apps taking turns stalling.
HIGH_PRESSURE = 0.30    # "overloaded"
LOW_PRESSURE = 0.05     # "calm"
PAGING_FAULTS = 300     # major faults/s by the managed apps that count as "actively paging"
HIGH_TICKS = 2          # consecutive overloaded ticks before freezing an app
LOW_TICKS = 4           # consecutive calm ticks before an early thaw
ACTION_GAP = 1.5        # minimum seconds between load-control actions
SLICE = 20.0            # minimum time an app stays frozen before it gets a turn
SLICE_COST_FACTOR = 4   # ...and at least 4x the time the swap traffic of a rotation costs
MAX_SLICE = 60.0        # ...but never longer: with slow swap (hard disk) that estimate reaches minutes,
                        # and an app standing still for minutes looks hung to its user
SWAP_LOW_MB = 512       # keep at least max(this, RAM/4) of swap free; add a file below that
SWAP_FILE_MB = 512
PAUSE_FILE = "/run/usm-guard.pause"   # present while a swap device is being emptied
STATE_FILE = "/run/usm-guard.state"   # what the guard is doing now (usm status, usm-logger)
FOCUS_STALE = 600       # desktop: focus info older than this (extension gone) = no focus info
AUDIO_CACHE = 5.0       # seconds an audio-stream lookup stays valid
# Desktop mode (2026-11-05, after the first day of real use): short pauses of busy background apps, only when
# apps really wait for memory. Faults and disk pressure alone are no reason (with VRAM/zswap a fault is cheap, and
# a busy SSD is not fixed by pausing apps).
DESK_TRIGGER = 0.30     # apps (app.slice) waiting for memory this share of the time ...
DESK_SESS_TRIGGER = 0.10  # ... or the desktop itself (session.slice: GNOME Shell, sound) this share
DESK_HIGH_TICKS = 6     # ... for 3 s in a row
DESK_CALM = 0.05        # below this for DESK_CALM_TICKS (2 s): pressure settled
DESK_CALM_TICKS = 4
PAUSE_MIN = 5.0         # a pause lasts at least this (unless the app comes to the front) ...
PAUSE_MAX = 20.0        # ... and at most this
COOLDOWN = 120.0        # an app is not paused again for this long after a pause
RECENT = 300.0          # apps that were in front this recently are never paused
MAX_PAUSED = 2          # at most this many apps paused at once
ACTIVE_FAULTS = 50      # a background app counts as "busy" (worth pausing) with this many major faults/s ...
ACTIVE_GROWTH = 20 << 20  # ... or this much memory growth per second
LAG_BRAKE_MS = 1000     # desktop lag this long while apps are paused but memory is calm: let them all run
BRAKE_COOLDOWN = 1800.0 # ... and do not pause those apps again for this long
DISK_BUSY_MS = 100.0    # the disk takes longer than this per read: it is the bottleneck, pausing will not help

libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
SYS_PIDFD_OPEN, SYS_PROCESS_MADVISE, MADV_WILLNEED = 434, 440, 3
SYS_IOPRIO_SET, IOPRIO_WHO_PROCESS, IOPRIO_LOWEST_BE = 251, 1, (2 << 13) | 7
ROOM_MARGIN = 64 << 20  # push out this much extra when making room
FAULT_CHUNK = 256 << 20 # push out this much more while the running apps keep faulting


class IOVec(ctypes.Structure):
    _fields_ = [("base", ctypes.c_void_p), ("len", ctypes.c_size_t)]


def parse_size(s):
    units = {"K": 2**10, "M": 2**20, "G": 2**30}
    return int(float(s[:-1]) * units[s[-1].upper()]) if s[-1].upper() in units else int(s)


def read(path):
    with open(path) as f:
        return f.read()


def read_safe(path):
    try:
        return read(path)
    except OSError:
        return ""


def write(path, value):
    with open(path, "w") as f:
        f.write(str(value))


def meminfo():
    out = {}
    for line in read("/proc/meminfo").splitlines():
        k, v = line.split(":")
        out[k] = int(v.split()[0]) * 1024
    return out


def zram_pool_bytes():
    """RAM used by zram for compressed pages (not charged to any app's cgroup)."""
    total = 0
    for st in glob.glob("/sys/block/zram*/mm_stat"):
        try:
            total += int(read(st).split()[2])
        except (OSError, IndexError, ValueError):
            pass
    return total


def root_disk():
    """Name of the whole disk holding / (e.g. nvme0n1), or None."""
    try:
        dev = os.stat("/").st_dev
        sysdev = os.path.realpath(f"/sys/dev/block/{os.major(dev)}:{os.minor(dev)}")
        if os.path.exists(f"{sysdev}/partition"):
            sysdev = os.path.dirname(sysdev)
        return os.path.basename(sysdev)
    except OSError:
        return None


def vram_top():
    """True if the highest-priority swap with room is the VRAM tier (usm-vramd's ublk device)."""
    best = None
    for line in read("/proc/swaps").splitlines()[1:]:
        name, kind, size, used, prio = line.split()
        if int(used) < int(size) and (best is None or int(prio) > best[1]):
            best = (name, int(prio))
    return bool(best) and best[0].startswith("/dev/ublkb")


def fast_swap():
    """True if the highest-priority active swap is fast (zram, VRAM tier, or a non-rotating disk)."""
    best = None
    for line in read("/proc/swaps").splitlines()[1:]:
        name, kind, size, used, prio = line.split()
        if int(used) < int(size) and (best is None or int(prio) > best[1]):
            best = (name, int(prio), kind)
    if best is None:
        return True
    name, _, kind = best
    if name.startswith(("/dev/zram", "/dev/usmvram", "/dev/ublkb")):
        return True
    try:
        st = os.stat(name)
        dev = st.st_rdev if kind == "partition" else st.st_dev
        sysdev = os.path.realpath(f"/sys/dev/block/{os.major(dev)}:{os.minor(dev)}")
        for d in (sysdev, os.path.dirname(sysdev)):    # partitions: the queue is on the parent disk
            if os.path.exists(f"{d}/queue/rotational"):
                return read(f"{d}/queue/rotational").strip() == "0"
    except OSError:
        pass
    return True


class PSI:
    """Share of wall time with tasks stalled on memory, from a PSI file ("some" or "full")."""

    def __init__(self, path="/proc/pressure/memory", kind="some"):
        self.path, self.kind = path, kind
        self.last, self.t = self._total(), time.monotonic()

    def _total(self):
        for line in read(self.path).splitlines():
            if line.startswith(self.kind):
                return int(line.split("total=")[1])
        return 0

    def sample(self):
        total, t = self._total(), time.monotonic()
        frac = (total - self.last) / 1e6 / max(t - self.t, 1e-3)
        self.last, self.t = total, t
        return frac


class App:
    """One app: one cgroup, or (desktop) all scopes of one launch - they share the launcher's PID suffix."""

    def __init__(self, path, name=None):
        self.paths = [path]
        self.name = name or os.path.basename(path)
        self.frozen = False
        self.since = time.monotonic()   # when it was last frozen or thawed
        self.reclaimer = None           # background thread pushing a frozen app's memory out
        self.last_front = -1e9          # desktop: when it was last the app in front
        self.cooldown_until = -1e9      # desktop: not paused again before this
        self.pause_reason = ""
        self.pushed = False
        self.flt_prev = self.mem_prev = self.act_t = None
        self.flt_rate = self.growth = 0.0

    @property
    def path(self):
        return self.paths[0]

    def populated(self):
        for p in self.paths:
            try:
                if "populated 1" in read(f"{p}/cgroup.events"):
                    return True
            except OSError:
                pass
        return False

    def pids(self):
        out = []
        for p in self.paths:
            try:
                out += [int(x) for x in read(f"{p}/cgroup.procs").split()]
            except OSError:
                pass
        return out

    def stat(self, name):
        total = 0
        for p in self.paths:
            try:
                total += int(read(f"{p}/{name}"))
            except (OSError, ValueError):
                pass
        return total

    def set_frozen(self, value):
        for p in self.paths:
            try:
                write(f"{p}/cgroup.freeze", int(value))
            except OSError:
                pass
        if not value and getattr(self, "zswap_off", False):
            self.set_zswap(True)

    def set_zswap(self, allow):
        """zswap (compressed pages kept in RAM) on or off for this app. Off while it is paused and the VRAM
        tier is the top swap: its memory should leave RAM, not be packed into it."""
        for p in self.paths:
            try:
                write(f"{p}/memory.zswap.max", "max" if allow else 0)
            except OSError:
                pass
        self.zswap_off = not allow

    def reclaim_bytes(self, amount):
        """memory.reclaim on each cgroup of the app, in proportion to its memory."""
        cur = {p: self.stat_one(p, "memory.current") for p in self.paths}
        total = sum(cur.values()) or 1
        for p, c in cur.items():
            share = amount * c // total
            if share >= (1 << 20):
                try:
                    write(f"{p}/memory.reclaim", share)
                except OSError:
                    pass   # EAGAIN = could not reclaim everything; that's fine

    @staticmethod
    def stat_one(p, name):
        try:
            return int(read(f"{p}/{name}"))
        except (OSError, ValueError):
            return 0

    def activity(self, now):
        """Desktop: update this app's major-fault rate and memory growth (per second, smoothed)."""
        flt = 0
        for p in self.paths:
            for line in read_safe(f"{p}/memory.stat").splitlines():
                if line.startswith("pgmajfault "):
                    flt += int(line.split()[1])
                    break
        mem = self.stat("memory.current")
        if self.act_t is not None and now > self.act_t:
            dt = now - self.act_t
            rate = max(0, flt - self.flt_prev) / dt
            growth = (mem - self.mem_prev) / dt
            self.flt_rate = 0.5 * self.flt_rate + 0.5 * rate
            self.growth = 0.5 * self.growth + 0.5 * growth
        self.flt_prev, self.mem_prev, self.act_t = flt, mem, now

    def footprint(self):
        """Memory the app needs to be fully resident: what is in RAM plus what is in swap."""
        return self.stat("memory.current") + self.stat("memory.swap.current")


def prefetch_pid(pid, budget):
    """Ask the kernel to start reading this process's swapped-out memory back in bulk
    (at most `budget` bytes of address space)."""
    fd = libc.syscall(ctypes.c_long(SYS_PIDFD_OPEN), ctypes.c_long(pid), ctypes.c_long(0))
    if fd < 0:
        return 0
    total = 0
    try:
        regions = []
        for line in read(f"/proc/{pid}/maps").splitlines():
            parts = line.split()
            perms, path = parts[1], (parts[5] if len(parts) > 5 else "")
            if not perms.startswith("rw") or perms[3] != "p":
                continue
            if path and path not in ("[heap]", "[stack]"):
                continue
            lo, hi = (int(x, 16) for x in parts[0].split("-"))
            n = min(hi - lo, budget)
            if n <= 0:
                break
            regions.append((lo, n))
            budget -= n
        for i in range(0, len(regions), 512):
            chunk = regions[i:i + 512]
            arr = (IOVec * len(chunk))(*[IOVec(lo, n) for lo, n in chunk])
            r = libc.syscall(ctypes.c_long(SYS_PROCESS_MADVISE), ctypes.c_long(fd),
                             ctypes.cast(arr, ctypes.c_void_p), ctypes.c_long(len(chunk)),
                             ctypes.c_long(MADV_WILLNEED), ctypes.c_long(0))
            if r > 0:
                total += r
    finally:
        os.close(fd)
    return total


class Guard:
    def __init__(self, args):
        self.desktop = args.desktop
        self.excludes = [e.strip().lower() for e in (args.exclude or "").split(",") if e.strip()]
        self.min_app = args.min_app_mb << 20
        self.protect_size = parse_size(args.protect_desktop)
        self.high_ticks = args.high_ticks or (DESK_HIGH_TICKS if self.desktop else HIGH_TICKS)
        self.trigger = args.trigger or DESK_TRIGGER
        self.pause_max, self.cooldown, self.recent = args.pause_max, args.cooldown, args.recent
        self.sess_psi = None
        self.sess_now = 0.0
        self.disk = root_disk()
        self.disk_prev = None
        self.disk_lat = []       # recent per-tick average read latency of the root disk, ms
        self.uid = self.user_name = None
        self.apps_root = None
        self.extra_protect = [(cg, parse_size(sz)) for cg, sz in (p.split("=") for p in args.protect)]
        self.protect = list(self.extra_protect)
        self.protect_orig = {}
        self.remember_protection()
        self.mode = args.mode
        self.swap_dir = args.swap_dir
        header = "t,action,app,detail,apps_pressure,mem_avail_mb,swap_free_mb"
        self.logf = self.dlog = None
        if args.log_dir:
            self.dlog = DailyLog(args.log_dir, "guard", "csv", header, args.keep_days, args.max_mb)
        else:
            self.logf = open(args.log, "a", buffering=1)
            self.logf.write(header + "\n")
        self.apps = {}
        self.focus_key, self.focus_valid, self.focus_name = None, None, ""
        self.audio = (-1e9, set())
        self.quiet = {}          # rate limit for repeated log lines
        self.io_now = 0.0
        self.majflt_rate = 0.0
        if args.apps:
            self.attach(args.apps)
        self.swap_bw = 100 << 20     # bytes/s, refined from measured reclaim speed
        self.psi_now = 0.0
        self.busy = threading.Lock()     # one freeze/thaw/rotation at a time
        self.swapfiles = []      # temporary swap files currently enabled
        self.spare = None        # next swap file, created in advance so enabling it is instant
        self.next_swap_id = 0
        self.swap_busy = False
        self.stop = False
        # Kernel-enforced backstop: cap the RAM the background apps may use together. When
        # they exceed it the kernel pushes them to swap, and if swap is full it throttles
        # their allocations (pauses them) instead of the OOM killer killing something.
        self.apps_high = None
        if args.apps_high != "off" and not (self.desktop and args.apps_high == "auto"):
            self.apps_high = (meminfo()["MemAvailable"] - (48 << 20) if args.apps_high == "auto"
                              else parse_size(args.apps_high))

    # ---- helpers -------------------------------------------------------------
    def attach(self, root):
        self.apps_root = root
        self.psi = PSI(f"{root}/memory.pressure", "some")
        self.io_psi = PSI(f"{root}/io.pressure", "some")   # swap-in waits on a slow disk show up here
        # PSI only counts refaults of recently used pages; random access over a big working set
        # looks like plain I/O to it. So also watch the apps' major-fault (swap-in) rate.
        self.majflt_last, self.majflt_t = self.apps_majflt(), time.monotonic()

    def remember_protection(self):
        for cg, _ in self.protect:      # restored on exit
            if cg not in self.protect_orig:
                try:
                    self.protect_orig[cg] = read(f"{cg}/memory.low").strip()
                except OSError:
                    pass

    def once_per(self, key, seconds):
        """True at most once per `seconds` for `key` (keeps repeated log lines from flooding)."""
        now = time.monotonic()
        if now - self.quiet.get(key, -1e9) < seconds:
            return False
        self.quiet[key] = now
        return True

    def apps_majflt(self):
        try:
            for line in read(f"{self.apps_root}/memory.stat").splitlines():
                if line.startswith("pgmajfault "):
                    return int(line.split()[1])
        except OSError:
            pass
        return 0

    def log(self, action, app="", detail=""):
        mi = meminfo()
        detail = f"{detail} majflt/s={self.majflt_rate:.0f} io={self.io_now:.2f}".strip().replace(",", ";")
        line = (f"{time.time():.3f},{action},{app.replace(',', ';')},{detail},{self.psi_now:.3f},"
                f"{mi['MemAvailable'] >> 20},{mi['SwapFree'] >> 20}")
        if self.dlog:
            self.dlog.write(line)
        else:
            self.logf.write(line + "\n")

    def discover(self):
        if self.desktop:
            return self.discover_desktop()
        try:
            names = [n for n in os.listdir(self.apps_root)
                     if os.path.isdir(os.path.join(self.apps_root, n))]
        except OSError:
            names = []
        for n in names:
            if n not in self.apps:
                self.apps[n] = App(os.path.join(self.apps_root, n))
        for n in list(self.apps):
            if n not in names or not self.apps[n].populated():
                del self.apps[n]

    def discover_desktop(self):
        """Apps = the user's app scopes (desktop services in app.slice are not apps), grouped per launch."""
        groups = {}
        try:
            names = os.listdir(self.apps_root)
        except OSError:
            names = []
        for n in names:
            path = os.path.join(self.apps_root, n)
            if n.endswith(".scope") and os.path.isdir(path):
                groups.setdefault(self.group_key(n), []).append(path)
        for key, paths in groups.items():
            paths.sort()
            app = self.apps.get(key)
            if app is None:
                app = self.apps[key] = App(paths[0], name=app_name(os.path.basename(min(paths, key=len))))
                app.key = key
            if app.paths != paths:
                new = set(paths) - set(app.paths)
                app.paths = paths
                if app.frozen:       # a new scope of a paused app pauses too
                    for p in new:
                        try:
                            write(f"{p}/cgroup.freeze", 1)
                        except OSError:
                            pass
        for key in list(self.apps):
            if key not in groups or not self.apps[key].populated():
                del self.apps[key]

    @staticmethod
    def group_key(scope):
        m = re.search(r"-(\d+)\.scope$", scope)
        return f"pid{m.group(1)}" if m else scope

    def find_user(self):
        """Desktop: follow the logged-in desktop user (none before login / after logout)."""
        u = desktop_user()
        root = f"{u[1]}/app.slice" if u else None
        if root == self.apps_root:
            return
        for a in self.apps.values():
            if a.frozen:
                a.set_frozen(0)
        self.apps = {}
        if not u:
            self.log("desktop", detail="no desktop user logged in - waiting")
            self.apps_root = None
            return
        self.uid, svc = u
        try:
            import pwd
            self.user_name = pwd.getpwuid(self.uid).pw_name
        except (KeyError, ImportError):
            self.user_name = None
        self.attach(root)
        try:
            self.sess_psi = PSI(f"{svc}/session.slice/memory.pressure", "some")
        except OSError:
            self.sess_psi = None
        chain = ["/sys/fs/cgroup/user.slice", f"/sys/fs/cgroup/user.slice/user-{self.uid}.slice", svc,
                 f"{svc}/session.slice"]
        self.protect = [(c, self.protect_size) for c in chain] + self.extra_protect
        self.remember_protection()
        # Apps left paused by a guard that crashed or was killed: let them run again (and use zswap again).
        for f in glob.glob(f"{root}/*/cgroup.freeze"):
            try:
                if read(f).strip() == "1":
                    write(f, 0)
                    self.log("thaw-leftover", app_name(os.path.basename(os.path.dirname(f))))
            except OSError:
                pass
        for f in glob.glob(f"{root}/*/memory.zswap.max"):
            try:
                if read(f).strip() == "0":
                    write(f, "max")
            except OSError:
                pass
        self.log("desktop", detail=f"user {self.uid}: apps in {root}; desktop kept in RAM up to "
                                   f"{self.protect_size >> 20}MB; never paused: {','.join(self.excludes) or '-'}")

    def update_focus(self):
        """Desktop: which app is in front (reported by the usm-desktop GNOME extension)."""
        d = read_desktop(self.uid) if self.uid is not None else {}
        valid = bool(d) and d["age"] < FOCUS_STALE
        key = None
        pid = d.get("focus_pid", "")
        if valid and pid.isdigit() and pid != "0":
            parts = read_safe(f"/proc/{pid}/cgroup").strip().split("::", 1)[-1].split("/")
            if "app.slice" in parts and parts.index("app.slice") + 1 < len(parts):
                key = self.group_key(parts[parts.index("app.slice") + 1])
        if valid != self.focus_valid:
            self.log("focus-info", detail="available (GNOME extension usm-desktop): pausing allowed" if valid
                     else "missing (GNOME extension usm-desktop not running): only logging what it would do")
        self.focus_valid, self.focus_key, self.focus_name = valid, key, d.get("focus_app", "")

    def audio_pids(self):
        """PIDs with a running sound stream (playing or recording: music, calls) - not paused."""
        t, pids = self.audio
        if time.monotonic() - t < AUDIO_CACHE:
            return pids
        pids = set()
        if self.user_name:
            try:
                out = subprocess.run(["runuser", "-u", self.user_name, "--", "env", f"XDG_RUNTIME_DIR=/run/user/{self.uid}",
                                      "pw-dump"], capture_output=True, timeout=3).stdout
                objs = json.loads(out or b"[]")
                clients = {}
                for o in objs:
                    if o.get("type") == "PipeWire:Interface:Client":
                        cp = (o.get("info") or {}).get("props") or {}
                        clients[o.get("id")] = cp.get("application.process.id") or cp.get("pipewire.sec.pid")
                for o in objs:
                    info = o.get("info") or {}
                    props = info.get("props") or {}
                    if (o.get("type") == "PipeWire:Interface:Node" and str(props.get("media.class", "")).startswith("Stream/")
                            and info.get("state") == "running"):
                        pid = props.get("application.process.id") or clients.get(props.get("client.id"))
                        if pid:
                            pids.add(int(pid))
            except (OSError, subprocess.SubprocessError, ValueError, TypeError):
                pass
        self.audio = (time.monotonic(), pids)
        return pids

    def eligible(self, app):
        """Desktop: None if the app may be paused now, else why not."""
        if getattr(app, "key", None) == self.focus_key:
            return "in-front"
        names = " ".join(os.path.basename(p).lower() for p in app.paths)
        for e in self.excludes:
            if e in names:
                return "excluded"
        if app.stat("memory.current") < self.min_app:
            return "small"
        return None

    def pick(self, apps, order):
        """Desktop: the app to pause - the biggest (order=memory) or longest running (order=since) one that
        may be paused. Returns (app or None, notes on the apps skipped)."""
        notes, cands = [], []
        for a in apps:
            why = self.eligible(a)
            if why:
                if why != "small":
                    notes.append(f"{a.name}:{why}")
            else:
                cands.append(a)
        now = time.monotonic()
        cands.sort(key=(lambda a: -a.stat("memory.current")) if order == "memory" else (lambda a: a.since - now))
        audio = self.audio_pids() if cands else set()
        for a in cands:
            if audio and set(a.pids()) & audio:
                notes.append(f"{a.name}:sound")
                continue
            return a, notes
        return None, notes

    def thaw_focus(self, app):
        """The user switched to a paused app: let it run at once, read its memory back in the background."""
        app.set_frozen(0)
        app.frozen, app.since = False, time.monotonic()
        self.log("thaw", app.name, f"in-front swapped={app.stat('memory.swap.current') >> 20}MB")

        def prefetch():
            budget = min(app.stat("memory.swap.current"), int(meminfo()["MemAvailable"] * 0.8))
            if budget > (16 << 20):
                pids = app.pids()
                for p in pids:
                    prefetch_pid(p, budget // max(len(pids), 1))
        threading.Thread(target=prefetch, daemon=True).start()

    def write_state(self):
        frozen = [a for a in self.apps.values() if a.frozen]
        if not self.apps_root:
            mode = "waiting"
        elif self.desktop and self.mode == "full" and not self.focus_valid:
            mode = "observe"
        else:
            mode = self.mode
        st = {"t": round(time.time(), 1), "mode": mode, "user": self.uid, "focus": self.focus_name,
              "focus_valid": bool(self.focus_valid), "apps": len(self.apps),
              "frozen": [os.path.basename(p) for a in frozen for p in a.paths],
              "frozen_apps": [a.name for a in frozen],
              "frozen_mb": sum(a.footprint() for a in frozen) >> 20, "pressure": round(self.psi_now, 3)}
        try:
            with open(STATE_FILE + ".tmp", "w") as f:
                json.dump(st, f)
            os.rename(STATE_FILE + ".tmp", STATE_FILE)
        except OSError:
            pass

    def apply_protection(self):
        for cg, size in self.protect:
            try:
                if read(f"{cg}/memory.low").strip() != str(size):
                    write(f"{cg}/memory.low", size)
            except OSError:
                pass

    # ---- load control ------------------------------------------------------
    def freeze(self, app, reason, push=None, notes=None):
        """Pause the app. On fast swap (zram, VRAM, SSD) push all of its memory out right away;
        on a slow rotating disk push out only as much as the running apps need (make_room),
        because pushing out a whole app there can take minutes."""
        app.set_frozen(1)
        app.frozen, app.since = True, time.monotonic()
        mem = app.stat("memory.current")
        self.log("freeze", app.name, f"{reason} mem={mem >> 20}MB" + (f" kept-running={' '.join(notes)}" if notes else ""))
        if push is None:
            push = fast_swap()
        if push and mem > (16 << 20):
            if vram_top():
                app.set_zswap(False)
            self.push_out(app, mem)

    def reclaiming(self):
        return any(a.reclaimer and a.reclaimer.is_alive() for a in self.apps.values())

    def push_out(self, app, amount, wait=False):
        """Push `amount` bytes of a frozen app's memory to swap, in the background."""
        app.reclaimer = threading.Thread(target=self.reclaim, args=(app, amount), daemon=True)
        app.reclaimer.start()
        if wait:
            app.reclaimer.join(timeout=180)

    def reclaim(self, app, amount):
        # Lowest disk priority: system services (WiFi, SSH, the desktop) get their reads first.
        libc.syscall(ctypes.c_long(SYS_IOPRIO_SET), ctypes.c_long(IOPRIO_WHO_PROCESS),
                     ctypes.c_long(0), ctypes.c_long(IOPRIO_LOWEST_BE))
        if app.frozen and vram_top() and not getattr(app, "zswap_off", False):
            app.set_zswap(False)
        mem = app.stat("memory.current")
        t = time.monotonic()
        # With an asynchronous swap device (the VRAM tier, a disk) one pass only starts the writes and the
        # next one frees the written pages; with zswap a pass may only move pages into zswap (still RAM).
        stuck = 0
        for _ in range(6):
            before = app.stat("memory.current")
            left = amount - (mem - before)
            if left < amount // 10 or stuck >= 2 or not app.frozen:
                break
            app.reclaim_bytes(left)
            stuck = stuck + 1 if before - app.stat("memory.current") < (4 << 20) else 0
            time.sleep(0.05)
        dt, freed = time.monotonic() - t, mem - app.stat("memory.current")
        if dt > 0.05 and freed > (8 << 20):
            self.swap_bw = int(0.7 * self.swap_bw + 0.3 * freed / dt)
        self.log("pushed-out", app.name, f"asked={amount >> 20}MB freed={freed >> 20}MB in {dt:.1f}s")

    def thaw(self, app, reason):
        swapped = app.stat("memory.swap.current")
        budget = min(swapped, int(meminfo()["MemAvailable"] * 0.8))   # prefetch what fits
        if budget > (16 << 20):
            started = sum(prefetch_pid(p, budget // max(len(app.pids()), 1)) for p in app.pids())
            self.log("prefetch", app.name, f"swapped={swapped >> 20}MB advised={started >> 20}MB")
        app.set_frozen(0)
        app.frozen, app.since = False, time.monotonic()
        self.log("thaw", app.name, reason)

    def run_action(self, fn):
        def work():
            try:
                fn()
            finally:
                self.busy.release()
        if self.busy.acquire(blocking=False):
            threading.Thread(target=work, daemon=True).start()
            return True
        return False

    # ---- capacity ------------------------------------------------------------
    def capacity(self):
        if self.swap_busy:
            return
        mi = meminfo()
        free = mi["SwapFree"]
        usable = mi["MemTotal"] - (mi.get("HugePages_Total", 0) >> 10) * mi.get("Hugepagesize", 0)  # huge pages held back don't count
        keep_free = max(SWAP_LOW_MB << 20, usable // 4)
        if mi["SwapTotal"] and free < keep_free:
            self.swap_busy = True
            threading.Thread(target=self.add_swap, daemon=True).start()
        elif self.swapfiles and free > keep_free + 2 * (SWAP_FILE_MB << 20):
            self.swap_busy = True
            threading.Thread(target=self.remove_swap, daemon=True).start()
        elif self.spare is None:
            self.swap_busy = True
            threading.Thread(target=self.prepare_spare, daemon=True).start()

    def prepare_spare(self, release=True):
        """Create (but do not enable) the next swap file."""
        try:
            os.makedirs(self.swap_dir, exist_ok=True)
            st = os.statvfs(self.swap_dir)
            if st.f_bavail * st.f_frsize < (SWAP_FILE_MB << 20) * 2:
                return
            path = os.path.join(self.swap_dir, f"swap{self.next_swap_id}")
            self.next_swap_id += 1
            subprocess.run(["fallocate", "-l", f"{SWAP_FILE_MB}M", path], check=True)
            os.chmod(path, 0o600)
            subprocess.run(["mkswap", path], check=True, capture_output=True)
            self.spare = path
        except (OSError, subprocess.CalledProcessError) as e:
            self.log("swap-prepare-failed", detail=str(e).replace(",", ";"))
        finally:
            if release:
                self.swap_busy = False

    def add_swap(self):
        try:
            if self.spare is None:
                self.prepare_spare(release=False)
            if self.spare is None:
                if time.monotonic() - getattr(self, "skip_logged", -1e9) > 60:   # log at most once a minute
                    self.log("swap-add-skipped", detail="could not create a swap file (disk space?)")
                    self.skip_logged = time.monotonic()
                return
            path, self.spare = self.spare, None
            subprocess.run(["swapon", path], check=True)   # auto priority: below existing swap
            self.swapfiles.append(path)
            self.log("swap-add", detail=f"{path} {SWAP_FILE_MB}MB")
            self.prepare_spare(release=False)                # get the next one ready
        except (OSError, subprocess.CalledProcessError) as e:
            self.log("swap-add-failed", detail=str(e).replace(",", ";"))
        finally:
            self.swap_busy = False

    def remove_swap(self, final=False):
        try:
            while self.swapfiles:
                path = self.swapfiles[-1]
                if subprocess.run(["swapoff", path]).returncode != 0:
                    break
                os.unlink(path)
                self.swapfiles.pop()
                self.log("swap-remove", detail=path)
                if not final:
                    break
            if final and self.spare:
                os.unlink(self.spare)
                self.spare = None
        finally:
            self.swap_busy = False

    # ---- main loop -------------------------------------------------------------
    def loop(self):
        st = {"high": 0, "low": 0, "last_action": 0.0, "paused": False}
        user_t = -1e9
        if self.apps_high and self.apps_root:
            write(f"{self.apps_root}/memory.high", self.apps_high)
            self.log("apps-high", detail=f"{self.apps_high >> 20}MB")
        sd_notify("READY=1")
        while not self.stop:
            sd_notify("WATCHDOG=1")
            if self.desktop and (time.monotonic() - user_t > 5 or not self.apps_root):
                user_t = time.monotonic()
                try:
                    self.find_user()
                except Exception as e:
                    if self.once_per("find-user", 60):
                        self.log("error", detail=f"find_user {type(e).__name__}: {e}")
            if self.apps_root:
                try:
                    self.tick_desktop(st) if self.desktop else self.tick(st)
                except Exception as e:      # never die with apps paused: log and go on
                    if self.once_per("tick-error", 60):
                        self.log("error", detail=f"{type(e).__name__}: {e}")
            if not self.desktop or time.monotonic() - getattr(self, "state_t", -1e9) >= 2 or self.psi_now >= LOW_PRESSURE:
                self.state_t = time.monotonic()
                self.write_state()
            time.sleep(TICK if self.apps_root else 1.0)

    def tick(self, st):
        self.psi_now = self.psi.sample()
        self.io_now = self.io_psi.sample()
        n, t = self.apps_majflt(), time.monotonic()
        self.majflt_rate = (n - self.majflt_last) / max(t - self.majflt_t, 1e-3)
        self.majflt_last, self.majflt_t = n, t
        # Desktop (a laptop, maybe on battery): the slow-changing parts not every tick unless under pressure.
        calm_now = self.psi_now < LOW_PRESSURE and self.io_now < 0.10
        if not self.desktop or not calm_now or t - st.get("disc_t", -1e9) >= 2:
            st["disc_t"] = t
            self.discover()
        if self.desktop:
            self.update_focus()
        if not self.desktop or t - st.get("prot_t", -1e9) >= 5:
            st["prot_t"] = t
            self.apply_protection()
        if not self.desktop or not calm_now or t - st.get("cap_t", -1e9) >= 2:
            st["cap_t"] = t
            self.capacity()
        if self.mode != "full":
            return
        now = time.monotonic()
        avail = meminfo()["MemAvailable"]
        if self.apps_high:
            cap = self.apps_high
        elif self.desktop:
            # The apps could have what is available plus what they hold now.
            cap = avail + sum(a.stat("memory.current") for a in self.apps.values())
        else:
            cap = avail
        if self.desktop and self.focus_key in self.apps and self.apps[self.focus_key].frozen:
            self.thaw_focus(self.apps[self.focus_key])    # the user switched to it: right away
        running = [a for a in self.apps.values() if not a.frozen]
        frozen = [a for a in self.apps.values() if a.frozen]
        act = not self.desktop or self.focus_valid        # desktop without focus info: only log
        # A swap device is being emptied (usm-vram-move): background apps wait meanwhile, so
        # the move has the RAM and the disk to itself. Normal operation brings them back after.
        if os.path.exists(PAUSE_FILE):
            if not st["paused"]:
                self.log("pause", detail=read(PAUSE_FILE).strip())
                st["paused"] = True
            for a in running:
                if act and (not self.desktop or self.eligible(a) is None):
                    self.freeze(a, "pause", push=False)
            return
        if st["paused"]:
            self.log("resume")
            st["paused"], st["last_action"] = False, 0.0
        faulting = self.majflt_rate >= 20
        # Frozen apps still hold RAM until pushed out, and with zram the compressed copy of a
        # frozen app's pages also stays in RAM (not charged to the app): count both.
        sw_frozen = sum(a.stat("memory.swap.current") for a in frozen)
        sw_all = sw_frozen + sum(a.stat("memory.swap.current") for a in running)
        zram_frozen = zram_pool_bytes() * sw_frozen // sw_all if sw_all else 0
        demand = (sum(a.footprint() for a in running) + sum(a.stat("memory.current") for a in frozen)
                  + zram_frozen)
        too_big = demand > cap
        stalled = (self.io_now >= HIGH_PRESSURE or self.majflt_rate >= PAGING_FAULTS)
        # Faults only count as overload when apps are actually waiting on a disk (io pressure);
        # with zram/VRAM a fault costs microseconds of CPU and is not a reason to freeze.
        disk_bound = self.io_now >= HIGH_PRESSURE and faulting
        overloaded = self.psi_now >= HIGH_PRESSURE or (stalled and too_big) or disk_bound
        st["high"] = st["high"] + 1 if overloaded else 0
        # Make room for the running apps: push out just the excess, largest frozen app first.
        # Accounting can be optimistic (the rest of the system needs RAM too), so if the running
        # apps keep faulting while frozen apps still hold RAM, push out another chunk.
        if frozen and not self.reclaiming() and (too_big or disk_bound):
            victim = max(frozen, key=lambda a: a.stat("memory.current"))
            resident = victim.stat("memory.current")
            if resident > (16 << 20):
                want = demand - cap + ROOM_MARGIN if too_big else FAULT_CHUNK
                self.push_out(victim, min(resident, max(want, ROOM_MARGIN)))
        calm = self.psi_now <= LOW_PRESSURE and self.io_now <= 0.10 and not faulting
        st["low"] = st["low"] + 1 if calm else 0
        if now - st["last_action"] < ACTION_GAP:
            return
        longest_frozen = max(frozen, key=lambda a: now - a.since, default=None)
        if self.desktop:
            out, notes = None, []
            if st["high"] >= self.high_ticks or (longest_frozen and running):
                out, notes = self.pick(running, "memory" if st["high"] >= self.high_ticks else "since")
            rotate_due = longest_frozen and out and now - longest_frozen.since >= min(MAX_SLICE, max(
                SLICE, SLICE_COST_FACTOR * (out.footprint() + longest_frozen.footprint()) / self.swap_bw))
        else:
            out = max(running, key=lambda a: now - a.since, default=None)   # longest running
            notes = None
            rotate_due = (longest_frozen and running and now - longest_frozen.since >= min(MAX_SLICE, max(
                SLICE, SLICE_COST_FACTOR * (out.footprint() + longest_frozen.footprint()) / self.swap_bw)))
        if not running and longest_frozen:
            # Nothing is running (the others finished): always let one back in.
            if self.run_action(lambda a=longest_frozen: self.thaw(a, "idle")):
                st["last_action"], st["low"] = now, 0
        elif st["high"] >= self.high_ticks and (out if self.desktop else len(running) > 1):
            if act:
                self.freeze(out, "overload", notes=notes)   # immediate; push-out runs in background
                st["last_action"] = now
            elif self.once_per("would-freeze", 10):
                self.log("would-freeze", out.name, "overload (no focus info: GNOME extension usm-desktop not active)"
                         + (f" kept-running={' '.join(notes)}" if notes else ""))
            st["high"] = 0
        elif self.desktop and st["high"] >= self.high_ticks and self.once_per("no-candidate", 30):
            self.log("overload-nothing-to-pause", detail=f"kept-running={' '.join(notes) or '-'}")
            st["high"] = 0
        elif (st["low"] >= LOW_TICKS and longest_frozen and longest_frozen.footprint()
              + sum(a.stat("memory.current") for a in running) <= cap
              and not (longest_frozen.reclaimer and longest_frozen.reclaimer.is_alive())):
            if self.run_action(lambda a=longest_frozen: self.thaw(a, "fits")):
                st["last_action"], st["low"] = now, 0
        elif rotate_due and act:
            def rotate(out=out, back=longest_frozen):
                self.freeze(out, "rotate", push=False)
                # Push out just enough of `out` for `back` to fit, then read `back` in.
                need = (sum(a.footprint() for a in self.apps.values() if not a.frozen)
                        + back.footprint()
                        + sum(a.stat("memory.current") for a in self.apps.values()
                              if a.frozen and a is not back) - cap + ROOM_MARGIN)
                if need > 0:
                    self.push_out(out, min(out.stat("memory.current"), need), wait=True)
                self.thaw(back, "rotate")
            if self.run_action(rotate):
                st["last_action"] = now

    # ---- desktop mode ------------------------------------------------------------
    def disk_busy(self):
        """True if the root disk has been slow (avg read latency over DISK_BUSY_MS) for the last 2 s."""
        if not self.disk:
            return False
        f = read_safe(f"/sys/block/{self.disk}/stat").split()
        if len(f) < 4:
            return False
        rio, rms = int(f[0]), int(f[3])
        if self.disk_prev:
            dio, dms = rio - self.disk_prev[0], rms - self.disk_prev[1]
            self.disk_lat = (self.disk_lat + [dms / dio if dio > 0 else 0.0])[-4:]
        self.disk_prev = (rio, rms)
        return len(self.disk_lat) >= 4 and sum(self.disk_lat) / len(self.disk_lat) >= DISK_BUSY_MS

    def why_not(self, app, now):
        """Desktop: None if the app may be paused now, else why not."""
        why = self.eligible(app)
        if why:
            return why
        if now - app.last_front < self.recent:
            return "used-recently"
        if now < app.cooldown_until:
            return "cooldown"
        if app.flt_rate < ACTIVE_FAULTS and app.growth < ACTIVE_GROWTH:
            return "idle"          # pausing an app that does nothing does not relieve anything
        return None

    def pick_busy(self, now):
        """Desktop: the busiest background app that may be paused (most faults, then growth)."""
        notes, cands = [], []
        for a in self.apps.values():
            if a.frozen:
                continue
            why = self.why_not(a, now)
            if why:
                if why not in ("small",):
                    notes.append(f"{a.name}:{why}")
            else:
                cands.append(a)
        cands.sort(key=lambda a: (a.flt_rate, a.growth), reverse=True)
        audio = self.audio_pids() if cands else set()
        for a in cands:
            if audio and set(a.pids()) & audio:
                notes.append(f"{a.name}:sound")
                continue
            return a, notes
        return None, notes

    def pause(self, app, reason, now):
        app.set_frozen(1)
        app.frozen, app.since, app.pause_reason, app.pushed = True, now, reason, False
        self.log("freeze", app.name, f"{reason} mem={app.stat('memory.current') >> 20}MB faults/s={app.flt_rate:.0f} "
                                     f"growth={app.growth / 2**20:.0f}MB/s for at most {self.pause_max:.0f}s")

    def resume(self, app, reason, now, cooldown=None, prefetch=False):
        held = now - app.since
        app.set_frozen(0)
        app.frozen, app.since = False, now
        app.cooldown_until = now + (self.cooldown if cooldown is None else cooldown)
        self.log("thaw", app.name, f"{reason} paused={held:.0f}s swapped={app.stat('memory.swap.current') >> 20}MB")
        if prefetch:
            def work():
                budget = min(app.stat("memory.swap.current"), int(meminfo()["MemAvailable"] * 0.8))
                if budget > (16 << 20):
                    pids = app.pids()
                    for p in pids:
                        prefetch_pid(p, budget // max(len(pids), 1))
            threading.Thread(target=work, daemon=True).start()

    def tick_desktop(self, st):
        now = time.monotonic()
        self.psi_now = self.psi.sample()
        self.io_now = self.io_psi.sample()
        self.sess_now = self.sess_psi.sample() if self.sess_psi else 0.0
        calm_now = self.psi_now < DESK_CALM and self.sess_now < DESK_CALM
        if not calm_now or now - st.get("disc_t", -1e9) >= 2:
            st["disc_t"] = now
            self.discover()
        self.update_focus()
        if now - st.get("prot_t", -1e9) >= 5:
            st["prot_t"] = now
            self.apply_protection()
        if not calm_now or now - st.get("cap_t", -1e9) >= 2:
            st["cap_t"] = now
            self.capacity()
        busy_disk = self.disk_busy()
        for a in self.apps.values():
            a.activity(now)
        front = self.apps.get(self.focus_key)
        if front:
            front.last_front = now
        if self.mode != "full":
            return
        paused = [a for a in self.apps.values() if a.frozen]
        # 1. The app the user switches to runs at once (and gets its memory back in the background).
        if front and front.frozen:
            self.resume(front, "in-front", now, cooldown=self.recent, prefetch=True)
            paused.remove(front)
        # 2. A swap device is being emptied (usm-vram-move): background apps wait meanwhile (no time limit).
        if os.path.exists(PAUSE_FILE):
            if not st.get("moving"):
                self.log("pause", detail=read_safe(PAUSE_FILE).strip())
                st["moving"] = True
            if self.focus_valid:
                for a in self.apps.values():
                    if not a.frozen and self.eligible(a) is None and now - a.last_front >= self.recent:
                        self.pause(a, "swap-move", now)
            return
        if st.get("moving"):
            st["moving"] = False
            self.log("resume")
            for a in paused:
                if a.pause_reason == "swap-move":
                    self.resume(a, "swap-move-done", now, cooldown=0)
            paused = [a for a in paused if a.frozen]
        # 3. Safety brake: the desktop lags while apps are paused, but memory is not the reason.
        lag = read_desktop(self.uid).get("lag_max_ms", "") if self.uid is not None else ""
        if paused and lag.isdigit() and int(lag) >= LAG_BRAKE_MS and self.psi_now < 0.10 and self.sess_now < DESK_CALM:
            for a in paused:
                self.resume(a, f"desktop-lag-{lag}ms", now, cooldown=BRAKE_COOLDOWN)
            return
        # 4. Pauses end: at the time limit, or once the pressure has settled.
        st["calm"] = st.get("calm", 0) + 1 if calm_now else 0
        for a in paused:
            held = now - a.since
            if held >= self.pause_max:
                self.resume(a, f"time-limit {held:.0f}s", now)
            elif st["calm"] >= DESK_CALM_TICKS and held >= PAUSE_MIN:
                self.resume(a, f"settled after {held:.0f}s", now)
            elif (not a.pushed and held >= 3 and not calm_now and meminfo()["MemAvailable"] < (512 << 20)
                  and not self.reclaiming()):
                a.pushed = True       # still tight: hand the kernel some of its memory (bounded, once)
                self.push_out(a, min(a.stat("memory.current"), 512 << 20))
        paused = [a for a in paused if a.frozen]
        # 5. Overload: apps (or the desktop) really wait for memory, for a few seconds.
        high = self.psi_now >= self.trigger or self.sess_now >= DESK_SESS_TRIGGER
        st["high"] = st.get("high", 0) + 1 if high else 0
        if st["high"] < self.high_ticks or now - st.get("last_action", -1e9) < 3 or len(paused) >= MAX_PAUSED:
            return
        st["high"] = 0
        if busy_disk:
            if self.once_per("disk-busy", 60):
                self.log("disk-busy", detail=f"the disk takes {sum(self.disk_lat) / max(len(self.disk_lat), 1):.0f}ms per read: "
                                             "it is the bottleneck - not pausing apps")
            return
        victim, notes = self.pick_busy(now)
        reason = f"overload apps={self.psi_now:.2f} desktop={self.sess_now:.2f}"
        if victim is None:
            if self.once_per("no-candidate", 30):
                self.log("overload-nothing-to-pause", detail=f"{reason} kept-running={' '.join(notes) or '-'}")
            return
        st["last_action"] = now
        if self.focus_valid:
            self.pause(victim, reason, now)
        elif self.once_per("would-freeze", 10):
            self.log("would-freeze", victim.name, f"{reason} (no focus info: GNOME extension usm-desktop not active)")

    def shutdown(self):
        self.stop = True
        if self.apps_high:
            try:
                write(f"{self.apps_root}/memory.high", "max")
            except OSError:
                pass
        for cg, value in self.protect_orig.items():
            try:
                write(f"{cg}/memory.low", value)
            except OSError:
                pass
        for app in self.apps.values():
            if app.frozen:
                app.set_frozen(0)
        try:
            os.unlink(STATE_FILE)
        except OSError:
            pass
        self.log("exit")
        deadline = time.monotonic() + 60
        while self.swap_busy and time.monotonic() < deadline:   # let a running swap step finish first
            time.sleep(0.1)
        self.swap_busy = True
        self.remove_swap(final=True)
        if self.dlog:
            self.dlog.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apps", help="cgroup whose children are the managed apps (or --desktop)")
    ap.add_argument("--desktop", action="store_true", help="manage the logged-in desktop user's apps")
    ap.add_argument("--exclude", default="", help="desktop: never pause apps whose scope name contains one of these (comma list)")
    ap.add_argument("--min-app-mb", type=int, default=256, help="desktop: never pause apps smaller than this")
    ap.add_argument("--protect-desktop", default="1G", help="desktop: RAM kept for the desktop itself (GNOME Shell, sound)")
    ap.add_argument("--high-ticks", type=int, default=0, help="overloaded ticks (0.5 s) before pausing (default 2, desktop 6)")
    ap.add_argument("--trigger", type=float, default=0, help="desktop: share of time apps wait for memory that counts as overload (default 0.30)")
    ap.add_argument("--pause-max", type=float, default=PAUSE_MAX, help="desktop: longest pause, s")
    ap.add_argument("--cooldown", type=float, default=COOLDOWN, help="desktop: no new pause of the same app for this long, s")
    ap.add_argument("--recent", type=float, default=RECENT, help="desktop: apps in front this recently are never paused, s")
    ap.add_argument("--protect", action="append", default=[], help="CGROUP=SIZE to keep in RAM")
    ap.add_argument("--mode", choices=["full", "protect"], default="full",
                    help="protect = only memory protection + swap capacity (no freezing)")
    ap.add_argument("--apps-high", default="auto",
                    help="RAM cap for all managed apps together (auto = available at start - 48 MB, off = none)")
    ap.add_argument("--swap-dir", default="/var/lib/usm-swap")
    ap.add_argument("--log", default="/dev/stdout")
    ap.add_argument("--log-dir", help="daily log files guard-DAY.csv here instead of --log")
    ap.add_argument("--keep-days", type=int, default=30)
    ap.add_argument("--max-mb", type=int, default=1024)
    args = ap.parse_args()
    if not args.apps and not args.desktop:
        ap.error("--apps CGROUP or --desktop")

    threading.stack_size(1 << 20)        # locked memory: small thread stacks
    libc.mlockall(7)                     # MCL_CURRENT | MCL_FUTURE | MCL_ONFAULT: never swapped out
    write("/proc/self/oom_score_adj", -1000)
    os.nice(-10)

    guard = Guard(args)
    signal.signal(signal.SIGTERM, lambda *_: setattr(guard, "stop", True))
    signal.signal(signal.SIGINT, lambda *_: setattr(guard, "stop", True))
    guard.log("start", detail=f"mode={args.mode}{' desktop' if args.desktop else ''}")
    try:
        guard.loop()
    finally:
        guard.shutdown()


if __name__ == "__main__":
    main()
