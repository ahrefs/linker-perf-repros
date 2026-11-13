#!/usr/bin/env python3
"""Attribute mold's CPU at 32 threads, using a symbolized build with frame pointers.
1. CPU per link, release vs symbolized build, 25 and 400 objects, 1 and 32 threads (check the build is representative).
2. perf record -g of 20 links at 32 threads (pinned 0-31), CPU split by component.
usage: cpu_split.py REPRO_DIR WORK_DIR RELEASE_MOLD SYM_MOLD
SYM_MOLD must be named `mold` (perf filters samples by that thread name)."""
import collections, os, re, resource, statistics, subprocess, sys, time

repro_dir, work, rel, sym = sys.argv[1:]
sys.path.insert(0, repro_dir)
import repro  # noqa: E402

PIN = ["taskset", "-c", "0-31"]


def cpu(cmd, n=7):
    out = []
    for _ in range(n + 1):
        a = resource.getrusage(resource.RUSAGE_CHILDREN)
        t = time.monotonic()
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)
        w = time.monotonic() - t
        b = resource.getrusage(resource.RUSAGE_CHILDREN)
        out.append((w * 1e3, (b.ru_utime - a.ru_utime) * 1e3, (b.ru_stime - a.ru_stime) * 1e3))
    out = out[1:]
    return tuple(statistics.median(x[i] for x in out) for i in range(3))


ARGS = {}
print("| objects | build | threads | wall ms | user ms | sys ms |")
print("|---|---|---|---|---|---|")
for files in (25, 400):
    objs = repro.generate(os.path.join(work, f"objs{files}"), files, 150)
    ARGS[files] = repro.link_args(objs, os.path.join(work, f"out{files}.exe"))
    for name, mold in (("release", rel), ("symbolized", sym)):
        for threads in ("1", "32"):
            w, u, s = cpu([*PIN, mold, "--no-fork", f"--threads={threads}", *ARGS[files]])
            print(f"| {files} | {name} | {threads} | {w:.0f} | {u:.0f} | {s:.0f} |", flush=True)

# Component of a sample = first frame (leaf to root) that names a known component;
# kernel frames count as "kernel: <the user-space component that called in>".
RULES = [
    ("rayon idle/steal", re.compile(r"rayon_core::(sleep|registry::WorkerThread::(wait_until|take_local_job|steal|find_work))|crossbeam_deque|crossbeam_epoch|crossbeam_utils::backoff")),
    ("rayon other", re.compile(r"rayon_core|rayon::")),
    ("mimalloc", re.compile(r"\b_?mi_|mimalloc")),
    ("mold", re.compile(r"\bmold|::elf::|::output|::input")),
]


def component(stack):
    for f in stack:
        for name, rx in RULES:
            if rx.search(f):
                return name
    return "other user" if stack else "unknown"


for files in (25, 400):
    data = os.path.join(work, f"perf{files}.data")
    loop = f"for i in $(seq 20); do {sym} --no-fork --threads=32 {' '.join(ARGS[files])} >/dev/null; done"
    subprocess.run(["sudo", "-n", "perf", "record", "-q", "-g", "-F", "4999", "-o", data, "--", *PIN, "sh", "-c", loop],
                   check=True, stdout=subprocess.DEVNULL)
    script = subprocess.run(["sudo", "-n", "perf", "script", "-i", data, "-F", "comm,ip,sym,dso", "--comm", "mold"],
                            capture_output=True, text=True).stdout
    by_comp, by_kernel_leaf, total = collections.Counter(), collections.Counter(), 0
    for block in script.split("\n\n"):
        lines = [l.strip() for l in block.strip().splitlines()]
        if len(lines) < 2:
            continue
        frames = [l.split(None, 1)[1] if " " in l else l for l in lines[1:]]
        kernel = [f for f in frames if "kernel.kallsyms" in f]
        user = [f for f in frames if f not in kernel]
        comp = component(user)
        if kernel:
            comp = "kernel via " + comp
            leaf = re.sub(r"\s*\(.*\)$", "", re.sub(r"\+0x[0-9a-f]+", "", frames[0]))
            by_kernel_leaf[(comp, leaf)] += 1
        by_comp[comp] += 1
        total += 1
    print(f"\n### {files} objects, 32 threads, 20 links: share of {total} samples by component")
    for comp, n in by_comp.most_common():
        print(f"  {100 * n / total:5.1f}%  {comp}")
    print("  top kernel leaf functions:")
    for (comp, leaf), n in by_kernel_leaf.most_common(12):
        print(f"  {100 * n / total:5.1f}%  {leaf}  [{comp}]")
    r = subprocess.run(["sudo", "-n", "perf", "report", "-i", data, "--comm", "mold", "--no-children", "--sort", "sym", "--stdio", "-g", "none",
                        "--percent-limit", "1.5"], capture_output=True, text=True)
    print("  top symbols (self):")
    print("\n".join("  " + l for l in r.stdout.splitlines() if l.strip() and not l.startswith("#")))
    subprocess.run(["sudo", "-n", "rm", "-f", data])
