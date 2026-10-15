#!/usr/bin/env python3
"""Offline checks for a Printune session G-code.

Simulates the moves (G90 XY/Z, M83 E) and reports:
  * moves outside the bed, extrusion widths per object family
  * nozzle crashes: a travel passing through something already printed
  * extrusions laid onto another object (an uncleared plate)
  * toolhead box: printed material taller than the nozzle tip (and taller than --low, default 1.2 mm)
    inside the toolhead's box around the nozzle, for every move
  * gantry: the X gantry spans the bed at the nozzle's Y and reaches as far in Y as the toolhead box;
    a move whose Y band holds printed material more than the gantry height above the nozzle tip
    runs the gantry into it
  * filament and a rough time estimate
Writes paths.npz (segments) for the render scripts.

Usage (the geometry is required, as for printune_gen.py; use your own measurements):
  check_gcode.py FILE --box=-X,+X,-Y,+Y --gantry=MM [--bed=X0,Y0,X1,Y1 | --bed=W,D]
                 [--skip=cool,cone,bridge,mvs] [--low=1.2]
  --skip simulates pressing "Skip" at those plate pauses.
"""
import math
import re
import sys
from collections import defaultdict

import numpy as np

RES = 0.25  # heightmap resolution, mm


def parse(path, h1=0.25, h=0.2, fil_d=1.75, bed=(0, 0, 375, 388), skip=()):
    """skip: WHAT labels (cool, cone, bridge, mvs) whose "Skip" button is pressed at the plate pause,
    simulating the macros: those objects are excluded and the plate stays uncleared."""
    fa = math.pi * (fil_d / 2) ** 2
    x = y = z = 0.0
    obj = None
    plate = 1
    excl = set()
    segs = []      # x0,y0,x1,y1,z,w,e,kind(obj), is_travel
    out_of_bed = []
    rx = re.compile(r"([XYZEF])(-?[\d.]+)")
    for ln in open(path, encoding="utf-8"):
        t = ln.split(";", 1)[0].strip()
        if not t:
            continue
        if t.startswith("_PRINTUNE_CP STEP=plate"):
            n = int(re.search(r"N=(\d+)", t).group(1))
            names = set(re.search(r"ALL=(\S+)", t).group(1).split(","))
            what = re.search(r"WHAT=(\w+)", t)
            if n > plate and not names <= excl:
                # the macro pauses here (later checkpoints for a cleared plate don't)
                if what and what.group(1) in skip:
                    # "Skip": objects excluded, plate not cleared
                    excl |= set(re.search(r"SKIP=(\S+)", t).group(1).split(","))
                else:
                    plate = n
                    segs.append((x, y, x, y, z, 0.0, 0.0, "PLATE", 2))
            continue
        if t.startswith("EXCLUDE_OBJECT NAME="):
            excl.add(t.split("NAME=")[1].strip())
            continue
        if t.startswith("EXCLUDE_OBJECT_START"):
            obj = t.split("NAME=")[1]
            continue
        if t.startswith("EXCLUDE_OBJECT_END"):
            obj = None
            continue
        if not (t.startswith("G1 ") or t.startswith("G0 ")):
            continue
        if obj in excl:
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


def main(path, box, gantry, h1=0.25, h=0.2, bed=(0, 0, 375, 388), skip=(), low=1.2):
    xn, xp, yn, yp = box
    segs, oob = parse(path, h1, h, bed=bed, skip=skip)
    if skip:
        print("simulated: Skip pressed at the plate pause for %s" % ",".join(skip))
    print("toolhead box %g,%g,%g,%g (-X,+X,-Y,+Y), gantry height %g mm, bed %s"
          % (xn, xp, yn, yp, gantry, ",".join("%g" % v for v in bed)))
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
    O = np.zeros((nx, ny), np.int32)    # which object printed the top of each cell
    Hy = np.zeros(ny, np.float32)        # tallest material in each row of Y (what the gantry meets)
    # 1 mm max-pooled copy of H for the toolhead box (cells grown by one so nothing is missed)
    C = int(round(1.0 / RES))
    Hc = np.zeros((nx // C, ny // C), np.float32)
    gantry_n, gworst = 0, (-99.0, None)
    box_n, bworst = 0, (-99.0, None)
    oid = {}
    onto = []   # extrusions laid onto another object's material
    crash = []
    for n, (x0, y0, x1, y1, z, w, e, ob, trav) in enumerate(segs):
        if trav == 2:
            H[:] = 0.0      # plate cleared
            O[:] = 0
            Hy[:] = 0.0
            Hc[:] = 0.0
            continue
        L = math.hypot(x1 - x0, y1 - y0)
        steps = max(1, int(L / 2.0))
        # gantry: the tallest material in the Y band the move covers, above the nozzle tip
        j0 = max(0, int((min(y0, y1) + yn) / RES))
        j1 = min(ny, int((max(y0, y1) + yp) / RES) + 1)
        over = float(Hy[j0:j1].max()) - z if j1 > j0 else -99.0
        if over > gworst[0]:
            gworst = (over, (round(x1, 1), round(y1, 1), round(z, 2), ob))
        if over > gantry:
            gantry_n += 1
        # toolhead box: sample along the move (endpoints + every quarter; every 3rd extrusion)
        if n % 3 == 0 or trav:
            hit = False
            for k in range(0, steps + 1, max(1, steps // 4)):
                px = x0 + (x1 - x0) * k / steps
                py = y0 + (y1 - y0) * k / steps
                a0 = max(0, int((px + xn) // 1.0) - 1)
                a1 = min(Hc.shape[0], int((px + xp) // 1.0) + 2)
                b0 = max(0, int((py + yn) // 1.0) - 1)
                b1 = min(Hc.shape[1], int((py + yp) // 1.0) + 2)
                if a1 <= a0 or b1 <= b0:
                    continue
                top = float(Hc[a0:a1, b0:b1].max())
                hmax = top - z
                if top > low:
                    if hmax > bworst[0]:
                        bworst = (hmax, (round(px, 1), round(py, 1), round(z, 2), ob))
                    hit = hit or hmax > 0.3
            if hit:
                box_n += 1
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
        # extruding onto another object (e.g. an uncleared plate)
        me = oid.setdefault(ob, len(oid) + 1)
        for k in range(steps + 1):
            px = x0 + (x1 - x0) * k / steps
            py = y0 + (y1 - y0) * k / steps
            ci, cj = int(px / RES), int(py / RES)
            if H[ci, cj] > z - 0.05 and O[ci, cj] != me:
                inv = {v: kk for kk, v in oid.items()}
                onto.append((round(px, 1), round(py, 1), z, ob, inv.get(int(O[ci, cj]))))
                break
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
        O[ia:ib, ja:jb][m] = me
        Hy[ja:jb] = np.maximum(Hy[ja:jb], z)
        if m.any():
            # refresh the 1 mm cells this extrusion touched
            ca, cb = ia // C, min(Hc.shape[0], (ib - 1) // C + 1)
            da, db = ja // C, min(Hc.shape[1], (jb - 1) // C + 1)
            blk = H[ca * C:cb * C, da * C:db * C]
            Hc[ca:cb, da:db] = blk.reshape(cb - ca, C, db - da, C).max(axis=(1, 3))
    print("extrusions onto another object: %d" % len(onto))
    for c in onto[:10]:
        print("  ", c)
    print("gantry over prints taller than the gantry height (%g mm): %d moves; worst %+.2f mm above the nozzle at %s"
          % (gantry, gantry_n, gworst[0], gworst[1]))
    print("toolhead box over finished prints (taller than %g mm and > 0.3 mm above the nozzle tip): %d moves; "
          "worst %s" % (low, box_n, ("%+.2f mm above the nozzle at %s" % bworst) if bworst[1] else "-"))
    print("nozzle crashes on travels: %d" % len(crash))
    for c in crash[:10]:
        print("  ", c)
    segs = [s for s in segs if s[8] != 2]
    np.savez_compressed(path + ".paths.npz",
                        segs=np.array([s[:7] for s in segs], np.float32),
                        trav=np.array([s[8] for s in segs], np.int8),
                        obj=np.array([s[7] for s in segs]))
    np.save(path + ".height.npy", H)
    e_tot = sum(s[6] for s in segs)
    print("filament %.2f m" % (e_tot / 1000))


def _floats(text, n, what):
    v = [float(t) for t in text.split(",")]
    if len(v) != n:
        raise SystemExit("check_gcode: %s needs %d numbers" % (what, n))
    return v


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    kw = dict(a[2:].split("=", 1) for a in sys.argv[1:] if a.startswith("--") and "=" in a)
    if len(args) != 1 or "box" not in kw or "gantry" not in kw:
        raise SystemExit(__doc__ + "\ncheck_gcode: give the file, --box and --gantry (the old radius "
                         "and rod arguments are gone; no geometry is assumed)")
    xn, xp, yn, yp = (abs(v) for v in _floats(kw["box"], 4, "--box"))
    bed = (0, 0, 375, 388)
    if kw.get("bed"):
        b = [float(t) for t in kw["bed"].split(",")]
        bed = tuple(b) if len(b) == 4 else (0, 0) + tuple(_floats(kw["bed"], 2, "--bed"))
    skip = tuple(kw["skip"].split(",")) if kw.get("skip") else ()
    main(args[0], (-xn, xp, -yn, yp), float(kw["gantry"]), bed=bed, skip=skip, low=float(kw.get("low", 1.2)))
