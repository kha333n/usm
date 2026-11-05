# Benchmarks: with USM and without

- [Run it on your machine](#run-it-on-your-machine)
- [The author's machine: Lenovo LOQ laptop](#the-authors-machine-lenovo-loq-laptop)
- [The test laptop: Lenovo Legion](#the-test-laptop-lenovo-legion), with overload runs against zram
- Correctness tests (data checks, sleep, guard rules) are in [test-results.md](test-results.md)

**Reading the arrows:** green = better, red = worse, compared with the baseline named above each table (usually
"without USM"). The arrow shows whether the number went up or down: $\color{green}\downarrow$ a lower time is better, $\color{green}\uparrow$ more work done
is better, $\color{red}\uparrow$ a higher latency is worse.

## Run it on your machine

```bash
sudo tests/bench/usm-bench.sh            # ~10 minutes; --quick: ~4 minutes; --out results.md: also save the table
```

The script compares the VRAM tier on ("with") and off ("without": your own zswap and disk swap, as before USM). The
test app runs in its own memory-limited scope outside the desktop's apps, so the rest of the machine keeps its RAM.
It measures:

1. **Swap-in latency:** page faults served by the swap device itself (zswap bypassed), with 1, 4 and 8 threads at
   once.
2. **An app coming back:** an app is pushed out to swap entirely, then reads all its memory again.
3. **A working set bigger than its RAM** (zswap as configured): 5 GiB of app-like data (compresses ~3:1) in 2 GiB of
   RAM. It measures the fill time, random touches per second (70% in a hot 30%) and the time to read everything
   again. Meanwhile it records how much the machine stalled and GNOME's desktop lag.

Every page is verified. Run it on a calm machine: few apps open, little in swap (a fresh boot is best). Before the
"without" part, the script refuses to switch the tier off if that would push your apps' memory to disk. Please share
your table in an issue: results from other machines are very welcome.

## The author's machine: Lenovo LOQ laptop

| | |
|---|---|
| Laptop | Lenovo LOQ 15IRX9 (83DV) |
| CPU | Intel Core i5-13450HX, 10 cores / 16 threads |
| RAM | 16 GB DDR5 (one SODIMM, 4800 MT/s), 15.3 GiB usable |
| GPU | NVIDIA GeForce RTX 3050 6 GB Laptop GPU, PCIe 4.0 ×8, Resizable BAR; the desktop runs on the Intel GPU |
| Driver | NVIDIA 615.71.09, open kernel modules |
| Disk (swap) | Lexar NM620 512 GB NVMe SSD (DRAM-less), 17 GB swap file + 16 GB hibernation swap file |
| System | Ubuntu 26.04.1 LTS, kernel 7.0, GNOME 50 (Wayland) |
| Without USM | zswap 40% of RAM, swap readahead off (page-cluster 0), then the NVMe swap file |
| With USM | zswap 10%, then the VRAM tier: 5.5 GiB of VRAM, compressed ×3 = 16.5 GiB swap device; readahead 3; then the NVMe swap file |

### With vs without (`usm-bench.sh`)

<!-- BENCH-LOQ: replaced by the output of: sudo tests/bench/usm-bench.sh --out ... -->
*Results follow after the full run on a freshly booted machine (the tier must be off for the "without" part).*

### Desktop measurements (with USM)

These come from real desktop use: two memory hogs against the whole desktop, guard in observe mode. "Waited" means
all tasks stalled (PSI full). In the 2 × 6 GB rows the arrows compare each setting with the ×2 row (lower is better
everywhere).

| Setting | 2nd app filling | app coming back (verify, app 1 / app 2) | whole machine waited | desktop shell waited |
|---|---|---|---|---|
| 2 × 4.5 GB app-like data, 4 GiB tier, zswap off / 10% / 40% (5 runs) | 3.1–4.5 s | 1.4–4.9 s | 0.0–1.4 s | 0.0–0.9 s |
| 2 × 6 GB app-like data, 5.5 GiB tier ×2 (baseline) | 12.6 s | 33.1 / 8.0 s | 3.9 s | 1.6 s |
| same, ×3 | 11.5 s $\color{green}\downarrow$ | 28.1 $\color{green}\downarrow$ / 10.3 s $\color{red}\uparrow$ | 4.2 s $\color{red}\uparrow$ | 1.9 s $\color{red}\uparrow$ |
| same, ×3 + readahead 3 (**the setting used**) | 8.5 s $\color{green}\downarrow$ | 9.9 $\color{green}\downarrow$ / 11.1 s $\color{red}\uparrow$ | 3.3 s $\color{green}\downarrow$ | 2.5 s $\color{red}\uparrow$ |

The setting used trades a little desktop wait (+0.9 s over the whole run) for apps that fill 1.5× faster and a
first app that comes back 3.3× faster (the second a little slower), and its device holds 50% more than ×2 in the
same VRAM.

zswap pool size while the tier is on. Working set: 2 apps, then 40 s of random page touches, 70% in a hot 30%. The
figures are million touches/s (higher = better), two runs each. Arrows compare with zswap off (first column) and
with 5% (second column).

| zswap pool | 2 × 5 GB | sized to available RAM + 5 GB |
|---|---|---|
| off (baseline, column 1) | 58.9, 43.2 | – |
| 5% (baseline, column 2) | – | 16.0, 18.3 (desktop lag max 13 ms) |
| **10%** (default) | 71.1, 72.3 $\color{green}\uparrow$ | 21.7, 31.8 $\color{green}\uparrow$ (desktop lag max 0.55 s $\color{red}\uparrow$) |
| 25% / 40% | 73.5 / 73.5 $\color{green}\uparrow$ | – |

Readahead 3 instead of 0 made no difference for random access, and made an app coming back as a whole 2.8× faster.
For scale: one swap-in from the NVMe swap file takes ~55 µs plus fault overhead on this machine; from the VRAM tier
~11 µs.

## The test laptop: Lenovo Legion

| | |
|---|---|
| CPU | Intel Core i7-13700HX |
| RAM | 32 GB, limited to 8 GiB for the overload runs |
| GPU | NVIDIA GeForce RTX 4060 8 GB Laptop GPU, driver 595.99 |
| Disk | Ubuntu on an external USB hard disk (slow: the worst case for disk swap) |
| System | Ubuntu, kernel 7.0 |

### Latency of one 4 KiB swap-in

The arrows describe the VRAM tier against the other column: NVMe swap (the usual swap without USM) and zram
(compressed swap in RAM).

| | VRAM tier | NVMe swap file | VRAM vs NVMe | zram (zstd) | VRAM vs zram |
|---|---|---|---|---|---|
| page fault, 1 app thread | **8.8 µs** (p99 12.8) = 107 k/s | ~55 µs + fault overhead | $\color{green}\downarrow$ ~7× faster | 2.0 µs = 463 k/s | $\color{red}\uparrow$ 4× slower |
| page fault, 8 threads, 4 queues | 13.8 µs = 515 k/s | | | 3.1 µs = 1.8 M/s | $\color{red}\uparrow$ slower |
| page fault, 16 threads, 4 queues | 22.5 µs = 596 k/s | | | 4.4 µs = 2.7 M/s | $\color{red}\uparrow$ slower |
| swap-out (bulk) | ~4 GB/s | | | ~0.9 GB/s (CPU compressing) | $\color{green}\uparrow$ 4× faster |
| RAM taken by the swapped data | none | none | same | ~2 of the 8 GB | $\color{green}\downarrow$ frees it |

For scale: the GPU copy itself takes 3.9 µs, NVMe swap ~55 µs plus fault overhead, a hard disk milliseconds. zram
answers faster per page, but it keeps its pages in RAM and burns CPU on every swap-out. The VRAM tier costs neither,
which is why it gets more work done under overload (next table).

### Under overload: 8 apps × 1.5 GiB on 8 GiB of RAM

7 GiB of VRAM was the only fast swap. The "UI probe" measures how late a small interactive task runs. "Hung" means a
login took more than 10 s.

Arrows compare each VRAM row with the zram row above it (no guard with no guard, guard with guard).

| | UI probe p99 / worst | logins, worst | all tasks stalled | work done (median) | hung runs |
|---|---|---|---|---|---|
| zram, no guard (baseline) | 62 / 705 ms | all > 6 s | 89% | 2.6 G | – |
| VRAM tier, no guard | 43 / 175 ms $\color{green}\downarrow$ | 7 of 9 > 6 s $\color{green}\downarrow$ | 67% $\color{green}\downarrow$ | 3.5 G $\color{green}\uparrow$ | – |
| zram + guard (6 runs, baseline) | 2.3–11 / 75–320 ms | 7–21 s | 9–38% | 5.9 G | **3 of 6** |
| **VRAM tier + guard** (4 runs) | **0.4–1.4 / 0.5–61 ms** $\color{green}\downarrow$ | **2.3–6.8 s** $\color{green}\downarrow$ | **0.3–4.3%** $\color{green}\downarrow$ | **9.3 G** $\color{green}\uparrow$ | **0 of 4** $\color{green}\downarrow$ |
| VRAM tier ×2 compressed + guard (2 runs) | 0.2 / 0.3–0.4 ms $\color{green}\downarrow$ | 1.7–2.6 s $\color{green}\downarrow$ | 0.5% $\color{green}\downarrow$ | 16.2 G $\color{green}\uparrow$ | 0 of 2 $\color{green}\downarrow$ |

No run killed an app, and every app's data was correct.

### Compression: cost and gain

Arrows compare the compressed tier with the plain tier (baseline): compression costs a little speed...

| page faults at once | plain tier (baseline) | compressed tier (2 GiB device on 1 GiB VRAM, pages stored at ~34%) |
|---|---|---|
| 1 thread | 8.3 µs, 115 k/s | 9.5 µs $\color{red}\uparrow$, 103 k/s $\color{red}\downarrow$ |
| 8 threads | 14.1 µs, 523 k/s | 17.9 µs $\color{red}\uparrow$, 429 k/s $\color{red}\downarrow$ |
| 16 threads | 20.7 µs, 607 k/s | 26.1 µs $\color{red}\uparrow$, 503 k/s $\color{red}\downarrow$ |
| capacity in the same VRAM | 1× | **~2.5–3×** $\color{green}\uparrow$ |

...and wins where it counts: with the same 2 GiB of VRAM, 6 apps × 1.5 GiB on 8 GiB of RAM, guard on:

| | plain (2 GiB device, baseline) | compressed (4 GiB device on the same 2 GiB) |
|---|---|---|
| logins, worst | 8.4 s | **2.9 s** $\color{green}\downarrow$ |
| apps' work | 0.6 M page touches (the overflow went to the hard disk) | **25,236 M** $\color{green}\uparrow$ |
| swap on the hard disk needed | yes | **no** $\color{green}\downarrow$ |

### Switching the tier off under load (VM, slow disk)

The VM had 1 GiB of RAM and a full 512 MiB tier. The tier was switched off with ~250 MB of RAM available; both disks
were limited to 25 MB/s.

| switched off by | took | UI worst | logins | result |
|---|---|---|---|---|
| plain `swapoff` (what you'd get without the spill, baseline) | 298 s | 8.7 s | 27 of 39 failed | froze for minutes |
| **spill** (VRAM → file, not through RAM) | **36 s** $\color{green}\downarrow$ | **1.1 ms** $\color{green}\downarrow$ | **0 of 140 failed** $\color{green}\downarrow$ | no hang, data correct |
| **spill**, NVMe-backed disk | **1.1 s** $\color{green}\downarrow$ | 1.8 ms $\color{green}\downarrow$ | 0 of 55 failed $\color{green}\downarrow$ | no hang, data correct |
