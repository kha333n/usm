// gpu-claim: act like a CUDA app that needs VRAM while the USM tier holds some.
// Allocates VRAM in steps through the CUDA driver API (libcuda loaded at run time, no toolkit), reports each step
// and the free VRAM, holds it for a while, then frees it. Retries a failed step for up to --wait seconds (an app
// that waits for memory), so it shows whether VRAM is given back while it runs.
//   gpu-claim MB [STEP_MB=128] [HOLD_S=5] [WAIT_S=0]
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>
typedef int CUresult;
typedef unsigned long long CUdeviceptr;
static double now(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec + t.tv_nsec / 1e9; }
int main(int argc, char **argv) {
    if (argc < 2) { fprintf(stderr, "usage: %s MB [STEP_MB] [HOLD_S] [WAIT_S]\n", argv[0]); return 2; }
    size_t want = strtoull(argv[1], 0, 10) << 20, step = (argc > 2 ? strtoull(argv[2], 0, 10) : 128) << 20;
    int hold = argc > 3 ? atoi(argv[3]) : 5;
    double wait = argc > 4 ? atof(argv[4]) : 0;
    void *l = dlopen("libcuda.so.1", RTLD_NOW);
    if (!l) { puts("no libcuda.so.1"); return 1; }
    CUresult (*init)(unsigned) = dlsym(l, "cuInit");
    CUresult (*get)(int *, int) = dlsym(l, "cuDeviceGet");
    CUresult (*retain)(void **, int) = dlsym(l, "cuDevicePrimaryCtxRetain");
    CUresult (*setc)(void *) = dlsym(l, "cuCtxSetCurrent");
    CUresult (*alloc)(CUdeviceptr *, size_t) = dlsym(l, "cuMemAlloc_v2");
    CUresult (*mfree)(CUdeviceptr) = dlsym(l, "cuMemFree_v2");
    CUresult (*info)(size_t *, size_t *) = dlsym(l, "cuMemGetInfo_v2");
    CUresult (*memset8)(CUdeviceptr, unsigned char, size_t) = dlsym(l, "cuMemsetD8_v2");
    int dev; void *ctx; size_t fr, tot;
    double t0 = now();
    if (init(0) || get(&dev, 0) || retain(&ctx, dev) || setc(ctx)) { puts("CUDA setup failed"); return 1; }
    info(&fr, &tot);
    printf("context ready in %.0f ms; VRAM free %zu of %zu MiB\n", (now() - t0) * 1e3, fr >> 20, tot >> 20);
    CUdeviceptr *p = calloc(want / step + 1, sizeof *p);
    size_t got = 0; int n = 0, failed = 0;
    while (got < want) {
        size_t s = want - got < step ? want - got : step;
        double t1 = now(); CUresult r;
        while ((r = alloc(&p[n], s)) && now() - t1 < wait)
            usleep(50000);
        info(&fr, &tot);
        if (r) { printf("  +%4zu MiB: FAILED (CUDA error %d) after %.2f s; have %zu MiB, VRAM free %zu MiB\n", s >> 20, r, now() - t1, got >> 20, fr >> 20); failed = 1; break; }
        memset8(p[n], 0x5a, s);   // really use it
        got += s; n++;
        printf("  +%4zu MiB: ok in %.2f s; have %zu MiB, VRAM free %zu MiB\n", s >> 20, now() - t1, got >> 20, fr >> 20);
    }
    printf("%s: got %zu of %zu MiB; holding %d s\n", failed ? "NOT ENOUGH VRAM" : "ALL ALLOCATED", got >> 20, want >> 20, hold);
    sleep(hold);
    while (n--) mfree(p[n]);
    info(&fr, &tot);
    printf("freed; VRAM free %zu MiB\n", fr >> 20);
    return failed;
}
