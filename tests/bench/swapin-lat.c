// swapin-lat: how long does it take to bring one swapped-out page back? (the real kernel path)
//
// Fills SIZE_MB of memory (pages ~3:1 compressible, each stamped with its index and a checksum),
// pushes all of it out to swap, then touches the pages again in random order and times every
// first touch = one page fault + one swap-in. With THREADS > 1, that many threads fault at the
// same time, each on its own share of the pages. Whichever swap device has the highest priority
// is the one measured. All data is verified.
//
// Pushing out: MADV_PAGEOUT writes the pages to swap. On devices that complete writes later
// (ublk, NVMe; not zram) the written copies stay in RAM as swap cache, so the tool also asks the
// kernel to reclaim its own cgroup (memory.reclaim, needs root) until mincore shows them gone.
// Everything else of the tool is locked in RAM first, so only the test pages are affected.
//
// Build: make -C tests/swapin-lat        Run: swapin-lat [size_mb=256] [threads=1]  (as root)
#define _GNU_SOURCE
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

#ifndef MADV_PAGEOUT
#define MADV_PAGEOUT 21
#endif

#define PAGE 4096
#define WORDS (PAGE / 8)
#define STAMP 0x5D3A9C1E00000000ull

static uint64_t rng = 0x9E3779B97F4A7C15ull;
static uint64_t xorshift(void) {
    rng ^= rng << 13;
    rng ^= rng >> 7;
    rng ^= rng << 17;
    return rng;
}

static double now_us(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec * 1e6 + t.tv_nsec / 1e3;
}

static int cmpd(const void *a, const void *b) {
    double x = *(const double *)a, y = *(const double *)b;
    return (x > y) - (x < y);
}

static uint64_t page_sum(const uint64_t *p) {   // FNV-1a over every word but the last
    uint64_t h = 1469598103934665603ull;
    for (int w = 0; w < WORDS - 1; w++)
        h = (h ^ p[w]) * 1099511628211ull;
    return h;
}

// memory.reclaim of the cgroup we run in ("0::/path" in /proc/self/cgroup), or "" if unknown.
static void own_reclaim_file(char *out, size_t len) {
    char line[4096] = "";
    FILE *f = fopen("/proc/self/cgroup", "r");
    out[0] = 0;
    while (f && fgets(line, sizeof line, f))
        if (!strncmp(line, "0::", 3)) {
            line[strcspn(line, "\n")] = 0;
            snprintf(out, len, "/sys/fs/cgroup%s/memory.reclaim", line + 3);
        }
    if (f)
        fclose(f);
}

static unsigned char *mem;
static size_t npages, *order;
static double *lat;

static size_t resident_pages(unsigned char *vec) {
    if (mincore(mem, npages * PAGE, vec))
        return npages;
    size_t n = 0;
    for (size_t i = 0; i < npages; i++)
        n += vec[i] & 1;
    return n;
}

struct share {
    size_t first, count;
};

static void *fault_in(void *arg) {
    struct share *s = arg;
    for (size_t k = s->first; k < s->first + s->count; k++) {
        const volatile uint64_t *p = (const volatile uint64_t *)(mem + order[k] * PAGE);
        double t0 = now_us();
        (void)p[1];
        lat[k] = now_us() - t0;
    }
    return NULL;
}

int main(int argc, char **argv) {
    size_t mb = argc > 1 ? strtoul(argv[1], NULL, 10) : 256;
    int threads = argc > 2 ? atoi(argv[2]) : 1;
    if (!mb || threads < 1 || threads > 256) {
        fprintf(stderr, "usage: %s [size_mb] [threads]\n", argv[0]);
        return 2;
    }
    setvbuf(stdout, NULL, _IONBF, 0);
    npages = mb * 1024 * 1024 / PAGE;
    // Everything the timed part needs is prepared and locked in RAM now (before the test
    // memory exists), so reclaim only ever takes test pages and the timed loop never waits
    // for its own code or arrays.
    unsigned char *vec = malloc(npages);
    order = malloc(npages * sizeof *order);
    lat = calloc(npages, sizeof *lat);
    struct share *sh = calloc(threads, sizeof *sh);
    pthread_t *tid = calloc(threads, sizeof *tid);
    memset(vec, 0, npages);
    for (size_t i = 0; i < npages; i++)
        order[i] = i;
    for (size_t i = npages - 1; i > 0; i--) {
        size_t j = xorshift() % (i + 1), t = order[i];
        order[i] = order[j];
        order[j] = t;
    }
    for (int t = 0; t < threads; t++) {
        sh[t].first = npages * t / threads;
        sh[t].count = npages * (t + 1) / threads - sh[t].first;
    }
    if (mlockall(MCL_CURRENT))
        perror("mlockall (results may include faults on the tool itself)");
    char reclaim[4200];
    own_reclaim_file(reclaim, sizeof reclaim);

    mem = mmap(NULL, npages * PAGE, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (mem == MAP_FAILED) {
        perror("mmap");
        return 1;
    }
    madvise(mem, npages * PAGE, MADV_NOHUGEPAGE);   // 4 KiB pages, like typical swap traffic
    for (size_t i = 0; i < npages; i++) {
        uint64_t *p = (uint64_t *)(mem + i * PAGE);
        for (int w = 0; w < WORDS; w++)
            p[w] = (w < WORDS / 3) ? xorshift() : 0x0101010101010101ull * (unsigned char)(i | 1);
        p[1] = (uint64_t)i ^ STAMP;
        p[WORDS - 1] = page_sum(p);
    }
    // Push it all out: start the writes, then drop the copies that are written, until gone.
    double t0 = now_us();
    size_t left = npages;
    int reclaim_ok = reclaim[0] != 0, reclaim_used = 0;
    for (int round = 0; round < 400 && left > npages / 1000; round++) {
        if (madvise(mem, npages * PAGE, MADV_PAGEOUT)) {
            perror("madvise(MADV_PAGEOUT)");
            return 1;
        }
        left = resident_pages(vec);
        if (left > npages / 1000 && reclaim_ok) {
            FILE *f = fopen(reclaim, "w");
            if (!f) {
                perror(reclaim);
                reclaim_ok = 0;
            } else {
                fprintf(f, "%zu", left * PAGE);
                fclose(f);   // EAGAIN = could not get all of it this time; fine
                reclaim_used = 1;
                left = resident_pages(vec);
            }
        }
        if (left > npages / 1000)
            usleep(5000);
    }
    double out_s = (now_us() - t0) / 1e6;
    printf("pushed out %zu of %zu MiB to swap in %.2f s (%.0f MiB/s)%s%s\n", (npages - left) * PAGE >> 20, mb,
           out_s, ((npages - left) * PAGE >> 20) / out_s, reclaim_used ? ", with cgroup reclaim" : "",
           left > npages / 100 ? "   WARNING: much of it is still in RAM (no swap? swap full? not root?)" : "");

    double start = now_us();
    for (int t = 0; t < threads; t++)
        pthread_create(&tid[t], NULL, fault_in, &sh[t]);
    for (int t = 0; t < threads; t++)
        pthread_join(tid[t], NULL);
    double total = (now_us() - start) / 1e6;

    size_t bad = 0;
    for (size_t i = 0; i < npages; i++) {
        const uint64_t *p = (const uint64_t *)(mem + i * PAGE);
        if (p[1] != ((uint64_t)i ^ STAMP) || p[WORDS - 1] != page_sum(p))
            bad++;
    }
    qsort(lat, npages, sizeof *lat, cmpd);
    size_t skip = 0;   // pages that were still in RAM (no fault)
    while (skip < npages && lat[skip] < 0.5)
        skip++;
    size_t n = npages - skip;
    double *f = lat + skip;
    if (n)
        printf("swap-in of one page, %d thread(s) faulting at once, %zu faults:\n"
               "    median %.1f us   p90 %.1f us   p99 %.1f us   max %.0f us\n",
               threads, n, f[n / 2], f[n * 9 / 10], f[n * 99 / 100], f[n - 1]);
    printf("all %zu MiB back in %.2f s = %.0f MiB/s (%.0f page faults/s)\n", mb, total, mb / total, n / total);
    printf("data check: %s (%zu pages)\n", bad ? "CORRUPTION DETECTED" : "every page correct", npages);
    if (bad)
        printf("    %zu pages wrong\n", bad);
    return bad ? 3 : 0;
}
