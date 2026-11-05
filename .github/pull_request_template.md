## What and why

<!-- What does this change, and why? Link the issue if there is one (Fixes #123). -->

## How it was tested

<!-- Machine, GPU + driver, distribution + kernel, and what you ran. -->

- [ ] `make check` passes
- [ ] Tested on real hardware (say which above), or not needed for this change
- [ ] Daemon / tier changes: data check run (`tests/spill/spill-check.py`, all pages correct)
- [ ] Settings changed: `docs/configuration.md` regenerated (`python3 tools/usm_config.py doc > docs/configuration.md`)
- [ ] Docs updated where users would notice the change
