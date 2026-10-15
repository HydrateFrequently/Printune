# Printune

Filament tuning for Kalico printers, made for people who just want to press a button. Printune prints the tuning tests on your printer, asks you to pick the best one after each test (on your phone, in Mainsail or Fluidd), and writes the results into an **OrcaSlicer filament preset** you import with two clicks.

- **One big blue button: Tuning Wizard.** Pick the filament type, give it a name, check the temperature range from the box, press Start. Printune builds the test prints for your printer, heat-soaks if you want, and walks you through every test.
- **Or one test only:** first layer, pressure advance, flow, or retraction and temperature.
- **The printer too, in the right order:** belt tension first (pluck each belt; the toolhead accelerometer hears it), then the filament, then the input shaper. Printune runs Kalico's own shaper calibration and, if installed, Shake&Tune's graphs. Steps whose tools are missing are greyed out and link to where to get them.
- **Works on a phone.** The Printune page is made for a phone next to the printer, and works on a desktop and on small touch screens with a browser.
- **Results go into the slicer, not the firmware.** Pressure advance, flow ratio, retraction and temperature are per filament, so they belong in the filament preset. The printer's own values are back when the session ends, so swapping filament can never leave a stale value behind.

| | |
|---|---|
| Firmware | Kalico (mainline Klipper may work but is not tested), Moonraker, Mainsail or Fluidd |
| Printer config | `[exclude_object]`, `[pause_resume]`, `[respond]`, `[virtual_sdcard]` (Mainsail's and Fluidd's standard configs have most of them) |
| Slicer | OrcaSlicer 2.x for the preset (tested with 2.4.2) |
| Session | all tests: about 2 h 10 min of printing plus heating and your decision time, about 17 m of filament |
| Licence | GPL-3.0-or-later |

> **Use at your own risk.** Printune generates G-code for your printer. We take no responsibility for that G-code or what it does; see [Disclaimer](#disclaimer).

## Install

On the printer (over SSH):

```
cd ~ && git clone https://github.com/HydrateFrequently/Printune.git
~/Printune/install.sh
sudo systemctl restart moonraker klipper
```

`install.sh` links the files from the Printune folder (so `git pull` updates them) and:

- links the Kalico plugin `printune.py` and the Moonraker page server `moonraker/printune_web.py`;
- adds `[include printune.cfg]` and `[include printune_settings.cfg]` at the top of printer.cfg (backup in `printer.cfg.before-printune`) and `[printune_web]` to moonraker.conf;
- writes `printune_settings.cfg` once, with every option commented out;
- adds **Printune** to the Mainsail sidebar.

Then open `printune_settings.cfg` and check two things:

- `start_gcode`: how the test prints call your start macro. The default passes the temperatures under the common names (`PRINT_START BED=… BED_TEMP=… HOTEND=… EXTRUDER=… EXTRUDER_TEMP=…`).
- `enclosed: True` on an enclosed printer, so ABS, ASA, PC and PA get a heat soak first.

## Use it

Open **Printune** in the Mainsail sidebar, or `http://<printer>/server/printune` on your phone (bookmark it or add it to the home screen).

1. Press **Tuning Wizard**, or one of the single tests.
2. Pick the filament type, name it (this becomes the preset name in Orca), set the nozzle range from the box and the bed temperature. Clean the plate and press **Start**.
3. With heat soak on, the printer heats the bed with the toolhead parked high and the hotend off, and starts the tests by itself when the temperature levels off. **Skip the soak** starts them right away.
4. After each test the printer pauses and the page shows one big button per printed value. Press the best one. The printer applies it and prints the next test with it.
5. At the end the page shows your results and a **Download Orca preset** button. In Orca: **File › Import › Import Configs**, pick the file. The preset shows up under your filaments with the name you gave it, based on Orca's Generic preset for that type.

No phone or page? The **PRINTUNE** macro button in Mainsail or Fluidd opens the same menu as prompts (belt tension, Tuning Wizard, single tests, input shaper, extruder calibration). It names the preset "<type> Printune"; rename it in Orca.

If you cancel half-way, press **Reset after cancel** in the PRINTUNE menu (or run `_PRINTUNE_RESET`). It puts pressure advance, flow, firmware retraction and the idle timeout back. After a firmware restart there is nothing to put back.

## Tuning the printer

Do these in order: the belts, then each filament (the wizard), then the input shaper. A ringing print or a pressure-advance pick reads better on a printer with the belts right and a tuned filament.

| # | Step | Needs | What happens |
|---|---|---|---|
| 1 | **Belt tension** | a CoreXY printer with an accelerometer on the toolhead (`[resonance_tester]`) | Set the target frequency your printer's build guide gives, and pluck the same stretch of belt it measures: the same tension rings lower on a longer stretch. Choose 3 or 5 plucks. The toolhead parks in the middle. For each belt: a second of quiet, then pluck it like a guitar string about once a second; Printune averages the plucks. After both belts it says which belt to turn and about how far. CoreXY belts pull on each other, so both are measured after every turn: first both roughly to 20–10 Hz under the target, then one belt at a time until both are within `belt_tolerance` of the target and of each other. The turn it asks for gets more precise as it learns how much a turn changes your belts. With Shake&Tune, a belt comparison graph checks the result. |
| 2 | **Filament** | | the Tuning Wizard, see below |
| 3 | **Input shaper** | an accelerometer and `[input_shaper]` | Kalico's `SHAPER_CALIBRATE`. Printune shows the pick for each axis and the highest acceleration that keeps corners sharp. The shaper is on right away; **Save to printer.cfg** runs `SAVE_CONFIG` (Kalico restarts). If the shaper changed, re-check pressure advance. With Shake&Tune, its shaper graphs run from the same step and show on the page. |

The belt measurement is new: the pluck detection is tested on a simulated accelerometer and synthetic recordings (fans, frame resonances, loose and over-tight belts, a belt heard mostly at twice its tone), not yet on many real printers. Fans and motors that run all the time are measured in the quiet second and taken off.

Every belt recording (and its quiet second) is saved in `printune_exports/recordings/` in the config folder, so you can download it from Mainsail's Machine page; the last 40 are kept. To record anything else, run `PRINTUNE_RECORD SECONDS=10 NAME=plucks` (optional `CHIP=` as for Kalico's own commands, e.g. `CHIP=beacon`; at most 30 s; the same `NAME` overwrites the earlier file). Kalico's own `ACCELEROMETER_MEASURE` writes to `/tmp`, which Mainsail can't reach.

## The tests

They run in this order because each one needs the picks before it. The heat soak is done once, and the printer stays hot between tests.

| # | Test | What prints | How you pick | Goes into Orca as |
|---|---|---|---|---|
| 1 | First layer | 9 patches of 25×25 mm over the whole bed, plus 4 optional extra patches | Babystep with the big ±0.01 / ±0.05 buttons while they print. Then *Squish is good*, or *Print 4 more patches* to check | your Z offset (see below) |
| 2 | Pressure advance | a row of small hollow frames, one per value (0–0.08 in 0.005 steps). Each long edge prints slow, fast, slow, so a wrong PA shows as a bulge or gap where the speed changes. Values are printed under the frames | the frame with the most even edges; then an optional fine pass in 0.001 steps | Filament › Pressure advance (Enable ticked) |
| 3 | Flow | 30×30×3 mm cubes at −4 … +4 % flow, numbered | the smoothest top surface; then an optional fine pass in 0.5 % steps | Filament › Flow ratio |
| 4 | Retraction and temperature | up to 4 towers across your temperature range, hottest first, each with rings at 0–1.0 mm retraction | the cleanest tower, then the ring 1–2 above where stringing stops | Filament › Nozzle temperature and retraction (below) |

Hands-on tools (on the page under *Hands-on tools*, no print): extruder calibration (`rotation_distance`, a printer setting that stays in printer.cfg) and a max volumetric flow test.

Where each result goes if you type it in by hand:

| Result | Orca field |
|---|---|
| Pressure advance | Filament › Pressure advance (tick *Enable*) |
| Flow | Filament › Flow ratio |
| Nozzle temperature | Filament › Nozzle temperature |
| Retraction with firmware retraction | Filament › Advanced › Filament start G-code: `SET_RETRACTION RETRACT_LENGTH=… RETRACT_SPEED=… UNRETRACT_EXTRA_LENGTH=0 UNRETRACT_SPEED=…` (firmware retraction is one setting for the whole printer, so every filament needs its own line) |
| Retraction without it | Filament › Setting overrides › Retraction length and speeds |
| Max volumetric flow | Filament › Max volumetric speed |
| First-layer squish | your Z offset: `Z_OFFSET_APPLY_PROBE` + `SAVE_CONFIG`, or however your probe setup stores it |

## Settings (`printune_settings.cfg`)

| Option | Default | |
|---|---|---|
| `start_gcode` | `PRINT_START BED={bed} BED_TEMP={bed} HOTEND={nozzle} EXTRUDER={nozzle} EXTRUDER_TEMP={nozzle}` | start of every test print; `M190`/`M109` follow it in case your macro uses other names |
| `end_gcode` | `PRINT_END` | |
| `enclosed` | `False` | offer a heat soak (default on for ABS, ASA, PC, PA) |
| `chamber_sensor` | the nozzle | sensor the soak watches, e.g. `temperature_sensor chamber`. Without one the nozzle is used: hotend off, parked high, it reads the air around it |
| `soak_min`, `soak_max` | `10`, `60` | minutes |
| `soak_rate` | `0.5` | the soak is done when the sensor rises less than this (°C) in 5 minutes |
| `pa_range` | `0-0.08/0.005` | direct drive; a bowden needs a longer range, e.g. `0-1.0/0.05` |
| `ret_range` | `0-1.0/0.1` | retraction rings (mm); bowden e.g. `0-4/0.4` |
| `toolhead_radius` | `65` | mm the layout keeps clear of finished prints. A small toolhead (Voron V0: about 35) fits more tests on one plate |
| `orca_settings` | | other Orca settings for the test prints, one `key = value` per line (speeds, accelerations, line widths) |
| `path` | `config/printune_exports` | where the Orca presets are written |
| `belt_target` | `110` | Hz, the belt target the belt step starts with (changeable on the page) |
| `belt_tolerance` | `2` | Hz; belts are done when both are this close to the target and to each other |

The test prints use the bed size from the axis limits. If your axes reach past the plate (a wiper or purge bucket), set the plate in `orca_settings`: `printable_area = 0x0,350x0,350x350,0x350`.

The test prints use the printer's own limits: the bed size from the axis limits, the nozzle and filament diameter from `[extruder]`, and speeds and accelerations capped at `max_velocity` and `max_accel`. Layer height is half the nozzle diameter.

## Slicing the session in Orca instead (advanced)

You can also slice the session in Orca with your own process profile, so the test prints use exactly your speeds and widths:

1. Orca › Process › Others › Post-processing scripts: `/usr/bin/python3 /path/to/printune_gen.py;` (Windows: `"C:\Path\To\python.exe" "C:\path\to\printune_gen.py";`). It touches only slices with an object named `Printune…` on the plate.
2. Turn on **Label objects**. Put `Printune.stl` on an empty plate, slice and print. Options go in the object name, e.g. `Printune pa=0-0.06/0.005 temps=255,245,235 skip=patches` (`_` works instead of `/`).
3. The exported preset is then a copy of the filament preset you sliced with plus your picks, named "<preset> (Printune)".

Options: `skip` (`patches,pa,em,ret`), `pa`, `pa_fine`, `em`, `em_fine`, `ret`, `ret_speed`, `temps`, `patches_extra`, `em_infill`, `em_size`, `em_height`, `clearance`. Orca only runs the script on export or upload, and not with *Use 3MF instead of G-code* switched on.

## Disclaimer

**Use at your own risk.** Printune is provided as is, without any warranty. You use it and the G-code it generates at your own risk; the authors take no responsibility for damage to your printer, property or anything else. Watch the first session from start to finish. The full terms are in the GPL-3.0 licence (sections 15 and 16: no warranty, no liability).

## Caveats

- **Z offset.** The squish test uses live babystepping. Whether `Z_OFFSET_APPLY_PROBE` stores it depends on your probe; with a contact-mode probe, or a start macro that sets its own offset, save it the way your setup expects.
- **Toolhead clearance.** The layout keeps travel away from finished prints using a 65 mm toolhead radius (`toolhead_radius`). The PA frames are under 1 mm tall and may sit closer.
- **Small beds (down to 120×120 mm).** Printune fits what it can on one plate and uses a coarser pressure advance step (the fine pass fills in). When the next test doesn't fit, the printer pauses with **Clear the plate**: take the prints off (keep them to compare), press continue, and the session goes on with the printer still hot. With every optional pass, the whole wizard needs one plate from about 300 mm, two at 250 mm, three or four at 220–235 mm, and seven on a Voron V0 with `toolhead_radius: 35` (eight with the default 65). Skipped passes need no plate change. Each single test fits on one 120×120 plate, except that the fine passes, the extra first-layer patches and a third retraction tower may need another.
- **Login.** The page uses Moonraker's API like Mainsail does. On a printer with `force_logins`, add your phone's network to `trusted_clients`.
- **Not tried yet:** importing the preset with the Orca app's File menu (tested with the Orca 2.4.2 command line), KlipperScreen (it can't show web pages), mainline Klipper.
- **During a pause** the toolhead parks above the prints at the back of the bed. Don't babystep while paused.

## Files

| File | |
|---|---|
| `printune_gen.py` | builds the session G-code (also the Orca post-processing script) |
| `printune.cfg` | macros: the PRINTUNE menu, prompts, picks, heat soak, hands-on tests |
| `printune.py` | Kalico plugin: wizard start (`_PRINTUNE_START`), heat soak, Orca preset export, belt listening, input shaper results |
| `printune_belts.py` | finds the plucks in an accelerometer recording and plans the next belt turn |
| `moonraker/printune_web.py`, `web/index.html` | the Printune page and the Moonraker component that serves it |
| `install.sh` | installer |
| `Printune.stl` | placeholder object for the Orca route |
| `example_session.gcode` | an example session (370 mm bed, ABS) |
| `tools/` | offline G-code checker and layout renders |
| `test_env/` | a virtual Kalico printer (Docker, 20x speed) for testing, see test_env/README.md |
