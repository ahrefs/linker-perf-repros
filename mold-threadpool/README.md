# mold 3 thread pool cost on concurrent small links

Standalone repro. It covers the CPU half of why our OCaml builds were
slower when linked with mold than with lld:
each mold process starts `min(CPUs, 32)` Rayon workers, and on small links
most of that CPU is overhead. When a build links many small executables at
once (dune links ~150 ppx drivers together), the overhead makes the batch
slower than `--threads=1` and slower than lld.

The other half, extra writes and slow exec from `MADV_HUGEPAGE` on the
output, is in [`../mold-hugepage`](../mold-hugepage). This repro writes its
outputs to tmpfs to keep writeback out of the numbers. On 2026-11-05, with
outputs on ext4 and devbox-sg's `vm.dirty_bytes` then at 512 MiB, a batch of
64 mold links of 35 MiB each stalled for minutes in `balance_dirty_pages`
(lld not tried there).

## Usage

```bash
MOLD=/path/to/mold-3.0.0-x86_64-linux/bin/mold
./repro.py --cpus 0-31 --work /dev/shm/mold-threadpool-work --jobs 1,8,32,64 \
  mold=$MOLD lld=ld.lld-19
./repro.py --cpus 0-31 --work /dev/shm/mold-threadpool-work --jobs 8,32 \
  --busy 32 --busy-ms 500 mold=$MOLD lld=ld.lld-19
MOLD_JOBS=1 ./repro.py --cpus 0-31 --work /dev/shm/mold-threadpool-work \
  --jobs 32 --threads default,1 mold=$MOLD
# whole host, K up to the number of CPU threads
./repro.py --work /dev/shm/mold-threadpool-work --jobs 1,32,64,128,256 \
  mold=$MOLD lld=ld.lld-19
```

`repro.py` compiles a 400-object C program with debug info (34 MiB PIE),
then links it K times at once with each linker at `--threads` default, 4
and 1, 7 batches per cell after one warm-up. It reports when the last link
finished and the CPU per link, counting mold's forked worker (the script is a
child subreaper). `--busy N` adds N processes that each burn `--busy-ms` of
CPU in the same batch, standing in for compilers running next to the links.
`--cpus` pins everything, which also sets mold's default thread count (32 on
32 CPUs). lld is optional. The last column counts host-wide compaction stalls
(`compact_stall` in `/proc/vmstat`) during each cell, which flags cells where
the kernel compacted memory to back mold's `MADV_HUGEPAGE` heap.

Prerequisites: Linux x86-64, `cc` (gcc or clang), Python 3.9+.

## Results

devbox-sg, 2026-11-05: Debian kernel 7.2.9, 256 CPUs, run pinned to CPUs
0-31 (idle before the run, 1-minute loadavg 22 host-wide), outputs on tmpfs,
mold 3.0.0 (official release binary), Debian lld 19.1.7. Medians in ms. Raw
output in `results/`.

Links only:

| K | mold default (32) | mold `--threads=4` | mold `--threads=1` | lld default | lld `--threads=1` |
|---|---|---|---|---|---|
| 1, wall | 25 | 41 | 94 | 78 | 118 |
| 1, CPU / link | 437 | 154 | 97 | 260 | 118 |
| 8, wall | 94 | 70 | 111 | 116 | 129 |
| 32, wall | 316 | 223 | 184 | 223 | 200 |
| 32, CPU / link | 249 | 169 | 160 | 187 | 180 |
| 64, wall | 616 | 497 | 388 | 436 | 431 |

With 32 busy processes of 500 ms CPU in each batch, time until the last link
finished:

| K | mold default | mold `--threads=4` | mold `--threads=1` | lld default |
|---|---|---|---|---|
| 8 | 472 | 242 | 203 | 189 |
| 32 | 631 | 367 | 381 | 432 |

`MOLD_JOBS=1`, K=32, links only: 737 ms with default threads, 2884 ms with
`--threads=1`.

- A lone link with 32 threads is 3.8x faster than with 1 thread and costs
  4.5x the CPU.
- From K=32 on 32 CPUs, the default is the slowest setting: 1.7x the
  `--threads=1` wall at K=32, 1.6x at K=64, and slower than lld's default.
- Next to other CPU work, links finish 2.3x later with the default than with
  `--threads=1` at K=8, 1.7x at K=32. When the whole batch (busy processes
  included) finished varied too much between runs to compare (raw files).
- `MOLD_JOBS=1` serializes the links, so it is 2.3x slower than the default.
- It is not the output write path: with outputs on ext4 instead of tmpfs
  (same host, unpinned, earlier run), a lone link cost about 550 ms CPU at 32
  threads and 105 ms at 1, the same gap.
- On devbox-sg on 2026-11-05, `strace -f -c` of one 32-thread link counted
  14,000 to 30,000 `sched_yield` calls depending on the run, about 1,000 to
  1,500 at 4 threads, none at 1. `perf` put 13% of a 32-thread link in
  `native_queued_spin_lock_slowpath` and 4% in `osq_lock`. The release
  binary is stripped, so the split between idle workers yielding and lock
  contention is not measured.

### Recheck on 2026-11-13

Same commands with mold 3.0.0 on devbox-sg (2x EPYC 9554, kernel 7.2.9),
devbox-us (same CPU, kernel 7.2.11) and devbox-uk (2x EPYC 9755, 512 threads,
kernel 7.2.11). CPUs 0-31 were 97-100% idle on average before each run. Wall
time until the last link finished, ms, with the compaction stalls of the cell
in parentheses when there were any:

| host | K | mold default | mold `--threads=4` | mold `--threads=1` | lld default |
|---|---|---|---|---|---|
| sg | 1 | 25 | 37 | 91 | 81 |
| sg | 32 | 332 (10,343) | 241 (2,941) | 237 (1,597) | 236 |
| sg | 64 | 605 (1,153) | 472 | 402 | 449 |
| sg | 8, next to 32 busy | 501 | 214 | 207 | 203 |
| us | 1 | 23 | 34 | 89 | 80 |
| us | 32 | 223 | 163 | 138 | 199 |
| us | 64 | 439 | 326 | 290 | 373 |
| us | 8, next to 32 busy | 419 | 231 | 204 | 192 |
| uk | 1 | 21 (14) | 31 | 81 | 71 |
| uk | 32 | 232 (1,015) | 170 | 133 | 199 |
| uk | 64 | 475 (8,223) | 337 | 293 | 367 |
| uk | 8, next to 32 busy | 411 | 209 | 208 | 173 |

`MOLD_JOBS=1`, K=32: 744 ms on sg, 620 ms on us and uk.

- On all three hosts the default is the slowest setting from K=32: 1.4x to
  1.7x the `--threads=1` wall at K=32, 1.5x to 1.6x at K=64, and slower than
  lld's default.
- Next to 32 busy processes at K=8, links finish 2.0x to 2.4x later with the
  default than with `--threads=1`.
- devbox-sg matches its 2026-11-05 numbers, and a 2026-11-11 run on sg and
  us gave the same results (`results/20261111-*`).

Only mold cells had compaction stalls, mostly with the default thread count.
mold calls `MADV_HUGEPAGE` on its heap (1 GiB, 16 MiB and 512 KiB regions).
With THP `defrag` set to `madvise`, a page fault in such a region compacts
memory synchronously when the local NUMA node has no free 2 MiB block. Before
the links run, the NUMA nodes of CPUs 0-31 had 259 and 718 free 2 MiB blocks
on sg and 515 on uk, against 2,086 on us, which had no stalls. The stalls cost
little this time: on uk, K=32 with the default took 232 ms with 1,015 stalls,
and 213 ms with none in a shorter run (3 batches) earlier that day.

They can cost a lot more. On 2026-11-11 devbox-uk had run out of free 2 MiB
blocks, and mold spent about three quarters of its CPU in compaction: one
link with default threads took 395 ms and 6.3 s of CPU
(`results/20261111-uk-*`, `results/20261111-uk-compaction.txt`).

### Whole host, K up to the number of CPU threads (2026-11-13)

Same program, not pinned, K up to 256 on sg and us and 512 on uk. mold's
default is still 32 threads per link; lld's default is one thread per CPU.
All CPUs were 98-99% idle before each run. devbox-us had no compaction
stalls. Wall time until the last link finished, ms:

| K | mold default | mold `--threads=4` | mold `--threads=1` | lld default |
|---|---|---|---|---|
| 1 | 25 | 46 | 100 | 96 |
| 32 | 107 | 71 | 124 | 126 |
| 64 | 192 | 118 | 149 | 202 |
| 128 | 331 | 209 | 235 | 343 |
| 256 | 639 | 379 | 370 | 763 |

CPU per mold link on devbox-us: 472-597 ms with the default at every K,
104-228 ms with `--threads=1`.

K equal to the number of CPU threads on each host, plus K=256 on uk, with the
compaction stalls of the cell in parentheses when there were any:

| host | K | mold default | mold `--threads=4` | mold `--threads=1` | lld default |
|---|---|---|---|---|---|
| sg | 256 | 765 (3,477) | 440 (410) | 462 (5,648) | 523 |
| us | 256 | 639 | 379 | 370 | 763 |
| uk | 256 | 545 | 303 | 284 (2,007) | 707 |
| uk | 512 | 1,071 (16,268) | 563 | 500 (2,070) | 1,404 |

- A lone link is fastest with the default. From K=64 the default is the
  slowest mold setting on all three hosts, and at K=256 it takes 1.7x to
  1.9x the `--threads=1` wall.
- `--threads=4` is the fastest mold setting from K=32 to K=128 on all three
  hosts.
- lld's default is slower than mold's at K=256 and above on us and uk. On
  sg, where most mold cells had compaction stalls, it is not.

### Where the CPU goes (2026-11-13)

Most of the extra CPU is a fixed cost per link, not something concurrency
adds. One link, devbox-us, pinned to CPUs 0-31, mold `--no-fork`, median of 7
(`tools/size_scaling.py`, `results/20261113-us-size-scaling.txt`):

| objects | output | 1 thread: wall, CPU | 32 threads: wall, CPU |
|---|---|---|---|
| 25 | 2.1 MiB | 9, 9 | 11, 162 |
| 100 | 8.5 MiB | 26, 26 | 13, 200 |
| 400 | 34.1 MiB | 92, 91 | 21, 379 |

Per link, from perf tracepoints (`tools/syscall_counts.py`,
`results/20261113-us-syscalls.txt`):

| objects | threads | CPU ms (user + sys) | `sched_yield` | context switches | `futex` | `clone3` |
|---|---|---|---|---|---|---|
| 25 | 1 | 9 | 0 | 1 | 1 | 0 |
| 25 | 4 | 23 | 5,489 | 90 | 190 | 3 |
| 25 | 32 | 154 | 37,997 | 6,667 | 2,810 | 31 |
| 400 | 1 | 96 | 0 | 2 | 1 | 0 |
| 400 | 32 | 399 | 49,159 | 9,123 | 3,747 | 31 |

Creating the 31 worker threads is cheap. The yields and context switches
barely depend on the input size.

To see where the CPU goes, mold 3.0.0 was built from its tag with debug info
and frame pointers (`CARGO_PROFILE_RELEASE_DEBUG=true RUSTFLAGS="-C
force-frame-pointers=yes" cargo build --release -p mold-cli`). That build costs
the same CPU as the release binary. `tools/cpu_split.py` records 20 links at
32 threads with `perf record -g` and assigns each sample to the first known
component on its stack (`results/20261113-us-cpu-split.txt`):

| component | 25 objects | 400 objects |
|---|---|---|
| Rayon idle loop (`WorkerThread::wait_until_cold`, crossbeam deque steal and epoch), incl. the kernel time under it | 59% | 56% |
| other Rayon code, incl. kernel | 12% | 12% |
| kernel, caller not resolved (likely more yields) | 18% | 16% |
| mold's own code, incl. kernel | 7% | 14% |
| mimalloc, incl. kernel | 3% | 2% |

The hottest single function is `crossbeam_deque::Stealer::steal` (23% and
12%). perf adds cost to every context switch, so the kernel shares are a bit
high; the CPU numbers above are measured without perf. The same idle loop
shows up without mold in [`../rayon-idle`](../rayon-idle).

### Limiting concurrent mold processes (2026-11-13)

mold 3.0.0 honours only `MOLD_JOBS=1` (`src/jobs.rs` checks for exactly `"1"`;
other values do nothing). `moldjobs.c` emulates `MOLD_JOBS=N` for any N: it
waits for one of N slots of a POSIX semaphore, runs mold with its default 32
threads, and frees the slot when mold exits.

```bash
cc -O2 -o moldjobs moldjobs.c
MOLD_REAL=$MOLD MOLD_JOBS_N=4 MOLD_JOBS_SEM=/moldjobs-4 \
  ./repro.py --cpus 0-31 --jobs 32,64 --threads default mold-jobs4=$PWD/moldjobs
```

devbox-us, time until the last link finished, ms. The `--threads` rows come
from the runs above on the same host and day (`results/20261113-us-moldjobs-*`):

| setting | 32 CPUs, K=32 | 32 CPUs, K=64 | 256 CPUs, K=128 | 256 CPUs, K=256 |
|---|---|---|---|---|
| no limit | 227 | 397 | 336 | 626 |
| `MOLD_JOBS=1` (mold's own lock) | 627 | 1,247 | 3,298 | 6,590 |
| emulated `MOLD_JOBS=4` | 325 | 651 | 1,016 | 2,047 |
| emulated `MOLD_JOBS=16` | 244 | 457 | 530 | 1,052 |
| no limit, `--threads=4` | 163 | 326 | 209 | 379 |
| no limit, `--threads=1` | 138 | 290 | 235 | 370 |

Limiting the number of mold processes is slower than no limit at every N
tried (1, 2, 4, 8 and 16). The emulation adds 3-9% over mold's own lock at
N=1. One link at a time with 32 threads leaves most of the 32 threads idle,
because a small link is only about 4x faster with 32 threads than with 1.

## Upstream context

- Default thread count is `min(available_parallelism, 32)` in 3.0.0 and on
  `main` (`elf/driver.rs`, checked 2026-11-05). `MOLD_JOBS` only accepts `1`
  (`common/jobs.rs`).
- rui314/mold#117 and #1381: maintainer position is that the build system should pass
  `--threads`; in #117 he argued that CPU competition alone should not hurt
  throughput. In #1381 he suggested allowing `MOLD_JOBS` values above 1.
- rui314/mold#1696 and #1723 (closed 2026-10): input-size-scaled thread count, declined
  because the single-link gain was a fraction of a millisecond.
- rui314/mold#1694, #1695, #1716, #1717: Rayon idle-polling and startup changes,
  declined as Rayon's business.
