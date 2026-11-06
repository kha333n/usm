// usm-vramd: a swap device backed by GPU VRAM, as a user-space program (no custom kernel driver).
//
// It borrows VRAM through CUDA, creates a ublk block device (/dev/ublkbN; ublk is part of the
// stock kernel), and serves every read/write by moving the data between RAM and VRAM with the
// GPU's copy engines. Requests that arrive together are copied as one batch.
//
// Several queues: the kernel spreads requests over them by CPU, and each queue is served by its
// own thread with its own io_uring, I/O buffers and CUDA stream, so faults on different CPUs are
// served in parallel. Copy mode. Run as root. While the device is in use (swapon) the program
// refuses to stop: `swapoff` the device first, then stop it with Ctrl-C or SIGTERM.
//
// Build: make   (top of the repository)
// Run:   usm-vramd [vram_mb=4096] [queues=4] [queue_depth=32] [poll_us=0]
//          poll_us > 0: after each batch, keep checking for new requests this long before
//          going to sleep (costs CPU time, answers faster)
//        usm-vramd del ID      remove a leftover /dev/ublkbID (after a crash)
//        usm-vramd probe       can this machine run it? one line: lz4, CUDA, GPU name, VRAM (for `usm check`)
//        usm-vramd --version
// Test mode (no GPU, e.g. in a VM): USM_VRAMD_FAKE=/sys/bus/pci/devices/.../resource2 uses that
// device memory instead (QEMU ivshmem: host memory outside the VM's RAM), with the copy times of
// a real RTX 3050 copy engine added as busy waits.
//
// Spill / fill (switching the VRAM off while the device stays in use): with USM_VRAMD_SPILL=FILE
//   SIGUSR1  spill: copy the data from VRAM into FILE (on a disk) and release the VRAM; the device
//            keeps working the whole time and afterwards serves from FILE (like a swap file)
//   SIGUSR2  fill: get VRAM again, copy the data back from FILE, delete FILE
// The data moves in 1 MiB chunks, straight between VRAM and the disk: no RAM is needed for it, so
// this works when RAM is full (unlike swapoff, which must bring every page into RAM first).
//
// Standby swap (USM_VRAMD_STANDBY=FILE, USM_VRAMD_STANDBY_PRIO=N, a swap file made by usm-vramd-ctl):
// switched on above this device whenever the VRAM is nearly full or data is in the spill file, so the
// kernel itself writes new pages to the disk. See "the standby swap" below for why that matters.
//
// Giving VRAM back (USM_VRAMD_GPU_FREE_MB=N, default 1024; 0 = never): once other programs start using
// the GPU, VRAM is given back to them in 64 MiB pieces until N MiB are free (the data of a piece moves to
// the spill file first, as in a spill), and taken back once they have been done for 30 s.
// Test mode: USM_VRAMD_FAKE_GPU=FILE holds how many MiB "other programs" use.
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <liburing.h>
#include <malloc.h>
#include <linux/ublk_cmd.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/eventfd.h>
#include <sys/mman.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <linux/fs.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/statfs.h>
#include <sys/swap.h>
#include <time.h>
#include <unistd.h>

#ifndef PR_SET_IO_FLUSHER
#define PR_SET_IO_FLUSHER 57
#endif

#ifndef FALLOC_FL_WRITE_ZEROES
#define FALLOC_FL_WRITE_ZEROES 0x80
#endif

#ifndef USM_VERSION
#define USM_VERSION "dev"
#endif
#define IO_BUF (256 * 1024)   // largest single request we accept
#define STREAM_NON_BLOCKING 1 // CU_STREAM_NON_BLOCKING
#define QUIT_TAG (~0ull)      // user_data of the "control thread says: quit" poll
#define CHUNK (1u << 20)      // data moves between VRAM and the spill file in pieces of this size

// ---- CUDA driver API, loaded at runtime (no toolkit needed) ----
typedef int CUresult;
typedef void *CUcontext;
typedef unsigned long long CUdeviceptr;
typedef void *CUstream;
static CUresult (*cuInit)(unsigned);
static CUresult (*cuDeviceGet)(int *, int);
static CUresult (*cuDevicePrimaryCtxRetain)(CUcontext *, int);
static CUresult (*cuCtxSetCurrent)(CUcontext);
static CUresult (*cuMemAlloc)(CUdeviceptr *, size_t);
static CUresult (*cuMemFree)(CUdeviceptr);
static CUresult (*cuMemGetInfo)(size_t *, size_t *);
static CUresult (*cuMemHostRegister)(void *, size_t, unsigned);
static CUresult (*cuMemcpyHtoDAsync)(CUdeviceptr, const void *, size_t, CUstream);
static CUresult (*cuMemcpyDtoHAsync)(void *, CUdeviceptr, size_t, CUstream);
static CUresult (*cuStreamCreate)(CUstream *, unsigned);
static CUresult (*cuStreamSynchronize)(CUstream);
// lz4 (compressed mode), loaded at run time as well
static int (*LZ4_compress_default)(const char *, char *, int, int);
static int (*LZ4_decompress_safe)(const char *, char *, int, int);

static void *cuda_sym(void *lib, const char *name) {
    void *p = dlsym(lib, name);
    if (!p) {
        fprintf(stderr, "libcuda.so.1: missing %s\n", name);
        exit(1);
    }
    return p;
}

static void cuda_check(CUresult r, const char *what) {
    if (r) {
        fprintf(stderr, "%s failed: CUDA error %d\n", what, r);
        exit(1);
    }
}

static double now_us(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec * 1e6 + t.tv_nsec / 1e3;
}

// A cleared 128-byte SQE (IORING_SETUP_SQE128) and its command area.
static struct io_uring_sqe *get_sqe128(struct io_uring *ring) {
    struct io_uring_sqe *sqe = io_uring_get_sqe(ring);
    if (!sqe) {
        fprintf(stderr, "io_uring submission queue full\n");
        exit(1);
    }
    memset((char *)sqe, 0, 2 * sizeof(*sqe));
    return sqe;
}

static void *sqe_cmd(struct io_uring_sqe *sqe) {
    return (char *)sqe + offsetof(struct io_uring_sqe, cmd);
}

// ---- ublk control commands (sent through io_uring) ----
static struct io_uring ctrl_ring;
static int ctrl_fd = -1;
static pthread_mutex_t ctrl_lock = PTHREAD_MUTEX_INITIALIZER;

static int ctrl_open(void) {
    ctrl_fd = open("/dev/ublk-control", O_RDWR);
    if (ctrl_fd < 0) {
        perror("/dev/ublk-control (is ublk_drv loaded? are we root?)");
        return -1;
    }
    if (io_uring_queue_init(8, &ctrl_ring, IORING_SETUP_SQE128)) {
        fprintf(stderr, "io_uring_queue_init (control) failed\n");
        return -1;
    }
    return 0;
}

static int ctrl_cmd(unsigned op, unsigned dev, void *addr, unsigned len, __u64 data0) {
    pthread_mutex_lock(&ctrl_lock);
    struct io_uring_sqe *sqe = get_sqe128(&ctrl_ring);
    sqe->opcode = IORING_OP_URING_CMD;
    sqe->fd = ctrl_fd;
    sqe->cmd_op = op;
    struct ublksrv_ctrl_cmd c = {.dev_id = dev, .queue_id = (__u16)-1, .len = (__u16)len,
                                 .addr = (__u64)(uintptr_t)addr, .data = {data0}};
    memcpy(sqe_cmd(sqe), &c, sizeof(c));
    io_uring_submit(&ctrl_ring);
    struct io_uring_cqe *cqe;
    int ret = io_uring_wait_cqe(&ctrl_ring, &cqe);
    if (!ret) {
        ret = cqe->res;
        io_uring_cqe_seen(&ctrl_ring, cqe);
    }
    pthread_mutex_unlock(&ctrl_lock);
    return ret;
}

static __u64 query_features(void) {
    __u64 f = 0;
    return ctrl_cmd(UBLK_U_CMD_GET_FEATURES, 0, &f, sizeof(f), 0) ? 0 : f;
}

static int safe_stop;   // kernel has UBLK_U_CMD_TRY_STOP_DEV

static int used_as_swap(unsigned dev) {
    char want[48], line[512];
    int n = snprintf(want, sizeof want, "/dev/ublkb%u ", dev), used = 0;
    FILE *f = fopen("/proc/swaps", "r");
    while (f && fgets(line, sizeof line, f))
        used |= !strncmp(line, want, n);
    if (f)
        fclose(f);
    return used;
}

// Stop the device unless something still uses it: 0 = stopped, -EBUSY = still in use.
static int stop_dev(unsigned dev) {
    for (int attempt = 0; attempt < 30; attempt++) {
        if (used_as_swap(dev))
            return -EBUSY;
        int ret = -EOPNOTSUPP;
        if (safe_stop)   // the kernel itself refuses while anyone (e.g. swapon) holds the device
            ret = ctrl_cmd(UBLK_U_CMD_TRY_STOP_DEV, dev, NULL, 0, 0);
        if (ret && ret != -EBUSY)
            ret = ctrl_cmd(UBLK_U_CMD_STOP_DEV, dev, NULL, 0, 0);
        if (ret != -EBUSY)
            return ret;
        usleep(100000);   // a short-lived opener (udev probing after swapoff): try again
    }
    return -EBUSY;
}

// ---- the queues ----
struct queue {
    int id;
    char *bufs;                           // depth * IO_BUF of pinned RAM, one slot per tag
    const struct ublksrv_io_desc *descs;  // the kernel's description of each pending request
    size_t desc_len;
    struct io_uring ring;
    int ring_ok;
    CUstream stream;
    pthread_t thread;
    int setup_err;
    unsigned long reads, writes, batches, errors, file_reads, file_writes;
    unsigned long long bytes_r, bytes_w;
    double busy_us;
    long minflt, majflt;                  // page faults of this thread while serving (should be 0)
    unsigned pending;                     // VRAM copies queued since the last dev_sync()
    unsigned long fake_ns, fake_fixed_ns; // test mode: copy time owed for the current batch
    // compressed mode only:
    char *rscratch;                       // depth * IO_BUF: compressed bytes on their way from VRAM
    char *obuf;                           // the open segment (CHUNK): new pages are appended here
    char *fscratch;                       // 2 pages: reading one compressed page from the spill file
    long oseg;                            // segment being filled (-1: none)
    unsigned ofill;                       // bytes used in obuf
    pthread_mutex_t olock;                // other queues reading from obuf vs. this one closing it
    struct dz_job { char *src, *dst; unsigned len; int idx; } *jobs;   // decompress after the copy
    unsigned njobs;
    char cbuf[4224];                      // lz4 output for one page (LZ4_compressBound(4096) = 4127)
};

static unsigned dev_id;
static int quit_fd = -1, cfd = -1, nr_queues, depth, poll_us;
static size_t vram_bytes;   // size of the device (and of the VRAM it borrows while the data is there)
static CUcontext ctx;
static sigset_t ctl_signals;
static char *fake_mem;   // test mode: device memory mapped from a PCI BAR instead of CUDA VRAM
static pthread_mutex_t ready_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t ready_cond = PTHREAD_COND_INITIALIZER;
static int queues_ready;

// Where each 1 MiB chunk of the device lives. The queue threads read these while holding
// move_lock for reading (once per batch); the mover changes a chunk's state only while holding it
// for writing, and holds it only for short steps, never while waiting for the disk.
enum { C_VRAM, C_TO_FILE, C_FILE, C_TO_VRAM };   // in C_TO_*: the old place is still the valid copy
static unsigned char *chunk_state, *chunk_dirty, *chunk_written;
static size_t nr_chunks;
static pthread_rwlock_t move_lock;
static const char *spill_path;   // USM_VRAMD_SPILL
static int spill_fd = -1;        // open while any chunk lives in the file

// The VRAM is borrowed in slabs of SLAB bytes (the last one may be smaller), so it can be given back a
// piece at a time (see "giving VRAM back"). Every chunk of a slab that is not held lives in the spill file.
// Only the mover changes these; the queues use slab_ptr only for chunks in VRAM.
#define SLAB (64u << 20)
#define CPS (SLAB / CHUNK)        // chunks per slab
static CUdeviceptr *slab_ptr;
static unsigned char *slab_held;  // its VRAM is allocated
static unsigned char *slab_leaving;   // compressed mode: being given back, its free segments are not handed out
static size_t nr_slabs, slabs_held;
static long long held_b;          // bytes of VRAM held
static inline size_t slab_len(size_t k) {
    size_t end = (k + 1) * (size_t)SLAB;
    return (end > vram_bytes ? vram_bytes : end) - k * (size_t)SLAB;
}
static inline CUdeviceptr vptr(unsigned long long off) { return slab_ptr[off / SLAB] + off % SLAB; }
// Our own disk I/O is allowed only while this is set: the standby swap is on and has room (see "the
// standby swap"). Without it, writes that would go to the spill file are refused instead.
static int sb_room_ok;
static unsigned long long st_full;   // writes refused (VRAM full, or the disk not safe to use now)
static size_t sb_on_free, sb_off_free;   // compressed mode: free VRAM segments at which the standby goes on / may go off
static void want_standby(void);

static void queue_io_cmd(struct queue *q, unsigned op, int tag, int result) {
    struct io_uring_sqe *sqe = get_sqe128(&q->ring);
    sqe->opcode = IORING_OP_URING_CMD;
    sqe->fd = cfd;
    sqe->cmd_op = op;
    sqe->user_data = tag;
    struct ublksrv_io_cmd cmd = {.q_id = q->id, .tag = tag, .result = result,
                                 .addr = (__u64)(uintptr_t)(q->bufs + (size_t)tag * IO_BUF)};
    memcpy(sqe_cmd(sqe), &cmd, sizeof(cmd));
}

static void mark_ready(struct queue *q, int err) {
    pthread_mutex_lock(&ready_lock);
    q->setup_err = err;
    queues_ready++;
    pthread_cond_broadcast(&ready_cond);
    pthread_mutex_unlock(&ready_lock);
}

// Copy between an I/O buffer and the device memory. With CUDA: queued on the queue's stream.
// In test mode: done at once, with the time a real copy engine would take owed until dev_sync().
// Measured on the RTX 3050: read 3.1 us + 1.4 us/page, write 2.9 us + 1.8 us/page (per batch).
static int dev_copy(struct queue *q, int to_dev, unsigned long long off, char *buf, unsigned len) {
    q->pending++;
    if (!fake_mem)
        return to_dev ? cuMemcpyHtoDAsync(vptr(off), buf, len, q->stream)
                      : cuMemcpyDtoHAsync(buf, vptr(off), len, q->stream);
    unsigned long pages = (len + 4095) / 4096, fixed = to_dev ? 2900 : 3100;
    if (to_dev)
        memcpy(fake_mem + off, buf, len);
    else
        memcpy(buf, fake_mem + off, len);
    q->fake_ns += pages * (to_dev ? 1800 : 1400);
    if (fixed > q->fake_fixed_ns)
        q->fake_fixed_ns = fixed;
    return 0;
}

static void fake_wait(unsigned long ns) {
    double until = now_us() + ns / 1000.0;
    while (now_us() < until)
        ;
}

static int dev_sync(struct queue *q) {
    if (!q->pending)   // nothing queued (e.g. the batch was served from the spill file)
        return 0;
    q->pending = 0;
    if (!fake_mem)
        return cuStreamSynchronize(q->stream);
    fake_wait(q->fake_ns + q->fake_fixed_ns);
    q->fake_ns = q->fake_fixed_ns = 0;
    return 0;
}

// Read or write the spill file (direct I/O: no page cache, so no RAM needed).
static int file_io(int to_file, unsigned long long off, char *buf, size_t len) {
    while (len) {
        ssize_t r = to_file ? pwrite(spill_fd, buf, len, (off_t)off) : pread(spill_fd, buf, len, (off_t)off);
        if (r < 0 && errno == EINTR)
            continue;
        if (r <= 0)
            return -1;
        off += r;
        buf += r;
        len -= r;
    }
    return 0;
}

// Serve one request (caller holds move_lock for reading), chunk by chunk from wherever each lives.
static int dev_io(struct queue *q, int to_dev, unsigned long long off, char *buf, unsigned len) {
    while (len) {
        size_t c = off / CHUNK;
        unsigned n = CHUNK - (unsigned)(off % CHUNK);
        if (n > len)
            n = len;
        unsigned char st = chunk_state[c];
        if (to_dev) {   // several queues may set these at once: same value, atomic stores
            __atomic_store_n(&chunk_written[c], 1, __ATOMIC_RELAXED);
            if (st == C_TO_FILE || st == C_TO_VRAM)   // the mover's copy is now out of date
                __atomic_store_n(&chunk_dirty[c], 1, __ATOMIC_RELAXED);
        }
        int r;
        if (st == C_VRAM || st == C_TO_FILE) {
            r = dev_copy(q, to_dev, off, buf, n);
        } else if (to_dev && !__atomic_load_n(&sb_room_ok, __ATOMIC_RELAXED)) {
            __atomic_add_fetch(&st_full, 1, __ATOMIC_RELAXED);   // the kernel keeps the page in RAM
            r = -1;
        } else {
            r = file_io(to_dev, off, buf, n);
            if (to_dev)
                q->file_writes++;
            else
                q->file_reads++;
        }
        if (r)
            return -1;
        off += n;
        buf += n;
        len -= n;
    }
    return 0;
}

// ---- compressed mode (USM_VRAMD_COMPRESS=RATIO) ----
// The device is RATIO times bigger than the VRAM it borrows. Each 4 KiB page written is compressed
// (lz4, on the CPU; all-zero pages are only marked) and appended to the queue's open segment, a
// CHUNK of pinned RAM; a full segment goes to VRAM (or, after a spill, to the spill file) in one
// copy. A map in RAM says where each page is: segment, offset, length. Rewritten and discarded
// pages leave dead bytes; the mover compacts mostly-dead segments (copies their live pages into a
// fresh one) so they can be reused. Segments are the chunks of spill/fill, so a compressed device
// spills to disk like a plain one. A read copies just the compressed bytes and decompresses them.
// When the VRAM runs full (data that does not compress well), the standby swap takes new pages and
// the room left takes rewrites; with no room at all a write is refused (the kernel keeps that page in RAM).
// Nothing ever goes to a file from here unless the device was spilled.
#define PG 4096u
#define E_PRESENT (1ull << 63)
#define E_ZERO (1ull << 62)
#define SEG_RESERVE 2                  // free segments only the compactor may take
enum { S_FREE = -1, S_CLOSED = 0, S_VICTIM = -2 };   // seg_open: else 1..nr_queues (open), nr_queues+1 (compactor)
static double cz_ratio;                // 0 = plain mode
static size_t dev_bytes;               // size of the device (plain mode: vram_bytes)
static uint64_t *pmap;                 // per page: E_PRESENT [E_ZERO] | seg << 29 | (off / 16) << 13 | (len - 1)
static uint32_t *seg_live;             // live bytes per segment
static int *seg_open;
static uint32_t *free_segs;            // stack of free segments in VRAM
static uint32_t *ffree_segs, *seg_tmp; // stack of free segments in the spill file (VRAM given back, spilled)
static size_t free_n, ffree_n, gc_low, gc_high;
static pthread_mutex_t seg_mx = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t seg_cv = PTHREAD_COND_INITIALIZER;   // the compactor freed segments
static struct queue *all_queues;
static unsigned long long st_zero, st_raw, st_comp, st_comp_bytes, st_discard, st_gc_runs, st_gc_bytes;
static void want_gc(void);

static inline uint64_t ent(size_t seg, unsigned off, unsigned len) {
    return E_PRESENT | (uint64_t)seg << 29 | (uint64_t)(off >> 4) << 13 | (len - 1);
}
static inline size_t e_seg(uint64_t e) { return (e >> 29) & 0xffffff; }
static inline unsigned e_off(uint64_t e) { return (unsigned)((e >> 13) & 0xffff) << 4; }
static inline unsigned e_len(uint64_t e) { return (unsigned)(e & 0x1fff) + 1; }
static inline int e_stored(uint64_t e) { return (e & E_PRESENT) && !(e & E_ZERO); }

// Which stack a free segment belongs on (caller holds seg_mx). Only the mover moves chunks, and it puts
// every free segment back on the right stack afterwards (seg_repartition).
static void seg_put(size_t s) {
    if (chunk_state[s] == C_VRAM && !slab_leaving[s / CPS])
        free_segs[free_n++] = (uint32_t)s;
    else
        ffree_segs[ffree_n++] = (uint32_t)s;
}

static void seg_push_free(size_t s) {
    __atomic_store_n(&chunk_written[s], 0, __ATOMIC_RELAXED);   // nothing in it for a spill to copy
    pthread_mutex_lock(&seg_mx);
    seg_put(s);
    pthread_cond_broadcast(&seg_cv);
    pthread_mutex_unlock(&seg_mx);
}

// Slabs were given back or taken again: every free segment onto the stack for where it lives now (mover).
static void seg_repartition(void) {
    if (!cz_ratio)
        return;
    pthread_mutex_lock(&seg_mx);
    size_t n = 0;
    for (size_t i = 0; i < free_n; i++)
        seg_tmp[n++] = free_segs[i];
    for (size_t i = 0; i < ffree_n; i++)
        seg_tmp[n++] = ffree_segs[i];
    free_n = ffree_n = 0;
    for (size_t i = 0; i < n; i++)
        seg_put(seg_tmp[i]);
    pthread_cond_broadcast(&seg_cv);
    pthread_mutex_unlock(&seg_mx);
}

// A closed segment with nothing live left goes back to the free list (exactly once).
static void seg_try_free(size_t s) {
    int closed = S_CLOSED;
    if (!__atomic_load_n(&seg_live[s], __ATOMIC_ACQUIRE) &&
        __atomic_compare_exchange_n(&seg_open[s], &closed, S_FREE, 0, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE))
        seg_push_free(s);
}

// A record is dead now (its page was rewritten or discarded).
static void seg_unref(uint64_t e) {
    if (!e_stored(e))
        return;
    size_t s = e_seg(e);
    if (!__atomic_sub_fetch(&seg_live[s], e_len(e), __ATOMIC_ACQ_REL))
        seg_try_free(s);
}

static long seg_alloc(int owner, size_t reserve) {
    long s = -1;
    pthread_mutex_lock(&seg_mx);
    if (free_n > reserve)
        s = free_segs[--free_n];
    else if (ffree_n > reserve)   // none left in VRAM: one in the spill file (its writes need the standby swap)
        s = ffree_segs[--ffree_n];
    size_t left = free_n, all = free_n + ffree_n;
    pthread_mutex_unlock(&seg_mx);
    if (s >= 0) {
        __atomic_store_n(&seg_open[s], owner, __ATOMIC_RELEASE);
        __atomic_store_n(&chunk_written[s], 1, __ATOMIC_RELAXED);   // in use: a spill copies it
    }
    if (all < gc_low)   // compact
        want_gc();
    if (left < sb_on_free)   // nearly full: the standby swap takes new pages from now on
        want_standby();
    return s;
}

static int all_zero(const char *p) {
    const uint64_t *w = (const uint64_t *)p;
    for (unsigned i = 0; i < PG / 8; i++)
        if (w[i])
            return 0;
    return 1;
}

static int unpack(const char *src, unsigned len, char *dst) {
    if (len == PG) {
        memcpy(dst, src, PG);
        return 0;
    }
    return LZ4_decompress_safe(src, dst, (int)len, PG) == (int)PG ? 0 : -1;
}

// Write a whole segment buffer to wherever segment s lives (caller holds move_lock for reading).
static int seg_write(struct queue *q, size_t s, char *buf) {
    unsigned char st = chunk_state[s];
    if (st == C_TO_FILE || st == C_TO_VRAM)   // the mover's copy is out of date now
        __atomic_store_n(&chunk_dirty[s], 1, __ATOMIC_RELAXED);
    if (st == C_VRAM || st == C_TO_FILE)
        return dev_copy(q, 1, (unsigned long long)s * CHUNK, buf, CHUNK) || dev_sync(q) ? -1 : 0;
    if (!__atomic_load_n(&sb_room_ok, __ATOMIC_RELAXED)) {   // spilled, and the disk is not safe to use now
        __atomic_add_fetch(&st_full, 1, __ATOMIC_RELAXED);
        return -1;
    }
    q->file_writes++;
    return file_io(1, (unsigned long long)s * CHUNK, buf, CHUNK);
}

// The open segment is full: write it out, close it, open a fresh one. While waiting for the
// compactor the batch lock is let go (everything queued so far is finished first).
static int next_segment(struct queue *q) {
    if (q->oseg >= 0) {
        size_t s = (size_t)q->oseg;
        if (seg_write(q, s, q->obuf))
            return -1;
        pthread_mutex_lock(&q->olock);
        __atomic_store_n(&seg_open[s], S_CLOSED, __ATOMIC_RELEASE);   // readers: now in VRAM / file
        q->oseg = -1;
        q->ofill = 0;
        pthread_mutex_unlock(&q->olock);
        seg_try_free(s);   // everything in it may have been rewritten already
    }
    long s = seg_alloc(q->id + 1, SEG_RESERVE);
    if (s < 0) {
        dev_sync(q);
        pthread_rwlock_unlock(&move_lock);
        struct timespec until;
        clock_gettime(CLOCK_REALTIME, &until);
        until.tv_nsec += 200 * 1000000L;
        if (until.tv_nsec >= 1000000000L) {
            until.tv_sec++;
            until.tv_nsec -= 1000000000L;
        }
        pthread_mutex_lock(&seg_mx);
        while (free_n <= SEG_RESERVE && ffree_n <= SEG_RESERVE && !pthread_cond_timedwait(&seg_cv, &seg_mx, &until))
            ;
        pthread_mutex_unlock(&seg_mx);
        pthread_rwlock_rdlock(&move_lock);
        s = seg_alloc(q->id + 1, SEG_RESERVE);
        if (s < 0) {
            __atomic_add_fetch(&st_full, 1, __ATOMIC_RELAXED);
            return -1;   // VRAM full of live data: the kernel keeps this page in RAM
        }
    }
    q->oseg = s;
    q->ofill = 0;
    return 0;
}

static int cz_write_page(struct queue *q, size_t page, const char *src) {
    uint64_t e = E_PRESENT | E_ZERO;
    if (all_zero(src)) {
        __atomic_add_fetch(&st_zero, 1, __ATOMIC_RELAXED);
    } else {
        int clen = LZ4_compress_default(src, q->cbuf, PG, (int)sizeof q->cbuf);
        const char *rec = q->cbuf;
        unsigned len = (unsigned)clen;
        if (clen <= 0 || clen > (int)(PG - PG / 8)) {   // hardly compressible: keep it as it is
            rec = src;
            len = PG;
            __atomic_add_fetch(&st_raw, 1, __ATOMIC_RELAXED);
        } else {
            __atomic_add_fetch(&st_comp, 1, __ATOMIC_RELAXED);
            __atomic_add_fetch(&st_comp_bytes, len, __ATOMIC_RELAXED);
        }
        unsigned room = (len + 15) & ~15u;
        if ((q->oseg < 0 || q->ofill + room > CHUNK) && next_segment(q))
            return -1;
        memcpy(q->obuf + q->ofill, rec, len);   // beyond what readers can see yet: no lock needed
        e = ent((size_t)q->oseg, q->ofill, len);
        __atomic_add_fetch(&seg_live[q->oseg], len, __ATOMIC_ACQ_REL);
        q->ofill += room;
    }
    seg_unref(__atomic_exchange_n(&pmap[page], e, __ATOMIC_ACQ_REL));
    return 0;
}

static int cz_read_page(struct queue *q, size_t page, char *dst, char **scratch, int idx) {
    uint64_t e = __atomic_load_n(&pmap[page], __ATOMIC_ACQUIRE);
    if (!e_stored(e)) {   // all zero, or never written
        memset(dst, 0, PG);
        return 0;
    }
    size_t s = e_seg(e);
    unsigned off = e_off(e), len = e_len(e);
    int owner = __atomic_load_n(&seg_open[s], __ATOMIC_ACQUIRE);
    if (owner >= 1 && owner <= nr_queues) {   // still in a queue's open segment, in RAM
        struct queue *o = &all_queues[owner - 1];
        pthread_mutex_lock(&o->olock);
        if (__atomic_load_n(&seg_open[s], __ATOMIC_ACQUIRE) == owner) {
            int r = unpack(o->obuf + off, len, dst);
            pthread_mutex_unlock(&o->olock);
            return r;
        }
        pthread_mutex_unlock(&o->olock);   // closed meanwhile: it is in VRAM / the file now
    }
    unsigned long long at = (unsigned long long)s * CHUNK + off;
    unsigned char st = chunk_state[s];
    if (st == C_VRAM || st == C_TO_FILE) {
        if (len == PG)
            return dev_copy(q, 0, at, dst, PG);
        struct dz_job *j = &q->jobs[q->njobs++];
        *j = (struct dz_job){.src = *scratch, .dst = dst, .len = len, .idx = idx};
        *scratch += (len + 15) & ~15u;
        return dev_copy(q, 0, at, j->src, len);   // decompressed after the batch's copies finished
    }
    unsigned a = off & ~(PG - 1), b = (off + len + PG - 1) & ~(PG - 1);   // direct I/O: whole blocks
    q->file_reads++;
    if (file_io(0, (unsigned long long)s * CHUNK + a, q->fscratch, b - a))
        return -1;
    return unpack(q->fscratch + (off - a), len, dst);
}

static int cz_io(struct queue *q, int write, unsigned long long off, char *buf, unsigned len, int tag, int idx) {
    char *scratch = q->rscratch + (size_t)tag * IO_BUF;
    for (size_t page = off / PG; len; page++, buf += PG, len -= PG)
        if (write ? cz_write_page(q, page, buf) : cz_read_page(q, page, buf, &scratch, idx))
            return -1;
    return 0;
}

static void cz_discard(unsigned long long off, unsigned long long len) {
    for (size_t page = off / PG; len >= PG; page++, len -= PG) {
        uint64_t e = __atomic_exchange_n(&pmap[page], 0, __ATOMIC_ACQ_REL);
        if (e)
            __atomic_add_fetch(&st_discard, 1, __ATOMIC_RELAXED);
        seg_unref(e);
    }
}

// A swap server must not need new memory while serving: when memory is full, any allocation
// of ours could wait forever (we may not start I/O to free memory - that I/O could be our own).
// So before the device starts, every queue thread touches its stack and runs real copies in
// both directions, letting CUDA set up whatever it needs per thread and stream right then.
static __attribute__((noinline)) void prefault_stack(void) {
    volatile char pad[512 * 1024];
    for (size_t i = 0; i < sizeof pad; i += 4096)
        pad[i] = 0;
}

static int warm_up(struct queue *q) {
    if (LZ4_compress_default) {   // compressed mode: run lz4 once in this thread, both ways
        char *page = q->bufs;
        for (unsigned i = 0; i < 4096; i++)
            page[i] = (char)(i * 7 % 13);
        int n = LZ4_compress_default(page, q->cbuf, 4096, (int)sizeof q->cbuf);
        if (n <= 0 || LZ4_decompress_safe(q->cbuf, page, n, 4096) != 4096)
            return -1;
    }
    for (int round = 0; round < 2; round++)
        for (int tag = 0; tag < depth; tag++) {
            char *buf = q->bufs + (size_t)tag * IO_BUF;
            size_t len = tag % 2 ? 4096 : IO_BUF;   // small and large requests
            if (dev_copy(q, 1, (size_t)q->id * IO_BUF, buf, len) || dev_copy(q, 0, (size_t)q->id * IO_BUF, buf, len))
                return -1;
        }
    return dev_sync(q) ? -1 : 0;
}

#ifndef MADV_POPULATE_WRITE
#define MADV_POPULATE_WRITE 23
#endif
static void populate_heap(void) {
    char line[512];
    FILE *f = fopen("/proc/self/maps", "r");
    while (f && fgets(line, sizeof line, f)) {
        unsigned long a, b;
        if (strstr(line, "[heap]") && sscanf(line, "%lx-%lx", &a, &b) == 2 &&
            madvise((void *)a, b - a, MADV_POPULATE_WRITE))
            perror("madvise(MADV_POPULATE_WRITE) heap");
    }
    if (f)
        fclose(f);
}

static void thread_faults(long *minflt, long *majflt) {
    struct rusage ru;
    getrusage(RUSAGE_THREAD, &ru);
    *minflt = ru.ru_minflt;
    *majflt = ru.ru_majflt;
}

// One thread per queue. The thread that fetches a queue's requests is the one that must serve them.
static void *serve_queue(void *arg) {
    struct queue *q = arg;
    // The kernel's request descriptors of queue N sit at a fixed offset in the character device.
    size_t slot = ((size_t)UBLK_MAX_QUEUE_DEPTH * sizeof(struct ublksrv_io_desc) + 4095) & ~(size_t)4095;
    q->desc_len = ((size_t)depth * sizeof(struct ublksrv_io_desc) + 4095) & ~(size_t)4095;
    q->descs = mmap(NULL, q->desc_len, PROT_READ, MAP_SHARED | MAP_POPULATE, cfd,
                    UBLKSRV_CMD_BUF_OFFSET + (off_t)(q->id * slot));
    if (q->descs == MAP_FAILED || (!fake_mem && (cuCtxSetCurrent(ctx) || cuStreamCreate(&q->stream, STREAM_NON_BLOCKING))) ||
        io_uring_queue_init(depth * 2 + 2, &q->ring, IORING_SETUP_SQE128) || warm_up(q)) {
        fprintf(stderr, "queue %d: setup failed\n", q->id);
        mark_ready(q, 1);
        return NULL;
    }
    q->ring_ok = 1;
    struct io_uring_sqe *qsqe = get_sqe128(&q->ring);
    io_uring_prep_poll_add(qsqe, quit_fd, POLLIN);
    qsqe->user_data = QUIT_TAG;
    for (int tag = 0; tag < depth; tag++)
        queue_io_cmd(q, UBLK_U_IO_FETCH_REQ, tag, -1);
    io_uring_submit(&q->ring);
    mark_ready(q, 0);

    int *tags = calloc(depth, sizeof(int)), *results = calloc(depth, sizeof(int));
    prefault_stack();
    long minflt0, majflt0;
    thread_faults(&minflt0, &majflt0);
    int quit = 0, aborted = 0;
    while (!quit && aborted < depth) {
        io_uring_submit(&q->ring);   // hand back the previous batch
        if (poll_us > 0) {           // busy-poll for a while before going to sleep
            double until = now_us() + poll_us;
            while (!io_uring_cq_ready(&q->ring) && now_us() < until)
                ;
        }
        struct io_uring_cqe *cqe;
        int ret = io_uring_wait_cqe(&q->ring, &cqe);
        if (ret == -EINTR)
            continue;
        if (ret < 0) {
            fprintf(stderr, "queue %d: io_uring_wait_cqe: %s\n", q->id, strerror(-ret));
            break;
        }
        double t0 = now_us();
        unsigned head, seen = 0;
        int n = 0;
        pthread_rwlock_rdlock(&move_lock);   // chunks stay where they are until this batch is done
        io_uring_for_each_cqe(&q->ring, head, cqe) {
            seen++;
            if (cqe->user_data == QUIT_TAG) {
                quit = 1;
                continue;
            }
            int tag = (int)cqe->user_data;
            if (cqe->res != UBLK_IO_RES_OK) {   // UBLK_IO_RES_ABORT: the device is going away
                if (cqe->res != UBLK_IO_RES_ABORT && !aborted)
                    fprintf(stderr, "queue %d: io command failed: %s\n", q->id, strerror(-cqe->res));
                aborted++;
                continue;
            }
            const struct ublksrv_io_desc *iod = &q->descs[tag];
            unsigned op = iod->op_flags & 0xff;
            unsigned long long off = iod->start_sector << 9;
            unsigned len = iod->nr_sectors << 9;
            char *buf = q->bufs + (size_t)tag * IO_BUF;
            int res = (int)len;
            if (op == UBLK_IO_OP_READ || op == UBLK_IO_OP_WRITE) {
                int w = op == UBLK_IO_OP_WRITE;
                if (off + len > dev_bytes || len > IO_BUF || (cz_ratio && (off | len) % PG)) {
                    res = -EIO;
                } else if (cz_ratio ? cz_io(q, w, off, buf, len, tag, n) : dev_io(q, w, off, buf, len)) {
                    res = -EIO;
                }
                if (w) {
                    q->writes++;
                    q->bytes_w += len;
                } else {
                    q->reads++;
                    q->bytes_r += len;
                }
            } else if (op == UBLK_IO_OP_FLUSH) {
                res = 0;
            } else if (op == UBLK_IO_OP_DISCARD && cz_ratio) {   // swap gave these pages up
                if (off + len <= dev_bytes)
                    cz_discard(off, len);
                res = 0;
            } else {
                res = -EOPNOTSUPP;
            }
            tags[n] = tag;
            results[n] = res;
            n++;
        }
        io_uring_cq_advance(&q->ring, seen);
        int sync_failed = dev_sync(q) != 0;   // one wait for the whole batch
        for (unsigned i = 0; i < q->njobs; i++) {   // compressed pages that came from VRAM
            struct dz_job *j = &q->jobs[i];
            if (LZ4_decompress_safe(j->src, j->dst, (int)j->len, PG) != (int)PG)
                results[j->idx] = -EIO;
        }
        q->njobs = 0;
        pthread_rwlock_unlock(&move_lock);
        if (n) {   // then complete all of it
            for (int i = 0; i < n; i++) {
                int res = sync_failed && results[i] > 0 ? -EIO : results[i];
                if (res < 0)
                    q->errors++;
                queue_io_cmd(q, UBLK_U_IO_COMMIT_AND_FETCH_REQ, tags[i], res);
            }
            q->batches++;
            q->busy_us += now_us() - t0;
        }
    }
    io_uring_submit(&q->ring);
    thread_faults(&q->minflt, &q->majflt);
    q->minflt -= minflt0;
    q->majflt -= majflt0;
    free(tags);
    free(results);
    return NULL;
}

// ---- the mover: spill (VRAM -> file) and fill (file -> VRAM), one chunk at a time ----
// For each chunk: take the lock, copy VRAM -> RAM stage (fast) and mark the chunk "moving", drop
// the lock, write the stage to the disk (slow - requests keep being served meanwhile, from the old
// place), take the lock again: if nothing wrote to the chunk meanwhile, it now lives in the new
// place; otherwise copy it again (after 3 tries: with the lock held throughout). Fill: the same
// the other way round. Chunks that were never written (unused swap space) are not copied at all.
enum { M_NONE, M_SPILL, M_FILL, M_QUIT };
static pthread_mutex_t move_mx = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t move_cv = PTHREAD_COND_INITIALIZER;
static int move_cmd, mover_state;   // mover_state: 0 starting, 1 ready, -1 setup failed
static CUstream move_stream;
static char *stage;   // CHUNK of pinned RAM

// A different request arrived (or quit): stop the current move between two chunks.
static int interrupted(int current) {
    pthread_mutex_lock(&move_mx);
    if (move_cmd == current)
        move_cmd = M_NONE;
    int stop = move_cmd != M_NONE;
    pthread_mutex_unlock(&move_mx);
    return stop;
}

static int chunk_copy(int to_vram, size_t c, char *buf) {
    unsigned long long off = (unsigned long long)c * CHUNK;
    if (fake_mem) {
        if (to_vram)
            memcpy(fake_mem + off, buf, CHUNK);
        else
            memcpy(buf, fake_mem + off, CHUNK);
        fake_wait((CHUNK / 4096) * (to_vram ? 1800 : 1400) + (to_vram ? 2900 : 3100));
        return 0;
    }
    CUresult r = to_vram ? cuMemcpyHtoDAsync(vptr(off), buf, CHUNK, move_stream)
                         : cuMemcpyDtoHAsync(buf, vptr(off), CHUNK, move_stream);
    return r || cuStreamSynchronize(move_stream) ? -1 : 0;
}

static int stage_copy(int to_vram, size_t c) {
    return chunk_copy(to_vram, c, stage);
}

// ---- compressed mode: the compactor (runs in the mover thread) ----
// Picks closed segments that are mostly dead, copies their live pages into its own open segment,
// then points the map at the new places (compare-and-swap: a page rewritten meanwhile keeps its
// newer place). Readers may still be copying from an old place, so the old segments are freed only
// after every batch that started before has finished (taking the batch lock for writing once).
#define GC_VICTIMS 16
static int gc_wanted;
// The compactor's open segment.
struct gc_move { size_t page; uint64_t old, neu; size_t from; };
static struct gc_dest {
    long seg;
    unsigned fill;
    char *buf;                 // CHUNK of pinned RAM
    struct gc_move *moves;     // pages copied into buf, not visible yet
    size_t n;
} gdst = {.seg = -1};

static void want_gc(void) {
    if (__atomic_load_n(&gc_wanted, __ATOMIC_RELAXED))
        return;
    pthread_mutex_lock(&move_mx);
    gc_wanted = 1;
    pthread_cond_signal(&move_cv);
    pthread_mutex_unlock(&move_mx);
}

// Free segments: all of them (in_vram = 0) or those in VRAM.
static size_t free_count(int in_vram) {
    pthread_mutex_lock(&seg_mx);
    size_t n = free_n + (in_vram ? 0 : ffree_n);
    pthread_mutex_unlock(&seg_mx);
    return n;
}

static long gc_seg_alloc(void) {
    long s = -1;
    pthread_mutex_lock(&seg_mx);
    if (free_n)
        s = free_segs[--free_n];
    else if (ffree_n)
        s = ffree_segs[--ffree_n];
    pthread_mutex_unlock(&seg_mx);
    if (s >= 0) {
        __atomic_store_n(&seg_open[s], nr_queues + 1, __ATOMIC_RELEASE);
        __atomic_store_n(&chunk_written[s], 1, __ATOMIC_RELAXED);
    }
    return s;
}

// Write a compactor segment out and make the moved pages visible there. close: also close it.
static void gc_flush(struct gc_dest *d, int close) {
    if (d->seg < 0)
        return;
    size_t s = (size_t)d->seg;
    int bad = d->n && (chunk_state[s] == C_VRAM ? chunk_copy(1, s, d->buf)
                                                : file_io(1, (unsigned long long)s * CHUNK, d->buf, CHUNK));
    if (!bad)
        for (size_t i = 0; i < d->n; i++) {
            struct gc_move *m = &d->moves[i];
            uint64_t expected = m->old;
            if (__atomic_compare_exchange_n(&pmap[m->page], &expected, m->neu, 0, __ATOMIC_ACQ_REL,
                                            __ATOMIC_ACQUIRE)) {
                __atomic_add_fetch(&seg_live[s], e_len(m->neu), __ATOMIC_ACQ_REL);
                __atomic_sub_fetch(&seg_live[m->from], e_len(m->old), __ATOMIC_ACQ_REL);
                st_gc_bytes += e_len(m->neu);
            }
        }
    d->n = 0;
    if (close || bad) {
        __atomic_store_n(&seg_open[s], S_CLOSED, __ATOMIC_RELEASE);
        d->seg = -1;
        d->fill = 0;
        seg_try_free(s);
    }
}

// One round: up to GC_VICTIMS mostly-dead segments. Returns how many were taken.
static int gc_pass(void) {
    struct gc_dest *d = &gdst;
    size_t victims[GC_VICTIMS];
    int nv = 0, stuck = 0;
    while (nv < GC_VICTIMS && !stuck) {
        long v = -1;
        uint32_t best = CHUNK * 2 / 3;   // more live than this: copying costs more than it frees
        for (size_t c = 0; c < nr_chunks; c++)
            if (__atomic_load_n(&seg_open[c], __ATOMIC_ACQUIRE) == S_CLOSED &&
                __atomic_load_n(&seg_live[c], __ATOMIC_ACQUIRE) < best) {
                best = __atomic_load_n(&seg_live[c], __ATOMIC_ACQUIRE);
                v = (long)c;
            }
        int closed = S_CLOSED;
        if (v < 0 || !__atomic_compare_exchange_n(&seg_open[v], &closed, S_VICTIM, 0, __ATOMIC_ACQ_REL,
                                                  __ATOMIC_ACQUIRE))
            break;
        victims[nv++] = (size_t)v;
        if (chunk_state[v] == C_VRAM ? stage_copy(0, (size_t)v)
                                     : file_io(0, (unsigned long long)v * CHUNK, stage, CHUNK))
            break;
        size_t pages = dev_bytes / PG;
        for (size_t p = 0; p < pages; p++) {
            uint64_t e = __atomic_load_n(&pmap[p], __ATOMIC_ACQUIRE);
            if (!e_stored(e) || e_seg(e) != (size_t)v)
                continue;
            unsigned len = e_len(e), room = (len + 15) & ~15u;
            if (d->seg < 0 || d->fill + room > CHUNK) {
                gc_flush(d, 1);
                d->seg = gc_seg_alloc();
                d->fill = 0;
                if (d->seg < 0) {   // not even one free segment: stop here (the rest stays put)
                    stuck = 1;
                    break;
                }
            }
            memcpy(d->buf + d->fill, stage + e_off(e), len);
            d->moves[d->n++] = (struct gc_move){p, e, ent((size_t)d->seg, d->fill, len), (size_t)v};
            d->fill += room;
        }
    }
    if (!nv)
        return 0;
    gc_flush(d, 0);   // the moved pages are visible now; the compactor's segment stays open for more
    pthread_rwlock_wrlock(&move_lock);   // every batch that might still read an old place is done
    pthread_rwlock_unlock(&move_lock);
    for (int i = 0; i < nv; i++) {
        __atomic_store_n(&seg_open[victims[i]], S_CLOSED, __ATOMIC_RELEASE);
        seg_try_free(victims[i]);
    }
    st_gc_runs++;
    return stuck ? 0 : nv;
}

static void gc(void) {
    __atomic_store_n(&gc_wanted, 0, __ATOMIC_RELAXED);
    if (!cz_ratio)
        return;
    for (int round = 0; round < 64 && free_count(0) < gc_high; round++)
        if (!gc_pass())
            break;
}

// ---- the standby swap ----
// The one way this program could hang the whole machine: waiting for the disk while serving swap.
// Reading or writing a file needs a little kernel memory. When memory has run out, that memory only
// comes back once swap writes finish - and if all of them wait for us, nothing ever finishes.
// So we touch the disk only while the kernel has another way to free memory: the standby swap,
// ordinary swap files switched on at a priority above our device. Swap writes then go there, through
// the kernel's own swap code, which is built to work with no free memory left; ours can wait.
// It is on (1) while the VRAM is nearly full: new pages then go to the disk, and the room left in
// the VRAM takes rewrites of pages already here, and (2) while any data is in the spill file.
// While it is off or full, writes that would need the disk are refused (the kernel keeps those pages
// in RAM) and the mover never makes the queues wait for the disk. When it fills up while needed, one
// more file of the same size is added (as long as the disk keeps 10% free, at least 2 GiB).
// It goes off again once the VRAM has room and its pages fit in RAM (swapoff, called off if RAM or
// VRAM get short meanwhile); the files added go away then.
enum { SB_NONE, SB_OFF, SB_ON, SB_GOING_OFF };
static const char *sb_names[] = {"none", "off", "on", "going-off"};
#define SB_FILES 16
static char sb_paths[SB_FILES][PATH_MAX];   // [0] USM_VRAMD_STANDBY (made by usm-vramd-ctl), then the ones added
static int sb_n = 1;                        // how many: while on, all of them are on; while off, only [0] exists
static long long sb_step;                   // bytes: the size of [0], and of each one added
static int sb_prio;                         // USM_VRAMD_STANDBY_PRIO: above our device's
static int sb_state = SB_NONE;
static pthread_mutex_t sb_mx = PTHREAD_MUTEX_INITIALIZER;   // state, sb_n (held across swapon)
static pthread_cond_t sb_cv = PTHREAD_COND_INITIALIZER;     // a swapoff of it finished
static pthread_t sb_worker;                                 // runs that swapoff
static double sb_retry_on, sb_retry_off, sb_retry_grow;     // now_us(): not before
static pthread_mutex_t sb_wake_mx = PTHREAD_MUTEX_INITIALIZER;   // only for waking the controller
static pthread_cond_t sb_wake_cv = PTHREAD_COND_INITIALIZER;
static int sb_wake, sb_quit;

static void want_standby(void) {
    if (__atomic_load_n(&sb_wake, __ATOMIC_RELAXED) || __atomic_load_n(&sb_state, __ATOMIC_RELAXED) == SB_NONE)
        return;
    pthread_mutex_lock(&sb_wake_mx);
    sb_wake = 1;
    pthread_cond_signal(&sb_wake_cv);
    pthread_mutex_unlock(&sb_wake_mx);
}

static void on_sigurg(int sig) {   // only there to interrupt the swapoff of the standby swap
    (void)sig;
}

static void timeout_in(struct timespec *t, long ms) {
    clock_gettime(CLOCK_REALTIME, t);
    t->tv_nsec += ms * 1000000L;
    t->tv_sec += t->tv_nsec / 1000000000L;
    t->tv_nsec %= 1000000000L;
}

// A small file of /proc into buf (no malloc: this runs when memory is short).
static int read_proc(const char *name, char *buf, size_t size) {
    int fd = open(name, O_RDONLY | O_CLOEXEC);
    if (fd < 0)
        return -1;
    size_t n = 0;
    ssize_t r;
    while (n + 1 < size && (r = read(fd, buf + n, size - 1 - n)) > 0)
        n += (size_t)r;
    close(fd);
    buf[n] = 0;
    return 0;
}

// Size and use (KiB) of the standby files that are on; returns how many are (caller holds sb_mx).
static int sb_usage(long long *size, long long *used) {
    char buf[8192], name[PATH_MAX];
    long long sz, us;
    int found = 0;
    *size = *used = 0;
    if (read_proc("/proc/swaps", buf, sizeof buf))
        return 0;
    for (char *l = strchr(buf, '\n'); l && *++l; l = strchr(l, '\n'))
        if (sscanf(l, "%4095s %*s %lld %lld", name, &sz, &us) == 3)
            for (int i = 0; i < sb_n; i++)
                if (!strcmp(name, sb_paths[i])) {
                    *size += sz;
                    *used += us;
                    found++;
                }
    return found;
}

static long long meminfo_kib(const char *key) {
    char buf[4096];
    if (read_proc("/proc/meminfo", buf, sizeof buf))
        return -1;
    const char *p = strstr(buf, key);
    return p ? strtoll(p + strlen(key), NULL, 10) : -1;
}

// RAM below this much available: too short to take the standby's pages back (KiB).
static long long ram_low_kib(void) {
    long long total = meminfo_kib("MemTotal:");
    return total / 25 > 262144 ? total / 25 : 262144;
}

// Is there room on the standby swap for what the kernel may still send there? Returns its free KiB
// (caller holds sb_mx).
static long long sb_update_room(void) {
    long long size = 0, used = 0;
    int on = sb_state == SB_ON && sb_usage(&size, &used);
    long long margin = size / 16 > 65536 ? size / 16 : 65536;   // 1/16 of it, at least 64 MiB
    __atomic_store_n(&sb_room_ok, on && size - used >= margin, __ATOMIC_RELEASE);
    return on ? size - used : 0;
}

static int sb_swapon(const char *path) {
    return !swapon(path, SWAP_FLAG_PREFER | (sb_prio & SWAP_FLAG_PRIO_MASK)) || errno == EBUSY ? 0 : -1;
}

// Make sure the standby swap is on. Returns 1 if it is.
static int standby_on(const char *why) {
    pthread_mutex_lock(&sb_mx);
    while (sb_state == SB_GOING_OFF) {   // call off the swapoff of it under way
        pthread_kill(sb_worker, SIGURG);
        struct timespec t;
        timeout_in(&t, 50);
        pthread_cond_timedwait(&sb_cv, &sb_mx, &t);
    }
    if (sb_state == SB_OFF && now_us() >= sb_retry_on) {
        if (!sb_swapon(sb_paths[0])) {
            __atomic_store_n(&sb_state, SB_ON, __ATOMIC_RELEASE);
            sb_retry_off = now_us() + 30e6;   // not off again right away
            printf("standby swap on: %s at priority %d (%s)\n", sb_paths[0], sb_prio, why);
        } else {
            printf("standby swap: cannot switch %s on: %s\n", sb_paths[0], strerror(errno));
            sb_retry_on = now_us() + 10e6;
        }
    }
    int on = sb_state == SB_ON;
    if (on) {
        sb_update_room();
        if (sb_retry_off < now_us() + 30e6)   // whoever needs it now: not off again for a while
            sb_retry_off = now_us() + 30e6;
    }
    pthread_mutex_unlock(&sb_mx);
    return on;
}

// mkswap: the header page of a swap file of this many bytes.
static int write_swap_header(int fd, long long bytes) {
    static char page[65536];   // only the controller uses it
    long ps = sysconf(_SC_PAGESIZE);
    if (ps <= 0 || ps > (long)sizeof page)
        return -1;
    memset(page, 0, (size_t)ps);
    uint32_t info[3] = {1, (uint32_t)(bytes / ps - 1), 0};   // version, last page, no bad pages
    memcpy(page + 1024, info, sizeof info);
    memcpy(page + 1024 + 12 + 16, "usmstandby", 10);         // after the (zero) UUID: the label
    memcpy(page + ps - 10, "SWAPSPACE2", 10);
    return pwrite(fd, page, (size_t)ps, 0) == ps ? 0 : -1;
}

#ifndef BTRFS_SUPER_MAGIC
#define BTRFS_SUPER_MAGIC 0x9123683E
#endif
// The standby swap is filling up while it is needed: add one more file of the same size, if the disk
// keeps 10% (at least 2 GiB) free after it. Controller thread only.
static void sb_grow(void) {
    static int told;
    if (sb_n >= SB_FILES || now_us() < sb_retry_grow)
        return;
    sb_retry_grow = now_us() + 30e6;   // if this fails: not again right away
    char dir[PATH_MAX], path[PATH_MAX];
    snprintf(dir, sizeof dir, "%s", sb_paths[0]);
    char *slash = strrchr(dir, '/');
    if (!slash)
        return;
    *slash = 0;
    struct statfs fs;
    if (statfs(dir, &fs))
        return;
    long long free_b = (long long)fs.f_bavail * fs.f_bsize, total = (long long)fs.f_blocks * fs.f_bsize;
    long long keep = total / 10 > (2LL << 30) ? total / 10 : (2LL << 30);
    if (free_b - sb_step < keep) {
        if (!told++)
            printf("standby swap nearly full, and the disk has no room for more (%lld MiB free)\n", free_b >> 20);
        return;
    }
    int len = (int)strlen(sb_paths[0]);
    if (len > 5 && !strcmp(sb_paths[0] + len - 5, ".swap"))
        len -= 5;
    int fd = -1;
    for (int i = sb_n + 1; fd < 0 && i < 100; i++) {
        snprintf(path, sizeof path, "%.*s-%d.swap", len, sb_paths[0], i);
        fd = open(path, O_RDWR | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
        if (fd < 0 && errno != EEXIST)
            break;
    }
    if (fd < 0) {
        printf("standby swap: cannot add a file in %s: %s\n", dir, strerror(errno));
        return;
    }
    if (fs.f_type == BTRFS_SUPER_MAGIC) {   // a swap file on btrfs: no copy-on-write
        int fl = 0;
        if (!ioctl(fd, FS_IOC_GETFLAGS, &fl)) {
            fl |= FS_NOCOW_FL;
            ioctl(fd, FS_IOC_SETFLAGS, &fl);
        }
    }
    double t0 = now_us();
    int bad = fallocate(fd, 0, 0, (off_t)sb_step) || write_swap_header(fd, sb_step) || fsync(fd);
    int err = errno;
    close(fd);
    if (bad || sb_swapon(path)) {
        printf("standby swap: cannot add %s: %s\n", path, strerror(bad ? err : errno));
        unlink(path);
        return;
    }
    pthread_mutex_lock(&sb_mx);
    if (sb_state != SB_ON) {   // switched off meanwhile: not needed any more
        pthread_mutex_unlock(&sb_mx);
        swapoff(path);
        unlink(path);
        return;
    }
    snprintf(sb_paths[sb_n], PATH_MAX, "%s", path);
    sb_n++;
    sb_update_room();
    pthread_mutex_unlock(&sb_mx);
    sb_retry_grow = 0;
    printf("standby swap nearly full: added %s (%lld MiB, %d files now, %.1f s)\n", path, sb_step >> 20, sb_n,
           (now_us() - t0) / 1e6);
}

static void *sb_off_main(void *arg) {
    long long used = (long long)(intptr_t)arg;
    double t0 = now_us();
    int r = 0, err = 0;
    for (;;) {   // the files added first (newest first; they are deleted), the first one last
        pthread_mutex_lock(&sb_mx);
        int i = sb_n - 1;
        pthread_mutex_unlock(&sb_mx);
        if (swapoff(sb_paths[i])) {
            r = -1;
            err = errno;
            break;
        }
        if (!i)
            break;
        pthread_mutex_lock(&sb_mx);
        unlink(sb_paths[i]);
        sb_n = i;
        pthread_mutex_unlock(&sb_mx);
    }
    pthread_mutex_lock(&sb_mx);
    __atomic_store_n(&sb_state, r ? SB_ON : SB_OFF, __ATOMIC_RELEASE);
    if (r)
        sb_retry_off = now_us() + 60e6;
    __atomic_store_n(&sb_room_ok, 0, __ATOMIC_RELEASE);   // the controller checks again
    pthread_cond_broadcast(&sb_cv);
    pthread_mutex_unlock(&sb_mx);
    if (r)
        printf("standby swap stays on: %s\n", err == EINTR ? "RAM or VRAM got short again" : strerror(err));
    else
        printf("standby swap off: %lld MiB moved into RAM in %.1f s - new pages go to the VRAM again\n", used >> 10,
               (now_us() - t0) / 1e6);
    return NULL;
}

// The VRAM has room again: switch the standby swap off if its pages fit in RAM.
static void sb_try_off(void) {
    long long size = 0, used = 0, avail = meminfo_kib("MemAvailable:");
    pthread_mutex_lock(&sb_mx);
    if (sb_state == SB_ON && !sb_usage(&size, &used)) {   // someone else switched it off
        __atomic_store_n(&sb_state, SB_OFF, __ATOMIC_RELEASE);
        while (sb_n > 1)
            unlink(sb_paths[--sb_n]);
        printf("standby swap: %s was switched off from outside\n", sb_paths[0]);
    } else if (sb_state == SB_ON && (avail < 0 || used + 2 * ram_low_kib() > avail)) {
        sb_retry_off = now_us() + 5e6;   // its pages would not fit in RAM yet
    } else if (sb_state == SB_ON) {
        __atomic_store_n(&sb_state, SB_GOING_OFF, __ATOMIC_RELEASE);
        __atomic_store_n(&sb_room_ok, 0, __ATOMIC_RELEASE);
        pthread_attr_t a;
        pthread_attr_init(&a);
        pthread_attr_setdetachstate(&a, PTHREAD_CREATE_DETACHED);
        if (pthread_create(&sb_worker, &a, sb_off_main, (void *)(intptr_t)used)) {
            __atomic_store_n(&sb_state, SB_ON, __ATOMIC_RELEASE);
            sb_retry_off = now_us() + 60e6;
        }
        pthread_attr_destroy(&a);
    }
    pthread_mutex_unlock(&sb_mx);
}

static void *sb_main(void *arg) {
    (void)arg;
    // We never serve I/O here: our swapon / swapoff may free memory the normal way, with I/O.
    prctl(PR_SET_IO_FLUSHER, 0, 0, 0, 0);
    for (;;) {
        pthread_mutex_lock(&sb_wake_mx);
        if (!sb_wake && !sb_quit) {
            struct timespec t;
            timeout_in(&t, 200);
            pthread_cond_timedwait(&sb_wake_cv, &sb_wake_mx, &t);
        }
        sb_wake = 0;
        int quit = sb_quit;
        pthread_mutex_unlock(&sb_wake_mx);
        if (quit)
            break;
        size_t fr = cz_ratio ? free_count(1) : nr_chunks;
        int in_file = __atomic_load_n(&spill_fd, __ATOMIC_RELAXED) >= 0;   // data in (or on its way to) the spill file
        int full = cz_ratio && __atomic_load_n(&slabs_held, __ATOMIC_RELAXED) && fr < sb_on_free;
        int st = __atomic_load_n(&sb_state, __ATOMIC_ACQUIRE);
        if (full || in_file) {
            if (st != SB_ON)
                standby_on(full ? "the VRAM is nearly full" : "data in the spill file");
        } else if (st == SB_GOING_OFF) {
            long long avail = meminfo_kib("MemAvailable:");
            if (avail >= 0 && avail < ram_low_kib())
                standby_on("RAM got short while its pages moved back");
        } else if (st == SB_ON && fr >= sb_off_free && now_us() >= sb_retry_off) {
            sb_try_off();
        }
        pthread_mutex_lock(&sb_mx);
        long long room = sb_state == SB_ON ? sb_update_room() : -1;
        pthread_mutex_unlock(&sb_mx);
        // Needed (the device cannot take new pages now) and filling up: add a file before it is full.
        long long low = sb_step / 4096 > 262144 ? sb_step / 4096 : 262144;   // KiB: 1/4 of a file, at least 256 MiB
        if ((full || in_file) && room >= 0 && room < low)
            sb_grow();
    }
    pthread_mutex_lock(&sb_mx);   // stopping: a swapoff under way is called off (usm-vramd-ctl decides)
    while (sb_state == SB_GOING_OFF) {
        pthread_kill(sb_worker, SIGURG);
        struct timespec t;
        timeout_in(&t, 50);
        pthread_cond_timedwait(&sb_cv, &sb_mx, &t);
    }
    pthread_mutex_unlock(&sb_mx);
    return NULL;
}

static int written(size_t c) {
    return __atomic_load_n(&chunk_written[c], __ATOMIC_RELAXED);
}

// Move chunk c to the other place. Returns 1 = moved with a copy, 0 = moved without (never
// written), -1 = copy or disk error (the chunk stays where it was), -2 = not now (it keeps being
// written to, and the standby swap has no room: the queues must not wait for the disk).
static int move_chunk(size_t c, int to_file, size_t *again) {
    int from = to_file ? C_VRAM : C_FILE, to = to_file ? C_FILE : C_VRAM;
    unsigned long long off = (unsigned long long)c * CHUNK;
    for (int tries = 0;; tries++) {
        // Still being written to: hold the lock for the whole copy. The queues then wait for the disk
        // too, which is safe only while the standby swap has room.
        int last = tries >= 3;
        if (last && !__atomic_load_n(&sb_room_ok, __ATOMIC_ACQUIRE))
            return -2;
        pthread_rwlock_wrlock(&move_lock);
        if (!written(c)) {
            chunk_state[c] = to;
            pthread_rwlock_unlock(&move_lock);
            return 0;
        }
        if (to_file && stage_copy(0, c)) {
            chunk_state[c] = from;
            pthread_rwlock_unlock(&move_lock);
            return -1;
        }
        chunk_state[c] = to_file ? C_TO_FILE : C_TO_VRAM;
        chunk_dirty[c] = 0;
        if (!last)
            pthread_rwlock_unlock(&move_lock);
        int bad = file_io(to_file, off, stage, CHUNK);   // the slow part
        if (!last)
            pthread_rwlock_wrlock(&move_lock);
        if (!bad && !chunk_dirty[c] && !to_file)
            bad = stage_copy(1, c);
        if (bad) {
            chunk_state[c] = from;
            pthread_rwlock_unlock(&move_lock);
            return -1;
        }
        if (!chunk_dirty[c]) {
            chunk_state[c] = to;
            pthread_rwlock_unlock(&move_lock);
            return 1;
        }
        pthread_rwlock_unlock(&move_lock);   // written to while we copied: once more
        (*again)++;
    }
}

static size_t count_state(int st, int only_written) {
    size_t n = 0;
    for (size_t c = 0; c < nr_chunks; c++)
        n += chunk_state[c] == st && (!only_written || written(c));
    return n;
}

// Chunks [*c0, *c1) of slab k.
static void slab_chunks(size_t k, size_t *c0, size_t *c1) {
    *c0 = k * CPS;
    *c1 = *c0 + slab_len(k) / CHUNK;
}

static size_t slab_count(size_t k, int st) {
    size_t c0, c1, n = 0;
    slab_chunks(k, &c0, &c1);
    for (size_t c = c0; c < c1; c++)
        n += chunk_state[c] == st;
    return n;
}

static size_t slab_data(size_t k) {   // chunks with data
    size_t c0, c1, n = 0;
    slab_chunks(k, &c0, &c1);
    for (size_t c = c0; c < c1; c++)
        n += written(c);
    return n;
}

// ---- the spill file ----
static int spill_mode;   // how its space is reserved: 2 written zeros, 1 plain, 0 only for the chunks with data
static const char *spill_how[] = {"space reserved for the current data only (disk too full for all of it)",
                                  "space reserved", "zeroed in advance"};

// Open (create) it. Returns NULL, or why not.
static const char *spill_open(void) {
    static char why[PATH_MAX + 128];
    if (spill_fd >= 0)
        return NULL;
    if (!spill_path)
        return "no spill file set (USM_VRAMD_SPILL)";
    int fd = open(spill_path, O_RDWR | O_CREAT | O_EXCL | O_DIRECT | O_CLOEXEC, 0600);
    if (fd < 0 || ftruncate(fd, (off_t)vram_bytes)) {
        snprintf(why, sizeof why, "%s: %s", spill_path, strerror(errno));
        if (fd >= 0) {
            close(fd);
            unlink(spill_path);
        }
        return why;
    }
    spill_mode = 2;
    // The queues use it only for chunks that move there later (each under the lock).
    __atomic_store_n(&spill_fd, fd, __ATOMIC_RELEASE);
    return NULL;
}

// Reserve disk space for the chunks of [c0, c1) that are in VRAM now: no "disk full" later, and no block
// allocation while serving. Their place in the file holds nothing, so it may be zeroed - written zeros, if the
// disk can do that without sending data: then later writes need no file system changes either (direct
// overwrites of allocated blocks). Returns 0, or -1 (errno set).
static int spill_reserve(size_t c0, size_t c1) {
    for (size_t c = c0; c < c1;) {
        if (chunk_state[c] != C_VRAM) {
            c++;
            continue;
        }
        size_t e = c + 1;
        while (e < c1 && chunk_state[e] == C_VRAM)
            e++;
        off_t a = (off_t)c * CHUNK, len = (off_t)(e - c) * CHUNK;
        if (spill_mode == 2 && fallocate(spill_fd, FALLOC_FL_WRITE_ZEROES, a, len))
            spill_mode = 1;
        if (spill_mode == 1 && fallocate(spill_fd, 0, a, len)) {
            // Not enough disk space for all of it: drop what got reserved of this part, then reserve room for
            // the data there is now; space for swap space never used so far is taken when it gets used.
            fallocate(spill_fd, FALLOC_FL_PUNCH_HOLE | FALLOC_FL_KEEP_SIZE, a, len);
            spill_mode = 0;
        }
        if (!spill_mode)
            for (size_t i = c; i < e; i++)
                if (written(i) && fallocate(spill_fd, 0, (off_t)i * CHUNK, CHUNK))
                    return -1;
        c = e;
    }
    return 0;
}

// Nothing lives in it any more: close and delete it. Returns 1 if it did.
static int spill_close(void) {
    if (spill_fd < 0 || count_state(C_FILE, 0))
        return 0;
    pthread_rwlock_wrlock(&move_lock);
    close(spill_fd);
    __atomic_store_n(&spill_fd, -1, __ATOMIC_RELEASE);
    pthread_rwlock_unlock(&move_lock);
    unlink(spill_path);
    return 1;
}

// ---- giving VRAM back ----
// CUDA does not share VRAM between programs: once ours is taken, other programs' allocations simply fail
// (CUDA_ERROR_OUT_OF_MEMORY) - an AI model, a game, a video editor. So the mover watches the GPU's free memory
// (every GPU_TICK_MS). Other programs' use = total - free - ours. Once it is GPU_BUSY above what it was at our
// start, they are using the GPU: slabs go back to them (the least data first: empty ones at once, the others
// after their data moved to the spill file, as in a spill) until gpu_keep is free - and more while they keep
// growing. Once their use has been back down for GPU_QUIET_S, the slabs are taken again (as long as what was
// free at our start stays free) and their data moves back. A request to switch off (spill) stays: then nothing
// is taken back until a fill. There is no warning before an allocation: a program that takes a lot of VRAM in
// one go right at its start may still find too little free.
#define GPU_TICK_MS 50
#define GPU_BUSY (64ll << 20)
#define GPU_QUIET_S 30
#define FAKE_GPU_SPARE (256ll << 20)          // test mode: the fake GPU has this much more than we borrow
static long long gpu_keep = 1024ll << 20;     // USM_VRAMD_GPU_FREE_MB; 0 = never give VRAM back
static const char *gpu_fake;                  // test mode: USM_VRAMD_FAKE_GPU
static long long gpu_others0 = -1, gpu_free0; // other programs' use at our start (the lowest seen since); free then
static long long gpu_free_now = -1, gpu_others_now = -1;   // last seen (for the status line)
static double gpu_quiet_since, gpu_retry;
static int user_off;   // switched off on request (spill): nothing is taken back by itself
enum { R_DONE = 1, R_STOPPED = 0, R_FAILED = -1, R_BUSY = -2, R_NO_VRAM = -3 };

static const char *r_text(int r) {
    return r == R_FAILED ? "copy or disk error" : r == R_BUSY ? "busy chunk, standby swap full"
         : r == R_NO_VRAM ? "no VRAM free" : "a request came";
}

static int gpu_info(long long *fr, long long *others) {
    long long f, t, held = __atomic_load_n(&held_b, __ATOMIC_RELAXED);
    if (fake_mem) {
        char buf[64];
        if (!gpu_fake || read_proc(gpu_fake, buf, sizeof buf))
            return -1;
        t = (long long)vram_bytes + FAKE_GPU_SPARE;
        f = t - held - (strtoll(buf, NULL, 10) << 20);
        if (f < 0)
            f = 0;
    } else {
        size_t a, b;
        if (cuMemGetInfo(&a, &b))
            return -1;
        f = (long long)a;
        t = (long long)b;
    }
    *fr = f;
    *others = t - f - held;
    __atomic_store_n(&gpu_free_now, f, __ATOMIC_RELAXED);
    __atomic_store_n(&gpu_others_now, *others, __ATOMIC_RELAXED);
    return 0;
}

static int gpu_busy(long long others) {
    return gpu_others0 >= 0 && others > gpu_others0 + GPU_BUSY;
}

static CUresult slab_alloc(size_t k) {
    if (!fake_mem)
        return cuMemAlloc(&slab_ptr[k], slab_len(k));
    long long fr, others;   // test mode: only as much as the fake GPU has free
    return gpu_fake && !gpu_info(&fr, &others) && fr < (long long)slab_len(k) ? 2 : 0;
}

// Move slab k's data to the spill file (open, space reserved, the standby swap on) and give its VRAM back.
// Returns R_DONE (given back), R_STOPPED (cmd: see interrupted), or a move_chunk error (it stays, partly moved).
static int slab_out(size_t k, int cmd, size_t *copied, size_t *again) {
    size_t c0, c1;
    slab_chunks(k, &c0, &c1);
    slab_leaving[k] = 1;   // compressed mode: its free segments are not handed out meanwhile
    seg_repartition();
    int r = R_DONE;
    for (size_t c = c0; c < c1 && r == R_DONE; c++) {
        if (chunk_state[c] != C_VRAM)
            continue;
        if (interrupted(cmd)) {
            r = R_STOPPED;
            break;
        }
        if (__atomic_load_n(&gc_wanted, __ATOMIC_RELAXED))   // new pages keep coming meanwhile
            gc();
        int m = move_chunk(c, 1, again);
        if (m < 0)
            r = m;
        else
            *copied += (size_t)m;
    }
    if (r == R_DONE && slab_count(k, C_VRAM))
        r = R_STOPPED;
    if (r == R_DONE) {
        // Every batch that copied from this slab finished before its last chunk changed place (that took the
        // lock), and no batch since sees any of it in VRAM: give it back.
        if (fake_mem)
            memset(fake_mem + c0 * CHUNK, 0x5a, slab_len(k));   // test mode: whatever still reads there gets garbage
        else
            cuMemFree(slab_ptr[k]);
        slab_held[k] = 0;
        __atomic_sub_fetch(&slabs_held, 1, __ATOMIC_RELAXED);
        __atomic_sub_fetch(&held_b, (long long)slab_len(k), __ATOMIC_RELAXED);
    }
    slab_leaving[k] = 0;
    seg_repartition();
    return r;
}

// Get slab k's VRAM again (if not held) and move its data back from the spill file. keep >= 0: only if this
// much GPU memory stays free for other programs. *cuerr: the CUDA error if the VRAM could not be had.
static int slab_in(size_t k, int cmd, long long keep, size_t *copied, size_t *again, int *cuerr) {
    size_t c0, c1;
    slab_chunks(k, &c0, &c1);
    if (!slab_held[k]) {
        long long fr, others;
        if (keep >= 0 && (gpu_info(&fr, &others) || fr - (long long)slab_len(k) < keep))
            return R_NO_VRAM;
        CUresult e = slab_alloc(k);
        if (e) {
            *cuerr = e;
            return R_NO_VRAM;
        }
        slab_held[k] = 1;   // its chunks are all still in the file: the queues do not use it before they move
        __atomic_add_fetch(&slabs_held, 1, __ATOMIC_RELAXED);
        __atomic_add_fetch(&held_b, (long long)slab_len(k), __ATOMIC_RELAXED);
    }
    int r = R_DONE;
    for (size_t c = c0; c < c1; c++) {
        if (chunk_state[c] != C_FILE)
            continue;
        if (interrupted(cmd)) {
            r = R_STOPPED;
            break;
        }
        if (__atomic_load_n(&gc_wanted, __ATOMIC_RELAXED))
            gc();
        int m = move_chunk(c, 0, again);
        if (m < 0) {
            r = m;
            break;
        }
        *copied += (size_t)m;
    }
    seg_repartition();
    return r;
}

// The next slab to give back: held, with the least data (ties: the highest). -1: none.
static long slab_to_give(void) {
    long best = -1;
    size_t bd = 0;
    for (size_t k = nr_slabs; k-- > 0;)
        if (slab_held[k]) {
            size_t d = slab_data(k);
            if (best < 0 || d < bd) {
                best = (long)k;
                bd = d;
            }
        }
    return best;
}

// The next slab to take back: not held, with the most data (ties: the lowest); then held ones with data in
// the spill file. -1: none.
static long slab_to_take(void) {
    long best = -1;
    size_t bd = 0;
    for (size_t k = 0; k < nr_slabs; k++)
        if (!slab_held[k]) {
            size_t d = slab_data(k);
            if (best < 0 || d > bd) {
                best = (long)k;
                bd = d;
            }
        }
    for (size_t k = 0; best < 0 && k < nr_slabs; k++)
        if (slab_count(k, C_FILE))
            best = (long)k;
    return best;
}

static void gpu_yield(long long fr, long long others) {
    double t0 = now_us();
    size_t given = 0, copied = 0, again = 0;
    const char *why = NULL;
    char err[128];
    printf("yield: other programs use %lld MiB more VRAM than at the start, %lld MiB free - giving VRAM back"
           " until %lld MiB are free\n", (others - gpu_others0) >> 20, fr >> 20, gpu_keep >> 20);
    for (int more = 1; more && fr < gpu_keep;) {
        // As many slabs as are missing, then look again (the other program may keep growing). Few driver calls:
        // while another program's allocations keep failing, each of ours waits behind them in the driver.
        // Empty slabs go at once: nothing can be read from them, and writes there are refused until the standby
        // swap is on (below). Slabs with data move to the spill file first, with the standby swap on.
        for (long long need = gpu_keep - fr; need > 0;) {
            long k = slab_to_give();
            if (k < 0) {
                more = 0;   // all of it given back
                break;
            }
            int empty = !slab_data((size_t)k);
            if (!empty && !standby_on("VRAM goes back to other programs")) {
                why = "no standby swap to take new pages meanwhile (see STANDBY_MB)";
                break;
            }
            if ((why = spill_open()))
                break;
            if (!empty) {
                standby_on("data moves to the spill file");   // stays on while the file is open (the controller sees it)
                size_t c0, c1;
                slab_chunks((size_t)k, &c0, &c1);
                if (spill_reserve(c0, c1)) {
                    snprintf(err, sizeof err, "cannot reserve disk space in %s: %s", spill_path, strerror(errno));
                    why = err;
                    break;
                }
            }
            int r = slab_out((size_t)k, M_NONE, &copied, &again);
            if (r != R_DONE) {
                why = r_text(r);
                break;
            }
            given += slab_len((size_t)k);
            need -= (long long)slab_len((size_t)k);
        }
        if (why || gpu_info(&fr, &others))
            break;
    }
    if (given) {
        // The VRAM is out: now the standby swap goes on (and stays on while the spill file is open), and the space
        // of the slabs given back is reserved in the file - plainly, never zeroed: a write may have landed there.
        if (!standby_on("VRAM went back to other programs") && !why)
            why = "no standby swap yet: writes there are refused meanwhile";
        for (size_t k = 0; k < nr_slabs; k++)
            if (!slab_held[k] && fallocate(spill_fd, 0, (off_t)k * SLAB, (off_t)slab_len(k)))
                break;   // disk full: space is taken when it gets used
    }
    if (why)
        gpu_retry = now_us() + 10e6;   // not again right away
    spill_close();                     // nothing went there after all
    printf("yield: %zu MiB of VRAM given back in %.2f s (%zu MiB of data moved to the spill file)%s%s; %lld MiB"
           " free now\n", given >> 20, (now_us() - t0) / 1e6, copied * (CHUNK >> 20), why ? " - stopped: " : "",
           why ? why : "", fr >> 20);
}

static void gpu_regrow(void) {
    long long keep = gpu_free0 > (32ll << 20) ? gpu_free0 - (32ll << 20) : 0;   // what was free at our start
    double t0 = 0;
    size_t taken = 0, copied = 0, again = 0;
    const char *why = NULL;
    char err[64];
    int cuerr = 0;
    for (long k; (k = slab_to_take()) >= 0;) {
        long long fr, others;
        int held = slab_held[k];
        if (!held && (gpu_info(&fr, &others) || fr - (long long)slab_len((size_t)k) < keep))
            break;   // no room (yet)
        if (!t0) {
            t0 = now_us();
            printf("regrow: other programs are done with the GPU - taking VRAM back\n");
        }
        standby_on("data moves back from the spill file");   // on already (the file is open)
        int r = slab_in((size_t)k, M_NONE, -1, &copied, &again, &cuerr);
        if (!held && slab_held[k])
            taken += slab_len((size_t)k);
        if (r != R_DONE) {
            why = r_text(r);
            if (cuerr) {
                snprintf(err, sizeof err, "cannot get VRAM (CUDA error %d)", cuerr);
                why = err;
            }
            gpu_retry = now_us() + 30e6;
            break;
        }
    }
    if (!t0)
        return;
    int closed = spill_close();
    long long fr = 0, others;
    gpu_info(&fr, &others);
    printf("regrow: %zu MiB of VRAM taken back in %.1f s (%zu MiB of data moved back)%s%s%s; %lld MiB free now\n",
           taken >> 20, (now_us() - t0) / 1e6, copied * (CHUNK >> 20), why ? " - stopped: " : "", why ? why : "",
           closed ? "; all data in VRAM again, spill file deleted" : "", fr >> 20);
}

static void gpu_tick(void) {
    long long fr, others;
    if (gpu_info(&fr, &others))
        return;
    if (gpu_others0 < 0) {   // the first look: our start
        gpu_others0 = others;
        gpu_free0 = fr;
    } else if (others < gpu_others0) {   // a program that ran at our start has ended
        gpu_others0 = others;
    }
    double now = now_us();
    int busy = gpu_busy(others);
    if (busy)
        gpu_quiet_since = 0;
    else if (!gpu_quiet_since)
        gpu_quiet_since = now;
    if (now < gpu_retry)
        return;
    if (busy && fr < gpu_keep && __atomic_load_n(&slabs_held, __ATOMIC_RELAXED) && sb_state == SB_NONE) {
        static int told;
        if (!told++)
            printf("yield: other programs need VRAM, but without a standby swap (STANDBY_MB=0) none can be given back\n");
    } else if (busy && fr < gpu_keep && __atomic_load_n(&slabs_held, __ATOMIC_RELAXED)) {
        gpu_yield(fr, others);
    }
    else if (!busy && !user_off && spill_fd >= 0 && now - gpu_quiet_since >= GPU_QUIET_S * 1e6)
        gpu_regrow();
}

// ---- spill / fill on request ----
static void spill(void) {
    double t0 = now_us();
    user_off = 1;   // nothing is taken back by itself now (only by a fill)
    if (!__atomic_load_n(&slabs_held, __ATOMIC_RELAXED)) {
        printf("spill: nothing in VRAM\n");
        return;
    }
    if (!standby_on("data moves to the spill file")) {
        printf("spill failed: no standby swap to take new pages meanwhile (see STANDBY_MB) - the data stays in VRAM\n");
        return;
    }
    const char *e = spill_open();
    if (e) {
        printf("spill failed: %s - the data stays in VRAM\n", e);
        return;
    }
    standby_on("data moves to the spill file");   // stays on now that the file is open (the controller sees it)
    if (spill_reserve(0, nr_chunks)) {
        printf("spill failed: %s: cannot reserve %zu MiB: %s - the data stays in VRAM\n", spill_path,
               count_state(C_VRAM, 1) * (CHUNK >> 20), strerror(errno));
        spill_close();
        return;
    }
    printf("spill: copying %zu MiB from VRAM to %s (%s)\n", count_state(C_VRAM, 1) * (CHUNK >> 20), spill_path,
           spill_how[spill_mode]);
    size_t copied = 0, again = 0;
    int r = R_DONE;
    for (size_t k = nr_slabs; k-- > 0 && r == R_DONE;)
        if (slab_held[k])
            r = slab_out(k, M_SPILL, &copied, &again);
    if (__atomic_load_n(&slabs_held, __ATOMIC_RELAXED)) {
        printf("spill %s after %.1f s: %zu MiB still in VRAM, %zu MiB in %s - the device keeps working from both\n",
               r == R_FAILED ? "failed (copy error)" : r == R_BUSY ? "stopped (busy chunk, standby swap full)" : "stopped",
               (now_us() - t0) / 1e6, count_state(C_VRAM, 1) * (CHUNK >> 20), count_state(C_FILE, 1) * (CHUNK >> 20),
               spill_path);
        return;
    }
    printf("spilled: VRAM released - /dev/ublkb%u now keeps its data in %s (%zu MiB copied in %.1f s,"
           " %zu chunk copies repeated)\n",
           dev_id, spill_path, copied * (CHUNK >> 20), (now_us() - t0) / 1e6, again);
}

static void fill(void) {
    double t0 = now_us();
    user_off = 0;
    if (!count_state(C_FILE, 0)) {
        printf("fill: nothing in the spill file\n");
        return;
    }
    standby_on("data moves back from the spill file");   // should be on already (since the spill)
    printf("fill: copying %zu MiB from %s back to VRAM\n", count_state(C_FILE, 1) * (CHUNK >> 20), spill_path);
    // While other programs use the GPU, they keep what would only be given back to them again.
    long long fr, others, keep = gpu_keep && !gpu_info(&fr, &others) && gpu_busy(others) ? gpu_keep : -1;
    size_t copied = 0, again = 0, got = 0;
    int r = R_DONE, cuerr = 0;
    for (size_t k = 0; k < nr_slabs && r == R_DONE; k++) {
        if (slab_held[k] && !slab_count(k, C_FILE))
            continue;
        int held = slab_held[k];
        r = slab_in(k, M_FILL, keep, &copied, &again, &cuerr);
        got += !held && slab_held[k];
    }
    if (count_state(C_FILE, 0)) {
        char vr[160] = "";
        if (cuerr)
            snprintf(vr, sizeof vr, "cannot get VRAM (CUDA error %d)", cuerr);
        else
            snprintf(vr, sizeof vr, "other programs use the GPU (%lld MiB kept free for them)", gpu_keep >> 20);
        const char *later = gpu_keep ? "; the rest comes back by itself once they are done" : "";
        if (r == R_NO_VRAM && !got && !copied)
            printf("fill failed: %s - the data stays in %s%s\n", vr, spill_path, later);
        else if (r == R_NO_VRAM)
            printf("fill stopped after %.1f s (no room in the VRAM: %s): %zu MiB still in %s - the device keeps"
                   " working from both%s\n", (now_us() - t0) / 1e6, vr, count_state(C_FILE, 1) * (CHUNK >> 20),
                   spill_path, later);
        else
            printf("fill %s after %.1f s: %zu MiB still in %s - the device keeps working from both\n",
                   r == R_FAILED ? "failed (copy error)" : r == R_BUSY ? "stopped (busy chunk, standby swap full)"
                                                         : "stopped",
                   (now_us() - t0) / 1e6, count_state(C_FILE, 1) * (CHUNK >> 20), spill_path);
        return;
    }
    spill_close();
    printf("filled: all data of /dev/ublkb%u is back in VRAM (%zu MiB copied in %.1f s, %zu chunk copies"
           " repeated); %s deleted\n",
           dev_id, copied * (CHUNK >> 20), (now_us() - t0) / 1e6, again, spill_path);
}

static void *mover_main(void *arg) {
    (void)arg;
    int ok = (fake_mem || (!cuCtxSetCurrent(ctx) && !cuStreamCreate(&move_stream, STREAM_NON_BLOCKING))) &&
             !stage_copy(0, 0) && !stage_copy(1, 0);   // warm-up, as for the queues
    prefault_stack();
    if (ok && gpu_keep)
        gpu_tick();   // what other programs use now: the baseline
    pthread_mutex_lock(&ready_lock);
    mover_state = ok ? 1 : -1;
    pthread_cond_broadcast(&ready_cond);
    pthread_mutex_unlock(&ready_lock);
    if (!ok) {
        fprintf(stderr, "mover: setup failed\n");
        return NULL;
    }
    double next_tick = 0;
    for (;;) {
        pthread_mutex_lock(&move_mx);
        if (move_cmd == M_NONE && !gc_wanted) {
            if (gpu_keep) {   // look at the GPU again in a moment
                struct timespec t;
                timeout_in(&t, GPU_TICK_MS);
                pthread_cond_timedwait(&move_cv, &move_mx, &t);
            } else {
                while (move_cmd == M_NONE && !gc_wanted)
                    pthread_cond_wait(&move_cv, &move_mx);
            }
        }
        int cmd = move_cmd;
        move_cmd = M_NONE;
        pthread_mutex_unlock(&move_mx);
        if (cmd == M_QUIT)
            return NULL;
        if (cmd == M_SPILL)
            spill();
        else if (cmd == M_FILL)
            fill();
        if (__atomic_load_n(&gc_wanted, __ATOMIC_RELAXED))
            gc();
        if (gpu_keep && now_us() >= next_tick) {
            next_tick = now_us() + GPU_TICK_MS * 1000.0 * 0.8;
            gpu_tick();
        }
    }
}

// key=value line for usm-vramd-ctl status (main thread only; numbers read without locks, approximate).
static void write_stats(int fd, struct queue *queues) {
    unsigned long long reads = 0, writes = 0, errors = 0, live = 0;
    for (int i = 0; i < nr_queues; i++) {
        reads += queues[i].reads;
        writes += queues[i].writes;
        errors += queues[i].errors;
    }
    size_t in_vram = 0, on_disk = 0;   // MiB of segments / chunks in use, by place
    for (size_t c = 0; c < nr_chunks; c++) {
        int used = cz_ratio ? __atomic_load_n(&seg_open[c], __ATOMIC_RELAXED) != S_FREE : written(c);
        if (!used)
            continue;
        if (cz_ratio)
            live += __atomic_load_n(&seg_live[c], __ATOMIC_RELAXED);
        if (chunk_state[c] == C_VRAM || chunk_state[c] == C_TO_FILE)
            in_vram++;
        else
            on_disk++;
    }
    unsigned long long pages = st_zero + st_raw + st_comp;
    long long held = __atomic_load_n(&held_b, __ATOMIC_RELAXED), gfree = __atomic_load_n(&gpu_free_now, __ATOMIC_RELAXED);
    long long gothers = __atomic_load_n(&gpu_others_now, __ATOMIC_RELAXED);
    gothers = gfree < 0 || gpu_others0 < 0 ? -1 : gothers > gpu_others0 ? (gothers - gpu_others0) >> 20 : 0;
    char buf[768];
    int n = snprintf(buf, sizeof buf,
                     "device_mb=%zu vram_mb=%zu vram_held=%d compress=%.2f in_vram_mb=%zu on_disk_mb=%zu live_mb=%llu"
                     " pages=%llu zero=%llu raw=%llu comp=%llu comp_pct=%.0f refused=%llu reads=%llu writes=%llu"
                     " errors=%llu standby=%s standby_files=%d standby_room=%d switched_off=%d given_back_mb=%lld"
                     " gpu_keep_mb=%lld gpu_free_mb=%lld gpu_others_mb=%lld\n",
                     dev_bytes >> 20, vram_bytes >> 20, held > 0, cz_ratio, in_vram * (CHUNK >> 20),
                     on_disk * (CHUNK >> 20), live >> 20, pages, st_zero, st_raw, st_comp,
                     st_comp ? 100.0 * st_comp_bytes / (st_comp * (double)PG) : 0.0, st_full, reads, writes, errors,
                     sb_names[__atomic_load_n(&sb_state, __ATOMIC_RELAXED)], sb_n, __atomic_load_n(&sb_room_ok, __ATOMIC_RELAXED),
                     user_off, ((long long)vram_bytes - held) >> 20, gpu_keep >> 20, gfree < 0 ? -1 : gfree >> 20, gothers);
    if (n > 0 && pwrite(fd, buf, (size_t)n, 0) == n && ftruncate(fd, n))
        n = 0;
}

static void tell_mover(int cmd) {
    pthread_mutex_lock(&move_mx);
    move_cmd = cmd;
    pthread_cond_signal(&move_cv);
    pthread_mutex_unlock(&move_mx);
}

// `usm-vramd probe`: can this machine run the tier? One line of key=value for `usm check` and the installer.
// Touches nothing: no ublk device, no VRAM allocated (it only asks the driver how much is free). While the tier
// runs, not even a CUDA context: the daemon would take it for another program using the GPU and give VRAM back.
static int probe(void) {
    printf("lz4=%s", dlopen("liblz4.so.1", RTLD_NOW) ? "yes" : "no");
    void *lib = dlopen("libcuda.so.1", RTLD_NOW);
    if (!lib) {
        printf(" cuda=no error=\"no NVIDIA driver (libcuda.so.1 not found)\"\n");
        return 1;
    }
    CUresult (*init)(unsigned) = dlsym(lib, "cuInit");
    CUresult (*version)(int *) = dlsym(lib, "cuDriverGetVersion");
    CUresult (*count)(int *) = dlsym(lib, "cuDeviceGetCount");
    CUresult (*get)(int *, int) = dlsym(lib, "cuDeviceGet");
    CUresult (*name)(char *, int, int) = dlsym(lib, "cuDeviceGetName");
    CUresult (*total)(size_t *, int) = dlsym(lib, "cuDeviceTotalMem_v2");
    CUresult (*retain)(CUcontext *, int) = dlsym(lib, "cuDevicePrimaryCtxRetain");
    CUresult (*release)(int) = dlsym(lib, "cuDevicePrimaryCtxRelease_v2");
    CUresult (*set_current)(CUcontext) = dlsym(lib, "cuCtxSetCurrent");
    CUresult (*mem_info)(size_t *, size_t *) = dlsym(lib, "cuMemGetInfo_v2");
    if (!init || !version || !count || !get || !name || !total || !retain || !release || !set_current || !mem_info) {
        printf(" cuda=no error=\"NVIDIA driver too old (CUDA driver API incomplete)\"\n");
        return 1;
    }
    int r = init(0), v = 0, n = 0, dev = 0;
    if (r) {
        printf(" cuda=no error=\"cuInit failed: CUDA error %d (driver not loaded, or no NVIDIA GPU)\"\n", r);
        return 1;
    }
    version(&v);
    if (count(&n) || n < 1 || get(&dev, 0)) {
        printf(" cuda=no error=\"no CUDA GPU found\"\n");
        return 1;
    }
    char nm[256] = "";
    size_t tot = 0, fr = 0, tt = 0;
    name(nm, sizeof nm, dev);
    for (char *c = nm; *c; c++)
        if (*c == '"')
            *c = '\'';
    total(&tot, dev);
    char st[1024];
    struct stat sst;
    const char *gf = NULL;   // the running daemon's last look at the GPU (its status line, rewritten every 5 s)
    if (!stat("/run/usm-vramd.stats", &sst) && time(NULL) - sst.st_mtime < 15 &&
        !read_proc("/run/usm-vramd.stats", st, sizeof st))
        gf = strstr(st, " gpu_free_mb=");
    CUcontext c;
    if (gf && strtoll(gf + 13, NULL, 10) >= 0)
        fr = (size_t)strtoll(gf + 13, NULL, 10) << 20;
    else if (!retain(&c, dev)) {
        if (!set_current(c))
            mem_info(&fr, &tt);
        set_current(NULL);
        release(dev);
    }
    printf(" cuda=yes cuda_api=%d.%d gpus=%d name=\"%s\" total_mb=%zu free_mb=%zu\n", v / 1000, (v % 1000) / 10, n, nm,
           tot >> 20, fr >> 20);
    return 0;
}

int main(int argc, char **argv) {
    setvbuf(stdout, NULL, _IOLBF, 0);
    // Stop / spill / fill requests are taken by the main thread (sigwait). Block them in every thread,
    // including the ones CUDA starts, before anything else.
    sigemptyset(&ctl_signals);
    sigaddset(&ctl_signals, SIGINT);
    sigaddset(&ctl_signals, SIGTERM);
    sigaddset(&ctl_signals, SIGHUP);
    sigaddset(&ctl_signals, SIGUSR1);   // spill
    sigaddset(&ctl_signals, SIGUSR2);   // fill
    pthread_sigmask(SIG_BLOCK, &ctl_signals, NULL);

    if (argc > 1 && !strcmp(argv[1], "--version")) {
        printf("usm-vramd %s\n", USM_VERSION);
        return 0;
    }
    if (argc > 1 && !strcmp(argv[1], "probe"))
        return probe();
    if (argc > 2 && !strcmp(argv[1], "del")) {   // clean up a leftover device
        unsigned id = (unsigned)strtoul(argv[2], NULL, 10);
        if (ctrl_open())
            return 1;
        safe_stop = !!(query_features() & UBLK_F_SAFE_STOP_DEV);
        if (stop_dev(id) == -EBUSY) {
            fprintf(stderr, "/dev/ublkb%u is in use - swapoff it first\n", id);
            return 1;
        }
        int ret = ctrl_cmd(UBLK_U_CMD_DEL_DEV, id, NULL, 0, 0);
        printf("delete /dev/ublkb%u: %s\n", id, ret ? strerror(-ret) : "done");
        return ret ? 1 : 0;
    }
    vram_bytes = (size_t)(argc > 1 ? strtoul(argv[1], NULL, 10) : 4096) << 20;   // whole MiB = whole chunks
    nr_queues = argc > 2 ? atoi(argv[2]) : 4;
    depth = argc > 3 ? atoi(argv[3]) : 32;
    poll_us = argc > 4 ? atoi(argv[4]) : 0;
    if (!vram_bytes || nr_queues < 1 || nr_queues > 64 || depth < 1 || depth > UBLK_MAX_QUEUE_DEPTH ||
        poll_us < 0) {
        fprintf(stderr, "usage: %s [vram_mb] [queues] [queue_depth] [poll_us]\n       %s del ID | probe | --version\n",
                argv[0], argv[0]);
        return 2;
    }

    // The kernel gives a ublk device at most one queue per CPU: more queues than CPUs would never be set up.
    long ncpu = sysconf(_SC_NPROCESSORS_CONF);
    if (ncpu > 0 && nr_queues > ncpu) {
        printf("%d queues requested, %ld CPUs: using %ld\n", nr_queues, ncpu, ncpu);
        nr_queues = (int)ncpu;
    }

    // Memory once used stays ours (and locked): one malloc arena, never trimmed, small blocks from it.
    mallopt(M_ARENA_MAX, 1);
    mallopt(M_TRIM_THRESHOLD, 1 << 30);
    mallopt(M_MMAP_THRESHOLD, 32 << 20);
    // A swap server must never wait on its own device: with PR_SET_IO_FLUSHER our memory
    // allocations never start I/O to free memory. Set before any thread starts (inherited).
    if (prctl(PR_SET_IO_FLUSHER, 1, 0, 0, 0))
        perror("prctl(PR_SET_IO_FLUSHER)");
    // The OOM killer must never pick us: every swapped-out page would go with us.
    FILE *oom = fopen("/proc/self/oom_score_adj", "w");
    if (!oom || fputs("-1000", oom) < 0 || fclose(oom))
        perror("oom_score_adj");

    spill_path = getenv("USM_VRAMD_SPILL");
    if (spill_path && !*spill_path)
        spill_path = NULL;
    const char *sb = getenv("USM_VRAMD_STANDBY"), *sbp = getenv("USM_VRAMD_STANDBY_PRIO");
    if (sb && *sb) {
        sb_prio = sbp ? atoi(sbp) : 0;
        struct stat sbst;
        if (sb_prio < 1 || sb_prio > 32767 || !realpath(sb, sb_paths[0]) || stat(sb_paths[0], &sbst) ||
            sbst.st_size < (64 << 20)) {
            fprintf(stderr, "standby swap %s (priority %s) not usable - without it, writes the VRAM has no room for"
                            " are refused, and no spill\n", sb, sbp ? sbp : "not set");
        } else {
            sb_state = SB_OFF;
            sb_step = (long long)sbst.st_size;
            struct sigaction sa = {.sa_handler = on_sigurg};   // no SA_RESTART: interrupts its swapoff
            sigaction(SIGURG, &sa, NULL);
        }
    }
    const char *cz = getenv("USM_VRAMD_COMPRESS");   // compressed mode: device = RATIO x VRAM
    if (cz && *cz) {
        cz_ratio = strtod(cz, NULL);
        if (cz_ratio < 1 || cz_ratio > 8) {
            fprintf(stderr, "USM_VRAMD_COMPRESS: a ratio from 1 to 8\n");
            return 2;
        }
        void *lz = dlopen("liblz4.so.1", RTLD_NOW);
        if (!lz || !(LZ4_compress_default = dlsym(lz, "LZ4_compress_default")) ||
            !(LZ4_decompress_safe = dlsym(lz, "LZ4_decompress_safe"))) {
            fprintf(stderr, "compressed mode needs liblz4.so.1\n");
            return 1;
        }
    }
    const char *gf = getenv("USM_VRAMD_GPU_FREE_MB");   // giving VRAM back to other programs
    if (gf && *gf) {
        char *end;
        long long mb = strtoll(gf, &end, 10);
        if (*end || mb < 0 || mb > (1ll << 24)) {
            fprintf(stderr, "USM_VRAMD_GPU_FREE_MB: MiB to keep free for other programs (0 = never give VRAM back)\n");
            return 2;
        }
        gpu_keep = mb << 20;
    }
    gpu_fake = getenv("USM_VRAMD_FAKE_GPU");
    if (gpu_fake && !*gpu_fake)
        gpu_fake = NULL;
    const char *fake = getenv("USM_VRAMD_FAKE");
    if (fake && *fake && !gpu_fake)   // test mode without a fake GPU: no GPU to watch
        gpu_keep = 0;
    if (fake && *fake) {   // test mode: map the device memory, no CUDA
        int fd = open(fake, O_RDWR);
        struct stat st;
        if (fd < 0 || fstat(fd, &st) || st.st_size <= 0) {
            perror(fake);
            return 1;
        }
        if ((size_t)st.st_size < vram_bytes)
            vram_bytes = (size_t)st.st_size & ~(size_t)(CHUNK - 1);
        fake_mem = mmap(NULL, vram_bytes, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
        if (fake_mem == MAP_FAILED) {
            perror("mmap test device memory");
            return 1;
        }
    }

    // ---- CUDA: borrow VRAM, pinned I/O buffers ----
    void *lib = fake_mem ? NULL : dlopen("libcuda.so.1", RTLD_NOW);
    if (fake_mem)
        goto buffers;
    if (!lib) {
        fprintf(stderr, "cannot load libcuda.so.1: %s\n", dlerror());
        return 1;
    }
    cuInit = cuda_sym(lib, "cuInit");
    cuDeviceGet = cuda_sym(lib, "cuDeviceGet");
    cuDevicePrimaryCtxRetain = cuda_sym(lib, "cuDevicePrimaryCtxRetain");
    cuCtxSetCurrent = cuda_sym(lib, "cuCtxSetCurrent");
    cuMemAlloc = cuda_sym(lib, "cuMemAlloc_v2");
    cuMemFree = cuda_sym(lib, "cuMemFree_v2");
    cuMemGetInfo = cuda_sym(lib, "cuMemGetInfo_v2");
    cuMemHostRegister = cuda_sym(lib, "cuMemHostRegister_v2");
    cuMemcpyHtoDAsync = cuda_sym(lib, "cuMemcpyHtoDAsync_v2");
    cuMemcpyDtoHAsync = cuda_sym(lib, "cuMemcpyDtoHAsync_v2");
    cuStreamCreate = cuda_sym(lib, "cuStreamCreate");
    cuStreamSynchronize = cuda_sym(lib, "cuStreamSynchronize");
    int cudev;
    cuda_check(cuInit(0), "cuInit");
    cuda_check(cuDeviceGet(&cudev, 0), "cuDeviceGet");
    cuda_check(cuDevicePrimaryCtxRetain(&ctx, cudev), "cuDevicePrimaryCtxRetain");
    cuda_check(cuCtxSetCurrent(ctx), "cuCtxSetCurrent");
buffers:;
    nr_slabs = (vram_bytes + SLAB - 1) / SLAB;
    slab_ptr = calloc(nr_slabs, sizeof *slab_ptr);
    slab_held = calloc(nr_slabs, 1);
    slab_leaving = calloc(nr_slabs, 1);
    if (!slab_ptr || !slab_held || !slab_leaving) {
        fprintf(stderr, "out of memory\n");
        return 1;
    }
    for (size_t k = 0; k < nr_slabs; k++) {   // the VRAM, in pieces that can be given back one by one
        if (!fake_mem)
            cuda_check(cuMemAlloc(&slab_ptr[k], slab_len(k)), "cuMemAlloc (VRAM)");
        slab_held[k] = 1;
    }
    slabs_held = nr_slabs;
    held_b = (long long)vram_bytes;
    dev_bytes = cz_ratio ? (size_t)((double)vram_bytes * cz_ratio) & ~(size_t)(CHUNK - 1) : vram_bytes;
    // Per queue: I/O buffers; compressed mode also: read scratch, open segment, 2 pages for file reads.
    size_t io_part = (size_t)depth * IO_BUF;
    size_t per_queue = io_part + (cz_ratio ? io_part + CHUNK + 2 * PG : 0);
    size_t buf_bytes = per_queue * nr_queues + CHUNK + (cz_ratio ? CHUNK : 0);   // + mover's stage, compactor's segment
    char *bufs = mmap(NULL, buf_bytes, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS | MAP_POPULATE, -1, 0);
    if (bufs == MAP_FAILED) {
        perror("mmap (I/O buffers)");
        return 1;
    }
    stage = bufs + per_queue * nr_queues;
    gdst.buf = cz_ratio ? stage + CHUNK : NULL;
    nr_chunks = vram_bytes / CHUNK;
    chunk_state = calloc(nr_chunks, 1);
    chunk_dirty = calloc(nr_chunks, 1);
    chunk_written = calloc(nr_chunks, 1);
    if (!chunk_state || !chunk_dirty || !chunk_written) {
        fprintf(stderr, "out of memory\n");
        return 1;
    }
    if (cz_ratio) {   // the page map is touched now (MAP_POPULATE) and locked below with everything else
        size_t pages = dev_bytes / PG;
        pmap = mmap(NULL, pages * sizeof *pmap, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS | MAP_POPULATE,
                    -1, 0);
        seg_live = calloc(nr_chunks, sizeof *seg_live);
        seg_open = calloc(nr_chunks, sizeof *seg_open);
        free_segs = calloc(nr_chunks, sizeof *free_segs);
        ffree_segs = calloc(nr_chunks, sizeof *ffree_segs);
        seg_tmp = calloc(nr_chunks, sizeof *seg_tmp);
        gdst.moves = calloc(CHUNK / 16, sizeof(struct gc_move));
        if (pmap == MAP_FAILED || !seg_live || !seg_open || !free_segs || !ffree_segs || !seg_tmp || !gdst.moves) {
            fprintf(stderr, "out of memory (page map)\n");
            return 1;
        }
        for (size_t c = 0; c < nr_chunks; c++) {
            seg_open[c] = S_FREE;
            free_segs[c] = (uint32_t)(nr_chunks - 1 - c);   // segment 0 is used first
        }
        free_n = nr_chunks;
        // Free segments: below sb_on_free (1/16 of the VRAM, at least 64 MiB) the standby swap goes on;
        // the compactor starts at twice that (so dead space is reclaimed first) and stops at three
        // times; at four times the standby may go off again. Never more than half the VRAM.
        size_t half = nr_chunks / 2 > SEG_RESERVE + 2 ? nr_chunks / 2 : SEG_RESERVE + 2;
#define AT_MOST_HALF(x) ((x) < half ? (x) : half)
        sb_on_free = AT_MOST_HALF(nr_chunks / 16 > 64 ? nr_chunks / 16 : 64);
        if (sb_on_free > nr_chunks / 4 && nr_chunks / 4 > SEG_RESERVE + 2)
            sb_on_free = nr_chunks / 4;
        gc_low = AT_MOST_HALF(2 * sb_on_free);
        gc_high = AT_MOST_HALF(3 * sb_on_free);
        sb_off_free = AT_MOST_HALF(4 * sb_on_free);
#undef AT_MOST_HALF
    }
    pthread_rwlockattr_t rwa;   // the mover's short steps must not wait behind a stream of batches
    pthread_rwlockattr_init(&rwa);
    pthread_rwlockattr_setkind_np(&rwa, PTHREAD_RWLOCK_PREFER_WRITER_NONRECURSIVE_NP);
    pthread_rwlock_init(&move_lock, &rwa);
    // Pinned, so the copy engines can DMA straight in and out. Ordinary memory, so the kernel's
    // ublk copy can reach it as well.
    if (!fake_mem && cuMemHostRegister(bufs, buf_bytes, 0))
        fprintf(stderr, "warning: cuMemHostRegister failed - copies will be staged (slower)\n");
    // Lock all our memory (as it gets used) so the swap server itself is never swapped out.
    if (mlockall(MCL_CURRENT | MCL_FUTURE | MCL_ONFAULT))
        perror("mlockall");

    // ---- ublk: create the device ----
    if (ctrl_open())
        return 1;
    __u64 features = query_features();
    safe_stop = !!(features & UBLK_F_SAFE_STOP_DEV);
    struct ublksrv_ctrl_dev_info info = {
        .nr_hw_queues = nr_queues, .queue_depth = depth, .max_io_buf_bytes = IO_BUF, .dev_id = (__u32)-1,
        .flags = UBLK_F_CMD_IOCTL_ENCODE | (features & (UBLK_F_SAFE_STOP_DEV | UBLK_F_NO_AUTO_PART_SCAN)),
    };
    int ret = ctrl_cmd(UBLK_U_CMD_ADD_DEV, (__u32)-1, &info, sizeof(info), 0);
    if (ret) {
        fprintf(stderr, "ADD_DEV failed: %s\n", strerror(-ret));
        return 1;
    }
    dev_id = info.dev_id;
    if ((int)info.nr_hw_queues < nr_queues) {   // the kernel may give fewer queues than asked for
        printf("the kernel gave %u queues (asked for %d)\n", info.nr_hw_queues, nr_queues);
        nr_queues = (int)info.nr_hw_queues;
    }

    struct queue *queues = calloc(nr_queues, sizeof(*queues));
    all_queues = queues;
    int started = 0, mover_started = 0, sb_started = 0;
    pthread_t mover, sb_thread;
    struct ublk_params p = {
        .len = sizeof(p),
        .types = UBLK_PARAM_TYPE_BASIC,
        .basic = {.logical_bs_shift = 12, .physical_bs_shift = 12, .io_opt_shift = 12, .io_min_shift = 12,
                  .max_sectors = IO_BUF >> 9, .dev_sectors = dev_bytes >> 9},
    };
    if (cz_ratio) {   // swap tells us which pages it gave up (swapon --discard): their space is reused
        p.types |= UBLK_PARAM_TYPE_DISCARD;
        p.discard = (struct ublk_param_discard){.discard_granularity = PG, .max_discard_sectors = 1u << 20,
                                                .max_discard_segments = 1};
    }
    ret = ctrl_cmd(UBLK_U_CMD_SET_PARAMS, dev_id, &p, sizeof(p), 0);
    if (ret) {
        fprintf(stderr, "SET_PARAMS failed: %s\n", strerror(-ret));
        goto out;
    }
    char cpath[64];
    snprintf(cpath, sizeof cpath, "/dev/ublkc%u", dev_id);
    for (int i = 0; i < 100 && cfd < 0; i++) {   // udev creates the node
        cfd = open(cpath, O_RDWR);
        if (cfd < 0)
            usleep(20000);
    }
    if (cfd < 0) {
        perror(cpath);
        goto out;
    }
    quit_fd = eventfd(0, EFD_CLOEXEC);
    if (quit_fd < 0) {
        perror("eventfd");
        goto out;
    }
    for (; started < nr_queues; started++) {
        queues[started].id = started;
        queues[started].bufs = bufs + per_queue * started;
        queues[started].descs = MAP_FAILED;
        queues[started].oseg = -1;
        pthread_mutex_init(&queues[started].olock, NULL);
        if (cz_ratio) {
            queues[started].rscratch = queues[started].bufs + io_part;
            queues[started].obuf = queues[started].rscratch + io_part;
            queues[started].fscratch = queues[started].obuf + CHUNK;
            queues[started].jobs = calloc((size_t)depth * (IO_BUF / PG), sizeof(struct dz_job));
            if (!queues[started].jobs) {
                fprintf(stderr, "out of memory\n");
                break;
            }
        }
        if (pthread_create(&queues[started].thread, NULL, serve_queue, &queues[started])) {
            fprintf(stderr, "pthread_create failed\n");
            break;
        }
    }
    int setup_failed = started < nr_queues;
    mover_started = !pthread_create(&mover, NULL, mover_main, NULL);
    sb_started = sb_state != SB_NONE && !pthread_create(&sb_thread, NULL, sb_main, NULL);
    pthread_mutex_lock(&ready_lock);
    while (queues_ready < started || (mover_started && !mover_state))
        pthread_cond_wait(&ready_cond, &ready_lock);
    pthread_mutex_unlock(&ready_lock);
    for (int i = 0; i < started; i++)
        setup_failed |= queues[i].setup_err;
    setup_failed |= mover_state != 1;

    // CUDA allocates a little more on first real traffic (seen: new heap pages written by libcuda).
    // With CUDA fully set up, grow the heap and give it back to malloc (not to the system): later
    // allocations reuse pages that are already present and locked.
    char *pre = malloc(8 << 20);
    if (pre) {
        memset(pre, 1, 8 << 20);
        free(pre);
    }
    // CUDA also has allocations whose pages it touches only later: make every heap page present
    // (and so locked) now, without changing any contents.
    populate_heap();

    // START_DEV only now: the kernel may read the new disk before it returns, and the queue
    // threads are already waiting to serve those reads.
    ret = setup_failed ? -EIO : ctrl_cmd(UBLK_U_CMD_START_DEV, dev_id, NULL, 0, getpid());
    if (ret) {
        fprintf(stderr, "starting the device failed: %s\n", strerror(-ret));
    } else {
        char czs[96] = "";
        if (cz_ratio)
            snprintf(czs, sizeof czs, ", compressed (lz4) into %zu MiB", vram_bytes >> 20);
        char gys[96] = "never";
        if (gpu_keep)
            snprintf(gys, sizeof gys, "once other programs use the GPU, until %lld MiB are free", gpu_keep >> 20);
        printf("ready: /dev/ublkb%u = %zu MiB of %s%s (%d queues x depth %d, poll %d us; spill file: %s;"
               " standby swap: %s; VRAM given back: %s)\n", dev_id, dev_bytes >> 20,
               fake_mem ? "TEST device memory (RTX 3050 copy times)" : "VRAM", czs, nr_queues, depth, poll_us,
               spill_path ? spill_path : "none", sb_state != SB_NONE ? sb_paths[0] : "none", gys);
        const char *sp = getenv("USM_VRAMD_STATS");   // a one-line status file, rewritten every 5 s
        int stats_fd = sp && *sp ? open(sp, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0644) : -1;
        for (;;) {
            struct timespec every = {.tv_sec = 5};
            int sig = sigtimedwait(&ctl_signals, NULL, &every);
            if (sig < 0) {
                if (stats_fd >= 0)
                    write_stats(stats_fd, queues);
                continue;
            }
            if (sig == SIGUSR1 || sig == SIGUSR2) {
                tell_mover(sig == SIGUSR1 ? M_SPILL : M_FILL);
                continue;
            }
            ret = stop_dev(dev_id);
            if (ret != -EBUSY)
                break;
            fprintf(stderr, "usm-vramd: /dev/ublkb%u is still in use - swapoff it first. Not stopping.\n",
                    dev_id);
        }
        if (ret)
            fprintf(stderr, "stopping /dev/ublkb%u: %s\n", dev_id, strerror(-ret));
    }
out:
    if (sb_started) {
        pthread_mutex_lock(&sb_wake_mx);
        sb_quit = 1;
        pthread_cond_signal(&sb_wake_cv);
        pthread_mutex_unlock(&sb_wake_mx);
        pthread_join(sb_thread, NULL);
    }
    if (mover_started && mover_state == 1) {
        tell_mover(M_QUIT);
        pthread_join(mover, NULL);
    }
    if (quit_fd >= 0) {
        uint64_t one = 1;
        if (write(quit_fd, &one, sizeof one) != sizeof one)
            perror("eventfd write");
    }
    unsigned long reads = 0, writes = 0, batches = 0, errors = 0, file_reads = 0, file_writes = 0;
    unsigned long long bytes_r = 0, bytes_w = 0;
    double busy_us = 0;
    char per_q[512] = "";
    long minflt = 0, majflt = 0;
    for (int i = 0; i < started; i++) {
        struct queue *q = &queues[i];
        pthread_join(q->thread, NULL);
        reads += q->reads;
        writes += q->writes;
        batches += q->batches;
        errors += q->errors;
        file_reads += q->file_reads;
        file_writes += q->file_writes;
        bytes_r += q->bytes_r;
        bytes_w += q->bytes_w;
        busy_us += q->busy_us;
        minflt += q->minflt;
        majflt += q->majflt;
        size_t l = strlen(per_q);
        snprintf(per_q + l, sizeof per_q - l, "%s%lu", i ? "/" : "", q->reads + q->writes);
    }
    if (started)
        printf("stopped: %lu reads (%llu MiB), %lu writes (%llu MiB), %lu errors; %.2f requests per batch,"
               " %.2f us of daemon work per request; requests per queue %s;"
               " page faults while serving: %ld minor, %ld major; spill file parts: %lu reads, %lu writes\n",
               reads, bytes_r >> 20, writes, bytes_w >> 20, errors,
               batches ? (double)(reads + writes) / batches : 0.0,
               (reads + writes) ? busy_us / (reads + writes) : 0.0, per_q, minflt, majflt, file_reads, file_writes);
    if (cz_ratio) {
        unsigned long long live = 0;
        size_t used = 0;
        for (size_t c = 0; c < nr_chunks; c++) {
            live += seg_live[c];
            used += seg_open[c] != S_FREE;
        }
        unsigned long long stored = st_zero + st_raw + st_comp;
        printf("compressed: %llu pages written (%llu all-zero, %llu kept raw, %llu compressed to %.0f%% on average);"
               " now %llu MiB live in %zu of %zu MiB of segments; %llu pages discarded; compactor: %llu rounds,"
               " %llu MiB moved\n",
               stored, st_zero, st_raw, st_comp, st_comp ? 100.0 * st_comp_bytes / (st_comp * (double)PG) : 0.0,
               live >> 20, used * (CHUNK >> 20), nr_chunks * (CHUNK >> 20), st_discard, st_gc_runs,
               st_gc_bytes >> 20);
    }
    if (st_full)
        printf("writes refused: %llu (no room in the VRAM, or the disk was not safe to use then)\n", st_full);
    if (sb_state == SB_ON)
        printf("standby swap still on: %s%s (usm-vramd-ctl switches it off once its pages fit in RAM)\n",
               sb_paths[0], sb_n > 1 ? " and the files added" : "");
    ctrl_cmd(UBLK_U_CMD_STOP_DEV, dev_id, NULL, 0, 0);
    // The device goes away once nothing holds its character device open any more.
    for (int i = 0; i < started; i++) {
        if (queues[i].ring_ok)
            io_uring_queue_exit(&queues[i].ring);
        if (queues[i].descs != MAP_FAILED)
            munmap((void *)queues[i].descs, queues[i].desc_len);
    }
    if (cfd >= 0)
        close(cfd);
    ret = ctrl_cmd(UBLK_U_CMD_DEL_DEV_ASYNC, dev_id, NULL, 0, 0);
    if (ret)
        ret = ctrl_cmd(UBLK_U_CMD_DEL_DEV, dev_id, NULL, 0, 0);
    if (ret)
        fprintf(stderr, "DEL_DEV: %s\n", strerror(-ret));
    if (spill_fd >= 0) {   // the device is gone: its data is no longer needed
        close(spill_fd);
        unlink(spill_path);
    }
    return 0;
}
