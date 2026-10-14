# ECAL export: write the session's picks as an OrcaSlicer filament preset.
#
# Install (Kalico): ln -s ~/printer_data/config/ecal_export.py ~/kalico/klippy/plugins/
#   (mainline Klipper: ~/klipper/klippy/extras/) and add [ecal_export] to printer.cfg.
# The ECAL summary calls ECAL_EXPORT at the end of a session; it can also be
# run by hand after a session (ECAL_EXPORT FILE=<session file>).
#
# How it works: an Orca G-code ends with a CONFIG_BLOCK holding every setting
# it was sliced with, including the filament preset's name and its parent.
# ECAL_EXPORT reads that block from the session file, takes the filament
# settings, puts the picked values on top (flow ratio, pressure advance,
# retraction, nozzle temperature) and writes one JSON file that Orca imports
# with File > Import > Import Configs. It always writes a new preset,
# "<filament> (ECAL)", so it never replaces one of your own presets.
#
# Copyright (C) 2026  ECAL contributors
# This file may be distributed under the terms of the GNU GPLv3 license.

import json
import logging
import os
import re
import time

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
SUFFIX = " (ECAL)"


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


class EcalExport:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.cfg_dir = os.path.dirname(os.path.expanduser(
            self.printer.get_start_args()["config_file"]))
        self.out_dir = os.path.expanduser(
            config.get("path", os.path.join(self.cfg_dir, "ecal_exports")))
        self.last_file = self.last_name = self.last_rel = ""
        gcode = self.printer.lookup_object("gcode")
        gcode.register_command(
            "ECAL_EXPORT", self.cmd_ECAL_EXPORT, desc=self.cmd_ECAL_EXPORT_help
        )

    def get_status(self, eventtime):
        return {"last_file": self.last_file, "last_name": self.last_name,
                "last_rel": self.last_rel}

    def _session_file(self, gcmd):
        name = gcmd.get("FILE", None)
        vsd = self.printer.lookup_object("virtual_sdcard", None)
        if vsd is None:
            raise gcmd.error("ECAL_EXPORT: needs [virtual_sdcard]")
        root = os.path.realpath(vsd.sdcard_dirname)
        if name is None:
            cur = vsd.file_path()
            if cur:
                return cur
            ps = self.printer.lookup_object("print_stats")
            name = ps.get_status(self.printer.get_reactor().monotonic())["filename"]
        if not name:
            raise gcmd.error("ECAL_EXPORT: no session file (give FILE=<name>)")
        path = os.path.realpath(os.path.join(root, name))
        if not path.startswith(root + os.sep):
            raise gcmd.error("ECAL_EXPORT: FILE must be inside the G-code folder")
        return path

    cmd_ECAL_EXPORT_help = "ECAL: write the picked values as an Orca filament preset"

    def cmd_ECAL_EXPORT(self, gcmd):
        # SOFT=1 (used by the ECAL summary): report problems instead of
        # raising, so the results prompt still shows
        self.last_file = self.last_name = self.last_rel = ""
        try:
            try:
                self._export(gcmd)
            except gcmd.error:
                raise
            except Exception as e:
                # never let a file or parsing problem shut the printer down
                logging.exception("ECAL_EXPORT failed")
                raise gcmd.error("ECAL_EXPORT failed: %s" % (e,))
        except gcmd.error as e:
            if not gcmd.get_int("SOFT", 0):
                raise
            gcmd.respond_info("%s\nNo preset file written; type the values in Orca." % (e,))

    def _export(self, gcmd):
        path = self._session_file(gcmd)
        if not os.path.exists(path):
            raise gcmd.error("ECAL_EXPORT: %s not found" % (path,))
        cfg, head = read_config_block(path)
        if "_ECAL_BEGIN" not in head:
            raise gcmd.error("ECAL_EXPORT: %s is not an ECAL session" % (os.path.basename(path),))
        if not cfg or "filament_settings_id" not in cfg:
            raise gcmd.error(
                "ECAL_EXPORT: %s has no Orca settings block (sliced with Orca and ecal.py?)"
                % (os.path.basename(path),))
        macro = self.printer.lookup_object("gcode_macro _ECAL", None)
        if macro is None:
            raise gcmd.error("ECAL_EXPORT: ellis_calibration.cfg is not included")
        S = macro.variables
        tested = set(str(S.get("tested", "")).split(","))
        fids = parse_value("filament_settings_id", cfg["filament_settings_id"])
        if len(fids) != 1:
            raise gcmd.error("ECAL_EXPORT: the slice has %d filaments; slice ECAL with one"
                             % (len(fids),))
        fid = fids[0]
        groups = unescape_strings(cfg.get("inherits_group", ""))
        # inherits_group = print;filament;printer
        parent = groups[1] if len(groups) == 3 else ""
        if fid.endswith(SUFFIX):
            # tuning an earlier export again: the import replaces it
            name, inherits = fid, parent
        elif parent:
            # your own preset: a copy of it, on the same system parent
            name, inherits = fid + SUFFIX, parent
        elif " @" in fid:
            # a system preset: a child of it
            name, inherits = fid.split(" @")[0] + SUFFIX, fid
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
            raise gcmd.error("ECAL_EXPORT: nothing was picked in this session")
        preset["filament_settings_id"] = [name]
        m = re.search(r"OrcaSlicer (\d+)\.(\d+)\.(\d+)", head)
        preset["version"] = "%s.%s.%s.0" % m.groups() if m else "2.0.0.0"
        preset["filament_notes"] = [
            ((preset.get("filament_notes", [""])[0] + "\n").lstrip()
             + "ECAL %s (from %s): %s" % (time.strftime("%Y-%m-%d"), fid, "; ".join(changed)))]
        os.makedirs(self.out_dir, exist_ok=True)
        fname = re.sub(r"[^A-Za-z0-9 ._()@+-]", "_", name).strip() + ".json"
        out = os.path.join(self.out_dir, fname)
        with open(out + ".tmp", "w") as f:
            json.dump(preset, f, indent=4)
        os.replace(out + ".tmp", out)
        rel = os.path.relpath(out, os.path.dirname(self.cfg_dir))
        self.last_file, self.last_name = out, name
        self.last_rel = out if rel.startswith("..") else rel
        logging.info("ECAL_EXPORT wrote %s (%s)", out, "; ".join(changed))
        gcmd.respond_info(
            "ECAL: Orca filament preset \"%s\" written to %s\n%s\n"
            "Download it (Mainsail: Machine tab) and import it in Orca: "
            "File > Import > Import Configs.%s"
            % (name, self.last_rel, "\n".join(changed),
               " It replaces the preset of the same name." if name == fid else ""))


def load_config(config):
    return EcalExport(config)
