# Installing USM

> [!WARNING]
> USM is **beta** software, written mostly by AI and tested on few machines. Don't use it on critical systems.
> See the [README](../README.md#beta-read-this-first).

## 1. Check your machine (optional, changes nothing)

Download and unpack a release (step 2), or clone the source (step 3). Then, in that folder:

```bash
python3 tools/usm_check.py
```

It lists what will work: the base system, the VRAM tier (kernel ublk, NVIDIA driver, lz4) and the guard (GNOME
version), and says why if something won't.

## 2. Install the prebuilt release (recommended)

Releases are on the [Releases page](https://github.com/kha333n/usm/releases). The daemon is built on Ubuntu 22.04 and
links only against libc (glibc 2.34 or newer: Ubuntu 22.04+, Debian 12+, Fedora 35+), so it runs on any recent x86_64
distribution.

```bash
VERSION=0.1.0-beta.2
curl -LO https://github.com/kha333n/usm/releases/download/v$VERSION/usm-$VERSION-linux-x86_64.tar.gz
curl -LO https://github.com/kha333n/usm/releases/download/v$VERSION/usm-$VERSION-linux-x86_64.tar.gz.sha256
sha256sum -c usm-$VERSION-linux-x86_64.tar.gz.sha256
tar xzf usm-$VERSION-linux-x86_64.tar.gz
cd usm-$VERSION-linux-x86_64
sudo ./install.sh
```

## 3. Or build from source

The build needs a C compiler and `make`, nothing else. liburing and the kernel's ublk header are included; CUDA and
lz4 are loaded at run time.

```bash
# Ubuntu / Debian:  sudo apt install git build-essential python3
# Fedora:           sudo dnf install git gcc make python3
# Arch:             sudo pacman -S git base-devel python
git clone https://github.com/kha333n/usm.git
cd usm
make              # builds vram/usm-vramd
make check        # optional: quick offline checks
sudo ./install.sh
```

`install.sh` runs `make` itself if the daemon isn't built yet, as the folder's owner, so no root-owned files end up
in your checkout.

To build a release tarball like CI does (in an Ubuntu 22.04 container), run `packaging/build-release.sh`. It needs
docker or podman (`DOCKER=podman`). The result goes to `dist/`.

### Making a GitHub release (maintainers)

1. Set the new version in `VERSION` and add its section to `CHANGELOG.md`, then commit and push.
2. Either create a release on GitHub (*Releases → Draft a new release*, new tag = `v` + the version, e.g.
   `v0.1.0-beta.2`, then publish), or push the tag: `git tag v0.1.0-beta.2 && git push origin v0.1.0-beta.2`.
3. The *Release* workflow builds the tarball in Ubuntu 22.04, runs the checks and attaches the `.tar.gz` and
   `.sha256` to the release, after a minute or two. It creates the release if there is none (a pre-release for
   alpha / beta / rc) and fills in an empty title and notes from `CHANGELOG.md`.

The tag is always `v` + `VERSION` (other tags don't start the workflow). For a tag that already exists, use
*Actions → Release → Run workflow* and enter the tag.

## What the installer asks

1. **The beta notice.** Type `y` to go on.
2. **GPU memory to use** for swap. `auto` = all but 1 GiB, or all but 2 GiB / 30% if this GPU drives a screen.
3. **Compression ratio.** `3` = about 3× more fits; `0` = off.
4. **Switch the VRAM tier on** now and at every boot?
5. **Switch the guard on?** (desktops only)

On an upgrade it asks nothing about settings: yours are kept.

Afterwards, **log out and back in once** so GNOME loads the USM extension (Quick Settings menu). Until then the guard
only watches. Check with `usm status`.

## Installer options (unattended installs)

```bash
sudo ./install.sh --yes --tier on --guard on                  # no questions, switch both on
sudo ./install.sh --yes --vram-mb 4096 --compress 2 --tier on
sudo ./install.sh --yes --server --tier on                    # a machine without a desktop
sudo ./install.sh --yes --set GUARD_PAUSE_MAX_S=10 --set LOG_MAX_MB=100
```

| Option | Meaning |
|---|---|
| `-y`, `--yes` | No questions: accept the beta notice, use defaults plus the options given. Nothing is switched on unless `--tier on` / `--guard on`; an upgrade keeps what was on. |
| `--desktop` / `--server` | What this machine is. Default: desktop if a graphical desktop is installed. `server` skips the GNOME extension and doesn't offer the guard. |
| `--tier on\|off` | VRAM swap tier on (now and at every boot) or off |
| `--guard on\|off` | Guard on or off |
| `--vram-mb MiB\|auto` | GPU memory to use (`VRAM_MB`) |
| `--compress N` | 0 = off, 2..8 = compression ratio (`COMPRESS`) |
| `--guard-mode full\|protect` | `full` = may pause busy background apps; `protect` = only keeps the desktop in RAM |
| `--set KEY=VALUE` | Any setting, repeatable: see [configuration.md](configuration.md) |
| `--no-logger` | Don't switch the logger on |
| `--no-desktop` | Don't install the GNOME extension |
| `--build` | Compile the daemon even if a prebuilt one is there |
| `--test-no-gpu` | Allow installing without an NVIDIA GPU (test VMs, with `TEST_DEVICE_MEMORY`) |

## Change settings later

```bash
usm config show                         # all settings, with what they do (--all: also the advanced ones)
sudo usm config set VRAM_MB=3072 COMPRESS=2
sudo usm config set GUARD_EXCLUDE=terminal,code,steam,blender
sudo usm config reset GUARD_EXCLUDE     # back to the default
sudo usm config edit                    # in your editor; saved only if every value is valid
```

Values are checked before they are saved, and the guard and logger restart by themselves. VRAM tier settings take
effect after `usm off && usm on`, or a reboot. All settings: [configuration.md](configuration.md).

## Where things go

| What | Where |
|---|---|
| programs | `/usr/local/lib/usm/` |
| the `usm` command | `/usr/local/sbin/usm` |
| settings | `/etc/usm/usm.conf` |
| services | `/etc/systemd/system/usm-vram.service`, `usm-guard.service`, `usm-logger.service` (+ sleep hooks) |
| logs | `/var/log/usm/` (30 days, at most 300 MiB) |
| standby swap file, spill file | `/var/lib/usm-swap/` |
| GNOME extension | `/usr/local/share/gnome-shell/extensions/usm-desktop@usm/` |
| polkit rule (desktop switches without a password) | `/usr/share/polkit-1/actions/org.usm.switch.policy` |

USM doesn't change your fstab, kernel command line, sysctl files or swap files. zswap and swap readahead are only
changed while the tier is on, and restored when it goes off.

## Upgrade

Download or pull the new version and run its `install.sh` again. Your settings are kept (new settings are added with
their defaults), and a running guard and logger restart with the new code. The VRAM tier keeps running the old
daemon until `usm off && usm on` or the next boot. `usm version` tells you.

## Uninstall

```bash
sudo usm uninstall           # keeps /etc/usm/usm.conf and /var/log/usm
sudo usm uninstall --purge   # removes those too
```

The tier is switched off first: its data moves into RAM, or your disk swap. zswap and readahead are restored and no
app stays paused. From a source or release folder, `sudo ./uninstall.sh` does the same.

## Troubleshooting

- `usm check`: what works on this machine and why not.
- `usm on` fails: the reason is printed (the last journal lines). `journalctl -u usm-vram -u usm-vramd` has more.
- Guard says "observe only": the GNOME extension isn't active yet. Log out and in once, or run
  `gnome-extensions enable usm-desktop@usm`.
- For a bug report: `usm collect` packs logs and settings into `~/usm-logs-*.tar.gz` (look inside before sharing).
