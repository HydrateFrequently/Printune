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
for f in ellis_calibration.cfg; do
  if [ -f /home/pi/host/$f ]; then
    ln -sfn /home/pi/host/$f $cfg/$f
  elif [ ! -f $cfg/$f ] || [ -L $cfg/$f ]; then
    rm -f $cfg/$f
    echo "# $f not found in the mounted folder" > $cfg/$f
  fi
done
# ECAL's export plugin, when the mounted folder has it
if [ -f /home/pi/host/ecal_export.py ]; then
  ln -sfn /home/pi/host/ecal_export.py /home/pi/kalico/klippy/plugins/ecal_export.py
  echo "[ecal_export]" > $cfg/ecal_export.cfg
else
  rm -f /home/pi/kalico/klippy/plugins/ecal_export.py
  echo "# ecal_export.py not found in the mounted folder" > $cfg/ecal_export.cfg
fi
# Own only the folders that are not bind-mounted from the host
chown -R pi:pi $cfg $pd/comms $pd/database
chown pi:pi $pd $pd/logs $pd/gcodes 2>/dev/null || true
mkdir -p /tmp/supervisor
exec /usr/bin/supervisord -c /etc/supervisor/supervisord.conf
