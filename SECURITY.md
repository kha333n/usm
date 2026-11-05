# Security policy

USM runs as root: the installer, the VRAM daemon, the guard and the logger. A polkit rule lets the user at the
machine switch the tier and the guard on and off without a password, through four fixed actions in
`/usr/local/lib/usm/usm-helper`. Please treat security problems in these parts seriously, and report them
privately.

## Reporting a vulnerability

**Please don't open a public issue.** Use GitHub's private reporting instead:
[Report a vulnerability](https://github.com/kha333n/usm/security/advisories/new) (the repository's *Security* tab).

Include what an attacker can do, the steps to reproduce, and your USM version (`usm version`), distribution and
kernel. You'll get an answer as soon as possible. USM is maintained in spare time, so please allow a few days.

## In scope

- Privilege escalation: through `usm-helper`, the polkit rule, the services or file permissions
- Other users reading or changing USM's data: the swapped memory in VRAM or in the spill file, logs, settings
- The daemon mishandling requests from the kernel (ublk) in a way that leaks or corrupts memory

**Data corruption** that isn't caused by an attacker (a page reads back wrong) is a serious bug, but not a security
issue: open a normal [bug report](https://github.com/kha333n/usm/issues/new?template=bug_report.yml) and mark it
urgent.

## Supported versions

USM is in beta: only the latest release gets fixes.
