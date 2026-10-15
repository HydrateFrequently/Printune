#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
r"""
Printune test-print generator: one heat-soaked session of tuning tests.

Generates a single Kalico/Klipper G-code file that prints these tests in order
on one bed, each depending on the values picked before it. The Tuning Wizard:

  1. First layer squish  - 25x25 mm patches scattered over the bed (live babystep)
  2. Pressure advance    - a row of frames, one per PA value (+ optional fine pass)
  3. Flow (extrusion multiplier) - 30x30x3 mm cubes (+ optional 0.5 % fine pass)
  4. Retraction          - towers at several temperatures, printed one at a time

Advanced tuning (option proc=advanced):

  1. Cooling             - overhang blocks, one per fan speed (fan %), then a cone whose layer
                           time falls with height (minimum layer time)
  2. Bridges             - strips bridging 20/40/60 mm between pillars, in three passes: fan,
                           then speed, then flow (Orca's external bridge settings)
  3. Max volumetric flow - a single-wall spiral tower, flow rising in bands with height

Between tests the printer pauses and the Printune page (or Mainsail/Fluidd)
shows one button per tested value. The picked value is applied to every later
test (PA via SET_PRESSURE_ADVANCE, flow via M221, retraction via
SET_RETRACTION) while the chamber stays hot. Needs printune.cfg included in
printer.cfg.

Usage
  On the printer: the Printune page / PRINTUNE menu runs this for you.

  Every way of running it needs your printer's geometry, measured by you (Printune assumes
  none of it and refuses to build a session without it):
      --toolhead-box=-X,+X,-Y,+Y   mm from the nozzle to the toolhead's edges (fans, ducts, probe)
      --gantry-height=MM           bed to the underside of the X gantry, nozzle at Z=0
      --bed-area=X0,Y0,X1,Y1       where the nozzle may print
      --nozzle-protrusion=MM       nozzle tip to the lowest other part of the toolhead (duct, probe, sock)
      --accept-risk                you measured them; no warranty, zero responsibility for damage
  (or the same as options: toolhead_box=... gantry_height=... bed_area=... nozzle_protrusion=...
  accept_risk=1)

  OrcaSlicer post-processing script (Process > Others > Post-processing scripts):
      /usr/bin/python3 /path/to/printune_gen.py --toolhead-box=... --gantry-height=... --bed-area=...
          --nozzle-protrusion=... --accept-risk;
      "C:\Path\To\python.exe" "C:\path\to\printune_gen.py" --toolhead-box=... (same options);   (Windows)
  then add Printune.stl to an empty plate and slice. Every setting (speeds, accels,
  widths, temps, retraction, PA, flow ratio, bed size, start/end G-code) is
  taken from the profiles used for that slice. Without an object named Printune...
  on the plate the script does nothing.

  Stand-alone, from an Orca G-code you already sliced (any object):
      python3 printune_gen.py --from path/to/sliced.gcode -o printune_session.gcode <geometry options>

  Stand-alone with built-in defaults:
      python3 printune_gen.py -o printune_session.gcode <geometry options>

Options can also be written into the Printune object's name in Orca, e.g.
  "Printune pa=0-0.08_0.005 em=0.92-1.00_0.02 ret=0-1_0.1 temps=260,250,240 skip=patches"
"""

import argparse
import hashlib
import math
import os
import re
import shlex
import sys

VERSION = 9            # must match variable_version in printune.cfg

# Every test in print order, and the procedures. A test moves between the Tuning Wizard and
# Advanced tuning by moving its key between the two lists (the plugin, the menu and the page
# read them from here).
# The tallest test (the max flow tower) prints last: the gantry never passes over it.
ORDER = ("squish", "pa", "em", "ret", "cool", "bridge", "mvs")
PROCS = {"wizard": ("squish", "pa", "em", "ret"), "advanced": ("cool", "bridge", "mvs")}
# print order of the objects' sections (the gantry rule needs to know what prints first)
SECTION_RANK = {"patches": 0, "patches_b": 1, "pa": 2, "pa_fine": 3, "em": 4, "em_fine": 5, "ret": 6,
                "cool": 7, "cone": 8, "br_fan": 9, "br_speed": 10, "br_flow": 11, "mvs": 12}
SKIP_KEY = {"squish": "patches", "pa": "pa", "em": "em", "ret": "ret", "mvs": "mvs", "cool": "cool",
            "bridge": "bridge"}
TITLE_WORD = {"wizard": "Printune", "advanced": "Advanced"}
MARKER = "PRINTUNE"

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
    "filament_type": "ABS",
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
    "bridge_speed": "25",
    "bridge_flow": "1",
    "thick_bridges": "0",
    "bridge_line_width": "100%",
    "bridge_density": "100%",
    "bridge_acceleration": "50%",
    "gcode_flavor": "klipper",
    "machine_max_speed_x": "500",
    "machine_max_speed_y": "500",
    "machine_max_acceleration_x": "20000",
    "machine_max_acceleration_y": "20000",
}

# Filament types for the on-printer wizard: starting points, not tuned values.
# t = nozzle range to test (°C), bed (°C), fan (%), vol = max volumetric speed (mm3/s),
# soak = wants a heat-soaked enclosure, parent = Orca system preset the result inherits,
# mvs = max flow tower range when it differs from OPTIONS["mvs"],
# cool = cooling test: fan speeds (%), cone layer time at the bottom (s), block layer time (s).
COOL_LOW = ("20,40,60,80", 26, 15)     # filaments that want little fan (ABS: ~15 s minimum layer time)
COOL_HIGH = ("40,70,100", 16, 8)
COOL_NOFAN = ("0,20,40,60,80", 26, 15)  # PC and PA often print best with no fan at all
FILAMENTS = {
    "PLA":  dict(t=(200, 220), bed=60,  fan=100, vol=15, soak=0, parent="Generic PLA @System", cool=COOL_HIGH),
    "PETG": dict(t=(230, 250), bed=80,  fan=40,  vol=12, soak=0, parent="Generic PETG @System", cool=COOL_HIGH),
    "ABS":  dict(t=(240, 260), bed=100, fan=20,  vol=20, soak=1, parent="Generic ABS @System", cool=COOL_LOW),
    "ASA":  dict(t=(250, 270), bed=100, fan=20,  vol=20, soak=1, parent="Generic ASA @System", cool=COOL_LOW),
    "PC":   dict(t=(260, 280), bed=110, fan=0,   vol=15, soak=1, parent="Generic PC @System", cool=COOL_NOFAN),
    "PA":   dict(t=(250, 270), bed=80,  fan=0,   vol=12, soak=1, parent="Generic PA @System", cool=COOL_NOFAN),
    "TPU":  dict(t=(220, 240), bed=50,  fan=60,  vol=4,  soak=0, parent="Generic TPU @System",
                mvs="1-12/0.5", cool=COOL_HIGH),
}


def tower_temps(tmin, tmax):
    """Tower temperatures spread over the range, hottest first, at most 4 towers."""
    tmin, tmax = int(round(tmin)), int(round(tmax))
    if tmax < tmin:
        tmin, tmax = tmax, tmin
    span = tmax - tmin
    if span == 0:
        return [tmax]
    n = min(4, span // 10 + 1) if span >= 10 else 2
    temps = [tmax - span * i / (n - 1) for i in range(n)]
    return [int(t) if i in (0, n - 1) else int(5 * round(t / 5.0)) for i, t in enumerate(temps)]


def mid_temp(tmin, tmax):
    """Temperature for the tests before the towers: the middle of the range, to 5 °C."""
    return int(5 * round((tmin + tmax) / 10.0))


# Stand-alone start/end G-code (Orca mode uses the slice's own).
START_GCODE = "PRINT_START BED=100 HOTEND=250 CHAMBER=0"
END_GCODE = "PRINT_END"

# Test options (not Orca settings). All can be set on the command line or in the object name.
OPTIONS = {
    "proc": "wizard",           # wizard (first layer, PA, flow, retraction) or advanced (cooling, bridges, max flow)
    "set_ret": 0,               # 1: set firmware retraction to the profile's retraction at the start
    "skip": "auto",             # comma list of: patches, pa, em, ret, cool, bridge, mvs; auto = the other procedure's
    "patches_extra": 4,         # optional 2nd round of patches (0 = none)
    "pa": "0-0.08/0.005",       # direct drive range
    "pa_fine": "0.005/0.001",   # +-range/step around the pick, 0 = no fine pass
    "em": "",                   # "" = profile flow ratio -4..+4 % in 2 % steps
    "em_fine": "0.01/0.005",    # +-range/step around the pick, 0 = no fine pass
    "em_infill": 30,            # sparse infill under the top surface
    "em_size": 30.0,            # 30x30x3 mm cubes
    "em_height": 3.0,
    "ret": "0-1.0/0.1",         # direct drive: 0.1 mm steps, max 1 mm
    "ret_speed": 30,            # retract and unretract speed
    "temps": "",                # "" = nozzle temp +10, 0, -10
    "ret_fan": 0,               # 0 = max(fan_max_speed, 80)
    "em_fan": 0,                # 0 = fan_max_speed
    "pa_fan": 0,                # 0 = fan_max_speed
    "mvs": "5-30/1",            # max volumetric flow tower: flow per band, mm3/s
    "mvs_band": 3.0,            # height of each flow band, mm
    "mvs_fan": 0,               # 0 = fan_max_speed
    "cool_fans": "",            # cooling blocks' fan speeds, %; "" = by filament type (PLA 40,70,100; ABS 20,40,60,80; PC, PA 0,20,40,60,80)
    "cool_angles": "30-70/10",  # overhang angles of each block, bottom to top, degrees from vertical
    "cool_time": 0,             # cone layer time at the bottom, s; 0 = by filament type (PLA 16, ABS 26)
    "cool_pad": 0,              # blocks: layers slowed down to this layer time, s; 0 = by type (PLA 8, ABS 15)
    "bridge_spans": "20,40,60", # bridge test: spans of each strip, left to right, mm (longest dropped if the bed is small)
    "bridge_fans": "",          # fan pass, %; "" = 4 steps from the fan (the cooling pick, else bridge_base) to 100
    "bridge_base": -1,          # the fan the bridges start from, %; -1 = the profile's fan_max_speed
    "bridge_speeds": "",        # speed pass, mm/s; "" = the profile's bridge_speed x 0.6, 1, 1.5, 2
    "bridge_flows": "0.85-1/0.05",  # flow pass: Orca's bridge_flow
    "mcr": 0.5,                 # the printer's minimum_cruise_ratio (the plugin sets it)
    "zv": 0,                    # max_z_velocity, 0 = max_velocity (the plugin sets it)
    "za": 0,                    # max_z_accel, 0 = max_accel (the plugin sets it)
    "scv": 5.0,                 # the printer's square_corner_velocity (the plugin sets it)
    # Machine geometry: REQUIRED, never assumed. The session refuses to build without all five.
    "toolhead_box": "",         # mm from the nozzle to the toolhead's edges: -X,+X,-Y,+Y (fans, ducts, probe included)
    "gantry_height": "",        # mm from the bed to the underside of the X gantry, nozzle at Z=0
    "bed_area": "",             # x0,y0,x1,y1: where the nozzle may print (e.g. the bed mesh area)
    "nozzle_protrusion": "",    # mm from the nozzle tip up to the lowest other part of the toolhead
    "accept_risk": 0,           # 1 = you measured these yourself and accept the risk (no warranty)
    "margin": 12.0,             # keep-out from the printable-area edge
}


# --------------------------------------------------------------------------
# Machine geometry (the user's measurements; Printune assumes none of it)
# --------------------------------------------------------------------------
GEOMETRY_KEYS = ("toolhead_box", "gantry_height", "bed_area", "nozzle_protrusion", "accept_risk")
# A finished print may sit under the toolhead when it is at least this much lower than the nozzle
# protrusion. The nozzle never goes below the first layer height, so the real gap is this margin
# plus the first layer (>= 0.4 mm); the margin covers a measurement off by a little and blobs or
# ridges on a print's top.
LOW_MARGIN = 0.2
GEOMETRY_HELP = (
    "Printune makes no assumptions about your printer's geometry: measure it yourself and pass "
    "toolhead_box=-X,+X,-Y,+Y (mm from the nozzle to the furthest point of the toolhead on each side: "
    "fans, ducts, probe and carriage included, and the X beam if it reaches further in Y), "
    "gantry_height=MM (from the bed to the underside of the X gantry with the nozzle at Z=0), "
    "bed_area=X0,Y0,X1,Y1 (where the nozzle may print, e.g. your bed mesh area), "
    "nozzle_protrusion=MM (from the nozzle tip up to the lowest other part of the toolhead: duct, probe, "
    "silicone sock) and accept_risk=1 "
    "(you measured these yourself; Printune and its authors take zero responsibility for the generated "
    "G-code, collisions or any damage, see the GPL-3.0 sections 15 and 16). "
    "Command line: --toolhead-box=-X,+X,-Y,+Y --gantry-height=MM --bed-area=X0,Y0,X1,Y1 "
    "--nozzle-protrusion=MM --accept-risk. "
    "Orca post-processing: put the same options on the script line before the ';', or in the Printune "
    "object's name (toolhead_box=... gantry_height=... bed_area=... nozzle_protrusion=... accept_risk=1).")


def _nums(text, n, key):
    try:
        v = [float(t) for t in str(text).replace(" ", "").split(",") if t != ""]
    except ValueError:
        v = []
    if len(v) != n or not all(math.isfinite(x) for x in v):
        raise ValueError("%s needs %d numbers separated by commas (got '%s')" % (key, n, text))
    return v


def parse_toolhead_box(text):
    """'-30,30,-40,25' -> (-30, 30, -40, 25). The order is -X, +X, -Y, +Y; signs are optional
    (each number is the distance from the nozzle to that side)."""
    v = _nums(text, 4, "toolhead_box")
    xn, xp, yn, yp = (round(abs(x), 2) for x in v)
    if min(xn, xp, yn, yp) <= 0 or max(xn, xp, yn, yp) > 300:
        raise ValueError("toolhead_box: each side must be more than 0 and at most 300 mm (got '%s')" % text)
    return (-xn, xp, -yn, yp)


def parse_bed_area(text):
    x0, y0, x1, y1 = (round(x, 2) + 0.0 for x in _nums(text, 4, "bed_area"))
    if x1 - x0 < 50 or y1 - y0 < 50:
        raise ValueError("bed_area is x0,y0,x1,y1 with x1 > x0 and y1 > y0, at least 50 mm each way "
                         "(got '%s')" % text)
    return (x0, y0, x1, y1)


def parse_gantry_height(text):
    v = round(_nums(text, 1, "gantry_height")[0], 2)
    if not 1 <= v <= 1000:
        raise ValueError("gantry_height must be 1 to 1000 mm (got '%s')" % text)
    return v


def parse_nozzle_protrusion(text):
    v = round(_nums(text, 1, "nozzle_protrusion")[0], 2) + 0.0
    if not 0 <= v <= 50:
        raise ValueError("nozzle_protrusion must be 0 to 50 mm (got '%s')" % text)
    return v


def geometry_hash(bed, box, gantry, protrusion):
    """A short fingerprint of the measured geometry: the acceptance and every session carry it, so
    a changed value (a hand edit of printer.cfg too) shows."""
    f = lambda v: "%.2f" % (round(float(v), 2) + 0.0)
    text = "bed=%s;box=%s;gantry=%s;protrusion=%s" % (
        ",".join(f(v) for v in bed), ",".join(f(v) for v in box), f(gantry), f(protrusion))
    return hashlib.sha1(text.encode("ascii")).hexdigest()[:10]


def parse_geometry(opt):
    """(bed_area, toolhead_box, gantry_height, nozzle_protrusion) from the options, or SystemExit
    saying what is missing."""
    missing = [k for k in GEOMETRY_KEYS[:4] if not str(opt.get(k, "")).strip()]
    accepted = str(opt.get("accept_risk", "0")).strip().lower() in ("1", "true", "yes")
    if not accepted:
        missing.append("accept_risk")
    if missing:
        raise SystemExit("Printune: refusing to build a session: missing %s.\n%s"
                         % (", ".join(missing), GEOMETRY_HELP))
    try:
        return (parse_bed_area(opt["bed_area"]), parse_toolhead_box(opt["toolhead_box"]),
                parse_gantry_height(opt["gantry_height"]), parse_nozzle_protrusion(opt["nozzle_protrusion"]))
    except ValueError as e:
        raise SystemExit("Printune: %s.\n%s" % (e, GEOMETRY_HELP))


def fmt_box(b):
    return "%g,%g,%g,%g" % tuple(b)


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
            raise SystemExit("Printune: G-code flavor must be Klipper (got %s)" % c.get("gcode_flavor"))

        if "clearance" in cfg or "clearance" in opts:
            raise SystemExit("Printune: the clearance option is gone: give the toolhead's size as "
                             "toolhead_box=-X,+X,-Y,+Y instead.\n" + GEOMETRY_HELP)
        if "low_ok" in cfg or "low_ok" in opts:
            raise SystemExit("Printune: the low_ok option is gone: give the nozzle protrusion as "
                             "nozzle_protrusion=MM instead.\n" + GEOMETRY_HELP)
        # the user's measured geometry; nothing is assumed
        area, self.box, self.gantry, self.protrusion = parse_geometry(self.opt)
        self.area = tuple(area)
        self.geom_hash = geometry_hash(area, self.box, self.gantry, self.protrusion)
        # finished prints at most this tall may sit under the toolhead (see LOW_MARGIN)
        self.low_ok = self.protrusion - LOW_MARGIN
        self.notes = []
        if "printable_area" in cfg and str(cfg["printable_area"]).strip():
            # the slicer's (or orca_settings') printable area: the session stays inside both
            pts = [tuple(map(float, p.split("x"))) for p in c["printable_area"].split(",")]
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            pa = (min(xs), min(ys), max(xs), max(ys))
            self.bed = (max(pa[0], area[0]), max(pa[1], area[1]), min(pa[2], area[2]), min(pa[3], area[3]))
            if self.bed[2] - self.bed[0] < 50 or self.bed[3] - self.bed[1] < 50:
                raise SystemExit("Printune: bed_area %s and the printable area %s hardly overlap"
                                 % (fmt_box(area), fmt_box(pa)))
            if self.bed != tuple(area):
                self.notes.append("the printable area %s cuts the bed area %s down to %s"
                                  % (fmt_box(pa), fmt_box(area), fmt_box(self.bed)))
        else:
            self.bed = tuple(area)
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
        self.w_top = self.nozzle  # flow test: top layer line width 100 %

        self.v_first = _num(c["initial_layer_speed"])
        self.v_first_infill = _num(c["initial_layer_infill_speed"])
        self.v_outer = _num(c["outer_wall_speed"])
        self.v_inner = _num(c["inner_wall_speed"])
        self.v_solid = _num(c["internal_solid_infill_speed"])
        self.v_sparse = _num(c["sparse_infill_speed"])
        self.v_top = _num(c["top_surface_speed"])
        self.v_travel = _num(c["travel_speed"])
        # Orca's overhang wall speeds (10/25/50/75 % of the line width unsupported); 0 = wall speed
        self.v_overhang = [0.0] * 4
        if _first(c.get("enable_overhang_speed", "1")) in ("1", "true"):
            for i in range(4):
                v = _first(c.get("overhang_%d_4_speed" % (i + 1), "0"))
                r = float(v[:-1]) / 100.0 * self.v_outer if v.endswith("%") else float(v or 0)
                self.v_overhang[i] = r if r > 0 else 0.0
        self.v_slow_min = _num(c.get("slow_down_min_speed", "10")) or 10.0
        # Orca's external bridge settings (the bridge test tunes speed and flow; width, density and
        # thick bridges stay as the profile has them)
        self.v_bridge = _num(c["bridge_speed"]) or 25.0
        self.br_flow = _num(c["bridge_flow"]) or 1.0
        self.thick_bridges = _first(c["thick_bridges"]) in ("1", "true")
        bw = _first(c["bridge_line_width"])
        self.w_bridge = max(0.0, float(bw[:-1]) / 100.0 * self.nozzle if bw.endswith("%") else float(bw or 0))
        self.br_density = (_num(c["bridge_density"]) / 100.0) or 1.0
        self.mvs = _num(c["filament_max_volumetric_speed"]) or 1e9
        # the printer's own limits (the plugin sets them from [printer])
        self.vmax = min(_num(c["machine_max_speed_x"]), _num(c["machine_max_speed_y"])) or 1e9
        self.scv = float(self.opt["scv"]) or 5.0
        self.amax = min(_num(c["machine_max_acceleration_x"]), _num(c["machine_max_acceleration_y"])) or 1e9
        self.mcr = float(self.opt["mcr"])
        self.zv = float(self.opt["zv"]) or self.vmax
        self.za = float(self.opt["za"]) or self.amax
        self.ftype = str(c.get("filament_type", "")).split(";")[0].strip().strip('"').upper()

        a0 = _num(c["default_acceleration"])
        self.a_default = a0
        self.a_outer = self._acc("outer_wall_acceleration", a0)
        self.a_inner = self._acc("inner_wall_acceleration", a0)
        self.a_top = self._acc("top_surface_acceleration", a0)
        self.a_solid = self._acc("internal_solid_infill_acceleration", a0)
        self.a_sparse = self._acc("sparse_infill_acceleration", a0)
        self.a_first = self._acc("initial_layer_acceleration", a0)
        self.a_travel = self._acc("travel_acceleration", a0)
        ba = _first(c["bridge_acceleration"])       # % of the outer wall acceleration
        ba = float(ba[:-1]) / 100.0 * self.a_outer if ba.endswith("%") else float(ba or 0)
        self.a_bridge = ba if ba > 0 else a0

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
        self.margin = float(o["margin"])
        self.proc = str(o["proc"]).strip().lower()
        if self.proc not in PROCS:
            raise SystemExit("Printune: proc must be one of %s" % ", ".join(PROCS))
        if str(o["skip"]).strip().lower() == "auto":
            self.skip = {SKIP_KEY[t] for t in ORDER if t not in PROCS[self.proc]}
        else:
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
        self.ret_macro = False      # use _PRINTUNE_R/_PRINTUNE_U (retraction test without fw retraction)
        self.no_retract = False     # ring 1 of the retraction test (0 mm)
        self.e_total = 0.0
        self.t_total = 0.0          # rough print time, s (moves only, no heating or pauses)
        self.max_h = 0.0            # tallest thing printed so far
        self.paths = []             # (x0,y0,x1,y1,z,w,kind) for checks/renders
        self.vscale = 1.0           # cooling blocks: extrusion moves slowed down to stretch a layer
        self.vmin = 10.0            # ... but not below this

    _STATE = ("x", "y", "z", "retracted", "hopped", "accel", "e_total", "t_total", "max_h")

    def snapshot(self):
        return dict((k, getattr(self, k)) for k in self._STATE), len(self.out), len(self.paths)

    def restore(self, snap):
        st, n_out, n_paths = snap
        for k, v in st.items():
            setattr(self, k, v)
        del self.out[n_out:]
        del self.paths[n_paths:]

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
            self.g("_PRINTUNE_R")
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
            self.g("_PRINTUNE_U")
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

    def line(self, x, y, w, h, v, accel=None, kind="ext", cap=True):
        self.unlift()
        self.unretract()
        if accel:
            self.set_accel(accel)
        L = math.hypot(x - self.x, y - self.y)
        if L < 1e-6:
            return
        a = self.area(w, h)
        if cap:
            v = min(v, self.s.mvs / a)
        if self.vscale < 1.0:
            v = max(v * self.vscale, min(v, self.vmin))
        e = L * a / self.s.fil_area
        self.e_total += e
        self.g("G1 X%.3f Y%.3f E%.5f F%d" % (x, y, e, v * 60))
        self._t(L, v)
        self.paths.append((self.x, self.y, x, y, self.z, w, kind))
        self.x, self.y = x, y
        self.max_h = max(self.max_h, self.z)

    def bridge(self, x, y, a, v, accel, w):
        """Extrude a bridge line to (x, y) with cross-section a mm2 at v mm/s (the bridge test
        checks v against the flow limit)."""
        self.unlift()
        self.unretract()
        self.set_accel(accel)
        L = math.hypot(x - self.x, y - self.y)
        if L < 1e-6:
            return
        e = L * a / self.s.fil_area
        self.e_total += e
        self.g("G1 X%.3f Y%.3f E%.5f F%d" % (x, y, e, round(v * 60)))
        self._t(L, v)
        self.paths.append((self.x, self.y, x, y, self.z, w, "bridge"))
        self.x, self.y = x, y
        self.max_h = max(self.max_h, self.z)

    def spiral(self, x, y, z, a, v, scale=1.0, kind="mvs", w=0.0):
        """Extrude to (x, y, z) at v mm/s with cross-section a mm2, no flow cap (max flow test)."""
        L = math.hypot(x - self.x, y - self.y)
        e = L * a * scale / self.s.fil_area
        self.e_total += e
        # 4 decimals: rounding to 0.001 on 0.26 mm segments bends them enough for Kalico to slow down
        self.g("G1 X%.4f Y%.4f Z%.4f E%.5f F%d" % (x, y, z, e, round(v * 60)))
        self.t_total += L / v          # one continuous move at constant speed
        self.paths.append((self.x, self.y, x, y, z, w, kind))
        self.x, self.y, self.z = x, y, z
        self.max_h = max(self.max_h, z)
        # what the printer reads (for the corner check)
        return (round(x, 4), round(y, 4), round(z, 4), round(e, 5), round(v * 60) / 60.0)

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


# ---- Digit font and pressure advance test (Printune's own design) ----

TEXT_H = 4.0          # text box height, mm (glyph body is TEXT_H - 2*_PAD)
_PAD = 0.3            # margin inside the text box so a ~0.45 mm stroke stays inside it
_GAP = 0.7            # gap between characters, mm

# width of each glyph body (mm) and its strokes in a unit cell, (0,0) = bottom left
_FONT = {
    "0": (2.4, [[(0.3, 0), (0.7, 0), (1, 0.15), (1, 0.85), (0.7, 1), (0.3, 1),
                 (0, 0.85), (0, 0.15), (0.3, 0), (0.7, 0)]]),
    "1": (1.8, [[(0, 0.75), (0.55, 1), (0.55, 0)], [(0, 0), (1, 0)]]),
    "2": (2.4, [[(0, 0.8), (0.25, 1), (0.75, 1), (1, 0.8), (1, 0.6), (0, 0), (1, 0)]]),
    "3": (2.4, [[(0, 0.85), (0.25, 1), (0.75, 1), (1, 0.85), (1, 0.65), (0.7, 0.52),
                 (1, 0.38), (1, 0.15), (0.75, 0), (0.25, 0), (0, 0.15)],
                [(0.3, 0.52), (0.7, 0.52)]]),
    "4": (2.4, [[(0.75, 0), (0.75, 1), (0, 0.32), (1, 0.32)]]),
    "5": (2.4, [[(1, 1), (0.05, 1), (0, 0.55), (0.7, 0.6), (1, 0.42), (1, 0.15),
                 (0.75, 0), (0.25, 0), (0, 0.15)]]),
    "6": (2.4, [[(0.95, 0.88), (0.75, 1), (0.25, 1), (0, 0.85), (0, 0.15), (0.25, 0),
                 (0.75, 0), (1, 0.15), (1, 0.42), (0.75, 0.57), (0.25, 0.57), (0, 0.42)]]),
    "7": (2.4, [[(0, 1), (1, 1), (0.3, 0)]]),
    "8": (2.4, [[(0.3, 0.55), (0.08, 0.7), (0.08, 0.86), (0.3, 1), (0.7, 1), (0.92, 0.86),
                 (0.92, 0.7), (0.7, 0.55), (0.3, 0.55), (0, 0.4), (0, 0.15), (0.25, 0),
                 (0.75, 0), (1, 0.15), (1, 0.4), (0.7, 0.55)]]),
    "9": (2.4, [[(0.05, 0.12), (0.25, 0), (0.75, 0), (1, 0.15), (1, 0.85), (0.75, 1),
                 (0.25, 1), (0, 0.85), (0, 0.58), (0.25, 0.43), (0.75, 0.43), (1, 0.58)]]),
    ".": (0.7, [[(0, 0), (1, 0)]]),
    "-": (1.8, [[(0, 0.5), (1, 0.5)]]),
    "+": (2.2, [[(0, 0.5), (1, 0.5)], [(0.5, 0.22), (0.5, 0.78)]]),
    " ": (1.2, []),
}


def text_len(txt):
    """Length of the text box along the reading direction, mm."""
    chars = [c for c in str(txt) if c in _FONT]
    if not chars:
        return 0.0
    return sum(_FONT[c][0] for c in chars) + _GAP * (len(chars) - 1) + 2 * _PAD


def _text_strokes(txt):
    """Polylines in text-box coordinates (u along the text, v up), box = text_len x TEXT_H."""
    body = TEXT_H - 2 * _PAD
    u0 = _PAD
    out = []
    for c in str(txt):
        if c not in _FONT:
            continue
        gw, strokes = _FONT[c]
        for st in strokes:
            out.append([(u0 + x * gw, _PAD + y * body) for x, y in st])
        u0 += gw + _GAP
    return out


def draw_text(wr, x0, y0, txt, w, h, v, front=False, accel=None):
    """Draw txt with the stroke font.  front=True: text runs +X, up is +Y, box is
    text_len(txt) x TEXT_H from (x0, y0).  front=False: text runs +Y, glyph up is -X,
    box is TEXT_H x text_len(txt) from (x0, y0) (reads from the right side of the bed)."""
    if front:
        T = lambda u, q: (x0 + u, y0 + q)
    else:
        T = lambda u, q: (x0 + TEXT_H - q, y0 + u)
    for st in _text_strokes(txt):
        pts = [T(u, q) for u, q in st]
        wr.travel(*pts[0])
        for p in pts[1:]:
            wr.line(p[0], p[1], w, h, v, accel, "ext")


# ---------------------------------------------------------------------------
# Speed-step frame PA test
# ---------------------------------------------------------------------------
def _pitch(w, h):
    """Centre distance of adjacent extrusions (rounded-rectangle bead model)."""
    return w - h * (1 - math.pi / 4)


class PATest:
    FRAME_W = 5.0        # frame outer size across X, mm
    FRAME_L = 24.0       # frame outer size along Y (the long, speed-stepped edges), mm
    GAP = 2.5            # clear space between frames, mm
    WALLS = 2
    LAYERS = 4           # frame layers (first one on the bed)
    SLOW_FRAC = 0.2      # slow part at each end of a long edge
    LABEL_MARGIN = 1.0   # strip margin around the labels, mm
    LINE_RATIO = 1.125

    def __init__(self, s, values, labels, cx, cy, setter):
        self.s = s
        self.values = values
        self.labels = labels
        self.setter = setter          # fn(i) -> G-code line that sets PA for frame i
        n = self.n = len(values)
        self.lw = s.nozzle * self.LINE_RATIO
        self.pitch = self.FRAME_W + self.GAP
        self.osx = n * self.FRAME_W + (n - 1) * self.GAP
        lab_len = max([text_len(t) for t in labels] + [0.0])
        self.strip_d = lab_len + 2 * self.LABEL_MARGIN
        self.osy = self.strip_d + self.FRAME_L - 0.5 * self.lw   # frames overlap the strip by half a line
        self.sx = cx - self.osx / 2
        self.sy = cy - self.osy / 2
        self.box = (self.sx - 1, self.sy - 1, self.sx + self.osx + 1, self.sy + self.osy + 1)
        self.height = s.h1 + (self.LAYERS - 1) * s.h

    # -- geometry -----------------------------------------------------------
    def frame_rect(self, i):
        x0 = self.sx + i * self.pitch
        y0 = self.sy + self.strip_d - 0.5 * self.lw
        return x0, y0, x0 + self.FRAME_W, y0 + self.FRAME_L

    def _loops(self, i, w, h):
        """Wall centre-line rectangles, innermost first (outer wall printed last)."""
        x0, y0, x1, y1 = self.frame_rect(i)
        p = _pitch(w, h)
        loops = []
        for k in range(self.WALLS):
            d = w / 2 + k * p
            loops.append((x0 + d, y0 + d, x1 - d, y1 - d))
        return loops[::-1]

    # -- printing -----------------------------------------------------------
    def _frame_layer(self, wr, i, w, h, v_slow, v_fast, acc, stepped):
        for (x0, y0, x1, y1) in self._loops(i, w, h):
            xm = (x0 + x1) / 2
            # start mid front edge, run counter-clockwise; the seam stays on a short edge
            wr.travel(xm, y0)
            wr.line(x1, y0, w, h, v_slow, acc)
            self._edge(wr, x1, y0, x1, y1, w, h, v_slow, v_fast, acc, stepped)
            wr.line(x0, y1, w, h, v_slow, acc)
            self._edge(wr, x0, y1, x0, y0, w, h, v_slow, v_fast, acc, stepped)
            wr.line(xm - 0.15 * w, y0, w, h, v_slow, acc)   # small gap closes the seam without a blob

    def _edge(self, wr, xa, ya, xb, yb, w, h, v_slow, v_fast, acc, stepped):
        if not stepped:
            wr.line(xb, yb, w, h, v_slow, acc)
            return
        f = self.SLOW_FRAC
        for t, v in ((f, v_slow), (1 - f, v_fast), (1.0, v_slow)):
            wr.line(xa + (xb - xa) * t, ya + (yb - ya) * t, w, h, v, acc)

    def _strip(self, wr):
        s = self.s
        w, h = s.w_first, s.h1
        x0, y0 = self.sx + w / 2, self.sy + w / 2
        x1, y1 = self.sx + self.osx - w / 2, self.sy + self.strip_d - w / 2
        wr.travel(x0, y0)
        for p in ((x1, y0), (x1, y1), (x0, y1), (x0, y0)):
            wr.line(p[0], p[1], w, h, s.v_first, s.a_first)
        # serpentine fill along X inside the loop
        p = _pitch(w, h)
        y = y0 + p
        left = True
        xa, xb = x0 + p, x1 - p
        wr.travel(xa, y)
        while y <= y1 - p + 1e-6:
            xe = xb if left else xa
            wr.line(xe, y, w, h, s.v_first, s.a_first)
            ny = y + p
            if ny <= y1 - p + 1e-6:
                wr.line(xe, ny, w, h, s.v_first, s.a_first)
            y, left = ny, not left

    def _labels(self, wr):
        s = self.s
        for i in range(self.n):
            fx0, _, fx1, _ = self.frame_rect(i)
            lx = (fx0 + fx1) / 2 - TEXT_H / 2
            ly = self.sy + self.LABEL_MARGIN
            draw_text(wr, lx, ly, self.labels[i], self.lw, s.h, s.v_first, front=False, accel=s.a_first)

    def draw(self, wr, fan_pct):
        s = self.s
        v_fast = s.v_outer
        v_slow = max(5.0, min(s.v_first, 0.3 * v_fast))
        wr.fan(0)
        wr.g(self.setter(0))
        for L in range(self.LAYERS):
            z = s.h1 + L * s.h
            h = s.h1 if L == 0 else s.h
            wr.z_to(z)
            if L == 0:
                self._strip(wr)
            if L == 1:
                wr.fan(fan_pct)
                wr.g(self.setter(0))
                self._labels(wr)
            order = range(self.n) if L % 2 == 0 else range(self.n - 1, -1, -1)
            for i in order:
                wr.g(self.setter(i))
                if L == 0:
                    self._frame_layer(wr, i, s.w_first, h, s.v_first, s.v_first, s.a_first, False)
                else:
                    self._frame_layer(wr, i, self.lw, h, v_slow, v_fast, s.a_outer, True)
        wr.max_h = max(wr.max_h, self.height)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------



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


# ---- 2. PA test: PATest (defined with the font above) --------------------


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
    else:  # top surface: 100 % width, monotonic lines, top speed
        print_fill(wr, s, x0, y0, x1, y1, edge, s.w_top, h, s.v_top, s.a_top,
                   spacing(s.w_top, h), ang, s.ov_solid, "top")


def label_tab(wr, s, x0, y0, txt, layer):
    """2-layer label: filled first-layer tab, digits on layer 2. Box = (len+3) x 7 mm."""
    tw, th = text_len(txt) + 3.0, 7.0
    if layer == 0:
        loops, edge = walls_rect(s, x0, y0, x0 + tw, y0 + th, 2, s.h1, True)
        print_walls(wr, s, loops, s.h1)
        print_fill(wr, s, x0, y0, x0 + tw, y0 + th, edge, s.w_first, s.h1, s.v_first, s.a_first,
                   spacing(s.w_first, s.h1), 45, s.ov_solid, "anchor")
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


# ---- 5. Max volumetric flow tower ----------------------------------------------
# A single-wall spiral (vase-style, Z rises continuously) round tower on a square
# base. The flow steps up in bands of fixed height; speed = flow / line cross-section.
# A small outward bump at the front marks each band that crosses a multiple of 5 mm3/s.
MVS_R = 30.0            # wall centre-line radius, mm (60 mm tower)
MVS_BASE = 3.0          # the square base reaches this far past the wall, mm
MVS_H_RATIO = 0.75      # layer height / nozzle (0.30 mm on a 0.4 nozzle)
MVS_W_RATIO = 1.5       # line width / nozzle (0.60 mm on a 0.4 nozzle)
MVS_BUMP = 0.6          # marker bump, mm outward (less if the acceleration can't take it)
MVS_BUMP_MIN = 0.25     # smallest bump still worth printing
MVS_BUMP_L = 20.0       # marker arc length, mm: long and shallow, so no corner slows the print
MVS_BUMP_REVS = 3       # marker height, revolutions
MVS_MARK_DEG = 270.0    # markers face the front (-Y)
MVS_START_DEG = 90.0    # the spiral starts at the back
MVS_SEG_DEG = 1.0       # one segment per degree (0.52 mm): Kalico's corner rule allows ~520 mm/s at SCV 5
MVS_BUMP_SEG_DEG = 0.5  # finer over the bumps
TYPICAL_H, TYPICAL_W = 0.2, 0.45   # "typical" print line for the mm/s equivalent
SQRT2_1 = math.sqrt(2.0) - 1.0


def kalico_junction_v2(d0, d1, len0, len1, er0, er1, accel, scv, icv=1.0):
    """Highest speed^2 Kalico allows through the joint of two moves: toolhead.Move.calc_junction
    (junction deviation and approximated centripetal) and the extruder's instant_corner_v rule.
    d0, d1: unit XYZ directions; len: move lengths; er: E per mm of move."""
    lim = []
    if er0 != er1:
        lim.append((icv / abs(er1 - er0)) ** 2)
    jcos = -(d0[0] * d1[0] + d0[1] * d1[1] + d0[2] * d1[2])
    sin_t = math.sqrt(max(0.5 * (1.0 - jcos), 0.0))
    cos_t = math.sqrt(max(0.5 * (1.0 + jcos), 0.0))
    if 1.0 - sin_t > 0.0 and cos_t > 0.0:
        r_jd = sin_t / (1.0 - sin_t)
        jd = scv * scv * SQRT2_1 / accel          # Kalico: junction_deviation = scv^2 (sqrt2 - 1) / accel
        qtan = 0.25 * sin_t / cos_t
        lim += [r_jd * jd * accel, 2.0 * len0 * accel * qtan, 2.0 * len1 * accel * qtan]
    return min(lim) if lim else 1e18


def circle_v_limit(r, seg_deg, accel, scv):
    """Kalico's corner speed limit on a circle of radius r drawn in seg_deg segments."""
    a = math.radians(seg_deg)
    L = 2 * r * math.sin(a / 2)
    d0, d1 = (1.0, 0.0, 0.0), (math.cos(a), math.sin(a), 0.0)
    return math.sqrt(kalico_junction_v2(d0, d1, L, L, 0, 0, accel, scv))


def kalico_plan(moves, scv, mcr=0.5, icv=1.0, vmax=1e9, zv=1e9, za=1e9):
    """Time of each move as Kalico's toolhead plans it: toolhead.Move (z and extrude-only limits),
    Move.calc_junction (junction deviation, approximated centripetal, extruder instant_corner_v)
    and LookAheadQueue.flush (with minimum_cruise_ratio smoothing), flushed once at the end.
    moves: [(start (x, y, z, e), end (x, y, z, e), speed mm/s, accel mm/s2)]; a negative speed
    is a dwell (G4) of -speed seconds: the toolhead stops before it. Returns [s]."""
    n = len(moves)
    kin, L, ax, cr2, acc, jd, dv2, sdv2 = [], [], [], [], [], [], [], []
    dwell = {}
    for i, (sp, ep, speed, accel) in enumerate(moves):
        if speed < 0:
            dwell[i] = -speed
            kin.append(False)
            L.append(0.0)
            ax.append((0.0, 0.0, 0.0, 1.0))
            cr2.append(0.0)
            acc.append(accel)
            jd.append(0.0)
            dv2.append(0.0)
            sdv2.append(0.0)
            continue
        d = [ep[i] - sp[i] for i in range(4)]
        md = math.sqrt(d[0] * d[0] + d[1] * d[1] + d[2] * d[2])
        a, v = accel, min(speed, vmax)
        if md < 1e-9:
            md = abs(d[3])
            k = False
            a, v = accel, speed          # extrude only: the extruder's own limits, not modelled
            r = (0.0, 0.0, 0.0, 1.0 if d[3] >= 0 else -1.0)
        else:
            k = True
            r = tuple(c / md for c in d)
        c2 = v * v
        sd = 2.0 * md * accel * (1.0 - mcr)
        if k and d[2]:
            ratio = md / abs(d[2])
            c2 = min(c2, (zv * ratio) ** 2)
            a = min(a, za * ratio)
        de = 2.0 * md * a
        kin.append(k)
        L.append(md)
        ax.append(r)
        cr2.append(c2)
        acc.append(a)
        jd.append(scv * scv * SQRT2_1 / accel)
        dv2.append(de)
        sdv2.append(min(sd, de))
    ms2 = [0.0] * n
    msm2 = [0.0] * n
    for i in range(1, n):
        if not (kin[i] and kin[i - 1]):
            continue
        r0, r1 = ax[i - 1], ax[i]
        dr = r1[3] - r0[3]
        m = min(cr2[i], cr2[i - 1], ms2[i - 1] + dv2[i - 1], (icv / abs(dr)) ** 2 if dr else cr2[i])
        jc = -(r1[0] * r0[0] + r1[1] * r0[1] + r1[2] * r0[2])
        st = math.sqrt(max(0.5 * (1.0 - jc), 0.0))
        ct = math.sqrt(max(0.5 * (1.0 + jc), 0.0))
        if 1.0 - st > 0.0 and ct > 0.0:
            rjd = st / (1.0 - st)
            qt = 0.25 * st / ct
            m = min(m, rjd * jd[i] * acc[i], rjd * jd[i - 1] * acc[i - 1], dv2[i] * qt, dv2[i - 1] * qt)
        ms2[i] = m
        msm2[i] = min(m, msm2[i - 1] + sdv2[i - 1])
    t = [0.0] * n

    def junction(i, s2, c2, e2):
        a = acc[i]
        sv, cv, ev = math.sqrt(s2), math.sqrt(c2), math.sqrt(e2)
        ad, dd = (c2 - s2) * 0.5 / a, (c2 - e2) * 0.5 / a
        t[i] = ((ad / ((sv + cv) * 0.5) if ad > 0 else 0.0) + (L[i] - ad - dd) / cv
                + (dd / ((ev + cv) * 0.5) if dd > 0 else 0.0)) if cv > 0 else 0.0

    delayed = []
    nxt_end = nxt_sm = peak = 0.0
    for i in range(n - 1, -1, -1):
        reach = nxt_end + dv2[i]
        st2 = min(ms2[i], reach)
        reach_sm = nxt_sm + sdv2[i]
        sm = min(msm2[i], reach_sm)
        if sm < reach_sm:
            if sm + sdv2[i] > nxt_sm or delayed:
                peak = min(cr2[i], (sm + reach_sm) * 0.5)
                if delayed:
                    mc = peak
                    for j, s2, e2 in reversed(delayed):
                        mc = min(mc, s2)
                        junction(j, min(s2, mc), mc, min(e2, mc))
                    delayed = []
            c2 = min((st2 + reach) * 0.5, cr2[i], peak)
            junction(i, min(st2, c2), c2, min(nxt_end, c2))
        else:
            delayed.append((i, st2, nxt_end))
        nxt_end, nxt_sm = st2, sm
    for i, T in dwell.items():
        t[i] = T
    return t


_GRX = re.compile(r"([XYZEF])(-?[\d.]+)")


def gcode_moves(lines, pos=(0.0, 0.0, 0.0), accel=3000.0, feed=50.0, fw=(0.5, 35.0)):
    """The moves of a stretch of Printune G-code (G90, M83), for kalico_plan. Also returns, for
    each line, how many moves come before it. fw: firmware retraction (length, speed) for G10/G11."""
    x, y, z = pos
    moves, before = [], []
    for ln in lines:
        before.append(len(moves))
        t = ln.split(";", 1)[0].strip()
        if not t:
            continue
        if t.startswith("SET_VELOCITY_LIMIT"):
            m = re.search(r"ACCEL=([\d.]+)", t)
            if m:
                accel = float(m.group(1))
            continue
        if t.startswith("G4 "):
            m = re.search(r"P([\d.]+)", t)
            moves.append(((x, y, z, 0.0), (x, y, z, 0.0), -float(m.group(1)) / 1000.0 if m else 0.0, accel))
            continue
        if t in ("G10", "G11"):
            e = -fw[0] if t == "G10" else fw[0]
            moves.append(((x, y, z, 0.0), (x, y, z, e), fw[1], accel))
            continue
        if not (t.startswith("G1 ") or t.startswith("G0 ")):
            continue
        d = dict((k, float(v)) for k, v in _GRX.findall(t))
        if "F" in d:
            feed = d["F"] / 60.0
        nx, ny, nz, e = d.get("X", x), d.get("Y", y), d.get("Z", z), d.get("E", 0.0)
        if (nx, ny, nz) != (x, y, z) or e:
            moves.append(((x, y, z, 0.0), (nx, ny, nz, e), feed, accel))
        x, y, z = nx, ny, nz
    return moves, before


class MVSTower:
    def __init__(self, s, flows, band_h):
        self.s = s
        self.h = round(MVS_H_RATIO * s.nozzle, 3)
        self.w = round(MVS_W_RATIO * s.nozzle, 3)
        self.area = Writer.area(self.w, self.h)
        self.base_h = s.h1 + (BASE_LAYERS - 1) * s.h
        self.band_h = band_h
        self.flows = flows
        self.top = self.base_h + len(flows) * band_h
        v = self.flows[-1] / self.area
        # bump as big as the acceleration allows at the top speed (curvature = 1/R + A (2 pi / L)^2 / 2)
        k_max = s.amax / (1.1 * v * v) - 1.0 / MVS_R
        self.bump = max(MVS_BUMP_MIN, min(MVS_BUMP, 2 * k_max * (MVS_BUMP_L / (2 * math.pi)) ** 2))
        self.k_max = 1.0 / MVS_R + self.bump * (2 * math.pi / MVS_BUMP_L) ** 2 / 2
        self.moves = []

    def marks(self):
        """Band indexes whose flow passes a multiple of 5 mm3/s (the first band has none)."""
        f = self.flows
        return {i for i in range(1, len(f)) if math.floor(f[i] / 5.0 + 1e-6) > math.floor(f[i - 1] / 5.0 + 1e-6)}

    def accel(self):
        """Enough acceleration for the tightest curve (a bump) at the top speed, within the printer's limit."""
        s = self.s
        need = 1.1 * (self.flows[-1] / self.area) ** 2 * self.k_max
        return min(s.amax, max(s.a_outer, need)), need

    def _bump_d(self, deg):
        """Arc distance (mm) from the marker centre."""
        return ((deg - MVS_MARK_DEG + 180.0) % 360.0 - 180.0) * math.pi / 180.0 * MVS_R

    def _radius(self, deg, zrel_band, band, marks):
        if band not in marks or zrel_band >= MVS_BUMP_REVS * self.h:
            return MVS_R
        d = self._bump_d(deg)
        if abs(d) >= MVS_BUMP_L / 2:
            return MVS_R
        return MVS_R + self.bump * (1 + math.cos(2 * math.pi * d / MVS_BUMP_L)) / 2

    def draw(self, wr, cx, cy):
        h, nb = self.h, len(self.flows)
        marks = self.marks()
        fine = int(round(360.0 / MVS_BUMP_SEG_DEG))           # grid steps per revolution
        every = int(round(MVS_SEG_DEG / MVS_BUMP_SEG_DEG))
        n = int(round((self.top - self.base_h) / h * fine))
        near = MVS_BUMP_L / 2 + 2 * MVS_R * math.radians(MVS_SEG_DEG)
        pt = lambda deg, r: (cx + r * math.cos(math.radians(deg)), cy + r * math.sin(math.radians(deg)))
        x, y = pt(MVS_START_DEG, MVS_R)
        wr.travel(x, y, force=True)
        wr.z_to(self.base_h)
        wr.unretract()
        self.accel_used = self.accel()[0]
        wr.set_accel(self.accel_used)      # after the travel, which uses the travel acceleration
        self.moves = [(round(x, 4), round(y, 4), round(self.base_h, 4), 0.0, 0.0, -1)]
        k0 = 0
        for k in range(1, n + 1):
            deg = MVS_START_DEG + 360.0 * k / fine
            zb = (k - 0.5) / fine * h
            band = min(int(zb / self.band_h), nb - 1)
            bumpy = band in marks and abs(self._bump_d(deg % 360.0)) < near
            if k % every and not bumpy and k != n:
                continue
            revs = k / float(fine)
            z = self.base_h + revs * h
            zm = self.base_h + (k + k0) / 2.0 / fine * h       # the segment's middle decides its band
            band = min(int((zm - self.base_h) / self.band_h), nb - 1)
            r = self._radius(deg % 360.0, zm - self.base_h - band * self.band_h, band, marks)
            # first revolution: the gap under the nozzle grows from 0 to h, so does the flow
            scale = min(1.0, (k + k0) / 2.0 / fine)
            self.moves.append(wr.spiral(*pt(deg, r), z, self.area, self.flows[band] / self.area, scale,
                                        w=self.w) + (band,))
            k0 = k
        # one flat revolution at the top, flow tapering to 0, for a level top edge
        per_rev = int(round(360.0 / MVS_SEG_DEG))
        for k in range(1, per_rev + 1):
            deg = MVS_START_DEG + 360.0 * (n / float(fine) + k / float(per_rev))
            self.moves.append(wr.spiral(*pt(deg, MVS_R), self.top, self.area, self.flows[-1] / self.area,
                                        1.0 - (k - 0.5) / per_rev, w=self.w) + (nb,))

    def check_corners(self, scv, icv=1.0):
        """Run Kalico's junction rule over the moves as written; returns the bands where a
        corner would make Kalico go slower than the band's speed: {band: (slowest, wanted) mm/s}."""
        out = {}
        mv = self.moves
        prev = None
        for i in range(1, len(mv)):
            x0, y0, z0 = mv[i - 1][:3]
            x1, y1, z1, e, v, band = mv[i]
            dx, dy, dz = x1 - x0, y1 - y0, z1 - z0
            L = math.sqrt(dx * dx + dy * dy + dz * dz)
            if L < 1e-9:
                continue
            cur = ((dx / L, dy / L, dz / L), L, e / L, v, band)
            if prev is not None and band < len(self.flows):
                v2 = kalico_junction_v2(prev[0], cur[0], prev[1], cur[1], prev[2], cur[2],
                                        self.accel_used, scv, icv)
                want = min(prev[3], cur[3])
                if v2 < (want * 0.999) ** 2:
                    lo = out.get(band, (1e9, want))[0]
                    out[band] = (min(lo, math.sqrt(v2)), want)
            prev = cur
        return out


# ---- 5. Cooling: the fan from overhang blocks, the minimum layer time from a cone ---------
# Part A: one block per fan speed, each printed alone with its fan. The block's front leans out
# more every COOL_BAND_LAYERS layers (30, 40 ... 70 degrees from vertical by default); a groove on
# both sides marks each step. Each layer is slowed down (as Orca does) to the filament's usual
# minimum layer time, so the fan is judged at a realistic layer time, not on a tiny hot block.
# The outer wall keeps its own speed (Orca's overhang speed on the front, if the profile has one):
# the inner walls and the fill slow down, and if that is not enough the nozzle waits the rest
# COOL_PARK mm behind the block.
# Part B: a round cone printed alone at one constant speed and the picked fan, nothing slows it.
# Its radius shrinks so that the layer time falls by the same amount every layer (the side curves
# in like a bullet); a small bump on the front marks every 2 s of layer time. Each layer is one
# Archimedean spiral from the edge in to the centre, then a travel back out: the edge is printed
# once per layer, so the time between two passes there is the layer time.
COOL_W = 20.0           # block width (X), mm
COOL_D = 8.0            # block depth (Y), mm
COOL_BAND_LAYERS = 6    # layers per overhang angle
COOL_GROOVE = 0.4       # side groove where the angle steps up, mm
COOL_BASE = 2.0         # the base plate reaches this far past the block (more under the overhang)
COOL_PARK = 22.0        # a short layer waits out its time this far behind the block, mm
CONE_V = 60.0           # cone speed, mm/s (lower if the outer wall speed or the flow limit is lower)
CONE_MARK_H = 1.2       # height for every 2 s of layer time, mm
CONE_STEP = 2.0         # a marker every 2 s
CONE_TOP = 1.0          # the cone ends at about this layer time, s
CONE_BUMP = 0.5         # marker bump, mm outward
CONE_BUMP_L = 10.0      # marker bump length, mm (shorter near the top)
CONE_MARK_DEG = 270.0   # markers face the front (-Y)
CONE_START_DEG = 90.0   # every layer starts at the back (+Y)
CONE_SEG = 0.8          # spiral segment length, mm
CONE_CHORD = 0.02       # largest chord error, mm


class CoolTest:
    def __init__(self, s, fans, angles, t0, pad):
        self.s = s
        self.fans = [int(round(f)) for f in fans]
        self.angles = angles
        self.t0 = t0
        self.pad = pad
        # block layers: (z, layer height, front offset, groove)
        self.layers = [(s.h1 + li * s.h, s.h1 if li == 0 else s.h, 0.0, False) for li in range(BASE_LAYERS)]
        z, off = self.layers[-1][0], 0.0
        for b, ang in enumerate(angles):
            for k in range(COOL_BAND_LAYERS):
                z += s.h
                off += s.h * math.tan(math.radians(ang))
                self.layers.append((z, s.h, off, b > 0 and k == 0))
        self.shift, self.block_h = off, z
        # cone
        self.w, self.h = s.w_inner, s.h
        self.area = Writer.area(self.w, self.h)
        self.p = spacing(self.w, self.h)
        self.v = min(CONE_V, s.v_outer, s.vmax, s.mvs / self.area)
        self.acc = min(s.a_outer, s.amax)
        self.v_travel = min(s.v_travel, s.vmax)
        self.per2 = max(2, int(round(CONE_MARK_H / s.h)))       # layers per 2 s
        self._plan()

    def plan_args(self):
        s = self.s
        return dict(scv=s.scv, mcr=s.mcr, vmax=s.vmax, zv=s.zv, za=s.za)

    # ---- cone geometry ----
    def _bump(self, th, r):
        lb = min(CONE_BUMP_L, 1.5 * r)
        d = ((th - CONE_MARK_DEG + 180.0) % 360.0 - 180.0) * math.pi / 180.0 * r
        return (CONE_BUMP * (1 + math.cos(2 * math.pi * d / lb)) / 2 if abs(d) < lb / 2 else 0.0), lb, d

    def spiral(self, R, inward, th0, bump=False):
        """One cone layer around (0, 0): a spiral from radius R in to the centre (inward) or from
        the centre out to R, counter-clockwise from angle th0 (deg). The outermost turn carries
        the marker bump. Returns the points and the end angle."""
        p, rc = self.p, self.p / 2
        span = max(0.0, (R - rc) / p * 2 * math.pi)
        pts, phi = [], 0.0
        while True:
            r = R - p * phi / (2 * math.pi) if inward else rc + p * phi / (2 * math.pi)
            th = th0 + math.degrees(phi)
            near = False
            if bump and ((inward and phi < 2 * math.pi) or (not inward and phi > span - 2 * math.pi)):
                b, lb, d = self._bump(th, r)
                r += b
                near = abs(d) < lb / 2 + CONE_SEG
            pts.append((r * math.cos(math.radians(th)), r * math.sin(math.radians(th))))
            if phi >= span:
                break
            rr = max(r, rc)
            dphi = min(CONE_SEG / rr, 2 * math.acos(max(-1.0, 1 - CONE_CHORD / rr)), math.pi / 6)
            if near:
                dphi = min(dphi, min(CONE_BUMP_L, 1.5 * rr) / 16 / rr)
            phi = min(span, phi + dphi)
        return pts, th0 + math.degrees(span)

    def _t(self, R):
        """Time of one layer of radius R on its own (starting and ending at rest), s: the travel
        from the centre out to the edge, rising one layer, then the spiral in."""
        pts, _ = self.spiral(R, True, CONE_START_DEG)
        h = self.h
        mv = [((pts[-1][0], pts[-1][1], 0, 0), (pts[0][0], pts[0][1], h, 0), self.v_travel, self.acc)]
        mv += [((a[0], a[1], h, 0), (b[0], b[1], h, math.hypot(b[0] - a[0], b[1] - a[1]) * self.area / self.s.fil_area),
                self.v, self.acc) for a, b in zip(pts, pts[1:])]
        return sum(kalico_plan(mv, **self.plan_args()))

    def _plan(self):
        """Radius of every cone layer, so the layer time falls by 2 s every per2 layers."""
        dt = CONE_STEP / self.per2
        targets = []
        while self.t0 - len(targets) * dt >= CONE_TOP - 1e-6:
            targets.append(self.t0 - len(targets) * dt)
        rmax = math.sqrt(self.t0 * self.p * self.v / math.pi) * 1.3 + 2.0
        grid = [0.5 * (rmax / 0.5) ** (i / 48.0) for i in range(49)]
        tt = [self._t(R) for R in grid]

        def inv(t):
            for i in range(1, len(grid)):
                if tt[i] >= t:
                    f = (t - tt[i - 1]) / max(1e-9, tt[i] - tt[i - 1])
                    return math.sqrt(grid[i - 1] ** 2 + f * (grid[i] ** 2 - grid[i - 1] ** 2))
            return grid[-1]
        radii = [inv(t) for t in targets]
        self.marks = {}
        for k, t in enumerate(targets):
            if k >= 3 and abs(t / CONE_STEP - round(t / CONE_STEP)) < 1e-6:
                # markers sit exactly on their time: refine the radius
                R = radii[k]
                for _ in range(4):
                    tr = self._t(R)
                    if abs(tr - t) < 0.01:
                        break
                    R *= math.sqrt(t / tr)
                radii[k] = R
                # layer k+1 takes t; the marker goes on layer k+2, which is laid on layer k+1's
                # edge t seconds after that edge was printed
                if k + 2 <= len(targets):
                    self.marks[k + 2] = int(round(t))
        self.radii = [radii[0]] + radii          # layer 0 (first layer) has the radius of layer 1
        self.targets = [None] + targets
        self.top = self.s.h1 + len(targets) * self.s.h
        self.R = max(self.radii) + CONE_BUMP
        self.cone_times = []

    def z(self, k):
        return self.s.h1 + k * self.s.h

    def draw_cone(self, wr, cx, cy, fan_cmd):
        """Every layer the same way: from the edge at CONE_START_DEG spiral in to the centre, then
        travel back out (rising one layer) for the next. So the edge, where the markers are, is
        passed once per layer and the time between passes is the layer time."""
        s = self.s
        th = CONE_START_DEG
        w0, h0 = s.w_first, s.h1
        a0 = Writer.area(w0, h0)
        for k, R in enumerate(self.radii):
            if k == s.fan_off_layers:
                wr.g(fan_cmd)
            pts, _ = self.spiral(R, True, th, k in self.marks or (k - 1) in self.marks)
            pts = [(cx + x, cy + y) for x, y in pts]
            if k == 0:
                wr.travel(*pts[0], force=True)
                wr.z_to(self.z(0))
                wr.unretract()
                wr.set_accel(min(s.a_first, s.amax))
                self.l0 = (len(wr.out), (wr.x, wr.y, wr.z), wr.accel)
                wr.g("; cone layer 0")
                for x, y in pts[1:]:
                    wr.spiral(x, y, self.z(0), a0, min(s.v_first, self.v), kind="cone", w=w0)
                continue
            if k == 1:
                wr.set_accel(self.acc)
            wr.g("; cone layer %d: %.2f s%s" % (k, self.targets[k],
                                                 (", marker %d s" % self.marks[k]) if k in self.marks else ""))
            # back out to the edge over the layer just printed, rising to the new layer (no
            # retraction: the extruder's own moves are not in the layer-time plan)
            z1 = self.z(k)
            x, y = pts[0]
            wr.g("G1 X%.4f Y%.4f Z%.4f F%d" % (x, y, z1, round(self.v_travel * 60)))
            wr.paths.append((wr.x, wr.y, x, y, z1, 0.0, "travel"))
            wr.t_total += math.hypot(x - wr.x, y - wr.y) / self.v_travel
            wr.x, wr.y, wr.z = x, y, z1
            for x, y in pts[1:]:
                wr.spiral(x, y, z1, self.area, self.v, 1.0, kind="cone", w=self.w)
        wr.max_h = max(wr.max_h, self.top)


def cone_layer_times(lines, pos, accel, plan):
    """Layer times of a cone from its G-code (lines from its '; cone layer 0' comment on):
    [(layer, target s or None, marker s or None, time s, naive length/speed s)]."""
    mv, before = gcode_moves(lines, pos, accel)
    t = kalico_plan(mv, **plan)
    starts = []
    for i, ln in enumerate(lines):
        m = re.match(r"; cone layer (\d+)(?:: ([\d.]+) s)?(?:, marker (\d+) s)?", ln)
        if m:
            starts.append((before[i], int(m.group(1)), m.group(2) and float(m.group(2)),
                           m.group(3) and int(m.group(3))))
    out = []
    for j, (i0, k, tgt, mk) in enumerate(starts):
        i1 = starts[j + 1][0] if j + 1 < len(starts) else len(mv)
        naive = sum(math.dist(a[:3], b[:3]) / v for a, b, v, _ in mv[i0:i1])
        out.append((k, tgt, mk, sum(t[i0:i1]), naive))
    return out


# ---- Bridges: strips that bridge 20/40/60 mm between pillars ----------------------------
# One block per pass: pillar walls across the block, and a row of strips, front to back, one per
# value, each bridging every gap between the walls. The walls print layer by layer, then the
# bridge layer strip by strip (each with its own fan, speed or flow), then one solid layer on top
# of each strip. Three passes (fan, speed, flow), each keeping the picks before it.
BR_PILLAR_W = 5.0       # pillar wall thickness along the bridge (X), mm
BR_D = 5.0              # strip width (Y), mm
BR_GAP = 3.0            # between strips (Y), mm
BR_PILLAR_H = 4.0       # pillar height: room under the bridge to sag, mm
BR_ANCHOR = 2.0         # bridge lines reach this far onto each pillar, mm
BR_COVER = 1            # solid layers on the bridge
BR_FANS_N = 4           # fan pass strips when bridge_fans is not given
BR_SPEED_F = (0.6, 1.0, 1.5, 2.0)   # speed pass: the profile's bridge speed times these
BR_MODES = ("fan", "speed", "flow")


def bridge_flow(s, ratio):
    """(cross-section mm2, line width mm, line spacing mm) of an external bridge line at Orca's
    bridge_flow = ratio, exactly as LayerRegion::bridging_flow and Flow compute it:
    thick_bridges on: a round thread of diameter (bridge_line_width or the nozzle) x sqrt(ratio),
      spaced at the diameter + 0.05 mm;
    off: the normal rounded-rectangle line (bridge_line_width, else the internal solid infill
      width, at the layer height h, area A, spacing w - h(1 - pi/4)) through
      Flow::with_cross_section(A x ratio):
        ratio > 1: same spacing, the line grows in height (h' = A ratio / spacing);
        ratio < 1: the line gets narrower, w' = w - A (1 - ratio) / h, and its spacing is
          recomputed (w' - h (1 - pi/4)), so the lines move closer together;
        w' <= h: Orca makes a w = h = sqrt(A ratio / pi) line at the old spacing, and that
          line's area (Flow::mm3_per_mm) is pi/4 of its square, i.e. A ratio / 4.
    Either way the spacing is divided by bridge_density; the printer's flow (M221) applies on top."""
    if s.thick_bridges:
        d = (s.w_bridge or s.nozzle) * math.sqrt(ratio) if ratio > 0 else (s.w_bridge or s.nozzle)
        return math.pi * d * d / 4.0, d, (d + 0.05) / s.br_density
    w, h = s.w_bridge or s.w_solid, s.h
    a0, sp0 = h * spacing(w, h), spacing(w, h)      # Flow::mm3_per_mm, rounded_rectangle_spacing
    an = a0 * ratio
    eps = 1e-4                                      # libslic3r EPSILON
    if an > a0 + eps:
        h2 = an / sp0
        return an, sp0 + h2 * (1 - math.pi / 4), sp0 / s.br_density
    if an < a0 - eps:
        w2 = w - (a0 - an) / h
        if w2 > h:
            return an, w2, spacing(w2, h) / s.br_density
        d = math.sqrt(an / math.pi)
        return d * d * math.pi / 4.0, d, sp0 / s.br_density
    return a0, w, sp0 / s.br_density


class BridgeTest:
    def __init__(self, s, spans, fans, speeds, flows, base, v_ref, pad):
        self.s = s
        self.spans = spans
        self.fans = fans            # [] = from the fan at print time (_PRINTUNE_BRIDGE_FANS)
        self.n_fans = len(fans) or BR_FANS_N
        self.speeds = speeds
        self.flows = flows
        self.base = base            # the fan the bridges start from when nothing was picked, %
        self.v_ref = v_ref          # bridge speed of the fan and flow passes, mm/s
        self.pad = pad              # pillar layers are slowed down to take at least this long, s
        n = max(2, int(math.ceil((BR_PILLAR_H - s.h1) / s.h - 1e-6)) + 1)
        self.zs = [s.h1 + i * s.h for i in range(n)]            # pillar layers
        self.zb = self.zs[-1] + s.h                             # the bridge layer
        self.zc = [self.zb + (k + 1) * s.h for k in range(BR_COVER)]
        self.height = self.zc[-1]
        self.length = self.strip_length(spans)

    @staticmethod
    def strip_length(spans):
        return (len(spans) + 1) * BR_PILLAR_W + sum(spans)

    @staticmethod
    def depth(n):
        return n * BR_D + (n - 1) * BR_GAP

    def labels(self, mode):
        if mode == "fan":
            return [str(i + 1) for i in range(self.n_fans)]
        if mode == "speed":
            return [fmt_val(v, 1) for v in self.speeds]
        return [fmt_val(f * 100, 1) for f in self.flows]

    def pillars(self, o):
        """Pillar walls of block o, left to right, across all its strips."""
        x, out = o.px, []
        y0, y1 = o.strips[0], o.strips[-1] + BR_D
        for k in range(len(self.spans) + 1):
            out.append((x, y0, x + BR_PILLAR_W, y1))
            if k < len(self.spans):
                x += BR_PILLAR_W + self.spans[k]
        return out

    def draw_bridges(self, wr, o, i, ratio, v):
        """The bridge layer of strip i: for each span, straight lines across the gap from pillar to
        pillar, joined on the pillars (serpentine)."""
        s = self.s
        a, w, sp = bridge_flow(s, ratio)
        n = max(1, int((BR_D - w) / sp + 1e-6) + 1)
        yc = o.strips[i] + BR_D / 2
        ys = [yc - (n - 1) * sp / 2 + j * sp for j in range(n)]
        pil = self.pillars(o)
        wr.g("; bridges, strip %d: flow %s = %.5f mm3/mm, %s mm/s, %d lines %.3f mm apart" % (
            i + 1, fmt_val(ratio, 3), a, fmt_val(v, 1), n, sp))
        for k in range(len(self.spans)):
            xa = pil[k][2] - BR_ANCHOR
            xb = pil[k + 1][0] + BR_ANCHOR
            wr.travel(xa, ys[0])
            wr.z_to(self.zb)
            for j, y in enumerate(ys):
                xe = xb if j % 2 == 0 else xa
                wr.bridge(xe, y, a, v, s.a_bridge, w)
                if j + 1 < n:
                    wr.bridge(xe, ys[j + 1], a, v, s.a_bridge, w)   # along the pillar


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
                raise SystemExit("Printune: can't read %s=%s (use start-end/step, e.g. 0-0.08/0.005, "
                                 "or a list, e.g. 260,250,240)" % (key, text))
        self.pa_vals = opt("pa", parse_range)
        self.pa_fine = opt("pa_fine", parse_fine)
        em = o["em"] or "%.4f-%.4f/0.02" % (s.em_profile - 0.04, s.em_profile + 0.04)
        self.em_vals = opt("em", parse_range, em)
        self.em_fine = opt("em_fine", parse_fine)
        self.ret_vals = opt("ret", parse_range)
        temps = o["temps"] or "%d,%d,%d" % (s.t_nozzle + 10, s.t_nozzle, s.t_nozzle - 10)
        self.temps = [int(round(t)) for t in opt("temps", parse_range, temps)]
        self.warn = list(s.notes)
        self.mvs = None
        if "mvs" not in s.skip:
            flows = opt("mvs", parse_range)
            try:
                band = float(o["mvs_band"])
            except ValueError:
                raise SystemExit("Printune: can't read mvs_band=%s" % o["mvs_band"])
            tw = MVSTower(s, flows, band)
            # the printer's limits cap the speed: max_velocity, and Kalico's corner rule on the
            # 1-degree segments (square_corner_velocity, and max_accel for the curve)
            lims = [(s.vmax, "max_velocity (%.0f mm/s)" % s.vmax),
                    (circle_v_limit(MVS_R, MVS_SEG_DEG, 1e9, s.scv) / 1.02,
                     "square_corner_velocity (%g)" % s.scv),
                    (math.sqrt(s.amax / (1.1 * (1.0 / MVS_R + MVS_BUMP_MIN * (2 * math.pi / MVS_BUMP_L) ** 2 / 2))),
                     "max_accel (%.0f) around the tower" % s.amax)]
            vcap, why = min(lims)
            fmax = math.floor(vcap * tw.area * 10 + 1e-6) / 10.0
            if flows[-1] > fmax + 1e-9:
                flows = [f for f in flows if f < fmax - 1e-9] + [fmax]
                self.warn.append("max flow test: %s mm3/s needs %.0f mm/s; %s allows %.0f mm/s, "
                                 "so the top band prints at %s mm3/s" % (fmt_val(tw.flows[-1], 2),
                                                                         tw.flows[-1] / tw.area, why, vcap,
                                                                         fmt_val(fmax, 1)))
            # lower bands when the tower would be too tall for the printer
            room = s.max_z - 5 - tw.base_h
            if len(flows) * band > room:
                band = math.floor(room / len(flows) * 10) / 10.0
                if band < 1.5:
                    raise SystemExit("Printune: the max flow tower is too tall for this printer: "
                                     "use fewer flow steps")
                self.warn.append("max flow test: %.1f mm bands to fit the printer's height" % band)
            self.mvs = MVSTower(s, flows, band)
            if self.mvs.bump < MVS_BUMP - 1e-6:
                self.warn.append("max flow test: %.2f mm marker bumps (max_accel %.0f)" % (self.mvs.bump, s.amax))
        self.cool = None
        if "cool" not in s.skip:
            ft = FILAMENTS.get(s.ftype)
            dflt = ft["cool"] if ft else (COOL_HIGH if s.fan_max >= 50 else COOL_LOW)
            fans = opt("cool_fans", parse_range, o["cool_fans"] or dflt[0])
            angles = opt("cool_angles", parse_range)
            if any(f < 0 or f > 100 for f in fans) or any(a <= 0 or a >= 85 for a in angles):
                raise SystemExit("Printune: cool_fans are 0-100 %, cool_angles 1-84 degrees")
            try:
                t0 = float(o["cool_time"]) or dflt[1]
                pad = float(o["cool_pad"]) or dflt[2]
            except ValueError:
                raise SystemExit("Printune: can't read cool_time or cool_pad")
            if t0 < 2 * CONE_STEP + CONE_TOP:
                raise SystemExit("Printune: cool_time must be at least %g s" % (2 * CONE_STEP + CONE_TOP))
            self.cool = CoolTest(s, fans, angles, t0, pad)
            if self.cool.top > s.max_z - 5:
                raise SystemExit("Printune: the cooling cone is too tall for this printer: use a lower cool_time")
        self.br = None
        if "bridge" not in s.skip:
            self.br = self._bridge_test(opt)
        self.objs = []
        self.t_print = int(round(s.t_nozzle))
        self.t_first = int(round(s.t_first))

    def _bridge_test(self, opt):
        s, o = self.s, self.s.opt
        spans = opt("bridge_spans", parse_range)
        fans = opt("bridge_fans", parse_range) if str(o["bridge_fans"]).strip() else []
        flows = opt("bridge_flows", parse_range)
        try:
            base = float(o["bridge_base"])
        except ValueError:
            raise SystemExit("Printune: can't read bridge_base=%s" % o["bridge_base"])
        base = int(round(base if base >= 0 else s.fan_max))
        if any(f < 0.5 for f in flows):
            # below about 0.42 (normal bridges) Orca's narrowed line gets no wider than it is high
            # and collapses to a round thread with a quarter of the material asked for (see
            # bridge_flow): such a strip does not show what that flow would print
            raise SystemExit("Printune: bridge_flows below 0.5 are not a meaningful test: below about "
                             "0.42 Orca collapses the bridge line to a thin round thread with a quarter "
                             "of the asked-for material. Use values from 0.5 to 2")
        if (any(v < 5 for v in spans) or any(f < 0 or f > 100 for f in fans + [base])
                or any(f > 2 for f in flows)):
            raise SystemExit("Printune: bridge_spans are 5 mm or more, bridge_fans 0-100 %, bridge_flows 0.5-2")
        # spans that don't fit the bed: the longest go
        W = s.bed[2] - s.bed[0]
        room = W - 2 * min(s.margin, max(5.0, 0.05 * min(W, s.bed[3] - s.bed[1]))) - 12.0
        spans = sorted(spans)
        while len(spans) > 1 and BridgeTest.strip_length(spans) > room:
            dropped = spans.pop()
            self.warn.append("bridge test: no room for the %s mm span on this bed" % fmt_val(dropped, 1))
        if BridgeTest.strip_length(spans) > room:
            raise SystemExit("Printune: the bridge test does not fit on this bed: use shorter bridge_spans")
        # speeds: never above what the printer and the filament's flow limit allow
        cap = min(s.vmax, s.mvs / bridge_flow(s, max(flows + [s.br_flow]))[0])
        if str(o["bridge_speeds"]).strip():
            speeds = opt("bridge_speeds", parse_range)
        else:
            speeds = sorted({max(5, int(5 * round(s.v_bridge * f / 5.0))) for f in BR_SPEED_F})
        if any(v <= 0 for v in speeds):
            raise SystemExit("Printune: bridge_speeds must be above 0")
        if speeds[-1] > cap + 1e-6:
            speeds = [v for v in speeds if v < cap - 1e-6] + [math.floor(cap)]
            self.warn.append("bridge test: speeds capped at %d mm/s (max_velocity or the max volumetric speed)"
                             % math.floor(cap))
        speeds = sorted(set(speeds))
        # the pillars' minimum layer time: as the cooling blocks'
        ft = FILAMENTS.get(s.ftype)
        dflt = ft["cool"] if ft else (COOL_HIGH if s.fan_max >= 50 else COOL_LOW)
        try:
            pad = float(o["cool_pad"]) or dflt[2]
        except ValueError:
            raise SystemExit("Printune: can't read cool_pad")
        return BridgeTest(s, spans, [int(round(f)) for f in fans], speeds, flows, base, min(s.v_bridge, cap), pad)

    def n_layers(self):
        mh = self.wr.max_h
        return 1 + int(round(max(0.0, mh - self.s.h1) / self.s.h))

    # ---------------- layout ----------------
    def layout(self):
        """Place the tests. Every test keeps its values; what doesn't fit next to the
        earlier ones goes on a new plate (the session pauses so you can clear it).
        Big beds keep the hand-made one-plate layout; beds down to 120x120 work."""
        s = self.s
        bx0, by0, bx1, by1 = s.bed
        W, D = bx1 - bx0, by1 - by0
        m = self.m = min(s.margin, max(5.0, 0.05 * min(W, D)))
        X = lambda f: bx0 + f * W
        Y = lambda f: by0 + f * D
        sk = s.skip
        self.plates = [[]]

        # patches: 3x3 over the whole bed (always plate 1)
        self.patches, self.patches_b = [], []
        if "patches" not in sk:
            if 3 * PATCH + 2 * m + 12 > min(W, D):
                raise SystemExit("Printune: the bed (%.0fx%.0f mm) is too small for the first-layer "
                                 "patches (Printune is made for beds from 120x120 mm)" % (W, D))
            cxs = [bx0 + m + PATCH / 2, X(0.5), bx1 - m - PATCH / 2]
            cys = [by0 + max(m, 7.0) + PATCH / 2, Y(0.5), by1 - m - PATCH / 2]
            k = 0
            for r, cy in enumerate(cys):            # serpentine, front row first
                row = cxs if r % 2 == 0 else list(reversed(cxs))
                for cx in row:
                    k += 1
                    o = Obj("PATCH_%d" % k, (cx - PATCH / 2, cy - PATCH / 2, cx + PATCH / 2, cy + PATCH / 2),
                            s.h1, "patches")
                    o.plate = 1
                    self.patches.append(o)
            b = self.patches[0].box                 # room for the prime line in front of patch 1
            self.patches[0].box = (b[0], b[1] - 6.0, b[2], b[3])
            self.plates[0] += self.patches

        col_r = (X(0.5) + PATCH / 2 + bx1 - m - PATCH) / 2
        col_l = (bx0 + m + PATCH + X(0.5) - PATCH / 2) / 2
        self.col_r = col_r
        size0 = float(s.opt["em_size"])
        hc = self._em_block_h(len(self.em_vals), size0, 2)
        hf = self._em_block_h(len(self.em_fine), size0, 2)
        ccy = Y(0.5)
        if "em" not in sk and self.em_fine:
            bc = self._bbox(self._em_block(col_l, by0 + m + hc / 2, ["0"] * len(self.em_vals), "x", "em", size0, 2))
            bf = self._bbox(self._em_block(col_l, by1 - m - hf / 2, ["0"] * len(self.em_fine), "x", "em", size0, 2))
            ccy = (bc[3] + bf[1]) / 2

        # extra patches: optional, low; only where they fit on plate 1
        # PA pattern(s): right column, between the tower slots; long rows wrap
        self.pa_objs, self.pa_fine_objs = [], []
        self.pa_fine_needed = False
        if "pa" not in sk:
            # small bed: a coarser coarse pass where the fine pass still covers the gaps
            v = self.pa_vals
            half = max([abs(d) for d in self.pa_fine] or [0.0])
            if len(v) > max(3, self._pa_per_row()) and v[1] - v[0] <= half + 1e-9:
                self.pa_vals = v[::2] + ([v[-1]] if (len(v) - 1) % 2 else [])
                self.pa_fine_needed = True
                self.warn.append("small bed: pressure advance coarse pass in %s steps (the fine pass fills in)"
                                 % fmt_val(2 * (v[1] - v[0])))
            labels = [fmt_val(v) for v in self.pa_vals]
            self.pa_objs = self._place_pa(self.pa_vals, labels, (col_r, Y(0.315)), "PA_COARSE", "pa",
                                          lambda j: "SET_PRESSURE_ADVANCE ADVANCE=%s" % fmt_val(self.pa_vals[j]))
            if self.pa_fine:
                lab = [fmt_val(d * 1000, 1) for d in self.pa_fine]
                self.pa_fine_objs = self._place_pa(
                    self.pa_fine, lab, (col_r, Y(0.665)), "PA_FINE", "pa_fine",
                    lambda j: "_PRINTUNE_PA_REL D=%s" % fmt_val(self.pa_fine[j], 5))

        # EM cubes: left column, coarse block at the front, fine block at the back
        self.em_c, self.em_f = [], []
        if "em" not in sk:
            labs = [("%g" % round(v * 100, 1)) for v in self.em_vals]
            self.em_c = self._place_em(labs, "EM_C", "em", (col_l, by0 + m + hc / 2))
            if self.em_fine:
                labs = [fmt_val(d * 100, 1) for d in self.em_fine]
                self.em_f = self._place_em(labs, "EM_F", "em_fine", (col_l, by1 - m - hf / 2))

        # retraction towers, cooling blocks and cone, bridge strips, then the max flow tower,
        # printed last on a plate of its own: it is far taller than the gantry clearance, so
        # nothing ever shares a plate with it. _fits still keeps the gantry off tall objects.
        self._place_towers(Y)
        self._place_cool(X, Y)
        self._place_bridge(X, Y)
        self._place_mvs(X, Y)

        # extra patches: printed right after the first round, so they only need to stay
        # off the other objects of plate 1 when they are low (one layer under the nozzle
        # protrusion); otherwise they get a plate of their own
        nb = int(s.opt["patches_extra"]) if self.patches else 0
        patches_low = s.h1 <= s.low_ok
        rank = {"patches": 0, "patches_b": 1}
        for i in range(nb):
            dx = (-1 if i % 2 == 0 else 1) * 16.0
            dy = (-1 if i < 2 else 1) * 16.0
            name = "PATCH_B%d" % (i + 1)
            build = lambda cx, cy, name=name: [Obj(name, (cx - PATCH / 2, cy - PATCH / 2, cx + PATCH / 2,
                                                          cy + PATCH / 2), s.h1, "patches_b")]
            got = None
            for cx, cy in (self._candidates(build, (col_l + dx, ccy + dy)) if patches_low else ()):
                objs = build(cx, cy)
                if all(self._on_bed(o) and not any(overlap(o.box, p.box, 0.5) for p in self.plates[0])
                       for o in objs):
                    got = objs
                    break
            if not got:
                break
            got[0].plate = 1
            self.plates[0].append(got[0])
            self.patches_b += got
        if len(self.patches_b) < nb:
            # no room: the extra round gets a plate of its own, right after plate 1
            for o in self.patches_b:
                self.plates[0].remove(o)
            self.patches_b = []
            out = []
            for i in range(nb):
                x = X(0.5) + (i % 2 - 0.5) * (PATCH + 8)
                y = Y(0.5) + (i // 2 - 0.5) * (PATCH + 8)
                out.append(Obj("PATCH_B%d" % (i + 1), (x - PATCH / 2, y - PATCH / 2, x + PATCH / 2,
                                                       y + PATCH / 2), s.h1, "patches_b"))
            b0 = out[0].box                         # room for a prime line: it waited hot
            out[0].box = (b0[0], b0[1] - 6.0, b0[2], b0[3])
            if all(self._on_bed(o) for o in out):
                # tests already on plate 1 move after the extra round: onto its plate where
                # they fit beside it (the patches are low), else onto a plate of their own
                rest = [o for o in self.plates[0] if o.section != "patches"]
                self.plates[0] = [o for o in self.plates[0] if o.section == "patches"]
                if not patches_low or any(overlap(o.box, p.box, 0.5) for o in rest for p in out):
                    self.plates[1:1] = [out, rest]
                else:
                    self.plates.insert(1, out + rest)
                self.plates = [p for p in self.plates if p]
                for k, p in enumerate(self.plates):
                    for o in p:
                        o.plate = k + 1
                self.patches_b = out
            else:
                self.warn.append("no room for the extra first-layer patches")
        self.plates[0].sort(key=lambda o: rank.get(o.section, 2))   # stable: keeps the rest in order

        self.objs = [o for p in self.plates for o in p]   # = print order
        if len(self.plates) > 1:
            self.warn.append("this bed needs %d plates: the session pauses so you can clear the plate"
                             % len(self.plates))

    def _place_towers(self, Y):
        s, sk, col_r = self.s, self.s.skip, self.col_r
        self.towers = []
        if "ret" not in sk:
            base_h, zs = tower_geometry(s, len(self.ret_vals))
            n = len(self.temps)
            fy = [Y(0.14), Y(0.49), Y(0.84)] if n == 3 else [Y(0.14 + 0.7 * i / max(1, n - 1)) for i in range(n)]
            for i, t in enumerate(self.temps):
                def build(cx, cy, i=i, t=t):
                    bw, bd = TOWER_PITCH + RING_D + 4, RING_D + 4
                    lw_, lh_ = label_size(str(t))
                    box = (cx - bw / 2, cy - bd / 2 - lh_ - 9, cx + bw / 2, cy + bd / 2)
                    ob = Obj("RET_%d" % (i + 1), box, zs[-1], "ret")
                    ob.tower = (cx, cy, bw, bd, t)
                    return [ob]
                got = self._place([(build, (col_r, fy[i]))])
                if not got:
                    raise SystemExit("Printune: a retraction tower does not fit on this bed "
                                     "(too few rings for its height, or the bed is too small)")
                self.towers += got

    def _place_mvs(self, X, Y):
        self.mvs_obj = None
        if self.mvs:
            hb = MVS_R + MVS_BASE

            def build(cx, cy):
                ob = Obj("MVS", (cx - hb, cy - hb - 7, cx + hb, cy + hb), self.mvs.top, "mvs")
                ob.c = (cx, cy)
                return [ob]
            got = self._place([(build, (X(0.5), Y(0.5)))], own_plate=True)
            if not got:
                raise SystemExit("Printune: the max flow tower does not fit on this bed")
            self.mvs_obj = got[0]

    def _place_cool(self, X, Y):
        self.cool_objs, self.cone_obj = [], None
        c = self.cool
        if not c:
            return
        lw_, lh_ = label_size("100")
        bw = COOL_W + 2 * COOL_BASE
        bd = COOL_D + c.shift + 2 * COOL_BASE
        n = len(c.fans)
        need_park = self._block_floor_time() < c.pad + 0.3
        for i, fan in enumerate(c.fans):
            def build(cx, cy, i=i, fan=fan):
                # base plate centred on (cx, cy); label behind it, prime line in front
                base = (cx - bw / 2, cy - bd / 2, cx + bw / 2, cy + bd / 2)
                # room to wait behind the block, only if its layers can be too short at the slowest
                park = (cx, base[3] - COOL_BASE + COOL_PARK) if need_park else None
                ob = Obj("COOL_%d" % (i + 1), (base[0] - 1, base[1] - 6, base[2] + 1,
                                               max(base[3] + 2 + lh_ + 1, park[1] + 2 if park else 0)),
                         c.block_h, "cool")
                ob.base, ob.fan, ob.park = base, fan, park
                ob.lab = (cx - label_size(str(fan))[0] / 2, base[3] + 2)
                return [ob]
            got = self._place([(build, (X(0.15 + 0.7 * i / max(1, n - 1)), Y(0.2)))])
            if not got:
                raise SystemExit("Printune: a cooling block does not fit on this bed")
            self.cool_objs += got
        hb = c.R + 2.0

        def build(cx, cy):
            ob = Obj("COOL_CONE", (cx - hb, cy - hb - 7, cx + hb, cy + hb), c.top, "cone")
            ob.c = (cx, cy)
            return [ob]
        got = self._place([(build, (X(0.5), Y(0.75)))])
        if not got:
            raise SystemExit("Printune: the cooling cone does not fit on this bed")
        self.cone_obj = got[0]

    def _place_bridge(self, X, Y):
        """Each pass is a block of strips, one per value, front to back, labelled on the left."""
        self.br_objs = {m: [] for m in BR_MODES}
        b = self.br
        if not b:
            return
        # front, back, then between them: three blocks a toolhead's length apart fit on a 300 mm bed
        pref = {"fan": 0.15, "speed": 0.85, "flow": 0.5}
        for mode in BR_MODES:
            labels = b.labels(mode)
            got = self._place([(lambda cx, cy, labels=labels, mode=mode: self._br_block(cx, cy, labels, mode),
                                (X(0.3), Y(pref[mode])))])
            if not got:
                raise SystemExit("Printune: the bridge test does not fit on this bed: use fewer values or "
                                 "shorter bridge_spans")
            self.br_objs[mode] = got

    def _br_block(self, cx, cy, labels, mode):
        b = self.br
        tw = max(label_size(l)[0] for l in labels)
        tot_x = tw + 1.0 + b.length
        tot_y = b.depth(len(labels))
        x0, y0 = cx - tot_x / 2, cy - tot_y / 2
        # strip 1 is at the front, with the prime line in front of it
        ob = Obj("BR_%s" % mode.upper(), (x0 - 1, y0 - 6.0, x0 + tot_x + 1, y0 + tot_y + 1), b.height, "br_" + mode)
        ob.px = x0 + tw + 1.0
        ob.strips = [y0 + i * (BR_D + BR_GAP) for i in range(len(labels))]
        ob.labels = labels
        ob.labs = [(x0 + tw - label_size(l)[0], y + (BR_D - label_size(l)[1]) / 2) for l, y in zip(labels, ob.strips)]
        return [ob]

    def _place(self, builds, new_plate=True, own_plate=False):
        """builds: [(build(cx, cy) -> objs, preferred (cx, cy))], tried in order. Place on the
        current plate if any fits, else on a new, empty plate. Returns the objects or None.
        own_plate: always a plate with nothing else on it (the current one only if empty)."""
        tries = (False, True) if new_plate else (False,)
        if own_plate:
            tries = (False,) if not self.plates[-1] else (True,)
        for fresh in tries:
            plate = [] if fresh else self.plates[-1]
            for build, pref in builds:
                for cx, cy in self._candidates(build, pref):
                    objs = build(cx, cy)
                    if self._fits(objs, plate):
                        if fresh:
                            self.plates.append([])
                        for o in objs:
                            o.plate = len(self.plates)
                        self.plates[-1].extend(objs)
                        return objs
        return None

    def _candidates(self, build, pref, step=4.0):
        """The preferred position first, then the bed scanned, nearest to it first."""
        s = self.s
        bx0, by0, bx1, by1 = s.bed
        b = self._bbox(build(*pref))
        w, h = b[2] - b[0], b[3] - b[1]
        ox, oy = pref[0] - (b[0] + b[2]) / 2, pref[1] - (b[1] + b[3]) / 2
        yield pref
        e = max(2.0, self.m - 6.0)     # the boxes include ~6 mm of prime line and label room
        x0, x1 = bx0 + e + w / 2, bx1 - e - w / 2
        y0, y1 = by0 + e + h / 2, by1 - e - h / 2
        if x1 < x0 or y1 < y0:
            return
        nx, ny = int((x1 - x0) // step) + 1, int((y1 - y0) // step) + 1
        pts = [(x0 + i * (x1 - x0) / max(1, nx - 1) + ox, y0 + j * (y1 - y0) / max(1, ny - 1) + oy)
               for i in range(nx) for j in range(ny)]
        pts.sort(key=lambda p: (p[0] - pref[0]) ** 2 + (p[1] - pref[1]) ** 2)
        for p in pts:
            yield p

    def _on_bed(self, o):
        bx0, by0, bx1, by1 = self.s.bed
        x0, y0, x1, y1 = o.box
        return x0 >= bx0 + 1 and y0 >= by0 + 1 and x1 <= bx1 - 1 and y1 <= by1 - 1

    def _fits(self, objs, plate):
        """On the bed, no overlap, and the toolhead and the X gantry clear everything taller printed
        before. The toolhead is the user's box around the nozzle (-X, +X, -Y, +Y); the X gantry spans
        the bed in X and is taken to reach as far in Y as that box (the setup asks for the box to
        cover the beam too where it sticks out further)."""
        s = self.s
        bx0, by0, bx1, by1 = s.bed
        xn, xp, yn, yp = s.box
        for o in objs:
            x0, y0, x1, y1 = o.box
            if x0 < bx0 + 1 or y0 < by0 + 1 or x1 > bx1 - 1 or y1 > by1 - 1:
                return False
            if o.height > s.max_z - 5:
                return False
            for p in plate:
                if overlap(o.box, p.box, 0.5):
                    return False
                # which of the two prints first (by section; same section: the one already placed)
                first, later = (p, o) if SECTION_RANK.get(p.section, 0) <= SECTION_RANK.get(o.section, 0) else (o, p)
                lb = later.box
                # while the later object prints, the toolhead box around the nozzle sweeps its box
                # grown by the box's sides: it must not reach a finished object that is taller than
                # low_ok (the nozzle protrusion less LOW_MARGIN). Objects of one section print layer by layer together, except the
                # retraction towers and cooling blocks, which print one at a time.
                if (first.height > s.low_ok and (p.section != o.section or o.section in ("ret", "cool"))
                        and overlap((lb[0] + xn, lb[1] + yn, lb[2] + xp, lb[3] + yp), first.box)):
                    return False
                # the X gantry spans the bed at the nozzle's Y: whatever prints later must keep the
                # gantry's Y band off anything taller than the gantry height
                if first.height > s.gantry and lb[1] + yn < first.box[3] and lb[3] + yp > first.box[1]:
                    return False
        return True

    def _place_pa(self, vals, labels, pref, name, section, setter):
        """One PA pattern, wrapped into rows of the same length if it is wider than the bed."""
        s = self.s
        per_row = self._pa_per_row()
        nrows = int(math.ceil(len(vals) / float(per_row)))
        per_row = int(math.ceil(len(vals) / float(nrows)))
        chunks = [list(range(i, min(i + per_row, len(vals)))) for i in range(0, len(vals), per_row)]

        def build(cx, cy):
            out, pats = [], []
            for k, idx in enumerate(chunks):
                pat = PATest(s, [vals[j] for j in idx], [labels[j] for j in idx], cx, 0.0,
                             lambda j, idx=idx: setter(idx[j]))
                pats.append(pat)
            hs = [p.osy + 14 for p in pats]
            y = cy + sum(hs) / 2
            for k, (idx, pat) in enumerate(zip(chunks, pats)):
                y -= hs[k]
                pat = PATest(s, pat.values, pat.labels, cx, cy if len(chunks) == 1 else y + hs[k] / 2,
                             pat.setter)
                o = Obj(name if len(chunks) == 1 else "%s_%d" % (name, k + 1), self._grow(pat.box, 6),
                        pat.height, section)
                o.pat = pat
                out.append(o)
            return out
        got = self._place([(build, pref)])
        if not got:
            raise SystemExit("Printune: the pressure advance pattern does not fit on this bed: "
                             "use fewer values or a coarser step")
        return got

    def _pa_per_row(self):
        W = self.s.bed[2] - self.s.bed[0]
        return max(2, int((W - 2 * self.m - 14 + PATest.GAP) // (PATest.FRAME_W + PATest.GAP)))

    def _place_em(self, labels, prefix, section, pref):
        """EM cubes: full size first; smaller cubes or 3 columns where the bed is small."""
        size0 = float(self.s.opt["em_size"])
        builds = []
        for size, cols in ((size0, 2), (size0, 3), (min(size0, 25.0), 3), (min(size0, 20.0), 3),
                           (min(size0, 20.0), 4), (min(size0, 20.0), 6)):
            builds.append((lambda cx, cy, size=size, cols=cols:
                           self._em_block(cx, cy, labels, prefix, section, size, cols), pref))
        got = self._place(builds)
        if not got:
            raise SystemExit("Printune: the flow cubes do not fit on this bed: use fewer flow values")
        w = got[0].cube[2] - got[0].cube[0]
        if w < size0 - 0.1:
            self.warn.append("small bed: %.0f mm flow cubes" % w)
        return got

    @staticmethod
    def _grow(b, d):
        return (b[0] - d, b[1] - d, b[2] + d, b[3] + d)

    @staticmethod
    def _bbox(objs):
        return (min(o.box[0] for o in objs), min(o.box[1] for o in objs),
                max(o.box[2] for o in objs), max(o.box[3] for o in objs))

    @staticmethod
    def _em_block_h(n, size, cols):
        if n == 0:
            return 0.0
        cols = cols if n > 3 else 1
        rows = int(math.ceil(n / float(cols)))
        return rows * (size + 9.0) + (rows - 1) * 9.0 + 5.0 + 2.0

    def _em_block(self, cx, cy, labels, prefix, section, size, cols):
        s = self.s
        n = len(labels)
        gap = 9.0
        cols = min(cols, n) if n > 3 else 1
        rows = int(math.ceil(n / float(cols)))
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
            ob = Obj("%s%d" % (prefix, i + 1), box, em_layers(s, float(s.opt["em_height"]))[0][-1], section)
            ob.cube, ob.label, ob.lab_xy, ob.idx = cube, lab, lab_xy, i
            out.append(ob)
        return out

    # ---------------- G-code ----------------
    def body(self):
        s = self.s
        wr = Writer(s)
        self.wr = wr
        g = wr.g
        g("; ===== Printune session body (generated by printune_gen.py v%d) =====" % VERSION)
        g("G90")
        g("M83")
        g("G92 E0")
        # PA=-1: Orca does not set PA, so keep whatever the printer has now
        T = self.titles()
        set_ret = int(float(s.opt["set_ret"])) and s.fw_retract
        # GEOM: the fingerprint of the geometry this session was laid out for; with the plugin
        # loaded, _PRINTUNE_BEGIN refuses it unless it matches the saved, accepted machine setup
        g("_PRINTUNE_BEGIN VERSION=%d GEOM=%s PA=%s EM=%.4f FW=%d PFR=%.4f STEPS=%s PROC=%s" % (
            VERSION, s.geom_hash, fmt_val(s.pa_profile, 4) if s.pa_enabled else "-1", s.em_profile,
            1 if s.fw_retract else 0, _num(s.cfg.get("print_flow_ratio", "1")) or 1.0,
            ",".join(self.steps), self.proc_word))
        if set_ret:
            # advanced tuning: the retraction the wizard picked (the session puts yours back at the end)
            g("SET_RETRACTION RETRACT_LENGTH=%s RETRACT_SPEED=%s UNRETRACT_EXTRA_LENGTH=0 UNRETRACT_SPEED=%s" % (
                fmt_val(s.ret_len, 3), fmt_val(s.ret_speed, 1), fmt_val(s.unret_speed, 1)))
        wr.set_accel(s.a_travel)
        wr.retracted = False
        wr.z = 0.0

        # ---- 1. patches ----
        if self.patches:
            g("; ----- 1. FIRST LAYER SQUISH -----")
            g("M117 %s first layer - babystep now" % T["squish"])
            g("RESPOND MSG=\"%s: first-layer patches. Babystep Z while they print.\"" % T["squish"])
            wr.fan(0)
            first = self.patches[0]
            wr.safe_travel(first.box[0], first.box[1] + 1)
            self._patch_round(wr, self.patches, prime=True)
            # after the last patch round, heat to the normal print temperature while you judge
            heat = ("M104 S%d" % self.t_print) if self.t_print != self.t_first else None
            if self.patches_b:
                self._cp(wr, "_PRINTUNE_CP STEP=squish SKIP=%s" % ",".join(o.name for o in self.patches_b))
                self._plate(wr, self.patches_b)
                self._patch_round(wr, self.patches_b, prime=self.patches_b[0].plate > 1)
                if heat:
                    g(heat)
                self._cp(wr, "_PRINTUNE_CP STEP=squish2 OBJ=%s" % self.patches_b[0].name)
            else:
                if heat:
                    g(heat)
                self._cp(wr, "_PRINTUNE_CP STEP=squish2")

        # ---- 2. PA ----
        if self.pa_objs:
            g("; ----- 2. PRESSURE ADVANCE -----")
            g("M117 %s pressure advance" % T["pa"])
            self._plate(wr, self.pa_objs)
            g("M109 S%d" % self.t_print)
            fan = float(s.opt["pa_fan"]) or s.fan_max
            vals = ",".join(fmt_val(v) for v in self.pa_vals)
            for o in self.pa_objs:
                self._pa(wr, o, fan)
            fine = ",".join(o.name for o in self.pa_fine_objs)
            # no "skip the fine pass" button when the coarse pass was thinned out for a small bed
            skip = ("SKIP=" + fine) if fine and not self.pa_fine_needed else ""
            self._cp(wr, "_PRINTUNE_CP STEP=pa VALUES=%s %s" % (vals, skip))
            g("_PRINTUNE_APPLY")
            if self.pa_fine_objs:
                self._plate(wr, self.pa_fine_objs)
                for o in self.pa_fine_objs:
                    self._pa(wr, o, fan)
                self._cp(wr, "_PRINTUNE_CP STEP=pa_fine OBJ=%s OFFSETS=%s"
                         % (self.pa_fine_objs[0].name, ",".join(fmt_val(d, 5) for d in self.pa_fine)))
                g("_PRINTUNE_APPLY")

        # ---- 3. EM ----
        if self.em_c:
            g("; ----- 3. FLOW (EXTRUSION MULTIPLIER) -----")
            g("M117 %s extrusion multiplier" % T["em"])
            g("M109 S%d" % self.t_print)
            g("_PRINTUNE_APPLY")
            self._plate(wr, self.em_c)
            fan = float(s.opt["em_fan"]) or s.fan_max
            self._em(wr, self.em_c, fan, lambda o: "M221 S%.2f" % (self.em_vals[o.idx] * 100))
            skip = ("SKIP=" + ",".join(o.name for o in self.em_f)) if self.em_f else ""
            self._cp(wr, "_PRINTUNE_CP STEP=em VALUES=%s %s" % (",".join("%.4f" % v for v in self.em_vals), skip))
            g("_PRINTUNE_APPLY")
            if self.em_f:
                self._plate(wr, self.em_f)
                self._em(wr, self.em_f, fan, lambda o: "_PRINTUNE_EM_REL D=%s" % fmt_val(self.em_fine[o.idx], 5))
                self._cp(wr, "_PRINTUNE_CP STEP=em_fine OBJ=%s OFFSETS=%s"
                         % (self.em_f[0].name, ",".join(fmt_val(d, 5) for d in self.em_fine)))
                g("_PRINTUNE_APPLY")

        # ---- 4. retraction ----
        if self.towers:
            g("; ----- 4. RETRACTION towers, one at a time, hottest first -----")
            g("M117 %s retraction" % T["ret"])
            # retraction tuning is optional; also the moment to brush the nozzle clean
            self._cp(wr, "_PRINTUNE_CP STEP=ret_ask SKIP=%s" % ",".join(o.name for o in self.towers))
            g("_PRINTUNE_APPLY")
            fan = float(s.opt["ret_fan"]) or max(s.fan_max, 80.0)
            wr.ret_macro = not s.fw_retract
            for o in self.towers:
                self._plate(wr, [o])
                self._tower(wr, o, fan)
            wr.ret_macro = False
            nxt = self._next_names("ret")
            if nxt:
                # pick the retraction (and tower temperature) now: the later tests print at it
                self._cp(wr, self._ret_cp() + " NEXT=%s" % ",".join(nxt))

        # ---- cooling: fan from the overhang blocks, minimum layer time from the cone ----
        if self.cone_obj:
            c = self.cool
            g("; ----- COOLING: overhang blocks, one per fan speed, then the layer-time cone -----")
            g("M117 %s cooling" % T["cool"])
            # the picks so far go into the preset now, so a cancel during the test keeps them
            g("_PRINTUNE_SAVEPOINT")
            names = [o.name for o in self.cool_objs] + [self.cone_obj.name]
            for i, o in enumerate(self.cool_objs):
                # the first plate pause can skip the whole cooling test
                self._plate(wr, [o], skip=names if i == 0 else None, what="cool")
                self._cool_block(wr, o)
            if self.cool_objs:
                self._cp(wr, "_PRINTUNE_CP STEP=cool_fan ALL=%s FANS=%s ANGLES=%s" % (
                    ",".join(o.name for o in self.cool_objs), ",".join(str(f) for f in c.fans),
                    ",".join(fmt_val(a, 1) for a in c.angles)))
                # the fan pick goes into the preset now, so a cancel during the cone keeps it
                g("_PRINTUNE_SAVEPOINT")
            self._plate(wr, [self.cone_obj], skip=[self.cone_obj.name], what="cone")
            self._cone(wr, self.cone_obj)
            nxt = self._next_names("cool")
            if nxt:
                self._cp(wr, self._cool_cp() + " NEXT=%s" % ",".join(nxt))

        # ---- bridges: fan, then speed, then flow ----
        if self.br_objs["flow"]:
            b, bo = self.br, self.br_objs
            g("; ----- BRIDGES: three passes (fan, speed, flow), each keeps the picks before it -----")
            g("M117 %s bridges" % T["bridge"])
            # the picks so far go into the preset now, so a cancel during the test keeps them
            g("_PRINTUNE_SAVEPOINT")
            names = lambda ms: [o.name for m in ms for o in bo[m]]
            # the fan pass's fans: from the fan picked in the cooling test, else from b.base
            g("_PRINTUNE_BRIDGE_FANS OBJ=%s N=%d BASE=%d%s" % (bo["fan"][0].name, b.n_fans, b.base,
                                                              (" FANS=" + ",".join(map(str, b.fans))) if b.fans else ""))
            for k, mode in enumerate(BR_MODES):
                # a plate pause can skip the rest of the bridge test
                # (the first one all of it, a later one the passes still to come)
                self._plate(wr, bo[mode], skip=names(BR_MODES[k:]), what="bridge" if k == 0 else "bridge_" + mode)
                self._bridge_pass(wr, bo[mode][0], mode)
                if mode == "fan":
                    self._cp(wr, "_PRINTUNE_CP STEP=bridge_fan OBJ=%s SPANS=%s" % (
                        bo["fan"][0].name, ",".join(fmt_val(v, 1) for v in b.spans)))
                    g("_PRINTUNE_SAVEPOINT")
                elif mode == "speed":
                    self._cp(wr, "_PRINTUNE_CP STEP=bridge_speed OBJ=%s SPEEDS=%s" % (
                        bo["speed"][0].name, ",".join(fmt_val(v, 1) for v in b.speeds)))
                    g("_PRINTUNE_SAVEPOINT")
            nxt = self._next_names("bridge")
            if nxt:
                self._cp(wr, self._bridge_cp() + " NEXT=%s" % ",".join(nxt))

        # ---- max volumetric flow ----
        if self.mvs_obj:
            g("; ----- MAX VOLUMETRIC FLOW tower -----")
            g("M117 %s max flow" % T["mvs"])
            # the picks so far go into the preset now, so a cancel during the tower keeps them
            g("_PRINTUNE_SAVEPOINT")
            self._plate(wr, [self.mvs_obj], skip=[self.mvs_obj.name], what="mvs")
            self._mvs(wr, self.mvs_obj)
            nxt = self._next_names("mvs")
            if nxt:
                # pick the flow now, then ask about the next test
                self._cp(wr, self._mvs_cp() + " NEXT=%s" % ",".join(nxt))

        self._park(wr)
        g("_PRINTUNE_END")
        # the last test's pick, after the end G-code
        last = self.steps[-1] if self.steps else ""
        if last == "cool":
            g(self._cool_cp())
        elif last == "bridge":
            g(self._bridge_cp())
        elif last == "mvs":
            g(self._mvs_cp())
        elif last == "ret":
            g(self._ret_cp())
        else:
            g("_PRINTUNE_CP STEP=summary")
        g("; ===== Printune session end =====")
        return wr

    def titles(self):
        """The tests this session prints, in order, and each one's prompt title prefix:
        'Printune 2/4' in the wizard, 'Advanced 1/2' in advanced tuning, 'Printune' alone."""
        have = {"squish": bool(self.patches), "pa": bool(self.pa_objs), "em": bool(self.em_c),
                "ret": bool(self.towers), "mvs": bool(self.mvs_obj), "cool": bool(self.cone_obj),
                "bridge": bool(self.br_objs["flow"])}
        self.steps = [t for t in ORDER if have[t]]
        adv = self.steps and all(t in PROCS["advanced"] for t in self.steps)
        self.proc_word = TITLE_WORD["advanced" if adv else "wizard"]
        n = len(self.steps)
        T = {t: "Printune" for t in ORDER}
        for i, t in enumerate(self.steps):
            T[t] = "%s %d/%d" % (self.proc_word, i + 1, n) if n > 1 else "Printune"
        return T

    def _next_names(self, step):
        """Objects of the test after this one (the session asks about it), else []."""
        i = self.steps.index(step)
        if i + 1 >= len(self.steps):
            return []
        nxt = self.steps[i + 1]
        if nxt == "mvs":
            return [self.mvs_obj.name]
        if nxt == "cool":
            return [o.name for o in self.cool_objs] + [self.cone_obj.name]
        if nxt == "bridge":
            return [o.name for m in BR_MODES for o in self.br_objs[m]]
        return []

    def _bridge_cp(self):
        return "_PRINTUNE_CP STEP=bridge_flow OBJ=%s FLOWS=%s" % (
            self.br_objs["flow"][0].name, ",".join(fmt_val(f, 3) for f in self.br.flows))

    def _mvs_cp(self):
        m = self.mvs
        return "_PRINTUNE_CP STEP=mvs OBJ=%s FLOWS=%s BASE=%.3f BAND=%.3f" % (
            self.mvs_obj.name, ",".join(fmt_val(f, 2) for f in m.flows), m.base_h, m.band_h)

    def _ret_cp(self):
        return "_PRINTUNE_CP STEP=ret ALL=%s TEMPS=%s RINGS=%s" % (
            ",".join(o.name for o in self.towers), ",".join(str(t) for t in self.temps),
            ",".join(fmt_val(v, 3) for v in self.ret_vals))

    def _cool_cp(self):
        c = self.cool
        return "_PRINTUNE_CP STEP=cool_lt OBJ=%s TIMES=%s TOP=%.1f FAN=%d TYPE=%s" % (
            self.cone_obj.name, ",".join(str(c.marks[k]) for k in sorted(c.marks)),
            max(CONE_TOP, c.cone_times[-1][3] if c.cone_times else CONE_TOP), self._fan0(),
            re.sub(r"[^A-Z0-9]", "", self.s.ftype) or "-")

    def _fan0(self):
        """The cone's fan when no fan was picked: the profile's."""
        return int(round(self.s.fan_max))

    def _layer_time(self, wr, snap):
        """Time of what was written since snap, as Kalico plans it, s."""
        st, i0, _ = snap
        mv, _ = gcode_moves(wr.out[i0:], (st["x"] or 0.0, st["y"] or 0.0, st["z"]), st["accel"] or 3000.0,
                            fw=(self.s.ret_len, self.s.ret_speed))
        s = self.s
        return sum(kalico_plan(mv, scv=s.scv, mcr=s.mcr, vmax=s.vmax, zv=s.zv, za=s.za))

    def _cool_block(self, wr, o):
        """One overhang block, alone, with its own fan; block layers stretched to cool.pad seconds."""
        s, c = self.s, self.cool
        x0, y0, x1, y1 = o.base
        cx = (x0 + x1) / 2
        yb = y1 - COOL_BASE                      # back face of the block at the base
        self._park(wr)
        wr.g("_PRINTUNE_COOL_TEMP OBJ=%s S=%d" % (o.name, self.t_print))
        wr.safe_travel(o.box[0] + 1, o.box[1] + 1)
        obj_start(wr, o)
        wr.fan(0)
        prime_line(wr, s, o.box[0] + 1, o.box[1] + 1, x1 - x0 - 2)
        wr.g("; cooling block: fan %d %%, overhangs %s degrees, %d layers each, layers stretched to %g s" % (
            o.fan, ",".join(fmt_val(a, 1) for a in c.angles), COOL_BAND_LAYERS, c.pad))
        for li, (z, h, off, groove) in enumerate(c.layers):
            if li == s.fan_off_layers:
                wr.fan(o.fan)
            wr.g("; block layer %d" % li)
            snap = wr.snapshot()
            wr.z_to(z)
            if li < BASE_LAYERS:
                loops, edge = walls_rect(s, x0, y0, x1, y1, s.wall_loops, h, li == 0)
                print_walls(wr, s, loops, h)
                w = s.w_first if li == 0 else s.w_solid
                v = s.v_first_infill if li == 0 else s.v_solid
                a = s.a_first if li == 0 else s.a_solid
                print_fill(wr, s, x0, y0, x1, y1, edge, w, h, v, a, spacing(w, h),
                           45 if li % 2 == 0 else -45, s.ov_solid, "solid")
                if li <= 1:
                    label_tab(wr, s, o.lab[0], o.lab[1], str(o.fan), li)
                continue
            g_ = COOL_GROOVE if groove else 0.0
            rect = (cx - COOL_W / 2 + g_, yb - COOL_D - off, cx + COOL_W / 2 - g_, yb - off)
            v_ov = self._overhang_speed(off - c.layers[li - 1][2])
            wr.vscale, wr.vmin = 1.0, s.v_slow_min
            t = 0.0
            for _ in range(12):
                self._block_layer(wr, rect, li, h, v_ov)
                t = self._layer_time(wr, snap)
                if t >= c.pad - 0.1 or wr.vscale * max(s.v_inner, s.v_solid) <= wr.vmin:
                    break
                # Orca-style: slow the layer down until it takes cool.pad seconds, but not the
                # outer wall (the overhang is judged at its real speed)
                vs = wr.vscale * t / c.pad
                wr.restore(snap)
                wr.vscale = vs
            wr.vscale = 1.0
            if t < c.pad - 0.1 and li < len(c.layers) - 1 and o.park:
                # at the slow-down floor: wait the rest away from the block, so it cools as in a print
                wr.retract()
                wr.lift()
                wr.set_accel(s.a_travel)
                wr.travel(*o.park)
                at = len(wr.out)
                wr.travel(rect[0], rect[3])         # back over the block, still lifted
                rest = c.pad - self._layer_time(wr, snap)
                if rest > 0.01:
                    wr.out.insert(at, "G4 P%d" % round(rest * 1000))
                    wr.t_total += rest
        if s.fan_off_layers >= len(c.layers):
            wr.fan(o.fan)
        wr.retract()
        obj_end(wr, o)
        wr.max_h = max(wr.max_h, c.block_h)

    def _block_floor_time(self):
        """Shortest block layer (a groove layer at the outer wall speed) with the inner walls and
        the fill at the slow-down floor, s."""
        s = self.s
        wr = Writer(s)
        wr.x, wr.y, wr.z, wr.accel = 0.0, 0.0, s.h1 + BASE_LAYERS * s.h, s.a_outer
        wr.vscale, wr.vmin = 1e-6, s.v_slow_min
        snap = wr.snapshot()
        self._block_layer(wr, (COOL_GROOVE, 0.0, COOL_W - COOL_GROOVE, COOL_D), BASE_LAYERS, s.h,
                          min(s.v_outer, s.vmax))
        return self._layer_time(wr, snap)

    def _overhang_speed(self, step):
        """Outer wall speed for a wall stepping out by step mm per layer, as Orca slices it."""
        s = self.s
        f = step / s.w_outer
        k = 3 if f >= 0.75 else 2 if f >= 0.5 else 1 if f >= 0.25 else 0 if f >= 0.1 else -1
        v = s.v_overhang[k] if k >= 0 else 0.0
        return min(v or s.v_outer, s.vmax)

    def _block_layer(self, wr, rect, li, h, v_ov):
        """One overhang block layer: the outer wall at full speed (its front, the overhang, at
        v_ov), the inner walls and the fill at wr.vscale."""
        s = self.s
        loops, edge = walls_rect(s, *rect, s.wall_loops, h, False)
        for pts, w, v, a, kind in (loops if s.outer_first else list(reversed(loops))):
            pts = pts + [pts[0]]
            wr.travel(*pts[0])
            for p, q in zip(pts, pts[1:]):
                if kind == "outer":
                    front = abs(p[1] - q[1]) < 1e-6 and abs(p[1] - min(y for _, y in pts)) < 1e-6
                    vs, wr.vscale = wr.vscale, 1.0
                    wr.line(q[0], q[1], w, h, v_ov if front else v, a, kind)
                    wr.vscale = vs
                else:
                    wr.line(q[0], q[1], w, h, v, a, kind)
        print_fill(wr, s, *rect, edge, s.w_solid, h, s.v_solid, s.a_solid, spacing(s.w_solid, h),
                   45 if li % 2 == 0 else -45, s.ov_solid, "solid")

    def _cone(self, wr, o):
        s, c = self.s, self.cool
        cx, cy = o.c
        self._park(wr)
        wr.g("_PRINTUNE_COOL_TEMP OBJ=%s S=%d" % (o.name, self.t_print))
        wr.safe_travel(o.box[0] + 1, o.box[1] + 1)
        obj_start(wr, o)
        wr.fan(0)
        prime_line(wr, s, o.box[0] + 1, o.box[1] + 1, o.box[2] - o.box[0] - 2)
        # the layer times are the real ones: no speed override, nothing slows it down
        wr.g("M220 S100")
        wr.g("; cooling cone: centre %.3f %.3f, %g mm/s, layer time %g s at the bottom, -%g s every %d layers, "
             "marker bumps every %g s at %g deg" % (cx, cy, c.v, c.t0, CONE_STEP, c.per2, CONE_STEP, CONE_MARK_DEG))
        c.draw_cone(wr, cx, cy, "_PRINTUNE_COOL_FAN D=%d" % self._fan0())
        # check: the layer times Kalico will print, from the G-code just written
        i0, pos, acc = c.l0
        c.cone_times = cone_layer_times(wr.out[i0:], pos, acc, c.plan_args())
        ct = c.cone_times
        off = [abs(ct[j - 1][3] - mk) for j, (k, tg, mk, t, _) in enumerate(ct) if mk and j]
        if off and max(off) > 0.3:
            self.warn.append("cooling cone: a marker is %.1f s off its layer time" % max(off))
        wr.retract()
        obj_end(wr, o)

    def _bridge_pass(self, wr, o, mode):
        """One pass: the pillar walls layer by layer, then the bridge layer strip by strip (fan pass:
        each strip's own fan; speed pass: its own speed; flow pass: its own flow), then a solid
        layer on each strip. The pillars and the top layer print at the filament's fan."""
        s, b = self.s, self.br
        self._park(wr)
        wr.g("_PRINTUNE_BRIDGE_TEMP OBJ=%s S=%d" % (o.name, self.t_print))
        wr.safe_travel(o.px, o.strips[0] - 4.0)
        obj_start(wr, o)
        wr.fan(0)
        prime_line(wr, s, o.px, o.strips[0] - 4.0, min(40.0, b.length))
        base_fan = "_PRINTUNE_COOL_FAN D=%d" % b.base      # the picked cooling fan, else the base
        wr.g("; bridge test, %s pass: spans %s mm, %d pillar layers (at least %g s each), bridge layer at Z %.3f" % (
            mode, ",".join(fmt_val(v, 1) for v in b.spans), len(b.zs), b.pad, b.zb))
        pil = b.pillars(o)
        for li, z in enumerate(b.zs):
            if li == s.fan_off_layers:
                wr.g(base_fan)
            h = s.h1 if li == 0 else s.h
            snap = wr.snapshot()
            wr.vscale, wr.vmin = 1.0, s.v_slow_min
            for _ in range(12):
                wr.z_to(z)
                for (x0, y0, x1, y1) in (pil if li % 2 == 0 else pil[::-1]):
                    loops, edge = walls_rect(s, x0, y0, x1, y1, s.wall_loops, h, li == 0)
                    print_walls(wr, s, loops, h)
                    w = s.w_first if li == 0 else s.w_solid
                    v = s.v_first_infill if li == 0 else s.v_solid
                    a = s.a_first if li == 0 else s.a_solid
                    print_fill(wr, s, x0, y0, x1, y1, edge, w, h, v, a, spacing(w, h),
                               45 if li % 2 == 0 else -45, s.ov_solid, "solid")
                if li <= 1:
                    for lab, xy in zip(o.labels, o.labs):
                        label_tab(wr, s, xy[0], xy[1], lab, li)
                if li == 0:
                    break
                # thin walls: slow down to the filament's minimum layer time, as Orca would
                t = self._layer_time(wr, snap)
                if t >= b.pad - 0.1 or wr.vscale * max(s.v_inner, s.v_solid, s.v_outer) <= wr.vmin:
                    break
                vs = wr.vscale * t / b.pad
                wr.restore(snap)
                wr.vscale = vs
            wr.vscale = 1.0
        if s.fan_off_layers >= len(b.zs):
            wr.g(base_fan)
        wr.g("; bridge layer")
        for i in range(len(o.strips)):
            if mode == "fan":
                wr.g("_PRINTUNE_BRIDGE_FAN I=%d" % (i + 1))
            else:
                wr.g("_PRINTUNE_BRIDGE_FAN")                  # the picked bridge fan
            ratio = b.flows[i] if mode == "flow" else s.br_flow
            if mode == "speed":
                b.draw_bridges(wr, o, i, ratio, b.speeds[i])
            else:
                if mode == "flow":
                    # the picked speed: M220 scales the speed the lines are written at
                    wr.g("_PRINTUNE_BRIDGE_SPEED REF=%s" % fmt_val(b.v_ref, 2))
                b.draw_bridges(wr, o, i, ratio, b.v_ref)
                if mode == "flow":
                    wr.g("M220 S100")
        wr.g(base_fan)
        for k, z in enumerate(b.zc):
            for i in (range(len(o.strips)) if k % 2 else range(len(o.strips) - 1, -1, -1)):
                x0, y0, x1, y1 = pil[0][0], o.strips[i], pil[-1][2], o.strips[i] + BR_D
                wr.travel(x0, y1)
                wr.z_to(z)
                loops, edge = walls_rect(s, x0, y0, x1, y1, s.wall_loops, s.h, False)
                print_walls(wr, s, loops, s.h)
                print_fill(wr, s, x0, y0, x1, y1, edge, s.w_solid, s.h, s.v_solid, s.a_solid,
                           spacing(s.w_solid, s.h), 0, s.ov_solid, "solid")
        wr.retract()
        obj_end(wr, o)
        wr.max_h = max(wr.max_h, b.height)

    def _mvs(self, wr, o):
        s, m = self.s, self.mvs
        cx, cy = o.c
        hb = MVS_R + MVS_BASE
        self._park(wr)
        # at the picked tower temperature if there was one, else the print temperature
        wr.g("_PRINTUNE_MVS_TEMP OBJ=%s S=%d" % (o.name, self.t_print))
        wr.safe_travel(o.box[0] + 1, o.box[1] + 1)
        obj_start(wr, o)
        wr.fan(0)
        prime_line(wr, s, o.box[0] + 1, o.box[1] + 1, 2 * hb - 2)
        fan = float(s.opt["mvs_fan"]) or s.fan_max
        bx0, by0, bx1, by1 = cx - hb, cy - hb, cx + hb, cy + hb
        for li in range(BASE_LAYERS):
            h = s.h1 if li == 0 else s.h
            if li == s.fan_off_layers:
                wr.fan(fan)
            wr.z_to(s.h1 + li * s.h)
            loops, edge = walls_rect(s, bx0, by0, bx1, by1, s.wall_loops, h, li == 0)
            print_walls(wr, s, loops, h)
            w = s.w_first if li == 0 else s.w_solid
            v = s.v_first_infill if li == 0 else s.v_solid
            a = s.a_first if li == 0 else s.a_solid
            print_fill(wr, s, bx0, by0, bx1, by1, edge, w, h, v, a, spacing(w, h),
                       45 if li % 2 == 0 else -45, s.ov_solid, "solid")
        if s.fan_off_layers >= BASE_LAYERS:
            wr.fan(fan)
        # the label flow is the real flow: no speed or flow override, no flow cap, no layer-time slow-down
        wr.g("M220 S100")
        wr.g("M221 S100")
        wr.g("; max flow bands: %s mm3/s, %.2f mm each from Z %.2f" % (
            ", ".join(fmt_val(f, 2) for f in m.flows), m.band_h, m.base_h))
        m.draw(wr, cx, cy)
        slow = m.check_corners(s.scv)
        if slow:
            self.warn.append("max flow test: Kalico's corner rule slows bands %s (slowest %.0f of %.0f mm/s)" % (
                ",".join(str(b + 1) for b in sorted(slow)), min(v[0] for v in slow.values()),
                max(v[1] for v in slow.values())))
        wr.retract()
        obj_end(wr, o)
        wr.g("_PRINTUNE_APPLY")

    def _park(self, wr):
        bx0, by0, bx1, by1 = self.s.bed
        wr.safe_travel((bx0 + bx1) / 2, by1 - 10, clear=10.0)

    def _plate(self, wr, objs, skip=None, what=""):
        """Checkpoint before a test that goes on a new plate: clear the plate, printer stays hot.
        skip: objects a 'skip' button cancels there; what: its label (cool, cone, bridge, bridge_speed, bridge_flow, mvs)."""
        n = objs[0].plate
        if n <= 1:
            return
        # before every group on a later plate: the macro asks once per plate, and not at all
        # when this group was skipped (a later group on the same plate asks instead)
        names = ",".join(o.name for o in objs)
        self._cp(wr, "_PRINTUNE_CP STEP=plate N=%d ALL=%s%s" % (
            n, names, (" SKIP=%s WHAT=%s" % (",".join(skip), what)) if skip else ""))

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
        prime_line(wr, s, first.cube[0] + 1, first.cube[3] + 2.0, min(28.0, first.cube[2] - first.cube[0] - 2))
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
        wr.g("_PRINTUNE_TEMP OBJ=%s S=%d" % (o.name, temp))
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
                wr.g("_PRINTUNE_RET_SET LEN=%s SPEED=%s" % (fmt_val(self.ret_vals[sec], 4),
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
        wr.g("_PRINTUNE_RET_RESTORE")

    def define_objects(self):
        lines = []
        for o in self.objs:
            cx, cy = o.center()
            lines.append("EXCLUDE_OBJECT_DEFINE NAME=%s CENTER=%.3f,%.3f POLYGON=%s" % (o.name, cx, cy, o.poly()))
        return lines

    def header(self):
        s = self.s
        h = ["; Printune tuning session (printune_gen.py v%d)" % VERSION,
             "; DISCLAIMER: generated G-code, provided as is without any warranty. Use it at your own",
             "; risk; the authors take zero responsibility for any damage. Watch the print.",
             "; Needs printune.cfg (Klipper/Kalico) included in printer.cfg.",
             "; Order: squish -> PA -> EM -> retraction -> cooling -> bridges -> max flow. Picks are applied to every later test.",
             "; PA values: %s" % ", ".join(fmt_val(v) for v in self.pa_vals),
             "; PA fine offsets: %s" % ", ".join(fmt_val(v, 5) for v in self.pa_fine),
             "; EM values: %s" % ", ".join("%.3f" % v for v in self.em_vals),
             "; EM fine offsets: %s" % ", ".join(fmt_val(v, 5) for v in self.em_fine),
             "; Retraction rings: %s mm @ %s mm/s, temps %s" % (", ".join(fmt_val(v, 3) for v in self.ret_vals),
                                                              s.opt["ret_speed"], ", ".join(map(str, self.temps))),
             "; Cooling: fans %s %%, overhangs %s deg, cone %s" % (
                 ",".join(str(f) for f in self.cool.fans), ",".join(fmt_val(a, 1) for a in self.cool.angles),
                 "%g mm/s, %g s at the bottom, markers %s s" % (
                     self.cool.v, self.cool.t0, ",".join(str(self.cool.marks[k]) for k in sorted(self.cool.marks))))
             if self.cool else "; Cooling: -",
             "; Bridges: spans %s mm, fans %s, speeds %s mm/s, flows %s; Orca %s bridges, width %.2f, density %g %%" % (
                 ",".join(fmt_val(v, 1) for v in self.br.spans),
                 (",".join(map(str, self.br.fans)) + " %") if self.br.fans else
                 "%d steps from the picked fan (else %d %%) to 100 %%" % (self.br.n_fans, self.br.base),
                 ",".join(fmt_val(v, 1) for v in self.br.speeds), ",".join(fmt_val(f, 3) for f in self.br.flows),
                 "thick" if s.thick_bridges else "normal", bridge_flow(s, 1.0)[1], s.br_density * 100)
             if self.br else "; Bridges: -",
             "; Max flow bands: %s" % (", ".join(fmt_val(f, 2) for f in self.mvs.flows) if self.mvs else "-"),
             "; Firmware retraction: %s, z-hop %.2f" % (s.fw_retract, s.z_hop),
             "; Machine geometry (the user's own measurements, risk accepted): bed area %s, "
             "toolhead box %s (-X,+X,-Y,+Y), gantry height %g mm, nozzle protrusion %g mm"
             % (fmt_box(s.area), fmt_box(s.box), s.gantry, s.protrusion),
             "; printune_geometry: bed_area=%s toolhead_box=%s gantry_height=%g nozzle_protrusion=%g "
             "print_area=%s geom=%s" % (fmt_box(s.area), fmt_box(s.box), s.gantry, s.protrusion,
                                        fmt_box(s.bed), s.geom_hash)]
        if s.h1 < 0.25 or s.w_first < 1.2 * s.nozzle:
            h.append("; NOTE: use a first layer >= 0.25 mm and first-layer width >= 120 %% "
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
    """Return the Printune object's name as typed in Orca, or None. The '; printing object'
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
    # Orca names objects after the file, extension included ("Printune temps=260,250.stl")
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
        raise SystemExit("Printune: no layer-change tag found - is this OrcaSlicer G-code?")
    custom = [i for i, l in enumerate(lines) if l.strip() in ("; FEATURE: Custom", ";TYPE:Custom")]
    starts = [i for i in custom if i < first_layer]
    ends = [i for i in custom if i > first_layer]
    if not starts or not ends:
        raise SystemExit("Printune: could not find start/end G-code markers")
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
        sys.stderr.write("Printune: the session replaces the whole plate; these objects are not printed: %s\n"
                         % ", ".join(others))
    s = Settings(cfg, opts)
    ses = Session(s)
    ses.layout()
    head, start, tail = split_orca(lines)
    # drop Orca's own object definitions (ours replace them) and its first-line M73 stays
    head = [l for l in head if not l.startswith("EXCLUDE_OBJECT_DEFINE")]
    ins = next((i for i, l in enumerate(head) if l.strip() == "; EXECUTABLE_BLOCK_START"), 0) + 1
    defs = ["SET_GCODE_VARIABLE MACRO=_PRINTUNE VARIABLE=loaded VALUE=1 ; fails fast if printune.cfg is missing"]
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
        "SET_GCODE_VARIABLE MACRO=_PRINTUNE VARIABLE=loaded VALUE=1 ; fails fast if printune.cfg is missing"
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
                    help="override an Orca setting or Printune option (repeatable)")
    ap.add_argument("--start", default=START_GCODE, help="stand-alone start G-code")
    ap.add_argument("--end", default=END_GCODE, help="stand-alone end G-code")
    g = ap.add_argument_group("machine geometry (required; your own measurements, see README: Machine setup)")
    g.add_argument("--toolhead-box", metavar="-X,+X,-Y,+Y",
                   help="mm from the nozzle to the toolhead's edges; write it with '=' (--toolhead-box=-30,30,-40,25)")
    g.add_argument("--gantry-height", metavar="MM", help="bed to the underside of the X gantry, nozzle at Z=0")
    g.add_argument("--bed-area", metavar="X0,Y0,X1,Y1", help="where the nozzle may print")
    g.add_argument("--nozzle-protrusion", metavar="MM",
                   help="nozzle tip up to the lowest other part of the toolhead (duct, probe, sock)")
    g.add_argument("--accept-risk", action="store_true",
                   help="you measured these yourself; no warranty, zero responsibility for collisions or damage")
    a = ap.parse_args(argv)

    cfg, opts = {}, {}
    for kv in a.set:
        k, v = kv.split("=", 1)
        (opts if k in OPTIONS else cfg)[k] = v
    for k in ("toolhead_box", "gantry_height", "bed_area", "nozzle_protrusion"):
        if getattr(a, k) is not None:
            opts[k] = getattr(a, k)
    if a.accept_risk:
        opts["accept_risk"] = "1"

    if a.gcode and not a.src and not a.output:
        # Orca post-processing mode: in place, only if the Printune object is on the plate
        with open(a.gcode, encoding="utf-8", errors="replace") as f:
            lines = f.read().split("\n")
        if not find_marker(lines):
            return 0
        out, ses = build_from_orca(lines, opts)
        write(a.gcode, out)
        out_name = os.environ.get("SLIC3R_PP_OUTPUT_NAME")
        if out_name and "printune" not in os.path.basename(out_name).lower():
            base, ext = os.path.splitext(out_name)
            with open(a.gcode + ".output_name", "w", encoding="utf-8") as f:
                f.write(base + "_PRINTUNE" + ext)
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
    path = a.output or "printune_session.gcode"
    write(path, out)
    print("Printune: wrote %s (%d lines, %.1f m filament, ~%.0f min of moves + heating and pauses)"
          % (path, len(out), ses.wr.e_total / 1000, ses.wr.t_total / 60))
    for w in ses.warn:
        print("Printune warning:", w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
