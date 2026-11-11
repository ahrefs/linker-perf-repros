# Slow concurrent exec of PIE binaries cached in 2 MiB folios

Standalone repro. It explains why, in our OCaml build, mold-linked ppx
drivers cost about 4x the CPU of GNU ld or lld ones when dune starts many of
them at once.

When a PIE executable's page cache sits in 2 MiB folios, 128 concurrent
starts cost about 135 ms of kernel CPU each, against about 4 ms when the same
file is cached in 64-128 KiB folios. The dynamic loader's relocation writes
take copy-on-write faults on the file mapping. Each fault locks the source
page-cache folio while it copies 4 KiB, and a 2 MiB folio has one lock for
512 pages, so the processes serialize on it (perf: 78% of cycles in
`native_queued_spin_lock_slowpath` under `filemap_fault` ->
`folio_wait_bit_common` / `folio_wake_bit`).

mold maps its output with `MADV_HUGEPAGE`, so on ext4 with large folios
(kernel 6.16 and later) its outputs land in 2 MiB folios. GNU ld's large
buffered writes can get there too on big outputs. lld's outputs end up in
128 KiB folios. Copying the file, or evicting it with `POSIX_FADV_DONTNEED`
and reading it back, makes it fast again.

## Usage

Run from a scratch directory on the filesystem you build on (the work
directory is created in the current directory), not from inside the repo:

```bash
cd ~/scratch && /path/to/linker-perf-repros/mold-hugepage/repro.py --runs 1000 --jobs 1,32,128 --passes 3 mold=/usr/bin/mold lld=/usr/bin/ld.lld-19 bfd=/usr/bin/ld.bfd
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

devbox-sg, 2026-11-05: Debian kernel 7.2.9, ext4 on md RAID, 256 CPUs, THP
`madvise`. Median CPU per exec over 3 passes of 1000 runs, ms, sys in
parentheses.

| linker | state | -j1 | -j32 | -j128 |
|---|---|---|---|---|
| mold 2.37.1 | fresh | 7.5 (3.7) | 30.5 (27.0) | 137.9 (134.2) |
| mold 2.37.1 | evicted | 7.9 (4.0) | 8.4 (4.3) | 8.2 (4.2) |
| mold 2.37.1 | copy | 7.3 (3.4) | 7.8 (3.8) | 7.8 (3.9) |
| mold 2.42.1 | fresh | 7.6 (3.6) | 30.9 (27.2) | 136.7 (133.2) |
| mold 3.0.0 | fresh | 7.4 (3.3) | 30.4 (26.6) | 131.0 (127.7) |
| lld 19.1.7 | fresh | 6.4 (3.3) | 6.5 (4.0) | 6.5 (4.0) |
| GNU ld 2.44 | fresh | 5.9 (3.4) | 32.3 (30.0) | 140.1 (138.0) |
| GNU ld 2.44 | copy | 5.9 (3.6) | 6.5 (3.9) | 6.5 (3.8) |

The `evicted` and `copy` rows of every linker are within 6.3-8.6 ms at every
concurrency.

Share of pages per folio size right after linking:

| linker | 32 MiB binary | 6 MiB binary |
|---|---|---|
| mold 2.37.1 | 2 MiB 85%, 4 KiB 13% | 2 MiB 65%, 4 KiB 33% |
| GNU ld 2.44 | 2 MiB 85% | 2 MiB 33%, 1 MiB 33%, rest smaller |
| lld 19.1.7 | 128 KiB 97% | 128 KiB 82% |
| `cp` of any output | 64 KiB 100% | 64 KiB 99% |

Running mold under `prctl(PR_SET_THP_DISABLE)` does not change its output's
folio sizes.

## Kernel settings and versions

No THP setting changes this: the folio size is decided when the file is
written, and neither the `VM_HUGEPAGE` fault path nor the ext4 buffered-write
path reads `transparent_hugepage/*`. The copy-on-write fault code is the same
from 6.16 to mainline as of 2026-11 (7.4 merge window).

What does work, tested on devbox-sg:

- Dropping the output from the page cache after the link (`fdatasync`, then
  `POSIX_FADV_DONTNEED`): the next exec reads it back in 64-128 KiB folios.
- `chattr +j` on the output directory: new files get 4 KiB folios (mold
  129.5 to 5.6 ms of sys per start at `-j128`). All data written there goes
  through the journal twice; the link-time cost was not measured.

`nr_file_hugepages` in `/proc/vmstat` counts 2 MiB page-cache folios, so a
host can be checked for them without root.
