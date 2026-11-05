#!/bin/bash
# usm-bench.sh: how a memory-starved app runs WITH the VRAM tier and WITHOUT it, on this machine.
#
# Contained: every test app runs in its own memory-limited scope in system.slice (not among the desktop's apps),
# so the rest of the machine keeps its RAM and the guard is not involved. Measured in each mode:
#   1. swap-in latency: page faults served by the swap device itself (zswap bypassed), 1 / 4 / 8 threads at once
#   2. an app coming back: an app pushed out to swap (zswap bypassed) reads all its memory again
#   3. a working set bigger than its RAM (zswap as configured): fill time, random touches per second (70% in a
#      hot 30%), coming back, and how the rest of the machine felt meanwhile (stall time, desktop lag)
# "without" = the VRAM tier off (usm off): your own zswap and disk swap, as before USM. The tier's state is
# restored at the end. Every page of every test is verified.
#
#   sudo tests/bench/usm-bench.sh [--quick] [--only with|without] [--out FILE.md]
#
# Needs: USM installed, root, a C compiler (two small test programs are built in a temporary folder).
# Takes ~10 minutes (--quick: ~4). Best on a calm machine: few apps open, little in swap (a fresh boot).
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
QUICK=0 ONLY= OUT=
while [ $# -gt 0 ]; do
  case "$1" in
    --quick) QUICK=1 ;;
    --only) ONLY=${2:?with|without}; shift ;;
    --out) OUT=${2:?file}; shift ;;
    -h|--help) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
  shift
done
[ "$(id -u)" = 0 ] || { echo "run as root: sudo $0" >&2; exit 1; }
command -v usm > /dev/null || { echo "USM is not installed" >&2; exit 1; }
CC=$(command -v cc || command -v gcc) || { echo "needs a C compiler (build-essential)" >&2; exit 1; }
if [ "$QUICK" = 1 ]; then LAT_MB=256 BACK_MB=512 WS_LIMIT=1024 WS_MB=2560 TOUCH=15
else LAT_MB=512 BACK_MB=2048 WS_LIMIT=2048 WS_MB=5120 TOUCH=30; fi
B=$(mktemp -d /tmp/usm-bench.XXXXXX)
"$CC" -O2 -Wall -pthread -o "$B/swapin-lat" "$HERE/swapin-lat.c"
"$CC" -O2 -Wall -o "$B/hog" "$ROOT/tests/desktop/hog.c"
TIER_WAS=off; systemctl is-active -q usm-vram.service && TIER_WAS=on
LOG=$B/log.txt

stop_scopes() {
  for u in $(systemctl list-units --type=scope --plain --no-legend 'usm-bench-*' | awk '{print $1}'); do
    systemctl stop "$u" 2> /dev/null || true
  done
}
finish() {
  stop_scopes
  if [ "$TIER_WAS" = on ] && ! systemctl is-active -q usm-vram.service; then
    echo "switching the VRAM tier back on"; usm on > /dev/null || echo "WARNING: could not switch the tier back on: usm on" >&2
  fi
}
trap finish EXIT
say() { echo "$*" | tee -a "$LOG"; }
mib() { awk -v k="$1" '$1 == k":" {print int($2 / 1024)}' /proc/meminfo; }
top_swap() { awk 'NR > 1 {print $5, $1}' /proc/swaps | sort -rn | sed -n 1p | cut -d' ' -f2; }
psi_total() { sed -n "s/^$2 .*total=\([0-9]*\).*/\1/p" "/proc/pressure/$1"; }
desk_file() { ls /run/user/*/usm-desktop 2> /dev/null | sed -n 1p; }
wait_for() {   # wait_for FILE REGEX SECONDS
  local i
  for ((i = 0; i < $3 * 10; i++)); do grep -q -E "$2" "$1" 2> /dev/null && return 0; sleep 0.1; done
  echo "timeout waiting for '$2' in $1" >&2; cat "$1" >&2; return 1
}
start_app() {   # start_app NAME OUTFILE PROPERTIES... -- COMMAND...
  local name=$1 out=$2; shift 2
  local props=()
  while [ "$1" != -- ]; do props+=(-p "$1"); shift; done
  shift
  systemd-run --scope -q --unit="usm-bench-$name" ${props[@]+"${props[@]}"} "$@" > "$out" 2>&1 &
}
cg() { echo "/sys/fs/cgroup/system.slice/usm-bench-$1.scope"; }

# ---- the machine ----
dmi() { cat "/sys/class/dmi/id/$1" 2> /dev/null || true; }
swapdisks=$(awk 'NR > 1 && $1 !~ /ublkb/ {print $1}' /proc/swaps | while read -r s; do
  src=$s; [ -b "$s" ] || src=$(findmnt -no SOURCE -T "$s" 2> /dev/null || true)
  pk=$(lsblk -no PKNAME "$src" 2> /dev/null | sed -n 1p); [ -n "$pk" ] || pk=$(basename "$src")
  echo "$s on $(lsblk -dno MODEL "/dev/$pk" 2> /dev/null | sed 's/ *$//') ($(lsblk -dno ROTA "/dev/$pk" 2> /dev/null | grep -q 1 && echo HDD || echo SSD))"
done | paste -sd ';' | sed 's/;/; /g')
SYS_MACHINE="$(dmi sys_vendor) $(dmi product_version) ($(dmi product_name))"
SYS_CPU="$(lscpu | sed -n 's/^Model name: *//p' | sed -n 1p), $(nproc) threads"
SYS_RAM="$(awk '/^MemTotal/ {printf "%.1f GiB", $2 / 1048576}' /proc/meminfo)"
SYS_GPU="$(nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader 2> /dev/null | sed -n 1p | sed 's/, / /; s/, / MiB, driver /; s/ MiB MiB/ MiB/')"
SYS_OS="$(. /etc/os-release; echo "$PRETTY_NAME"), kernel $(uname -r)"
SYS_USM="$(usm version | sed -n 1p)"
say "USM benchmark $(date '+%Y-%m-%d %H:%M') ($([ "$QUICK" = 1 ] && echo quick || echo full))"
say "machine: $SYS_MACHINE | $SYS_CPU | $SYS_RAM | ${SYS_GPU:-no NVIDIA GPU} | $SYS_OS | $SYS_USM"
say "disk swap: ${swapdisks:-none}"

declare -A R
mode_setup() {   # mode_setup with|without
  if [ "$1" = with ]; then
    systemctl is-active -q usm-vram.service || usm on > /dev/null
    case "$(top_swap)" in /dev/ublkb*) ;; *) echo "the VRAM tier is not the top swap device" >&2; exit 1 ;; esac
  else
    if systemctl is-active -q usm-vramd; then
      local used avail
      used=$(awk '$1 ~ /ublkb/ {print int($4 / 1024)}' /proc/swaps); avail=$(mib MemAvailable)
      if [ "${used:-0}" -gt $(( avail - 1536 )) ]; then
        echo "the VRAM tier holds ${used} MiB of your apps' memory and only ${avail} MiB of RAM are available:" >&2
        echo "switching it off now would push that to the disk. Close some apps (or reboot) and run again." >&2
        exit 1
      fi
      say "switching the VRAM tier off (moving ${used:-0} MiB into RAM)..."
      usm off > /dev/null
      local i; for ((i = 0; i < 600; i++)); do grep -q ublkb /proc/swaps || break; sleep 1; done
      grep -q ublkb /proc/swaps && { echo "the tier did not go off" >&2; exit 1; }
    fi
  fi
  local z; z=$(cat /sys/module/zswap/parameters/enabled 2> /dev/null || echo N)
  R[$1,config]="zswap $([ "$z" = Y ] && echo "$(cat /sys/module/zswap/parameters/max_pool_percent)% ($(cat /sys/module/zswap/parameters/compressor))" || echo off), readahead $(sysctl -n vm.page-cluster), top swap $(top_swap)"
  say ""
  say "=== $1 USM: ${R[$1,config]}"
}

test_latency() {   # 1: swap-in latency of the device (zswap bypassed)
  local m=$1 t o
  for t in 1 4 8; do
    o=$B/$m-lat$t.txt
    systemd-run --scope -q --unit="usm-bench-lat$t" -p MemoryZSwapMax=0 "$B/swapin-lat" "$LAT_MB" "$t" > "$o" 2>&1 || true
    cat "$o" >> "$LOG"
    R[$m,lat$t]=$(sed -n 's/.*median \([0-9.]*\) us .*p99 \([0-9.]*\) us.*/\1 µs (p99 \2)/p' "$o")
    R[$m,fps$t]=$(sed -n 's/.*(\([0-9]*\) page faults\/s).*/\1/p' "$o")
    grep -q 'every page correct' "$o" || R[$m,bad]="latency test $t: $(grep 'data check' "$o")"
    say "  swap-in, $t thread(s): ${R[$m,lat$t]:-?}, ${R[$m,fps$t]:-?} faults/s"
  done
}

test_back() {   # 2: app coming back after being pushed out (zswap bypassed)
  local m=$1 o=$B/$m-back.txt c i cur t0 t1
  start_app back "$o" MemoryZSwapMax=0 -- "$B/hog" "$BACK_MB" mixed
  wait_for "$o" filled 120
  c=$(cg back)
  for ((i = 0; i < 300; i++)); do
    cur=$(cat "$c/memory.current"); [ "$cur" -lt $(( 32 << 20 )) ] && break
    echo "$cur" > "$c/memory.reclaim" 2> /dev/null || true
    sleep 0.1
  done
  R[$m,pushed]=$(( $(cat "$c/memory.swap.current") >> 20 ))
  t0=$(date +%s.%N)
  systemctl kill -s USR1 --kill-whom=all usm-bench-back.scope
  wait_for "$o" 'verify:' 600
  t1=$(date +%s.%N)
  R[$m,back_s]=$(awk -v a="$t0" -v b="$t1" 'BEGIN {printf "%.1f", b - a}')
  R[$m,back_mbs]=$(awk -v a="$t0" -v b="$t1" -v mb="$BACK_MB" 'BEGIN {printf "%.0f", mb / (b - a)}')
  grep -q ' 0 bad' "$o" || R[$m,bad]="app coming back: $(grep verify "$o")"
  cat "$o" >> "$LOG"
  say "  app coming back: ${R[$m,pushed]} MiB were in swap; all $BACK_MB MiB read again in ${R[$m,back_s]} s (${R[$m,back_mbs]} MiB/s)"
  systemctl stop usm-bench-back.scope
}

test_workset() {   # 3: working set bigger than its RAM (zswap as configured)
  local m=$1 o=$B/$m-ws.txt t0 t1 s0 f0 i0 s1 f1 i1 lag=0 l df tt
  df=$(desk_file)
  t0=$(date +%s.%N)
  start_app ws "$o" "MemoryMax=${WS_LIMIT}M" -- env TOUCH_S="$TOUCH" "$B/hog" "$WS_MB" mixed
  wait_for "$o" filled 900
  t1=$(date +%s.%N)
  R[$m,fill_s]=$(awk -v a="$t0" -v b="$t1" 'BEGIN {printf "%.1f", b - a}')
  s0=$(psi_total memory some); f0=$(psi_total memory full); i0=$(psi_total io some); tt=$(date +%s%N)
  systemctl kill -s USR2 --kill-whom=all usm-bench-ws.scope
  while ! grep -q 'touch:' "$o"; do
    systemctl is-active -q usm-bench-ws.scope || { echo "the test app stopped:" >&2; cat "$o" >&2; exit 1; }
    if [ -n "$df" ]; then
      l=$(sed -n 's/^lag_max_ms=//p' "$df" 2> /dev/null); l=${l%%.*}
      [ "${l:-0}" -gt "$lag" ] 2> /dev/null && lag=$l
    fi
    sleep 0.5
  done
  s1=$(psi_total memory some); f1=$(psi_total memory full); i1=$(psi_total io some); tt=$(( ($(date +%s%N) - tt) / 1000 ))
  R[$m,touch]=$(sed -n 's/.*(\([0-9]*\)\/s).*/\1/p' "$o")
  R[$m,stall]=$(awk -v a="$s0" -v b="$s1" -v c="$f0" -v d="$f1" -v e="$i0" -v f="$i1" -v t="$tt" \
    'BEGIN {printf "memory %.0f%% / all tasks %.0f%% / I/O %.0f%%", (b - a) * 100 / t, (d - c) * 100 / t, (f - e) * 100 / t}')
  R[$m,lag]=$([ -n "$df" ] && echo "$lag ms" || echo "n/a")
  t0=$(date +%s.%N)
  systemctl kill -s USR1 --kill-whom=all usm-bench-ws.scope
  wait_for "$o" 'verify:' 900
  t1=$(date +%s.%N)
  R[$m,wsback_s]=$(awk -v a="$t0" -v b="$t1" 'BEGIN {printf "%.1f", b - a}')
  grep -q ' 0 bad' "$o" || R[$m,bad]="working set: $(grep verify "$o")"
  cat "$o" >> "$LOG"
  say "  working set $WS_MB MiB in $WS_LIMIT MiB of RAM: filled in ${R[$m,fill_s]} s; ${R[$m,touch]:-?} touches/s;" \
      "read again in ${R[$m,wsback_s]} s; stalled meanwhile (memory / all / I/O): ${R[$m,stall]}; desktop lag max ${R[$m,lag]}"
  systemctl stop usm-bench-ws.scope
}

MODES=(with without)
[ -n "$ONLY" ] && MODES=("$ONLY")
for m in "${MODES[@]}"; do
  mode_setup "$m"
  test_latency "$m"
  test_back "$m"
  test_workset "$m"
done
finish; trap - EXIT

# ---- the table ----
v() { echo "${R[$1,$2]:-–}"; }
vs() { if [ -n "${R[$1,$2]:-}" ]; then echo "${R[$1,$2]} s"; else echo "–"; fi; }
# Green / red arrows for GitHub (LaTeX): the arrow = the number went up / down, green = better, red = worse.
ratio() {   # ratio WITHOUT WITH higher|lower -> "<arrow> N× better" (WITH compared with WITHOUT)
  awk -v a="$1" -v b="$2" -v h="$3" 'BEGIN { if (a + 0 <= 0 || b + 0 <= 0) exit;
    r = (h == "higher") ? b / a : a / b; up = (b > a);
    arrow = sprintf("$\\color{%s}\\%sarrow$", r >= 1 ? "green" : "red", up ? "up" : "down");
    if (r >= 1.05) printf "%s %.1f× better", arrow, r; else if (r <= 0.95) printf "%s %.1f× worse", arrow, 1 / r; else printf "same" }'
}
num() { echo "${1%% *}"; }
{
  echo "### $SYS_MACHINE, $(date +%Y-%m-%d)"
  echo
  echo "- **CPU:** $SYS_CPU; **RAM:** $SYS_RAM"
  echo "- **GPU:** ${SYS_GPU:-none}"
  echo "- **System:** $SYS_OS; $SYS_USM"
  echo "- **Disk swap:** ${swapdisks:-none}"
  echo "- **Without USM:** $(v without config)"
  echo "- **With USM:** $(v with config)"
  echo "- Test sizes: latency $LAT_MB MiB, app coming back $BACK_MB MiB, working set $WS_MB MiB in $WS_LIMIT MiB of RAM, $TOUCH s of touches$([ "$QUICK" = 1 ] && echo " (quick run)")"
  echo
  echo "| Test | without USM | with USM | |"
  echo "|---|---|---|---|"
  for t in 1 4 8; do
    echo "| Swap-in latency, $t app thread$([ $t = 1 ] || echo s) faulting, median | $(v without lat$t) | $(v with lat$t) | $(ratio "$(num "$(v without lat$t)")" "$(num "$(v with lat$t)")" lower) |"
  done
  echo "| Swap-ins per second, 8 threads | $(v without fps8) | $(v with fps8) | $(ratio "$(v without fps8)" "$(v with fps8)" higher) |"
  echo "| App coming back: $BACK_MB MiB read again | $(vs without back_s) | $(vs with back_s) | $(ratio "$(v without back_s)" "$(v with back_s)" lower) |"
  echo "| Working set: filling $WS_MB MiB into $WS_LIMIT MiB | $(vs without fill_s) | $(vs with fill_s) | $(ratio "$(v without fill_s)" "$(v with fill_s)" lower) |"
  echo "| Working set: random touches per second | $(v without touch) | $(v with touch) | $(ratio "$(v without touch)" "$(v with touch)" higher) |"
  echo "| Working set: all of it read again | $(vs without wsback_s) | $(vs with wsback_s) | $(ratio "$(v without wsback_s)" "$(v with wsback_s)" lower) |"
  echo "| Whole machine during the touches: share of time stalled (memory / all tasks / I/O incl. swap) | $(v without stall) | $(v with stall) | |"
  echo "| Desktop lag during the touches, worst (GNOME Shell) | $(v without lag) | $(v with lag) | |"
  echo "| Data check (every page) | ${R[without,bad]:-all correct} | ${R[with,bad]:-all correct} | |"
} > "$B/table.md"
echo
# On the terminal: plain arrows instead of the LaTeX ones.
sed -E 's/\$\\color\{(green|red)\}\\uparrow\$/↑/g; s/\$\\color\{(green|red)\}\\downarrow\$/↓/g' "$B/table.md"
if [ -n "$OUT" ]; then
  cp "$B/table.md" "$OUT"; { echo; echo '<details><summary>raw output</summary>'; echo; echo '```'; cat "$LOG"; echo '```'; echo '</details>'; } >> "$OUT"
  [ -n "${SUDO_USER:-}" ] && chown "$SUDO_USER:" "$OUT"
  echo; echo "written: $OUT"
fi
rm -rf "$B"
