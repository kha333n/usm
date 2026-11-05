# FAQ

**Jump to:** [Basics](#basics) · [Daily use](#daily-use) · [Hardware and systems](#hardware-and-systems) ·
[Safety and privacy](#safety-and-privacy) · [Known limits](#known-limits) · [Roadmap](#roadmap)

---

## Basics

### Is this "more RAM"?

**No, but it's the next best thing.** RAM is still faster. USM makes the overflow, the part that doesn't fit in
RAM, much faster:

| Where a swapped-out page comes back from | Time per page |
|---|---|
| RAM (no swapping) | ~0.1 µs |
| **USM's VRAM tier** | **~9–11 µs** |
| NVMe SSD swap file | ~55 µs and more; whole seconds when the SSD is busy |
| Hard disk | milliseconds |

### How does it fit with zswap?

While the tier is on, zswap stays as a **small, fast cache (10% of RAM)** in front of it, and the VRAM takes the
rest, so RAM stays free for your apps.

- Your own zswap settings come back when the tier goes off.
- `ZSWAP_POOL_WITH_TIER=keep` leaves zswap alone completely.

### And zram (Fedora's default)?

The VRAM tier goes **in front of zram**, and zram stays as the next tier. Nothing needs to change.

### Why "written with AI"?

The author directed the project, used it daily and chose what to keep. The code, tests and docs were written by
**Claude** (Anthropic's AI coding agent). It's disclosed so you can judge for yourself: please review it, test it, and
report what you find.

---

## Daily use

### Will it hurt my games, AI or video work?

**Only while it's on**, because it holds the VRAM. Before GPU-heavy work:

```bash
usm off     # the VRAM is free again in seconds
# ... play / render / train ...
usm on
```

The **Quick Settings toggle** does the same with one click. Automatic switching is on the [roadmap](#roadmap).

### What happens if I switch it off while RAM is full?

**Nothing bad.** The data doesn't have to fit into RAM:

1. The daemon copies it **straight from VRAM to a file on disk** (`/var/lib/usm-swap`); swapping keeps working.
   Meanwhile, new pages go to the standby swap file there.
2. The VRAM is released.
3. Later, once RAM has room, the data moves into RAM by itself.

### What does the guard's "pause" do to an app?

It **freezes the app for a few seconds** (cgroup freezer), like a very short `kill -STOP`. The app just sees a gap.

- It never pauses the app you're using, or one you used in the last 5 minutes.
- It never pauses apps playing sound, terminals, editors, calls, games or VMs.
- A pause lasts at most 20 s, and **switching to a paused app resumes it at once**.

### Suspend and hibernate?

**Handled automatically.**

- **Before sleep:** the tier moves its data into RAM and your disk swap, then switches off.
- **After waking:** it switches back on.
- **If it can't empty itself:** the sleep is refused rather than risking a hang.

> [!TIP]
> If you hibernate, keep a disk swap big enough for your RAM **plus** what is in the tier.

### Battery?

While the tier is on, the GPU can't go into deep sleep. On the tested laptops it then idles at **about 3 W**. Switch
the tier off on battery (`usm off`) if that matters to you.

---

## Hardware and systems

### What works where?

| | VRAM tier | Guard | Logger and tools |
|---|---|---|---|
| NVIDIA GPU + NVIDIA driver | ✅ | ✅ | ✅ |
| AMD / Intel GPU, or no GPU | ❌ (not yet) | ✅ | ✅ |
| GNOME 45–50 | ✅ | ✅ full | ✅ |
| KDE, XFCE, other desktops | ✅ | 👀 watches and logs only | ✅ |
| Server, no desktop (`install.sh --server`) | ✅ | – (desktop apps only) | ✅ |

### Which NVIDIA GPUs?

**Any GPU your installed NVIDIA driver supports**, with either the proprietary driver or the open kernel modules.

- The CUDA toolkit is **not** needed.
- The free nouveau driver does **not** work.
- Only the first GPU is used.

### AMD or Intel GPUs?

Not yet. You can still install the guard, the logger and the tools. Support is planned through a **Vulkan backend**,
one code path for AMD, Intel and NVIDIA, and help is welcome.

### Why does the guard only watch on KDE and other desktops?

To pause apps safely, the guard must know **which app is in front**. Today that comes from the GNOME extension. A
KWin script for KDE Plasma is planned.

### Secure Boot?

**Works.** USM installs no kernel module; it uses the kernel's own ublk driver.

### Servers?

The tier works headless: `sudo ./install.sh --server`. The guard manages desktop apps only.

> [!WARNING]
> USM is beta software. Read the [beta warning](../README.md#beta-read-this-first) before using it on a server.

---

## Safety and privacy

### What if the daemon crashes?

The data in VRAM is lost, and **the apps whose memory was there crash**, as if a swap disk had failed. The daemon is
built to make this unlikely:

- the OOM killer can't pick it;
- it is locked in RAM;
- it never waits on its own swap;
- it refuses to stop while it is in use.

But it is beta: don't use it where a crashed app would be a disaster.

### Does it send data anywhere?

**No.** USM has no network code. The logs stay on your machine in `/var/log/usm`.

### How do I remove it?

```bash
sudo usm uninstall            # keeps settings and logs
sudo usm uninstall --purge    # removes those too
```

The tier is switched off first, your zswap and readahead settings are restored, and no app stays paused.

---

## Known limits

| Limit | What it means |
|---|---|
| NVIDIA only, first GPU only | Other GPUs get the guard and logger, not the tier |
| Daemon crash or GPU reset | The data in VRAM is lost, and those apps crash |
| Guard needs GNOME 45–50 | Elsewhere it only watches and logs |
| Capacity = VRAM (×3 with compression) | Beyond that, USM's standby swap file on the disk takes over (`STANDBY_MB`, it grows while needed and the disk has room) |
| The disk itself is the bottleneck | A big download or update hammering a slow SSD: USM can't help, and the guard stays out of the way |
| Hibernation on real hardware | Not tested with a real power-off yet (suspend is, and hibernation in a VM) |

## Roadmap

- [ ] Switch the tier off automatically when a game or other GPU app starts
- [ ] AMD and Intel GPUs (Vulkan backend)
- [ ] The guard on KDE Plasma (KWin script)
- [ ] `.deb` / `.rpm` packages, then a PPA / COPR
- [ ] Tested on more distributions and GPUs ([reports welcome](../README.md#docs))
