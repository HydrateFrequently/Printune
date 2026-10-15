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
# retraction, nozzle temperature, fan and minimum layer time, bridge fan, max volumetric speed)
# and writes one JSON file that Orca imports
# with File > Import > Import Configs. It always writes a new preset,
# "<filament> (Printune)", so it never replaces one of your own presets.
# The bridge test's speed and flow are process settings: they go into a second JSON file, an Orca
# process preset (a copy of the sliced process preset with those two values changed, or, for a
# session made on the printer, just those two values).
# A session made by the wizard carries a small block of its own with the name you
# typed, so its preset gets exactly that name.
#
# Copyright (C) 2026  Printune contributors
# This file may be distributed under the terms of the GNU GPLv3 license.

import json
import logging
import math
import os
import re
import subprocess
import sys
import threading
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

# Orca's process preset keys (Preset.cpp, s_Preset_print_options), without inherits and
# compatible_*; the option types are from PrintConfig.cpp. Per-extruder-variant (vector) options
# are lists in a preset file, the rest single values. Only keys in the sliced file are copied.
PROCESS_KEYS = """
layer_height initial_layer_print_height wall_loops alternate_extra_wall slice_closing_radius
spiral_mode spiral_mode_smooth spiral_mode_max_xy_smoothing spiral_starting_flow_ratio
spiral_finishing_flow_ratio slicing_mode top_shell_layers top_shell_thickness top_surface_density
bottom_surface_density bottom_shell_layers bottom_shell_thickness extra_perimeters_on_overhangs
ensure_vertical_shell_thickness reduce_crossing_wall detect_thin_wall detect_overhang_wall
unsupported_wall_last overhang_reverse overhang_reverse_threshold overhang_reverse_internal_only
wall_direction seam_position staggered_inner_seams wall_sequence is_infill_first
sparse_infill_density fill_multiline tpms_adaptive tpms_interior_density tpms_adaptive_gradient
gyroid_optimized sparse_infill_pattern sparse_infill_smooth_factor lateral_lattice_angle_1
lateral_lattice_angle_2 infill_overhang_angle lightning_overhang_angle lightning_prune_angle
lightning_straightening_angle top_surface_pattern top_surface_expansion
top_surface_expansion_margin top_surface_expansion_direction bottom_surface_pattern
top_surface_fill_order bottom_surface_fill_order infill_direction solid_infill_direction
top_layer_direction bottom_layer_direction counterbore_hole_bridging infill_shift_step
sparse_infill_rotate_template solid_infill_rotate_template symmetric_infill_y_axis
skeleton_infill_density infill_lock_depth skin_infill_depth skin_infill_density
infill_complete_top align_infill_direction_to_model extra_solid_infills center_of_surface_pattern
separated_infills minimum_sparse_infill_area reduce_infill_retraction
internal_solid_infill_pattern gap_fill_target ironing_type ironing_pattern ironing_flow
ironing_speed ironing_spacing ironing_angle ironing_angle_fixed ironing_inset support_ironing
support_ironing_pattern support_ironing_flow support_ironing_spacing max_travel_detour_distance
fuzzy_skin fuzzy_skin_thickness fuzzy_skin_point_distance fuzzy_skin_first_layer
fuzzy_skin_noise_type fuzzy_skin_mode fuzzy_skin_scale fuzzy_skin_octaves fuzzy_skin_persistence
fuzzy_skin_ripples_per_layer fuzzy_skin_ripple_offset fuzzy_skin_layers_between_ripple_offset
max_volumetric_extrusion_rate_slope max_volumetric_extrusion_rate_slope_segment_length
extrusion_rate_smoothing_external_perimeter_only inner_wall_speed outer_wall_speed
sparse_infill_speed internal_solid_infill_speed top_surface_speed support_speed
support_object_xy_distance support_object_first_layer_gap support_interface_speed bridge_speed
internal_bridge_speed gap_infill_speed travel_speed travel_speed_z initial_layer_speed
outer_wall_acceleration initial_layer_acceleration top_surface_acceleration default_acceleration
skirt_type skirt_loops skirt_speed min_skirt_length skirt_distance skirt_start_angle skirt_height
single_loop_draft_shield draft_shield brim_width leading_brim_length extra_brim_width
brim_object_gap brim_flow_ratio brim_use_efc_outline combine_brims brim_type brim_ears_max_angle
brim_ears_detection_length brim_ears_outer_only enable_support support_type
support_threshold_angle support_threshold_overlap enforce_support_layers raft_layers
raft_first_layer_density raft_first_layer_expansion raft_contact_distance raft_expansion
support_base_pattern support_base_pattern_spacing support_expansion support_style
print_extruder_id print_extruder_variant independent_support_layer_height support_angle
support_interface_top_layers support_interface_bottom_layers support_interface_pattern
support_interface_spacing support_interface_loop_pattern support_top_z_distance
support_on_build_plate_only support_critical_regions_only bridge_no_support thick_bridges
thick_internal_bridges dont_filter_internal_bridges enable_extra_bridge_layer max_bridge_length
print_sequence print_order support_remove_small_overhang filename_format outer_wall_filament_id
inner_wall_filament_id support_bottom_z_distance sparse_infill_filament_id
internal_solid_filament_id top_surface_filament_id bottom_surface_filament_id support_filament
support_interface_filament support_interface_not_for_body ooze_prevention
standby_temperature_delta preheat_time preheat_steps interface_shells line_width
initial_layer_line_width inner_wall_line_width outer_wall_line_width sparse_infill_line_width
internal_solid_infill_line_width skin_infill_line_width skeleton_infill_line_width
top_surface_line_width support_line_width infill_wall_overlap top_bottom_infill_wall_overlap
bridge_flow bridge_line_width internal_bridge_flow elefant_foot_compensation
elefant_foot_compensation_layers elefant_foot_layers_density xy_contour_compensation
xy_hole_compensation resolution enable_prime_tower prime_tower_enable_framework prime_tower_width
prime_tower_brim_width prime_tower_skip_points prime_volume prime_tower_infill_gap
prime_tower_flat_ironing belt_purge_tower_width belt_purge_tower_object
enable_tower_interface_features enable_tower_interface_cooldown_during_tower
wipe_tower_no_sparse_layers wipe_tower_sparse_layers_combination flush_into_infill
flush_into_objects flush_into_support enable_mixed_color_sublayer tree_support_branch_angle
tree_support_angle_slow
""".split()
PROCESS_VECTOR_KEYS = set("""
inner_wall_speed outer_wall_speed sparse_infill_speed internal_solid_infill_speed
top_surface_speed support_speed support_interface_speed bridge_speed internal_bridge_speed
gap_infill_speed travel_speed travel_speed_z initial_layer_speed outer_wall_acceleration
initial_layer_acceleration top_surface_acceleration default_acceleration print_extruder_id
print_extruder_variant
""".split())
PROCESS_STRING_KEYS = set("""
sparse_infill_rotate_template solid_infill_rotate_template extra_solid_infills filename_format
print_extruder_variant
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


def unescape_cstyle(raw):
    """Orca's unescape_string_cstyle for a single string option: \\n, \\r, \\\\ and \\"."""
    out, i = [], 0
    while i < len(raw):
        c = raw[i]
        if c == "\\" and i + 1 < len(raw):
            i += 1
            c = {"n": "\n", "r": "\r"}.get(raw[i], raw[i])
        out.append(c)
        i += 1
    return "".join(out)


def process_value(key, raw):
    """Orca config-block value of a process option -> its value in a preset file."""
    raw = raw.strip()
    if key in PROCESS_VECTOR_KEYS:
        if key in PROCESS_STRING_KEYS:
            return unescape_strings(raw) or [""]
        return raw.split(",") if raw else [""]
    return unescape_cstyle(raw) if key in PROCESS_STRING_KEYS else raw


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


def pval(preset, key):
    """One value of an exported preset (Orca writes most of them as one-item lists), as text."""
    if not isinstance(preset, dict):
        return ""
    v = preset.get(key, "")
    if isinstance(v, list):
        v = v[0] if v else ""
    return str(v).strip()


def fnum_or(text):
    """A number from text, else None."""
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def cstr(v):
    """One Orca string value, quoted C-style."""
    return '"%s"' % str(v).replace("\\", "\\\\").replace('"', '\\"')


# what the user may type for a name: also safe in G-code parameters and file names
NAME_RE = re.compile(r"[^A-Za-z0-9 ._()+-]")
# every test in print order, and the two procedures (Tuning Wizard, Advanced tuning)
TESTS = gen.ORDER
SKIP_KEY = gen.SKIP_KEY
PROCS = gen.PROCS
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
        self.mvs_range = config.get("mvs_range", "")      # "" = per filament type
        if config.get("toolhead_radius", None) is not None:
            # replaced by the machine setup's toolhead box; read so an old config still starts
            logging.warning("printune: toolhead_radius is no longer used: the machine setup "
                            "(toolhead_box) replaces it")
        # Machine setup: the user's own measurements, saved under [printune] by SAVE_CONFIG.
        # Nothing that prints is generated until all of it is set and the risk is accepted.
        self.setup = {}
        for key, parse in (("bed_area", gen.parse_bed_area), ("toolhead_box", gen.parse_toolhead_box),
                           ("gantry_height", gen.parse_gantry_height),
                           ("nozzle_protrusion", gen.parse_nozzle_protrusion)):
            raw = config.get(key, "").strip()
            if raw:
                try:
                    self.setup[key] = parse(raw)
                except ValueError as e:
                    raise config.error("[printune] %s (fix it, or delete it and run the machine setup "
                                       "again)" % (e,))
        # the acceptance holds the fingerprint of the values it was given for: a value changed
        # later (also by hand in printer.cfg) no longer matches it, so it has to be accepted again
        acc = config.get("setup_accepted", "").strip()
        self.setup["accepted"] = ""
        self._accept_note = ""
        if acc and acc == self._geom_hash():
            self.setup["accepted"] = acc
        elif acc and acc.lower() not in ("no", "false", "0"):
            self._accept_note = ("the saved acceptance is for other values (changed by hand, or saved by "
                                 "an older Printune): check the values and accept again")
            logging.info("printune: machine setup: %s", self._accept_note)
        self._setup_saved = dict(self.setup)     # what printer.cfg holds
        self._bed_hint = None
        # extra Orca settings for the wizard's sessions, one "key = value" per line
        self.orca = {}
        for line in config.get("orca_settings", "").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                self.orca[k.strip()] = v.strip()
        self.last_file = self.last_name = self.last_rel = ""
        self.last_proc_name = self.last_proc_rel = ""
        self.state, self.message = "idle", ""
        self.session = {}
        self.proc = None
        self.soak = None
        self.timer = None
        self.gen_timer = None
        self.errors = 0           # counts errors, so the page sees a repeated one
        self.belt_tol = config.getfloat("belt_tolerance", 0.25, above=0.)   # Hz, average of 5 plucks
        self.belt_target = config.getfloat("belt_target", 110., minval=20., maxval=400.)
        self.belts = {}           # belt tension session, for the page and the prompts
        self.belt_now = None      # the belt being listened to (the test printer's fake plucks use it)
        self.belt_plucks = 5
        self.belt_live = []       # plucks heard so far while listening (the page shows them live)
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
            "_PRINTUNE_BELT_TAP", self.cmd_PRINTUNE_BELT_TAP, desc=self.cmd_PRINTUNE_BELT_TAP_help)
        gcode.register_command(
            "PRINTUNE_RECORD", self.cmd_PRINTUNE_RECORD, desc=self.cmd_PRINTUNE_RECORD_help)
        gcode.register_command(
            "_PRINTUNE_SHAPER_RUN", self.cmd_PRINTUNE_SHAPER_RUN, desc=self.cmd_PRINTUNE_SHAPER_RUN_help)
        gcode.register_command(
            "_PRINTUNE_GRAPH", self.cmd_PRINTUNE_GRAPH, desc=self.cmd_PRINTUNE_GRAPH_help)
        gcode.register_command(
            "_PRINTUNE_SETUP", self.cmd_PRINTUNE_SETUP, desc=self.cmd_PRINTUNE_SETUP_help)

    def get_status(self, eventtime):
        soak = {"active": False, "elapsed": 0., "min": self.soak_min, "max": self.soak_max,
                "temp": None, "rise": None, "sensor": self._sensor_name()}
        if self.soak is not None:
            soak.update(self.soak["status"])
        return {"last_file": self.last_file, "last_name": self.last_name,
                "last_rel": self.last_rel, "last_proc_name": self.last_proc_name,
                "last_proc_rel": self.last_proc_rel, "state": self.state, "message": self.message,
                "errors": self.errors,
                "enclosed": self.enclosed, "session": dict(self.session), "soak": soak,
                "tools": self._tools(), "belts": self.belts, "belt_target": self.belt_target,
                "belt_now": self.belt_now if self.belt_now in ("A", "B") else None,
                "belt_live": self.belt_live,
                "shaper": self.shaper, "graphs": self.graphs, "setup": self._setup_status(),
                "procs": {k: list(v) for k, v in PROCS.items()},
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
            cmds = getattr(look("gcode"), "ready_gcode_handlers", {})
            # Z offset routine: a probe (BLTouch, inductive, eddy), a Beacon (it has no
            # PROBE_CALIBRATE; BEACON_CALIBRATE is its paper test), or the Z endstop
            zcal = ("probe" if "PROBE_CALIBRATE" in cmds else
                    "beacon" if "BEACON_CALIBRATE" in cmds else
                    "endstop" if "Z_ENDSTOP_CALIBRATE" in cmds else "")
            self._tool_info = {
                "zcal": zcal,
                "accel": look("resonance_tester", None) is not None,
                "input_shaper": look("input_shaper", None) is not None,
                "shaketune": look("shaketune", None) is not None,
                "corexy": kin in ("corexy", "limited_corexy", "corexz", "limited_corexz"),
                "kinematics": kin}
        return self._tool_info

    # ---------------- belt tension from plucks ----------------
    cmd_PRINTUNE_BELTS_START_help = "Printune: start belt tensioning (TARGET=Hz)"

    def cmd_PRINTUNE_BELTS_START(self, gcmd):
        if self.printer.lookup_object("resonance_tester", None) is None:
            raise gcmd.error("Printune: belt tension needs an accelerometer ([resonance_tester])")
        if not self._tools()["corexy"]:
            raise gcmd.error("Printune: the belt step is made for CoreXY printers")
        target = gcmd.get_float("TARGET", self.belt_target, minval=20., maxval=400.)
        plucks = 5      # on target: the average of 5 good plucks
        self.belt_plucks = plucks
        self.belts = {"target": target, "plucks": plucks, "round": 0, "phase": "coarse",
                      "A": None, "B": None, "heard": [], "advice": "Measure belt A", "done": False,
                      "tol": self.belt_tol, "tap": [], "fails": {"A": 0, "B": 0}, "deaf": False,
                      "possible": False, "good": {}, "prev": {}}
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
            self.belt_live = []
            self.state, self.message = "listening", "Hands off the printer for a moment"
            quiet = self._record(chip, toolhead, 1.5)
            self.belt_now = belt
            self.message = "Pluck belt %s %d times, evenly, about once a second" % (belt, n)
            if gcmd.get_int("UI", 0):
                self.gcode.run_script_from_command("_PRINTUNE_BELTS_UI STEP=pluck BELT=%s" % belt)
            live = dict(quiet=[[s[i] for s in quiet] for i in (1, 2, 3)], expect=n,
                        avoid=self.belts.get("tap", []), target=self.belts["target"],
                        prev=(self.belts.get("good") or {}).get(belt),
                        other=(self.belts.get("B" if belt == "A" else "A") or {}).get("freq"))
            samples = self._record(chip, toolhead, secs, live=live)
        finally:
            self.belt_now = None
            self.state, self.message = "idle", ""
        if not samples:
            raise gcmd.error("Printune: the accelerometer sent no data")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self._save_recording("belt%s-%s-quiet" % (belt, stamp), quiet)
        self._save_recording("belt%s-%s" % (belt, stamp), samples)
        t = [s[0] for s in samples]
        other = (self.belts.get("B" if belt == "A" else "A") or {}).get("freq")
        good = dict(self.belts.get("good") or {})
        res = belts.analyse(t, [s[1] for s in samples], [s[2] for s in samples],
                            [s[3] for s in samples], target=self.belts["target"], expect=n,
                            quiet=[[s[i] for s in quiet] for i in (1, 2, 3)],
                            avoid=self.belts.get("tap", []), other=other, prev=good.get(belt))
        self.belt_live = res["plucks"]
        b = dict(self.belts)                 # a new object: status updates send what changed
        b[belt] = res
        if res["freq"] is not None:
            b["prev"] = dict(b.get("prev") or {}, **({belt: good[belt]} if belt in good else {}))
            good[belt] = res["freq"]
            b["good"] = good
            b["possible"] = True
        # a sensor that can't hear the belts at all: say so instead of guessing
        fails = dict(b.get("fails") or {"A": 0, "B": 0})
        # a failed series: not one clear pluck in it (plucks that only disagree don't count)
        clear = any(p["ok"] or p["why"] not in ("no belt tone", "too weak", "a knock, not a belt")
                    for p in res["plucks"])
        fails[belt] = 0 if clear else fails.get(belt, 0) + 1
        b["fails"] = fails
        ever = b.get("ever") or res["freq"] is not None
        b["ever"] = ever
        b["deaf"] = not ever and fails[belt] >= 2
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
        for p in res["plucks"]:
            if not p["ok"]:
                gcmd.respond_info("Printune: belt %s, pluck at %.1f s left out: %s" % (belt, p["t"], p["why"]))
        if res["freq"] is None:
            gcmd.respond_info("Printune: belt %s: %s" % (belt, res["note"]))
        else:
            gcmd.respond_info("Printune: belt %s: %.1f Hz (%s)" % (belt, res["freq"], res["note"]))

    cmd_PRINTUNE_BELT_TAP_help = ("Printune: learn how the toolhead rings by itself from one light tap, "
                                  "so a tap is never taken for a belt")

    def cmd_PRINTUNE_BELT_TAP(self, gcmd):
        try:
            self._belt_tap(gcmd)
        except Exception:
            if gcmd.get_int("UI", 0):
                self.gcode.respond_raw("// action:prompt_end")
            raise

    def _belt_tap(self, gcmd):
        import printune_belts as belts
        if not self.belts:
            raise gcmd.error("Printune: run _PRINTUNE_BELTS_START first")
        rt = self.printer.lookup_object("resonance_tester")
        chips = getattr(rt, "accel_chips", None) or []
        if not chips:
            raise gcmd.error("Printune: no accelerometer found")
        chip = chips[0][1]
        toolhead = self.printer.lookup_object("toolhead")
        toolhead.wait_moves()
        try:
            self.state, self.message = "listening", "Hands off the printer for a moment"
            quiet = self._record(chip, toolhead, 1.5)
            self.belt_now = "tap"
            self.message = "Tap the toolhead once, lightly"
            if gcmd.get_int("UI", 0):
                self.gcode.run_script_from_command("_PRINTUNE_BELTS_UI STEP=tapnow")
            samples = self._record(chip, toolhead, 3.0)
        finally:
            self.belt_now = None
            self.state, self.message = "idle", ""
        if not samples:
            raise gcmd.error("Printune: the accelerometer sent no data")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self._save_recording("tap-%s-quiet" % stamp, quiet)
        self._save_recording("tap-%s" % stamp, samples)
        res = belts.analyse_tap([s[0] for s in samples], [s[1] for s in samples],
                                [s[2] for s in samples], [s[3] for s in samples],
                                quiet=[[s[i] for s in quiet] for i in (1, 2, 3)])
        b = dict(self.belts)
        b["tap"] = res["modes"]
        b["tap_note"] = res["note"]
        self.belts = b
        if res["modes"]:
            gcmd.respond_info("Printune: the toolhead rings by itself at %s Hz; ignored when listening to the belts"
                              % ", ".join("%.1f" % m for m in res["modes"]))
        else:
            gcmd.respond_info("Printune: tap: %s" % res["note"])

    # ---------------- raw recordings, readable from Mainsail (Kalico's own go to /tmp) ----------------
    REC_KEEP = 40

    def _record(self, chip, toolhead, secs, live=None):
        """Record for secs. live: analyse() arguments; then the plucks heard so far are
        put in belt_live about once a second while recording (in a thread, so a slow
        host doesn't hold up Kalico)."""
        client = chip.start_internal_client()
        timer = None
        if live is not None:
            import printune_belts as belts
            busy = []
            self._live_gen = gen_now = getattr(self, "_live_gen", 0) + 1

            def work(samples):
                try:
                    res = belts.analyse([s[0] for s in samples], [s[1] for s in samples],
                                        [s[2] for s in samples], [s[3] for s in samples], **live)
                    if self.belt_now in ("A", "B") and self._live_gen == gen_now:
                        self.belt_live = res["plucks"]
                except Exception:
                    logging.exception("printune: live belt analysis")
                finally:
                    busy.clear()

            def tick(eventtime):
                try:
                    if not busy:
                        samples = [s for m in list(client.msgs) for s in m["data"]
                                   if s[0] >= client.request_start_time]
                        if len(samples) > 200:
                            busy.append(1)
                            threading.Thread(target=work, args=(samples,), daemon=True).start()
                except Exception:
                    logging.exception("printune: live belt samples")
                    return self.reactor.NEVER
                return eventtime + 1.0
            timer = self.reactor.register_timer(tick, self.reactor.monotonic() + 1.5)
        try:
            toolhead.dwell(secs)
        finally:
            try:
                client.finish_measurements()     # never leave the chip streaming
            finally:
                # after finish: dwell returns while ~2 s are still queued, keep showing those
                if timer is not None:
                    self.reactor.unregister_timer(timer)
                    self._live_gen += 1          # a late live result can't overwrite the final one
        return client.get_samples()

    def _find_chip(self, name):
        """A chip by Kalico's CHIP= name (beacon, hotend for [adxl345 hotend]) or its section name."""
        rt = self.printer.lookup_object("resonance_tester", None)
        chips = getattr(rt, "accel_chips", None) or []
        if not name:
            return chips[0][1] if chips else None
        cands = [name] + ["%s %s" % (k, name) for k in
                          ("adxl345", "lis2dw", "lis3dh", "mpu9250", "icm20948", "bmi160")]
        for c in cands:
            obj = self.printer.lookup_object(c, None)
            if obj is not None and hasattr(obj, "start_internal_client"):
                return obj
        return None

    def _save_recording(self, name, samples):
        """Write samples as ACCELEROMETER_MEASURE's CSV into printune_exports/recordings."""
        if not samples:
            return None
        d = os.path.join(self.out_dir, "recordings")
        path = os.path.join(d, name + ".csv")
        keep = self.REC_KEEP

        def write():
            # in a thread like Kalico's own writer: a 30 s recording takes a while on a Pi
            try:
                os.makedirs(d, exist_ok=True)
                with open(path + ".part", "w") as f:
                    f.write("#time,accel_x,accel_y,accel_z\n")
                    f.writelines("%.6f,%.6f,%.6f,%.6f\n" % tuple(s[:4]) for s in samples)
                os.replace(path + ".part", path)
                old = sorted((os.path.getmtime(os.path.join(d, n)), n)
                             for n in os.listdir(d) if n.endswith(".csv"))
                for _, n in old[:-keep]:
                    os.remove(os.path.join(d, n))
            except OSError as e:
                logging.warning("printune: could not save recording %s: %s", name, e)
        threading.Thread(target=write, daemon=True).start()
        return path

    cmd_PRINTUNE_RECORD_help = ("Printune: record an accelerometer for SECONDS and save it where "
                                "Mainsail can download it (CHIP=, SECONDS=, NAME=)")

    def cmd_PRINTUNE_RECORD(self, gcmd):
        name = gcmd.get("CHIP", "")
        chip = self._find_chip(name)
        if chip is None:
            raise gcmd.error("Printune: no accelerometer %s" % (name or "found"))
        secs = gcmd.get_float("SECONDS", 10., minval=.5, maxval=30.)
        label = re.sub(r"[^A-Za-z0-9_-]", "_", gcmd.get("NAME", "")) or time.strftime("%Y%m%d-%H%M%S")
        label = "%s-%s" % (re.sub(r"[^A-Za-z0-9_-]", "_", name or "accel"), label)
        toolhead = self.printer.lookup_object("toolhead")
        toolhead.wait_moves()
        gcmd.respond_info("Printune: recording %.0f s, go" % secs)
        samples = self._record(chip, toolhead, secs)
        if not samples:
            raise gcmd.error("Printune: the accelerometer sent no data")
        path = self._save_recording(label, samples)
        gcmd.respond_info("Printune: saving %d samples to %s" % (
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

    # ---------------- machine setup (mandatory before anything prints) ----------------
    SETUP_KEYS = ("bed_area", "toolhead_box", "gantry_height", "nozzle_protrusion")
    SETUP_NAMES = {"bed_area": "bed area", "toolhead_box": "toolhead box", "gantry_height": "gantry height",
                   "nozzle_protrusion": "nozzle protrusion", "risk": "accepting the risk"}

    def _axes(self):
        """The axis limits (x0, y0, x1, y1) where the nozzle can go."""
        st = self.printer.lookup_object("toolhead").get_status(self.reactor.monotonic())
        amin, amax = st["axis_minimum"], st["axis_maximum"]
        return (amin[0], amin[1], amax[0], amax[1])

    def _past_axes(self, a):
        """None, or the error text when the bed area a reaches past the axis limits."""
        ax = self._axes()
        if a[0] < ax[0] - 1e-6 or a[1] < ax[1] - 1e-6 or a[2] > ax[2] + 1e-6 or a[3] > ax[3] + 1e-6:
            return ("the bed area %s reaches past the axis limits %s"
                    % (gen.fmt_box(a), ",".join("%g" % round(v, 3) for v in ax)))
        return None

    def _bed_suggestion(self):
        """The default bed area: [bed_mesh] mesh_min/mesh_max (where the probe has been, so on the
        plate), cut to the axis limits and rounded inward to 0.1 mm, so it never reaches past them.
        None without a rectangular [bed_mesh]."""
        if self._bed_hint is None:
            hint = {"area": None, "source": "", "note": ""}
            try:
                cf = self.printer.lookup_object("configfile").get_status(self.reactor.monotonic())
                raw = cf.get("config", {}).get("bed_mesh")
                if raw is None:
                    hint["note"] = "No [bed_mesh] in the printer's config: enter the bed area yourself"
                elif "mesh_min" not in raw or "mesh_max" not in raw:
                    hint["note"] = "The bed mesh is round: enter the bed area yourself"
                else:
                    mn = [float(v) for v in str(raw["mesh_min"]).split(",")]
                    mx = [float(v) for v in str(raw["mesh_max"]).split(",")]
                    ax = self._axes()
                    a = (max(mn[0], ax[0]), max(mn[1], ax[1]), min(mx[0], ax[2]), min(mx[1], ax[3]))
                    up = lambda v: math.ceil(round(v * 10, 6)) / 10.0
                    down = lambda v: math.floor(round(v * 10, 6)) / 10.0
                    hint.update(area=[up(a[0]) + 0.0, up(a[1]) + 0.0, down(a[2]) + 0.0, down(a[3]) + 0.0],
                                source="bed_mesh")
            except Exception:
                logging.exception("printune: reading [bed_mesh] for the setup")
                hint["note"] = "Could not read [bed_mesh]: enter the bed area yourself"
            self._bed_hint = hint
        return self._bed_hint

    def _geom_hash(self):
        """The fingerprint of the measured values, '' while one is missing."""
        su = self.setup
        if any(k not in su for k in self.SETUP_KEYS):
            return ""
        return gen.geometry_hash(su["bed_area"], su["toolhead_box"], su["gantry_height"],
                                 su["nozzle_protrusion"])

    def _accepted(self):
        h = self._geom_hash()
        return bool(h) and self.setup.get("accepted") == h

    def _setup_missing(self):
        miss = [k for k in self.SETUP_KEYS if k not in self.setup]
        if not self._accepted():
            miss.append("risk")
        return miss

    def _setup_status(self):
        st = {k: (list(self.setup[k]) if isinstance(self.setup.get(k), tuple) else self.setup.get(k))
              for k in self.SETUP_KEYS}
        st["accepted"] = self._accepted()
        st["missing"] = self._setup_missing()
        st["ready"] = not st["missing"]
        st["hash"] = self._geom_hash()
        st["summary"] = self._setup_summary()
        st["missing_text"] = ", ".join(self.SETUP_NAMES[m] for m in st["missing"])
        st["accept_note"] = self._accept_note if not st["accepted"] else ""
        st["low_margin"] = gen.LOW_MARGIN
        try:
            cf = self.printer.lookup_object("configfile")
            st["unsaved"] = (self.setup != self._setup_saved
                             and bool(getattr(cf, "save_config_pending", True)))
            st["axes"] = [round(v, 3) for v in self._axes()]
        except Exception:
            st["unsaved"], st["axes"] = False, None
        try:
            hint = self._bed_suggestion()
        except Exception:
            hint = {"area": None, "source": "", "note": ""}
        st["bed_hint"], st["bed_hint_note"] = hint["area"], hint["note"]
        st["orca_area"] = self.orca.get("printable_area", "")
        return st

    def _setup_summary(self):
        su = self.setup
        if any(k not in su for k in self.SETUP_KEYS):
            return ""
        return ("bed area %s, toolhead box %s, gantry height %g mm, nozzle protrusion %g mm"
                % (gen.fmt_box(su["bed_area"]), gen.fmt_box(su["toolhead_box"]), su["gantry_height"],
                   su["nozzle_protrusion"]))

    def _require_setup(self, gcmd):
        """Every command that generates or starts a print calls this first (server side, so no
        page or macro can get round it). _PRINTUNE_BEGIN checks it again in every session file."""
        miss = self._setup_missing()
        if miss:
            note = ("; %s" % self._accept_note) if self._accept_note and "risk" in miss else ""
            raise gcmd.error(
                "Printune: the machine setup is not done (missing: %s%s). Printune makes no assumptions "
                "about your printer: open Machine setup on the Printune page or in the PRINTUNE menu, "
                "or run _PRINTUNE_SETUP" % (", ".join(self.SETUP_NAMES[m] for m in miss), note))
        # the axis limits can change after the setup (a new printer.cfg): never print past them
        err = self._past_axes(self.setup["bed_area"])
        if err:
            raise gcmd.error("Printune: %s. Enter a bed area inside them in the machine setup" % (err,))

    def _pending_elsewhere(self):
        """Sections other than [printune] with changes waiting for SAVE_CONFIG (or written by a
        SAVE_CONFIG without a restart and not live yet), as "[section] option" texts."""
        try:
            cf = self.printer.lookup_object("configfile")
            items = cf.get_status(self.reactor.monotonic()).get("save_config_pending_items") or {}
        except Exception:
            logging.exception("printune: reading the pending SAVE_CONFIG items")
            return ["(could not read what is pending)"]
        out = []
        for sec, opts in sorted(items.items()):
            if sec == "printune":
                continue
            if opts is None:
                out.append("[%s] removed" % (sec,))
            else:
                out.append("[%s] %s" % (sec, ", ".join(sorted(opts))))
        return out

    cmd_PRINTUNE_SETUP_help = ("Printune: machine setup (BED=x0,y0,x1,y1|mesh TOOLHEAD=-X,+X,-Y,+Y "
                               "GANTRY=mm PROTRUSION=mm ACCEPT=1 SAVE=1); no parameters shows it")

    def cmd_PRINTUNE_SETUP(self, gcmd):
        ui = gcmd.get_int("UI", 0)
        try:
            self._setup_cmd(gcmd)
        except Exception:
            if ui:
                self.gcode.respond_raw("// action:prompt_end")
            raise
        if ui:
            self.gcode.run_script_from_command("_PRINTUNE_SETUP_UI")

    def _setup_cmd(self, gcmd):
        bed = gcmd.get("BED", None)
        box = gcmd.get("TOOLHEAD", None)
        gantry = gcmd.get("GANTRY", None)
        prot = gcmd.get("PROTRUSION", None)
        accept = gcmd.get_int("ACCEPT", None, minval=0, maxval=1)
        save = gcmd.get_int("SAVE", 0, minval=0, maxval=1)
        if bed is None and box is None and gantry is None and prot is None and accept is None and not save:
            self._setup_report(gcmd)
            if not gcmd.get_int("UI", 0):
                self.gcode.run_script_from_command("_PRINTUNE_SETUP_UI")
            return
        ps = self.printer.lookup_object("print_stats").get_status(self.reactor.monotonic())
        if ps["state"] in ("printing", "paused") or self.soak is not None:
            raise gcmd.error("Printune: finish or cancel the print before changing the machine setup")
        if self.state == "generating" or self.proc is not None:
            raise gcmd.error("Printune: a session is being prepared for the current machine setup: wait "
                             "until it starts and finish or cancel it, or stop it with _PRINTUNE_ABORT, "
                             "before changing the setup")
        new = {}
        try:
            if bed is not None:
                if bed.strip().lower() == "mesh":
                    hint = self._bed_suggestion()
                    if not hint["area"]:
                        raise ValueError(hint["note"] or "no bed mesh area to use")
                    new["bed_area"] = tuple(hint["area"])
                else:
                    new["bed_area"] = gen.parse_bed_area(bed)
                err = self._past_axes(new["bed_area"])
                if err:
                    raise ValueError(err)
            if box is not None:
                new["toolhead_box"] = gen.parse_toolhead_box(box)
            if gantry is not None:
                new["gantry_height"] = gen.parse_gantry_height(gantry)
            if prot is not None:
                new["nozzle_protrusion"] = gen.parse_nozzle_protrusion(prot)
        except ValueError as e:
            raise gcmd.error("Printune: %s" % (e,))
        changed = [k for k, v in new.items() if self.setup.get(k) != v]
        configfile = self.printer.lookup_object("configfile")
        for k, v in new.items():
            self.setup[k] = v
            configfile.set("printune", k, gen.fmt_box(v) if isinstance(v, tuple) else "%g" % v)
        if changed and self.setup.get("accepted"):
            # the acceptance was for the old values
            self.setup["accepted"] = ""
            configfile.set("printune", "setup_accepted", "no")
            if not accept:
                gcmd.respond_info("Printune: the values changed, so the risk has to be accepted again")
        if accept is not None:
            if accept and any(k not in self.setup for k in self.SETUP_KEYS):
                raise gcmd.error("Printune: enter the bed area, the toolhead box, the gantry height and the "
                                 "nozzle protrusion before accepting")
            self.setup["accepted"] = self._geom_hash() if accept else ""
            configfile.set("printune", "setup_accepted", self.setup["accepted"] or "no")
            if accept:
                self._accept_note = ""
        self._setup_report(gcmd)
        if save and self.setup != self._setup_saved:
            others = self._pending_elsewhere()
            if others:
                # SAVE_CONFIG RESTART=0 would write those too while the printer keeps running with the
                # old values (a new probe z_offset is not live until a restart) and clear the "save"
                # notice in Mainsail and Fluidd: leave the restart to the user
                raise gcmd.error(
                    "Printune: the machine setup is set but NOT saved to printer.cfg yet. SAVE_CONFIG also "
                    "has other changes waiting: %s. Saving without a restart would write them too while "
                    "the printer keeps running with the old values (a new Z offset only takes effect "
                    "after a restart), and would clear the save notice in Mainsail and Fluidd. Run "
                    "SAVE_CONFIG: it saves them and the machine setup together and restarts."
                    % ("; ".join(others),))
            # nothing else is waiting: Kalico can save without a restart
            try:
                self.gcode.run_script_from_command("SAVE_CONFIG RESTART=0")
                self._setup_saved = dict(self.setup)
            except gcmd.error as e:
                raise gcmd.error(
                    "Printune: could not save to printer.cfg: %s\nIf a machine setup value is also set "
                    "in an included file (printune_settings.cfg), remove it there; the values above "
                    "stay active until the next restart." % (e,))

    def _setup_report(self, gcmd):
        su = self.setup
        lines = ["Printune machine setup (your own measurements):",
                 "  bed area: %s" % (gen.fmt_box(su["bed_area"]) if "bed_area" in su else "not set"),
                 "  toolhead box -X,+X,-Y,+Y: %s" % ((gen.fmt_box(su["toolhead_box"]) + " mm")
                                                       if "toolhead_box" in su else "not set"),
                 "  gantry height: %s" % (("%g mm" % su["gantry_height"]) if "gantry_height" in su
                                          else "not set"),
                 "  nozzle protrusion: %s" % (("%g mm" % su["nozzle_protrusion"])
                                              if "nozzle_protrusion" in su else "not set"),
                 "  risk accepted: %s" % ("yes" if self._accepted() else "no")]
        miss = self._setup_missing()
        if miss == ["risk"]:
            if self._accept_note:
                lines.append("Note: " + self._accept_note + ".")
            lines.append("Check that these are your own measurements, then accept the risk: "
                         "_PRINTUNE_SETUP ACCEPT=1 SAVE=1 (Printune and its authors take zero responsibility "
                         "for the generated G-code, collisions or damage; no warranty, GPL-3.0)")
        elif miss:
            hint = self._bed_suggestion()
            lines.append("Set it with, for example (your numbers, not these):")
            lines.append("  _PRINTUNE_SETUP BED=%s TOOLHEAD=-30,30,-40,25 GANTRY=42 PROTRUSION=3" % (
                "mesh" if hint["area"] else "x0,y0,x1,y1"))
            lines.append("  _PRINTUNE_SETUP ACCEPT=1 SAVE=1")
            if hint["area"]:
                lines.append("  (BED=mesh uses the bed mesh area cut to the axis limits: %s)"
                             % gen.fmt_box(hint["area"]))
        else:
            lines.append("Done. Printune and its authors take zero responsibility for the generated G-code, "
                         "collisions or damage.")
        gcmd.respond_info("\n".join(lines))

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
        self._require_setup(gcmd)
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
        # PROC=wizard (default) or advanced picks the tests; TESTS= picks them one by one
        proc = gcmd.get("PROC", "wizard").lower()
        if proc not in PROCS:
            self._fail(gcmd, "PROC must be one of %s" % (", ".join(PROCS),))
        tests = [t.strip().lower() for t in gcmd.get("TESTS", ",".join(PROCS[proc])).split(",")
                 if t.strip()]
        bad = [t for t in tests if t not in TESTS]
        if bad or not tests:
            self._fail(gcmd, "TESTS is a list of %s" % (",".join(TESTS),))
        tests = [t for t in TESTS if t in tests]           # print order
        proc = "advanced" if all(t in PROCS["advanced"] for t in tests) else "wizard"
        ex = self.printer.lookup_object("toolhead").get_extruder().get_name()
        settings = self.printer.lookup_object("configfile").get_status(now)["settings"]
        exc = settings.get(ex, {})
        heater = self.printer.lookup_object(ex).get_heater()
        if tmax > heater.max_temp - 5:
            self._fail(gcmd, "%d C is too close to the hotend's max_temp (%.0f)"
                       % (tmax, heater.max_temp))
        cfg, opts, mid = self._settings(ftype, tmin, tmax, exc, settings)
        opts["proc"] = proc
        opts["skip"] = ",".join(SKIP_KEY[t] for t in TESTS if t not in tests)
        reused = []
        flow_ratio = "1"
        if proc == "advanced":
            # advanced tuning prints with what the wizard picked for this preset: temperature,
            # pressure advance, flow ratio and retraction (from its last export)
            prev = self._exported_preset(name)
            ptype = pval(prev, "filament_type").upper() if prev else ""
            if prev and ptype != ftype:
                new = "%s %s" % (name, ftype)
                gcmd.respond_info("Printune: the preset \"%s\" is %s, not %s: starting a new preset \"%s\""
                                  % (name, ptype or "another type", ftype, new))
                name, prev = new[:40], None
            if prev:
                t = fnum_or(pval(prev, "nozzle_temperature"))
                if t and tmin - 30 <= t <= tmax + 30 and t <= heater.max_temp - 5:
                    mid = int(t)
                    cfg["nozzle_temperature"] = cfg["nozzle_temperature_initial_layer"] = str(mid)
                    reused.append("%d C" % mid)
                pa = fnum_or(pval(prev, "pressure_advance"))
                if pval(prev, "enable_pressure_advance") in ("1", "true") and pa is not None:
                    cfg["enable_pressure_advance"], cfg["pressure_advance"] = "1", fnum(pa, 4)
                    reused.append("PA %s" % fnum(pa, 4))
                fr = fnum_or(pval(prev, "filament_flow_ratio"))
                if fr:
                    cfg["filament_flow_ratio"] = flow_ratio = fnum(fr, 4)
                    reused.append("flow ratio %s" % flow_ratio)
                rl = fnum_or(pval(prev, "filament_retraction_length"))
                rs = fnum_or(pval(prev, "filament_retraction_speed"))
                if rl is not None and rs:
                    ds = fnum_or(pval(prev, "filament_deretraction_speed")) or rs
                    cfg["retraction_length"] = fnum(rl, 3)
                    cfg["retraction_speed"], cfg["deretraction_speed"] = fnum(rs, 1), fnum(ds, 1)
                    opts["set_ret"] = "1"
                    reused.append("retraction %s mm" % fnum(rl, 2))
                # the bridge test starts from the fan the cooling test picked
                fan = fnum_or(pval(prev, "fan_max_speed"))
                if fan is not None and 0 <= fan <= 100:
                    opts["bridge_base"] = "%d" % round(fan)
                    reused.append("fan %d %%" % round(fan))
        start = []
        if soak:
            start.append("_PRINTUNE_SOAK BED=%d NOZZLE=%d" % (bed, mid))
        start.append(self.start_gcode.format(bed=bed, nozzle=mid))
        # in case the start macro takes other parameter names
        start += ["M190 S%d" % (bed,), "M109 S%d" % (mid,)]
        vsd = self.printer.lookup_object("virtual_sdcard")
        gdir = os.path.join(os.path.realpath(vsd.sdcard_dirname), "printune")
        os.makedirs(gdir, exist_ok=True)
        # its own file: an advanced session must not replace the wizard's
        fname = re.sub(r"[^A-Za-z0-9._()+-]", "_", name) + ("_advanced" if proc == "advanced" else "") + ".gcode"
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
        block += ["; print_flow_ratio = 1", "; filament_flow_ratio = " + flow_ratio,
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
        self.session = {"name": name, "type": ftype, "tests": tests, "proc": proc, "soak": bool(soak),
                        "file": "printune/" + fname, "tmin": tmin, "tmax": tmax, "bed": bed,
                        "geom": self._geom_hash()}
        self.pending = (path, block)
        self.state, self.message = "generating", "Preparing the test prints"
        if proc == "advanced":
            gcmd.respond_info("Printune: preparing advanced tuning for \"%s\" (%s, nozzle %d C, bed %d C)%s..."
                              % (name, ftype, mid, bed,
                                 ("; from the preset: " + ", ".join(reused)) if reused else
                                 "; no earlier export of this preset, so the printer's PA, flow and retraction"))
        else:
            gcmd.respond_info("Printune: preparing \"%s\" (%s, %d-%d C, bed %d C)..."
                              % (name, ftype, tmin, tmax, bed))
        if self.gen_timer is None:
            self.gen_timer = self.reactor.register_timer(self._gen_poll)
        self.reactor.update_timer(self.gen_timer, now + .5)

    def _settings(self, ftype, tmin, tmax, exc, settings):
        """Orca settings and Printune options for a wizard session, from the printer's config."""
        th = self.printer.lookup_object("toolhead")
        st = th.get_status(self.reactor.monotonic())
        amax = st["axis_maximum"]
        # the bed area from the machine setup (the generator keeps inside orca_settings'
        # printable_area too, when that is set)
        x0, y0, x1, y1 = self.setup["bed_area"]
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
            "filament_type": ftype,
            "use_firmware_retraction": "1" if "firmware_retraction" in settings else "0",
            # the max flow tower clamps its top bands to these
            "machine_max_speed_x": "%g" % (st["max_velocity"],),
            "machine_max_speed_y": "%g" % (st["max_velocity"],),
            "machine_max_acceleration_x": "%g" % (st["max_accel"],),
            "machine_max_acceleration_y": "%g" % (st["max_accel"],),
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
                "pa": self.pa_range, "ret": self.ret_range, "mvs": self.mvs_range or fil.get("mvs", gen.OPTIONS["mvs"])}
        # Kalico's corner rule limits the max flow tower's speed; the cooling cone's layer times
        # are planned the way Kalico plans them
        opts["scv"] = "%g" % (st.get("square_corner_velocity", 5.),)
        opts["mcr"] = "%g" % (st.get("minimum_cruise_ratio", .5),)
        pr = settings.get("printer", {})
        opts["zv"] = "%g" % (float(pr.get("max_z_velocity", st["max_velocity"])),)
        opts["za"] = "%g" % (float(pr.get("max_z_accel", st["max_accel"])),)
        # the user's measured geometry; the generator refuses without it
        opts["bed_area"] = gen.fmt_box(self.setup["bed_area"])
        opts["toolhead_box"] = gen.fmt_box(self.setup["toolhead_box"])
        opts["gantry_height"] = "%g" % (self.setup["gantry_height"],)
        opts["nozzle_protrusion"] = "%g" % (self.setup["nozzle_protrusion"],)
        opts["accept_risk"] = "1" if self._accepted() else "0"
        return cfg, opts, mid

    def _exported_preset(self, name):
        """The last export of this preset (a dict), else None."""
        fname = re.sub(r"[^A-Za-z0-9 ._()@+-]", "_", name).strip() + ".json"
        try:
            with open(os.path.join(self.out_dir, fname)) as f:
                d = json.load(f)
            return d if isinstance(d, dict) and d.get("type") == "filament" else None
        except (OSError, ValueError):
            return None

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
        # the setup can't change while generating (_PRINTUNE_SETUP refuses), but check anyway:
        # never start a session laid out for other values
        if self._setup_missing() or self._geom_hash() != self.session.get("geom"):
            self._error("Printune: the machine setup changed or is no longer accepted while the session "
                        "was being prepared, so it was not started. Check the machine setup and start "
                        "the session again")
            self.gcode.respond_raw("!! " + self.message)
            return self.reactor.NEVER
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
        self.last_proc_name = self.last_proc_rel = ""
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
        changed, keys = [], set()

        def put(key, value, label):
            old = preset.get(key, [""])[0]
            preset[key] = [value]
            keys.add(key)
            changed.append("%s %s -> %s" % (label, old or "-", value))

        if "em" in tested:
            pfr = float(first(cfg, "print_flow_ratio", "1") or 1.0) or 1.0
            put("filament_flow_ratio", fnum(float(S["em"]) / pfr, 4), "flow ratio")
        if "cool" in tested and int(S.get("cool_fan", -1)) >= 0:
            # one constant fan: the same speed at both thresholds, never off, also for overhangs
            f = str(int(S["cool_fan"]))
            put("fan_min_speed", f, "fan min")
            put("fan_max_speed", f, "fan max")
            put("reduce_fan_stop_start_freq", "1", "keep fan always on")
            put("overhang_fan_speed", f, "overhang fan")
        if "cool" in tested and float(S.get("cool_lt", 0)) > 0:
            put("slow_down_layer_time", fnum(S["cool_lt"], 1), "minimum layer time")
            put("slow_down_for_layer_cooling", "1", "slow down for cooling")
        if "bridge" in tested and int(S.get("br_fan", -1)) >= 0:
            # Orca's "Overhangs and external bridges fan speed" (used when above the normal fan)
            put("enable_overhang_bridge_fan", "1", "overhang and bridge fan on")
            put("overhang_fan_speed", str(int(S["br_fan"])), "bridge fan")
            # Orca's "Overhang cooling activation threshold": at 0 % every outer wall runs at the
            # bridge fan (GCode::_extrude), not at the cooling test's constant fan. 95 % (Orca's
            # default) keeps it to bridges and walls with almost nothing below them.
            if preset.get("overhang_fan_threshold", [""])[0] in ("", "0%"):
                put("overhang_fan_threshold", "95%", "overhang fan threshold")
        if "mvs" in tested and float(S.get("mvs", 0)) > 0:
            put("filament_max_volumetric_speed", fnum(S["mvs"], 1), "max volumetric speed")
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
                keys.add("filament_start_gcode")
                changed.append("filament start G-code: SET_RETRACTION line")
        # the bridge test's speed and flow: process settings, a process preset of their own
        proc = {}
        if "bridge" in tested and float(S.get("br_speed", 0)) > 0:
            proc["bridge_speed"] = fnum(S["br_speed"], 1)
        if "bridge" in tested and float(S.get("br_flow", 0)) > 0:
            proc["bridge_flow"] = fnum(S["br_flow"], 3)
        if not changed and not proc:
            raise gcmd.error("_PRINTUNE_EXPORT: nothing was picked in this session")
        m = re.search(r"OrcaSlicer (\d+)\.(\d+)\.(\d+)", head)
        version = "%s.%s.%s.0" % m.groups() if m else "2.0.0.0"
        if changed:
            self._export_filament(gcmd, cfg, path, preset, name, fid, keys, changed, version)
        if proc:
            self._export_process(gcmd, cfg, groups, name, proc, version)

    def _export_process(self, gcmd, cfg, groups, fname_base, values, version):
        """An Orca process preset with the bridge speed and flow. Sliced in Orca: a copy of the
        process preset the session was sliced with (every process setting in the file), on its
        system parent, with these values changed. Made by the wizard (no process preset was
        sliced): a preset holding only these values, on Orca's defaults."""
        pid = first(cfg, "print_settings_id")
        parent = groups[0] if len(groups) == 3 else ""
        wizard = first(cfg, "printune_named") == "1" or not pid
        if wizard:
            name, inherits = fname_base + " bridges", ""
        elif pid.endswith(SUFFIX):
            name, inherits = pid, parent
        elif parent:
            name, inherits = pid + SUFFIX, parent
        elif " @" in pid:
            base, vendor = pid.rsplit(" @", 1)
            name, inherits = base + ("" if vendor == "System" else " " + vendor) + SUFFIX, pid
        else:
            name, inherits = pid + SUFFIX, ""
        # bridge_speed is a per-extruder-variant list, bridge_flow a single value
        nvar = max(1, len(parse_value("print_extruder_variant", cfg["print_extruder_variant"]))
                   if "print_extruder_variant" in cfg else 1)
        out = os.path.join(self.out_dir, re.sub(r"[^A-Za-z0-9 ._()@+-]", "_", name).strip() + ".json")
        preset = {"type": "process", "name": name, "inherits": inherits, "from": "User",
                  "instantiation": "true"}
        if not wizard:
            for k in PROCESS_KEYS:
                if k in cfg:
                    preset[k] = process_value(k, cfg[k])
        try:
            with open(out) as f:
                old = json.load(f)
            if isinstance(old, dict) and old.get("type") == "process" and old.get("name") == name:
                # an earlier bridge test of this preset: keep the value this session did not test
                # (and, for the wizard's preset, whatever else it had)
                if wizard:
                    preset = old
                else:
                    for k in ("bridge_speed", "bridge_flow"):
                        if k not in values and k in old:
                            preset[k] = old[k]
        except (OSError, ValueError):
            pass
        if "bridge_speed" in values:
            preset["bridge_speed"] = [values["bridge_speed"]] * nvar
        if "bridge_flow" in values:
            preset["bridge_flow"] = values["bridge_flow"]
        # always this session's: an old file may have another parent or Orca version
        preset["inherits"] = inherits
        preset["print_settings_id"] = name
        preset["version"] = version
        os.makedirs(self.out_dir, exist_ok=True)
        with open(out + ".tmp", "w") as f:
            json.dump(preset, f, indent=4)
        os.replace(out + ".tmp", out)
        rel = os.path.relpath(out, os.path.dirname(self.cfg_dir))
        self.last_proc_name = name
        self.last_proc_rel = out if rel.startswith("..") else rel
        logging.info("_PRINTUNE_EXPORT wrote %s (%s)", out, values)
        picked = "; ".join("%s %s" % ({"bridge_speed": "bridge speed", "bridge_flow": "bridge flow"}[k],
                                      v + (" mm/s" if k == "bridge_speed" else ""))
                           for k, v in sorted(values.items()))
        if wizard:
            what = ("It holds only these values, on top of Orca's defaults. To keep your own process "
                    "settings, type them into your process preset instead.")
        else:
            what = ("It is a copy of the process preset you sliced with (\"%s\")%s, with only these "
                    "values changed." % (pid, (", based on \"%s\"" % inherits) if inherits else ""))
        gcmd.respond_info(
            "Printune: Orca process preset \"%s\" written to %s\n%s\n"
            "Import it the same way (File > Import > Import Configs); it shows up under the process "
            "presets. %s" % (name, self.last_proc_rel, picked, what))

    def _export_filament(self, gcmd, cfg, path, preset, name, fid, keys, changed, version):
        fname = re.sub(r"[^A-Za-z0-9 ._()@+-]", "_", name).strip() + ".json"
        out = os.path.join(self.out_dir, fname)
        # An earlier export of this preset (the Tuning Wizard before Advanced tuning, or the other
        # way round): keep its results and add this session's on top
        merged = False
        try:
            with open(out) as f:
                old = json.load(f)
            same_type = pval(old, "filament_type").upper() == first(cfg, "filament_type").upper()
            if not same_type:
                gcmd.respond_info("Printune: %s is a %s preset, this session is %s: writing a new preset"
                                  % (fname, pval(old, "filament_type") or "different",
                                     first(cfg, "filament_type")))
            if (isinstance(old, dict) and old.get("type") == "filament" and old.get("name") == name
                    and same_type and "Printune" in str(old.get("filament_notes", ""))):
                for k in keys:
                    old[k] = preset[k]
                # this session's parent (the version is set below): the old file may be older
                old["inherits"] = preset["inherits"]
                preset, merged = old, True
        except (OSError, ValueError):
            pass
        preset["filament_settings_id"] = [name]
        preset["version"] = version
        # one note line per session (a session exports again at each savepoint: replace its line)
        sid = "#%d" % int(os.path.getmtime(path))
        notes = [l for l in str(preset.get("filament_notes", [""])[0]).split("\n")
                 if not l.rstrip().endswith(sid)]
        while notes and not notes[-1].strip():
            notes.pop()
        notes.append("Printune %s (from %s): %s %s" % (time.strftime("%Y-%m-%d"), fid, "; ".join(changed), sid))
        preset["filament_notes"] = ["\n".join(notes)]
        os.makedirs(self.out_dir, exist_ok=True)
        with open(out + ".tmp", "w") as f:
            json.dump(preset, f, indent=4)
        os.replace(out + ".tmp", out)
        rel = os.path.relpath(out, os.path.dirname(self.cfg_dir))
        self.last_file, self.last_name = out, name
        self.last_rel = out if rel.startswith("..") else rel
        logging.info("_PRINTUNE_EXPORT wrote %s (%s)", out, "; ".join(changed))
        gcmd.respond_info(
            "Printune: Orca filament preset \"%s\" written to %s\n%s\n%s"
            "Download it (Mainsail: Machine tab) and import it in Orca: "
            "File > Import > Import Configs.%s"
            % (name, self.last_rel, "\n".join(changed),
               "Added to the results of the earlier session for this preset.\n" if merged else "",
               " It replaces a preset of the same name." if name == fid else ""))


def load_config(config):
    return Printune(config)
