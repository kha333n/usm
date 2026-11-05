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
for f in tools/*.py guard/*.py vram/*.py tests/desktop/*.py tests/spill/*.py; do
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

echo "installer"
./install.sh --help > /dev/null && ok "install.sh --help" || bad "install.sh --help"
[ "$fail" = 0 ] && echo "all checks passed" || { echo "SOME CHECKS FAILED"; exit 1; }
