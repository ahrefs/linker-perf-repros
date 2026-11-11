#!/usr/bin/env python3
"""Exec cost of PIE binaries under concurrency, by linker and page-cache state.

Builds one C program whose startup applies many R_X86_64_RELATIVE relocations
(a large pointer table in .data.rel.ro), links it with each given linker, then
runs it RUNS times at each concurrency level and reports CPU per run.

Each linker output is measured in three page-cache states:
  fresh    the file exactly as the linker left it in the page cache
  evicted  same file after fsync + posix_fadvise(DONTNEED), so the next exec
           reads it back through normal readahead
  copy     a copy of the file (shutil.copyfile), never mapped by a linker

With root (sudo -n), the first pass also prints the page-cache folio sizes
of each file (folios.py, from /proc/self/pagemap and /proc/kpageflags).

usage: repro.py [--runs N] [--jobs 1,32,128] [--passes P] [--entries M]
                NAME=LINKER_PATH [NAME=LINKER_PATH ...]
"""
import argparse, os, resource, shutil, subprocess, sys, tempfile, time

ap = argparse.ArgumentParser()
ap.add_argument("--runs", type=int, default=2000)
ap.add_argument("--jobs", default="1,32,128")
ap.add_argument("--passes", type=int, default=3)
ap.add_argument("--entries", type=int, default=1_000_000)
ap.add_argument("--dir", default=None)
ap.add_argument("linkers", nargs="+")
a = ap.parse_args()
jobs = [int(j) for j in a.jobs.split(",")]
linkers = dict(x.split("=", 1) for x in a.linkers)

work = a.dir or tempfile.mkdtemp(prefix="mold-thp-", dir=os.getcwd())
os.makedirs(work, exist_ok=True)

MAIN_C = r"""
#include <stdio.h>
#include <string.h>
#include <unistd.h>
extern const char *const tab[];
int main(int argc, char **argv) {
  if (argc > 1 && strcmp(argv[1], "hold") == 0) {
    printf("%d\n", getpid()); fflush(stdout); pause();
  }
  return tab[argc][0] == 'x';
}
"""


def gen_table(path, n):
    with open(path, "w") as f:
        f.write(f"static char buf[{n}];\n")
        f.write(f"const char *const tab[{n}] = {{\n")
        for i in range(0, n, 16):
            f.write(",".join(f"buf+{j}" for j in range(i, min(i + 16, n))) + ",\n")
        f.write("};\n")


def sh(*cmd, **kw):
    return subprocess.run(cmd, check=True, **kw)


def link(name, out):
    # gcc runs the first "ld" it finds in -B dirs; a symlink dir picks the linker.
    d = os.path.join(work, "ld", name)
    os.makedirs(d, exist_ok=True)
    if not os.path.lexists(os.path.join(d, "ld")):
        os.symlink(os.path.abspath(linkers[name]), os.path.join(d, "ld"))
    sh("cc", "-pie", f"-B{d}/", "main.o", "tab.o", "-o", out, cwd=work)


def build():
    gen_table(os.path.join(work, "tab.c"), a.entries)
    with open(os.path.join(work, "main.c"), "w") as f:
        f.write(MAIN_C)
    for src in ("tab", "main"):
        sh("cc", "-O1", "-fPIE", "-c", f"{src}.c", "-o", f"{src}.o", cwd=work)
    for name, ld in linkers.items():
        out = os.path.join(work, f"prog.{name}")
        if os.path.exists(out):
            os.unlink(out)
        link(name, out)
        comment = subprocess.run(["readelf", "-p", ".comment", out], capture_output=True, text=True).stdout
        tag = [l.split("]", 1)[1].strip() for l in comment.splitlines() if "]" in l and ("mold" in l or "LLD" in l or "Linker" in l)]
        print(f"linked {name}: {os.path.getsize(out) / 2**20:.1f} MiB, {tag or '(GNU ld)'}", flush=True)


def evict(path):
    fd = os.open(path, os.O_RDONLY)
    os.fsync(fd)
    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    os.close(fd)


def folio_histogram(path):
    """Page-cache folio sizes of PATH via folios.py, or None without root."""
    if subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode:
        return None
    folios = os.path.join(os.path.dirname(os.path.abspath(__file__)), "folios.py")
    r = subprocess.run(["sudo", "-n", sys.executable, folios, path], capture_output=True, text=True)
    return r.stdout.split(": ", 1)[-1].strip() if r.returncode == 0 else None


def run_batch(path, j):
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    t = time.monotonic()
    sh("sh", "-c", f"seq {a.runs} | xargs -P {j} -n 1 {path}", stdout=subprocess.DEVNULL)
    wall = time.monotonic() - t
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    u = (after.ru_utime - before.ru_utime) / a.runs * 1e3
    s = (after.ru_stime - before.ru_stime) / a.runs * 1e3
    return u, s, wall


def prepare(name, state):
    src = os.path.join(work, f"prog.{name}")
    if state == "fresh":
        # Relink so the page cache holds exactly what the linker wrote.
        os.unlink(src)
        link(name, src)
        return src
    if state == "evicted":
        evict(src)
        return src
    dst = os.path.join(work, f"copy.{name}")
    if os.path.exists(dst):
        os.unlink(dst)
    shutil.copyfile(src, dst)
    os.chmod(dst, 0o755)
    return dst


build()
print(f"host: {os.uname().nodename}, kernel {os.uname().release}, {os.cpu_count()} cpus, "
      f"THP {open('/sys/kernel/mm/transparent_hugepage/enabled').read().strip()}", flush=True)
fs = subprocess.run(["df", "-T", work], capture_output=True, text=True).stdout.splitlines()[-1].split()[1]
print(f"work dir: {work} ({fs}), runs per cell: {a.runs}, passes: {a.passes}\n", flush=True)

states = ["fresh", "evicted", "copy"]
names = list(linkers)
results = {}
for p in range(a.passes):
    order = names[p % len(names):] + names[:p % len(names)]
    for name in order:
        for state in states:
            path = prepare(name, state)
            fh = folio_histogram(path) if p == 0 else None
            if fh:
                print(f"pass {p} {name:10s} {state:8s} folios: {fh}", flush=True)
            for j in jobs:
                u, s, w = run_batch(path, j)
                results.setdefault((name, state, j), []).append((u, s))
                print(f"pass {p} {name:10s} {state:8s} -j{j:<4d} user {u:6.2f} ms  sys {s:7.2f} ms  per run", flush=True)

print("\nmedian CPU per run over passes (user + sys, ms):")
print(f"{'linker':10s} {'state':8s} " + " ".join(f"{'-j' + str(j):>16s}" for j in jobs))
for name in names:
    for state in states:
        cells = []
        for j in jobs:
            v = sorted(results[(name, state, j)], key=lambda x: x[0] + x[1])
            u, s = v[len(v) // 2]
            cells.append(f"{u + s:6.1f} (sys {s:5.1f})")
        print(f"{name:10s} {state:8s} " + " ".join(f"{c:>16s}" for c in cells))
