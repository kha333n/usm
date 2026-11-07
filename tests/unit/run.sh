#!/bin/bash
# Offline checks (no root, no GPU, changes nothing on the system): what CI runs. From the repository: make check
set -euo pipefail
cd "$(dirname "$0")/../.."
fail=0
ok() { printf '  ok    %s\n' "$*"; }
bad() { printf '  FAIL  %s\n' "$*"; fail=1; }
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT

echo "shell scripts (bash -n)"
for f in install.sh uninstall.sh tools/usm tools/usm-helper vram/usm-vramd-ctl vram/usm-vram-service guard/usm-guard-service \
         packaging/*.sh tests/unit/run.sh tests/desktop/*.sh; do
  bash -n "$f" && ok "$f" || bad "$f"
done
if command -v shellcheck > /dev/null; then
  shellcheck -S error install.sh uninstall.sh tools/usm vram/usm-vramd-ctl vram/usm-vram-service && ok "shellcheck (errors)" || bad "shellcheck"
fi

echo "python (compile)"
for f in tools/*.py guard/*.py vram/*.py desktop/app/*.py tests/desktop/*.py tests/spill/*.py; do
  python3 -c 'import ast, sys; ast.parse(open(sys.argv[1]).read(), sys.argv[1])' "$f" && ok "$f" || bad "$f"
done

echo "daemon"
if [ -x vram/usm-vramd ]; then
  v=$(vram/usm-vramd --version) && [ "$v" = "usm-vramd $(cat VERSION)" ] && ok "--version: $v" || bad "--version: ${v:-}"
  p=$(vram/usm-vramd probe || true)
  case "$p" in lz4=*" cuda="*) ok "probe: $p" ;; *) bad "probe: $p" ;; esac
  ldd vram/usm-vramd | grep -v -E 'linux-vdso|libc\.so|ld-linux|libdl|libpthread' | grep . && bad "links more than libc" || ok "links only libc"
else
  bad "vram/usm-vramd not built (make)"
fi

echo "settings (tools/usm_config.py)"
C="python3 tools/usm_config.py --conf $T/usm.conf"
$C init --set VRAM_MB=4096 --set COMPRESS=2 > /dev/null && ok "init new file" || bad "init new file"
[ "$($C get VRAM_MB)" = 4096 ] && [ "$($C get COMPRESS)" = 2 ] && [ "$($C get GUARD_MODE)" = full ] && ok "get" || bad "get"
$C check > /dev/null && ok "new file valid" || bad "new file invalid"
grep -v '^LOG_MAX_MB=\|^GUARD_RECENT_S=' "$T/usm.conf" | sed 's/^GUARD_MODE=.*/GUARD_MODE=protect/' > "$T/old.conf"
python3 tools/usm_config.py --conf "$T/old.conf" init > /dev/null
[ "$(python3 tools/usm_config.py --conf "$T/old.conf" get GUARD_MODE)" = protect ] && grep -q '^LOG_MAX_MB=300$' "$T/old.conf" \
  && grep -q '^GUARD_RECENT_S=300$' "$T/old.conf" && ok "upgrade keeps values, adds missing keys" || bad "upgrade"
for v in VRAM_MB=abc COMPRESS=9 GUARD_MODE=x SPILL_DIR=relative 'GUARD_EXCLUDE=a b' NOPE=1; do
  python3 tools/usm_config.py validate "$v" 2> /dev/null && bad "accepted $v" || ok "rejects $v"
done
python3 tools/usm_config.py validate VRAM_MB=auto ZSWAP_POOL_WITH_TIER=keep GUARD_TRIGGER=0.25 LOG_INTERVAL=0.5 && ok "accepts valid values" || bad "valid values"
# The file must be readable by systemd as an EnvironmentFile: only comments, blank lines and KEY=VALUE.
grep -v -E '^(#.*|[A-Z][A-Z0-9_]*=[^ "'"'"']*|)$' "$T/usm.conf" && bad "file has lines systemd would misread" || ok "EnvironmentFile format"
python3 tools/usm_config.py doc | diff -q - docs/configuration.md > /dev/null && ok "docs/configuration.md is current" \
  || bad "docs/configuration.md is out of date: python3 tools/usm_config.py doc > docs/configuration.md"

echo "machine check (tools/usm_check.py --shell)"
out=$(python3 tools/usm_check.py --shell) && echo "$out" | grep -q '^CORE_OK=' && ok "runs" || bad "usm_check --shell"

echo "live view (tools/usm_dash.py)"
out=$(COLUMNS=100 LINES=60 timeout 30 python3 tools/usm-tool.py dash 1 < /dev/null 2>&1) &&
  grep -q '^SWAP ' <<< "$out" && grep -q 'now$' <<< "$out" && ok "one picture (piped)" || bad "usm dash: ${out:0:400}"
python3 - <<'PY' && ok "latency: exact, rough (~), upper bound (<), looking back" || bad "latency rules"
import sys
sys.path.insert(0, "tools")
import usm_dash as d
s = d.Sampler(300)
def rec(t, rio, rms):
    return {"t": t, "dev": {"x": (rio, rms, 0, 0)}}
s.hist.extend([rec(1000, 1000, 20)])
assert s.latency("x") == (20.0, ""), s.latency("x")
s.hist.clear(); s.hist.extend([rec(1000, 100, 2)])
assert s.latency("x") == (20.0, "~"), s.latency("x")
s.hist.clear(); s.hist.extend([rec(1000, 4, 0)])
assert s.latency("x") == (250.0, "<"), s.latency("x")
s.hist.clear(); s.hist.extend(rec(1000 + i, 100, 1) for i in range(30))
assert s.latency("x") == (10.0, ""), s.latency("x")          # 10 s back: 1000 reads, 10 ms
s.hist.clear(); s.hist.extend(rec(1000 + i, 1, 0) for i in range(30))
assert s.latency("x") == (1000 / 16, "<"), s.latency("x")    # 15 s back at most
s.hist.clear(); s.hist.extend([rec(1000, 0, 0)])
assert s.latency("x") == (None, ""), s.latency("x")
assert d.chart_rows([0, 0.01, 2.5, 10, None, d.FEW], 2, 10, d.Style(False, True)) == [" " * 3 + "█" + " " * 2, " ▁▄█ ·"]
PY

echo "app (desktop/app)"
if command -v desktop-file-validate > /dev/null; then
  desktop-file-validate desktop/app/com.kha333n.USM.desktop && ok "desktop file" || bad "desktop file"
fi
if command -v xvfb-run > /dev/null && command -v dbus-run-session > /dev/null && python3 -c 'import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1"); gi.require_version("Gsk", "4.0")' 2> /dev/null; then
  out=$(env -u WAYLAND_DISPLAY -u DBUS_SESSION_BUS_ADDRESS GDK_BACKEND=x11 GDK_DEBUG=no-portals NO_AT_BRIDGE=1 timeout 90 \
    dbus-run-session -- xvfb-run -a -s "-screen 0 1400x1100x24" python3 desktop/app/usm-app.py --screenshot "$T/app.png" \
    --size 1300x1000 --wait 2.5 2>&1)
  if [ -s "$T/app.png" ] && ! grep -q Traceback <<< "$out"; then ok "renders (Xvfb), no Python errors"; else bad "app: ${out: -600}"; fi
else
  printf '  skip  %s\n' "app render test (needs xvfb-run, dbus-run-session and GTK 4 + libadwaita for Python)"
fi

echo "installer"
./install.sh --help > /dev/null && ok "install.sh --help" || bad "install.sh --help"
[ "$fail" = 0 ] && echo "all checks passed" || { echo "SOME CHECKS FAILED"; exit 1; }
