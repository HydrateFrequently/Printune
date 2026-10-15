#!/usr/bin/env python3
"""Shaded 3D render of a simulated Printune session (needs pyvista, scipy).

Low parts are drawn from the heightmap that check_gcode.py writes
(<gcode>.height.npy, 0.25 mm grid). The retraction towers have overhanging
rings a heightmap can't show, so they are voxelised from the toolpaths
(<gcode>.paths.npz) and drawn as a true 3D surface.
"""
import sys

import numpy as np
import pyvista as pv
from scipy.ndimage import gaussian_filter

RES = 0.25


def surface(H, x0, x1, y0, y1, step=1):
    i0, i1 = int(x0 / RES), int(x1 / RES)
    j0, j1 = int(y0 / RES), int(y1 / RES)
    h = H[i0:i1:step, j0:j1:step].astype(float)
    xs = np.arange(i0, i1, step) * RES
    ys = np.arange(j0, j1, step) * RES
    X, Y = np.meshgrid(xs, ys, indexing="ij")
    g = pv.StructuredGrid(X, Y, h)
    g["h"] = h.ravel(order="F")
    return g


VX, VZ = 0.1, 0.05


def tower_mesh(segs, objs, trav, name, h1=0.25, h=0.2):
    m = (objs == name) & (trav == 0)
    S = segs[m]
    pad = 1.0
    xa, xb = S[:, [0, 2]].min() - pad, S[:, [0, 2]].max() + pad
    ya, yb = S[:, [1, 3]].min() - pad, S[:, [1, 3]].max() + pad
    zt = S[:, 4].max() + 0.5
    nx, ny, nz = int((xb - xa) / VX) + 1, int((yb - ya) / VX) + 1, int(zt / VZ) + 1
    V = np.zeros((nx, ny, nz), np.float32)
    for x0, y0, x1, y1, z, w, e in S:
        hh = h1 if z <= h1 + 1e-3 else h
        hw = w / 2
        ia, ib = int((min(x0, x1) - hw - xa) / VX), int((max(x0, x1) + hw - xa) / VX) + 2
        ja, jb = int((min(y0, y1) - hw - ya) / VX), int((max(y0, y1) + hw - ya) / VX) + 2
        gi, gj = np.mgrid[ia:ib, ja:jb]
        px, py = xa + gi * VX, ya + gj * VX
        dx, dy = x1 - x0, y1 - y0
        L2 = max(dx * dx + dy * dy, 1e-9)
        tt = np.clip(((px - x0) * dx + (py - y0) * dy) / L2, 0, 1)
        dd = np.hypot(px - (x0 + tt * dx), py - (y0 + tt * dy))
        ka, kb = int((z - hh) / VZ), int(z / VZ) + 1
        sub = V[ia:ib, ja:jb, ka:kb]
        sub[dd <= hw] = 1.0
    V = gaussian_filter(V, (0.6, 0.6, 1.2))
    img = pv.ImageData(dimensions=V.shape, spacing=(VX, VX, VZ), origin=(xa, ya, 0))
    img["v"] = V.ravel(order="F")
    return img.contour([0.5], scalars="v"), (xa, xb, ya, yb)


def render(H, box, out, cam, step=1, size=(1800, 1200), title=None, towers=()):
    x0, x1, y0, y1 = box
    g = surface(H, x0, x1, y0, y1, step)
    part = g.threshold(0.05, scalars="h", preference="point")
    pl = pv.Plotter(off_screen=True, window_size=size)
    pl.set_background("#f4f4f2")
    bed = pv.Box((x0, x1, y0, y1, -3, 0))
    pl.add_mesh(bed, color="#3a3d42", smooth_shading=False, specular=0.2)
    pl.add_mesh(part.extract_surface(), color="#e2762a", smooth_shading=True,
                specular=0.35, specular_power=20, ambient=0.18, diffuse=0.85)
    for tm in towers:
        pl.add_mesh(tm, color="#e2762a", smooth_shading=True,
                    specular=0.35, specular_power=20, ambient=0.18, diffuse=0.85)
    pl.camera_position = cam
    pl.add_light(pv.Light(position=(x0 - 200, y0 - 300, 400), focal_point=((x0 + x1) / 2, (y0 + y1) / 2, 0), intensity=0.6))
    if title:
        pl.add_text(title, position="upper_left", font_size=12, color="#222222")
    pl.screenshot(out)
    pl.close()
    print("wrote", out)


def main(gpath, outdir):
    H = np.load(gpath + ".height.npy").copy()
    d = np.load(gpath + ".paths.npz")
    # check_gcode.py's heightmap starts at its origin (the bed's corner less a margin): this script
    # works in bed coordinates from 0, so shift it there
    ox, oy = (float(v) for v in d["origin"]) if "origin" in d.files else (0.0, 0.0)
    for axis, o in ((0, ox), (1, oy)):
        n = int(round(abs(o) / RES))
        if o < 0:
            H = H[n:] if axis == 0 else H[:, n:]
        elif o > 0:
            H = np.concatenate([np.zeros((n, H.shape[1]) if axis == 0 else (H.shape[0], n), H.dtype), H], axis)
    segs, objs, trav = d["segs"], d["obj"], d["trav"]
    towers = []
    for name in sorted(set(objs)):
        if name.startswith("RET_"):
            mesh, (xa, xb, ya, yb) = tower_mesh(segs, objs, trav, name)
            towers.append(mesh)
            # the voxel mesh replaces the heightmap over the whole tower object
            H[int(xa / RES):int(xb / RES) + 1, int(ya / RES):int(yb / RES) + 1] = 0
    cx, cy = 187.5, 194
    render(H, (0, 375, 0, 388), outdir + "/shaded_bed.png",
           [(cx - 40, -330, 380), (cx, cy - 10, 0), (0, 0, 1)], step=2,
           title="Shaded render of the whole session, simulated from the G-code (front of bed at bottom)",
           towers=towers)
    # close-ups: find object regions from the heightmap automatically is fiddly; use fixed boxes
    for name, box, cam in CLOSEUPS:
        render(H, box, outdir + "/shaded_%s.png" % name, cam, step=1, size=(1600, 1100),
               towers=towers[:1] if name == "tower" else ())


CLOSEUPS = [
    ("pa", (222, 316, 85, 155), [(269, 40, 110), (269, 120, 0), (0, 0, 1)]),
    ("em", (66, 146, 8, 156), [(40, -60, 90), (106, 80, 0), (0, 0, 1)]),
    ("tower", (240, 298, 26, 68), [(330, -40, 75), (269, 47, 20), (0, 0, 1)]),
]

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
