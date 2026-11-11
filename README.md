# linker-perf-repros

Standalone reproductions of linker-related performance problems we hit in a
large OCaml build, written so upstream maintainers can run them. Each one
needs only a C compiler, Python 3 and the linkers under test.

- [`mold-hugepage`](mold-hugepage): parallel starts of a PIE executable whose
  page cache sits in 2 MiB folios cost ~30x the kernel CPU of the same file in
  smaller folios, because ld.so's copy-on-write relocation faults serialize
  on the folio lock. mold's `MADV_HUGEPAGE` on its output puts every output in
  2 MiB folios; GNU ld gets there on large outputs.
- [`mold-threadpool`](mold-threadpool): mold 3's default thread pool
  (`min(CPUs, 32)` workers per link) makes a batch of concurrent small links
  slower than `--threads=1` and slower than lld.
