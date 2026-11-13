# Rayon idle workers burn CPU between short parallel phases

Standalone repro, no mold involved. In mold 3.0.0 most of the CPU of a small
link at 32 threads goes to Rayon's idle loop rather than to linking (see
[`../mold-threadpool`](../mold-threadpool)). This program has the same shape
as such a link: a series of short parallel phases on a Rayon pool, separated
by short serial steps. It uses the Rayon versions in mold 3.0.0's
`Cargo.lock` (rayon 1.12.0, rayon-core 1.13.0).

## Usage

```bash
cargo build --release
./run.py --cpus 0-31 --jobs 1,8,32,64
./run.py --cpus 0-31 --jobs 1,8,32,64 -- --items 512 --item-us 3
./run.py --jobs 1,64,128,256
```

`target/release/rayon-idle` runs `--phases` (50) phases. Each phase spins for
`--serial-us` (30) on the calling thread, then runs a `par_iter` over `--items`
(64) items that each spin for `--item-us` (2.5), so the default is 9.5 ms of
work. It prints its wall time and the CPU time of the whole process.
`run.py` starts K copies at once for each `--threads` value (1, 4 and 32 by
default) and prints the median time until the last copy finished and the CPU
per copy, 7 batches per cell after one warm-up. `--cpus` pins everything.

## Results

devbox-us, 2026-11-13: Debian kernel 7.2.11, 2x EPYC 9554 (256 CPUs), rustc
1.99.1. Time until the last copy finished, ms, with CPU per copy in
parentheses. Raw output in `results/`.

9.5 ms of work per copy, pinned to CPUs 0-31:

| K | 1 thread | 4 threads | 32 threads |
|---|---|---|---|
| 1 | 11.3 (11.5) | 5.4 (15.3) | 6.6 (116.5) |
| 8 | 12.0 (11.2) | 8.1 (15.0) | 13.4 (44.8) |
| 32 | 21.8 (11.1) | 24.9 (14.3) | 41.1 (35.4) |
| 64 | 37.2 (11.1) | 38.3 (13.7) | 72.1 (31.8) |

Same work, not pinned (256 CPUs):

| K | 1 thread | 4 threads | 32 threads |
|---|---|---|---|
| 1 | 11.4 (11.6) | 5.8 (15.7) | 7.4 (146.9) |
| 64 | 24.1 (11.6) | 20.1 (16.1) | 45.1 (121.3) |
| 128 | 37.7 (11.7) | 34.6 (15.9) | 87.9 (119.2) |
| 256 | 64.0 (11.6) | 63.6 (15.9) | 173.7 (115.2) |

78 ms of work per copy (`--items 512 --item-us 3`), pinned to CPUs 0-31:

| K | 1 thread | 4 threads | 32 threads |
|---|---|---|---|
| 1 | 83.1 (83.1) | 23.6 (86.7) | 10.8 (216.8) |
| 8 | 83.9 (82.7) | 42.2 (86.1) | 45.3 (164.8) |
| 32 | 90.1 (82.5) | 129.5 (86.8) | 143.8 (138.2) |
| 64 | 236.2 (82.8) | 219.5 (86.2) | 260.8 (124.2) |

- One copy with 9.5 ms of work costs 10x the CPU at 32 threads (116.5 ms
  against 11.5) and finishes later than with 4 threads.
- With copies running at once, 32-thread pools finish 1.9x later than single
  threads at K=32 and K=64 on 32 CPUs, and 2.7x later at K=256 on 256 CPUs.
- With 8x more work per phase, one copy is 7.7x faster at 32 threads for 2.6x
  the CPU, and 32 threads are still 1.6x slower than 1 at K=32.
- The idle loop takes whatever CPU is free: its cost per copy drops as K
  grows, but the batch still finishes later.

## Where the CPU goes

`perf record` of 20 single copies at 32 threads, pinned to CPUs 0-31
(`results/20261113-us-perf.txt`): 39% in
`crossbeam_epoch::default::with_handle` (pinning the epoch), 9% in
`crossbeam_deque::Stealer::steal`, 4% in `crossbeam_epoch::Global::try_advance`,
2% in the steal scan loop and 2% in `rayon_core::sleep::Sleep::wake_specific_thread`.
The work itself (`bridge_producer_consumer::helper` and `clock_gettime`) is
about 10%.

That is rayon-core 1.13's idle loop (`WorkerThread::wait_until_cold` and
`sleep/mod.rs`). A worker that runs out of work tries to steal from every
other worker's deque, pinning the crossbeam epoch for each attempt, then calls
`thread::yield_now()`. It repeats this 33 times (`ROUNDS_UNTIL_SLEEPY` is 32)
before it sleeps on a condition variable, and new jobs wake sleeping workers
into the same loop. A pool of 32 threads running short phases goes through it
many times per phase.
