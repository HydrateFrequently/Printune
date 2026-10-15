#!/usr/bin/env python3
"""Layer times of the cooling test, from a session G-code.

Reads the G-code as the printer would, runs every move through printune_gen.kalico_plan (a port
of Kalico's toolhead lookahead: junction deviation, centripetal, minimum_cruise_ratio, Z limits)
and prints:
  * the cone: each marker's layer time (target, as Kalico plans it, and length / speed)
  * the overhang blocks: the shortest and longest layer above the base

check_layer_time.py FILE [--scv 5] [--mcr 0.5] [--zv V] [--za A] [--vmax V]
(zv/za/vmax default to no limit; pass your printer's values for an exact match)
"""
import argparse
import math
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import printune_gen as gen  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("gcode")
    ap.add_argument("--scv", type=float, default=5.0)
    ap.add_argument("--mcr", type=float, default=0.5)
    ap.add_argument("--zv", type=float, default=1e9)
    ap.add_argument("--za", type=float, default=1e9)
    ap.add_argument("--vmax", type=float, default=1e9)
    a = ap.parse_args()
    plan = dict(scv=a.scv, mcr=a.mcr, zv=a.zv, za=a.za, vmax=a.vmax)
    lines = open(a.gcode, encoding="utf-8").read().split("\n")
    mv, before = gen.gcode_moves(lines)
    # each object's stretch of moves
    spans, cur = {}, None
    for i, ln in enumerate(lines):
        if ln.startswith("EXCLUDE_OBJECT_START NAME="):
            cur = ln.split("NAME=")[1].strip()
            spans.setdefault(cur, [before[i], None, i, None])
        elif ln.startswith("EXCLUDE_OBJECT_END") and cur:
            spans[cur][1], spans[cur][3] = before[i], i
            cur = None
    cone = spans.get("COOL_CONE")
    if not cone:
        print("no cooling cone in %s" % a.gcode)
    else:
        m0, m1, l0, l1 = cone
        t = gen.kalico_plan(mv[m0:m1], **plan)
        rows = []
        for i in range(l0, l1):
            m = re.match(r"; cone layer (\d+)(?:: ([\d.]+) s)?(?:, marker (\d+) s)?", lines[i])
            if m:
                rows.append([before[i] - m0, int(m.group(1)), m.group(2), m.group(3)])
        for j, r in enumerate(rows):
            r.append(rows[j + 1][0] if j + 1 < len(rows) else m1 - m0)
        print("cone: %d layers; markers (layer time as Kalico plans it, and length / speed):" % len(rows))
        print("  marker  layer   target    Kalico   len/speed")
        worst = 0.0
        for i0, k, tgt, mk, i1 in rows:
            tk = sum(t[i0:i1])
            naive = sum(math.dist(p[:3], q[:3]) / v for p, q, v, _ in mv[m0 + i0:m0 + i1])
            if mk:
                worst = max(worst, abs(tk - float(tgt)))
                print("  %4s s  %5d  %6.2f s  %6.2f s  %6.2f s" % (mk, k, float(tgt), tk, naive))
        print("  top layer %d: %.2f s; worst marker off its target: %.2f s" % (
            rows[-1][1], sum(t[rows[-1][0]:rows[-1][4]]), worst))
    for name in sorted(n for n in spans if re.match(r"COOL_\d+$", n)):
        m0, m1, l0, l1 = spans[name]
        t = gen.kalico_plan(mv[m0:m1], **plan)
        st = [before[i] - m0 for i in range(l0, l1) if lines[i].startswith("; block layer")] + [m1 - m0]
        lt = [sum(t[st[j]:st[j + 1]]) for j in range(len(st) - 1)]
        fan = next((l for l in lines[l0:l1] if l.startswith("; cooling block")), "")
        ramp = lt[gen.BASE_LAYERS:]
        print("%s: %d layers, block layers %.1f-%.1f s  (%s)" % (
            name, len(lt), min(ramp), max(ramp), fan[2:].split(",")[0]))


if __name__ == "__main__":
    main()
