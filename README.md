# linker-perf-repros

Standalone reproductions of linker-related performance problems we hit in a
large OCaml build, written so upstream maintainers can run them. They need
Linux x86-64, a C compiler, binutils, Python 3 and the linkers under test
(`rayon-idle` needs a Rust toolchain instead of a linker). See each
directory's README for details.

- [`mold-hugepage`](mold-hugepage): parallel starts of a PIE executable whose
  page cache sits in 2 MiB folios cost ~30x the kernel CPU of the same file in
  smaller folios, because ld.so's copy-on-write relocation faults serialize
  on the folio lock. mold's `MADV_HUGEPAGE` on its output puts every output in
  2 MiB folios; GNU ld gets there on large outputs.
- [`mold-threadpool`](mold-threadpool): mold 3's default thread pool
  (`min(CPUs, 32)` workers per link) makes a batch of concurrent small links
  slower than `--threads=1`. A 9 ms link costs about 150 ms of CPU at 32
  threads, most of it in Rayon's idle loop. Limiting the number of mold
  processes (`MOLD_JOBS`) is slower still.
- [`rayon-idle`](rayon-idle): the same cost without mold: a Rayon program made
  of short parallel phases uses 10x the CPU at 32 threads, and concurrent
  copies finish 1.9x to 2.7x later than with 1 thread.
