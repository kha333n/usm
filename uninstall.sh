#!/bin/bash
# Remove USM: switches the VRAM tier off first (its data goes back to RAM / disk swap; zswap and swap readahead
# are restored) and the guard (no app stays paused). Keeps /etc/usm/usm.conf and the logs in /var/log/usm
# unless --purge.   sudo usm uninstall [--purge]   or, from the source / release folder: sudo ./uninstall.sh
set -u
[ "$(id -u)" = 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
LIB=/usr/local/lib/usm
EXT=usm-desktop@usm
if systemctl is-active -q usm-vram.service || systemctl is-active -q usm-vramd; then
  echo "switching the VRAM tier off (moving its data out of VRAM)..."
  systemctl disable -q usm-vram.service 2>/dev/null
  systemctl stop usm-vram.service 2>/dev/null
  if systemctl is-active -q usm-vramd; then
    $LIB/usm-vramd-ctl off || { echo "the tier could not be switched off - not uninstalling (try again later)" >&2; exit 1; }
  fi
fi
systemctl stop usm-vramd-drain.service 2>/dev/null
# Standby swap files the tier left on (they held pages while RAM was full): their pages go to RAM / disk swap.
SD=$(sed -n 's/^SPILL_DIR=//p' /etc/usm/usm.conf 2>/dev/null); SD=$(realpath -m "${SD:-/var/lib/usm-swap}")
for f in "$SD"/standby-*.swap; do
  [ -e "$f" ] || continue
  if grep -q "^$f " /proc/swaps; then echo "switching the standby swap $f off..."; swapoff "$f" || continue; fi
  rm -f "$f"
done
systemctl disable -q --now usm-guard.service usm-logger.service 2>/dev/null
systemctl disable -q usm-vram.service 2>/dev/null
[ -x $LIB/usm-vramd-ctl ] && $LIB/usm-vramd-ctl hooks-remove /etc/systemd/system
rm -f /etc/systemd/system/usm-vram.service /etc/systemd/system/usm-guard.service /etc/systemd/system/usm-logger.service
rm -f /usr/local/sbin/usm /run/usm-guard.state /run/usm-guard.pause
rm -rf $LIB /usr/local/share/gnome-shell/extensions/$EXT
rm -f /usr/share/polkit-1/actions/org.usm.switch.policy
# The GNOME helper: take it out of the desktop user's enabled list.
if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
  uid=$(id -u "$SUDO_USER")
  if [ -S "/run/user/$uid/bus" ]; then
    as_user() { runuser -u "$SUDO_USER" -- env DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" "$@"; }
    as_user gnome-extensions disable $EXT 2>/dev/null
    cur=$(as_user gsettings get org.gnome.shell enabled-extensions 2>/dev/null || true)
    case "$cur" in
      *"'$EXT'"*)
        new=$(echo "$cur" | sed "s/, '$EXT'//; s/'$EXT', //; s/'$EXT'//")
        as_user gsettings set org.gnome.shell enabled-extensions "$new" ;;
    esac
  fi
fi
if [ "${1:-}" = --purge ]; then
  rm -rf /etc/usm /var/log/usm
  rm -f "$SD/vram-spill"
  rmdir "$SD" 2>/dev/null
fi
systemctl daemon-reload
echo "USM uninstalled$([ "${1:-}" = --purge ] && echo " (settings and logs removed)" || echo " (kept: /etc/usm/usm.conf, /var/log/usm - remove with --purge)")"
