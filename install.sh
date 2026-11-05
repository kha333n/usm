#!/bin/bash
# USM installer. Run it from the unpacked release (daemon prebuilt) or from the source code (it builds the
# daemon first: needs gcc and make). Upgrading = running a newer install.sh; your settings are kept.
#
#   sudo ./install.sh                  asks a few questions (after the beta notice)
#   sudo ./install.sh --yes [OPTIONS]  no questions; nothing is switched on unless you say so
#   sudo ./install.sh --help           all options
#
# What goes where: programs /usr/local/lib/usm, command /usr/local/sbin/usm, settings /etc/usm/usm.conf,
# services /etc/systemd/system/usm-*.service, logs /var/log/usm, GNOME extension
# /usr/local/share/gnome-shell/extensions/usm-desktop@usm, polkit rule /usr/share/polkit-1/actions/org.usm.switch.policy.
# Undo: sudo usm uninstall [--purge]   (or ./uninstall.sh)
set -euo pipefail
ROOT=$(cd "$(dirname "$0")" && pwd)
VERSION=$(cat "$ROOT/VERSION")
LIB=/usr/local/lib/usm
CMD=/usr/local/sbin/usm
CONF=/etc/usm/usm.conf
EXT=usm-desktop@usm
EXT_DIR=/usr/local/share/gnome-shell/extensions/$EXT
POLKIT=/usr/share/polkit-1/actions

usage() {
  cat <<EOF
USM $VERSION installer (BETA)

usage: sudo ./install.sh [OPTIONS]

  -y, --yes              no questions: accept the beta notice, use the defaults and the options given.
                         Nothing is switched on unless --tier on / --guard on (an upgrade keeps what was on).
  --desktop | --server   what this machine is (default: desktop if a graphical desktop is installed).
                         server = no GNOME extension, guard not offered (it manages desktop apps only).
  --tier on|off          the VRAM swap tier: switch on now and at every boot / switch off
  --guard on|off         the guard: switch on now and at every boot / switch off
  --vram-mb MiB|auto     GPU memory to use as swap (default auto: all but 1 GiB, or 2 GiB+ if it drives a screen)
  --compress N           0 = off, 2..8 = compress pages in VRAM, device = N x VRAM (default 3)
  --guard-mode full|protect
                         full = may briefly pause busy background apps; protect = only keeps the desktop in RAM
  --set KEY=VALUE        any setting (repeatable). All settings: docs/configuration.md or, after installing,
                         usm config show --all
  --no-logger            do not switch the logger on (usm report then has no data)
  --no-desktop           do not install the GNOME extension (Quick Settings menu + the guard's focus info)
  --build                compile the daemon even if a prebuilt one is there
  --test-no-gpu          allow installing without an NVIDIA GPU (test VMs; see TEST_DEVICE_MEMORY)
  -h, --help

After installing: usm status | usm check | usm config show | usm on / off | usm guard on / off
EOF
}

die() { echo "install.sh: $*" >&2; exit 1; }
say() { printf '%s\n' "$*"; }
YES=0 PROFILE=auto TIER=keep GUARD=keep LOGGER=on DESKTOP_EXT=yes BUILD=0 TEST=0
SETS=()
while [ $# -gt 0 ]; do
  case "$1" in --*=*) set -- "${1%%=*}" "${1#*=}" "${@:2}" ;; esac
  case "$1" in
    -y|--yes) YES=1 ;;
    --desktop) PROFILE=desktop ;;
    --server) PROFILE=server ;;
    --tier|--guard)
      [ $# -ge 2 ] && { [ "$2" = on ] || [ "$2" = off ]; } || die "$1 on|off"
      if [ "$1" = --tier ]; then TIER=$2; else GUARD=$2; fi; shift ;;
    --vram-mb) [ $# -ge 2 ] || die "--vram-mb MiB|auto"; SETS+=("VRAM_MB=$2"); shift ;;
    --compress) [ $# -ge 2 ] || die "--compress N"; SETS+=("COMPRESS=$2"); shift ;;
    --guard-mode) [ $# -ge 2 ] || die "--guard-mode full|protect"; SETS+=("GUARD_MODE=$2"); shift ;;
    --set) [ $# -ge 2 ] || die "--set KEY=VALUE"; SETS+=("$2"); shift ;;
    --no-logger) LOGGER=off ;;
    --no-desktop) DESKTOP_EXT=no ;;
    --build) BUILD=1 ;;
    --test-no-gpu) TEST=1 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
  shift
done
[ "$(id -u)" = 0 ] || die "run as root: sudo $0 $*"
command -v python3 > /dev/null || die "needs python3 (sudo apt install python3)"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || die "needs Python 3.10 or newer"
[ ${#SETS[@]} -eq 0 ] || python3 "$ROOT/tools/usm_config.py" validate "${SETS[@]}" || exit 1

# Questions go to the terminal even when the script itself comes through a pipe.
TTY=
if [ "$YES" = 0 ]; then
  if [ -t 0 ]; then TTY=/dev/stdin; elif [ -r /dev/tty ] && : < /dev/tty 2> /dev/null; then TTY=/dev/tty
  else die "no terminal for questions: run with --yes [options] (see --help)"; fi
fi
ask() {   # ask "question" default -> answer in $REPLY
  local q=$1 def=$2
  printf '%s [%s]: ' "$q" "$def"
  read -r REPLY < "$TTY" || REPLY=
  REPLY=${REPLY:-$def}
}
yes_no() {   # yes_no "question" Y|N -> 0 = yes
  local q=$1 def=$2 hint="[Y/n]"
  [ "$def" = N ] && hint="[y/N]"
  while true; do
    printf '%s %s ' "$q" "$hint"
    read -r REPLY < "$TTY" || REPLY=
    case "${REPLY:-$def}" in [Yy]|[Yy][Ee][Ss]) return 0 ;; [Nn]|[Nn][Oo]) return 1 ;; esac
  done
}

OLD=
[ -e "$LIB/VERSION" ] && OLD=$(cat "$LIB/VERSION")
[ -z "$OLD" ] && [ -e "$LIB/usm-vramd" ] && OLD="an earlier version"

cat <<EOF

  USM $VERSION - use your GPU's spare memory as fast swap, and keep the desktop responsive under memory overload

  !! BETA SOFTWARE - PLEASE READ !!
  - This is beta software in testing. It was written with the help of AI (Claude) and has been tested on a
    few machines only (Ubuntu 26.04, kernel 7.0, NVIDIA RTX 3050 and RTX 4060 laptops).
  - It works well there, but it changes how your system swaps memory. If the VRAM daemon crashes or the GPU
    resets, the data swapped into VRAM is lost and the apps it belongs to will crash.
  - Do NOT use it on critical systems (production servers, machines whose work you cannot lose).
  - No warranty (MIT license). You can remove it at any time: sudo usm uninstall

EOF
if [ "$YES" = 0 ]; then
  yes_no "I understand - install USM $VERSION${OLD:+ (upgrade from $OLD)}?" N || { say "nothing installed"; exit 1; }
fi

# ---- the daemon: prebuilt, or build it now ----
if [ "$BUILD" = 1 ] || [ ! -x "$ROOT/vram/usm-vramd" ]; then
  command -v make > /dev/null && { command -v cc > /dev/null || command -v gcc > /dev/null; } ||
    die "no prebuilt daemon here and no compiler: sudo apt install build-essential (or use a release download)"
  say "building the VRAM daemon (make)..."
  # Build as the owner of the source directory (no root-owned files in your checkout).
  owner=$(stat -c %U "$ROOT")
  run_as=()
  [ "$owner" != root ] && [ -n "${SUDO_USER:-}" ] && [ "$owner" = "$SUDO_USER" ] && run_as=(runuser -u "$owner" --)
  [ "$BUILD" = 1 ] && rm -f "$ROOT/vram/usm-vramd"
  ${run_as[@]+"${run_as[@]}"} make -C "$ROOT" -s > /dev/null 2>&1 || die "build failed - run make in $ROOT to see why"
fi
"$ROOT/vram/usm-vramd" --version > /dev/null 2>&1 ||
  die "vram/usm-vramd does not run on this system (too old?) - build it from source: sudo ./install.sh --build"

# ---- what this machine can do ----
eval "$(python3 "$ROOT/tools/usm_check.py" --shell)"
if [ "$CORE_OK" != 1 ]; then
  python3 "$ROOT/tools/usm_check.py" || true
  die "this system lacks something USM needs (see FAIL above)"
fi
if [ "$TIER_OK" != 1 ] && [ "$TEST" = 0 ]; then
  say "note: the VRAM swap tier cannot run here ($TIER_WHY)."
  say "      Installing the guard, logger and tools; the tier can be switched on later if that changes (usm check)."
  [ "$TIER" = on ] && die "--tier on: the VRAM tier is not available on this machine"
  TIER_POSSIBLE=0
else
  TIER_POSSIBLE=1
fi
if [ "$PROFILE" = auto ]; then
  if [ "$DESKTOP" = 1 ]; then PROFILE=desktop; else PROFILE=server; fi
fi
GNOME_OK=0
[ -n "$GNOME_VERSION" ] && [ "$GNOME_VERSION" -ge 45 ] && [ "$GNOME_VERSION" -le 50 ] && GNOME_OK=1

# ---- questions (fresh install only; an upgrade keeps everything) ----
if [ "$YES" = 0 ]; then
  say ""
  say "This machine: $(awk '/^MemTotal/ {printf "%.1f GiB RAM", $2 / 1048576}' /proc/meminfo)," \
      "${GPU_NAME:-no NVIDIA GPU}${GPU_TOTAL_MB:+ ($GPU_TOTAL_MB MiB)}, ${PROFILE}${GNOME_VERSION:+, GNOME $GNOME_VERSION}"
  if [ -e "$CONF" ]; then
    say "Your settings in $CONF are kept (change them later: usm config show / usm config set KEY=VALUE)."
  elif [ "$TIER_POSSIBLE" = 1 ]; then
    auto=$("$ROOT/vram/usm-vram-service" auto-size 2> /dev/null || true)
    say ""
    say "VRAM swap tier: GPU memory used as swap. Switch it off (usm off) before games / AI / GPU-heavy apps."
    while true; do
      ask "  GPU memory to use, in MiB (auto = ${auto:+$auto MiB now}${auto:-decided when the tier starts})" auto
      python3 "$ROOT/tools/usm_config.py" validate "VRAM_MB=$REPLY" && break
    done
    [ "$REPLY" = auto ] || SETS+=("VRAM_MB=$REPLY")
    while true; do
      ask "  Compress pages in VRAM? 0 = no, 2-8 = ratio (3: about 3x more fits, uses a little CPU)" 3
      python3 "$ROOT/tools/usm_config.py" validate "COMPRESS=$REPLY" && break
    done
    [ "$REPLY" = 3 ] || SETS+=("COMPRESS=$REPLY")
  fi
  if [ "$TIER_POSSIBLE" = 1 ] && [ "$TIER" = keep ] && ! systemctl is-enabled -q usm-vram.service 2> /dev/null; then
    yes_no "Switch the VRAM swap tier on now and at every boot?" Y && TIER=on || TIER=off
  fi
  if [ "$PROFILE" = desktop ] && [ "$GUARD" = keep ] && ! systemctl is-enabled -q usm-guard.service 2> /dev/null; then
    say ""
    say "Guard: under memory overload it pauses a busy background app for a few seconds (never the app in"
    say "front, apps you used in the last 5 minutes, apps playing sound, terminals, editors, calls, games)."
    [ "$GNOME_OK" = 1 ] || say "  (Without GNOME 45-50 it only watches and logs - it cannot see which app is in front.)"
    yes_no "Switch the guard on now and at every boot?" Y && GUARD=on || GUARD=off
  fi
fi

# ---- install the files ----
say ""
say "installing USM $VERSION..."
install -d -m 755 $LIB /etc/usm /var/log/usm
install -m 755 "$ROOT/vram/usm-vramd" "$ROOT/vram/usm-vramd-ctl" "$ROOT/vram/usm-vram-move.py" "$ROOT/vram/usm-vram-service" \
  "$ROOT/guard/usm-guard.py" "$ROOT/guard/usm-guard-service" \
  "$ROOT/tools/usm-logger.py" "$ROOT/tools/usm-tool.py" "$ROOT/tools/usm_config.py" "$ROOT/tools/usm_check.py" \
  "$ROOT/tools/usm-helper" "$ROOT/uninstall.sh" $LIB/
install -m 644 "$ROOT/tools/usm_log.py" "$ROOT/VERSION" "$ROOT/README.md" "$ROOT/LICENSE" $LIB/
install -d $LIB/docs && install -m 644 "$ROOT"/docs/*.md $LIB/docs/
install -m 755 "$ROOT/tools/usm" "$CMD"
for u in usm-vram usm-guard usm-logger; do
  install -m 644 "$ROOT/system/$u.service" /etc/systemd/system/$u.service
done
if [ -d $POLKIT ]; then
  # Desktop switches (Quick Settings): the user at the machine may run usm-helper's on/off actions without a password.
  install -m 644 "$ROOT/system/org.usm.switch.policy" $POLKIT/org.usm.switch.policy
fi
init_args=()
for s in ${SETS[@]+"${SETS[@]}"}; do init_args+=(--set "$s"); done
[ "$TEST" = 1 ] && [ "$TIER_OK" != 1 ] && [ ! -e "$CONF" ] && say "note: no GPU - set TEST_DEVICE_MEMORY for tests"
python3 $LIB/usm_config.py init ${init_args[@]+"${init_args[@]}"}

# Sleep: the tier goes off before suspend/hibernate and back on after (no-ops while it is off).
$LIB/usm-vramd-ctl hooks-install /etc/systemd/system
systemctl daemon-reload
if [ "$LOGGER" = on ]; then
  systemctl enable -q usm-logger.service
  systemctl restart usm-logger.service
fi
# An upgrade: a running guard restarts with the new code (an app it paused runs on meanwhile).
systemctl try-restart usm-guard.service

# ---- GNOME helper extension (desktop) ----
if [ "$PROFILE" = desktop ] && [ "$DESKTOP_EXT" = yes ]; then
  install -d $EXT_DIR
  install -m 644 "$ROOT/desktop/$EXT/metadata.json" "$ROOT/desktop/$EXT/extension.js" "$ROOT/desktop/$EXT/panel.js" $EXT_DIR/
  if [ "$GNOME_OK" = 1 ] && [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
    uid=$(id -u "$SUDO_USER")
    if [ -S "/run/user/$uid/bus" ]; then
      as_user() { runuser -u "$SUDO_USER" -- env DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" "$@"; }
      was=$(as_user gsettings get org.gnome.shell enabled-extensions 2> /dev/null || true)
      if ! as_user gnome-extensions enable $EXT 2> /dev/null; then
        cur=$(as_user gsettings get org.gnome.shell enabled-extensions 2> /dev/null || echo "@as []")
        case "$cur" in
          *"'$EXT'"*) ;;
          "@as []"|"[]") as_user gsettings set org.gnome.shell enabled-extensions "['$EXT']" ;;
          *) as_user gsettings set org.gnome.shell enabled-extensions "${cur%]}, '$EXT']" ;;
        esac
      fi
      case "$was" in
        *"'$EXT'"*) say "GNOME extension updated (GNOME loads the new version at the next login)" ;;
        *) say "GNOME extension $EXT enabled for $SUDO_USER - it starts at the next login (log out and in once)" ;;
      esac
    fi
  elif [ "$GNOME_OK" = 1 ]; then
    say "GNOME extension installed; enable it as the desktop user: gnome-extensions enable $EXT (then log out and in)"
  fi
fi

# ---- switch on / off as asked ----
if [ "$TIER" = on ]; then
  say "switching the VRAM swap tier on..."
  $CMD on || say "the VRAM tier did not start - see above; settings: usm config show; check: usm check"
elif [ "$TIER" = off ] && systemctl is-enabled -q usm-vram.service 2> /dev/null; then
  $CMD off
fi
if [ "$GUARD" = on ]; then
  $CMD guard on
elif [ "$GUARD" = off ] && systemctl is-enabled -q usm-guard.service 2> /dev/null; then
  $CMD guard off
fi
if systemctl is-active -q usm-vramd && [ -n "$OLD" ] && [ "$OLD" != "$VERSION" ]; then
  say "note: the VRAM tier keeps running the previous daemon until  usm off && usm on  (or the next boot)"
fi

say ""
say "USM $VERSION installed."
say "  usm status          what is on, memory and swap now"
say "  usm on | off        VRAM swap tier       usm guard on | off   the guard"
say "  usm config show     all settings         usm config set KEY=VALUE"
say "  usm check           what works on this machine"
say "  sudo usm uninstall  remove it again"
say "Docs and bug reports: https://github.com/kha333n/usm"
