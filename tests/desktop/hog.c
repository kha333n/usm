// hog MB [mixed]: allocate MB of incompressible data (each 4 KiB page derived from its index), then wait.
// mixed: only the first third of each page is random, the rest a repeated pattern (~3:1, like app memory).
// SIGUSR1: verify every page and print "verify: N pages, M bad". SIGTERM: exit. For USM tests.
// SIGUSR2: touch random pages for TOUCH_S seconds (env, default 40): 70% in a hot 30% of the memory, 1 in 4
// a write (same value: content stays checkable); prints "touch: N touches in S s".
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <time.h>
static double now(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec + t.tv_nsec / 1e9; }
static volatile sig_atomic_t check, stop, touch;
static void on_usr2(int s) { (void)s; touch = 1; }
static int mixed;
static void on_usr1(int s) { (void)s; check = 1; }
static void on_term(int s) { (void)s; stop = 1; }
static void fill(uint64_t *p, uint64_t idx) {
    uint64_t x = idx * 0x9E3779B97F4A7C15ull + 1;
    for (int i = 0; i < 512; i++) {
        if (mixed && i >= 170) { p[i] = 0x0123456789abcdefull + (uint64_t)(i & 7); continue; }
        x ^= x << 13; x ^= x >> 7; x ^= x << 17; p[i] = x;
    }
}
int main(int argc, char **argv) {
    size_t mb = argc > 1 ? strtoul(argv[1], 0, 10) : 256, pages = mb << 8;
    mixed = argc > 2 && argv[2][0] == 'm';
    uint64_t *mem = malloc(pages * 4096), tmp[512];
    if (!mem) { perror("malloc"); return 1; }
    for (size_t i = 0; i < pages; i++) fill(mem + i * 512, i);
    signal(SIGUSR1, on_usr1); signal(SIGUSR2, on_usr2); signal(SIGTERM, on_term); signal(SIGINT, on_term);
    printf("hog %d: %zu MiB filled\n", getpid(), mb); fflush(stdout);
    while (!stop) {
        pause();
        if (touch) {
            touch = 0;
            double secs = getenv("TOUCH_S") ? atof(getenv("TOUCH_S")) : 40, t0 = now(), t;
            uint64_t x = 88172645463325252ull ^ (uint64_t)getpid(), n = 0, hot = pages * 3 / 10, sum = 0;
            do {
                for (int k = 0; k < 256; k++) {
                    x ^= x << 13; x ^= x >> 7; x ^= x << 17;
                    size_t pg = (x % 10 < 7) ? (x >> 8) % hot : (x >> 8) % pages;
                    volatile uint64_t *w = mem + pg * 512 + (x & 511);
                    uint64_t v = *w; sum += v;
                    if ((x & 3) == 0) *w = v;
                    n++;
                }
            } while ((t = now()) - t0 < secs);
            printf("touch: %llu touches in %.1f s (%.0f/s) %llu\n", (unsigned long long)n, t - t0, n / (t - t0),
                   (unsigned long long)(sum & 1)); fflush(stdout);
        }
        if (check) {
            check = 0;
            size_t bad = 0;
            for (size_t i = 0; i < pages; i++) {
                fill(tmp, i);
                for (int j = 0; j < 512; j++) if (mem[i * 512 + j] != tmp[j]) { bad++; break; }
            }
            printf("verify: %zu pages, %zu bad\n", pages, bad); fflush(stdout);
        }
    }
    return 0;
}
