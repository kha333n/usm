#!/usr/bin/env python3
"""usm check: what of USM works on this machine, and why not. Changes nothing; root not needed.

  usm_check.py            readable list (what `usm check` shows)
  usm_check.py --shell    KEY=VALUE lines for install.sh (CORE_OK, TIER_OK, TIER_WHY, GNOME_VERSION, ...)
"""
import glob
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
OK, WARN, FAIL, INFO = "ok", "warn", "FAIL", "info"


def run(*cmd, timeout=20):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""


def read(p, default=""):
    try:
        return open(p).read().strip()
    except OSError:
        return default


def daemon():
    for p in (os.path.join(HERE, "usm-vramd"), os.path.join(HERE, "..", "vram", "usm-vramd")):
        if os.access(p, os.X_OK):
            return os.path.realpath(p)
    return None


def kernel_tuple():
    m = re.match(r"(\d+)\.(\d+)", platform.release())
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def distro():
    d = {}
    for line in read("/etc/os-release").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            d[k] = v.strip('"')
    return d.get("PRETTY_NAME", "unknown Linux")


def gnome_version():
    if not shutil.which("gnome-shell"):
        return None
    rc, out = run("gnome-shell", "--version", timeout=10)
    m = re.search(r"(\d+)(?:\.\d+)*", out) if rc == 0 else None
    return int(m.group(1)) if m else None


def nvidia_display():
    if not shutil.which("nvidia-smi"):
        return ""
    rc, out = run("nvidia-smi", "--query-gpu=display_active", "--format=csv,noheader", timeout=20)
    return out.splitlines()[0].strip() if rc == 0 and out else ""


def probe():
    d = daemon()
    if not d:
        return {"cuda": "no", "error": "usm-vramd is not built (make)"}
    rc, out = run(d, "probe", timeout=60)
    vals = dict(re.findall(r'(\w+)=("[^"]*"|\S+)', out))
    vals = {k: v.strip('"') for k, v in vals.items()}
    if not vals:
        vals = {"cuda": "no", "error": f"usm-vramd probe failed ({rc})"}
    return vals


def checks():
    """[(group, status, name, detail)], plus a summary dict."""
    res, s = [], {}
    add = lambda g, st, n, d="": res.append((g, st, n, d))

    # --- needed by every part ---
    add("System", INFO, distro(), f"kernel {platform.release()}, {platform.machine()}")
    systemd = os.path.isdir("/run/systemd/system")
    add("System", OK if systemd else FAIL, "systemd", "" if systemd else "USM needs systemd as the init system")
    cg2 = os.path.exists("/sys/fs/cgroup/cgroup.controllers")
    add("System", OK if cg2 else FAIL, "cgroup v2", "" if cg2 else "needs the unified cgroup hierarchy (cgroup v2)")
    psi = bool(read("/proc/pressure/memory"))
    add("System", OK if psi else FAIL, "memory pressure (PSI)",
        "" if psi else "kernel without PSI, or switched off: add the boot option psi=1")
    py = sys.version_info >= (3, 10)
    add("System", OK if py else FAIL, f"Python {platform.python_version()}", "" if py else "needs Python 3.10 or newer")
    tools = [t for t in ("swapon", "swapoff", "mkswap", "blockdev", "taskset", "flock", "modprobe") if not shutil.which(t)]
    add("System", FAIL if tools else OK, "system tools", "missing: " + " ".join(tools) if tools else "")
    s["CORE_OK"] = int(systemd and cg2 and psi and py and not tools)

    # --- VRAM tier ---
    why = []
    machine_ok = platform.machine() in ("x86_64", "aarch64")
    if not machine_ok:
        why.append(f"CPU {platform.machine()} not supported")
    ublk = os.path.isdir("/sys/module/ublk_drv") or run("modinfo", "ublk_drv")[0] == 0
    k = kernel_tuple()
    if ublk:
        add("VRAM tier", OK if k >= (6, 8) else WARN, "kernel ublk driver",
            "" if k >= (6, 8) else f"kernel {k[0]}.{k[1]} is older than the oldest tested (6.8); it may not work")
    else:
        add("VRAM tier", FAIL, "kernel ublk driver", "this kernel has no ublk_drv module (needs CONFIG_BLK_DEV_UBLK)")
        why.append("no ublk in this kernel")
    p = probe()
    s["GPU_NAME"], s["GPU_TOTAL_MB"], s["GPU_FREE_MB"] = p.get("name", ""), p.get("total_mb", ""), p.get("free_mb", "")
    if p.get("cuda") == "yes":
        drv = read("/sys/module/nvidia/version") or "?"
        kind = "open kernel modules" if "Open Kernel" in read("/proc/driver/nvidia/version") else "proprietary"
        tier_on = run("systemctl", "is-active", "-q", "usm-vramd")[0] == 0
        free = f"{p['free_mb']} MiB free now" + (" (the VRAM tier holds the rest)" if tier_on else "")
        add("VRAM tier", OK, f"NVIDIA GPU: {p['name']}", f"{p['total_mb']} MiB, {free}; "
            f"driver {drv} ({kind}), CUDA {p.get('cuda_api', '?')}")
        if int(p.get("total_mb", 0) or 0) < 2048:
            add("VRAM tier", WARN, "small GPU", "less than 2 GiB of VRAM: little to gain")
    else:
        add("VRAM tier", FAIL, "NVIDIA GPU with the NVIDIA driver", p.get("error", "not found") +
            " (AMD / Intel GPUs are not supported yet)")
        why.append(p.get("error", "no NVIDIA GPU"))
    lz4 = p.get("lz4") == "yes"
    add("VRAM tier", OK if lz4 else WARN, "lz4 library", "" if lz4 else "liblz4.so.1 missing: compression "
        "(COMPRESS) will be off; install liblz4-1 (Ubuntu/Debian) or lz4-libs (Fedora)")
    disp = nvidia_display()
    s["GPU_DISPLAY"] = disp
    if disp:
        add("VRAM tier", INFO, "GPU drives a screen" if disp == "Enabled" else "GPU drives no screen",
            "auto size keeps 2 GiB (or 30%) for the desktop" if disp == "Enabled"
            else "laptop with the desktop on the integrated GPU: auto size keeps 1 GiB")
    elif p.get("cuda") == "yes":
        add("VRAM tier", WARN, "nvidia-smi", "not found: VRAM_MB=auto assumes the GPU drives a screen")
    swaps = [l for l in read("/proc/swaps").splitlines()[1:] if not l.startswith("/dev/ublkb")]
    if swaps:
        add("VRAM tier", INFO, "existing swap", "; ".join(f"{l.split()[0]} prio {l.split()[4]}" for l in swaps) +
            " - the VRAM tier goes in front of it")
    else:
        add("VRAM tier", WARN, "no disk swap", "the tier works, but sleep/hibernate and switching off under full "
            "RAM have less room; a swap file of a few GiB is recommended")
    if "resume=" in read("/proc/cmdline"):
        add("VRAM tier", INFO, "hibernation is set up", "before hibernating the tier empties into RAM and the "
            "disk swap: keep the disk swap big enough for both")
    s["TIER_OK"] = int(ublk and p.get("cuda") == "yes" and machine_ok)
    s["TIER_WHY"] = "; ".join(why)
    s["LZ4_OK"] = int(lz4)

    # --- guard + desktop ---
    g = gnome_version()
    s["GNOME_VERSION"] = g or ""
    if g and 45 <= g <= 50:
        add("Guard", OK, f"GNOME Shell {g}", "the desktop helper tells the guard which app is in front")
    elif g:
        add("Guard", WARN, f"GNOME Shell {g}", "helper extension supports GNOME 45-50: the guard only watches and "
            "logs (it never pauses apps without knowing which one is in front)")
    else:
        add("Guard", INFO, "no GNOME Shell", "other desktops / servers: the guard only watches and logs; the VRAM "
            "tier and the logger work fully")
    pw = bool(shutil.which("pw-dump"))
    add("Guard", OK if pw else INFO, "PipeWire (pw-dump)", "" if pw else "not found: the guard cannot see which "
        "apps play sound (and may pause music)")
    pk = bool(shutil.which("pkexec"))
    if g:
        add("Guard", OK if pk else WARN, "polkit (pkexec)", "" if pk else "missing: the desktop menu cannot switch "
            "USM on/off (use the usm command)")
    s["DESKTOP"] = int(bool(g) or bool(glob.glob("/usr/share/xsessions/*")) or bool(glob.glob("/usr/share/wayland-sessions/*")))
    return res, s


def main():
    res, s = checks()
    if "--shell" in sys.argv:
        for k, v in s.items():
            print(f"{k}={shlex.quote(str(v))}")
        return 0
    ver = read(os.path.join(HERE, "VERSION")) or read(os.path.join(HERE, "..", "VERSION")) or "dev"
    print(f"USM {ver} - check of this machine (changes nothing)")
    group = None
    for g, st, name, detail in res:
        if g != group:
            group = g
            print(f"\n{g}")
        print(f"  [{st:4s}] {name}" + (f" - {detail}" if detail else ""))
    print()
    print("Result:")
    print(f"  base (logger, usm tools): {'works' if s['CORE_OK'] else 'NOT possible here'}")
    print(f"  VRAM swap tier:           {'works' if s['TIER_OK'] and s['CORE_OK'] else 'not available - ' + (s['TIER_WHY'] or 'see above')}")
    gv = s["GNOME_VERSION"]
    guard = "full (pauses busy background apps under overload)" if gv and 45 <= int(gv) <= 50 else \
        "watch only (logs what it would do)"
    print(f"  guard:                    {guard if s['CORE_OK'] else 'not available'}")
    return 0 if s["CORE_OK"] else 1


if __name__ == "__main__":
    sys.exit(main())
