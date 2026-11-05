#!/bin/bash
# pressure-test.sh LABEL [MB_EACH=4500] [mixed|random]: two hogs (app scopes) against the real desktop, then
# verify both; prints what usm-logger saw in that window. For comparing settings (zswap etc.).
set -u
L=$1; MB=${2:-4500}; MODE=${3:-mixed}
S=${SCRATCH:-/tmp}; D=$(cd "$(dirname "$0")" && pwd)
t0=$(date +%s.%N)
systemd-run --user --scope --slice=app.slice --unit=app-usmtest-c.scope "$D/hog" $MB $MODE > $S/hogC.out 2>&1 &
until grep -q filled $S/hogC.out 2>/dev/null; do sleep 0.2; done
t1=$(date +%s.%N)
systemd-run --user --scope --slice=app.slice --unit=app-usmtest-d.scope "$D/hog" $MB $MODE > $S/hogD.out 2>&1 &
until grep -q filled $S/hogD.out 2>/dev/null; do sleep 0.2; done
t2=$(date +%s.%N)
sleep 15
PC=$(grep -o 'hog [0-9]*' $S/hogC.out | cut -d' ' -f2); PD=$(grep -o 'hog [0-9]*' $S/hogD.out | cut -d' ' -f2)
t3=$(date +%s.%N); kill -USR1 $PC; until grep -q verify $S/hogC.out; do sleep 0.1; done; t4=$(date +%s.%N)
kill -USR1 $PD; until grep -q verify $S/hogD.out; do sleep 0.1; done; t5=$(date +%s.%N)
zs=$(awk '/^Zswap:/{z=$2} /^Zswapped:/{d=$2} END{printf "%d/%d", z/1024, d/1024}' /proc/meminfo)
vr=$(awk '/ublkb/{print int($4/1024)}' /proc/swaps)
systemctl --user stop app-usmtest-c.scope app-usmtest-d.scope
sleep 3
python3 - "$L" "$t0" "$t1" "$t2" "$t3" "$t4" "$t5" "$(tail -1 $S/hogC.out) / $(tail -1 $S/hogD.out)" "$zs" "$vr" <<'PY'
import csv, glob, sys
L, t0, t1, t2, t3, t4, t5 = sys.argv[1], *map(float, sys.argv[2:8])
check, zs, vr = sys.argv[8:11]
rows = [r for f in sorted(glob.glob("/var/log/usm/samples-*.csv"))[-1:] for r in csv.DictReader(open(f)) if t0 <= float(r["t"]) <= t5 + 1]
s = lambda k: sum(int(r[k] or 0) for r in rows)
print(f"{L:28s} fill C {t1-t0:4.1f} s, fill D {t2-t1:5.1f} s, verify C {t4-t3:5.1f} s, D {t5-t4:5.1f} s | everything waited {s('mem_full_ms')/1000:5.1f} s,"
      f" desktop shell waited {s('sess_mem_full_ms')/1000:4.1f} s, logger late max {max(int(r['lag_ms']) for r in rows)} ms,"
      f" RAM avail min {min(int(r['avail_mb']) for r in rows)} MB | at end zswap pool/data {zs} MB, VRAM tier {vr} MB | {check}")
PY
