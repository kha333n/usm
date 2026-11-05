"""Daily log files for USM's guard and logger.

<dir>/<name>-YYYY-MM-DD.<ext>, one per day; finished days are gzipped; files older than keep_days, and
the oldest ones beyond max_mb for the whole directory, are deleted. Writing never blocks the caller:
lines go through a queue to a writer thread (a disk busy with swap I/O must not stall the sampler).
"""
import ctypes
import glob
import gzip
import os
import queue
import shutil
import threading
import time

_libc = ctypes.CDLL(None, use_errno=True)
_SYS_IOPRIO_SET, _IOPRIO_CLASS_IDLE = 251, 3


def _idle_thread():
    """Lowest CPU and disk priority for the calling thread (Linux: per thread)."""
    try:
        os.nice(19)
        _libc.syscall(ctypes.c_long(_SYS_IOPRIO_SET), ctypes.c_long(1), ctypes.c_long(0),
                      ctypes.c_long(_IOPRIO_CLASS_IDLE << 13))
    except OSError:
        pass


class DailyLog:
    def __init__(self, directory, name, ext="csv", header=None, keep_days=30, max_mb=1024):
        self.dir, self.name, self.ext, self.header = directory, name, ext, header
        self.keep_days, self.max_bytes = keep_days, max_mb << 20
        self.q = queue.Queue(maxsize=200000)
        self.dropped = 0
        os.makedirs(directory, mode=0o755, exist_ok=True)
        self.thread = threading.Thread(target=self._run, daemon=True, name=f"log-{name}")
        self.thread.start()

    def write(self, line):
        try:
            self.q.put_nowait((time.time(), line))
        except queue.Full:
            self.dropped += 1

    def close(self, timeout=5):
        self.q.put(None)
        self.thread.join(timeout)

    def path(self, day):
        return os.path.join(self.dir, f"{self.name}-{day}.{self.ext}")

    def _run(self):
        f, day, done = None, None, False
        while not done:
            items = [self.q.get()]
            while True:
                try:
                    items.append(self.q.get_nowait())
                except queue.Empty:
                    break
            for item in items:
                if item is None:
                    done = True
                    continue
                t, line = item
                d = time.strftime("%Y-%m-%d", time.localtime(t))
                try:
                    if d != day:
                        if f:
                            f.close()
                        day, p = d, self.path(d)
                        if self.header and os.path.exists(p) and os.path.getsize(p) > 0:
                            with open(p) as fh:
                                if fh.readline().rstrip("\n") != self.header:   # columns changed: new file
                                    p = p[:-len(self.ext) - 1] + f"-v2.{self.ext}"
                        new = not os.path.exists(p) or os.path.getsize(p) == 0
                        f = open(p, "a")
                        if new and self.header:
                            f.write(self.header + "\n")
                        threading.Thread(target=self._housekeep, daemon=True).start()
                    f.write(line + "\n")
                except OSError:
                    f, day = None, None     # disk full etc.: try again with the next line
            try:
                if f:
                    f.flush()
            except OSError:
                f, day = None, None
        if f:
            f.close()

    def _housekeep(self):
        """gzip finished days of this log; delete old files of the whole directory."""
        _idle_thread()
        today = time.strftime("%Y-%m-%d")
        for p in glob.glob(os.path.join(self.dir, f"{self.name}-*.{self.ext}")):
            if today in os.path.basename(p):       # today's files (also NAME-DAY-v2) are still being written
                continue
            try:
                with open(p, "rb") as src, gzip.open(p + ".gz.tmp", "wb", compresslevel=6) as dst:
                    shutil.copyfileobj(src, dst)
                os.rename(p + ".gz.tmp", p + ".gz")
                os.unlink(p)
            except OSError:
                pass
        files = []
        for p in glob.glob(os.path.join(self.dir, "*")):
            try:
                st = os.stat(p)
            except OSError:
                continue
            if os.path.isfile(p):
                files.append((st.st_mtime, st.st_size, p))
        files.sort()
        total = sum(s for _, s, _ in files)
        cutoff = time.time() - self.keep_days * 86400
        for mtime, size, p in files:
            if (mtime < cutoff or total > self.max_bytes) and p.endswith(".gz"):
                try:
                    os.unlink(p)
                    total -= size
                except OSError:
                    pass


def sd_notify(msg):
    """Tell systemd something (READY=1, WATCHDOG=1) if run as a Type=notify service."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    import socket
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.send(msg.encode())
    except OSError:
        pass


def desktop_user():
    """(uid, user@UID.service cgroup) of the desktop user: the one whose GNOME helper reports,
    else the user with the most memory in apps. None if nobody is logged in."""
    best = None
    for app in glob.glob("/sys/fs/cgroup/user.slice/user-*.slice/user@*.service/app.slice"):
        svc = os.path.dirname(app)
        try:
            uid = int(os.path.basename(svc)[5:-8])
            mem = int(open(f"{app}/memory.current").read())
        except (OSError, ValueError):
            continue
        if not 1000 <= uid < 60000:      # system and login-screen users (gdm greeter: 60578) have no desktop apps
            continue
        helper = os.path.exists(f"/run/user/{uid}/usm-desktop")
        key = (helper, mem)
        if best is None or key > best[0]:
            best = (key, uid, svc)
    return (best[1], best[2]) if best else None


def read_desktop(uid):
    """What the GNOME helper extension reports (focus, idle, shell lag), plus its age in seconds."""
    p = f"/run/user/{uid}/usm-desktop"
    try:
        age = time.time() - os.stat(p).st_mtime
        with open(p) as f:
            d = dict(line.strip().split("=", 1) for line in f if "=" in line)
        d["age"] = age
        return d
    except (OSError, ValueError):
        return {}


def app_name(scope):
    """'app-gnome-google\\x2dchrome-322199.scope' -> 'google-chrome'."""
    n = scope
    for pre in ("app-gnome-", "app-flatpak-", "app-"):
        if n.startswith(pre):
            n = n[len(pre):]
            break
    for suf in (".scope", ".service", ".slice"):
        if n.endswith(suf):
            n = n[:-len(suf)]
    head, _, tail = n.rpartition("-")
    if head and tail.isdigit():
        n = head
    return n.replace("\\x2d", "-").replace("\\x20", " ")
