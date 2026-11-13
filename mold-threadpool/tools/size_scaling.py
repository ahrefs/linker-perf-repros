#!/usr/bin/env python3
"""Where does mold's extra CPU at 32 threads go? Run pinned (taskset -c 0-31).
1. CPU and wall of one link vs input size, --threads=32 vs 1 (median of 7, mold --no-fork).
2. mold --perf for the full-size link at 32 and 1 threads.
usage: size_scaling.py REPRO_DIR WORK_DIR MOLD"""
import os, resource, statistics, subprocess, sys, time

repro_dir, work, mold = sys.argv[1:]
sys.path.insert(0, repro_dir)
import repro  # noqa: E402


def link(args, extra):
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    t = time.monotonic()
    subprocess.run([mold, "--no-fork", *extra, *args], check=True, stdout=subprocess.DEVNULL)
    wall = time.monotonic() - t
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    return wall * 1e3, (after.ru_utime - before.ru_utime) * 1e3, (after.ru_stime - before.ru_stime) * 1e3


print("| objects | output MiB | threads | wall ms | user ms | sys ms | extra CPU vs 1 thread |")
print("|---|---|---|---|---|---|---|")
for files in (25, 50, 100, 200, 400):
    objs = repro.generate(os.path.join(work, f"objs{files}"), files, 150)
    out = os.path.join(work, f"out{files}.exe")
    args = repro.link_args(objs, out)
    cpu1 = None
    for threads in ("1", "32"):
        runs = [link(args, [f"--threads={threads}"]) for _ in range(8)][1:]
        w, u, s = (statistics.median(r[i] for r in runs) for i in range(3))
        if threads == "1":
            cpu1 = u + s
        print(f"| {files} | {os.path.getsize(out) / 2**20:.1f} | {threads} | {w:.0f} | {u:.0f} | {s:.0f} | {u + s - cpu1:+.0f} |", flush=True)

for threads in ("32", "1"):
    print(f"\n### mold --perf, --threads={threads}, 400 objects")
    r = subprocess.run([mold, "--no-fork", "--perf", f"--threads={threads}", *args], capture_output=True, text=True, check=True)
    print(r.stdout + r.stderr)
