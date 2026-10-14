"""Rough top-view sanity plot of an ECAL session: toolpaths by test, object boxes, print order."""
import re, sys
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.collections import LineCollection
from matplotlib.patches import Rectangle, Circle

path, out = sys.argv[1], sys.argv[2]
R = float(sys.argv[3]) if len(sys.argv) > 3 else 45
d = np.load(path + ".paths.npz")
segs, trav, obj = d["segs"], d["trav"], d["obj"]
fam = np.array([re.sub(r"\d+$", "", o).rstrip("_") for o in obj])
col = {"PATCH": "#4C78A8", "PATCH_B": "#9ECAE9", "PA_COARSE": "#E45756", "PA_FINE": "#FF9DA6",
       "EM_C": "#54A24B", "EM_F": "#88D27A", "RET": "#B279A2", "-": "#999999"}
defs = []
for ln in open(path):
    if ln.startswith("EXCLUDE_OBJECT_DEFINE"):
        name = ln.split("NAME=")[1].split()[0]
        pts = np.array(eval(ln.split("POLYGON=")[1]))
        defs.append((name, pts))
fig, ax = plt.subplots(figsize=(10, 10.6))
ax.add_patch(Rectangle((0, 0), 375, 388, fill=False, lw=1.5, ec="k"))
m = trav == 0
for f, c in col.items():
    sel = m & (fam == f)
    if sel.any():
        s = segs[sel]
        ax.add_collection(LineCollection(np.stack([s[:, 0:2], s[:, 2:4]], 1), colors=c, lw=0.6))
order = {}
for i, (name, pts) in enumerate(defs):
    x0, y0 = pts.min(0); x1, y1 = pts.max(0)
    ax.add_patch(Rectangle((x0, y0), x1 - x0, y1 - y0, fill=False, lw=0.6, ls="--", ec="0.4"))
    ax.text(x0 + 1, y1 + 1, "%d %s" % (i + 1, name), fontsize=6.5)
    if name.startswith("RET"):
        ax.add_patch(Circle(((x0 + x1) / 2, (y0 + y1) / 2), R + (x1 - x0) / 2, fill=False, lw=0.6, ec="#B279A2", ls=":"))
ax.set_xlim(-5, 380); ax.set_ylim(-5, 393); ax.set_aspect("equal")
ax.set_xlabel("X (mm)  - bed front is at the bottom"); ax.set_ylabel("Y (mm)")
ax.set_title("ROUGH layout check - ECAL session, top view (assumed 375 x 388 bed)\n"
             "numbers = print order; dotted circles = %d mm toolhead clearance around the towers" % R, fontsize=10)
from matplotlib.lines import Line2D
ax.legend([Line2D([0], [0], color=c, lw=3) for c in list(col.values())[:-1]],
          ["1 squish patches", "1 extra patches (optional)", "2 PA pattern", "2 PA fine (optional)",
           "3 EM cubes", "3 EM fine (optional)", "4 retraction towers"], loc="upper right", fontsize=7, framealpha=0.9)
fig.tight_layout(); fig.savefig(out, dpi=130)
print("wrote", out)
