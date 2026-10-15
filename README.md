# Printune

Tune your filament and printer by pressing buttons, not by reading guides. Printune is a plugin for Kalico printers (Moonraker plus Mainsail or Fluidd). It prints the tuning tests on the printer, you pick the best result on your phone, and the values end up in an **OrcaSlicer filament preset**.

## What it does

- **Tuning Wizard:** one button runs first layer, pressure advance, flow, retraction and temperature for a filament.
- **Advanced tuning:** cooling, bridging and max volumetric flow.
- **Printer tuning:** belt tension by plucking, read by the toolhead accelerometer. After that, the input shaper (Kalico's own, plus Shake&Tune graphs if installed).
- **Results go into the slicer, not the firmware,** so swapping filament never leaves a stale value behind.
- **Made for a phone** standing next to the printer.

## Install

On the printer, over SSH:

```
cd ~ && git clone https://github.com/HydrateFrequently/Printune.git
~/Printune/install.sh
sudo systemctl restart moonraker klipper
```

Then set `start_gcode` (and `enclosed: True` on an enclosed printer) in `printune_settings.cfg`. Open **Printune** in the Mainsail sidebar, or `http://<printer>/server/printune` on your phone. Do the **Machine setup** first: until it is done, nothing prints. `git pull` in `~/Printune` updates it.

## Needs

Kalico, Moonraker, Mainsail or Fluidd, and `[exclude_object]`, `[pause_resume]`, `[respond]` and `[virtual_sdcard]` in printer.cfg. OrcaSlicer 2.x for the presets. Belt tension and the input shaper need an accelerometer on the toolhead.

## More

The full guide covers every test, the settings and the caveats: [docs/GUIDE.md](docs/GUIDE.md).

**Use at your own risk.** Printune generates G-code from the geometry you enter, and comes without any warranty (GPL-3.0-or-later, sections 15 and 16).
