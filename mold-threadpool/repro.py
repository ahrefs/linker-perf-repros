#!/usr/bin/env python3
"""CPU cost of mold's default thread pool on small links, alone and in batches.

Generates a C program of ~400 objects (34 MiB PIE with debug info), then links
it with each LINKER at each --threads setting, K links at a time, and prints
the median batch makespan and CPU per link. With --busy N, each batch also
runs N processes that each burn a fixed amount of CPU (--busy-ms on an idle
CPU), standing in for the compilers a build runs next to its links.

CPU per link includes every process the linker starts: the script is a child
subreaper, so mold's forked worker (default fork mode) is reaped and accounted
here. "links done" is when the last linker the script started has exited;
"batch done" also waits for the busy processes.
"""

import argparse
import ctypes
import os
import platform
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

PR_SET_CHILD_SUBREAPER = 36

BUSY_C = """
#include <stdlib.h>
int main(int c, char **v) {
  volatile unsigned long x = 0;
  for (unsigned long i = strtoul(v[1], 0, 10); i; i--) x += i;
  return 0;
}
"""


def generate(objdir, nfiles, nfuncs):
    os.makedirs(objdir, exist_ok=True)

    def one(i):
        obj = os.path.join(objdir, f"m{i}.o")
        if os.path.exists(obj):
            return obj
        lines = ["#include <string.h>"]
        if i > 0:
            lines.append(f"int f{i - 1}_0(int);")
        lines += [f"int f{i}_{j}(int);" for j in range(nfuncs)]
        for j in range(nfuncs):
            if j + 1 < nfuncs:
                tail = f" + f{i}_{j + 1}(x - 1)"
            elif i > 0:
                tail = f" + f{i - 1}_0(x)"
            else:
                tail = ""
            lines.append(f'static const char s{i}_{j}[] = "str_{i}_{j}";')
            lines.append(
                f"int f{i}_{j}(int x) {{ int a[16]; for (int k = 0; k < 16; k++) a[k] = x * k + {j};"
                f" return a[x & 15] + (int)strlen(s{i}_{j}){tail}; }}"
            )
        if i == nfiles - 1:
            lines.append(f"int main(int c, char **v) {{ return f{i}_0(c) & 1; }}")
        src = obj[:-2] + ".c"
        with open(src, "w") as f:
            f.write("\n".join(lines) + "\n")
        subprocess.run(
            ["cc", "-c", "-g", "-O1", "-fPIC", "-ffunction-sections", "-o", obj, src],
            check=True,
        )
        return obj

    with ThreadPoolExecutor(len(os.sched_getaffinity(0))) as ex:
        return list(ex.map(one, range(nfiles)))


def link_args(objs, out):
    def gcc_file(name):
        return subprocess.run(
            ["cc", f"-print-file-name={name}"], capture_output=True, text=True, check=True
        ).stdout.strip()

    crt = {n: gcc_file(n) for n in ["Scrt1.o", "crti.o", "crtbeginS.o", "crtendS.o", "crtn.o"]}
    libdirs = {os.path.dirname(gcc_file("libc.so")), os.path.dirname(crt["crtbeginS.o"])}
    return [
        "-pie", "--build-id", "--eh-frame-hdr", "-m", "elf_x86_64", "--hash-style=gnu",
        "-dynamic-linker", "/lib64/ld-linux-x86-64.so.2", "-o", out,
        crt["Scrt1.o"], crt["crti.o"], crt["crtbeginS.o"],
        *[f"-L{d}" for d in sorted(libdirs)], *objs,
        "-lc", crt["crtendS.o"], crt["crtn.o"],
    ]  # fmt: skip


def busy_cmd(work, ms):
    src, exe = os.path.join(work, "busy.c"), os.path.join(work, "busy")
    with open(src, "w") as f:
        f.write(BUSY_C)
    subprocess.run(["cc", "-O1", "-o", exe, src], check=True)
    iters = 2 * 10**8
    t = time.monotonic()
    subprocess.run([exe, str(iters)], check=True)
    return [exe, str(int(iters * ms / 1000 / (time.monotonic() - t)))]


def batch(cmd, args, outdir, k, busy, nbusy):
    out_idx = args.index("-o") + 1
    start = time.monotonic()
    # posix_spawnp rather than subprocess.Popen: Popen objects reap their pid
    # on garbage collection, which would hide it from the wait4 loop below.
    busy_pids = {os.posix_spawnp(busy[0], busy, os.environ) for _ in range(nbusy)}
    links = set()
    for i in range(k):
        a = list(args)
        a[out_idx] = os.path.join(outdir, f"out{i}.exe")
        links.add(os.posix_spawnp(cmd[0], [*cmd, *a], os.environ))
    link_cpu, links_done = 0.0, None
    while True:
        try:
            pid, status, ru = os.wait4(-1, 0)
        except ChildProcessError:
            break
        if status != 0:
            sys.exit(f"pid {pid} ({cmd}) exited with status {status}")
        # Anything not started as a busy process is a linker or a process a
        # linker forked.
        if pid not in busy_pids:
            link_cpu += ru.ru_utime + ru.ru_stime
        links.discard(pid)
        if not links and links_done is None:
            links_done = time.monotonic() - start
    return links_done, time.monotonic() - start, link_cpu


def parse_cpus(spec):
    cpus = set()
    for part in spec.split(","):
        lo, _, hi = part.partition("-")
        cpus.update(range(int(lo), int(hi or lo) + 1))
    return cpus


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("linkers", nargs="+", metavar="NAME=PATH", help="e.g. mold=/usr/bin/mold lld=/usr/bin/ld.lld")
    p.add_argument("--threads", default="default,4,1", help="comma list of --threads values; 'default' passes none")
    p.add_argument("--jobs", default="1,8,32", help="comma list of concurrent links per batch")
    p.add_argument("--busy", type=int, default=0, help="CPU-bound processes started with each batch")
    p.add_argument("--busy-ms", type=int, default=500, help="CPU time of each busy process on an idle CPU")
    p.add_argument("--runs", type=int, default=7, help="batches per cell (one extra warm-up batch is dropped)")
    p.add_argument("--cpus", help="pin this script and its children to these CPUs, e.g. 0-31")
    p.add_argument("--work", default="mold-threadpool-work", help="directory for objects and outputs; use tmpfs to keep writeback out of the numbers")
    p.add_argument("--files", type=int, default=400)
    p.add_argument("--funcs", type=int, default=150)
    o = p.parse_args()

    if ctypes.CDLL(None).prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        sys.exit("prctl(PR_SET_CHILD_SUBREAPER) failed")
    if o.cpus:
        os.sched_setaffinity(0, parse_cpus(o.cpus))

    objs = generate(os.path.join(o.work, "objs"), o.files, o.funcs)
    outdir = os.path.join(o.work, "out")
    os.makedirs(outdir, exist_ok=True)
    args = link_args(objs, os.path.join(outdir, "out.exe"))
    busy = busy_cmd(o.work, o.busy_ms)

    linkers = [spec.split("=", 1) for spec in o.linkers]
    print(f"kernel {platform.release()}, nproc {os.cpu_count()}, affinity {len(os.sched_getaffinity(0))} CPUs")
    for name, path in linkers:
        v = subprocess.run([path, "--version"], capture_output=True, text=True).stdout.splitlines()[0]
        print(f"{name}: {v}")
    batch([linkers[0][1]], args, outdir, 1, busy, 0)
    size = os.path.getsize(os.path.join(outdir, "out0.exe")) / 2**20
    print(f"output {size:.1f} MiB, {len(objs)} objects, out dir {os.path.abspath(outdir)}")
    if o.busy:
        print(f"each batch also runs {o.busy} busy processes of {o.busy_ms} ms CPU")
    print(f"loadavg before: {open('/proc/loadavg').read().strip()}")
    print()
    print("| linker | --threads | K | links done ms | batch done ms | CPU ms / link |")
    print("|---|---|---|---|---|---|")
    for k in map(int, o.jobs.split(",")):
        for name, path in linkers:
            for t in o.threads.split(","):
                cmd = [path] if t == "default" else [path, f"--threads={t}"]
                res = [batch(cmd, args, outdir, k, busy, o.busy) for _ in range(o.runs + 1)][1:]
                links_done, batch_done, cpu = (statistics.median(r[i] for r in res) for i in range(3))
                print(f"| {name} | {t} | {k} | {links_done * 1000:.0f} | {batch_done * 1000:.0f} | {cpu / k * 1000:.0f} |", flush=True)
    print()
    print(f"loadavg after: {open('/proc/loadavg').read().strip()}")


if __name__ == "__main__":
    main()
