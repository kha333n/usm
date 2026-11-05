# How USM works

USM = Unified Storage Memory: one memory space over RAM, zswap, the GPU's memory and the disk, with the fastest
place first and apps unchanged.

USM has three parts that work on their own and better together: the **VRAM swap tier**, the **guard** and the
**logger**. All of it is user space. The only kernel features used are ones every recent distribution kernel has:
ublk, swap, cgroup v2, PSI and zswap.

## The VRAM swap tier

```
 app touches a swapped-out page
        │ page fault
        ▼
 Linux swap ──▶ /dev/ublkb0 (kernel ublk driver) ──io_uring──▶ usm-vramd ──CUDA copy engine──▶ GPU memory
```

- **`usm-vramd`** (C, `vram/usm-vramd.c`) borrows VRAM through the NVIDIA driver's CUDA driver API. `libcuda.so.1`
  is loaded at run time, so no CUDA toolkit is needed. It creates a block device with the kernel's **ublk** driver
  and serves each read and write by moving data between RAM and VRAM with the GPU's copy engines. Requests that
  arrive together are copied as one batch.
- **Queues:** 4 by default. Each has its own thread, io_uring, pinned I/O buffers and CUDA stream, so page faults on
  different CPUs are served in parallel (~500 k faults/s).
- **Swap priority:** `usm-vramd-ctl on` makes the device the top-priority swap (200, above zram's 100). The kernel
  fills VRAM first; your disk swap stays below it as the next tier.
- **zswap in front:** while the tier is on, zswap's pool is set to 10% of RAM (`ZSWAP_POOL_WITH_TIER`). A page from
  zswap comes back in a few µs, one from VRAM in ~9–18 µs. A small zswap cache pays off, and the VRAM takes the rest
  so RAM stays free for the apps. Swap readahead is set to 8 pages (`PAGE_CLUSTER_WITH_TIER=3`), so an app coming
  back from swap returns about 3× faster. Both settings are restored when the tier goes off.

### Compression (`COMPRESS=3`)

Each page written is compressed with lz4 on the CPU, which takes about 1 µs:
- All-zero pages are only marked, not stored.
- Pages that barely compress are kept as they are.

The pages are appended to the queue's open 1 MiB segment in pinned RAM, and a full segment goes to VRAM in one copy.
A map in RAM (8 bytes per page) says where each page lives.

When pages are rewritten, or swap no longer needs them, they leave dead space. The kernel tells the daemon about
those pages through discard. A mover thread compacts mostly-dead segments in the background.

With `COMPRESS=3` the device is 3× the VRAM. If the VRAM fills with data that doesn't compress, new segments go to an
overflow file in `SPILL_DIR` instead. The file's space is reserved ahead, so serving never waits on the file system.
So the device can always hold its full size.

### Switching off: swapoff or spill

`usm off` gives the VRAM back:
- **RAM has room:** the pages move into RAM (`swapoff`). The move is watched and interrupted if RAM gets low.
- **RAM is full:** the daemon **spills** instead. It copies its data chunk by chunk (1 MiB) straight from VRAM to a
  file on disk, never through RAM, and the device keeps serving the whole time. Then it frees the VRAM.

After a spill, a background job (`usm-vramd-drain`) moves the pages into RAM once RAM has room, then stops the
daemon. `usm on` during that time copies the data back into VRAM (**fill**).

### Sleep and hibernation

Before suspend or hibernate, Linux freezes all programs, `usm-vramd` too. A page still in VRAM could then not be read,
and hibernation would hang. So a systemd unit ordered before `sleep.target` (and before NVIDIA's own suspend units)
switches the tier fully off: the data goes into RAM, or into the disk swap if RAM is tight. Another unit switches the
tier back on after waking. If the tier can't be emptied, the sleep is refused rather than risking a hang. This was
tested with suspend on real GPUs, and with hibernation in a VM (kernel test mode).

### Safety measures in the daemon

- It is **never picked by the OOM killer** (`oom_score_adj -1000`), and all its memory is **locked** (`mlockall`).
- Its own memory allocations never start I/O (`PR_SET_IO_FLUSHER`), so it never waits on its own swap device.
- It pre-touches everything it needs before the device goes live: **0 page faults** while serving, measured.
- It **refuses to stop while its device is in use** (ublk `TRY_STOP_DEV`). Swap must be off first.
- It keeps the GPU out of runtime sleep while on. This avoids a wake-up delay on every swap-in and avoids sleep/wake
  cycles, which crashed one test GPU's firmware.
- What it can't survive: being killed with SIGKILL, or a GPU or driver reset. Then the pages in VRAM are gone.

## The guard

The guard (`guard/usm-guard.py`) manages the desktop user's apps. Each app is a systemd scope under `app.slice`, as
GNOME launches it; the scopes of one launch, such as Chrome's two, count as one app. It ticks every 0.5 s.

**When it acts:** only on a real memory stall. That means apps waiting for memory ≥ 30% of the time
(`GUARD_TRIGGER`), or the desktop session itself ≥ 10%, for 3 s in a row (`GUARD_HIGH_TICKS`). Page faults and fast
swap traffic alone don't count.

**When it does nothing:**
- The disk is the bottleneck: average read latency ≥ 100 ms. Pausing apps doesn't help a stalled SSD.
- It doesn't know which app is in front, because the GNOME extension isn't active. Then it only logs what it would
  have done (`would-freeze`).

**What it pauses:** the busiest eligible app, measured by page faults and memory growth. It never pauses:
- the app in front, or an app in front within the last 5 minutes (`GUARD_RECENT_S`);
- apps playing or recording sound (PipeWire);
- apps in `GUARD_EXCLUDE` (terminals, editors, calls, games, VMs…);
- apps under 256 MiB, or idle apps.

At most 2 apps are paused at once.

**How long:**
- A pause ends once memory has been calm for 2 s, and at the latest after `GUARD_PAUSE_MAX_S` (20 s).
- The same app then isn't paused again for `GUARD_COOLDOWN_S` (120 s).
- Switching to a paused app resumes it in the same tick.
- If the desktop lags while apps are paused but memory is calm, everything resumes (the safety brake).

**Other protection:**
- It keeps `GUARD_PROTECT_MB` (1 GiB) of RAM protected for GNOME Shell and sound (`memory.low`), so the mouse and
  windows stay responsive.
- systemd watches it (watchdog 30 s). Whenever it stops or crashes, systemd resumes every paused app.

The **GNOME extension** (`desktop/usm-desktop@usm`) writes the app in front, idle time and GNOME Shell's own lag to
`$XDG_RUNTIME_DIR/usm-desktop`; it doesn't record window titles. It also adds the Quick Settings menu. Its switches
run `/usr/local/lib/usm/usm-helper`, which has four fixed actions, through pkexec. The polkit action
`org.usm.switch` allows them without a password for the user at the machine.

## The logger

`tools/usm-logger.py` uses ~0.3% of a core. Files in `/var/log/usm`:
- `samples-DAY.csv` every second: RAM, PSI for everything / apps / the desktop shell, paging, zswap, swap per tier,
  device and disk latency, tier state, GPU, the app in front, desktop lag, the guard;
- `apps-DAY.csv` every 10 s: per-app memory, swap and disk I/O;
- `events-DAY.log`: stall episodes, sleep and wake, swap and tier changes, OOM kills.

Finished days are gzipped. Logs are kept for 30 days, at most 300 MiB (`LOG_*` settings). It only reads (procfs,
sysfs, cgroups, NVML) and writes from a background thread at idle I/O priority, so a busy disk never stalls the
sampling. `usm report` and `usm dash` read these files.

## Files

| Path | What |
|---|---|
| `/usr/local/lib/usm/` | programs, `README.md`, `VERSION` |
| `/usr/local/sbin/usm` | the `usm` command |
| `/etc/usm/usm.conf` | settings (`usm config`) |
| `/etc/systemd/system/usm-{vram,guard,logger}.service` | the services; `usm-vramd-{sleep,wake}.service` + drop-ins for sleep |
| `/var/log/usm/` | logs |
| `/var/lib/usm-swap/` | overflow / spill file (only while needed) |
| `/usr/local/share/gnome-shell/extensions/usm-desktop@usm/` | GNOME extension |
| `/usr/share/polkit-1/actions/org.usm.switch.policy` | lets the desktop menu switch USM without a password |
| `/run/usm-*` | runtime state (gone at reboot) |
