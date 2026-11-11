#!/usr/bin/env python3
"""Histogram of page-cache folio sizes backing a file (needs root).

Maps FILE read-only, touches every page (a read of a cached page does not
change its folio), then reads PFNs from /proc/self/pagemap and walks
/proc/kpageflags to size each folio.

usage: sudo python3 folios.py FILE [FILE ...]
"""
import collections, mmap, os, struct, sys

KPF_HEAD, KPF_TAIL = 1 << 15, 1 << 16
kf = open("/proc/kpageflags", "rb")


def flags(pfn):
    kf.seek(pfn * 8)
    return struct.unpack("Q", kf.read(8))[0]


def folio_pages(pfn):
    while flags(pfn) & KPF_TAIL:
        pfn -= 1
    head = pfn
    if not flags(head) & KPF_HEAD:
        return head, 1
    n = 1
    while flags(head + n) & KPF_TAIL:
        n += 1
    return head, n


for path in sys.argv[1:]:
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        m = mmap.mmap(f.fileno(), size, prot=mmap.PROT_READ)
    for off in range(0, size, 4096):
        m[off]
    real = os.path.realpath(path)
    lo = None
    for line in open("/proc/self/maps"):
        fs = line.split()
        if len(fs) >= 6 and fs[5] == real and int(fs[2], 16) == 0:
            lo = int(fs[0].split("-")[0], 16)
    pm = open("/proc/self/pagemap", "rb")
    hist = collections.Counter()
    seen = set()
    for off in range(0, size, 4096):
        pm.seek((lo + off) // 4096 * 8)
        e = struct.unpack("Q", pm.read(8))[0]
        if not e >> 63:
            hist["not present"] += 1
            continue
        head, n = folio_pages(e & ((1 << 55) - 1))
        if head not in seen:
            seen.add(head)
            hist[n * 4] += n
    total = sum(hist.values())
    desc = ", ".join(f"{k} KiB: {v * 100 / total:.0f}%" if k != "not present" else f"not present: {v}"
                     for k, v in sorted(hist.items(), key=lambda kv: (isinstance(kv[0], str), kv[0])))
    print(f"{path}: {total} pages; share of pages by folio size: {desc}")
    m.close()
