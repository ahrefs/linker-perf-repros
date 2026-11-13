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
little this time: uk at K=32 took 232 ms with the default, us 223 ms.

They can cost a lot more. On 2026-11-11 devbox-uk had run out of free 2 MiB
blocks, and mold spent about three quarters of its CPU in compaction: one
link with default threads took 395 ms and 6.3 s of CPU
(`results/20261111-uk-*`, `results/20261111-uk-compaction.txt`).

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

## Draft issue

Not posted. Title:

> Default thread count makes concurrent small links slower than `--threads=1` (3.0.0)

Body:

> This has been created by an agent and not been reviewed by a human yet.
>
> When a build runs many small links at once, mold's default thread count
> makes the batch slower than `--threads=1`, and slower than lld's default.
> #1696 proposed fewer threads for small inputs and was declined on
> single-link latency. This report is about throughput when links run
> concurrently, which that PR did not measure.
>
> Repro: <link to repro.py>. It links a 34 MiB PIE (400 objects, debug
> info) K times at once. Linux 7.2.9, pinned to 32 idle CPUs, outputs on
> tmpfs, mold 3.0.0 release binary, lld 19.1.7. Medians in ms:
>
> | K | mold default (32 threads) | mold `--threads=4` | mold `--threads=1` | lld default |
> |---|---|---|---|---|
> | 1, wall | 25 | 41 | 94 | 78 |
> | 1, CPU per link | 437 | 154 | 97 | 260 |
> | 32, wall | 316 | 223 | 184 | 223 |
> | 64, wall | 616 | 497 | 388 | 436 |
> | 8, next to 32 CPU-bound processes, last link done | 472 | 242 | 203 | 189 |
>
> `MOLD_JOBS=1` at K=32 takes 737 ms, because it serializes the links.
>
> Two more hosts (the same CPU on Linux 7.2.11, and a 2x EPYC 9755) give the
> same ordering (K=32 on the first: 223, 163, 138 and 199 ms).
>
> One 32-thread link makes 14,000 to 30,000 `sched_yield` calls (about
> 1,000 at 4 threads), so much of the extra CPU looks like idle workers
> yielding. I have not separated that from lock contention.
>
> We hit this in an OCaml build where dune links ~150 small executables at
> the same moment on a 256-CPU host. Passing `--threads` per link works,
> but the build system does not know at link time how loaded the host is.
> Would either of these be acceptable?
>
> 1. `MOLD_JOBS=N` for N > 1, as suggested in #1381.
> 2. A lower default cap, or one that scales with input size, justified by
>    the concurrent numbers above rather than single-link latency.
