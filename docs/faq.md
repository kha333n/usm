# FAQ

**Is this "more RAM"?** No. RAM is still faster. USM makes the overflow much faster than a disk (a page comes back
in ~9–11 µs, against ~55 µs+ from an NVMe swap file and milliseconds from a hard disk), and it keeps the desktop
responsive while that happens.

**Will it hurt my games, AI or video work?** Only while it's on, because it holds the VRAM. Switch it off first
(`usm off` or the Quick Settings toggle). The VRAM is free again in seconds. Run `usm on` afterwards. Automatic
switching is on the roadmap.

**What happens when I switch it off while RAM is full?** The data doesn't have to fit into RAM. The daemon copies it
straight from VRAM to a file on disk (`/var/lib/usm-swap`) while swapping keeps working, releases the VRAM, and moves
the data into RAM later, once there's room.

**Battery?** While the tier is on, the GPU stays awake (no deep sleep). On the tested laptops it then idles at about
3 W. Switch the tier off on battery if that matters to you.

**Suspend and hibernate?** Handled. Before the machine sleeps, the tier moves its data into RAM and your disk swap,
then switches off. After waking it switches back on. If it can't empty itself, the sleep is refused rather than
risking a hang. For hibernation, keep a disk swap big enough for RAM plus what's in the tier.

**What if the daemon crashes?** The data in VRAM is lost, and the apps whose memory was there crash (as if a swap
disk failed). The daemon is built against this: the OOM killer can't pick it, it is locked in RAM, it never waits on
its own swap, and it refuses to stop while in use. But it's beta, so don't use it where a crashed app is a disaster.

**No NVIDIA GPU, or AMD or Intel?** You can install the guard, the logger and the tools; the VRAM tier needs NVIDIA
for now. A Vulkan backend (AMD, Intel and NVIDIA in one) is the planned way to support the others.

**Which NVIDIA GPUs?** Any that your installed NVIDIA driver supports, with the proprietary driver or the open kernel
modules. The CUDA toolkit is not needed, and neither is the free nouveau driver (it won't work). Only the first GPU
is used.

**Secure Boot?** Works. USM installs no kernel module; it uses the kernel's own ublk driver.

**KDE, XFCE, other desktops?** The VRAM tier and the logger work fully. The guard only watches and logs, because it
needs to know which app is in front, and that comes from the GNOME extension. A KWin script for KDE is planned.

**Servers?** The tier works headless (`sudo ./install.sh --server`). The guard manages desktop apps only. Read the
beta warning again before using it on a server.

**zram (Fedora's default)?** The VRAM tier goes in front of it; zram stays as the next tier.

**zswap?** While the tier is on, zswap is a small (10%) fast cache in front of it, so RAM stays free for the apps.
Your own zswap settings come back when the tier goes off. `ZSWAP_POOL_WITH_TIER=keep` leaves zswap alone.

**What does the guard's "pause" do to an app?** It freezes the app's processes (cgroup freezer) for a few seconds,
like a very short `kill -STOP`. The app just sees a gap. The guard never pauses the app you're using, and resumes a
paused app the moment you switch to it.

**Does it send data anywhere?** No. USM has no network code; the logs stay in `/var/log/usm`.

**Why "written with AI"?** The author directed the project, tested it daily and chose what to keep. The code, tests
and docs were written by Claude (Anthropic's AI coding agent). It's disclosed so you can judge it: please review it,
test it, and report what you find.

## Known limits

- NVIDIA only; the first GPU only.
- If the daemon crashes or the GPU resets, the data in VRAM is lost.
- The guard needs GNOME 45–50 to know which app is in front; elsewhere it only watches.
- Capacity is the VRAM (×3 with compression). Beyond that your disk swap takes over, so keep one.
- When the disk itself is the bottleneck (a big download or update hammering a slow SSD), USM can't help; the guard
  sees it and stays out of the way.
- Hibernation with a real power-off on real hardware is not tested yet (suspend is, and hibernation in a VM).

## Roadmap

- Switch the tier off automatically when a game or other GPU app starts
- AMD and Intel GPUs (Vulkan backend)
- The guard on KDE Plasma (KWin script)
- `.deb` / `.rpm` packages, then a PPA / COPR
- Tested on more distributions and GPUs: reports welcome
