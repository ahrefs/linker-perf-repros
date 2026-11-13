#!/usr/bin/env python3
"""Run K copies of rayon-idle at once for each --threads value and print the
median time until the last copy finished and the CPU per copy.

usage: run.py [--cpus 0-31] [--jobs 1,8,32] [--threads 1,4,32] [--runs 7] [-- rayon-idle args]
"""
import argparse, os, statistics, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(HERE, "target", "release", "rayon-idle")


def batch(cmd, k):
    start = time.monotonic()
    quiet = [(os.POSIX_SPAWN_OPEN, 1, os.devnull, os.O_WRONLY, 0)]
    pids = {os.posix_spawn(cmd[0], cmd, os.environ, file_actions=quiet) for _ in range(k)}
    cpu = 0.0
    while pids:
        pid, status, ru = os.wait4(-1, 0)
        if status != 0:
            sys.exit(f"{cmd} exited with status {status}")
        pids.discard(pid)
        cpu += ru.ru_utime + ru.ru_stime
    return time.monotonic() - start, cpu / k


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--cpus", help="pin to these CPUs, e.g. 0-31")
    p.add_argument("--jobs", default="1,8,32", help="comma list of concurrent copies")
    p.add_argument("--threads", default="1,4,32", help="comma list of Rayon pool sizes")
    p.add_argument("--runs", type=int, default=7, help="batches per cell (one extra warm-up batch is dropped)")
    p.add_argument("rest", nargs="*", help="extra rayon-idle arguments, after --")
    o = p.parse_args()
    if o.cpus:
        cpus = set()
        for part in o.cpus.split(","):
            lo, _, hi = part.partition("-")
            cpus.update(range(int(lo), int(hi or lo) + 1))
        os.sched_setaffinity(0, cpus)
    print(f"kernel {os.uname().release}, nproc {os.cpu_count()}, affinity {len(os.sched_getaffinity(0))} CPUs, args {o.rest}")
    print(os.popen(f"{BIN} --threads 1 {' '.join(o.rest)}").read().strip())
    print("\n| K | --threads | done ms | CPU ms / process |")
    print("|---|---|---|---|")
    for k in map(int, o.jobs.split(",")):
        for t in o.threads.split(","):
            cmd = [BIN, "--threads", t, *o.rest]
            res = [batch(cmd, k) for _ in range(o.runs + 1)][1:]
            done, cpu = (statistics.median(r[i] for r in res) for i in range(2))
            print(f"| {k} | {t} | {done * 1e3:.1f} | {cpu * 1e3:.1f} |", flush=True)


if __name__ == "__main__":
    main()
