# Printune: Kalico plugin. Builds the tuning session on the printer (wizard),
# runs the heat soak, and writes the picks as an OrcaSlicer filament preset.
#
# Install: ./install.sh (links this file into ~/kalico/klippy/plugins/), then add
# [printune] to printer.cfg. The file must stay next to printune_gen.py.
#
# _PRINTUNE_START (the Printune page and the PRINTUNE menu call it): generates a
# session for the chosen filament with printune_gen.py, writes it to
# <gcodes>/printune/ and starts it. Optional heat soak first.
# _PRINTUNE_EXPORT: the Printune summary calls it at the end of a session; it can
# also be run by hand after a session (_PRINTUNE_EXPORT FILE=<session file>).
#
# How the export works: an Orca G-code ends with a CONFIG_BLOCK holding every setting
# it was sliced with, including the filament preset's name and its parent.
# _PRINTUNE_EXPORT reads that block from the session file, takes the filament
# settings, puts the picked values on top (flow ratio, pressure advance,
# retraction, nozzle temperature) and writes one JSON file that Orca imports
# with File > Import > Import Configs. It always writes a new preset,
# "<filament> (Printune)", so it never replaces one of your own presets.
# A session made by the wizard carries a small block of its own with the name you
# typed, so its preset gets exactly that name.
#
# Copyright (C) 2026  Printune contributors
# This file may be distributed under the terms of the GNU GPLv3 license.

import json
import logging
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.realpath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import printune_gen as gen  # noqa: E402

# Orca's filament preset keys (Preset.cpp, s_Preset_filament_options, 2.4.x),
# without inherits/compatible_* (set below) and bed_type (a process setting).
# Only keys that appear in the sliced file are copied; the rest come from the
# parent preset (or Orca's defaults for a standalone preset).
FILAMENT_KEYS = """
filament_colour default_filament_colour filament_diameter pellet_flow_coefficient
volumetric_speed_coefficients filament_type filament_soluble filament_is_support
filament_printable filament_extruder_compatibility filament_max_volumetric_speed
filament_adaptive_volumetric_speed filament_flow_ratio filament_density
filament_adhesiveness_category filament_cost filament_minimal_purge_on_wipe_tower
filament_tower_interface_pre_extrusion_dist filament_tower_interface_pre_extrusion_length
filament_tower_ironing_area filament_tower_interface_purge_volume
filament_tower_interface_print_temp nozzle_temperature nozzle_temperature_initial_layer
cool_plate_temp textured_cool_plate_temp eng_plate_temp hot_plate_temp textured_plate_temp
cool_plate_temp_initial_layer textured_cool_plate_temp_initial_layer
eng_plate_temp_initial_layer hot_plate_temp_initial_layer textured_plate_temp_initial_layer
supertack_plate_temp_initial_layer supertack_plate_temp temperature_vitrification
reduce_fan_stop_start_freq dont_slow_down_outer_wall slow_down_for_layer_cooling fan_min_speed
fan_max_speed enable_overhang_bridge_fan overhang_fan_speed overhang_fan_threshold
close_fan_the_first_x_layers close_additional_fan_first_x_layers first_x_layer_fan_speed
full_fan_speed_layer initial_layer_fan_speed additional_fan_full_speed_layer
fan_cooling_layer_time slow_down_layer_time slow_down_min_speed filament_start_gcode
filament_end_gcode filament_change_extrusion_role_gcode activate_air_filtration
activate_air_filtration_during_print activate_air_filtration_on_completion
during_print_exhaust_fan_speed complete_print_exhaust_fan_speed filament_deretraction_speed
filament_retract_after_wipe filament_retract_before_wipe filament_retract_lift_above
filament_retract_lift_below filament_retract_lift_enforce filament_retract_restart_extra
filament_retract_when_changing_layer filament_retraction_length
filament_retraction_minimum_travel filament_retraction_speed filament_retract_length_toolchange
filament_retract_restart_extra_toolchange filament_wipe filament_z_hop filament_z_hop_types
filament_vendor filament_wipe_distance additional_cooling_fan_speed
nozzle_temperature_range_low nozzle_temperature_range_high filament_extruder_variant
enable_pressure_advance pressure_advance adaptive_pressure_advance
adaptive_pressure_advance_model adaptive_pressure_advance_overhangs
adaptive_pressure_advance_bridges chamber_temperature filament_shrink
filament_shrinkage_compensation_z support_material_interface_fan_speed
internal_bridge_fan_speed filament_notes filament_seam_gap ironing_fan_speed
filament_ironing_flow filament_ironing_spacing filament_ironing_inset filament_ironing_speed
filament_loading_speed filament_loading_speed_start filament_unloading_speed
filament_unloading_speed_start filament_toolchange_delay filament_cooling_moves
filament_stamping_loading_speed filament_stamping_distance filament_cooling_initial_speed
filament_cooling_final_speed filament_ramming_parameters filament_multitool_ramming
filament_multitool_ramming_volume filament_multitool_ramming_flow activate_chamber_temp_control
chamber_minimal_temperature filament_long_retractions_when_cut
filament_retraction_distances_when_cut idle_temperature filament_change_length
filament_flush_volumetric_speed filament_flush_temp filament_flush_temp_fast
filament_cooling_before_tower filament_ramming_volumetric_speed
filament_ramming_volumetric_speed_nc filament_ramming_travel_time
filament_ramming_travel_time_nc filament_pre_cooling_temperature
filament_pre_cooling_temperature_nc filament_preheat_temperature_delta
filament_retract_length_nc filament_change_length_nc filament_prime_volume
filament_prime_volume_nc long_retractions_when_ec retraction_distances_when_ec
filament_plugin_config_overrides filament_dev_ams_drying_ams_limitations
filament_dev_ams_drying_temperature filament_dev_ams_drying_time
filament_dev_ams_drying_heat_distortion_temperature filament_dev_chamber_drying_bed_temperature
filament_dev_chamber_drying_time filament_dev_drying_softening_temperature
filament_dev_drying_cooling_temperature
""".split()

# String options: Orca writes these C-style (';'-separated, quoted when needed)
STRING_KEYS = set("""
filament_settings_id inherits_group filament_type filament_vendor filament_start_gcode
filament_end_gcode filament_notes filament_colour default_filament_colour filament_ids
filament_change_extrusion_role_gcode filament_extruder_variant adaptive_pressure_advance_model
filament_ramming_parameters filament_plugin_config_overrides
filament_dev_ams_drying_ams_limitations
""".split())

TAIL_BYTES = 2 * 1024 * 1024
HEAD_BYTES = 2 * 1024 * 1024
SUFFIX = " (Printune)"


def unescape_strings(raw):
    """Orca's unescape_strings_cstyle: 'a;"b c";' -> ['a', 'b c', '']."""
    out, i, n = [], 0, len(raw)
    if not raw:
        return out
    while True:
        while i < n and raw[i] in " \t":
            i += 1
        buf = []
        if i < n and raw[i] == '"':
            i += 1
            while i < n and raw[i] != '"':
                c = raw[i]
                if c == "\\" and i + 1 < n:
                    i += 1
                    c = {"n": "\n", "r": "\r"}.get(raw[i], raw[i])
                buf.append(c)
                i += 1
            i += 1  # closing quote
        else:
            while i < n and raw[i] != ";":
                buf.append(raw[i])
                i += 1
        out.append("".join(buf))
        while i < n and raw[i] in " \t":
            i += 1
        if i >= n:
            return out
        if raw[i] == ";":
            i += 1
            if i >= n:
                out.append("")
                return out


def parse_value(key, raw):
    """Orca config-block value -> list of strings, one per filament slot."""
    raw = raw.strip()
    if key in STRING_KEYS or raw.startswith('"') or ";" in raw:
        return unescape_strings(raw) or [""]
    return raw.split(",") if raw else [""]


def read_config_block(path):
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        head = f.read(HEAD_BYTES).decode("utf-8", "replace")
        f.seek(max(0, size - TAIL_BYTES))
        tail = f.read().decode("utf-8", "replace")
    a = tail.rfind("; CONFIG_BLOCK_START")
    b = tail.rfind("; CONFIG_BLOCK_END")
    if a < 0 or b < a:
        return None, head
    cfg = {}
    for line in tail[a:b].splitlines():
        m = re.match(r"^; ([A-Za-z0-9_]+) = ?(.*)$", line)
        if m:
            cfg[m.group(1)] = m.group(2)
    return cfg, head


def first(cfg, key, default=""):
    if key not in cfg:
        return default
    return parse_value(key, cfg[key])[0]


def fnum(v, digits):
    s = ("%." + str(digits) + "f") % float(v)
    return s.rstrip("0").rstrip(".") if "." in s else s


def cstr(v):
    """One Orca string value, quoted C-style."""
    return '"%s"' % str(v).replace("\\", "\\\\").replace('"', '\\"')


# what the user may type for a name: also safe in G-code parameters and file names
NAME_RE = re.compile(r"[^A-Za-z0-9 ._()+-]")
TESTS = ("squish", "pa", "em", "ret")
SKIP_KEY = {"squish": "patches", "pa": "pa", "em": "em", "ret": "ret"}
DEFAULT_START = ("PRINT_START BED={bed} BED_TEMP={bed} HOTEND={nozzle} "
                 "EXTRUDER={nozzle} EXTRUDER_TEMP={nozzle}")


class Printune:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.cfg_dir = os.path.dirname(os.path.expanduser(
            self.printer.get_start_args()["config_file"]))
        self.out_dir = os.path.expanduser(
            config.get("path", os.path.join(self.cfg_dir, "printune_exports")))
        self.start_gcode = config.get("start_gcode", DEFAULT_START)
        self.end_gcode = config.get("end_gcode", "PRINT_END")
        self.enclosed = config.getboolean("enclosed", False)
        self.soak_min = config.getfloat("soak_min", 10., minval=0.) * 60.
        self.soak_max = config.getfloat("soak_max", 60., minval=1.) * 60.
        self.soak_rate = config.getfloat("soak_rate", .5, above=0.)
        self.chamber_sensor = config.get("chamber_sensor", "")
        self.pa_range = config.get("pa_range", gen.OPTIONS["pa"])
        self.ret_range = config.get("ret_range", gen.OPTIONS["ret"])
        # toolhead radius for keeping travel clear of finished prints (0 = Orca's 65 mm)
        self.toolhead_radius = config.getfloat("toolhead_radius", 0., minval=0., maxval=200.)
        # extra Orca settings for the wizard's sessions, one "key = value" per line
        self.orca = {}
        for line in config.get("orca_settings", "").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                self.orca[k.strip()] = v.strip()
        self.last_file = self.last_name = self.last_rel = ""
        self.state, self.message = "idle", ""
        self.session = {}
        self.proc = None
        self.soak = None
        self.timer = None
        self.gen_timer = None
        self.errors = 0           # counts errors, so the page sees a repeated one
        self.belt_tol = config.getfloat("belt_tolerance", 2., above=0.)
        self.belt_target = config.getfloat("belt_target", 110., minval=20., maxval=400.)
        self.belts = {}           # belt tension session, for the page and the prompts
        self.belt_now = None      # the belt being listened to (the test printer's fake plucks use it)
        self.belt_plucks = 5
        self._tool_info = None
        self.shaper = {}
        self.graphs = []
        self.graph_mark = {}
        gcode = self.printer.lookup_object("gcode")
        self.gcode = gcode
        gcode.register_command(
            "_PRINTUNE_EXPORT", self.cmd_PRINTUNE_EXPORT, desc=self.cmd_PRINTUNE_EXPORT_help)
        gcode.register_command(
            "_PRINTUNE_START", self.cmd_PRINTUNE_START, desc=self.cmd_PRINTUNE_START_help)
        gcode.register_command(
            "_PRINTUNE_SOAK_WAIT", self.cmd_PRINTUNE_SOAK_WAIT, desc=self.cmd_PRINTUNE_SOAK_WAIT_help)
        gcode.register_command(
            "_PRINTUNE_ABORT", self.cmd_PRINTUNE_ABORT, desc=self.cmd_PRINTUNE_ABORT_help)
        gcode.register_command(
            "_PRINTUNE_SOAK_SKIP", self.cmd_PRINTUNE_SOAK_SKIP, desc=self.cmd_PRINTUNE_SOAK_SKIP_help)
        gcode.register_command(
            "_PRINTUNE_BELTS_START", self.cmd_PRINTUNE_BELTS_START, desc=self.cmd_PRINTUNE_BELTS_START_help)
        gcode.register_command(
            "_PRINTUNE_BELT_LISTEN", self.cmd_PRINTUNE_BELT_LISTEN, desc=self.cmd_PRINTUNE_BELT_LISTEN_help)
        gcode.register_command(
            "PRINTUNE_RECORD", self.cmd_PRINTUNE_RECORD, desc=self.cmd_PRINTUNE_RECORD_help)
        gcode.register_command(
            "_PRINTUNE_SHAPER_RUN", self.cmd_PRINTUNE_SHAPER_RUN, desc=self.cmd_PRINTUNE_SHAPER_RUN_help)
        gcode.register_command(
            "_PRINTUNE_GRAPH", self.cmd_PRINTUNE_GRAPH, desc=self.cmd_PRINTUNE_GRAPH_help)

    def get_status(self, eventtime):
        soak = {"active": False, "elapsed": 0., "min": self.soak_min, "max": self.soak_max,
                "temp": None, "rise": None, "sensor": self._sensor_name()}
        if self.soak is not None:
            soak.update(self.soak["status"])
        return {"last_file": self.last_file, "last_name": self.last_name,
                "last_rel": self.last_rel, "state": self.state, "message": self.message,
                "errors": self.errors,
                "enclosed": self.enclosed, "session": dict(self.session), "soak": soak,
                "tools": self._tools(), "belts": self.belts, "belt_target": self.belt_target,
                "shaper": self.shaper, "graphs": self.graphs,
                "filaments": {k: {"tmin": f["t"][0], "tmax": f["t"][1], "bed": f["bed"],
                                  "soak": f["soak"]} for k, f in gen.FILAMENTS.items()}}

    # ---------------- printer tuning: which tools are installed ----------------
    def _tools(self):
        # which tuning tools are installed: fixed once klippy is ready
        if self._tool_info is None:
            kin = ""
            try:
                kin = self.printer.lookup_object("configfile").get_status(
                    self.reactor.monotonic())["settings"]["printer"]["kinematics"]
            except Exception:
                pass
            look = self.printer.lookup_object
            self._tool_info = {
                "accel": look("resonance_tester", None) is not None,
                "input_shaper": look("input_shaper", None) is not None,
                "shaketune": look("shaketune", None) is not None,
                "corexy": kin in ("corexy", "limited_corexy", "corexz", "limited_corexz"),
                "kinematics": kin}
        return self._tool_info

    # ---------------- belt tension from plucks ----------------
    cmd_PRINTUNE_BELTS_START_help = "Printune: start belt tensioning (TARGET=Hz PLUCKS=3|5)"

    def cmd_PRINTUNE_BELTS_START(self, gcmd):
        if self.printer.lookup_object("resonance_tester", None) is None:
            raise gcmd.error("Printune: belt tension needs an accelerometer ([resonance_tester])")
        if not self._tools()["corexy"]:
            raise gcmd.error("Printune: the belt step is made for CoreXY printers")
        target = gcmd.get_float("TARGET", self.belt_target, minval=20., maxval=400.)
        plucks = gcmd.get_int("PLUCKS", 5, minval=3, maxval=5)
        self.belt_plucks = plucks
        self.belts = {"target": target, "plucks": plucks, "round": 0, "phase": "coarse",
                      "A": None, "B": None, "heard": [], "advice": "Measure belt A", "done": False,
                      "tol": self.belt_tol}
        gcmd.respond_info("Printune: belt tension, target %.0f Hz, %d plucks per belt" % (target, plucks))

    cmd_PRINTUNE_BELT_LISTEN_help = "Printune: record plucks of one belt (BELT=A|B) and find its frequency"

    def cmd_PRINTUNE_BELT_LISTEN(self, gcmd):
        try:
            self._belt_listen(gcmd)
        except Exception:
            if gcmd.get_int("UI", 0):
                # don't leave a "pluck now" prompt up without a button
                self.gcode.respond_raw("// action:prompt_end")
            raise

    def _belt_listen(self, gcmd):
        import printune_belts as belts      # numpy: only needed here
        if not self.belts:
            raise gcmd.error("Printune: run _PRINTUNE_BELTS_START first")
        belt = gcmd.get("BELT", "A").upper()
        if belt not in ("A", "B"):
            raise gcmd.error("Printune: BELT is A or B")
        rt = self.printer.lookup_object("resonance_tester")
        chips = getattr(rt, "accel_chips", None) or []
        if not chips:
            raise gcmd.error("Printune: no accelerometer found")
        chip = chips[0][1]
        n = self.belts["plucks"]
        secs = gcmd.get_float("SECONDS", 2.0 + 1.3 * n, minval=2., maxval=30.)
        toolhead = self.printer.lookup_object("toolhead")
        toolhead.wait_moves()
        try:
            # a second of background first (fans, motors), so they aren't taken for the belt
            self.state, self.message = "listening", "Hands off the printer for a moment"
            client = chip.start_internal_client()
            toolhead.dwell(1.5)
            client.finish_measurements()
            quiet = client.get_samples()
            self.belt_now = belt
            self.message = "Pluck belt %s %d times, about once a second" % (belt, n)
            if gcmd.get_int("UI", 0):
                self.gcode.run_script_from_command("_PRINTUNE_BELTS_UI STEP=pluck BELT=%s" % belt)
            client = chip.start_internal_client()
            toolhead.dwell(secs)
            client.finish_measurements()
            samples = client.get_samples()
        finally:
            self.belt_now = None
            self.state, self.message = "idle", ""
        if not samples:
            raise gcmd.error("Printune: the accelerometer sent no data")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self._save_recording("belt%s-%s-quiet" % (belt, stamp), quiet)
        self._save_recording("belt%s-%s" % (belt, stamp), samples)
        t = [s[0] for s in samples]
        res = belts.analyse(t, [s[1] for s in samples], [s[2] for s in samples],
                            [s[3] for s in samples], target=self.belts["target"], expect=n,
                            quiet=[[s[i] for s in quiet] for i in (1, 2, 3)])
        b = dict(self.belts)                 # a new object: status updates send what changed
        b[belt] = res
        heard = set(b.get("heard", [])) - {belt}
        if res["freq"] is not None:
            heard.add(belt)
        heard = sorted(heard)
        if heard == ["A", "B"]:
            # both belts measured since the last instruction: the next one
            b["round"] += 1
            b["advice"], upd = belts.plan(b)
            b.update(upd)
            heard = []
        else:
            other = "B" if belt == "A" else "A"
            b["advice"] = ("Pluck belt %s again" % belt if res["freq"] is None
                           else "Now measure belt %s" % other)
        b["heard"] = heard
        self.belts = b
        if res["freq"] is None:
            gcmd.respond_info("Printune: belt %s: %s" % (belt, res["note"]))
        else:
            gcmd.respond_info("Printune: belt %s: %.1f Hz (average of %d plucks)%s" % (
                belt, res["freq"], res["used"], (" - " + res["note"]) if res["note"] else ""))

    # ---------------- raw recordings, readable from Mainsail (Kalico's own go to /tmp) ----------------
    REC_KEEP = 40

    def _save_recording(self, name, samples):
        """Write samples as ACCELEROMETER_MEASURE's CSV into printune_exports/recordings."""
        if not samples:
            return None
        d = os.path.join(self.out_dir, "recordings")
        try:
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, name + ".csv")
            with open(path, "w") as f:
                f.write("#time,accel_x,accel_y,accel_z\n")
                f.writelines("%.6f,%.6f,%.6f,%.6f\n" % tuple(s[:4]) for s in samples)
            old = sorted((os.path.getmtime(os.path.join(d, n)), n)
                         for n in os.listdir(d) if n.endswith(".csv"))
            for _, n in old[:-self.REC_KEEP]:
                os.remove(os.path.join(d, n))
            return path
        except OSError as e:
            logging.warning("printune: could not save recording %s: %s", name, e)
            return None

    cmd_PRINTUNE_RECORD_help = ("Printune: record an accelerometer for SECONDS and save it where "
                                "Mainsail can download it (CHIP=, SECONDS=, NAME=)")

    def cmd_PRINTUNE_RECORD(self, gcmd):
        name = gcmd.get("CHIP", "")
        if name:
            try:
                chip = self.printer.lookup_object(name)
            except Exception:
                chip = None
        else:
            rt = self.printer.lookup_object("resonance_tester", None)
            chips = getattr(rt, "accel_chips", None) or []
            chip = chips[0][1] if chips else None
        if chip is None or not hasattr(chip, "start_internal_client"):
            raise gcmd.error("Printune: no accelerometer %s" % (name or "found"))
        secs = gcmd.get_float("SECONDS", 10., minval=.5, maxval=30.)
        label = re.sub(r"[^A-Za-z0-9_-]", "_", gcmd.get("NAME", "")) or time.strftime("%Y%m%d-%H%M%S")
        label = "%s-%s" % (re.sub(r"[^A-Za-z0-9_-]", "_", name or "accel"), label)
        toolhead = self.printer.lookup_object("toolhead")
        toolhead.wait_moves()
        gcmd.respond_info("Printune: recording %.0f s, go" % secs)
        client = chip.start_internal_client()
        toolhead.dwell(secs)
        client.finish_measurements()
        samples = client.get_samples()
        if not samples:
            raise gcmd.error("Printune: the accelerometer sent no data")
        path = self._save_recording(label, samples)
        if not path:
            raise gcmd.error("Printune: could not save the recording")
        gcmd.respond_info("Printune: saved %d samples to %s" % (
            len(samples), os.path.relpath(path, self.cfg_dir)))

    # ---------------- input shaper (Kalico's SHAPER_CALIBRATE) ----------------
    cmd_PRINTUNE_SHAPER_RUN_help = "Printune: run SHAPER_CALIBRATE and keep its recommendation"

    def _saved_shaper(self):
        try:
            st = self.printer.lookup_object("configfile").get_status(self.reactor.monotonic())
        except Exception:
            return {}
        # what printer.cfg holds now (not an earlier, unsaved measurement)
        cur = dict(st.get("settings", {}).get("input_shaper", {}))
        out = {}
        for ax in ("x", "y"):
            if cur.get("shaper_freq_" + ax):
                out[ax] = {"type": str(cur.get("shaper_type_" + ax, cur.get("shaper_type", "mzv"))),
                           "freq": round(float(cur["shaper_freq_" + ax]), 1)}
        return out

    def cmd_PRINTUNE_SHAPER_RUN(self, gcmd):
        try:
            self._shaper_run(gcmd)
        except Exception:
            if gcmd.get_int("UI", 0):
                self.gcode.respond_raw("// action:prompt_end")
            raise

    def _shaper_run(self, gcmd):
        if self.printer.lookup_object("resonance_tester", None) is None:
            raise gcmd.error("Printune: input shaper needs an accelerometer ([resonance_tester])")
        before = self._saved_shaper()
        lines = []
        cb = lines.append
        self.gcode.register_output_handler(cb)
        self.state, self.message = "shaper", "Measuring resonances: the printer shakes for a few minutes"
        try:
            self.gcode.run_script_from_command("SHAPER_CALIBRATE")
        finally:
            if cb in self.gcode.output_callbacks:
                self.gcode.output_callbacks.remove(cb)
            self.state, self.message = "idle", ""
        res, ax, accels = {}, None, {}
        for ln in "\n".join(lines).splitlines():
            m = re.search(r"best input shaper parameters for (\w) axis", ln)
            if m:
                ax, accels = m.group(1).lower(), {}
                continue
            m = re.search(r"smoothing with '(\w+)', suggested max_accel <= ([\d.]+)", ln)
            if m:
                accels[m.group(1)] = float(m.group(2))
                continue
            m = re.search(r"Recommended shaper_type_(\w) = (\w+), shaper_freq_\w = ([\d.]+) Hz", ln)
            if m:
                a = m.group(1).lower()
                res[a] = {"type": m.group(2), "freq": float(m.group(3)),
                          "max_accel": accels.get(m.group(2)) if ax == a else None}
        if not res:
            raise gcmd.error("Printune: SHAPER_CALIBRATE gave no recommendation")
        changed = any(a not in before or before[a]["type"] != r["type"]
                      or abs(before[a]["freq"] - r["freq"]) > 1.0 for a, r in res.items())
        accs = [r["max_accel"] for r in res.values() if r.get("max_accel")]
        self.shaper = {"new": res, "old": before, "changed": changed,
                       "max_accel": min(accs) if accs else None, "time": time.time()}
        gcmd.respond_info("Printune: input shaper %s" % ", ".join(
            "%s %s %.1f Hz" % (a.upper(), r["type"], r["freq"]) for a, r in sorted(res.items())))

    # ---------------- Shake&Tune graphs ----------------
    cmd_PRINTUNE_GRAPH_help = ("Printune: list the Shake&Tune graphs made since MARK=1 "
                               "(KIND=belts|input_shaper|...)")

    def _graph_files(self, kind):
        folder = "~/printer_data/config/ShakeTune_results"
        try:
            st = self.printer.lookup_object("configfile").get_status(self.reactor.monotonic())
            folder = st["settings"].get("shaketune", {}).get("result_folder", folder)
        except Exception:
            pass
        folder = os.path.join(os.path.expanduser(folder), kind)
        try:
            return folder, {os.path.join(folder, f): os.path.getmtime(os.path.join(folder, f))
                            for f in os.listdir(folder) if f.endswith(".png")}
        except OSError:
            return folder, {}

    def cmd_PRINTUNE_GRAPH(self, gcmd):
        kind = gcmd.get("KIND", "belts")
        if not re.match(r"^[a-z_]+$", kind):
            raise gcmd.error("Printune: bad KIND")
        folder, files = self._graph_files(kind)
        if gcmd.get_int("MARK", 0):
            # what is there before the run (file times, not the clock: the clock may differ)
            self.graph_mark = files
            self.graphs = []
            return
        old = self.graph_mark or {}
        new = sorted(f for f, t in files.items() if f not in old or t > old[f])
        graphs = []
        for f in new:
            rel = os.path.relpath(f, self.cfg_dir)
            if not rel.startswith(".."):
                graphs.append(rel)        # the page can only show files under the config folder
            gcmd.respond_info("Printune: Shake&Tune graph: %s" % f)
        self.graphs = graphs
        if not new:
            gcmd.respond_info("Printune: no new Shake&Tune graph in %s" % folder)

    # ---------------- wizard: build and start a session ----------------
    cmd_PRINTUNE_START_help = "Printune: build a tuning session for a filament and start it"

    def cmd_PRINTUNE_START(self, gcmd):
        if self.state == "generating" or self.soak is not None:
            # a second tap: leave the running one alone
            raise gcmd.error("Printune: a session is already being prepared")
        try:
            self._start(gcmd)
        except gcmd.error as e:
            self._error(str(e))
            raise
        except Exception as e:
            logging.exception("_PRINTUNE_START failed")
            self._error("Printune: could not start: %s" % (e,))
            raise gcmd.error(self.message)

    def _error(self, msg):
        self.state, self.message = "error", msg
        self.errors += 1

    cmd_PRINTUNE_ABORT_help = "Printune: stop preparing a session and stop the heat soak watch"

    def cmd_PRINTUNE_ABORT(self, gcmd):
        if self.proc is not None:
            try:
                self.proc.kill()
                self.proc.wait()
            except OSError:
                pass
            self.proc = None
            path = self.pending[0]
            for f in (path + ".tmp", path + ".log"):
                if os.path.exists(f):
                    os.remove(f)
            gcmd.respond_info("Printune: stopped preparing the session")
        self.soak = None
        self.state, self.message = "idle", ""

    def _fail(self, gcmd, msg):
        raise gcmd.error("Printune: " + msg)

    def _start(self, gcmd):
        now = self.reactor.monotonic()
        ps = self.printer.lookup_object("print_stats").get_status(now)
        if ps["state"] in ("printing", "paused"):
            self._fail(gcmd, "the printer is busy (%s)" % (ps["filename"],))
        for obj, sect in (("gcode_macro _PRINTUNE", "[include printune.cfg]"),
                          ("virtual_sdcard", "[virtual_sdcard]"),
                          ("exclude_object", "[exclude_object]"),
                          ("pause_resume", "[pause_resume]"),
                          ("respond", "[respond]")):
            if self.printer.lookup_object(obj, None) is None:
                self._fail(gcmd, "add %s to printer.cfg" % (sect,))
        ftype = gcmd.get("TYPE", "PLA").upper()
        if ftype not in gen.FILAMENTS:
            self._fail(gcmd, "TYPE must be one of %s" % (", ".join(gen.FILAMENTS),))
        fil = gen.FILAMENTS[ftype]
        name = NAME_RE.sub("", gcmd.get("NAME", "")).strip()[:40] or "%s Printune" % (ftype,)
        vendor = NAME_RE.sub("", gcmd.get("VENDOR", "")).strip()[:40]
        tmin = gcmd.get_int("TMIN", fil["t"][0], minval=150, maxval=350)
        tmax = gcmd.get_int("TMAX", fil["t"][1], minval=150, maxval=350)
        tmin, tmax = min(tmin, tmax), max(tmin, tmax)
        bed = gcmd.get_int("BED", fil["bed"], minval=0, maxval=130)
        soak = gcmd.get_int("SOAK", fil["soak"] if self.enclosed else 0, minval=0, maxval=1)
        if soak and self._sensor_temp(now) is None:
            self._fail(gcmd, "chamber_sensor '%s' not found" % (self._sensor_name(),))
        tests = [t.strip().lower() for t in gcmd.get("TESTS", ",".join(TESTS)).split(",")
                 if t.strip()]
        bad = [t for t in tests if t not in TESTS]
        if bad or not tests:
            self._fail(gcmd, "TESTS is a list of %s" % (",".join(TESTS),))
        ex = self.printer.lookup_object("toolhead").get_extruder().get_name()
        settings = self.printer.lookup_object("configfile").get_status(now)["settings"]
        exc = settings.get(ex, {})
        heater = self.printer.lookup_object(ex).get_heater()
        if tmax > heater.max_temp - 5:
            self._fail(gcmd, "%d C is too close to the hotend's max_temp (%.0f)"
                       % (tmax, heater.max_temp))
        cfg, opts, mid = self._settings(ftype, tmin, tmax, exc, settings)
        opts["skip"] = ",".join(SKIP_KEY[t] for t in TESTS if t not in tests)
        start = []
        if soak:
            start.append("_PRINTUNE_SOAK BED=%d NOZZLE=%d" % (bed, mid))
        start.append(self.start_gcode.format(bed=bed, nozzle=mid))
        # in case the start macro takes other parameter names
        start += ["M190 S%d" % (bed,), "M109 S%d" % (mid,)]
        vsd = self.printer.lookup_object("virtual_sdcard")
        gdir = os.path.join(os.path.realpath(vsd.sdcard_dirname), "printune")
        os.makedirs(gdir, exist_ok=True)
        fname = re.sub(r"[^A-Za-z0-9._()+-]", "_", name) + ".gcode"
        path = os.path.join(gdir, fname)
        args = [sys.executable, os.path.join(HERE, "printune_gen.py"), "-o", path + ".tmp",
                "--start", "\n".join(start), "--end", self.end_gcode]
        for k, v in list(cfg.items()) + list(opts.items()):
            args += ["--set", "%s=%s" % (k, v)]
        block = ["; CONFIG_BLOCK_START",
                 "; filament_settings_id = " + cstr(name),
                 "; inherits_group = ;%s;" % (cstr(fil["parent"]),),
                 "; printune_named = 1",
                 "; filament_type = " + cstr(ftype),
                 "; filament_vendor = " + cstr(vendor or "Generic"),
                 "; filament_diameter = " + cfg["filament_diameter"],
                 "; nozzle_temperature = %d" % (mid,),
                 "; nozzle_temperature_initial_layer = %d" % (mid,),
                 "; nozzle_temperature_range_low = %d" % (tmin,),
                 "; nozzle_temperature_range_high = %d" % (tmax,)]
        for plate in ("hot_plate_temp", "textured_plate_temp"):
            block += ["; %s = %d" % (plate, bed), "; %s_initial_layer = %d" % (plate, bed)]
        block += ["; print_flow_ratio = 1", "; filament_flow_ratio = 1",
                  "; use_firmware_retraction = " + cfg["use_firmware_retraction"],
                  "; CONFIG_BLOCK_END"]
        log = open(path + ".log", "w")
        try:
            # low priority, so the printer's own work always comes first
            self.proc = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL, close_fds=True,
                                         preexec_fn=lambda: os.nice(10))
        finally:
            log.close()
        self.session = {"name": name, "type": ftype, "tests": tests, "soak": bool(soak),
                        "file": "printune/" + fname, "tmin": tmin, "tmax": tmax, "bed": bed}
        self.pending = (path, block)
        self.state, self.message = "generating", "Preparing the test prints"
        gcmd.respond_info("Printune: preparing \"%s\" (%s, %d-%d C, bed %d C)..."
                          % (name, ftype, tmin, tmax, bed))
        if self.gen_timer is None:
            self.gen_timer = self.reactor.register_timer(self._gen_poll)
        self.reactor.update_timer(self.gen_timer, now + .5)

    def _settings(self, ftype, tmin, tmax, exc, settings):
        """Orca settings and Printune options for a wizard session, from the printer's config."""
        th = self.printer.lookup_object("toolhead")
        st = th.get_status(self.reactor.monotonic())
        amin, amax = st["axis_minimum"], st["axis_maximum"]
        x0, y0 = max(0., amin[0]), max(0., amin[1])
        x1, y1 = amax[0], amax[1]
        fil = gen.FILAMENTS[ftype]
        nz = float(exc.get("nozzle_diameter", .4))
        mid = gen.mid_temp(tmin, tmax)
        cfg = dict(gen.DEFAULTS)
        cfg.update({
            "printable_area": "%gx%g,%gx%g,%gx%g,%gx%g" % (x0, y0, x1, y0, x1, y1, x0, y1),
            "printable_height": "%g" % (amax[2],),
            "nozzle_diameter": "%g" % (nz,),
            "filament_diameter": "%g" % (float(exc.get("filament_diameter", 1.75)),),
            "layer_height": "%.2f" % (nz * .5,),
            "initial_layer_print_height": "%.2f" % (nz * .625,),
            "line_width": "112.5%", "initial_layer_line_width": "125%",
            "outer_wall_line_width": "105%", "inner_wall_line_width": "112.5%",
            "internal_solid_infill_line_width": "105%", "sparse_infill_line_width": "112.5%",
            "top_surface_line_width": "100%",
            "nozzle_temperature": str(mid), "nozzle_temperature_initial_layer": str(mid),
            "fan_max_speed": str(fil["fan"]), "fan_min_speed": str(min(30, fil["fan"])),
            "filament_max_volumetric_speed": str(fil["vol"]),
            "use_firmware_retraction": "1" if "firmware_retraction" in settings else "0",
        })
        # never ask for more than the printer allows (SET_VELOCITY_LIMIT is not clamped)
        for k, v in list(cfg.items()):
            if v.endswith("%"):
                continue
            if k.endswith("_acceleration"):
                cfg[k] = "%g" % min(float(v), st["max_accel"])
            elif k.endswith("_speed") and k in ("initial_layer_speed", "initial_layer_infill_speed",
                                                 "outer_wall_speed", "inner_wall_speed",
                                                 "internal_solid_infill_speed", "sparse_infill_speed",
                                                 "top_surface_speed", "travel_speed"):
                cfg[k] = "%g" % min(float(v), st["max_velocity"])
        cfg.update(self.orca)
        opts = {"temps": ",".join(str(t) for t in gen.tower_temps(tmin, tmax)),
                "pa": self.pa_range, "ret": self.ret_range}
        if self.toolhead_radius:
            opts["clearance"] = "%g" % (self.toolhead_radius,)
        return cfg, opts, mid

    def _gen_poll(self, eventtime):
        if self.proc is None:
            return self.reactor.NEVER
        rc = self.proc.poll()
        if rc is None:
            return eventtime + .5
        self.proc = None
        path, block = self.pending
        try:
            with open(path + ".log") as f:
                out = f.read().strip()
        except OSError:
            out = ""
        if rc != 0 or not os.path.exists(path + ".tmp"):
            self._error("Printune: could not build the session: %s" % (
                out.splitlines()[-1] if out else "generator exit %d" % (rc,)))
            self.gcode.respond_raw("!! " + self.message)
            return self.reactor.NEVER
        try:
            with open(path + ".tmp", "a") as f:
                f.write("\n".join(block) + "\n")
            os.replace(path + ".tmp", path)
            os.remove(path + ".log")
        except OSError as e:
            self._error("Printune: could not write %s: %s" % (path, e))
            self.gcode.respond_raw("!! " + self.message)
            return self.reactor.NEVER
        for w in out.splitlines():
            if w.startswith("Printune warning"):
                self.gcode.respond_info(w)
        self.state, self.message = "idle", ""
        self.reactor.register_callback(lambda e: self._run(
            'SDCARD_PRINT_FILE FILENAME="%s"' % (self.session["file"],)))
        return self.reactor.NEVER

    def _run(self, script):
        try:
            self.gcode.run_script(script)
        except Exception as e:
            self._error("Printune: %s" % (e,))
            self.gcode.respond_raw("!! " + self.message)

    # ---------------- heat soak ----------------
    def _sensor_name(self):
        return self.chamber_sensor or "extruder"

    def _sensor_temp(self, eventtime):
        obj = self.printer.lookup_object(self._sensor_name(), None)
        if obj is None:
            return None
        return obj.get_status(eventtime).get("temperature")

    cmd_PRINTUNE_SOAK_WAIT_help = "Printune: watch the heat soak and resume when it levels off"

    def cmd_PRINTUNE_SOAK_WAIT(self, gcmd):
        now = self.reactor.monotonic()
        if self._sensor_temp(now) is None:
            raise gcmd.error("Printune: chamber_sensor '%s' not found" % (self._sensor_name(),))
        self.soak = {"t0": now, "hist": [], "paused": False,
                     "status": {"active": True, "elapsed": 0., "temp": None, "rise": None}}
        self.state = "soaking"
        if self.timer is None:
            self.timer = self.reactor.register_timer(self._soak_poll)
        self.reactor.update_timer(self.timer, now + 5.)
        gcmd.respond_info("Printune: heat soak, %.0f to %.0f min, until %s rises less than "
                          "%.1f C in 5 min" % (self.soak_min / 60, self.soak_max / 60,
                                               self._sensor_name(), self.soak_rate))

    def _soak_poll(self, eventtime):
        sk = self.soak
        if sk is None:
            return self.reactor.NEVER
        ps = self.printer.lookup_object("print_stats").get_status(eventtime)["state"]
        if ps == "paused":
            sk["paused"] = True
        elif sk["paused"] or ps not in ("printing",):
            # resumed by hand, cancelled or finished: stop watching
            self._soak_stop()
            return self.reactor.NEVER
        temp = self._sensor_temp(eventtime)
        el = eventtime - sk["t0"]
        hist = sk["hist"]
        hist.append((eventtime, temp))
        while len(hist) > 2 and hist[1][0] <= eventtime - 300.:
            hist.pop(0)
        rise = (temp - hist[0][1]) if eventtime - hist[0][0] >= 299. else None
        sk["status"].update(elapsed=el, temp=temp, rise=rise)
        done = None
        if el >= self.soak_max:
            done = "maximum time reached"
        elif el >= self.soak_min and rise is not None and abs(rise) < self.soak_rate:
            # level, not just "not rising": a cooling nozzle must not end it
            done = "temperature levelled off (%+.1f C in 5 min)" % (rise,)
        if done and sk["paused"]:
            self._soak_stop()
            self.reactor.register_callback(lambda e: self._run(
                "_PRINTUNE_SOAK_DONE MSG=\"%s\"" % (done,)))
            return self.reactor.NEVER
        return eventtime + 5.

    def _soak_stop(self):
        self.soak = None
        if self.state == "soaking":
            self.state = "idle"

    cmd_PRINTUNE_SOAK_SKIP_help = "Printune: end the heat soak now"

    def cmd_PRINTUNE_SOAK_SKIP(self, gcmd):
        if self.soak is None:
            raise gcmd.error("Printune: no heat soak running")
        self._soak_stop()
        self.gcode.run_script_from_command("_PRINTUNE_SOAK_DONE MSG=\"skipped\"")

    # ---------------- export ----------------
    def _session_file(self, gcmd):
        name = gcmd.get("FILE", None)
        vsd = self.printer.lookup_object("virtual_sdcard", None)
        if vsd is None:
            raise gcmd.error("_PRINTUNE_EXPORT: needs [virtual_sdcard]")
        root = os.path.realpath(vsd.sdcard_dirname)
        if name is None:
            cur = vsd.file_path()
            if cur:
                return cur
            ps = self.printer.lookup_object("print_stats")
            name = ps.get_status(self.printer.get_reactor().monotonic())["filename"]
        if not name:
            raise gcmd.error("_PRINTUNE_EXPORT: no session file (give FILE=<name>)")
        path = os.path.realpath(os.path.join(root, name))
        if not path.startswith(root + os.sep):
            raise gcmd.error("_PRINTUNE_EXPORT: FILE must be inside the G-code folder")
        return path

    cmd_PRINTUNE_EXPORT_help = "Printune: write the picked values as an Orca filament preset"

    def cmd_PRINTUNE_EXPORT(self, gcmd):
        # SOFT=1 (used by the Printune summary): report problems instead of
        # raising, so the results prompt still shows
        self.last_file = self.last_name = self.last_rel = ""
        try:
            try:
                self._export(gcmd)
            except gcmd.error:
                raise
            except Exception as e:
                # never let a file or parsing problem shut the printer down
                logging.exception("_PRINTUNE_EXPORT failed")
                raise gcmd.error("_PRINTUNE_EXPORT failed: %s" % (e,))
        except gcmd.error as e:
            if not gcmd.get_int("SOFT", 0):
                raise
            gcmd.respond_info("%s\nNo preset file written; type the values in Orca." % (e,))

    def _export(self, gcmd):
        path = self._session_file(gcmd)
        if not os.path.exists(path):
            raise gcmd.error("_PRINTUNE_EXPORT: %s not found" % (path,))
        cfg, head = read_config_block(path)
        if "_PRINTUNE_BEGIN" not in head:
            raise gcmd.error("_PRINTUNE_EXPORT: %s is not a Printune session" % (os.path.basename(path),))
        if not cfg or "filament_settings_id" not in cfg:
            raise gcmd.error(
                "_PRINTUNE_EXPORT: %s has no Orca settings block (sliced with Orca and printune_gen.py?)"
                % (os.path.basename(path),))
        macro = self.printer.lookup_object("gcode_macro _PRINTUNE", None)
        if macro is None:
            raise gcmd.error("_PRINTUNE_EXPORT: printune.cfg is not included")
        S = macro.variables
        tested = set(str(S.get("tested", "")).split(","))
        fids = parse_value("filament_settings_id", cfg["filament_settings_id"])
        if len(fids) != 1:
            raise gcmd.error("_PRINTUNE_EXPORT: the slice has %d filaments; slice Printune with one"
                             % (len(fids),))
        fid = fids[0]
        groups = unescape_strings(cfg.get("inherits_group", ""))
        # inherits_group = print;filament;printer
        parent = groups[1] if len(groups) == 3 else ""
        if first(cfg, "printune_named") == "1":
            # made by the wizard: the name you typed, on the generic parent
            name, inherits = fid, parent
        elif fid.endswith(SUFFIX):
            # tuning an earlier export again: the import replaces it
            name, inherits = fid, parent
        elif parent:
            # your own preset: a copy of it, on the same system parent
            name, inherits = fid + SUFFIX, parent
        elif " @" in fid:
            # a system preset: a child of it
            # "Generic ABS @System" -> "Generic ABS (Printune)", "Generic ABS @Voron"
            # -> "Generic ABS Voron (Printune)", so different vendors don't collide
            base, vendor = fid.rsplit(" @", 1)
            name = base + ("" if vendor == "System" else " " + vendor) + SUFFIX
            inherits = fid
        else:
            # a root preset (yours, or a vendor's without '@'): a standalone
            # copy holding every filament setting of the slice
            name, inherits = fid + SUFFIX, ""
        preset = {
            "type": "filament",
            "name": name,
            "inherits": inherits,
            "from": "User",
            "instantiation": "true",
        }
        for k in FILAMENT_KEYS:
            if k in cfg:
                preset[k] = [parse_value(k, cfg[k])[0]]
        changed = []

        def put(key, value, label):
            old = preset.get(key, [""])[0]
            preset[key] = [value]
            changed.append("%s %s -> %s" % (label, old or "-", value))

        if "em" in tested:
            pfr = float(first(cfg, "print_flow_ratio", "1") or 1.0) or 1.0
            put("filament_flow_ratio", fnum(float(S["em"]) / pfr, 4), "flow ratio")
        if "pa" in tested:
            put("enable_pressure_advance", "1", "PA enabled")
            put("pressure_advance", fnum(S["pa"], 4), "pressure advance")
        if "ret" in tested:
            if int(S.get("ret_temp", 0)) > 0:
                put("nozzle_temperature", str(int(S["ret_temp"])), "nozzle temp")
            ln, sp = fnum(S["ret_len"], 2), fnum(S["ret_speed"], 0)
            put("filament_retraction_length", ln, "retraction length")
            put("filament_retraction_speed", sp, "retraction speed")
            put("filament_deretraction_speed", sp, "deretraction speed")
            if first(cfg, "use_firmware_retraction") == "1":
                # firmware retraction is printer-wide: send it per filament
                start = preset.get("filament_start_gcode", [""])[0]
                lines = [l for l in start.split("\n")
                         if not l.strip().upper().startswith("SET_RETRACTION")]
                while lines and not lines[-1].strip():
                    lines.pop()
                lines.append(
                    "SET_RETRACTION RETRACT_LENGTH=%s RETRACT_SPEED=%s "
                    "UNRETRACT_EXTRA_LENGTH=0 UNRETRACT_SPEED=%s" % (ln, sp, sp))
                preset["filament_start_gcode"] = ["\n".join(lines)]
                changed.append("filament start G-code: SET_RETRACTION line")
        if not changed:
            raise gcmd.error("_PRINTUNE_EXPORT: nothing was picked in this session")
        preset["filament_settings_id"] = [name]
        m = re.search(r"OrcaSlicer (\d+)\.(\d+)\.(\d+)", head)
        preset["version"] = "%s.%s.%s.0" % m.groups() if m else "2.0.0.0"
        preset["filament_notes"] = [
            ((preset.get("filament_notes", [""])[0] + "\n").lstrip()
             + "Printune %s (from %s): %s" % (time.strftime("%Y-%m-%d"), fid, "; ".join(changed)))]
        os.makedirs(self.out_dir, exist_ok=True)
        fname = re.sub(r"[^A-Za-z0-9 ._()@+-]", "_", name).strip() + ".json"
        out = os.path.join(self.out_dir, fname)
        with open(out + ".tmp", "w") as f:
            json.dump(preset, f, indent=4)
        os.replace(out + ".tmp", out)
        rel = os.path.relpath(out, os.path.dirname(self.cfg_dir))
        self.last_file, self.last_name = out, name
        self.last_rel = out if rel.startswith("..") else rel
        logging.info("_PRINTUNE_EXPORT wrote %s (%s)", out, "; ".join(changed))
        gcmd.respond_info(
            "Printune: Orca filament preset \"%s\" written to %s\n%s\n"
            "Download it (Mainsail: Machine tab) and import it in Orca: "
            "File > Import > Import Configs.%s"
            % (name, self.last_rel, "\n".join(changed),
               " It replaces a preset of the same name." if name == fid else ""))


def load_config(config):
    return Printune(config)
