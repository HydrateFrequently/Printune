#!/bin/bash
# First start: fill ~/printer_data/config with the default virtual-printer
# config (files already there are kept). Every start: link the macros under
# test from the mounted host folder, match the host user's uid/gid, then run
# the virtual MCU, Kalico, Moonraker and nginx under supervisord.
set -e
export SPEED=${SPEED:-20}
PUID=${PUID:-1000}
PGID=${PGID:-1000}
if [ "$(id -u pi)" != "$PUID" ] || [ "$(id -g pi)" != "$PGID" ]; then
  groupmod -o -g "$PGID" pi
  usermod -o -u "$PUID" -g "$PGID" pi
  chown -R pi:pi /home/pi --one-file-system 2>/dev/null || true
fi
pd=/home/pi/printer_data
cfg=$pd/config
mkdir -p $pd/gcodes $pd/logs $pd/comms $pd/database $cfg
for f in /home/pi/default_config/*.cfg /home/pi/default_config/*.conf; do
  [ -e "$cfg/$(basename "$f")" ] || cp "$f" "$cfg/"
done
# The macros under test live in the host folder mounted at /home/pi/host
# (a folder mount, so editors that save by rename still update them).
for f in printune.cfg; do
  if [ -f /home/pi/host/$f ]; then
    ln -sfn /home/pi/host/$f $cfg/$f
  elif [ ! -f $cfg/$f ] || [ -L $cfg/$f ]; then
    rm -f $cfg/$f
    echo "# $f not found in the mounted folder" > $cfg/$f
  fi
done
# Printune's Kalico plugin and its Moonraker page, when the mounted folder has them
plug=$cfg/printune_plugin.cfg
if [ -f /home/pi/host/printune.py ]; then
  ln -sfn /home/pi/host/printune.py /home/pi/kalico/klippy/plugins/printune.py
  printf '[printune]\nenclosed: True\nchamber_sensor: temperature_sensor chamber\nsoak_min: 5\nsoak_max: 30\n' > $plug
else
  rm -f /home/pi/kalico/klippy/plugins/printune.py
  echo "# printune.py not found in the mounted folder" > $plug
fi
comp=/home/pi/moonraker/moonraker/components/printune_web.py
if [ -f /home/pi/host/moonraker/printune_web.py ]; then
  ln -sfn /home/pi/host/moonraker/printune_web.py $comp
  grep -q '^\[printune_web\]' $cfg/moonraker.conf || printf '\n[printune_web]\n' >> $cfg/moonraker.conf
  mkdir -p $cfg/.theme
  [ -e $cfg/.theme/navi.json ] || cp /home/pi/host/web/navi.json $cfg/.theme/navi.json
else
  rm -f $comp
  sed -i '/^\[printune_web\]/d' $cfg/moonraker.conf
fi
# configs from before the rename (ECAL) include files that no longer exist
sed -i 's/^\[include ellis_calibration.cfg\]/[include printune.cfg]/; s/^\[include ecal_export.cfg\]/[include printune_plugin.cfg]/' $cfg/printer.cfg
# add an include, above Kalico's SAVE_CONFIG block if there is one
add_include() {
  grep -q "^\[include $1\]" $cfg/printer.cfg && return
  awk -v inc="[include $1]" '!done && /^#\*# <-+ SAVE_CONFIG -+>/ {print inc; print ""; done=1} {print} END {if (!done) print inc}' \
    $cfg/printer.cfg > $cfg/.printer.cfg.tmp && mv $cfg/.printer.cfg.tmp $cfg/printer.cfg
}
add_include printune_plugin.cfg
# Simulated accelerometer, input shaper and Shake&Tune: ACCEL=0 / SHAKETUNE=0 make
# a printer without them, to see the steps greyed out. sim_accel.py is linked from
# the host folder when there, so it can be edited without a rebuild.
acc=$cfg/testenv_accel.cfg
fa=/home/pi/host/test_env/printer_config/sim_accel.py
[ -f $fa ] || fa=/home/pi/default_config/sim_accel.py
st=/home/pi/kalico/klippy/extras/shaketune
# an earlier, PC-only version of this used other file names
sed -i '/^\[include sim_accel\.cfg\]/d; /^\[include shaketune\.cfg\]/d' $cfg/printer.cfg
rm -f $cfg/sim_accel.cfg $cfg/shaketune.cfg /home/pi/kalico/klippy/extras/fake_accel.py
if [ "${ACCEL:-1}" = 1 ] && [ -f $fa ]; then
  ln -sfn $fa /home/pi/kalico/klippy/extras/sim_accel.py
  printf '[sim_accel]\nx_modes: 52.0:0.08, 87:0.12:0.3\ny_modes: 41.0:0.10\nbelt_a: 96\nbelt_b: 91\n\n[resonance_tester]\naccel_chip: sim_accel\nprobe_points: 187.5,194,20\n\n[input_shaper]\n' > $acc
  if [ "${SHAKETUNE:-1}" = 1 ]; then
    if [ -e $st ]; then
      printf '\n[shaketune]\n' >> $acc
    else
      echo "SHAKETUNE=1 but this image has no Shake&Tune: rebuild it (docker compose up -d --build)"
    fi
  fi
else
  echo "# no accelerometer (ACCEL=0)" > $acc
fi
add_include testenv_accel.cfg
awk '!(/^\[include / && seen[$0]++)' $cfg/printer.cfg > $cfg/.printer.cfg.tmp && mv $cfg/.printer.cfg.tmp $cfg/printer.cfg
rm -f $cfg/ellis_calibration.cfg $cfg/ecal_export.cfg
# Own only the folders that are not bind-mounted from the host
chown -R pi:pi $cfg $pd/comms $pd/database
chown pi:pi $pd $pd/logs $pd/gcodes 2>/dev/null || true
mkdir -p /tmp/supervisor
exec /usr/bin/supervisord -c /etc/supervisor/supervisord.conf
