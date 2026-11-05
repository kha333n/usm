#!/usr/bin/env python3
"""USM settings: /etc/usm/usm.conf.

One place for every setting: its default, what values it takes, what it does and which part of USM
reads it. Used by install.sh (create / upgrade the file), by `usm config ...` and to write
docs/configuration.md. The file itself is plain KEY=VALUE lines (systemd reads it as an
EnvironmentFile), so it can also be edited by hand: `usm config edit` checks it afterwards.

  usm_config.py init [--conf FILE] [--set KEY=VALUE ...]   create the file, or add settings a newer
                                                           version knows; existing values are kept
  usm_config.py show [--all]          current settings (--all: also the advanced ones)
  usm_config.py get KEY
  usm_config.py set KEY=VALUE ...     change, check, and apply (restarts the guard / logger if running)
  usm_config.py reset KEY ...         back to the default
  usm_config.py edit                  edit it in $EDITOR; saved only if valid, then applied
  usm_config.py check                 is the file valid?
  usm_config.py validate KEY=VALUE    only check values (no file)
  usm_config.py doc                   the settings reference as Markdown
"""
import os
import re
import subprocess
import sys
import tempfile
import textwrap

CONF = os.environ.get("USM_CONF", "/etc/usm/usm.conf")
HERE = os.path.dirname(os.path.realpath(__file__))


def version():
    for p in (os.path.join(HERE, "VERSION"), os.path.join(HERE, "..", "VERSION")):
        try:
            return open(p).read().strip()
        except OSError:
            pass
    return "dev"


# ---- value checks: each returns None (fine) or what is wrong ----
def integer(lo, hi, *words):
    def check(v):
        if v in words:
            return None
        if not re.fullmatch(r"\d+", v):
            return f"a whole number from {lo} to {hi}" + (f", or {' / '.join(words)}" if words else "")
        if not lo <= int(v) <= hi:
            return f"from {lo} to {hi}" + (f", or {' / '.join(words)}" if words else "")
        return None
    check.kind = f"{lo}..{hi}" + "".join(f" or {w}" for w in words)
    return check


def number(lo, hi, *words):
    def check(v):
        if v in words:
            return None
        try:
            f = float(v)
        except ValueError:
            return f"a number from {lo} to {hi}"
        return None if lo <= f <= hi else f"from {lo} to {hi}"
    check.kind = f"{lo}..{hi}" + "".join(f" or {w}" for w in words)
    return check


def choice(*options):
    def check(v):
        return None if v in options else "one of: " + ", ".join(options)
    check.kind = " or ".join(options)
    return check


def path(empty_ok=False):
    def check(v):
        if not v and empty_ok:
            return None
        if not v.startswith("/") or re.search(r"[\s\"'\\$`]", v):
            return "an absolute path without spaces or quotes"
        return None
    check.kind = "path" + (" or empty" if empty_ok else "")
    return check


def name_list(v):
    if v and not re.fullmatch(r"[a-z0-9._+-]+(,[a-z0-9._+-]+)*", v):
        return "a comma list of lower-case names without spaces (e.g. steam,zoom,code)"
    return None


name_list.kind = "comma list"


class S:
    def __init__(self, key, default, check, part, text, advanced=False):
        self.key, self.default, self.check, self.part, self.text, self.advanced = key, default, check, part, text, advanced


# Apps the guard never pauses (part of the scope name, lower case). Terminals and code editors: the
# programs they start (dev servers, builds) often serve the app in front. Calls, games, screen recording
# and remote desktop must never stop. Virtual machines: a paused VM looks like a hung one.
EXCLUDE = ("terminal,ptyxis,kgx,wezterm,kitty,alacritty,konsole,tilix,terminator,foot,ghostty,"
           "code,codium,cursor,zed,jetbrains,android-studio,sublime,"
           "zoom,teams,discord,slack,skype,signal,"
           "obs,anydesk,rustdesk,remmina,"
           "steam,lutris,heroic,"
           "virtualbox,vmware,virt-manager,gnome-boxes")

PARTS = {
    "tier": ("VRAM swap tier", "usm off && usm on, or reboot"),
    "guard": ("Guard", "the guard restarts by itself"),
    "logger": ("Logger", "the logger restarts by itself"),
}

SETTINGS = [
    S("VRAM_MB", "auto", integer(256, 1 << 20, "auto"), "tier",
      "How much GPU memory to use as swap, in MiB. auto = all of it but 1 GiB; if this GPU also drives a "
      "screen, all but 2 GiB or 30% (whichever is more); never more than is free when the tier starts. Before "
      "games, AI or other GPU-heavy apps: usm off (usm on afterwards)."),
    S("COMPRESS", "3", integer(0, 8), "tier",
      "0 = store pages as they are. 2..8 = compress them (lz4, on the CPU, ~1 us per page): the swap device is "
      "COMPRESS x VRAM_MB big. Typical app memory shrinks to about a third, so 3 fills the VRAM well. When the "
      "VRAM is nearly full (data that does not compress well), new pages go to the standby swap (STANDBY_MB). "
      "Needs liblz4."),
    S("SPILL_DIR", "/var/lib/usm-swap", path(), "tier",
      "A directory on a disk (not tmpfs). Holds the standby swap file, and the data when the tier is switched "
      "off while RAM is full (it then moves from VRAM to a file here, never through RAM)."),
    S("STANDBY_MB", "auto", integer(256, 1 << 20, "auto", "0"), "tier",
      "Size of the standby swap: a swap file in SPILL_DIR that Linux uses before the VRAM while the VRAM is "
      "nearly full or its data is on disk (switching off with RAM full). It keeps the daemon itself from ever "
      "waiting on the disk while it serves swap. auto = as big as the VRAM tier, at most 4096, smaller if the "
      "disk is short of space. When it fills up while needed, more files of this size are added (as long as the "
      "disk keeps 10% free, at least 2 GiB); they go again when it switches off. 0 = none: then a full VRAM "
      "refuses writes (Linux keeps those pages in RAM) and switching off needs room in RAM."),
    S("ZSWAP_POOL_WITH_TIER", "10", integer(0, 100, "keep"), "tier",
      "zswap (compressed swap cache in RAM) while the tier is on: a pool of this % of RAM; restored when the "
      "tier goes off. 0 = zswap off while on. keep = do not touch zswap. 10 keeps a small fast cache in RAM and "
      "leaves the rest of RAM to the apps (tested: 10..40 equally fast, 0 slower, 5 smoother but slower apps)."),
    S("PAGE_CLUSTER_WITH_TIER", "3", integer(0, 6, "keep"), "tier",
      "Swap readahead while the tier is on (vm.page-cluster: 2^N pages per swap-in; restored when off). 3 = 8 "
      "pages: an app coming back from swap returns ~3x faster than with 0. keep = do not touch."),
    S("QUEUES", "4", integer(1, 16), "tier",
      "Request queues of the VRAM daemon (one CPU thread and 8 MiB of locked RAM each). 4 serves ~500 k page "
      "faults per second.", advanced=True),
    S("TEST_DEVICE_MEMORY", "", path(empty_ok=True), "tier",
      "Testing without a GPU: use this PCI device memory instead (e.g. a QEMU ivshmem .../resource2 file). "
      "Empty = use the NVIDIA GPU.", advanced=True),

    S("GUARD_MODE", "full", choice("full", "protect"), "guard",
      "full = when apps really wait for memory, briefly pause the busiest background app (never the one in "
      "front, one used in the last GUARD_RECENT_S seconds, one playing sound, or one in GUARD_EXCLUDE). "
      "protect = only keep the desktop itself in RAM (GUARD_PROTECT_MB), never pause anything."),
    S("GUARD_EXCLUDE", EXCLUDE, name_list, "guard",
      "Never paused: apps whose name contains one of these (comma list, lower case; see the names in "
      "`usm status` or `usm report`). Default: terminals, code editors, calls, games, screen recording, remote "
      "desktop, virtual machines."),
    S("GUARD_MIN_APP_MB", "256", integer(0, 1 << 20), "guard",
      "Apps using less RAM than this (MiB) are never paused - it would free too little."),
    S("GUARD_PROTECT_MB", "1024", integer(0, 1 << 16), "guard",
      "RAM (MiB) kept for the desktop itself (GNOME Shell, sound), so the mouse and windows stay responsive."),
    S("GUARD_TRIGGER", "0.30", number(0, 0.95), "guard",
      "Overload = apps spend at least this share of the time waiting for memory (memory pressure, PSI). Lower = "
      "acts sooner. 0 = the default (0.30)."),
    S("GUARD_HIGH_TICKS", "6", integer(0, 120), "guard",
      "How long the overload must last before a pause, in half-seconds (6 = 3 s). 0 = the default (6)."),
    S("GUARD_PAUSE_MAX_S", "20", integer(3, 600), "guard",
      "A pause ends once memory has settled, at the latest after this many seconds."),
    S("GUARD_COOLDOWN_S", "120", integer(0, 86400), "guard",
      "After a pause the same app is not paused again for this many seconds."),
    S("GUARD_RECENT_S", "300", integer(0, 86400), "guard",
      "Apps you had in front within this many seconds are never paused."),

    S("LOG_DIR", "/var/log/usm", path(), "logger",
      "Where the logs go: one file per day and kind (samples, apps, events, guard), gzipped after the day."),
    S("LOG_INTERVAL", "1", number(0.2, 60), "logger", "Seconds between samples."),
    S("LOG_KEEP_DAYS", "30", integer(1, 3650), "logger", "Days of logs to keep."),
    S("LOG_MAX_MB", "300", integer(10, 1 << 20), "logger",
      "At most this much (MiB) of logs in LOG_DIR; the oldest go first (a day is ~10 MiB once gzipped)."),
]
BY_KEY = {s.key: s for s in SETTINGS}


def comment(text, width=110):
    return "\n".join("# " + line for line in textwrap.wrap(text, width - 2))


def read(conf=CONF):
    """{KEY: value} of the file, in file order; missing file = {}."""
    vals = {}
    try:
        lines = open(conf).read().splitlines()
    except FileNotFoundError:
        return vals
    for line in lines:
        m = re.match(r"\s*([A-Z][A-Z0-9_]*)\s*=(.*)$", line)
        if m:
            v = m.group(2).strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            vals[m.group(1)] = v
    return vals


def problems(vals):
    out = []
    for k, v in vals.items():
        s = BY_KEY.get(k)
        if s is None:
            continue   # unknown keys are left alone (a newer or older version's)
        err = s.check(v)
        if err:
            out.append(f"{k}={v}: must be {err}")
    return out


def parse_assignments(args):
    out = {}
    for a in args:
        if "=" not in a:
            raise SystemExit(f"not KEY=VALUE: {a}")
        k, v = a.split("=", 1)
        k = k.strip().upper()
        if k not in BY_KEY:
            raise SystemExit(f"unknown setting {k} (list: usm config show --all)")
        out[k] = v.strip()
    bad = problems(out)
    if bad:
        raise SystemExit("invalid: " + "; ".join(bad))
    return out


def write_atomic(conf, text):
    d = os.path.dirname(conf) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".usm.conf.")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, conf)


def new_file(values):
    out = [f"# USM settings (written by USM {version()}). Change with: usm config set KEY=VALUE  (list: usm config show)",
           "# or edit this file (usm config edit). One KEY=VALUE per line, no comments at the end of a line."]
    part = None
    for s in SETTINGS:
        if s.part != part:
            part = s.part
            title, when = PARTS[part]
            out += ["", f"# --- {title} (after a change: {when}) ---"]
        out += [comment(s.text), f"{s.key}={values.get(s.key, s.default)}"]
    return "\n".join(out) + "\n"


def set_in_text(text, changes):
    """Replace KEY= lines in place; append keys that are missing (with their comment)."""
    lines = text.splitlines()
    done = set()
    for i, line in enumerate(lines):
        m = re.match(r"\s*([A-Z][A-Z0-9_]*)\s*=", line)
        if m and m.group(1) in changes:
            k = m.group(1)
            if k in done:
                lines[i] = "# " + line + "   (duplicate, ignored)"
            else:
                lines[i] = f"{k}={changes[k]}"
                done.add(k)
    for k, v in changes.items():
        if k not in done:
            lines += ["", comment(BY_KEY[k].text), f"{k}={v}"]
    return "\n".join(lines) + "\n"


def init(conf, changes):
    if not os.path.exists(conf):
        write_atomic(conf, new_file(changes))
        print(f"settings: {conf} (new)")
        return
    text = open(conf).read()
    have = read(conf)
    missing = {s.key: s.default for s in SETTINGS if s.key not in have}
    add = dict(missing)
    add.update(changes)
    if add:
        if missing:
            text = text.rstrip("\n") + f"\n\n# --- added by USM {version()} ---\n"
        write_atomic(conf, set_in_text(text, add))
    parts = []
    if missing:
        parts.append("added " + ", ".join(missing))
    if changes:
        parts.append("set " + ", ".join(f"{k}={v}" for k, v in changes.items()))
    print(f"settings: {conf} (kept your values" + ("; " + "; ".join(parts) if parts else "") + ")")


def active(unit):
    return subprocess.run(["systemctl", "is-active", "-q", unit]).returncode == 0


def apply(changed_parts):
    for part, unit in (("guard", "usm-guard.service"), ("logger", "usm-logger.service")):
        if part in changed_parts and active(unit):
            r = subprocess.run(["systemctl", "restart", unit])
            print(f"{PARTS[part][0]}: restarted" if r.returncode == 0 else f"{PARTS[part][0]}: restart FAILED "
                  f"(journalctl -u {unit})")
    if "tier" in changed_parts:
        if active("usm-vram.service"):
            print("VRAM swap tier: takes effect after  usm off && usm on  (or the next boot)")
        else:
            print("VRAM swap tier: takes effect when it is switched on (usm on)")


def show(conf, show_all):
    vals = read(conf)
    if not vals and not os.path.exists(conf):
        print(f"{conf} does not exist (USM not installed?) - defaults:")
    part = None
    for s in SETTINGS:
        if s.advanced and not show_all and vals.get(s.key, s.default) == s.default:
            continue
        if s.part != part:
            part = s.part
            print(f"\n{PARTS[part][0]}  (after a change: {PARTS[part][1]})")
        v = vals.get(s.key, s.default)
        mark = "" if v == s.default else f"   (default: {s.default or 'empty'})" if len(s.default) < 40 else "   (changed)"
        print(f"  {s.key}={v}{mark}")
        for line in textwrap.wrap(s.text, 104):
            print(f"      {line}")
    unknown = [k for k in vals if k not in BY_KEY]
    if unknown:
        print("\nnot known to this version (ignored): " + ", ".join(unknown))
    if not show_all:
        print("\nadvanced settings: usm config show --all")


def doc():
    out = ["# USM settings", "",
           "All settings live in `/etc/usm/usm.conf` (plain `KEY=VALUE` lines). Change them with",
           "`usm config set KEY=VALUE` (checked, then applied), see them with `usm config show`, or edit the",
           "file with `usm config edit`. At install time: `sudo ./install.sh --set KEY=VALUE` (repeatable).", "",
           "This file is generated from `tools/usm_config.py` (`python3 tools/usm_config.py doc`).", ""]
    part = None
    for s in SETTINGS:
        if s.part != part:
            part = s.part
            out += ["", f"## {PARTS[part][0]}", "", f"After a change: {PARTS[part][1]}.", "",
                    "| Setting | Default | Values | What it does |", "|---|---|---|---|"]
        default = s.default.replace(",", ", ") if s.key == "GUARD_EXCLUDE" else s.default
        out.append(f"| `{s.key}`{' (advanced)' if s.advanced else ''} | `{default or '(empty)'}` | "
                   f"{s.check.kind} | {s.text} |")
    return "\n".join(out) + "\n"


def need_root():
    if os.geteuid() != 0:
        raise SystemExit("needs root: sudo usm config ...")


def edit(conf):
    """Edit a copy; only a valid file replaces the settings. Then apply what changed."""
    need_root()
    if not os.path.exists(conf):
        raise SystemExit(f"{conf} does not exist - is USM installed?")
    old = read(conf)
    fd, tmp = tempfile.mkstemp(prefix="usm.conf.", suffix=".edit")
    os.close(fd)
    try:
        with open(conf) as src, open(tmp, "w") as dst:
            dst.write(src.read())
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or ("nano" if subprocess.run(
            ["sh", "-c", "command -v nano"], capture_output=True).returncode == 0 else "vi")
        while True:
            subprocess.run(["sh", "-c", f'{editor} "$1"', "editor", tmp])
            bad = problems(read(tmp))
            if not bad:
                break
            print("invalid:\n  " + "\n  ".join(bad))
            if input("edit again? [Y/n] ").strip().lower() in ("n", "no"):
                print("not saved - the settings are unchanged")
                return 1
        new = read(tmp)
        changed = {k for k in set(old) | set(new) if old.get(k) != new.get(k)}
        if not changed:
            print("nothing changed")
            return 0
        write_atomic(conf, open(tmp).read())
        print("changed: " + ", ".join(sorted(changed)))
        apply({BY_KEY[k].part for k in changed if k in BY_KEY})
    finally:
        os.unlink(tmp)
    return 0


def main(argv):
    conf = CONF
    no_apply = "--no-apply" in argv
    argv = [a for a in argv if a != "--no-apply"]
    if "--conf" in argv:
        i = argv.index("--conf")
        conf = argv[i + 1]
        del argv[i:i + 2]
    cmd = argv[0] if argv else "show"
    args = argv[1:]
    if cmd == "init":
        changes = {}
        while args:
            if args[0] == "--set" and len(args) > 1:
                changes.update(parse_assignments([args[1]]))
                args = args[2:]
            else:
                raise SystemExit(f"init: unknown argument {args[0]}")
        init(conf, changes)
        bad = problems(read(conf))
        if bad:
            print("warning: invalid settings in " + conf + ":\n  " + "\n  ".join(bad), file=sys.stderr)
    elif cmd == "show":
        show(conf, "--all" in args)
    elif cmd == "get":
        if len(args) != 1 or args[0].upper() not in BY_KEY:
            raise SystemExit("usage: usm config get KEY   (keys: usm config show --all)")
        k = args[0].upper()
        print(read(conf).get(k, BY_KEY[k].default))
    elif cmd in ("set", "reset"):
        if not args:
            raise SystemExit(f"usage: usm config {cmd} " + ("KEY=VALUE ..." if cmd == "set" else "KEY ..."))
        if cmd == "reset":
            unknown = [a for a in args if a.upper() not in BY_KEY]
            if unknown:
                raise SystemExit("unknown setting " + ", ".join(unknown))
            args = [f"{a.upper()}={BY_KEY[a.upper()].default}" for a in args]
        changes = parse_assignments(args)
        need_root()
        if not os.path.exists(conf):
            raise SystemExit(f"{conf} does not exist - is USM installed?")
        old = read(conf)
        changes = {k: v for k, v in changes.items() if old.get(k) != v}
        if not changes:
            print("nothing changed")
            return 0
        write_atomic(conf, set_in_text(open(conf).read(), changes))
        for k, v in changes.items():
            print(f"{k}={v}   (was: {old.get(k, 'not set')})")
        if not no_apply:
            apply({BY_KEY[k].part for k in changes})
    elif cmd == "check":
        if not os.path.exists(conf):
            raise SystemExit(f"{conf} does not exist")
        bad = problems(read(conf))
        if bad:
            print("invalid settings in " + conf + ":\n  " + "\n  ".join(bad))
            return 1
        print(f"{conf}: all settings valid")
    elif cmd == "edit":
        return edit(conf)
    elif cmd == "validate":   # KEY=VALUE ... without touching any file (install.sh's questions)
        parse_assignments(args)
    elif cmd == "doc":
        sys.stdout.write(doc())
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]) or 0)
