#!/usr/bin/env python3
"""usm-vram-move: empty a swap device into RAM (swapoff), but only while RAM really has room.

swapoff brings every page of the device back into RAM. That is the fast and simple way to switch
the VRAM tier off - if the pages fit. This checks first, and while swapoff runs it watches the
available RAM: when it gets low, swapoff is interrupted (Linux keeps what was already moved; the
rest stays on the device, which keeps working). While swapoff runs, the guard (if running) pauses
background apps (request file /run/usm-guard.pause), so they do not compete for the RAM.

The other way - when RAM is full - is a spill: usm-vramd copies its data straight from VRAM to a
file on disk (usm-vramd-ctl does that when this exits with 3).

Usage (root): usm-vram-move.py DEV
  exit 0 = DEV is empty and off; 3 = not enough free RAM (DEV stays on with what is left); 1 = error
"""
import os
import signal
import subprocess
import sys
import time

PAUSE = "/run/usm-guard.pause"
POLL = 0.05


def swaps():
    out = []
    for line in open("/proc/swaps").read().splitlines()[1:]:
        name, kind, size, used, prio = line.split()
        out.append(dict(name=name, kind=kind, size=int(size) << 10, used=int(used) << 10, prio=int(prio)))
    return out


def meminfo():
    return {l.split(":")[0]: int(l.split()[1]) << 10 for l in open("/proc/meminfo")}


def mib(b):
    return f"{b >> 20} MiB"


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def device(dev):
    return next((s for s in swaps() if s["name"] == dev), None)


def main():
    dev = sys.argv[1]
    me = device(dev)
    if not me:
        log(f"{dev} is not in use as swap - nothing to move")
        return 0
    mi = meminfo()
    # Count all of it: some may already be in RAM (swap cache), but /proc/meminfo's SwapCached covers every
    # swap device, so subtracting it can hide what this one needs. Too high only means a spill instead.
    need = me["used"]
    usable = mi["MemTotal"] - mi.get("Hugetlb", 0)
    low = max(256 << 20, usable // 25)   # interrupt swapoff below this much available RAM
    if mi["MemAvailable"] < need + 2 * low:
        log(f"not enough free RAM for the {mib(need)} on {dev}: {mib(mi['MemAvailable'])} available,"
            f" want {mib(need + 2 * low)} (+ {mib(2 * low)} to spare)")
        return 3
    log(f"moving {mib(need)} from {dev} into RAM ({mib(mi['MemAvailable'])} available)")
    open(PAUSE, "w").write(f"moving {dev}\n")
    t0 = time.monotonic()
    try:
        p = subprocess.Popen(["swapoff", dev], stderr=subprocess.DEVNULL)
        interrupted = False
        while p.poll() is None:
            if meminfo()["MemAvailable"] < low:
                p.send_signal(signal.SIGINT)
                interrupted = True
                p.wait()
                break
            time.sleep(POLL)
    finally:
        try:
            os.unlink(PAUSE)
        except FileNotFoundError:
            pass
    cur = device(dev)
    if cur is None:
        log(f"done: {dev} is empty and off after {time.monotonic() - t0:.1f} s")
        return 0
    if interrupted:
        log(f"RAM got low after {time.monotonic() - t0:.1f} s: stopped, {mib(cur['used'])} still on {dev}")
        return 3
    log(f"swapoff stopped (exit {p.returncode}): {mib(cur['used'])} still on {dev}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
