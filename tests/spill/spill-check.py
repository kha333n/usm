#!/usr/bin/env python3
"""spill-check: hammer a usm-vramd device with stamped writes and verified reads (raw device, not
swap), so a spill, fill or compaction can run underneath and any lost or stale write shows up.

Usage (root): spill-check.py DEV SIZE_MB SECONDS
Env: CONTENT=stamp (default: a 16-byte stamp repeated) | random (incompressible) | mixed (compressible like real memory: every
     tenth page all zero, the others 0/1/2/3 KiB of random bytes after the stamp, the rest repeats)
     DISCARD=1: now and then discard a 1 MiB range (those pages must then read back as zeros)
Every 4 KiB block written carries (block number, write generation); reads compare the whole block
with what was written last. 1 in 8 writes is 64 KiB (16 blocks, may cross 1 MiB chunks). At the end
every written block is read back once more.
"""
import fcntl
import mmap
import os
import random
import struct
import sys
import time

dev, size_mb, secs = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
CONTENT = os.environ.get("CONTENT", "stamp")
MIXED = CONTENT == "mixed"
RANDOM = CONTENT == "random"
DISCARD = os.environ.get("DISCARD") == "1"
BLKDISCARD = 0x1277
B = 4096
nb = size_mb * 256
gen = [0] * nb           # 0 = never written, -1 = discarded (reads as zeros), else last generation
fd = os.open(dev, os.O_RDWR | os.O_DIRECT)
buf = mmap.mmap(-1, 16 * B)
rbuf = mmap.mmap(-1, B)
bad = writes = reads = discards = 0
ZERO = bytes(B)


def content(b, g):
    if g < 0:
        return ZERO
    st = struct.pack("<QQ", b, g)
    if RANDOM:   # does not compress at all
        return st + random.Random(b * 1000003 + g).randbytes(B - 16)
    if not MIXED:
        return st * (B // 16)
    h = (b * 2654435761 + g * 40503) & 0xffffffff
    if h % 10 == 0:
        return ZERO
    k = (h >> 4) % 4 * 1024
    rnd = random.Random(h).randbytes(k)
    return (st + rnd + st * (B // 16))[:B]


def check(b):
    global bad
    os.preadv(fd, [rbuf], b * B)
    if rbuf[:] != content(b, gen[b]):
        bad += 1
        if bad <= 10:
            print(f"BAD block {b} (gen {gen[b]}): starts {bytes(rbuf[:16]).hex()}", flush=True)


t_end = time.time() + secs
last = time.time()
while time.time() < t_end:
    r = random.random()
    if DISCARD and r < 0.002:
        b0 = random.randrange(0, nb // 256) * 256
        try:
            fcntl.ioctl(fd, BLKDISCARD, struct.pack("QQ", b0 * B, 256 * B))
            for i in range(256):
                if gen[b0 + i]:
                    gen[b0 + i] = -1
            discards += 1
        except OSError as e:
            print(f"discard not supported ({e}) - no more discards", flush=True)
            DISCARD = False
    elif r < 0.5:
        n = 16 if random.random() < 0.125 else 1
        b0 = random.randrange(0, nb - n)
        for i in range(n):
            gen[b0 + i] = max(gen[b0 + i], 0) + 1
            buf[i * B:(i + 1) * B] = content(b0 + i, gen[b0 + i])
        os.pwritev(fd, [memoryview(buf)[:n * B]], b0 * B)
        writes += 1
    else:
        b = random.randrange(0, nb)
        if gen[b]:
            check(b)
            reads += 1
    if time.time() - last >= 5:
        last = time.time()
        print(f"  {time.strftime('%T')} {writes} writes, {reads} verified reads, {discards} discards, {bad} bad",
              flush=True)

final = 0
for b in range(nb):
    if gen[b]:
        check(b)
        final += 1
print(f"RESULT: {writes} writes, {reads} reads, {discards} discards during the run, {final} blocks re-read at "
      f"the end: {'ALL CORRECT' if bad == 0 else str(bad) + ' BAD'}")
sys.exit(1 if bad else 0)
