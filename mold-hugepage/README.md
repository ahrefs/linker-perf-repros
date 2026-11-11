# Slow concurrent exec of PIE binaries cached in 2 MiB folios

Standalone repro. It explains why, in our OCaml build, mold-linked ppx
drivers cost about 4x the CPU of GNU ld or lld ones when dune starts many of
them at once.

When a PIE executable's page cache sits in 2 MiB folios, 128 concurrent
starts cost 135-151 ms of kernel CPU each on our three test hosts, against
about 4 ms when the same file is cached in 64-128 KiB folios. The dynamic
loader's relocation writes take copy-on-write faults on the file mapping.
Each fault locks the source page-cache folio while it copies 4 KiB, and a
2 MiB folio has one lock for 512 pages, so the processes serialize on it
(perf, mold 3.0.0 output: 94% of cycles in
`native_queued_spin_lock_slowpath`, reached from `filemap_fault` through
`folio_wait_bit_common` and `folio_wake_bit`).

mold 3.0.0 maps its output file `MAP_SHARED` and calls `MADV_HUGEPAGE` on
that mapping, so on ext4 with large folios (kernel 6.16 and later) its outputs
land in 2 MiB folios. GNU ld's large buffered writes can get there too on big
outputs. lld's outputs, and files read back after `POSIX_FADV_DONTNEED`, land
in 128 KiB folios on hosts where the filesystem's `read_ahead_kb` is 128, and
in 1 MiB folios on the host where it is 1024. A copy of the file is in 64 KiB
folios and fast to start on every host we tried.

## Usage

Run from a scratch directory on the filesystem you build on (the work
directory is created in the current directory), not from inside the repo:

```bash
cd ~/scratch && /path/to/linker-perf-repros/mold-hugepage/repro.py --runs 1000 --jobs 1,32,128 --passes 3 \
  mold=/path/to/mold-3.0.0-x86_64-linux/bin/mold lld=/usr/bin/ld.lld-19 bfd=/usr/bin/ld.bfd
```

`repro.py` builds a C program with a 1M-entry pointer table (1M relative
relocations, 32 MiB binary), links it with each `NAME=LINKER_PATH`, and runs
it `--runs` times at each concurrency. It reports user and sys CPU per run for
three page-cache states of each output: `fresh` (as linked), `evicted`
(`fsync` + `POSIX_FADV_DONTNEED`, then read back on exec) and `copy` (a `cp`).
With passwordless sudo it also prints folio sizes through `folios.py`, which
reads `/proc/self/pagemap` and `/proc/kpageflags`.

`--entries 200000` gives a 6 MiB binary that still shows the effect and runs
in a few seconds.

Report CPU per run, not wall time: CPU per run stays stable on a loaded
shared host.

## Results

2026-11-11, mold 3.0.0 (release binary), lld 19.1.7, GNU ld 2.44. Three
Debian hosts, work directory on ext4, THP `madvise`:

| host | CPUs | kernel | `read_ahead_kb` of the work filesystem |
|---|---|---|---|
| devbox-sg | 2x EPYC 9554, 256 threads | 7.2.9 | 128 |
| devbox-us | 2x EPYC 9554, 256 threads | 7.2.11 | 128 |
| devbox-uk | 2x EPYC 9755, 512 threads | 7.2.11 | 1024 |

Median CPU per exec over 3 passes of 1000 runs, ms, sys in parentheses. Raw
output is in `results/`.

devbox-us:

| linker | state | -j1 | -j32 | -j128 |
|---|---|---|---|---|
| mold | fresh | 7.4 (3.9) | 31.9 (28.4) | 138.2 (135.2) |
| mold | evicted | 7.0 (3.7) | 7.8 (4.2) | 7.9 (4.3) |
| mold | copy | 7.1 (3.5) | 7.6 (4.0) | 7.6 (4.0) |
| lld | fresh | 6.1 (3.6) | 6.3 (3.9) | 6.3 (3.9) |
| lld | evicted | 6.0 (3.9) | 6.6 (4.2) | 6.5 (4.1) |
| lld | copy | 5.7 (3.6) | 6.2 (3.9) | 6.3 (3.8) |
| GNU ld | fresh | 5.6 (3.3) | 34.6 (32.6) | 149.1 (147.3) |
| GNU ld | evicted | 6.0 (3.9) | 6.5 (4.2) | 6.6 (4.4) |
| GNU ld | copy | 6.4 (3.8) | 6.3 (3.9) | 6.3 (4.0) |

`-j128` on each host:

| linker | state | devbox-sg | devbox-us | devbox-uk |
|---|---|---|---|---|
| mold | fresh | 148.6 (144.8) | 138.2 (135.2) | 145.6 (143.0) |
| mold | evicted | 8.2 (4.2) | 7.9 (4.3) | 7.8 (5.0) |
| mold | copy | 8.4 (4.3) | 7.6 (4.0) | 6.1 (3.2) |
| lld | fresh | 6.8 (4.2) | 6.3 (3.9) | 48.1 (46.4) |
| lld | evicted | 6.8 (4.3) | 6.5 (4.1) | 54.4 (52.8) |
| lld | copy | 6.5 (4.0) | 6.3 (3.8) | 5.3 (3.2) |
| GNU ld | fresh | 147.5 (145.3) | 149.1 (147.3) | 152.8 (151.2) |
| GNU ld | evicted | 6.8 (4.2) | 6.6 (4.4) | 53.5 (51.8) |
| GNU ld | copy | 6.8 (4.1) | 6.3 (4.0) | 5.3 (3.3) |

On devbox-uk the `evicted` and lld `fresh` files are in 1 MiB folios, and
their sys time at `-j128` varies between passes (mold `evicted`: 5.0, 4.4
and 28.7 ms).

With `--entries 200000`, mold `fresh` costs 104.1, 91.1 and 93.2 ms per start
at `-j128` on sg, us and uk, and lld costs under 2 ms in every state.

Share of pages per folio size right after linking:

| linker | 32 MiB binary | 6 MiB binary |
|---|---|---|
| mold 3.0.0 | 2 MiB 79-92%, rest mostly 4 KiB | 2 MiB 65-98%, rest mostly 4 KiB |
| GNU ld 2.44 | 2 MiB 85% | 2 MiB 33%, 1 MiB 33%, rest smaller |
| lld 19.1.7, sg and us | 128 KiB 97% | 128 KiB 82% |
| lld 19.1.7, uk | 1 MiB 79% | 4 KiB 51%, 16, 64 and 256 KiB 16% each |
| `cp` of any output | 64 KiB 100% | 64 KiB 99% |

mold 3.0.0 calls `prctl(PR_SET_THP_DISABLE, 0)` at startup, so running it
under `PR_SET_THP_DISABLE` does not change its output's folio sizes.

mold 2.37.1 and 2.42.1 gave the same `fresh` numbers on devbox-sg on
2026-11-05 (136.7 and 137.9 ms at `-j128`).

## Kernel settings and versions

No THP setting changes this: the folio size is decided when the file is
written, and neither the `VM_HUGEPAGE` fault path nor the ext4 buffered-write
path reads `transparent_hugepage/*`. The copy-on-write fault code is the same
from 6.16 to mainline as of 2026-11 (7.4 merge window).

What does work, tested on devbox-us with mold 3.0.0:

- Dropping the output from the page cache after the link (`fdatasync`, then
  `POSIX_FADV_DONTNEED`): the next exec reads it back in 128 KiB folios
  (135.2 to 4.3 ms of sys per start at `-j128`). This depends on the
  readahead size: on devbox-uk (`read_ahead_kb` 1024) the file comes back in
  1 MiB folios and costs 4 to 58 ms per start, depending on the pass.
- `chattr +j` on the output directory: new files get 4 KiB folios (mold
  135.2 to 5.7 ms of sys per start at `-j128`, GNU ld 147.3 to 5.7). At
  `-j1` a start costs about 2 ms more sys than with 128 KiB folios. All data
  written there goes through the journal twice; the link-time cost was not
  measured.

`nr_file_hugepages` in `/proc/vmstat` counts 2 MiB page-cache folios, so a
host can be checked for them without root.
