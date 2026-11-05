#!/bin/bash
# touch-test.sh LABEL [MB_EACH=5000] [SECONDS=40]: two hogs (3:1 data, app scopes) fill memory, then both touch
# random pages (70% hot 30%) at once; prints touches/s and what usm-logger saw. For comparing zswap/readahead.
set -u
L=$1; MB=${2:-5000}; SEC=${3:-40}
S=${SCRATCH:-/tmp}; D=$(cd "$(dirname "$0")" && pwd)
# Same load every run: first wait until zswap's pool is under its (maybe just lowered) limit, then
# MB=auto: both hogs together = RAM available now + OVER (5000) MB.
lim=$(( $(awk '/MemTotal/{print $2}' /proc/meminfo) * $(cat /sys/module/zswap/parameters/max_pool_percent) / 100 ))
for _ in $(seq 60); do [ "$(awk '/^Zswap:/{print $2}' /proc/meminfo)" -le "$lim" ] && break; sleep 1; done
avail=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo)
[ "$MB" = auto ] && MB=$(( (avail + ${OVER:-5000}) / 2 ))
L="$L (avail $avail MB, 2x$MB)"
rm -f $S/hogC.out $S/hogD.out
for h in C D; do
  TOUCH_S=$SEC systemd-run --user --scope --slice=app.slice --unit=app-usmtest-${h,,}.scope -E TOUCH_S=$SEC "$D/hog" $MB mixed > $S/hog$h.out 2>&1 &
  until grep -q filled $S/hog$h.out 2>/dev/null; do sleep 0.2; done
done
PC=$(grep -o 'hog [0-9]*' $S/hogC.out | cut -d' ' -f2); PD=$(grep -o 'hog [0-9]*' $S/hogD.out | cut -d' ' -f2)
sleep 5
t0=$(date +%s.%N); kill -USR2 $PC $PD
until grep -q touch: $S/hogC.out && grep -q touch: $S/hogD.out; do sleep 0.5; done
t1=$(date +%s.%N)
kill -USR1 $PC $PD; until grep -q verify $S/hogC.out && grep -q verify $S/hogD.out; do sleep 0.5; done
systemctl --user stop app-usmtest-c.scope app-usmtest-d.scope
python3 - "$L" "$t0" "$t1" "$(grep touch: $S/hogC.out | awk '{print $2}')" "$(grep touch: $S/hogD.out | awk '{print $2}')" "$(grep verify $S/hogC.out) / $(grep verify $S/hogD.out)" <<'PY'
import csv, glob, sys
L, t0, t1, nc, nd, check = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5]), sys.argv[6]
rows = [r for f in sorted(glob.glob("/var/log/usm/samples-*.csv"))[-1:] for r in csv.DictReader(open(f)) if t0 <= float(r["t"]) <= t1]
s = lambda k: sum(int(r[k] or 0) for r in rows)
mx = lambda k: max(int(r[k] or 0) for r in rows)
dt = t1 - t0
print(f"{L:34s} {(nc + nd) / dt / 1e6:6.2f} M touches/s | waited: all {s('mem_full_ms') / dt / 10:4.1f}% of the time, desktop shell "
      f"{s('sess_mem_full_ms') / 1000:4.1f} s; desktop lag max {mx('shell_lag_ms')} ms | swap-in {s('pswpin') * 4 / 1024 / dt:5.0f} MiB/s, "
      f"zswap in {s('zswpin') * 4 / 1024 / dt:5.0f} MiB/s | cpu {sum(int(r['cpu_pct'] or 0) for r in rows) / max(len(rows), 1):.0f}% | "
      f"{'data ok' if check.count(' 0 bad') == 2 else check}")
PY
