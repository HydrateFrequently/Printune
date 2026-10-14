#!/bin/bash
# Printune installer for a Kalico printer (Mainsail or Fluidd + Moonraker).
#
#   cd ~ && git clone https://github.com/HydrateFrequently/Printune.git
#   ~/Printune/install.sh
#
# It links the files from this folder (so a git pull updates them) and adds
# two lines to printer.cfg and one section to moonraker.conf. Run it again any
# time; it does nothing twice. Options (environment variables):
#   KLIPPER_DIR   default: ~/kalico, else ~/klipper
#   MOONRAKER_DIR default: ~/moonraker
#   CONFIG_DIR    default: ~/printer_data/config
set -e
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
KLIPPER_DIR=${KLIPPER_DIR:-$( [ -d ~/kalico/klippy ] && echo ~/kalico || echo ~/klipper )}
MOONRAKER_DIR=${MOONRAKER_DIR:-~/moonraker}
CONFIG_DIR=${CONFIG_DIR:-~/printer_data/config}

say() { echo "Printune: $*"; }
die() { echo "Printune: $*" >&2; exit 1; }

[ "$(id -u)" != 0 ] || [ -n "$PRINTUNE_ALLOW_ROOT" ] || die "run it as your normal user, not with sudo"

[ -d "$KLIPPER_DIR/klippy" ] || die "no Kalico/Klipper in $KLIPPER_DIR (set KLIPPER_DIR=...)"
[ -d "$MOONRAKER_DIR/moonraker/components" ] || die "no Moonraker in $MOONRAKER_DIR (set MOONRAKER_DIR=...)"
[ -f "$CONFIG_DIR/printer.cfg" ] || die "no printer.cfg in $CONFIG_DIR (set CONFIG_DIR=...)"

# keep a repo's update check clean of our links
exclude() {  # repo, path inside it
  local ex="$1/.git/info/exclude"
  [ -d "$1/.git" ] || return 0
  grep -qxF "$2" "$ex" 2>/dev/null || echo "$2" >> "$ex"
}

# 0. From the ECAL days: old includes and links that would stop Kalico from starting
sed -i 's/^\[include ellis_calibration.cfg\]/[include printune.cfg]/; /^\[include ecal_export.cfg\]/d; /^\[ecal_export\]/d' "$CONFIG_DIR/printer.cfg"
for f in "$KLIPPER_DIR"/klippy/plugins/ecal_export.py "$KLIPPER_DIR"/klippy/extras/ecal_export.py; do
  [ -L "$f" ] && rm -f "$f" && say "removed the old link $f"
done

# 1. Kalico plugin (Kalico: klippy/plugins, mainline Klipper: klippy/extras)
if [ -d "$KLIPPER_DIR/klippy/plugins" ]; then sub=klippy/plugins; else sub=klippy/extras; fi
ln -sfn "$HERE/printune.py" "$KLIPPER_DIR/$sub/printune.py"
exclude "$KLIPPER_DIR" "$sub/printune.py"
say "plugin linked into $KLIPPER_DIR/$sub"

# 2. Moonraker component that serves the Printune page
ln -sfn "$HERE/moonraker/printune_web.py" "$MOONRAKER_DIR/moonraker/components/printune_web.py"
exclude "$MOONRAKER_DIR" "moonraker/components/printune_web.py"
if ! grep -q '^\[printune_web\]' "$CONFIG_DIR/moonraker.conf"; then
  printf '\n[printune_web]\n' >> "$CONFIG_DIR/moonraker.conf"
  say "added [printune_web] to moonraker.conf"
fi

# 3. Macros and settings
ln -sfn "$HERE/printune.cfg" "$CONFIG_DIR/printune.cfg"
if [ ! -e "$CONFIG_DIR/printune_settings.cfg" ]; then
  cat > "$CONFIG_DIR/printune_settings.cfg" <<'EOF'
# Printune settings (yours to edit; install.sh never overwrites this file)
[printune]
# Your start and end G-code for the wizard's test prints. {bed} and {nozzle}
# are filled in; unknown parameters are ignored by most start macros.
#start_gcode: PRINT_START BED={bed} BED_TEMP={bed} HOTEND={nozzle} EXTRUDER={nozzle} EXTRUDER_TEMP={nozzle}
#end_gcode: PRINT_END
# Enclosed printer: offer a heat soak before the tests (ABS, ASA, PC, PA)
#enclosed: False
# Sensor that tells when the soak is done; default the nozzle (heater off, parked high)
#chamber_sensor: temperature_sensor chamber
# Soak time limits in minutes, and the rise per 5 minutes that counts as level
#soak_min: 10
#soak_max: 60
#soak_rate: 0.5
# Test ranges: start-end/step. A bowden printer needs a longer PA and retraction range.
#pa_range: 0-0.08/0.005
#ret_range: 0-1.0/0.1
# Toolhead radius (mm) the layout keeps clear of finished prints; default 65.
# A small toolhead (Voron V0 about 35) fits more tests on one plate.
#toolhead_radius: 65
# Belt tension: starting target (Hz) and how close both belts must get (Hz)
#belt_target: 110
#belt_tolerance: 2
# Other OrcaSlicer settings for the test prints, one "key = value" per line, e.g.
#orca_settings:
#    outer_wall_speed = 100
#    travel_acceleration = 5000
EOF
  say "wrote printune_settings.cfg (edit it to set enclosed: True on an enclosed printer)"
fi
added=""
for inc in printune.cfg printune_settings.cfg; do
  grep -q "^\[include $inc\]" "$CONFIG_DIR/printer.cfg" && continue
  added="$added[include $inc]\n"
done
if [ -n "$added" ]; then
  cp "$CONFIG_DIR/printer.cfg" "$CONFIG_DIR/printer.cfg.before-printune"
  # at the top: everything after the SAVE_CONFIG block at the end must stay last
  { printf "$added"; cat "$CONFIG_DIR/printer.cfg.before-printune"; } > "$CONFIG_DIR/printer.cfg"
  say "added the includes at the top of printer.cfg (backup: printer.cfg.before-printune)"
fi
for sect in exclude_object pause_resume respond virtual_sdcard; do
  grep -rqs "^\[$sect\]" "$CONFIG_DIR"/*.cfg || say "note: printer.cfg needs [$sect] (Mainsail's mainsail.cfg has pause_resume and virtual_sdcard)"
done

# 4. Sidebar link in Mainsail
mkdir -p "$CONFIG_DIR/.theme"
python3 - "$CONFIG_DIR/.theme/navi.json" "$HERE/web/navi.json" <<'EOF'
import json, os, sys
dst, src = sys.argv[1], sys.argv[2]
links = []
if os.path.exists(dst):
    with open(dst) as f:
        links = json.load(f)
links = [l for l in links if l.get("title") != "Printune"]
with open(src) as f:
    links += json.load(f)
with open(dst, "w") as f:
    json.dump(links, f, indent=2)
EOF
say "Mainsail sidebar link added (.theme/navi.json)"

say "Printune comes without any warranty: you use it and the G-code it generates at your own risk."
say "done. Restart Moonraker and Kalico: sudo systemctl restart moonraker klipper"
say "then open Printune in the Mainsail sidebar, or http://<printer>/server/printune on your phone."
