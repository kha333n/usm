"""usm dash: live view of RAM, swap and USM, with charts of the last minutes (default 5).

Swap latency comes from the kernel's block-device counters (/sys/block/<dev>/stat): the average time from the
kernel queueing a read or write until it completes, which is what a program waits for a page that has to come
back from swap. A swap file has the latency of the disk that holds it, so for a file that number covers all of
the disk's I/O, not only swap. The counters count whole milliseconds, so a handful of fast requests (the VRAM
tier answers in tens of microseconds) is below what they can show: the dash then looks further back (up to
15 s), and if that is still not enough it shows an upper bound ("<").

The charts start with the logger's samples of the last minutes (when the logger runs), so they are full at
once; from then on the dash samples every second itself. Read-only, no root needed.

Sampler holds the data and knows nothing about the screen, so a graphical front end can use it too.
"""
import collections
import glob
import gzip
import io
import json
import math
import os
import select
import shutil
import sys
import time

from usm_log import app_name, desktop_user, read_desktop

PAGE_MB = os.sysconf("SC_PAGE_SIZE") / 2**20
STATS = "/run/usm-vramd.stats"
LAT_GOOD_MS = 10        # request time one latency figure needs: the counters count whole ms -> at most 10 % off
LAT_LOOK_BACK_S = 15    # how far back a latency figure may look for that much
FEW = "few"             # chart value: requests there, but too few and too fast to measure
GUT = 8                 # chart scale column


CONF = "/etc/usm/usm.conf"
GUARD_STATE = "/run/usm-guard.state"


def conf():
    out = {}
    try:
        for line in open(CONF):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


LOG_DIR = conf().get("LOG_DIR", "/var/log/usm")


def guard_state():
    try:
        st = json.loads(read(GUARD_STATE) or "{}")
    except ValueError:
        return {}
    return st if st and time.time() - st.get("t", 0) < 10 else {}


def swap_label(name):
    if name.startswith("/dev/ublkb"):
        return "VRAM tier"
    if name.startswith("/dev/zram"):
        return "zram"
    if "/usm-swap/standby" in name:
        return "VRAM tier standby"
    if "/usm-swap/" in name:
        return "guard temp file"
    return name


# ---------------------------------------------------------------------------------- logs
def log_files(name, t0, t1):
    """Daily files of `name` that can hold [t0, t1], oldest first."""
    out = []
    for p in glob.glob(f"{LOG_DIR}/{name}-*"):
        day = os.path.basename(p)[len(name) + 1:len(name) + 11]
        try:
            d0 = time.mktime(time.strptime(day, "%Y-%m-%d"))
        except ValueError:
            continue
        if d0 + 86400 >= t0 and d0 <= t1:
            out.append((day, p))
    return [p for _, p in sorted(out)]


def open_text(p):
    return io.TextIOWrapper(gzip.open(p), errors="replace") if p.endswith(".gz") else open(p, errors="replace")


def events(t0, t1):
    out = []
    for p in log_files("events", t0, t1):
        try:
            with open_text(p) as f:
                for line in f:
                    try:
                        t = time.mktime(time.strptime(line[:19], "%Y-%m-%d %H:%M:%S"))
                    except ValueError:
                        continue
                    if t0 <= t <= t1:
                        out.append((t, line.rstrip("\n")))
        except (OSError, EOFError, gzip.BadGzipFile):
            continue
    return out


def tail_events(n):
    return [line for _, line in events(time.time() - 2 * 86400, time.time())[-n:]]


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
        m[k] = int(v.split()[0]) >> 10
    return m


def vmstat():
    out = {}
    for line in read("/proc/vmstat").splitlines():
        k, v = line.split()
        out[k] = int(v)
    return out


def psi_total(path):
    """Time (us, since boot) in which some / all non-idle tasks waited: (some, full)."""
    out = {"some": 0, "full": 0}
    for line in read(path).splitlines():
        parts = line.split()
        out[parts[0]] = int(parts[-1].split("=")[1])
    return out["some"], out["full"]


def swaps():
    """[(name, kind, size_mb, used_mb, prio)]"""
    out = []
    for line in read("/proc/swaps").splitlines()[1:]:
        name, kind, size, used, prio = line.split()
        out.append((name.replace("\\040", " "), kind, int(size) >> 10, int(used) >> 10, int(prio)))
    return out


def device_of(name, kind):
    """Block device whose counters show a swap's I/O: the device itself, or the whole disk under a swap file or
    partition (as the logger does, so its samples fit)."""
    try:
        st = os.stat(name)
        dev = st.st_rdev if kind == "partition" else st.st_dev
        sysdev = os.path.realpath(f"/sys/dev/block/{os.major(dev)}:{os.minor(dev)}")
        if os.path.exists(f"{sysdev}/partition"):
            sysdev = os.path.dirname(sysdev)
        return os.path.basename(sysdev)
    except OSError:
        return None


def blkstat(dev):
    """(reads, read ms, writes, write ms, sectors read, sectors written) completed since boot."""
    f = read(f"/sys/block/{dev}/stat").split()
    if len(f) < 8:
        return None
    return int(f[0]), int(f[3]), int(f[4]), int(f[7]), int(f[2]), int(f[6])


def tier_stats():
    return dict(kv.split("=", 1) for kv in read(STATS).split() if "=" in kv)


# ---------------------------------------------------------------------------------- data
class Sampler:
    """One record per second: RAM in use, paging, waiting for memory and every swap device's requests, for the
    last span_s seconds."""

    def __init__(self, span_s=300):
        self.span = span_s
        self.hist = collections.deque()
        self.prev = None
        self.started = time.time()

    def _raw(self):
        devs = {}
        for name, kind, _size, _used, _prio in swaps():
            d = device_of(name, kind)
            if d and d not in devs:
                devs[d] = blkstat(d)
        return {"t": time.time(), "mono": time.monotonic(), "vs": vmstat(),
                "psi": psi_total("/proc/pressure/memory"), "devs": devs}

    def _add(self, rec):
        self.hist.append(rec)
        while self.hist and self.hist[0]["t"] < rec["t"] - self.span - 2:
            self.hist.popleft()

    def step(self):
        """Take a sample; returns the new record (None the first time: nothing to compare with yet)."""
        cur, p = self._raw(), self.prev
        self.prev = cur
        if p is None:
            return None
        dt = max(cur["mono"] - p["mono"], 1e-3)

        def d(k):
            return cur["vs"].get(k, 0) - p["vs"].get(k, 0)
        mi = meminfo()
        rec = {"t": cur["t"], "dt": dt, "used_mb": mi["MemTotal"] - mi["MemAvailable"],
               "swin": (d("pswpin") + d("zswpin")) * PAGE_MB / dt, "swout": (d("pswpout") + d("zswpout")) * PAGE_MB / dt,
               "pin": d("pswpin") * PAGE_MB / dt, "pout": d("pswpout") * PAGE_MB / dt,
               "majflt": d("pgmajfault") / dt, "zin": d("zswpin") / dt, "zout": d("zswpout") / dt,
               "stall": min(100.0, (cur["psi"][0] - p["psi"][0]) / 1e4 / dt),
               "stall_all": min(100.0, (cur["psi"][1] - p["psi"][1]) / 1e4 / dt), "dev": {}}
        for dev, b in cur["devs"].items():
            pb = p["devs"].get(dev)
            if b and pb:
                rec["dev"][dev] = tuple(x - y for x, y in zip(b, pb))
        self._add(rec)
        return rec

    def prefill(self, log_dir):
        """The logger's samples of the last span seconds (today's file), so the charts are full at once.
        Its VRAM and disk columns go to the devices the dash finds now. Returns the number of records."""
        path = os.path.join(log_dir, f"samples-{time.strftime('%Y-%m-%d')}.csv")
        try:
            with open(path, "rb") as f:
                head = f.readline()
                size = f.seek(0, 2)
                start = max(len(head), size - (self.span + 60) * 600)
                f.seek(start)
                lines = f.read().decode(errors="replace").splitlines()
        except OSError:
            return 0
        if start > len(head):
            lines = lines[1:]                       # cut in the middle of a line
        cols = head.decode(errors="replace").strip().split(",")
        vdev = ddev = None
        best = None
        for name, kind, _size, _used, prio in swaps():   # as the logger picks them
            if name.startswith("/dev/ublkb"):
                vdev = os.path.basename(name)
            elif best is None or prio > best:
                best, ddev = prio, device_of(name, kind)
        total = meminfo()["MemTotal"]
        now, prev_t, n = time.time(), None, 0
        for line in lines:
            vals = line.split(",")
            if len(vals) != len(cols):
                continue
            r = dict(zip(cols, vals))

            def num(k):
                try:
                    return float(r[k])
                except (KeyError, ValueError):
                    return None
            t = num("t")
            if t is None or t < now - self.span or t > now:
                prev_t = t
                continue
            dt = t - prev_t if prev_t and 0 < t - prev_t < 5 else 1.0
            prev_t = t
            rec = {"t": t, "dt": dt, "used_mb": total - (num("avail_mb") or 0),
                   "swin": ((num("pswpin") or 0) + (num("zswpin") or 0)) * PAGE_MB / dt,
                   "swout": ((num("pswpout") or 0) + (num("zswpout") or 0)) * PAGE_MB / dt,
                   "pin": (num("pswpin") or 0) * PAGE_MB / dt, "pout": (num("pswpout") or 0) * PAGE_MB / dt,
                   "majflt": (num("majflt") or 0) / dt, "zin": (num("zswpin") or 0) / dt,
                   "zout": (num("zswpout") or 0) / dt, "stall": min(100.0, (num("mem_some_ms") or 0) / 10 / dt),
                   "stall_all": min(100.0, (num("mem_full_ms") or 0) / 10 / dt), "dev": {}}
            for pre, dev in (("vram", vdev), ("disk", ddev)):
                rps, wps = num(f"{pre}_rps"), num(f"{pre}_wps")
                if dev and rps is not None and wps is not None:
                    rio, wio = rps * dt, wps * dt
                    rec["dev"][dev] = (rio, (num(f"{pre}_rlat_us") or 0) * rio / 1000,
                                       wio, (num(f"{pre}_wlat_us") or 0) * wio / 1000,
                                       (num(f"{pre}_rmbps") or 0) * dt * 2048, (num(f"{pre}_wmbps") or 0) * dt * 2048)
            self._add(rec)
            n += 1
        return n

    def latency(self, dev, writes=False, end=None, t_from=None):
        """Average request time (us) of dev up to record `end` (default: the last one), from t_from (default:
        that record only) back - and further back, up to LAT_LOOK_BACK_S, while the requests add up to less
        than LAT_GOOD_MS. Returns (us, mark): mark "" exact enough, "~" rough, "<" an upper bound;
        (None, "") if there were no requests."""
        recs = self.hist
        if not recs:
            return None, ""
        end = len(recs) - 1 if end is None else end
        k = 2 if writes else 0
        t_end = recs[end]["t"]
        t_from = t_end - 0.5 if t_from is None else t_from
        io = ms = 0.0
        i = end
        while i >= 0:
            r = recs[i]
            if r["t"] < t_from and (ms >= LAT_GOOD_MS or t_end - r["t"] > LAT_LOOK_BACK_S):
                break
            dd = r["dev"].get(dev)
            if dd:
                io += dd[k]
                ms += dd[k + 1]
            i -= 1
        if io < 0.5:
            return None, ""
        if ms >= LAT_GOOD_MS:
            return ms * 1000 / io, ""
        if ms >= 1:
            return ms * 1000 / io, "~"
        return (int(ms) + 1) * 1000 / io, "<"

    def latency_at(self, i, dev, writes=False):
        """latency() of record i alone (with its look-back), kept in the record: a record's past doesn't change."""
        r = self.hist[i]
        cache = r.setdefault("lat", {})
        key = (dev, writes)
        if key not in cache:
            cache[key] = self.latency(dev, writes, end=i)
        return cache[key]

    @staticmethod
    def tiers(rec, vdev):
        """MiB/s each way per tier: {"zswap": (in, out), "vram": (in, out), "disk": (in, out)}. The VRAM tier's
        device carries nothing but swap; disk swap is the rest of the swap I/O (pswpin/pswpout)."""
        dt = rec.get("dt") or 1.0
        v = rec["dev"].get(vdev) if vdev else None
        vin = v[4] / 2048 / dt if v and len(v) > 5 else 0.0
        vout = v[5] / 2048 / dt if v and len(v) > 5 else 0.0
        return {"zswap": (rec["zin"] * PAGE_MB, rec["zout"] * PAGE_MB), "vram": (vin, vout),
                "disk": (max(0.0, rec.get("pin", 0.0) - vin), max(0.0, rec.get("pout", 0.0) - vout))}

    def buckets(self, n, now=None):
        """Indexes of the records in each of n equal time slices of the last span seconds, oldest first."""
        now = now or time.time()
        w = self.span / n
        out = [[] for _ in range(n)]
        for i, r in enumerate(self.hist):
            j = int((r["t"] - (now - self.span)) / w)
            if 0 <= j < n:
                out[j].append(i)
        return out

    def series(self, key, n, now=None):
        """Peak per slice of a per-second value; None where there is no record."""
        return [max(self.hist[i][key] for i in b) if b else None for b in self.buckets(n, now)]

    def latency_series(self, dev, n, now=None, writes=False):
        out = []
        w = self.span / n
        now = now or time.time()
        for j, b in enumerate(self.buckets(n, now)):
            if not b:
                out.append(None)
                continue
            us, mark = self.latency(dev, writes, end=b[-1], t_from=now - self.span + j * w)
            out.append(None if us is None else FEW if mark == "<" else us)
        return out


# ---------------------------------------------------------------------------------- text helpers
class Style:
    def __init__(self, color, unicode):
        self.color, self.uni = color, unicode
        self.blocks = " ▁▂▃▄▅▆▇█" if unicode else " .:-=+*#@"
        self.dot, self.tick, self.line = ("·", "┤", "│") if unicode else (".", "|", "|")

    def c(self, code, s):
        return f"\033[{code}m{s}\033[0m" if self.color and s else s


def fit(s, w):
    return s if len(s) <= w else s[:max(0, w - 1)] + ">"


def gib(mb):
    return f"{mb / 1024:.1f}"


def fmt_lat(us, mark=""):
    if us is None:
        return "-"
    if us < 1000:
        return f"{mark}{us:.0f} us"
    if us < 100000:
        return f"{mark}{us / 1000:.1f} ms"
    return f"{mark}{us / 1000:.0f} ms"


def fmt_rate(v):
    return "-" if v is None else f"{v:.0f}"


def fmt_num(v):
    if v is None:
        return "-"
    return f"{v:.0f}" if v >= 100 or v == 0 else f"{v:.1f}" if v >= 1 else f"{v:.2f}"


def nice(v):
    """Round a chart's top up to 1, 2 or 5 x 10^k."""
    if not v or v <= 0:
        return 1.0
    e = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 5, 10):
        if v <= m * e * 1.0001:
            return m * e
    return 10 * e


def bar(frac, width=30):
    frac = max(0.0, min(1.0, frac))
    n = int(round(frac * width))
    return "[" + "#" * n + "." * (width - n) + "]"


def chart_rows(vals, h, top, st):
    """Area chart, h rows high, one character per value; values above top are cut off."""
    rows = []
    for row in range(h - 1, -1, -1):
        s = []
        for v in vals:
            if v is None:
                s.append(" ")
            elif v == FEW:
                s.append(st.dot if row == 0 else " ")
            else:
                e = v / top * h * 8 if top else 0
                if v > 0:
                    e = max(e, 1)
                f = int(round(min(e, h * 8))) - row * 8
                s.append(st.blocks[max(0, min(8, f))])
        rows.append("".join(s))
    return rows


# ---------------------------------------------------------------------------------- screen
class Dash:
    def __init__(self, sampler, st, label, guard_state, tail_events, version=""):
        self.s, self.st, self.label = sampler, st, label
        self.guard_state, self.tail_events, self.version = guard_state, tail_events, version
        self.show = {"charts": True, "apps": True, "events": True}

    # -- the parts
    def head(self, W):
        s = self.s
        last = s.hist[-1] if s.hist else {}
        mi = meminfo()
        used = mi["MemTotal"] - mi["MemAvailable"]
        mins = s.span // 60
        out = [fit(f"USM {self.version}  {time.strftime('%H:%M:%S')}   charts: last {mins} min"
                   f"   keys: q quit, c charts, a apps, e events", W)]
        line = (f"RAM     {bar(used / mi['MemTotal'], 30)} {gib(used)} of {gib(mi['MemTotal'])} GiB in use, "
                f"{gib(mi['MemAvailable'])} available")
        out.append(fit(line, W))
        ts = tier_stats()
        if ts:
            if ts.get("switched_off") == "1":
                state = "VRAM switched off (data on disk)"
            elif ts.get("vram_held") == "1":
                state = "VRAM held"
            else:
                state = "VRAM given back"
            line = (f"TIER    {state}: {ts.get('in_vram_mb', '?')} MiB in VRAM, {ts.get('on_disk_mb', '?')} MiB on disk, "
                    f"{ts.get('live_mb', '?')} MiB of data")
            if ts.get("comp", "0") != "0":
                line += f" compressed to {ts.get('comp_pct', '?')}%"
            if int(ts.get("given_back_mb", "0") or 0):
                line += f"; {ts['given_back_mb']} MiB of VRAM given back to other programs"
            if ts.get("refused", "0") != "0":
                line += f"; {ts['refused']} writes REFUSED"
            if ts.get("gpu_free_mb", "-1") != "-1":
                line += f"; GPU: {ts['gpu_free_mb']} MiB free"
            out.append(fit(line, W))
        if last:
            zs = (f" (zswap: in {fmt_num(last['zin'] * PAGE_MB)}, out {fmt_num(last['zout'] * PAGE_MB)})"
                  if last["zin"] or last["zout"] else "")
            out.append(fit(f"PAGING  swap-in {fmt_num(last['swin'])} MiB/s   swap-out {fmt_num(last['swout'])} MiB/s{zs}"
                           f"   major faults {last['majflt']:.0f}/s   waiting for memory {last['stall']:.0f}%"
                           f" (all at once {last['stall_all']:.0f}%)", W))
        u = desktop_user()
        dk = read_desktop(u[0]) if u else {}
        gs = self.guard_state()
        line = ""
        if dk:
            line = (f"DESK    in front: {dk.get('focus_app', '?')}   desktop lag {dk.get('lag_max_ms', '?')} ms"
                    f"   idle {int(dk.get('idle_ms') or 0) // 1000} s   ")
        line += f"GUARD {gs.get('mode', 'not running')}"
        if gs.get("frozen_apps"):
            line += f"   PAUSED: {', '.join(gs['frozen_apps'])}"
        out.append(fit(line, W))
        return out

    def swap_table(self, W):
        s, mi = self.s, meminfo()
        last = s.hist[-1] if s.hist else None
        out = [fit(f"{'SWAP':20s} {'USED/SIZE GiB':>13s} {'READ/s':>6s} {'LATENCY':>9s} {'WRITE/s':>7s} "
                   f"{'LATENCY':>9s}  DEVICE", W)]
        zswap = mi.get("Zswap") is not None and read("/sys/module/zswap/parameters/enabled").strip() == "Y"
        if zswap:
            pool = int(read("/sys/module/zswap/parameters/max_pool_percent") or 20) * mi["MemTotal"] / 100
            zin = zout = None
            if last:
                zin, zout = last["zin"], last["zout"]
            used = f"{gib(mi.get('Zswapped', 0))} in {gib(mi.get('Zswap', 0))}"
            out.append(fit(f"{'zswap (RAM, first)':20s} {used:>13s} {fmt_rate(zin):>6s} {'in RAM':>9s} "
                           f"{fmt_rate(zout):>7s} {'in RAM':>9s}  pool {gib(pool)} GiB", W))
        seen = {}
        for name, kind, size, used, prio in sorted(swaps(), key=lambda x: -x[4]):
            dev = device_of(name, kind)
            lab = self.label(name)
            lab = fit(f"{lab} p{prio}" if len(lab) + len(str(prio)) < 18 else lab, 20)
            us = f"{gib(used)} / {gib(size)}"
            if dev in seen:
                out.append(fit(f"{lab:20s} {us:>13s}   same disk as {seen[dev]}", W))
                continue
            seen[dev] = self.label(name)
            dd = last["dev"].get(dev) if last else None
            rps = fmt_rate(dd[0]) if dd else "-"
            wps = fmt_rate(dd[2]) if dd else "-"
            rl = fmt_lat(*s.latency(dev))
            wl = fmt_lat(*s.latency(dev, writes=True))
            out.append(fit(f"{lab:20s} {us:>13s} {rps:>6s} {rl:>9s} {wps:>7s} {wl:>9s}  {dev or '?'}", W))
        if zswap:
            out.append(fit("USED counts the pages zswap keeps in RAM for that swap too", W))
        out.append(fit("latency: average per request; a disk's covers all its I/O, not only swap; ~ rough; "
                       "< at most", W))
        return out

    def chart_specs(self, n):
        s = self.s
        now = time.time()
        specs = []

        def add(title, vals, color, unit, top=None, fmt=fmt_num, now_text=None):
            pts = [v for v in vals if v not in (None, FEW)]
            peak = max(pts) if pts else 0
            specs.append({"title": title, "vals": vals, "color": color, "top": top or nice(peak), "fmt": fmt,
                          "unit": unit, "now": now_text, "peak": peak})
        add("swap-in (pages back from zswap and swap)", s.series("swin", n, now), "32", "MiB/s")
        add("swap-out (pages to zswap and swap)", s.series("swout", n, now), "33", "MiB/s")
        devs = []
        for name, kind, _size, _used, _prio in sorted(swaps(), key=lambda x: -x[4]):
            dev = device_of(name, kind)
            if dev and dev not in [d for d, _ in devs]:
                devs.append((dev, self.label(name) if name.startswith(("/dev/ublkb", "/dev/zram")) else f"disk {dev}"))
        for dev, lab in devs:
            us, mark = s.latency(dev)
            add(f"read latency: {lab}", s.latency_series(dev, n, now), "35" if dev.startswith("ublkb") else "36",
                "", fmt=fmt_lat, now_text=fmt_lat(us, mark))
        mi = meminfo()
        add("RAM in use", s.series("used_mb", n, now), "34", "GiB", top=mi["MemTotal"],
            fmt=lambda v: gib(v))
        add("waiting for memory (% of each second, some apps)", s.series("stall", n, now), "31", "%")
        for sp in specs:
            if sp["unit"] == "%":
                sp["top"] = min(100, max(10, sp["top"]))
        return specs

    def chart_block(self, sp, cw, h):
        """Lines of one chart, (plain, coloured): title + h rows, each cw characters wide."""
        st, fmt = self.st, sp["fmt"]
        last = next((v for v in reversed(sp["vals"]) if v not in (None, FEW)), None)
        now_text = sp["now"] if sp["now"] is not None else fmt(last) if last is not None else "-"
        unit = f" {sp['unit']}" if sp["unit"] else ""
        if sp["now"] is not None and all(v is None for v in sp["vals"]):
            title = fit(f"{sp['title']}: no reads in the last {self.s.span / 60:g} min", cw)
        else:
            title = fit(f"{sp['title']}: now {now_text}{unit}, peak {fmt(sp['peak'])}{unit}", cw)
        out = [(title, st.c(sp["color"] + ";1", title) + " " * (cw - len(title)))]
        rows = chart_rows(sp["vals"], h, sp["top"], st)
        empty = all(v is None for v in sp["vals"])
        for i, row in enumerate(rows):
            lab = "" if empty else fmt(sp["top"]) if i == 0 else (fmt(0) if i == h - 1 and h > 2 else "")
            g = f"{lab:>{GUT - 1}}"[-(GUT - 1):] + (st.tick if lab else st.line)
            plain = g + row
            out.append((plain, g + st.c(sp["color"], row) + " " * (cw - len(plain))))
        return out

    def axis(self, n):
        mins = self.s.span / 60
        a = [" "] * n
        lab = "now"
        a[n - len(lab):] = lab
        step = 1 if mins <= 10 else 5 if mins <= 30 else 10
        m = step
        while m <= mins:
            pos = n - 1 - int(round(m * 60 / self.s.span * n))
            text = f"-{m:g}m"
            if pos >= 0 and all(c == " " for c in a[max(0, pos - 1):pos + len(text) + 1]) and pos + len(text) <= n:
                a[pos:pos + len(text)] = text
            m += step
        return " " * GUT + "".join(a)

    def apps(self, rows, W):
        u = desktop_user()
        if not u or rows < 2:
            return []
        lst = []
        for c in os.listdir(f"{u[1]}/app.slice") if os.path.isdir(f"{u[1]}/app.slice") else []:
            if not c.endswith(".scope"):
                continue
            p = f"{u[1]}/app.slice/{c}"
            cur = int(read(f"{p}/memory.current", "0") or 0) >> 20
            sw = int(read(f"{p}/memory.swap.current", "0") or 0) >> 20
            fr = read(f"{p}/cgroup.freeze").strip() == "1"
            lst.append((cur + sw, cur, sw, fr, app_name(c)))
        lst.sort(reverse=True)
        out = [fit(f"{'APP':34s} {'RAM MiB':>8s} {'SWAP MiB':>9s}", W)]
        for _tot, cur, sw, fr, name in lst[:rows - 1]:
            out.append(fit(f"{name[:34]:34s} {cur:8d} {sw:9d}  {'PAUSED' if fr else ''}", W))
        return out

    # -- the whole screen
    def render(self, W, H):
        out = self.head(W) + [""] + self.swap_table(W)
        rest = H - len(out)
        if self.show["charts"] and rest > 4 and self.s.hist is not None:
            ncol = 2 if W >= 150 else 1
            cw = (W - 3 * (ncol - 1)) // ncol
            n = max(10, min(cw - GUT, self.s.span))
            specs = self.chart_specs(n)
            others = self.show["apps"] or self.show["events"]
            room = rest - 1 - (min(12, rest // 3) if others else 0)

            def need(k, h):
                return -(-k // ncol) * (1 + h) + 1
            h = 4
            while h > 1 and need(len(specs), h) > room:
                h -= 1
            while specs and need(len(specs), h) > room:     # too little room: empty charts go first
                empty = [i for i, sp in enumerate(specs) if all(v is None for v in sp["vals"])]
                specs.pop(empty[-1] if empty else -1)
            k = len(specs)
            if k:
                out.append("")
                blocks = [self.chart_block(sp, cw, h) for sp in specs]
                for i in range(0, k, ncol):
                    row = blocks[i:i + ncol]
                    for j in range(1 + h):
                        out.append("   ".join(b[j][1] for b in row))
                ax = self.axis(n)
                out.append(("   ".join([ax + " " * (cw - len(ax))] * ncol)).rstrip())
        rest = H - len(out)
        if self.show["apps"] and rest > 3:
            ev_room = 4 if self.show["events"] and rest > 10 else 0
            out += [""] + self.apps(rest - 1 - ev_room, W)
        rest = H - len(out)
        if self.show["events"] and rest > 2:
            ev = self.tail_events(rest - 2)
            if ev:
                out += ["", "EVENTS"] + [fit(f"  {e}", W) for e in ev]
        return out[:H]


def run(minutes=5, version=""):
    s = Sampler(int(minutes * 60))
    s.prefill(LOG_DIR)
    s.step()
    fd = sys.stdin.fileno()
    live = os.isatty(fd) and sys.stdout.isatty()
    uni = "utf" in (sys.stdout.encoding or "").lower()
    color = live and "NO_COLOR" not in os.environ and os.environ.get("TERM", "") != "dumb"
    d = Dash(s, Style(color, uni), swap_label, guard_state, tail_events, version)
    if not live:                                     # piped: one picture after one second of sampling
        time.sleep(1)
        s.step()
        W, H = shutil.get_terminal_size((100, 60))
        print("\n".join(line.rstrip() for line in d.render(W, H)))
        return
    import termios
    import tty
    old = termios.tcgetattr(fd)
    out = sys.stdout

    def draw():
        W, H = shutil.get_terminal_size((100, 40))
        lines = d.render(W - 1, H)                   # a full last column makes some terminals wrap
        out.write("\033[H" + "".join(f"{line}\033[K\n" for line in lines[:-1]) + lines[-1] + "\033[K\033[J")
        out.flush()
    try:
        tty.setcbreak(fd)
        out.write("\033[?1049h\033[?25l")            # own screen, no cursor
        draw()
        nxt = time.monotonic() + 1
        while True:
            wait = nxt - time.monotonic()
            if wait <= 0:
                s.step()
                nxt = max(nxt + 1, time.monotonic() + 0.5)
                draw()
                continue
            r, _, _ = select.select([sys.stdin], [], [], wait)
            if not r:
                continue
            key = os.read(fd, 32).decode(errors="ignore")[-1:].lower()
            if key == "q":
                break
            if key in ("c", "a", "e"):
                part = {"c": "charts", "a": "apps", "e": "events"}[key]
                d.show[part] = not d.show[part]
                draw()
    except KeyboardInterrupt:
        pass
    finally:
        out.write("\033[?25h\033[?1049l")
        out.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
