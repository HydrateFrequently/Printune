#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
r"""
ECAL - Ellis' tuning guide as ONE heat-soaked print session.

Generates a single Klipper/Kalico G-code file that runs these tests from
Andrew Ellis' Print Tuning Guide, in the guide's order, on one bed:

  1. First layer squish  - 25x25 mm patches scattered over the bed (live babystep)
  2. Pressure advance    - Ellis' pattern method (+ optional fine pass)
  3. Extrusion multiplier- 30x30x3 mm cubes (+ optional 0.5 % fine pass)
  4. Retraction          - 3 towers at 3 temperatures, printed one at a time

Between tests the printer pauses and Mainsail/Fluidd shows a prompt with one
button per tested value. The picked value is applied to every later test
(PA via SET_PRESSURE_ADVANCE, EM via M221, retraction via SET_RETRACTION),
so the order and dependencies of the guide are kept while the chamber stays
hot. Needs ellis_calibration.cfg included in printer.cfg.

Usage
  OrcaSlicer post-processing script (Process > Others > Post-processing scripts):
      /usr/bin/python3 /path/to/ecal.py;
      "C:\Path\To\python.exe" "C:\path\to\ecal.py";        (Windows)
  then add ECAL.stl to an empty plate and slice. Every setting (speeds, accels,
  widths, temps, retraction, PA, flow ratio, bed size, start/end G-code) is
  taken from the profiles used for that slice. Without an object named ECAL...
  on the plate the script does nothing.

  Stand-alone, from an Orca G-code you already sliced (any object):
      python3 ecal.py --from path/to/sliced.gcode -o ecal_session.gcode

  Stand-alone with built-in defaults (edit START_GCODE/END_GCODE first):
      python3 ecal.py -o ecal_session.gcode

Options can also be written into the ECAL object's name in Orca, e.g.
  "ECAL pa=0-0.08/0.005 em=0.92-1.00/0.02 ret=0-1/0.1 temps=260,250,240 skip=patches"

The PA pattern is a port of Andrew Ellis' Pressure_Linear_Advance_Tool
(Copyright (C) 2019 Sineos, (C) 2022 AndrewEllis93, GPL-3.0), which is why
this file is GPL-3.0 too.
"""

import argparse
import math
import os
import re
import shlex
import sys

VERSION = 5            # must match variable_version in ellis_calibration.cfg
MARKER = "ECAL"

# --------------------------------------------------------------------------
# Defaults. Orca key names so a config dump can override them 1:1.
# Used only where neither the Orca slice nor the options give a value.
# These are generic ABS-on-a-370-mm-CoreXY values, NOT measured on anyone's printer.
# --------------------------------------------------------------------------
DEFAULTS = {
    "printable_area": "0x0,375x0,375x388,0x388",
    "printable_height": "350",
    "nozzle_diameter": "0.4",
    "filament_diameter": "1.75",
    "layer_height": "0.2",
    "initial_layer_print_height": "0.25",
    "line_width": "0.45",
    "initial_layer_line_width": "0.5",
    "outer_wall_line_width": "0.42",
    "inner_wall_line_width": "0.45",
    "internal_solid_infill_line_width": "0.42",
    "sparse_infill_line_width": "0.45",
    "top_surface_line_width": "0.4",
    "initial_layer_speed": "50",
    "initial_layer_infill_speed": "80",
    "outer_wall_speed": "150",
    "inner_wall_speed": "200",
    "internal_solid_infill_speed": "200",
    "sparse_infill_speed": "250",
    "top_surface_speed": "60",
    "travel_speed": "300",
    "default_acceleration": "5000",
    "outer_wall_acceleration": "3000",
    "inner_wall_acceleration": "5000",
    "top_surface_acceleration": "3000",
    "internal_solid_infill_acceleration": "100%",
    "sparse_infill_acceleration": "100%",
    "initial_layer_acceleration": "1000",
    "travel_acceleration": "8000",
    "filament_max_volumetric_speed": "24",
    "nozzle_temperature": "250",
    "nozzle_temperature_initial_layer": "250",
    "fan_max_speed": "60",
    "fan_min_speed": "30",
    "close_fan_the_first_x_layers": "1",
    "use_firmware_retraction": "1",
    "retraction_length": "0.5",
    "retraction_speed": "35",
    "deretraction_speed": "0",
    "z_hop": "0.2",
    "retraction_minimum_travel": "1",
    "pressure_advance": "0.04",
    "enable_pressure_advance": "0",
    "filament_flow_ratio": "1",
    "print_flow_ratio": "1",
    "wall_loops": "2",
    "wall_sequence": "inner wall/outer wall",
    "infill_wall_overlap": "15%",
    "top_bottom_infill_wall_overlap": "25%",
    "extruder_clearance_radius": "65",
    "extruder_clearance_height_to_rod": "36",
    "gcode_flavor": "klipper",
}

# Stand-alone start/end G-code (Orca mode uses the slice's own).
START_GCODE = "PRINT_START BED=100 HOTEND=250 CHAMBER=0"
END_GCODE = "PRINT_END"

# Test options (not Orca settings). All can be set on the command line or in the object name.
OPTIONS = {
    "skip": "",                 # comma list of: patches, pa, em, ret
    "patches_extra": 4,         # optional 2nd round of patches (0 = none)
    "pa": "0-0.08/0.005",       # Ellis tool default for direct drive
    "pa_fine": "0.005/0.001",   # +-range/step around the pick, 0 = no fine pass
    "em": "",                   # "" = profile flow ratio -4..+4 % in 2 % steps
    "em_fine": "0.01/0.005",    # +-range/step around the pick, 0 = no fine pass
    "em_infill": 30,            # Ellis: 30 %+ sparse infill
    "em_size": 30.0,            # Ellis: 30x30x3 mm cubes
    "em_height": 3.0,
    "ret": "0-1.0/0.1",         # Ellis: direct drive 0.1 mm steps, max 1 mm
    "ret_speed": 30,            # Ellis: start at 30 mm/s retract and unretract
    "temps": "",                # "" = nozzle temp +10, 0, -10 (Ellis 3x10 deg)
    "ret_fan": 0,               # 0 = max(fan_max_speed, 80) (Ellis 80-100 %)
    "em_fan": 0,                # 0 = fan_max_speed (Ellis: moderate to high)
    "pa_fan": 0,                # 0 = fan_max_speed
    "clearance": 0,             # toolhead radius for the collision check, 0 = Orca extruder_clearance_radius
    "low_ok": 1.2,              # objects lower than this may sit under the toolhead
    "margin": 12.0,             # keep-out from the printable-area edge
}


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
def _first(v):
    v = str(v).strip().strip('"')
    return v.split(",")[0].strip() if ("," in v and "x" not in v) else v


def _num(v):
    return float(_first(v).rstrip("%"))


class Settings:
    def __init__(self, cfg, opts):
        self.cfg = dict(DEFAULTS)
        self.cfg.update({k: v for k, v in cfg.items() if v not in (None, "")})
        self.opt = dict(OPTIONS)
        self.opt.update(opts)
        c = self.cfg
        if "klipper" not in c.get("gcode_flavor", "klipper").lower():
            raise SystemExit("ECAL: G-code flavor must be Klipper (got %s)" % c.get("gcode_flavor"))

        pts = [tuple(map(float, p.split("x"))) for p in c["printable_area"].split(",")]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        self.bed = (min(xs), min(ys), max(xs), max(ys))
        self.max_z = _num(c["printable_height"])
        self.nozzle = _num(c["nozzle_diameter"])
        self.fil_area = math.pi * (_num(c["filament_diameter"]) / 2) ** 2
        self.h = _num(c["layer_height"])
        self.h1 = _num(c["initial_layer_print_height"])

        lw = self._width("line_width", self.nozzle * 1.125)
        self.w_first = self._width("initial_layer_line_width", lw)
        self.w_outer = self._width("outer_wall_line_width", lw)
        self.w_inner = self._width("inner_wall_line_width", lw)
        self.w_solid = self._width("internal_solid_infill_line_width", lw)
        self.w_sparse = self._width("sparse_infill_line_width", lw)
        self.w_top = self.nozzle  # Ellis EM method: top layer line width 100 %

        self.v_first = _num(c["initial_layer_speed"])
        self.v_first_infill = _num(c["initial_layer_infill_speed"])
        self.v_outer = _num(c["outer_wall_speed"])
        self.v_inner = _num(c["inner_wall_speed"])
        self.v_solid = _num(c["internal_solid_infill_speed"])
        self.v_sparse = _num(c["sparse_infill_speed"])
        self.v_top = _num(c["top_surface_speed"])
        self.v_travel = _num(c["travel_speed"])
        self.mvs = _num(c["filament_max_volumetric_speed"]) or 1e9

        a0 = _num(c["default_acceleration"])
        self.a_default = a0
        self.a_outer = self._acc("outer_wall_acceleration", a0)
        self.a_inner = self._acc("inner_wall_acceleration", a0)
        self.a_top = self._acc("top_surface_acceleration", a0)
        self.a_solid = self._acc("internal_solid_infill_acceleration", a0)
        self.a_sparse = self._acc("sparse_infill_acceleration", a0)
        self.a_first = self._acc("initial_layer_acceleration", a0)
        self.a_travel = self._acc("travel_acceleration", a0)

        self.t_nozzle = _num(c["nozzle_temperature"])
        self.t_first = _num(c["nozzle_temperature_initial_layer"])
        self.fan_max = _num(c["fan_max_speed"])
        self.fan_min = _num(c["fan_min_speed"])
        self.fan_off_layers = int(_num(c["close_fan_the_first_x_layers"]))

        self.fw_retract = _first(c["use_firmware_retraction"]) in ("1", "true")
        self.ret_len = _num(c["retraction_length"])
        self.ret_speed = _num(c["retraction_speed"])
        dr = _num(c["deretraction_speed"])
        self.unret_speed = dr if dr > 0 else self.ret_speed
        self.z_hop = _num(c["z_hop"])
        self.min_travel = _num(c["retraction_minimum_travel"])

        self.pa_profile = _num(c["pressure_advance"])
        self.pa_enabled = _first(c["enable_pressure_advance"]) in ("1", "true")
        self.em_profile = _num(c["filament_flow_ratio"]) * _num(c["print_flow_ratio"])
        self.wall_loops = max(1, int(_num(c["wall_loops"])))
        self.outer_first = "outer wall/inner" in c["wall_sequence"].lower()
        self.ov_sparse = _num(c["infill_wall_overlap"]) / 100.0
        self.ov_solid = _num(c["top_bottom_infill_wall_overlap"]) / 100.0
        o = self.opt
        self.clearance = float(o["clearance"]) or _num(c["extruder_clearance_radius"])
        self.low_ok = float(o["low_ok"])
        self.margin = float(o["margin"])
        self.skip = {s.strip().lower() for s in str(o["skip"]).split(",") if s.strip()}

    def _width(self, key, fallback):
        v = str(self.cfg.get(key, "0")).strip()
        if v.endswith("%"):
            r = float(v[:-1]) / 100.0 * self.nozzle
        else:
            r = _num(v)
        return r if r > 0 else fallback

    def _acc(self, key, base):
        v = _first(self.cfg.get(key, "0"))
        r = float(v[:-1]) / 100.0 * base if v.endswith("%") else float(v)
        return r if r > 0 else base


def parse_range(text):
    """'0-0.08/0.005' -> [0, 0.005, ...]; '255,245' -> list."""
    # '_' works as the step separator too: a file or object name can't hold '/'
    text = re.sub(r"^([^/_]*)_([\d.]+)$", r"\1/\2", str(text).strip())
    if "/" in text:
        span, step = text.split("/")
        a, b = re.split(r"(?<=\d)-", span)
        a, b, step = float(a), float(b), float(step)
        n = int(round((b - a) / step)) + 1
        return [round(a + i * step, 6) for i in range(n)]
    return [float(v) for v in text.split(",") if v.strip()]


def parse_fine(text):
    """'0.005/0.001' -> [-0.005 ... +0.005]; '0' -> []"""
    text = str(text).strip()
    if text in ("", "0", "none", "off"):
        return []
    text = re.sub(r"^([\d.]+)_([\d.]+)$", r"\1/\2", text)
    rng, step = (float(v) for v in text.split("/"))
    n = int(round(rng / step))
    return [round(i * step, 6) for i in range(-n, n + 1)]


def fmt_val(v, nd=4):
    s = ("%." + str(nd) + "f") % v
    s = s.rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


# --------------------------------------------------------------------------
# G-code writer
# --------------------------------------------------------------------------
class Writer:
    def __init__(self, s):
        self.s = s
        self.out = []
        self.x = self.y = None
        self.z = 0.0
        self.retracted = False
        self.hopped = False
        self.accel = None
        self.ret_macro = False      # use _ECAL_R/_ECAL_U (retraction test without fw retraction)
        self.no_retract = False     # ring 1 of the retraction test (0 mm)
        self.e_total = 0.0
        self.t_total = 0.0          # rough print time, s (moves only, no heating or pauses)
        self.max_h = 0.0            # tallest thing printed so far
        self.paths = []             # (x0,y0,x1,y1,z,w,kind) for checks/renders

    def g(self, line):
        self.out.append(line)

    def comment(self, text):
        self.out.append("; " + text)

    def _t(self, L, v):
        a = self.accel or 3000
        if L >= v * v / a:
            self.t_total += L / v + v / a
        else:
            self.t_total += 2 * math.sqrt(L / a)

    def set_accel(self, a):
        a = int(round(a))
        if a > 0 and a != self.accel:
            self.g("SET_VELOCITY_LIMIT ACCEL=%d" % a)
            self.accel = a

    def fan(self, pct):
        self.g("M106 S%d" % int(round(max(0, min(100, pct)) * 2.55)))

    def retract(self):
        if self.retracted or self.no_retract:
            return
        s = self.s
        if s.fw_retract:
            self.g("G10")
        elif self.ret_macro:
            self.g("_ECAL_R")
        else:
            self.g("G1 E-%.4f F%d" % (s.ret_len, s.ret_speed * 60))
        self.retracted = True

    def unretract(self):
        if not self.retracted:
            return
        s = self.s
        if s.fw_retract:
            self.g("G11")
        elif self.ret_macro:
            self.g("_ECAL_U")
        else:
            self.g("G1 E%.4f F%d" % (s.ret_len, s.unret_speed * 60))
        self.retracted = False

    def lift(self):
        if self.s.z_hop > 0 and not self.hopped:
            self.g("G1 Z%.3f F900" % (self.z + self.s.z_hop))
            self.hopped = True

    def unlift(self):
        if self.hopped:
            self.g("G1 Z%.3f F900" % self.z)
            self.hopped = False

    def z_to(self, z):
        if self.hopped or abs(z - self.z) > 1e-6:
            self.g("G1 Z%.3f F900" % z)
        self.hopped = False
        self.z = z

    def travel(self, x, y, force=False):
        if self.x is not None and abs(x - self.x) < 1e-6 and abs(y - self.y) < 1e-6:
            return
        d = 1e9 if self.x is None else math.hypot(x - self.x, y - self.y)
        if force or d > self.s.min_travel:
            self.retract()
            self.lift()
            self.set_accel(self.s.a_travel)   # short hops between fill lines keep the print accel
        self.g("G1 X%.3f Y%.3f F%d" % (x, y, self.s.v_travel * 60))
        if self.x is not None:
            self.paths.append((self.x, self.y, x, y, self.z, 0.0, "travel"))
            self._t(d, self.s.v_travel)
        self.x, self.y = x, y

    def safe_travel(self, x, y, clear=5.0):
        """Retract, rise above everything printed, move, stay up (caller lowers)."""
        self.retract()
        zs = max(self.z, self.max_h + clear)
        self.hopped = False
        self.g("G1 Z%.3f F900" % zs)
        self.z = zs
        self.set_accel(self.s.a_travel)
        self.g("G1 X%.3f Y%.3f F%d" % (x, y, self.s.v_travel * 60))
        self.x, self.y = x, y

    @staticmethod
    def area(w, h):
        return (w - h) * h + math.pi * (h / 2) ** 2

    def line(self, x, y, w, h, v, accel=None, kind="ext"):
        self.unlift()
        self.unretract()
        if accel:
            self.set_accel(accel)
        L = math.hypot(x - self.x, y - self.y)
        if L < 1e-6:
            return
        a = self.area(w, h)
        v = min(v, self.s.mvs / a)
        e = L * a / self.s.fil_area
        self.e_total += e
        self.g("G1 X%.3f Y%.3f E%.5f F%d" % (x, y, e, v * 60))
        self._t(L, v)
        self.paths.append((self.x, self.y, x, y, self.z, w, kind))
        self.x, self.y = x, y
        self.max_h = max(self.max_h, self.z)

    def polyline(self, pts, w, h, v, accel=None, kind="ext", closed=False):
        pts = list(pts)
        if closed:
            pts = pts + [pts[0]]
        self.travel(*pts[0])
        for p in pts[1:]:
            self.line(p[0], p[1], w, h, v, accel, kind)


# --------------------------------------------------------------------------
# Geometry helpers
# --------------------------------------------------------------------------
def spacing(w, h):
    return w - h * (1 - math.pi / 4)


def rect_loop(x0, y0, x1, y1, start="bl"):
    pts = [(x0, y0), (x0, y1), (x1, y1), (x1, y0)]
    k = {"bl": 0, "tl": 1, "tr": 2, "br": 3}[start]
    return pts[k:] + pts[:k]


def hatch(x0, y0, x1, y1, pitch, angle):
    """Lines at +-45 or 0/90 deg clipped to the box, zig-zag order (monotonic)."""
    segs = []
    if angle in (45, -45):
        # line: y = x*sgn + k  (sgn=+1 for 45)
        sg = 1 if angle == 45 else -1
        kp = pitch * math.sqrt(2)
        if sg == 1:
            kmin, kmax = y0 - x1, y1 - x0
        else:
            kmin, kmax = y0 + x0, y1 + x1
        n = int(math.floor((kmax - kmin) / kp))
        off = ((kmax - kmin) - n * kp) / 2
        for i in range(n + 1):
            k = kmin + off + i * kp
            if sg == 1:
                xa, xb = max(x0, y0 - k), min(x1, y1 - k)
                a, b = (xa, xa + k), (xb, xb + k)
            else:
                xa, xb = max(x0, k - y1), min(x1, k - y0)
                a, b = (xa, k - xa), (xb, k - xb)
            if math.hypot(b[0] - a[0], b[1] - a[1]) > 0.3:
                segs.append((a, b))
    else:
        horiz = angle == 0
        lo, hi = (y0, y1) if horiz else (x0, x1)
        n = int(math.floor((hi - lo) / pitch))
        off = ((hi - lo) - n * pitch) / 2
        for i in range(n + 1):
            c = lo + off + i * pitch
            segs.append(((x0, c), (x1, c)) if horiz else ((c, y0), (c, y1)))
    out = []
    for i, (a, b) in enumerate(segs):
        out.append((a, b) if i % 2 == 0 else (b, a))
    return out


def circle(cx, cy, r, n=None, start_deg=90.0):
    # chord error <= 0.02 mm
    n = n or max(12, int(math.ceil(math.pi / math.acos(max(-1.0, 1 - 0.02 / max(r, 0.05))))))
    return [(cx + r * math.cos(math.radians(start_deg + 360.0 * i / n)),
             cy + r * math.sin(math.radians(start_deg + 360.0 * i / n))) for i in range(n)]


# Ellis' glyphs (from the PA tool), plus a minus sign. Native frame: text runs +Y,
# digit "up" points -X (readable from the printer's right-hand side).
GLYPHS = {
    '1': ['bl', 'right', 'right'],
    '2': ['bl', 'up', 'right', 'down', 'right', 'up'],
    '3': ['bl', 'up', 'right', 'down', 'mup', 'right', 'down'],
    '4': ['ul', 'right', 'right', 'mleft', 'down', 'left'],
    '5': ['ul', 'down', 'right', 'up', 'right', 'down'],
    '6': ['ul', 'down', 'right', 'right', 'up', 'left', 'down'],
    '7': ['bl', 'up', 'right', 'right'],
    '8': ['bl', 'right', 'right', 'up', 'left', 'left', 'down', 'mright', 'up'],
    '9': ['br', 'up', 'left', 'left', 'down', 'right', 'up'],
    '0': ['bl', 'right', 'right', 'up', 'left', 'left', 'down'],
    '.': ['br', 'dot'],
    '-': ['ml', 'up'],
}
SEG = 2.0
DOT = 0.75


def text_len(txt):
    return sum(1 if ch in "1." else 3 for ch in txt)


def draw_text(wr, x0, y0, txt, w, h, v, front=False, accel=None):
    """Ellis' drawNumber. front=True rotates so the text reads from the front of the bed
    (text runs +X, up is +Y); the text box is then text_len(txt) x 4 mm from (x0, y0)."""
    def T(dx, dy):
        if not front:
            return (x0 + dx, y0 + dy)
        return (x0 + dy, y0 + 4.0 - dx)
    cx = cy = 0.0
    adv = 0.0
    for ch in txt:
        for seg in GLYPHS[ch]:
            if seg == 'bl':
                cx, cy = 0.0, adv
                wr.travel(*T(cx, cy))
            elif seg == 'br':
                cx, cy = 2 * SEG, adv
                wr.travel(*T(cx, cy))
            elif seg == 'ul':
                cx, cy = 0.0, adv + SEG
                wr.travel(*T(cx, cy))
            elif seg == 'ml':
                cx, cy = SEG, adv
                wr.travel(*T(cx, cy))
            elif seg in ('up', 'down', 'right', 'left', 'dot'):
                d = {'up': (0, SEG), 'down': (0, -SEG), 'right': (SEG, 0),
                     'left': (-SEG, 0), 'dot': (-DOT, 0)}[seg]
                cx, cy = cx + d[0], cy + d[1]
                wr.line(*T(cx, cy), w, h, v, accel, kind="glyph")
            else:  # mup/mdown/mright/mleft
                d = {'mup': (0, SEG), 'mdown': (0, -SEG), 'mright': (SEG, 0), 'mleft': (-SEG, 0)}[seg]
                cx, cy = cx + d[0], cy + d[1]
                wr.travel(*T(cx, cy))
        adv += 1 if ch in "1." else 3


def ellis_draw_box(wr, s, min_x, min_y, size_x, size_y, w, h, v, perims, fill):
    """Port of Ellis' drawBox (perimeters stepping inwards + optional 45 deg fill)."""
    sp = w - h * (1 - math.pi / 4)
    s45 = math.sin(math.radians(45))
    max_p = min(math.floor(size_x * s45 / (sp / s45)), math.floor(size_y * s45 / (sp / s45)))
    perims = min(perims, max_p)
    x, y = min_x, min_y
    max_x, max_y = min_x + size_x, min_y + size_y
    wr.travel(x, y)
    for i in range(perims):
        if i:
            x += sp
            y += sp
            wr.travel(x, y)
        y += size_y - (i * sp) * 2
        wr.line(x, y, w, h, v, accel=s.a_first, kind="anchor")
        x += size_x - (i * sp) * 2
        wr.line(x, y, w, h, v, accel=s.a_first, kind="anchor")
        y -= size_y - (i * sp) * 2
        wr.line(x, y, w, h, v, accel=s.a_first, kind="anchor")
        x -= size_x - (i * sp) * 2
        wr.line(x, y, w, h, v, accel=s.a_first, kind="anchor")
    if fill:
        enc = 1.0 / 3
        inset = sp * (perims - 1) + w * (1 - enc)
        x0, x1, y0, y1 = min_x + inset, max_x - inset, min_y + inset, max_y - inset
        for a, b in hatch(x0, y0, x1, y1, sp, 45):
            wr.travel(*a)
            wr.line(*b, w, h, v, accel=s.a_first, kind="anchor")


# --------------------------------------------------------------------------
# Objects: each has a name, footprint (x0,y0,x1,y1), height, and a draw(layer) fn
# --------------------------------------------------------------------------
class Obj:
    def __init__(self, name, box, height, section, low=False):
        self.name = name.upper()
        self.box = box
        self.height = height
        self.section = section

    def poly(self):
        x0, y0, x1, y1 = self.box
        return "[[%.3f,%.3f],[%.3f,%.3f],[%.3f,%.3f],[%.3f,%.3f]]" % (x0, y0, x1, y0, x1, y1, x0, y1)

    def center(self):
        x0, y0, x1, y1 = self.box
        return ((x0 + x1) / 2, (y0 + y1) / 2)


def obj_start(wr, o):
    wr.g("EXCLUDE_OBJECT_START NAME=%s" % o.name)


def obj_end(wr, o):
    wr.g("EXCLUDE_OBJECT_END NAME=%s" % o.name)


def walls_rect(s, x0, y0, x1, y1, n, h, first):
    """List of (loop_points, width, speed, accel, kind) for n rectangular perimeters, plus inner edge inset."""
    loops = []
    inset = 0.0
    prev_sp = None
    for i in range(n):
        if first:
            w, v, a = s.w_first, s.v_first, s.a_first
        elif i == 0:
            w, v, a = s.w_outer, s.v_outer, s.a_outer
        else:
            w, v, a = s.w_inner, s.v_inner, s.a_inner
        sp = spacing(w, h)
        inset = w / 2 if i == 0 else inset + (prev_sp + sp) / 2
        loops.append((rect_loop(x0 + inset, y0 + inset, x1 - inset, y1 - inset, "tl"), w, v, a,
                      "outer" if i == 0 else "inner"))
        prev_sp = sp
    inner_edge = inset + loops[-1][1] / 2
    return loops, inner_edge


def print_walls(wr, s, loops, h):
    order = loops if s.outer_first else list(reversed(loops))
    for pts, w, v, a, kind in order:
        wr.polyline(pts, w, h, v, a, kind, closed=True)


def print_fill(wr, s, x0, y0, x1, y1, inner_edge, w, h, v, a, pitch, angle, overlap, kind):
    ins = inner_edge + w / 2 - overlap * w
    for p, q in hatch(x0 + ins, y0 + ins, x1 - ins, y1 - ins, pitch, angle):
        wr.travel(*p)
        wr.line(*q, w, h, v, a, kind)


def prime_line(wr, s, x0, y, length, z=None):
    """Straight purge line at first-layer height, left to right."""
    z = s.h1 if z is None else z
    wr.travel(x0, y, force=True)
    wr.z_to(z)
    w = s.w_first * 1.5
    wr.line(x0 + length, y, w, z, min(s.v_first, 30), s.a_first, "prime")
    wr.line(x0 + length, y + w * 0.9, w, z, min(s.v_first, 30), s.a_first, "prime")


# ---- 1. First layer patches ----------------------------------------------
PATCH = 25.0


def patch_draw(wr, s, o):
    x0, y0, x1, y1 = o.box
    loops, edge = walls_rect(s, x0, y0, x1, y1, s.wall_loops, s.h1, True)
    print_walls(wr, s, loops, s.h1)
    sp = spacing(s.w_first, s.h1)
    print_fill(wr, s, x0, y0, x1, y1, edge, s.w_first, s.h1, s.v_first_infill, s.a_first,
               sp, 45, s.ov_solid, "patch")


# ---- 2. Ellis PA pattern ---------------------------------------------------
class PAPattern:
    """Ellis pattern method (pressure_linear_advance.js genGcode, anchor frame + numbering)."""
    WALLS, SIDE, SPACE, ANGLE, LAYERS, PERIMS = 3, 30.0, 2.0, 90.0, 4, 4
    LINE_RATIO, ANCHOR_RATIO = 1.125, 1.40
    PAD_H = PAD_V = 1.0
    ENC = 1.0 / 3

    def __init__(self, s, values, labels, cx, cy, setter):
        self.s = s
        self.values = values      # PA per pattern (abs) or offsets (rel)
        self.labels = labels      # text per pattern (every other is printed)
        self.setter = setter      # fn(i) -> gcode line that sets PA for pattern i
        n = self.n = len(values)
        self.lw = s.nozzle * self.LINE_RATIO
        self.lwa = s.nozzle * self.ANCHOR_RATIO
        self.ls = self.lw - s.h * (1 - math.pi / 4)
        self.lsa = self.lwa - s.h1 * (1 - math.pi / 4)
        half = math.radians(self.ANGLE) / 2
        self.lsang = self.ls / math.sin(half)
        self.osx = round(n * ((self.WALLS - 1) * self.lsang) + (n - 1) * (self.SPACE + self.lw)
                         + math.cos(half) * self.SIDE, 4) + self.lsa * self.PERIMS
        self.maxnum = max(text_len(labels[i]) for i in range(0, n, 2))
        self.osy = round(2 * math.sin(half) * self.SIDE, 4) + self.maxnum + self.PAD_V * 2 + self.lwa
        self.frame_y = math.sin(half) * self.SIDE * 2
        glyph_start = cx - self.osx / 2 + ((self.WALLS - 1) / 2 * self.lsang - 2)
        shift = cx - self.osx / 2 - glyph_start + self.PAD_H
        self.shift = shift + self.lwa / 2 if shift > 0 else 0.0
        self.sx = cx - (self.osx + self.shift) / 2
        self.sy = cy - self.osy / 2
        self.cx, self.cy = cx, cy
        glyph_end = (cx - self.osx / 2 + (n - 1) * (self.SPACE + self.lw)
                     + (n - 1) * ((self.WALLS - 1) * self.lsang) + 4)
        self.tab_max_x = glyph_end + self.PAD_H + self.lwa / 2
        self.box = (self.sx - 1, self.sy - 1, self.sx + self.osx + self.shift + 1, self.sy + self.osy + 1)
        self.height = s.h1 + (self.LAYERS - 1) * s.h

    def draw(self, wr, fan_pct):
        s = self.s
        half = math.radians(self.ANGLE) / 2
        v_first = s.v_first
        wr.fan(0)
        wr.g(self.setter(0))
        wr.z_to(s.h1)
        # anchor frame + numbering tab (layer 1)
        ellis_draw_box(wr, s, self.sx, self.sy, self.osx + self.shift, self.frame_y,
                       self.lwa, s.h1, v_first, self.PERIMS, False)
        tab_y = self.sy + self.frame_y + self.lsa
        tab_h = self.maxnum + self.lsa + self.PAD_V * 2
        ellis_draw_box(wr, s, self.sx, tab_y, self.tab_max_x - self.sx, tab_h,
                       self.lwa, s.h1, v_first, self.PERIMS, True)
        for i in range(self.LAYERS):
            h = s.h1 if i == 0 else s.h
            z = s.h1 + i * s.h
            if i == 1:
                wr.fan(fan_pct)
            wr.z_to(z)
            if i == 1:  # numbering on layer 2
                wr.g(self.setter(0))
                for j in range(0, self.n, 2):
                    gx = self.sx + j * (self.SPACE + self.lw) + j * ((self.WALLS - 1) * self.lsang)
                    gx += ((self.WALLS - 1) / 2) * self.lsang - 2 + self.shift
                    draw_text(wr, gx, self.sy + self.frame_y + self.PAD_V + self.lw,
                              self.labels[j], self.lw, s.h, v_first, accel=s.a_first)
            tx, ty, side = self.sx + self.shift, self.sy, self.SIDE
            if i == 0:
                shrink = ((self.lsa * (self.PERIMS - 1) + self.lwa * (1 - self.ENC)) / math.sin(half))
                side = self.SIDE - shrink
                tx += shrink * math.sin(math.radians(90) - half)
                ty += self.lsa * (self.PERIMS - 1) + self.lwa * (1 - self.ENC)
            ix, iy = tx, ty
            wr.travel(tx, ty)
            v = v_first if i == 0 else s.v_outer
            acc = s.a_first if i == 0 else s.a_outer
            for j in range(self.n):
                wr.g(self.setter(j))
                for k in range(self.WALLS):
                    tx += math.cos(half) * side
                    ty += math.sin(half) * side
                    wr.line(tx, ty, self.lw, h, v, acc, "pa")
                    tx -= math.cos(half) * side
                    ty += math.sin(half) * side
                    wr.line(tx, ty, self.lw, h, v, acc, "pa")
                    ty = iy
                    if k != self.WALLS - 1:
                        tx += self.lsang
                        wr.travel(tx, ty)
                    elif j != self.n - 1:
                        tx += self.SPACE + self.lw
                        wr.travel(tx, ty)
                    elif i != self.LAYERS - 1:
                        tx = ix
                        wr.travel(tx, ty)
        wr.max_h = max(wr.max_h, self.height)


# ---- 3. EM cubes -------------------------------------------------------------
def em_layers(s, height):
    n = 1 + int(round((height - s.h1) / s.h))
    zs = [s.h1 + i * s.h for i in range(n)]
    bottom, infill = 2, 2
    kinds = []
    for i in range(n):
        if i < bottom:
            kinds.append("bottom")
        elif i < bottom + infill:
            kinds.append("sparse")
        elif i == n - 1:
            kinds.append("top")
        else:
            kinds.append("solid")
    return zs, kinds


def em_cube_layer(wr, s, o, li, z, kind, density):
    x0, y0, x1, y1 = o.cube
    h = s.h1 if li == 0 else s.h
    loops, edge = walls_rect(s, x0, y0, x1, y1, s.wall_loops, h, li == 0)
    print_walls(wr, s, loops, h)
    ang = 45 if li % 2 == 0 else -45
    if li == 0:
        print_fill(wr, s, x0, y0, x1, y1, edge, s.w_first, h, s.v_first_infill, s.a_first,
                   spacing(s.w_first, h), ang, s.ov_solid, "bottom")
    elif kind in ("bottom", "solid"):
        print_fill(wr, s, x0, y0, x1, y1, edge, s.w_solid, h, s.v_solid, s.a_solid,
                   spacing(s.w_solid, h), ang, s.ov_solid, "solid")
    elif kind == "sparse":
        print_fill(wr, s, x0, y0, x1, y1, edge, s.w_sparse, h, s.v_sparse, s.a_sparse,
                   s.w_sparse / (density / 100.0), ang, s.ov_sparse, "sparse")
    else:  # top surface: Ellis - 100 % width, monotonic lines, top speed
        print_fill(wr, s, x0, y0, x1, y1, edge, s.w_top, h, s.v_top, s.a_top,
                   spacing(s.w_top, h), ang, s.ov_solid, "top")


def label_tab(wr, s, x0, y0, txt, layer):
    """2-layer label: filled first-layer tab, digits on layer 2. Box = (len+3) x 7 mm."""
    tw, th = text_len(txt) + 3.0, 7.0
    if layer == 0:
        ellis_draw_box(wr, s, x0, y0, tw, th, s.w_first, s.h1, s.v_first, 2, True)
    elif layer == 1:
        draw_text(wr, x0 + 1.5, y0 + 1.5, txt, s.nozzle * 1.125, s.h, s.v_first, front=True, accel=s.a_first)


def label_size(txt):
    return text_len(txt) + 3.0, 7.0


# ---- 4. Retraction towers ----------------------------------------------------
TOWER_PITCH = 30.0       # pillar centre distance
PILLAR_D = 7.0
RING_D = 10.0
SECTION_H = 4.0
RING_LAYERS = 3
BASE_LAYERS = 3


def tower_geometry(s, n_sections):
    base_h = s.h1 + (BASE_LAYERS - 1) * s.h
    n_layers = BASE_LAYERS + int(round(n_sections * SECTION_H / s.h))
    zs = [s.h1 + i * s.h for i in range(n_layers)]
    return base_h, zs


def disc_loops(s, cx, cy, r, h, start_deg):
    """wall_loops round perimeters; returns (loops, radius of the inner edge)."""
    loops = []
    rr = r - s.w_outer / 2
    prev = None
    for i in range(s.wall_loops):
        w = s.w_outer if i == 0 else s.w_inner
        v = s.v_outer if i == 0 else s.v_inner
        a = s.a_outer if i == 0 else s.a_inner
        if i:
            rr -= (prev + spacing(w, h)) / 2
        if rr < 0.3:
            break
        loops.append((circle(cx, cy, rr, start_deg=start_deg), w, v, a, "outer" if i == 0 else "inner"))
        prev = spacing(w, h)
    return loops, rr - loops[-1][1] / 2


def disc_fill(wr, s, cx, cy, r_edge, h, angle):
    """Solid 45 deg lines inside a circle of radius r_edge (inner edge of the walls)."""
    w, v, a = s.w_solid, s.v_solid, s.a_solid
    rc = r_edge + s.ov_solid * w - w / 2
    if rc < w / 2:
        return
    p = spacing(w, h)
    n = int(2 * rc / p)
    off = (2 * rc - n * p) / 2
    ca, sa = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    for i in range(n + 1):
        c = -rc + off + i * p
        hl = math.sqrt(max(0.0, rc * rc - c * c))
        if hl < 0.2:
            continue
        ends = [(-hl, c), (hl, c)] if i % 2 == 0 else [(hl, c), (-hl, c)]
        pts = [(cx + u * ca - c_ * sa, cy + u * sa + c_ * ca) for u, c_ in ends]
        wr.travel(*pts[0])
        wr.line(*pts[1], w, h, v, a, "solid")


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------
class Session:
    def __init__(self, s):
        self.s = s
        o = s.opt
        def opt(key, fn, text=None):
            text = o[key] if text is None else text
            try:
                v = fn(text)
                if fn is parse_range and (not v or (len(v) > 1 and v[1] < v[0] and key != "temps")):
                    raise ValueError
                return v
            except (ValueError, ZeroDivisionError):
                raise SystemExit("ECAL: can't read %s=%s (use start-end/step, e.g. 0-0.08/0.005, "
                                 "or a list, e.g. 260,250,240)" % (key, text))
        self.pa_vals = opt("pa", parse_range)
        self.pa_fine = opt("pa_fine", parse_fine)
        em = o["em"] or "%.4f-%.4f/0.02" % (s.em_profile - 0.04, s.em_profile + 0.04)
        self.em_vals = opt("em", parse_range, em)
        self.em_fine = opt("em_fine", parse_fine)
        self.ret_vals = opt("ret", parse_range)
        temps = o["temps"] or "%d,%d,%d" % (s.t_nozzle + 10, s.t_nozzle, s.t_nozzle - 10)
        self.temps = [int(round(t)) for t in opt("temps", parse_range, temps)]
        self.objs = []
        self.warn = []
        self.t_print = int(round(s.t_nozzle))
        self.t_first = int(round(s.t_first))

    def n_layers(self):
        mh = self.wr.max_h
        return 1 + int(round(max(0.0, mh - self.s.h1) / self.s.h))

    # ---------------- layout ----------------
    def layout(self):
        s = self.s
        bx0, by0, bx1, by1 = s.bed
        m = s.margin
        W, D = bx1 - bx0, by1 - by0
        X = lambda f: bx0 + f * W
        Y = lambda f: by0 + f * D
        objs = []
        sk = s.skip

        # patches: 3x3 over the whole bed (Ellis: scatter around the bed)
        self.patches, self.patches_b = [], []
        if "patches" not in sk:
            cxs = [bx0 + m + PATCH / 2, X(0.5), bx1 - m - PATCH / 2]
            cys = [by0 + m + PATCH / 2, Y(0.5), by1 - m - PATCH / 2]
            k = 0
            for r, cy in enumerate(cys):            # serpentine, front row first
                row = cxs if r % 2 == 0 else list(reversed(cxs))
                for cx in row:
                    k += 1
                    objs.append(Obj("PATCH_%d" % k, (cx - PATCH / 2, cy - PATCH / 2, cx + PATCH / 2, cy + PATCH / 2),
                                    s.h1, "patches"))
            self.patches = objs[:]
            b = self.patches[0].box                 # room for the prime line in front of patch 1
            self.patches[0].box = (b[0], b[1] - 6.0, b[2], b[3])
        # PA pattern(s): right column, between the tower slots
        col_r = (X(0.5) + PATCH / 2 + bx1 - m - PATCH) / 2
        self.pa_obj = self.pa_fine_obj = None
        if "pa" not in sk:
            labels = [fmt_val(v) for v in self.pa_vals]
            pat = PAPattern(s, self.pa_vals, labels, col_r, Y(0.315),
                            lambda j: "SET_PRESSURE_ADVANCE ADVANCE=%s" % fmt_val(self.pa_vals[j]))
            self.pa_obj = Obj("PA_COARSE", self._grow(pat.box, 6), pat.height, "pa")
            self.pa_obj.pat = pat
            objs.append(self.pa_obj)
            if self.pa_fine:
                lab = [fmt_val(d * 1000, 1) for d in self.pa_fine]
                patf = PAPattern(s, self.pa_fine, lab, col_r, Y(0.665),
                                 lambda j: "_ECAL_PA_REL D=%s" % fmt_val(self.pa_fine[j], 5))
                self.pa_fine_obj = Obj("PA_FINE", self._grow(patf.box, 6), patf.height, "pa_fine")
                self.pa_fine_obj.pat = patf
                objs.append(self.pa_fine_obj)

        # EM cubes: left column, coarse block at the front, fine block at the back, extra
        # patches in between (patches are low, so they may sit inside the toolhead clearance)
        self.em_c, self.em_f = [], []
        col_l = (bx0 + m + PATCH + X(0.5) - PATCH / 2) / 2
        if "em" not in sk:
            labs = [("%g" % round(v * 100, 1)) for v in self.em_vals]
            hc = self._em_block_h(len(labs))
            self.em_c = self._em_block(col_l, by0 + m + hc / 2, labs, "EM_C", "em")
            objs += self.em_c
            if self.em_fine:
                labs = [fmt_val(d * 100, 1) for d in self.em_fine]
                hf = self._em_block_h(len(labs))
                self.em_f = self._em_block(col_l, by1 - m - hf / 2, labs, "EM_F", "em_fine")
                objs += self.em_f
                gap = box_dist(self._bbox(self.em_c), self._bbox(self.em_f))
                if gap < s.clearance:
                    self.warn.append("EM blocks only %.0f mm apart (< clearance)" % gap)
        nb = int(s.opt["patches_extra"]) if self.patches else 0
        if nb:
            if self.em_c and self.em_f:
                ccy = (self._bbox(self.em_c)[3] + self._bbox(self.em_f)[1]) / 2
            else:
                ccy = Y(0.5)
            for i in range(nb):
                dx = (-1 if i % 2 == 0 else 1) * 16.0
                dy = (-1 if i < 2 else 1) * 16.0
                cx, cy = col_l + dx, ccy + dy
                ob = Obj("PATCH_B%d" % (i + 1), (cx - PATCH / 2, cy - PATCH / 2, cx + PATCH / 2, cy + PATCH / 2),
                         s.h1, "patches_b")
                self.patches_b.append(ob)
                objs.append(ob)

        # retraction towers: right column, front to back, hottest first
        self.towers = []
        if "ret" not in sk:
            base_h, zs = tower_geometry(s, len(self.ret_vals))
            fy = [Y(0.14), Y(0.49), Y(0.84)]
            if len(self.temps) != 3:
                fy = [Y(0.14 + 0.7 * i / max(1, len(self.temps) - 1)) for i in range(len(self.temps))]
            for i, t in enumerate(self.temps):
                cx, cy = col_r, fy[i]
                bw, bd = TOWER_PITCH + RING_D + 4, RING_D + 4
                lw_, lh_ = label_size(str(t))
                box = (cx - bw / 2, cy - bd / 2 - lh_ - 9, cx + bw / 2, cy + bd / 2)
                ob = Obj("RET_%d" % (i + 1), box, zs[-1], "ret")
                ob.tower = (cx, cy, bw, bd, t)
                self.towers.append(ob)
                objs.append(ob)
        rank = {"patches": 0, "patches_b": 1, "pa": 2, "pa_fine": 3, "em": 4, "em_fine": 5, "ret": 6}
        self.objs = sorted(objs, key=lambda o: rank[o.section])   # = print order (stable)
        self.check_layout()

    @staticmethod
    def _grow(b, d):
        return (b[0] - d, b[1] - d, b[2] + d, b[3] + d)

    @staticmethod
    def _bbox(objs):
        return (min(o.box[0] for o in objs), min(o.box[1] for o in objs),
                max(o.box[2] for o in objs), max(o.box[3] for o in objs))

    def _em_block_h(self, n):
        size = float(self.s.opt["em_size"])
        rows = int(math.ceil(n / (2 if n > 3 else 1)))
        return rows * (size + 9.0) + (rows - 1) * 9.0 + 5.0 + 2.0

    def _em_block(self, cx, cy, labels, prefix, section):
        s = self.s
        n = len(labels)
        size = float(s.opt["em_size"])
        gap = 9.0
        cols = 2 if n > 3 else 1
        rows = int(math.ceil(n / cols))
        cell_w, cell_h = size, size + 9.0         # label tab below each cube
        tot_w = cols * cell_w + (cols - 1) * gap
        tot_h = rows * cell_h + (rows - 1) * gap
        cy -= 2.5        # room for the prime line behind the first cube
        out = []
        for i, lab in enumerate(labels):
            r, c = divmod(i, cols)
            x0 = cx - tot_w / 2 + c * (cell_w + gap)
            y_top = cy + tot_h / 2 - r * (cell_h + gap)
            cube = (x0, y_top - size, x0 + size, y_top)
            lw_, lh_ = label_size(lab)
            lab_xy = (x0 + size / 2 - lw_ / 2, y_top - size - 2.0 - lh_)
            box = (x0 - 1, lab_xy[1] - 1, x0 + size + 1, y_top + (6.0 if i == 0 else 1.0))
            ob = Obj("%s%d" % (prefix, i + 1), box, s.h1 + (em_layers(s, float(s.opt["em_height"]))[0][-1] - s.h1), section)
            ob.cube, ob.label, ob.lab_xy, ob.idx = cube, lab, lab_xy, i
            out.append(ob)
        return out

    def check_layout(self):
        s = self.s
        bx0, by0, bx1, by1 = s.bed
        for o in self.objs:
            x0, y0, x1, y1 = o.box
            if x0 < bx0 + 1 or y0 < by0 + 1 or x1 > bx1 - 1 or y1 > by1 - 1:
                raise SystemExit("ECAL: %s does not fit on the bed (%s)" % (o.name, o.box))
            if o.height > s.max_z - 5:
                raise SystemExit("ECAL: %s is taller than the printable height" % o.name)
        for i, a in enumerate(self.objs):
            for b in self.objs[i + 1:]:
                if overlap(a.box, b.box, 0.5):
                    raise SystemExit("ECAL: %s and %s overlap - too many test values for this bed. "
                                     "On a 375x388 bed: up to 6 EM cubes, ~30 PA patterns, 3 towers per "
                                     "pass (use fewer values or a coarser step)." % (a.name, b.name))
        # toolhead clearance: while printing an object, nothing taller than low_ok that was
        # printed EARLIER may be within the clearance radius of its footprint.
        order = [o.name for o in self.objs]
        R = s.clearance
        for i, o in enumerate(self.objs):
            for p in self.objs[:i]:
                if p.height <= s.low_ok or p.section == o.section:
                    continue
                if box_dist(o.box, p.box) < R:
                    raise SystemExit("ECAL: toolhead (radius %.0f mm) would hit %s while printing %s"
                                     % (R, p.name, o.name))
        # objects printed one at a time in the same section (towers)
        for i, o in enumerate(self.towers):
            for p in self.towers[:i]:
                if box_dist(o.box, p.box) < R:
                    raise SystemExit("ECAL: towers %s and %s are closer than the clearance radius %.0f mm"
                                     % (p.name, o.name, R))
        return order

    # ---------------- G-code ----------------
    def body(self):
        s = self.s
        wr = Writer(s)
        self.wr = wr
        g = wr.g
        g("; ===== ECAL session body (generated by ecal.py v%d) =====" % VERSION)
        g("G90")
        g("M83")
        g("G92 E0")
        # PA=-1: Orca does not set PA, so keep whatever the printer has now
        g("_ECAL_BEGIN VERSION=%d PA=%s EM=%.4f FW=%d PFR=%.4f" % (
            VERSION, fmt_val(s.pa_profile, 4) if s.pa_enabled else "-1", s.em_profile,
            1 if s.fw_retract else 0, _num(s.cfg.get("print_flow_ratio", "1")) or 1.0))
        wr.set_accel(s.a_travel)
        wr.retracted = False
        wr.z = 0.0

        # ---- 1. patches ----
        if self.patches:
            g("; ----- 1. FIRST LAYER SQUISH (Ellis) -----")
            g("M117 ECAL 1/4 first layer - babystep now")
            g("RESPOND MSG=\"ECAL 1/4: first-layer patches. Babystep Z while they print.\"")
            wr.fan(0)
            first = self.patches[0]
            wr.safe_travel(first.box[0], first.box[1] + 1)
            self._patch_round(wr, self.patches, prime=True)
            # after the last patch round, heat to the normal print temperature while you judge
            heat = ("M104 S%d" % self.t_print) if self.t_print != self.t_first else None
            if self.patches_b:
                self._cp(wr, "_ECAL_CP STEP=squish SKIP=%s" % ",".join(o.name for o in self.patches_b))
                self._patch_round(wr, self.patches_b, prime=False)
                if heat:
                    g(heat)
                self._cp(wr, "_ECAL_CP STEP=squish2 OBJ=%s" % self.patches_b[0].name)
            else:
                if heat:
                    g(heat)
                self._cp(wr, "_ECAL_CP STEP=squish2")

        # ---- 2. PA ----
        if self.pa_obj:
            g("; ----- 2. PRESSURE ADVANCE, pattern method (Ellis) -----")
            g("M117 ECAL 2/4 pressure advance")
            g("M109 S%d" % self.t_print)
            fan = float(s.opt["pa_fan"]) or s.fan_max
            vals = ",".join(fmt_val(v) for v in self.pa_vals)
            self._pa(wr, self.pa_obj, fan)
            skip = "SKIP=PA_FINE" if self.pa_fine_obj else ""
            self._cp(wr, "_ECAL_CP STEP=pa VALUES=%s %s" % (vals, skip))
            g("_ECAL_APPLY")
            if self.pa_fine_obj:
                self._pa(wr, self.pa_fine_obj, fan)
                self._cp(wr, "_ECAL_CP STEP=pa_fine OBJ=PA_FINE OFFSETS=%s"
                         % ",".join(fmt_val(d, 5) for d in self.pa_fine))
                g("_ECAL_APPLY")

        # ---- 3. EM ----
        if self.em_c:
            g("; ----- 3. EXTRUSION MULTIPLIER (Ellis) -----")
            g("M117 ECAL 3/4 extrusion multiplier")
            g("M109 S%d" % self.t_print)
            g("_ECAL_APPLY")
            fan = float(s.opt["em_fan"]) or s.fan_max
            self._em(wr, self.em_c, fan, lambda o: "M221 S%.2f" % (self.em_vals[o.idx] * 100))
            skip = ("SKIP=" + ",".join(o.name for o in self.em_f)) if self.em_f else ""
            self._cp(wr, "_ECAL_CP STEP=em VALUES=%s %s" % (",".join("%.4f" % v for v in self.em_vals), skip))
            g("_ECAL_APPLY")
            if self.em_f:
                self._em(wr, self.em_f, fan, lambda o: "_ECAL_EM_REL D=%s" % fmt_val(self.em_fine[o.idx], 5))
                self._cp(wr, "_ECAL_CP STEP=em_fine OBJ=%s OFFSETS=%s"
                         % (self.em_f[0].name, ",".join(fmt_val(d, 5) for d in self.em_fine)))
                g("_ECAL_APPLY")

        # ---- 4. retraction ----
        if self.towers:
            g("; ----- 4. RETRACTION towers (Ellis), one at a time, hottest first -----")
            g("M117 ECAL 4/4 retraction")
            # Ellis calls retraction tuning optional; also the moment to brush the nozzle clean
            self._cp(wr, "_ECAL_CP STEP=ret_ask SKIP=%s" % ",".join(o.name for o in self.towers))
            g("_ECAL_APPLY")
            fan = float(s.opt["ret_fan"]) or max(s.fan_max, 80.0)
            wr.ret_macro = not s.fw_retract
            for o in self.towers:
                self._tower(wr, o, fan)
            wr.ret_macro = False
        self._park(wr)
        g("_ECAL_END")
        if self.towers:
            g("_ECAL_CP STEP=ret ALL=%s TEMPS=%s RINGS=%s" % (",".join(o.name for o in self.towers),
                                                              ",".join(str(t) for t in self.temps),
                                                              ",".join(fmt_val(v, 3) for v in self.ret_vals)))
        else:
            g("_ECAL_CP STEP=summary")
        g("; ===== ECAL session end =====")
        return wr

    def _park(self, wr):
        bx0, by0, bx1, by1 = self.s.bed
        wr.safe_travel((bx0 + bx1) / 2, by1 - 10, clear=10.0)

    def _cp(self, wr, cmd):
        """Checkpoint: park first so PAUSE (and RESUME's return move) keeps the nozzle off the prints."""
        self._park(wr)
        wr.g(cmd)

    def _patch_round(self, wr, objs, prime):
        s = self.s
        for i, o in enumerate(objs):
            wr.safe_travel(o.box[0], o.box[1] + (1.0 if prime and i == 0 else 0.0), clear=2.0)
            obj_start(wr, o)
            if prime and i == 0:
                prime_line(wr, s, o.box[0], o.box[1] + 1.0, 25)
            wr.z_to(s.h1)
            wr.set_accel(s.a_first)
            patch_draw(wr, s, o)
            obj_end(wr, o)

    def _pa(self, wr, o, fan):
        s = self.s
        pat = o.pat
        wr.safe_travel(pat.sx, o.box[1] + 1)
        obj_start(wr, o)
        prime_line(wr, s, pat.sx, o.box[1] + 1.5, min(60.0, pat.osx))
        wr.fan(0)
        pat.draw(wr, fan)
        obj_end(wr, o)

    def _em(self, wr, cubes, fan, setter):
        s = self.s
        zs, kinds = em_layers(s, float(s.opt["em_height"]))
        density = float(s.opt["em_infill"])
        first = cubes[0]
        wr.safe_travel(first.cube[0] + 1, first.cube[3] + 2.0)
        obj_start(wr, first)
        prime_line(wr, s, first.cube[0] + 1, first.cube[3] + 2.0, 28)
        obj_end(wr, first)
        for li, (z, kind) in enumerate(zip(zs, kinds)):
            if li == s.fan_off_layers:
                wr.fan(fan)
            order = cubes if li % 2 == 0 else list(reversed(cubes))
            for o in order:
                obj_start(wr, o)
                wr.g(setter(o))
                if li == 0:
                    wr.travel(*o.cube[:2])
                wr.z_to(z)
                em_cube_layer(wr, s, o, li, z, kind, density)
                if li <= 1:
                    label_tab(wr, s, o.lab_xy[0], o.lab_xy[1], o.label, li)
                obj_end(wr, o)
        wr.max_h = max(wr.max_h, zs[-1])

    def _tower(self, wr, o, fan):
        s = self.s
        cx, cy, bw, bd, temp = o.tower
        base_h, zs = tower_geometry(s, len(self.ret_vals))
        lw_, lh_ = label_size(str(temp))
        lab = (cx - lw_ / 2, cy - bd / 2 - lh_ - 2)
        self._park(wr)
        wr.g("_ECAL_TEMP OBJ=%s S=%d" % (o.name, temp))
        wr.safe_travel(o.box[0] + 1, o.box[1] + 1)
        obj_start(wr, o)
        wr.fan(0)
        prime_line(wr, s, o.box[0] + 1, o.box[1] + 1, bw - 2)
        pa_ = (cx - TOWER_PITCH / 2, cy)
        pb_ = (cx + TOWER_PITCH / 2, cy)
        bx0, by0, bx1, by1 = cx - bw / 2, cy - bd / 2, cx + bw / 2, cy + bd / 2
        section = -1
        for li, z in enumerate(zs):
            h = s.h1 if li == 0 else s.h
            if li == s.fan_off_layers:
                wr.fan(fan)
            if li < BASE_LAYERS:
                wr.z_to(z)
                loops, edge = walls_rect(s, bx0, by0, bx1, by1, s.wall_loops, h, li == 0)
                print_walls(wr, s, loops, h)
                w = s.w_first if li == 0 else s.w_solid
                v = s.v_first_infill if li == 0 else s.v_solid
                a = s.a_first if li == 0 else s.a_solid
                print_fill(wr, s, bx0, by0, bx1, by1, edge, w, h, v, a, spacing(w, h),
                           45 if li % 2 == 0 else -45, s.ov_solid, "solid")
                if li <= 1:
                    label_tab(wr, s, lab[0], lab[1], str(temp), li)
                continue
            zrel = z - base_h - 1e-6
            sec = min(int(zrel // SECTION_H), len(self.ret_vals) - 1)
            ring = (zrel - sec * SECTION_H) < RING_LAYERS * s.h - 1e-6
            if sec != section:
                section = sec
                # change only while un-retracted (Klipper keeps the old unretract length otherwise)
                wr.unlift()
                wr.unretract()
                wr.g("; ring %d: retraction %s mm" % (sec + 1, fmt_val(self.ret_vals[sec], 3)))
                wr.g("_ECAL_RET_SET LEN=%s SPEED=%s" % (fmt_val(self.ret_vals[sec], 4),
                                                        fmt_val(float(s.opt["ret_speed"]), 2)))
                wr.no_retract = self.ret_vals[sec] <= 0
            wr.z_to(z)
            r = (RING_D if ring else PILLAR_D) / 2
            pair = (pa_, pb_) if li % 2 == 0 else (pb_, pa_)
            for n_, p in enumerate(pair):
                start = 0.0 if p is pa_ else 180.0   # seam on the outside face, away from the gap
                loops, edge = disc_loops(s, p[0], p[1], r, h, start)
                if n_ == 1:
                    first_pt = (loops if s.outer_first else list(reversed(loops)))[0][0][0]
                    wr.travel(*first_pt, force=True)
                print_walls(wr, s, loops, h)
                disc_fill(wr, s, p[0], p[1], edge, h, 45 if li % 2 == 0 else -45)
        wr.no_retract = False
        wr.max_h = max(wr.max_h, zs[-1])
        wr.unretract()
        obj_end(wr, o)
        wr.g("_ECAL_RET_RESTORE")

    def define_objects(self):
        lines = []
        for o in self.objs:
            cx, cy = o.center()
            lines.append("EXCLUDE_OBJECT_DEFINE NAME=%s CENTER=%.3f,%.3f POLYGON=%s" % (o.name, cx, cy, o.poly()))
        return lines

    def header(self):
        s = self.s
        h = ["; ECAL - Ellis tuning guide, combined session (ecal.py v%d)" % VERSION,
             "; Needs ellis_calibration.cfg (Klipper/Kalico) included in printer.cfg.",
             "; Order: squish -> PA -> EM -> retraction. Picks are applied to every later test.",
             "; PA values: %s" % ", ".join(fmt_val(v) for v in self.pa_vals),
             "; PA fine offsets: %s" % ", ".join(fmt_val(v, 5) for v in self.pa_fine),
             "; EM values: %s" % ", ".join("%.3f" % v for v in self.em_vals),
             "; EM fine offsets: %s" % ", ".join(fmt_val(v, 5) for v in self.em_fine),
             "; Retraction rings: %s mm @ %s mm/s, temps %s" % (", ".join(fmt_val(v, 3) for v in self.ret_vals),
                                                              s.opt["ret_speed"], ", ".join(map(str, self.temps))),
             "; Firmware retraction: %s, z-hop %.2f, toolhead clearance radius %.0f mm" % (
                 s.fw_retract, s.z_hop, s.clearance)]
        if s.h1 < 0.25 or s.w_first < 1.2 * s.nozzle:
            h.append("; NOTE: Ellis recommends first layer >= 0.25 mm and first-layer width >= 120 %% "
                     "(profile: %.2f mm, %.2f mm)" % (s.h1, s.w_first))
        return h


def overlap(a, b, gap=0.0):
    return not (a[2] + gap <= b[0] or b[2] + gap <= a[0] or a[3] + gap <= b[1] or b[3] + gap <= a[1])


def box_dist(a, b):
    dx = max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
    return math.hypot(dx, dy)


# --------------------------------------------------------------------------
# Orca G-code handling
# --------------------------------------------------------------------------
CFG_RE = re.compile(r"^;\s*([A-Za-z0-9_]+)\s*=\s?(.*)$")


def read_orca_config(lines):
    cfg = {}
    inside = False
    for ln in lines:
        t = ln.strip()
        if t == "; CONFIG_BLOCK_START":
            inside = True
            continue
        if t == "; CONFIG_BLOCK_END":
            inside = False
            continue
        if inside:
            m = CFG_RE.match(t)
            if m:
                cfg[m.group(1)] = m.group(2).strip()
    return cfg


def object_names(lines):
    """Object names as typed in Orca (from '; printing object NAME id:N copy K')."""
    names = []
    for ln in lines:
        if ln.startswith("; printing object "):
            name = ln[len("; printing object "):].rsplit(" id:", 1)[0].strip()
            if name not in names:
                names.append(name)
    return names


def find_marker(lines):
    """Return the ECAL object's name as typed in Orca, or None. The '; printing object'
    comment keeps '=' and ','; EXCLUDE_OBJECT_DEFINE names are sanitised, so they are only
    a fallback (options in the name are then lost)."""
    for name in object_names(lines):
        if name.upper().startswith(MARKER):
            return name
    for ln in lines:
        if ln.startswith("EXCLUDE_OBJECT_DEFINE NAME="):
            name = ln.split("NAME=", 1)[1].split(" ", 1)[0]
            if name.upper().startswith(MARKER):
                return MARKER
    return None


def options_from_name(name):
    out = {}
    # Orca names objects after the file, extension included ("ECAL temps=260,250.stl")
    name = re.sub(r"\.(stl|3mf|obj|step|stp|amf)$", "", name.strip(), flags=re.I)
    for tok in name.split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            k = k.lower().strip()
            if k in OPTIONS:
                out[k] = v.strip()
    return out


def split_orca(lines):
    """(head, start_gcode, tail): head up to/incl. the start-gcode role tag, the user's start
    G-code (everything until the first layer change), and the end G-code (from the last
    '; FEATURE: Custom' tag) up to the end of the file."""
    first_layer = next((i for i, l in enumerate(lines) if l.strip() in ("; CHANGE_LAYER", ";LAYER_CHANGE")), None)
    if first_layer is None:
        raise SystemExit("ECAL: no layer-change tag found - is this OrcaSlicer G-code?")
    custom = [i for i, l in enumerate(lines) if l.strip() in ("; FEATURE: Custom", ";TYPE:Custom")]
    starts = [i for i in custom if i < first_layer]
    ends = [i for i in custom if i > first_layer]
    if not starts or not ends:
        raise SystemExit("ECAL: could not find start/end G-code markers")
    s0 = starts[-1]
    e0 = ends[-1]
    return lines[:s0 + 1], lines[s0 + 1:first_layer], lines[e0:]


def build_from_orca(lines, extra_opts=None):
    cfg = read_orca_config(lines)
    name = find_marker(lines) or MARKER
    opts = options_from_name(name)
    opts.update(extra_opts or {})
    others = [n for n in object_names(lines) if not n.upper().startswith(MARKER)]
    if others:
        sys.stderr.write("ECAL: the session replaces the whole plate; these objects are not printed: %s\n"
                         % ", ".join(others))
    s = Settings(cfg, opts)
    ses = Session(s)
    ses.layout()
    head, start, tail = split_orca(lines)
    # drop Orca's own object definitions (ours replace them) and its first-line M73 stays
    head = [l for l in head if not l.startswith("EXCLUDE_OBJECT_DEFINE")]
    ins = next((i for i, l in enumerate(head) if l.strip() == "; EXECUTABLE_BLOCK_START"), 0) + 1
    defs = ["SET_GCODE_VARIABLE MACRO=_ECAL VARIABLE=loaded VALUE=1 ; fails fast if ellis_calibration.cfg is missing"]
    defs += ses.define_objects()
    head = head[:ins] + ses.header() + defs + head[ins:]
    wr = ses.body()
    out = head + start + wr.out + ["M106 S0"] + tail
    out = fix_stats(out, ses, cfg)
    return out, ses


def fix_stats(lines, ses, cfg):
    """Replace Orca's stats for the placeholder with the session's (Moonraker reads these)."""
    wr = ses.wr
    mm = wr.e_total
    cm3 = mm * ses.s.fil_area / 1000.0
    try:
        dens = _num(cfg.get("filament_density", "1.04"))
    except ValueError:
        dens = 1.04
    g = cm3 * dens
    t = int(wr.t_total)
    ts = "%dh %dm %ds" % (t // 3600, t % 3600 // 60, t % 60) if t >= 3600 else "%dm %ds" % (t // 60, t % 60)
    rep = [
        (re.compile(r"^; filament used \[mm\] ="), "; filament used [mm] = %.2f" % mm),
        (re.compile(r"^; filament used \[cm3\] ="), "; filament used [cm3] = %.2f" % cm3),
        (re.compile(r"^; filament used \[g\] ="), "; filament used [g] = %.2f" % g),
        (re.compile(r"^; total filament used \[g\] ="), "; total filament used [g] = %.2f" % g),
        (re.compile(r"^; estimated printing time \(normal mode\) ="),
         "; estimated printing time (normal mode) = %s" % ts),
        (re.compile(r"^; max_z_height:"), "; max_z_height: %.2f" % wr.max_h),
        (re.compile(r"^; total layer number:"), "; total layer number: %d" % ses.n_layers()),
        (re.compile(r"^; total layers count ="), "; total layers count = %d" % ses.n_layers()),
    ]
    out = []
    for ln in lines:
        for rx, new in rep:
            if rx.match(ln):
                ln = new
                break
        out.append(ln)
    return out


def build_standalone(cfg_overrides, opts, start=START_GCODE, end=END_GCODE):
    s = Settings(cfg_overrides, opts)
    ses = Session(s)
    ses.layout()
    wr = ses.body()
    out = ses.header() + [
        "SET_GCODE_VARIABLE MACRO=_ECAL VARIABLE=loaded VALUE=1 ; fails fast if ellis_calibration.cfg is missing"
    ] + ses.define_objects() + ["; ----- start G-code -----", start] + wr.out + ["; ----- end G-code -----", end]
    return out, ses


def write(path, lines):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(l.rstrip("\n") for l in lines) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("gcode", nargs="?", help="Orca post-processing: G-code file to rewrite in place")
    ap.add_argument("--from", dest="src", help="read profile + start/end G-code from this Orca G-code")
    ap.add_argument("-o", "--output", help="write the session to this file")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="override an Orca setting or ECAL option (repeatable)")
    ap.add_argument("--start", default=START_GCODE, help="stand-alone start G-code")
    ap.add_argument("--end", default=END_GCODE, help="stand-alone end G-code")
    a = ap.parse_args(argv)

    cfg, opts = {}, {}
    for kv in a.set:
        k, v = kv.split("=", 1)
        (opts if k in OPTIONS else cfg)[k] = v

    if a.gcode and not a.src and not a.output:
        # Orca post-processing mode: in place, only if the ECAL object is on the plate
        with open(a.gcode, encoding="utf-8", errors="replace") as f:
            lines = f.read().split("\n")
        if not find_marker(lines):
            return 0
        out, ses = build_from_orca(lines, opts)
        write(a.gcode, out)
        out_name = os.environ.get("SLIC3R_PP_OUTPUT_NAME")
        if out_name and "ecal" not in os.path.basename(out_name).lower():
            base, ext = os.path.splitext(out_name)
            with open(a.gcode + ".output_name", "w", encoding="utf-8") as f:
                f.write(base + "_ECAL" + ext)
        return 0

    if a.src or a.gcode:
        with open(a.src or a.gcode, encoding="utf-8", errors="replace") as f:
            lines = f.read().split("\n")
        if cfg:
            # apply --set overrides on top of the file's config block
            lines = lines + ["; CONFIG_BLOCK_START"] + ["; %s = %s" % kv for kv in cfg.items()] + ["; CONFIG_BLOCK_END"]
        out, ses = build_from_orca(lines, opts)
    else:
        out, ses = build_standalone(cfg, opts, a.start, a.end)
    path = a.output or "ecal_session.gcode"
    write(path, out)
    print("ECAL: wrote %s (%d lines, %.1f m filament, ~%.0f min of moves + heating and pauses)"
          % (path, len(out), ses.wr.e_total / 1000, ses.wr.t_total / 60))
    for w in ses.warn:
        print("ECAL warning:", w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
