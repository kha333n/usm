# USM settings

All settings live in `/etc/usm/usm.conf` (plain `KEY=VALUE` lines). Change them with
`usm config set KEY=VALUE` (checked, then applied), see them with `usm config show`, or edit the
file with `usm config edit`. At install time: `sudo ./install.sh --set KEY=VALUE` (repeatable).

This file is generated from `tools/usm_config.py` (`python3 tools/usm_config.py doc`).


## VRAM swap tier

After a change: usm off && usm on, or reboot.

| Setting | Default | Values | What it does |
|---|---|---|---|
| `VRAM_MB` | `auto` | 256..1048576 or auto | How much GPU memory to use as swap, in MiB. auto = all of it but 1 GiB; if this GPU also drives a screen, all but 2 GiB or 30% (whichever is more); never more than is free when the tier starts. Before games, AI or other GPU-heavy apps: usm off (usm on afterwards). |
| `COMPRESS` | `3` | 0..8 | 0 = store pages as they are. 2..8 = compress them (lz4, on the CPU, ~1 us per page): the swap device is COMPRESS x VRAM_MB big. Typical app memory shrinks to about a third, so 3 fills the VRAM well; what does not fit (data that does not compress) overflows into a file in SPILL_DIR. Needs liblz4. |
| `SPILL_DIR` | `/var/lib/usm-swap` | path | A directory on a disk (not tmpfs). Used for the compressed overflow and when the tier is switched off while RAM is full (the data then moves from VRAM to a file here, never through RAM). |
| `ZSWAP_POOL_WITH_TIER` | `10` | 0..100 or keep | zswap (compressed swap cache in RAM) while the tier is on: a pool of this % of RAM; restored when the tier goes off. 0 = zswap off while on. keep = do not touch zswap. 10 keeps a small fast cache in RAM and leaves the rest of RAM to the apps (tested: 10..40 equally fast, 0 slower, 5 smoother but slower apps). |
| `PAGE_CLUSTER_WITH_TIER` | `3` | 0..6 or keep | Swap readahead while the tier is on (vm.page-cluster: 2^N pages per swap-in; restored when off). 3 = 8 pages: an app coming back from swap returns ~3x faster than with 0. keep = do not touch. |
| `QUEUES` (advanced) | `4` | 1..16 | Request queues of the VRAM daemon (one CPU thread and 8 MiB of locked RAM each). 4 serves ~500 k page faults per second. |
| `TEST_DEVICE_MEMORY` (advanced) | `(empty)` | path or empty | Testing without a GPU: use this PCI device memory instead (e.g. a QEMU ivshmem .../resource2 file). Empty = use the NVIDIA GPU. |

## Guard

After a change: the guard restarts by itself.

| Setting | Default | Values | What it does |
|---|---|---|---|
| `GUARD_MODE` | `full` | full or protect | full = when apps really wait for memory, briefly pause the busiest background app (never the one in front, one used in the last GUARD_RECENT_S seconds, one playing sound, or one in GUARD_EXCLUDE). protect = only keep the desktop itself in RAM (GUARD_PROTECT_MB), never pause anything. |
| `GUARD_EXCLUDE` | `terminal, ptyxis, kgx, wezterm, kitty, alacritty, konsole, tilix, terminator, foot, ghostty, code, codium, cursor, zed, jetbrains, android-studio, sublime, zoom, teams, discord, slack, skype, signal, obs, anydesk, rustdesk, remmina, steam, lutris, heroic, virtualbox, vmware, virt-manager, gnome-boxes` | comma list | Never paused: apps whose name contains one of these (comma list, lower case; see the names in `usm status` or `usm report`). Default: terminals, code editors, calls, games, screen recording, remote desktop, virtual machines. |
| `GUARD_MIN_APP_MB` | `256` | 0..1048576 | Apps using less RAM than this (MiB) are never paused - it would free too little. |
| `GUARD_PROTECT_MB` | `1024` | 0..65536 | RAM (MiB) kept for the desktop itself (GNOME Shell, sound), so the mouse and windows stay responsive. |
| `GUARD_TRIGGER` | `0.30` | 0..0.95 | Overload = apps spend at least this share of the time waiting for memory (memory pressure, PSI). Lower = acts sooner. 0 = the default (0.30). |
| `GUARD_HIGH_TICKS` | `6` | 0..120 | How long the overload must last before a pause, in half-seconds (6 = 3 s). 0 = the default (6). |
| `GUARD_PAUSE_MAX_S` | `20` | 3..600 | A pause ends once memory has settled, at the latest after this many seconds. |
| `GUARD_COOLDOWN_S` | `120` | 0..86400 | After a pause the same app is not paused again for this many seconds. |
| `GUARD_RECENT_S` | `300` | 0..86400 | Apps you had in front within this many seconds are never paused. |

## Logger

After a change: the logger restarts by itself.

| Setting | Default | Values | What it does |
|---|---|---|---|
| `LOG_DIR` | `/var/log/usm` | path | Where the logs go: one file per day and kind (samples, apps, events, guard), gzipped after the day. |
| `LOG_INTERVAL` | `1` | 0.2..60 | Seconds between samples. |
| `LOG_KEEP_DAYS` | `30` | 1..3650 | Days of logs to keep. |
| `LOG_MAX_MB` | `300` | 10..1048576 | At most this much (MiB) of logs in LOG_DIR; the oldest go first (a day is ~10 MiB once gzipped). |
