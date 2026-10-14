# ECAL export: write the session's picks as an OrcaSlicer filament preset.
#
# Install (Kalico): ln -s ~/printer_data/config/ecal_export.py ~/kalico/klippy/plugins/
#   (mainline Klipper: ~/klipper/klippy/extras/) and add [ecal_export] to printer.cfg.
# The ECAL summary calls ECAL_EXPORT at the end of a session; it can also be
# run by hand after a session.
#
# How it works: an Orca G-code ends with a CONFIG_BLOCK holding every setting
# it was sliced with, including the filament preset's name and its parent.
# ECAL_EXPORT reads that block from the session file, takes the filament
# settings, puts the picked values on top (flow ratio, pressure advance,
# retraction, nozzle temperature) and writes one JSON file that Orca imports
# with File > Import > Import Configs.
#
# Copyright (C) 2026  ECAL contributors
# This file may be distributed under the terms of the GNU GPLv3 license.

import json
import logging
import os
import re
import time

# Orca's filament preset keys (Preset.cpp, s_Preset_filament_options, 2.4.x).
# Only keys that appear in the sliced file are copied; Orca drops unknown ones.
FILAMENT_KEYS = """
default_filament_colour required_nozzle_HRC filament_diameter pellet_flow_coefficient
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
reduce_fan_stop_start_freq dont_slow_down_outer_wall slow_down_for_layer_cooling
fan_min_speed fan_max_speed enable_overhang_bridge_fan overhang_fan_speed
overhang_fan_threshold close_fan_the_first_x_layers close_additional_fan_first_x_layers
first_x_layer_fan_speed full_fan_speed_layer initial_layer_fan_speed
additional_fan_full_speed_layer fan_cooling_layer_time slow_down_layer_time
slow_down_min_speed filament_start_gcode filament_end_gcode
filament_change_extrusion_role_gcode activate_air_filtration
activate_air_filtration_during_print activate_air_filtration_on_completion
during_print_exhaust_fan_speed complete_print_exhaust_fan_speed
filament_deretraction_speed filament_retract_after_wipe filament_retract_before_wipe
filament_retract_lift_above filament_retract_lift_below filament_retract_lift_enforce
filament_retract_restart_extra filament_retract_when_changing_layer
filament_retraction_length filament_retraction_minimum_travel filament_retraction_speed
filament_retract_length_toolchange filament_retract_restart_extra_toolchange
filament_wipe filament_z_hop filament_z_hop_types filament_vendor
filament_wipe_distance additional_cooling_fan_speed nozzle_temperature_range_low
nozzle_temperature_range_high filament_extruder_variant enable_pressure_advance
pressure_advance adaptive_pressure_advance adaptive_pressure_advance_model
adaptive_pressure_advance_overhangs adaptive_pressure_advance_bridges
chamber_temperature filament_shrink filament_shrinkage_compensation_z
support_material_interface_fan_speed internal_bridge_fan_speed filament_notes
ironing_fan_speed
""".split()

TAIL_BYTES = 2 * 1024 * 1024


def parse_value(raw):
    """Orca config-block value -> list of strings, one per filament slot."""
    raw = raw.strip()
    if raw.startswith('"'):
        out, i = [], 0
        while i < len(raw):
            if raw[i] != '"':
                i += 1
                continue
            j, buf = i + 1, []
            while j < len(raw) and raw[j] != '"':
                if raw[j] == "\\" and j + 1 < len(raw):
                    buf.append({"n": "\n", "t": "\t", "r": "\r"}.get(raw[j + 1], raw[j + 1]))
                    j += 2
                else:
                    buf.append(raw[j])
                    j += 1
            out.append("".join(buf))
            i = j + 1
        return out
    return raw.split(",") if raw else [""]


def read_config_block(path):
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        f.seek(max(0, size - TAIL_BYTES))
        tail = f.read().decode("utf-8", "replace")
    with open(path, "rb") as f:
        head = f.read(4096).decode("utf-8", "replace")
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
    return parse_value(cfg[key])[0]


def fnum(v, digits):
    s = ("%." + str(digits) + "f") % float(v)
    return s.rstrip("0").rstrip(".") if "." in s else s


class EcalExport:
    def __init__(self, config):
        self.printer = config.get_printer()
        default_dir = os.path.join(
            os.path.dirname(os.path.expanduser(
                self.printer.get_start_args()["config_file"])),
            "ecal_exports",
        )
        self.out_dir = os.path.expanduser(config.get("path", default_dir))
        self.last_file = ""
        self.last_name = ""
        gcode = self.printer.lookup_object("gcode")
        gcode.register_command(
            "ECAL_EXPORT", self.cmd_ECAL_EXPORT, desc=self.cmd_ECAL_EXPORT_help
        )

    def get_status(self, eventtime):
        return {"last_file": self.last_file, "last_name": self.last_name}

    def _session_file(self, gcmd):
        name = gcmd.get("FILE", None)
        vsd = self.printer.lookup_object("virtual_sdcard")
        root = vsd.sdcard_dirname
        if name is None:
            cur = vsd.file_path()
            if cur:
                return cur
            ps = self.printer.lookup_object("print_stats")
            name = ps.get_status(self.printer.get_reactor().monotonic())["filename"]
        if not name:
            raise gcmd.error("ECAL_EXPORT: no session file (give FILE=<name>)")
        path = os.path.normpath(os.path.join(root, name))
        if not path.startswith(os.path.normpath(root) + os.sep):
            raise gcmd.error("ECAL_EXPORT: FILE must be inside the G-code folder")
        return path

    cmd_ECAL_EXPORT_help = "ECAL: write the picked values as an Orca filament preset"

    def cmd_ECAL_EXPORT(self, gcmd):
        # SOFT=1 (used by the ECAL summary): report problems instead of
        # raising, so the results prompt still shows
        try:
            self._export(gcmd)
        except gcmd.error as e:
            if not gcmd.get_int("SOFT", 0):
                raise
            gcmd.respond_info("%s\nNo preset file written; type the values in Orca." % (e,))

    def _export(self, gcmd):
        self.last_file = self.last_name = ""
        path = self._session_file(gcmd)
        if not os.path.exists(path):
            raise gcmd.error("ECAL_EXPORT: %s not found" % (path,))
        cfg, head = read_config_block(path)
        if not cfg or "filament_settings_id" not in cfg:
            raise gcmd.error(
                "ECAL_EXPORT: %s has no Orca settings block (sliced with Orca and ecal.py?)"
                % (os.path.basename(path),))
        S = self.printer.lookup_object("gcode_macro _ECAL").variables
        tested = set(str(S.get("tested", "")).split(","))
        fid = first(cfg, "filament_settings_id")
        groups = parse_value(cfg.get("inherits_group", ""))
        if len(groups) == 1 and ";" in cfg.get("inherits_group", ""):
            groups = cfg["inherits_group"].split(";")
        parent = groups[1].strip().strip('"') if len(groups) > 1 else ""
        if parent:
            # a user preset: same name, so the import replaces it
            name, inherits = fid, parent
        elif " @" in fid:
            # a system preset cannot be overwritten: make a child of it
            name, inherits = fid.split(" @")[0] + " (ECAL)", fid
        else:
            name, inherits = fid, ""   # a custom root preset
        preset = {
            "type": "filament",
            "name": name,
            "inherits": inherits,
            "from": "User",
            "instantiation": "true",
        }
        for k in FILAMENT_KEYS:
            if k in cfg:
                preset[k] = [parse_value(cfg[k])[0]]
        changed = []

        def put(key, value, label):
            old = preset.get(key, [""])[0]
            preset[key] = [value]
            changed.append("%s %s -> %s" % (label, old or "-", value))

        if "em" in tested:
            pfr = float(first(cfg, "print_flow_ratio", "1") or 1.0)
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
                lines = [l for l in lines if l.strip()] + [
                    "SET_RETRACTION RETRACT_LENGTH=%s RETRACT_SPEED=%s UNRETRACT_SPEED=%s"
                    % (ln, sp, sp)]
                preset["filament_start_gcode"] = ["\n".join(lines)]
                changed.append("filament start G-code: SET_RETRACTION line")
        if not changed:
            raise gcmd.error("ECAL_EXPORT: nothing was picked in this session")
        preset["filament_settings_id"] = [name]
        m = re.search(r"OrcaSlicer (\d+)\.(\d+)\.(\d+)", head)
        preset["version"] = "%s.%s.%s.0" % m.groups() if m else "2.0.0.0"
        preset["filament_notes"] = [
            ((preset.get("filament_notes", [""])[0] + "\n").lstrip()
             + "ECAL %s: %s" % (time.strftime("%Y-%m-%d"), "; ".join(changed)))]
        os.makedirs(self.out_dir, exist_ok=True)
        fname = re.sub(r"[^A-Za-z0-9 ._()@+-]", "_", name).strip() + ".json"
        out = os.path.join(self.out_dir, fname)
        with open(out + ".tmp", "w") as f:
            json.dump(preset, f, indent=4)
        os.replace(out + ".tmp", out)
        self.last_file, self.last_name = out, name
        logging.info("ECAL_EXPORT wrote %s (%s)", out, "; ".join(changed))
        gcmd.respond_info(
            "ECAL: Orca filament preset \"%s\" written to %s\n%s\n"
            "Download it (Mainsail: Machine > ecal_exports) and import it in Orca: "
            "File > Import > Import Configs.%s"
            % (name, out, "\n".join(changed),
               " It replaces your preset of the same name." if parent or not inherits else ""))


def load_config(config):
    return EcalExport(config)
