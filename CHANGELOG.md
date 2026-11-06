# Changelog

## [0.1.0-beta.3] - 2026-11-06

The VRAM tier now steps aside by itself when other programs need the GPU. Tested on an RTX 3050 6 GB laptop GPU: a
CUDA program asking for 4.5 GB got all of it while the tier held 800 MB of an app's swapped data, which moved to the
disk and back with no errors.

- **New: VRAM goes back to other programs by itself** (`GPU_FREE_MB`, default 1024). CUDA doesn't share VRAM, so
  while the tier held it, a game or AI app that needed GPU memory simply got "out of memory". Now the daemon borrows
  its VRAM in 64 MiB pieces and watches the GPU's free memory. Once another program starts using the GPU, pieces go
  back until `GPU_FREE_MB` is free (empty ones at once, the others after their data moved to the spill file, as in a
  spill), and they are taken back once that program has been done for 30 s. `usm off` stays off until `usm on`.
  `GPU_FREE_MB=off` keeps the old behaviour. Known limit: a program that takes a lot of VRAM in one go right as it
  starts may still find too little free (see the FAQ).
- `usm status` shows how much VRAM is given back, and names the standby swap files correctly (they showed as guard
  temp files).
- `usm check` and the installer no longer open a CUDA context while the tier runs (the daemon would have taken it for
  another program and given VRAM back).
- **Fixed: the Quick Settings menu could switch the guard or the VRAM tier off by itself** on newer GNOME (seen on
  GNOME 50), where showing a switch's new state counts as a click. The guard restarting during an upgrade, or the
  tier coming back on after a wake, could so turn it off for good. Only real clicks switch anything now.

## [0.1.0-beta.2] - 2026-11-05

Fixes a hang. Please update if you run 0.1.0-beta.1.

- **Fixed: the whole system could hang under heavy memory pressure** once the VRAM was full in compressed mode
  (the default). The daemon wrote the data that didn't fit in VRAM to a file itself, while it was serving swap.
  When memory had run out, that file write waited for memory that only finishing swap writes could free, and those
  writes waited for the daemon. Found in a release test: a 2 GiB VM froze within seconds, every time.
- **New: the standby swap** (`STANDBY_MB`, default `auto`: as big as the VRAM tier, at most 4 GiB). An ordinary swap
  file in `SPILL_DIR` that the daemon switches on above its own device while the VRAM is nearly full, or while data
  is in the spill file. Linux then writes new pages to the disk itself. When it fills up while needed, more files of
  the same size are added (the disk keeps 10%, at least 2 GiB, free). It switches off again once the VRAM has room
  and its pages fit in RAM. The daemon no longer touches the disk while the kernel depends on it to free memory.
- The compressed overflow file is gone. Without a standby swap (`STANDBY_MB=0`), a full VRAM refuses writes (Linux
  keeps those pages in RAM), and `usm off` with RAM full is refused.
- The compactor starts earlier (below 1/8 of the VRAM free), so dead space is reclaimed before the standby swap is
  needed.
- `usm status` shows the standby swap when it is on.

## [0.1.0-beta.1] - 2026-11-05

First public beta. Tested on Ubuntu 26.04 (kernel 7.0) with an RTX 3050 6 GB and an RTX 4060 8 GB laptop GPU.

- **VRAM swap tier** (`usm on` / `usm off`): `usm-vramd` (CUDA driver API + kernel ublk) serves a swap device from
  GPU memory, ~9 µs per page fault, ~500 k faults/s with 4 queues. lz4 compression (`COMPRESS=3` by default) with
  overflow to a file on disk. Switching off moves the data into RAM, or straight to a disk file when RAM is full
  (spill), while swapping keeps working. Suspend and hibernate are handled (the tier empties before sleep).
- **Guard** (`usm guard on`, GNOME 45-50): under a real memory stall it pauses the busiest background app for at
  most 20 s; never the app in front, recently used apps, apps playing sound, or excluded apps; does nothing when the
  disk is the bottleneck.
- **Logger and tools**: `usm status | dash | report | mark | log | collect`.
- **GNOME Quick Settings menu** with switches for the tier and the guard.
- **Installer** with questions or fully unattended (`--yes`, `--tier`, `--guard`, `--vram-mb`, `--compress`,
  `--set KEY=VALUE`, `--server`), **`usm config show | get | set | reset | edit`** (checked, applied), **`usm check`**
  (what works on this machine), `VRAM_MB=auto`, `usm uninstall`.
- Prebuilt x86_64 release built on Ubuntu 22.04 (glibc 2.35); the daemon links only against libc.
