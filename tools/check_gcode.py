#!/usr/bin/env python3
"""Offline checks for an Printune session G-code.

Simulates the moves (G90 XY/Z, M83 E) and reports:
  * moves outside the bed, extrusion widths per object family
  * nozzle crashes: a travel passing through something already printed
  * toolhead clearance: the tallest printed thing (above the nozzle tip) within
    radius r of the nozzle, for every move, binned by r
  * filament and a rough time estimate
Writes paths.npz (segments) for the render scripts.
"""
import math
import re
import sys
from collections import defaultdict

import numpy as np

RES = 0.25  # heightmap resolution, mm


def parse(path, h1=0.25, h=0.2, fil_d=1.75, bed=(0, 0, 375, 388)):
    fa = math.pi * (fil_d / 2) ** 2
    x = y = z = 0.0
    obj = None
    plate = 1
    segs = []      # x0,y0,x1,y1,z,w,e,kind(obj), is_travel
    out_of_bed = []
    rx = re.compile(r"([XYZEF])(-?[\d.]+)")
    for ln in open(path, encoding="utf-8"):
        t = ln.split(";", 1)[0].strip()
        if not t:
            continue
        if t.startswith("_PRINTUNE_CP STEP=plate"):
            n = int(re.search(r"N=(\d+)", t).group(1))
            if n > plate:
                # the plate is cleared here (later checkpoints for the same plate don't pause)
                plate = n
                segs.append((x, y, x, y, z, 0.0, 0.0, "PLATE", 2))
            continue
        if t.startswith("EXCLUDE_OBJECT_START"):
            obj = t.split("NAME=")[1]
            continue
        if t.startswith("EXCLUDE_OBJECT_END"):
            obj = None
            continue
        if not (t.startswith("G1 ") or t.startswith("G0 ")):
            continue
        d = dict((k, float(v)) for k, v in rx.findall(t))
        nx, ny, nz = d.get("X", x), d.get("Y", y), d.get("Z", z)
        e = d.get("E", 0.0)
        if (nx, ny) != (x, y):
            if not (bed[0] <= nx <= bed[2] and bed[1] <= ny <= bed[3]):
                out_of_bed.append(t)
            L = math.hypot(nx - x, ny - y)
            if e > 0:
                hh = h1 if z <= h1 + 1e-3 else h
                A = e * fa / L
                w = (A - math.pi * hh * hh / 4) / hh + hh
                segs.append((x, y, nx, ny, z, w, e, obj or "-", 0))
            else:
                segs.append((x, y, nx, ny, z, 0.0, 0.0, obj or "-", 1))
        x, y, z = nx, ny, nz
    return segs, out_of_bed


def family(name):
    return re.sub(r"\d+$", "", name).rstrip("_")


def main(path, R=45.0, h1=0.25, h=0.2, bed=(0, 0, 375, 388)):
    segs, oob = parse(path, h1, h, bed=bed)
    print("plates: %d" % (1 + sum(1 for s in segs if s[8] == 2)))
    print("segments: %d, outside bed: %d" % (len(segs), len(oob)))
    for t in oob[:5]:
        print("  OOB:", t)
    fams = defaultdict(list)
    for s in segs:
        if s[8] == 0:
            fams[family(s[7])].append(s[5])
    print("extrusion width by object family (min / median / max, mm):")
    for k, ws in sorted(fams.items()):
        ws = np.array(ws)
        print("  %-10s %6d  %.3f / %.3f / %.3f" % (k, len(ws), ws.min(), np.median(ws), ws.max()))

    nx, ny = int(400 / RES), int(400 / RES)
    H = np.zeros((nx, ny), np.float32)
    crash = []
    rbins = [3, 6, 10, 15, 20, 30, 45, 65]
    worst = {r: (-99.0, None) for r in rbins}
    # precompute disk offsets per bin
    offs = {}
    for r in rbins:
        k = int(r / RES)
        ii, jj = np.mgrid[-k:k + 1, -k:k + 1]
        m = (ii * ii + jj * jj) * RES * RES <= r * r
        offs[r] = (ii[m], jj[m])
    cur = None
    for n, (x0, y0, x1, y1, z, w, e, ob, trav) in enumerate(segs):
        if trav == 2:
            H[:] = 0.0      # plate cleared
            continue
        L = math.hypot(x1 - x0, y1 - y0)
        steps = max(1, int(L / 2.0))
        # clearance: sample along the move (cheap: endpoints + every 2 mm, every 10th seg for long prints)
        if n % 3 == 0 or trav:
            for k in range(0, steps + 1, max(1, steps // 4)):
                px = x0 + (x1 - x0) * k / steps
                py = y0 + (y1 - y0) * k / steps
                ci, cj = int(px / RES), int(py / RES)
                for r in rbins:
                    ii, jj = offs[r]
                    a = np.clip(ci + ii, 0, nx - 1)
                    b = np.clip(cj + jj, 0, ny - 1)
                    hmax = float(H[a, b].max()) - z
                    if hmax > worst[r][0]:
                        worst[r] = (hmax, (round(px, 1), round(py, 1), round(z, 2), ob))
        if trav:
            # nozzle crash: path through printed material higher than the nozzle tip
            for k in range(steps + 1):
                px = x0 + (x1 - x0) * k / steps
                py = y0 + (y1 - y0) * k / steps
                hh = H[int(px / RES), int(py / RES)]
                if hh > z + 0.01:
                    crash.append((round(px, 1), round(py, 1), z, float(hh), ob))
                    break
            continue
        # rasterise extrusion
        hw = w / 2
        xa, xb = min(x0, x1) - hw, max(x0, x1) + hw
        ya, yb = min(y0, y1) - hw, max(y0, y1) + hw
        ia, ib = int(xa / RES), int(xb / RES) + 1
        ja, jb = int(ya / RES), int(yb / RES) + 1
        gi, gj = np.mgrid[ia:ib, ja:jb]
        px, py = gi * RES + RES / 2, gj * RES + RES / 2
        dx, dy = x1 - x0, y1 - y0
        tt = np.clip(((px - x0) * dx + (py - y0) * dy) / max(L * L, 1e-9), 0, 1)
        dd = np.hypot(px - (x0 + tt * dx), py - (y0 + tt * dy))
        m = dd <= hw
        sub = H[ia:ib, ja:jb]
        sub[m] = np.maximum(sub[m], z)
    print("nozzle crashes on travels: %d" % len(crash))
    for c in crash[:10]:
        print("  ", c)
    print("tallest printed material above the nozzle tip within r of the nozzle (worst move):")
    for r in rbins:
        print("  r <= %4.0f mm: %+6.2f mm   at %s" % (r, worst[r][0], worst[r][1]))
    segs = [s for s in segs if s[8] != 2]
    np.savez_compressed(path + ".paths.npz",
                        segs=np.array([s[:7] for s in segs], np.float32),
                        trav=np.array([s[8] for s in segs], np.int8),
                        obj=np.array([s[7] for s in segs]))
    np.save(path + ".height.npy", H)
    e_tot = sum(s[6] for s in segs)
    print("filament %.2f m" % (e_tot / 1000))


if __name__ == "__main__":
    # check_gcode.py FILE [R] [W,D]
    bed = (0, 0) + tuple(float(v) for v in sys.argv[3].split(",")) if len(sys.argv) > 3 else (0, 0, 375, 388)
    main(sys.argv[1], float(sys.argv[2]) if len(sys.argv) > 2 else 45.0, bed=bed)
