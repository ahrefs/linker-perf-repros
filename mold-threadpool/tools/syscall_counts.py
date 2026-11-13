#!/usr/bin/env python3
"""perf stat of one mold link (--no-fork, pinned 0-31) per input size and thread count, plus a
kernel-side perf profile of the 32-thread 400-object link.
usage: syscall_counts.py REPRO_DIR WORK_DIR MOLD"""
import os, resource, subprocess, sys

repro_dir, work, mold = sys.argv[1:]
sys.path.insert(0, repro_dir)
import repro  # noqa: E402

EVENTS = ["sched:sched_switch", "sched:sched_migrate_task", "exceptions:page_fault_user", "exceptions:page_fault_kernel",
          "syscalls:sys_enter_sched_yield", "syscalls:sys_enter_futex", "syscalls:sys_enter_clone3",
          "syscalls:sys_enter_mmap", "syscalls:sys_enter_madvise", "syscalls:sys_enter_munmap"]


def vm(key):
    with open("/proc/vmstat") as f:
        return next(int(l.split()[1]) for l in f if l.startswith(key + " "))


print("| objects | threads | user ms | sys ms | " + " | ".join(e.split(":")[-1].replace("sys_enter_", "") for e in EVENTS) + " | thp faults |")
print("|---|---|---|---|" + "---|" * (len(EVENTS) + 1))
args = None
for files in (25, 400):
    objs = repro.generate(os.path.join(work, f"objs{files}"), files, 150)
    args = repro.link_args(objs, os.path.join(work, f"out{files}.exe"))
    for threads in ("1", "4", "32"):
        cmd = ["taskset", "-c", "0-31", mold, "--no-fork", f"--threads={threads}", *args]
        subprocess.run(cmd, check=True)  # warm-up
        thp = vm("thp_fault_alloc")
        ru0 = resource.getrusage(resource.RUSAGE_CHILDREN)
        for _ in range(5):
            subprocess.run(cmd, check=True)
        ru1 = resource.getrusage(resource.RUSAGE_CHILDREN)
        user, sys_ = ((ru1.ru_utime - ru0.ru_utime) * 200, (ru1.ru_stime - ru0.ru_stime) * 200)
        r = subprocess.run(["sudo", "-n", "perf", "stat", "-r", "5", "-x", ",", "-e", ",".join(EVENTS), "--", *cmd],
                           capture_output=True, text=True, check=True)
        thp = (vm("thp_fault_alloc") - thp) / 10
        vals = {}
        for line in r.stderr.splitlines():
            f = line.split(",")
            if len(f) > 3 and f[2] in EVENTS:
                vals[f[2]] = float(f[0]) if f[0] not in ("<not counted>", "<not supported>") else float("nan")
        cells = [f"{user:.0f}", f"{sys_:.0f}"] + [f"{vals[e]:.0f}" for e in EVENTS]
        print(f"| {files} | {threads} | " + " | ".join(cells) + f" | {thp:.0f} |", flush=True)

print("\n### kernel symbols, perf record -g, 32 threads, 400 objects, 20 links")
data = os.path.join(work, "perf.data")
cmd = ["taskset", "-c", "0-31", "sh", "-c", f"for i in $(seq 20); do {mold} --no-fork --threads=32 {' '.join(args)}; done"]
subprocess.run(["sudo", "-n", "perf", "record", "-q", "-g", "-o", data, "--", *cmd], check=True, stdout=subprocess.DEVNULL)
r = subprocess.run(["sudo", "-n", "perf", "report", "-i", data, "--no-children", "--sort", "dso,sym", "--stdio", "-g", "none",
                    "--percent-limit", "1"], capture_output=True, text=True)
print("\n".join(l for l in r.stdout.splitlines() if l and not l.startswith("#")))
r = subprocess.run(["sudo", "-n", "perf", "report", "-i", data, "--no-children", "--sort", "sym", "--stdio", "-g", "caller,0.5,callee,function,percent",
                    "--percent-limit", "5", "--dsos", "[kernel.kallsyms]"], capture_output=True, text=True)
print("\n".join(l for l in r.stdout.splitlines() if l.strip() and not l.startswith("#"))[:6000])
subprocess.run(["sudo", "-n", "rm", "-f", data])
