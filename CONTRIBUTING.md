# Contributing to USM

Thanks for helping! USM is a young beta. The most valuable help right now is **testing on machines we don't have**,
followed by bug reports and code.

## Ways to help

| | How |
|---|---|
| 🧪 **Test it on your machine** | Install it, use it for a while, then open a [test report](https://github.com/kha333n/usm/issues/new?template=test_report.yml) with `usm check` and, if you can, `sudo tests/bench/usm-bench.sh` (with vs without USM). "It works" reports matter as much as "it doesn't". |
| 🐛 **Report bugs** | [Bug report](https://github.com/kha333n/usm/issues/new?template=bug_report.yml) with `usm check`, `usm version` and `usm collect`. |
| 💬 **Ask, share results** | [Discussions](https://github.com/kha333n/usm/discussions) |
| 🧑‍💻 **Code** | See below. Wanted most: an **AMD / Intel GPU backend** (Vulkan), **KDE Plasma support** for the guard (KWin script), **.deb / .rpm packaging**, auto-off when a game starts. |
| 📝 **Docs** | Fixes and clearer explanations are always welcome. |

## Development setup

```bash
git clone https://github.com/kha333n/usm.git && cd usm
make              # builds vram/usm-vramd (needs gcc + make; liburing is included)
make check        # offline checks: syntax, settings tool, docs up to date, daemon --version / probe
sudo ./install.sh # installs your working copy (an upgrade keeps your settings); remove: sudo usm uninstall
```

The tests that need root, a GPU or a desktop are described in [tests/README.md](tests/README.md). They only touch
their own test apps.

## Where things are

| Path | What |
|---|---|
| `vram/usm-vramd.c` | the VRAM daemon (C): CUDA driver API (loaded at run time) + kernel ublk, compression, spill / fill |
| `vram/usm-vramd-ctl`, `vram/usm-vram-service`, `vram/usm-vram-move.py` | switching the tier on / off, sleep hooks, moving data out |
| `guard/usm-guard.py` | the guard (Python, stdlib only) |
| `tools/` | the `usm` command, logger, `usm_config.py` (all settings), `usm_check.py` |
| `desktop/usm-desktop@usm/` | GNOME Shell extension (focus info + Quick Settings menu) |
| `system/` | systemd units, polkit rule |
| `install.sh`, `uninstall.sh` | installer |
| `tests/` | unit checks, desktop and data tests, benchmark |
| `docs/` | user documentation |

How it all works: [docs/how-it-works.md](docs/how-it-works.md).

## Rules of thumb

- **Keep `make check` green.** CI runs it on Ubuntu 22.04 / 24.04 / 26.04 and Debian 13.
- **New or changed settings** go in `tools/usm_config.py` only, then regenerate the reference:
  `python3 tools/usm_config.py doc > docs/configuration.md`.
- **Python:** 3.10+, standard library only. **Shell:** bash, must pass `bash -n` (and `shellcheck -S error`).
  **C:** the daemon links only against libc; anything else is loaded at run time (`dlopen`).
- **Data safety first.** Changes to the daemon or the tier need a data check on real hardware
  (`tests/spill/spill-check.py`, every page verified). Say in the pull request what you tested, and on which
  machine, GPU and kernel.
- **Never change user settings outside USM's own files.** Anything USM changes while on (zswap, readahead) must be
  restored when it goes off.
- Keep the README short; details go in `docs/`.

## Pull requests

1. Fork, branch, change, `make check`.
2. Fill in the pull request template: what and why, how it was tested.
3. Small, focused pull requests are reviewed fastest.

AI-assisted contributions are welcome (USM itself was written with AI). You are responsible for understanding and
testing what you submit.

By contributing you agree that your contribution is licensed under the [MIT license](LICENSE), and you follow the
[Code of Conduct](CODE_OF_CONDUCT.md).
