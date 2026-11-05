# Test results: correctness

Does USM keep data intact, survive sleep, and does the guard follow its rules? Speed comparisons are in
[benchmarks.md](benchmarks.md). The machines are described there too. The test tools are in `tests/` (see
[tests/README.md](../tests/README.md)).

## Data checks

`tests/spill/spill-check.py` writes stamped random data to the raw device and verifies it, while spill and fill run.
Some of those were interrupted halfway, so the data lived partly in VRAM and partly on disk. Every block was
re-read at the end.

| | writes | verified reads | result |
|---|---|---|---|
| VM, plain tier | 1.89 M | 1.87 M | all correct |
| VM, compressed tier (mixed pages, with discards) | 1.65 M | 1.62 M | all correct |
| Legion (real GPU), plain | 1.11 M | 1.02 M | all correct, 0 page faults while serving |
| Legion (real GPU), compressed, 1.6 GB in a 2 GiB device on 1 GiB VRAM | 917 k | 777 k | all correct, no write refused |
| Legion (real GPU), compressed, 320 MB of incompressible data on 256 MiB VRAM (overflow to disk) | 109 k | 82 k | all correct, no write refused |

## Sleep

| | result |
|---|---|
| VM: suspend with a full 512 MiB tier | moved out in 1 s → suspended → woke → tier on again; app data correct |
| VM: hibernate (kernel test mode) | moved out in 0.5 s → hibernated → resumed → tier on; data correct |
| VM: hibernate **without** the hook | failed after 20 s ("task refusing to freeze"): this is why the hook exists |
| Legion: suspend with 1.8 GB in the tier | moved 1,800 MiB into RAM in 7.4 s → suspended → woke → tier on again; data correct |
| LOQ: sleep / wake hook with 1 GiB in the tier | off in 4.7 s / on again in 0.8 s |

Hibernation with a real power-off on real hardware has **not** been tested yet.

## Desktop (LOQ)

These tests use real test apps in their own app scopes, with memory verified page by page.

| test | result |
|---|---|
| guard: only the busy background app is paused under overload (not the one in front, a recently used one or an idle one) | pass |
| guard: no pause before 3 s of overload; pause ends at 20 s under lasting pressure, 2 s after it settles | pass |
| guard: cooldown, at most 2 paused, disk busy → no pause, no focus info → only logs | pass (`guard-policy-test.py`) |
| guard: switching to a paused app | running again in the same tick (< 1 s) |
| guard: app playing sound | never paused |
| guard: desktop lag while paused, memory calm | all apps resumed (safety brake) |
| replay of a real day's log with the current rules | 0 pauses in the window where an older version paused Chrome 10 times |
