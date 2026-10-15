# Printune

Tune your filament and printer by pressing buttons, not by reading guides. Printune is a plugin for Kalico printers (Moonraker plus Mainsail or Fluidd). It prints the tuning tests on the printer, you pick the best result on your phone, and the values end up in an **OrcaSlicer filament preset**.

> **Very early development. Not yet tested on a real printer.**
> Printune was written entirely by AI ("vibecoded"), with a person steering and checking the results. It has not run on a physical printer yet. Try it only if you are comfortable watching the printer the whole time, with a hand near the power switch.

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

## How it has been tested so far

To avoid breaking your printer, every change has been tried on:

- **A virtual Kalico printer:** real Kalico, Moonraker and Mainsail on a simulated MCU that drives nothing, with simulated temperatures and accelerometer. Scripts drive whole tuning sessions from start to end, including heat soak, pauses and the phone page.
- **A fleet of 30 virtual printer setups:** 8 bed sizes from 120×120 to 375×388 mm, running the wizard with PLA, ABS and TPU and Advanced tuning with PETG and ASA. All 30 passed with no errors.
- **A G-code checker** (`tools/check_gcode.py`) that replays every generated session and looks for moves off the bed, the nozzle or toolhead hitting finished prints, the X gantry passing over prints taller than its measured height, and extrusion onto other objects. It found none across the fleet.
- **A safety gate:** nothing prints until you have entered your own measured machine geometry and accepted the risk.
- **The installer** on a clean virtual printer.
- **Recordings from a real printer's accelerometers** (belt plucks and taps) for the belt tension module, checked against a high-quality stereo field-recorder microphone as the reference (the averages agreed within about 0.1 Hz).
- **OrcaSlicer 2.4.2** (from the command line) importing the exported presets.
- **Reviews by separate AI agents** looking for mistakes in each finished feature.

None of this replaces a real printer. Bugs that only show up on real hardware are expected.

## More

The full guide covers every test, the settings and the caveats: [docs/GUIDE.md](docs/GUIDE.md).

**Use at your own risk. We accept no responsibility** for damage to your printer, your prints, your property or anything else. Printune generates G-code from the geometry you enter and comes without any warranty (GPL-3.0-or-later, sections 15 and 16).
