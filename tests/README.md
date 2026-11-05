# Tests

| What | How | Needs |
|---|---|---|
| Benchmark with vs without USM on this machine (contained, ~10 min) | `sudo tests/bench/usm-bench.sh [--quick] [--out FILE.md]` | USM installed, a C compiler; see [docs/benchmarks.md](../docs/benchmarks.md) |
| Offline checks (syntax, settings tool, daemon `--version` / `probe`, docs up to date) | `make check` | nothing (CI runs this) |
| Guard rules, deterministic (fake clock and pressure, real pause/resume of tiny test apps) | `sudo tests/desktop/guard-policy-test.py UID` | USM installed, guard **stopped** (`usm guard off`), a logged-in desktop user UID |
| Guard on the real desktop (memory hogs: in front / busy / recent / idle) | `make tests/desktop/hog` then `sudo tests/desktop/guard-desktop-test.py UID` | as above; uses a lot of RAM for a minute |
| Pressure / working-set runs | `tests/desktop/pressure-test.sh`, `tests/desktop/touch-test.sh` | as above |
| VRAM device data check while spill / fill run | `sudo tests/spill/spill-check.py DEV SIZE_MB SECONDS` (see the script) | a tier device that is **not** in use as swap |

The desktop tests only touch their own test apps (`app-usmtest-*` scopes) and stop them at the end.
