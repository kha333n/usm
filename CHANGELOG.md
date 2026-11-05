# Changelog

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
