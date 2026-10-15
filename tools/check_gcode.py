#!/usr/bin/env python3
"""Offline checks for a Printune session G-code.

Simulates the moves (G90 XY/Z, M83 E) and reports:
  * moves outside the bed, extrusion widths per object family
  * nozzle crashes: a travel passing through something already printed
  * extrusions laid onto another object (an uncleared plate)
  * toolhead box: printed material inside the toolhead's box around the nozzle that reaches the
    toolhead's underside (more than the nozzle protrusion less printune_gen.LOW_MARGIN above the
    nozzle tip), checked along every move, travels included, at least every 1 mm
  * gantry: the X gantry spans the bed at the nozzle's Y and reaches as far in Y as the toolhead box;
    a move whose Y band holds printed material more than the gantry height above the nozzle tip
    runs the gantry into it
  * filament and a rough time estimate
Writes paths.npz (segments, and the heightmap's origin) and height.npy for the render scripts.

Usage (the geometry is required, as for printune_gen.py; use your own measurements):
  check_gcode.py FILE [--box=-X,+X,-Y,+Y] [--gantry=MM] [--protrusion=MM] [--bed=X0,Y0,X1,Y1 | --bed=W,D]
                 [--skip=cool,cone,bridge,bridge_speed,bridge_flow,mvs]
  A value not given is read from the session's header (the printune_geometry line, written by
  printune_gen.py v9 and later); without it the check refuses. The header's bed is the area the
  session was laid out in (the machine setup's bed area cut to the printable area).
  --skip simulates pressing "Skip" at those plate pauses.
"""
import math
import os
import re
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from printune_gen import LOW_MARGIN  # noqa: E402

RES = 0.25  # heightmap resolution, mm
CELL = 1.0  # max-pooled grid for the toolhead box, mm


def read_header(path):
    """The printune_geometry header line as a dict, or {}."""
    with open(path, encoding="utf-8", errors="replace") as f:
        for n, ln in enumerate(f):
            if ln.startswith("; printune_geometry:"):
                return dict(kv.split("=", 1) for kv in ln.split(":", 1)[1].split() if "=" in kv)
            if n > 400:
                break
    return {}


def parse(path, bed, h1=0.25, h=0.2, fil_d=1.75, skip=()):
    """skip: WHAT labels (cool, cone, bridge, bridge_speed, bridge_flow, mvs) whose "Skip" button is pressed at the plate pause,
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
            if not (bed[0] - 1e-6 <= nx <= bed[2] + 1e-6 and bed[1] - 1e-6 <= ny <= bed[3] + 1e-6):
                out_of_bed.append(t)
            L = math.hypot(nx - x, ny - y)
            if e > 0:
                hh = h1 if z <= h1 + 1e-3 else h
                A = e * fa / L
                w = (A - math.pi * hh * hh / 4) / hh + hh
                segs.append((x, y, nx, ny, z, w, e, obj or "-", 0))
            else:
                segs.append((x, y, nx, ny, z, 0.0, 0.0, obj or "-", 1))
        elif nz != z:
            # a Z-only move: keep it, the box check needs the nozzle's height where it is
            segs.append((x, y, x, y, nz, 0.0, 0.0, obj or "-", 1))
        x, y, z = nx, ny, nz
    return segs, out_of_bed


def family(name):
    return re.sub(r"\d+$", "", name).rstrip("_")


def _window_max(A, lo, hi, axis):
    """out[i] = max(A[i + lo .. i + hi]) along axis (outside the grid counts as 0)."""
    B = np.moveaxis(A, axis, 0)
    out = np.zeros_like(B)
    n = B.shape[0]
    for d in range(lo, hi + 1):
        if abs(d) >= n:
            continue
        if d >= 0:
            np.maximum(out[:n - d], B[d:], out=out[:n - d])
        else:
            np.maximum(out[-d:], B[:n + d], out=out[-d:])
    return np.moveaxis(out, 0, axis)


def main(path, box, gantry, protrusion, bed, h1=0.25, h=0.2, skip=()):
    xn, xp, yn, yp = box
    segs, oob = parse(path, bed, h1, h, skip=skip)
    if skip:
        print("simulated: Skip pressed at the plate pause for %s" % ",".join(skip))
    thr = max(0.0, protrusion - LOW_MARGIN)
    print("toolhead box %g,%g,%g,%g (-X,+X,-Y,+Y), gantry height %g mm, nozzle protrusion %g mm, bed %s"
          % (xn, xp, yn, yp, gantry, protrusion, ",".join("%g" % v for v in bed)))
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

    # grids cover the bed plus a margin (negative coordinates work), in whole 1 mm cells
    pad = 5.0
    ox, oy = math.floor(bed[0] - pad), math.floor(bed[1] - pad)
    C = int(round(CELL / RES))
    ncx = int(math.ceil((bed[2] + pad - ox) / CELL)) + 1
    ncy = int(math.ceil((bed[3] + pad - oy) / CELL)) + 1
    nx, ny = ncx * C, ncy * C
    H = np.zeros((nx, ny), np.float32)
    O = np.zeros((nx, ny), np.int32)    # which object printed the top of each cell
    Hy = np.zeros(ny, np.float32)        # tallest material in each row of Y (what the gantry meets)
    Hc = np.zeros((ncx, ncy), np.float32)  # 1 mm max-pooled copy of H
    # BM: for each 1 mm cell of the nozzle, the tallest material anywhere in the toolhead box around
    # it (the box grown by one cell each way, so nothing is missed between samples). Rebuilt only
    # when material laid since the last rebuild could matter: material at the nozzle's own height
    # never reaches the toolhead's underside.
    lo_x, hi_x = int(math.floor(xn / CELL)) - 1, int(math.ceil(xp / CELL)) + 1
    lo_y, hi_y = int(math.floor(yn / CELL)) - 1, int(math.ceil(yp / CELL)) + 1
    BM = np.zeros_like(Hc)
    bm_top = 0.0                        # BM.max()
    dirty_z = -1.0                      # tallest material laid since BM was built
    rebuilds = 0
    ds = min(1.0, min(abs(v) for v in box) / 2.0)   # box samples along a move, mm

    def gi(px):
        return np.clip(((px - ox) / RES).astype(np.int64), 0, nx - 1)

    def gj(py):
        return np.clip(((py - oy) / RES).astype(np.int64), 0, ny - 1)

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
            BM[:] = 0.0
            bm_top, dirty_z = 0.0, -1.0
            continue
        L = math.hypot(x1 - x0, y1 - y0)
        # gantry: the tallest material in the Y band the move covers, above the nozzle tip
        j0 = max(0, int((min(y0, y1) + yn - oy) / RES))
        j1 = min(ny, int((max(y0, y1) + yp - oy) / RES) + 1)
        over = float(Hy[j0:j1].max()) - z if j1 > j0 else -99.0
        if over > gworst[0]:
            gworst = (over, (round(x1, 1), round(y1, 1), round(z, 2), ob))
        if over > gantry:
            gantry_n += 1
        # toolhead box, along the whole move
        if dirty_z - z > thr:
            BM = _window_max(_window_max(Hc, lo_x, hi_x, 0), lo_y, hi_y, 1)
            bm_top, dirty_z = float(BM.max()), -1.0
            rebuilds += 1
        if bm_top - z > thr:
            k = max(1, int(math.ceil(L / ds)))
            t = np.linspace(0.0, 1.0, k + 1)
            px, py = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
            ci = np.clip(((px - ox) / CELL).astype(np.int64), 0, ncx - 1)
            cj = np.clip(((py - oy) / CELL).astype(np.int64), 0, ncy - 1)
            tops = BM[ci, cj]
            m = int(np.argmax(tops))
            hmax = float(tops[m]) - z
            if hmax > thr:
                box_n += 1
            if hmax > bworst[0]:
                bworst = (hmax, (round(float(px[m]), 1), round(float(py[m]), 1), round(z, 2), ob))
        if trav:
            # nozzle crash: path through printed material higher than the nozzle tip
            if L > 0:
                k = max(1, int(math.ceil(L / RES)))
                t = np.linspace(0.0, 1.0, k + 1)
                px, py = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
                hh = H[gi(px), gj(py)]
                bad = np.nonzero(hh > z + 0.01)[0]
                if len(bad):
                    b = bad[0]
                    crash.append((round(float(px[b]), 1), round(float(py[b]), 1), z, float(hh[b]), ob))
            continue
        # extruding onto another object (e.g. an uncleared plate)
        me = oid.setdefault(ob, len(oid) + 1)
        steps = max(1, int(L / 2.0))
        for k in range(steps + 1):
            px = x0 + (x1 - x0) * k / steps
            py = y0 + (y1 - y0) * k / steps
            ci, cj = min(nx - 1, max(0, int((px - ox) / RES))), min(ny - 1, max(0, int((py - oy) / RES)))
            if H[ci, cj] > z - 0.05 and O[ci, cj] != me:
                inv = {v: kk for kk, v in oid.items()}
                onto.append((round(px, 1), round(py, 1), z, ob, inv.get(int(O[ci, cj]))))
                break
        # rasterise extrusion
        hw = w / 2
        xa, xb = min(x0, x1) - hw, max(x0, x1) + hw
        ya, yb = min(y0, y1) - hw, max(y0, y1) + hw
        ia, ib = max(0, int((xa - ox) / RES)), min(nx, int((xb - ox) / RES) + 1)
        ja, jb = max(0, int((ya - oy) / RES)), min(ny, int((yb - oy) / RES) + 1)
        if ib <= ia or jb <= ja:
            continue
        gx, gy = np.mgrid[ia:ib, ja:jb]
        px, py = gx * RES + ox + RES / 2, gy * RES + oy + RES / 2
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
            ca, cb = ia // C, min(ncx, (ib - 1) // C + 1)
            da, db = ja // C, min(ncy, (jb - 1) // C + 1)
            blk = H[ca * C:cb * C, da * C:db * C]
            Hc[ca:cb, da:db] = blk.reshape(cb - ca, C, db - da, C).max(axis=(1, 3))
            dirty_z = max(dirty_z, z)
    print("extrusions onto another object: %d" % len(onto))
    for c in onto[:10]:
        print("  ", c)
    print("gantry over prints taller than the gantry height (%g mm): %d moves; worst %+.2f mm above the nozzle at %s"
          % (gantry, gantry_n, gworst[0], gworst[1]))
    print("toolhead box over finished prints (more than %g mm above the nozzle tip = protrusion %g - %g): "
          "%d moves; worst %s" % (thr, protrusion, LOW_MARGIN, box_n,
                                  ("%+.2f mm above the nozzle at %s" % bworst) if bworst[1] else "-"))
    print("nozzle crashes on travels: %d" % len(crash))
    for c in crash[:10]:
        print("  ", c)
    segs = [s for s in segs if s[8] != 2]
    np.savez_compressed(path + ".paths.npz",
                        segs=np.array([s[:7] for s in segs], np.float32),
                        trav=np.array([s[8] for s in segs], np.int8),
                        obj=np.array([s[7] for s in segs]),
                        origin=np.array([ox, oy], np.float64))
    np.save(path + ".height.npy", H)
    e_tot = sum(s[6] for s in segs)
    print("filament %.2f m" % (e_tot / 1000))
    return {"box": box_n, "gantry": gantry_n, "crash": len(crash), "onto": len(onto), "oob": len(oob)}


def _floats(text, n, what):
    try:
        v = [float(t) for t in text.split(",")]
    except ValueError:
        v = []
    if len(v) != n:
        raise SystemExit("check_gcode: %s needs %d numbers" % (what, n))
    return v


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    kw = dict(a[2:].split("=", 1) for a in sys.argv[1:] if a.startswith("--") and "=" in a)
    if len(args) != 1:
        raise SystemExit(__doc__)
    if "low" in kw:
        raise SystemExit("check_gcode: --low is gone: give the nozzle protrusion with --protrusion=MM")
    hd = read_header(args[0])
    src = []

    def val(key, hkey):
        if key in kw:
            return kw[key]
        if hkey in hd:
            src.append(key)
            return hd[hkey]
        raise SystemExit(__doc__ + "\ncheck_gcode: give --%s (the session has no printune_geometry header; "
                         "no geometry is assumed)" % key)
    xn, xp, yn, yp = (abs(v) for v in _floats(val("box", "toolhead_box"), 4, "--box"))
    gantry = float(val("gantry", "gantry_height"))
    prot = float(val("protrusion", "nozzle_protrusion"))
    braw = val("bed", "print_area")
    b = [float(t) for t in braw.split(",")]
    bed = tuple(b) if len(b) == 4 else (0.0, 0.0) + tuple(_floats(braw, 2, "--bed"))
    if src:
        print("from the session header: %s" % ", ".join(src))
    skip = tuple(kw["skip"].split(",")) if kw.get("skip") else ()
    main(args[0], (-xn, xp, -yn, yp), gantry, prot, bed, skip=skip)
