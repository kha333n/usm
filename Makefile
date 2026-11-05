# USM - build from source.
#   make              build the VRAM daemon (vram/usm-vramd); needs gcc (or clang) and make, nothing else
#   sudo ./install.sh install (see README.md); it runs `make` itself if vram/usm-vramd is missing
#   make check        quick offline checks (syntax, settings tool, daemon --version)
#   make dist         release tarball in dist/ (with the daemon built here; packaging/build-release.sh
#                     builds it in an Ubuntu 22.04 container instead, so it runs on older systems too)
#   make tests/desktop/hog   test helper for tests/desktop (see tests/README.md)
#   make clean
VERSION := $(shell cat VERSION)
CC ?= cc
CFLAGS ?= -O2
URING := third_party/liburing
DAEMON := vram/usm-vramd

all: $(DAEMON)

$(URING)/src/liburing.a:
	cd $(URING) && ./configure --cc="$(CC)" > /dev/null && $(MAKE) -C src liburing.a > /dev/null

# third_party/linux: the kernel's ublk interface header (newer than many distributions' kernel headers).
$(DAEMON): vram/usm-vramd.c $(URING)/src/liburing.a VERSION
	$(CC) $(CFLAGS) -Wall -Wextra -DUSM_VERSION='"$(VERSION)"' -Ithird_party/linux -I$(URING)/src/include \
		-o $@ $< $(URING)/src/liburing.a -ldl -lpthread

check: $(DAEMON)
	tests/unit/run.sh

# Test helper for the desktop tests (tests/desktop/*, run as root on a real desktop: see tests/README.md).
tests/desktop/hog: tests/desktop/hog.c
	$(CC) $(CFLAGS) -Wall -o $@ $<

dist: $(DAEMON)
	packaging/make-tarball.sh

clean:
	rm -f $(DAEMON) tests/desktop/hog
	-$(MAKE) -C $(URING)/src clean > /dev/null 2>&1
	rm -f $(URING)/config-host.h $(URING)/config-host.mak $(URING)/config.log \
		$(URING)/src/include/liburing/compat.h $(URING)/src/include/liburing/io_uring_version.h
	rm -rf dist

.PHONY: all check dist clean
