# Third-party code

USM itself is under the [MIT license](LICENSE). It includes:

| Path | What | License |
|---|---|---|
| `third_party/liburing/` | [liburing](https://github.com/axboe/liburing) 2.15 (io_uring helper library), statically linked into `usm-vramd` | MIT, or LGPL-2.1 at your choice (see its `LICENSE`, `COPYING`) |
| `third_party/linux/linux/ublk_cmd.h` | the Linux kernel's ublk user-space API header | GPL-2.0 WITH Linux-syscall-note (allows use from programs under any license) |

At run time USM loads, but does not include, NVIDIA's `libcuda.so.1` (part of the NVIDIA driver) and `liblz4.so.1`
(BSD-2-Clause). NVIDIA, CUDA and GeForce are trademarks of NVIDIA Corporation.
