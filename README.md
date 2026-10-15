# Printune

Filament tuning for Kalico printers, made for people who just want to press a button. Printune prints the tuning tests on your printer, asks you to pick the best one after each test (on your phone, in Mainsail or Fluidd), and writes the results into an **OrcaSlicer filament preset** you import with two clicks.

- **One big blue button: Tuning Wizard.** Pick the filament type, give it a name, check the temperature range from the box, press Start. Printune builds the test prints for your printer, heat-soaks if you want, and walks you through first layer, pressure advance, flow, and retraction and temperature.
- **Advanced tuning, when the basics are done:** the cooling test (fan and minimum layer time), the bridge test (bridge fan, speed and flow) and the max volumetric flow tower, for the same preset.
- **Or one test only:** any of the above on its own.
- **Your printer's geometry is yours to give.** Before the first test print, the **Machine setup** asks for the bed area, the size of the toolhead around the nozzle and the height of the X gantry, measured by you, and for you to accept the risk. Printune assumes none of it and prints nothing until it is done.
- **The printer too, in the right order:** belt tension first (pluck each belt; the toolhead accelerometer hears it), then the filament, then the input shaper. Printune runs Kalico's own shaper calibration and, if installed, Shake&Tune's graphs. Steps whose tools are missing are greyed out and link to where to get them.
- **Works on a phone.** The Printune page is made for a phone next to the printer, and works on a desktop and on small touch screens with a browser.
- **Results go into the slicer, not the firmware.** Pressure advance, flow ratio, retraction, temperature, fan and minimum layer time are per filament, so they belong in the filament preset; bridge speed and flow go into a process preset. The printer's own values are back when the session ends, so swapping filament can never leave a stale value behind.

| | |
|---|---|
| Firmware | Kalico (mainline Klipper may work but is not tested), Moonraker, Mainsail or Fluidd |
| Printer config | `[exclude_object]`, `[pause_resume]`, `[respond]`, `[virtual_sdcard]` (Mainsail's and Fluidd's standard configs have most of them) |
| Slicer | OrcaSlicer 2.x for the preset (tested with 2.4.2) |
| Session | Tuning Wizard: about 2 h 10 min of printing plus heating and your decision time, about 17 m of filament. Advanced tuning: about 1 h 45 min for ABS (1 h 15 min for PLA), 11–13 m: the cooling test about 50 min for ABS and 20 min for PLA, the bridge test about 40 min, the max flow tower about 15 min |
| Licence | GPL-3.0-or-later |

> **Use at your own risk.** Printune generates G-code for your printer from the geometry you enter. We take zero responsibility for that G-code, collisions or what it does; see [Disclaimer](#disclaimer).

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

Then do the **Machine setup** on the Printune page (step 1 of [Tuning the printer](#tuning-the-printer)). Until it is done, every test print is locked.

## Use it

Open **Printune** in the Mainsail sidebar, or `http://<printer>/server/printune` on your phone (bookmark it or add it to the home screen).

1. The first time: **Machine setup** (see below). Then press **Tuning Wizard**, **Advanced tuning** or one of the single tests.
2. Pick the filament type, name it (this becomes the preset name in Orca), set the nozzle range from the box and the bed temperature. Clean the plate and press **Start**.
3. With heat soak on, the printer heats the bed with the toolhead parked high and the hotend off, and starts the tests by itself when the temperature levels off. **Skip the soak** starts them right away.
4. After each test the printer pauses and the page shows one big button per printed value. Press the best one. The printer applies it and prints the next test with it.
5. At the end the page shows your results and a **Download Orca preset** button. In Orca: **File › Import › Import Configs**, pick the file. The preset shows up under your filaments with the name you gave it, based on Orca's Generic preset for that type. After the bridge test there is a second button, **Download process preset**: import it the same way; it shows up under your process (print) presets.

No phone or page? The **PRINTUNE** macro button in Mainsail or Fluidd opens the same menu as prompts (machine setup, belt tension, Tuning Wizard, single tests, Advanced tuning, input shaper, extruder calibration). The machine setup's numbers are typed in the console there: `_PRINTUNE_SETUP BED=mesh TOOLHEAD=-30,30,-40,25 GANTRY=42 PROTRUSION=3`, then `_PRINTUNE_SETUP ACCEPT=1 SAVE=1` (your own numbers, not these). It names the preset "<type> Printune"; rename it in Orca.

### Advanced tuning and the preset

Advanced tuning is a second session for a filament the wizard has tuned: the cooling test, the bridge test, then the max flow tower (the tallest print goes last, so the gantry never passes over it). Like the wizard it heat-soaks first on an enclosed printer, you can switch any test off on the form, it asks before the bridge test and the max flow tower, and each test also runs on its own. Give it **the same name** as in the wizard and the same filament type. It then prints with what the wizard picked (from the exported preset): the nozzle temperature from the retraction towers, pressure advance, flow ratio and retraction. It writes its own G-code file (`printune/<name>_advanced.gcode`), so the wizard's stays. Its results are **added to that preset**: Printune keeps the last export of each preset name in `printune_exports/`, and every new session updates only the fields it tested and keeps the rest. So after the wizard and Advanced tuning the one file holds everything; import it in Orca again and it replaces the earlier import. Running the wizard again later keeps the advanced results the same way. If you edited the preset in Orca in between, those edits are not in the file; re-check them after the import. A different name starts a separate preset, at the middle of the temperature range and with the printer's own pressure advance, flow and retraction. A preset of another filament type is never reused or merged: Printune says so and names the new preset "<name> <type>".

The bridge test gives **two files**. The bridge fan goes into the filament preset like everything else. Bridge speed and bridge flow are print (process) settings in Orca, so they go into a second file, a process preset. When the session was sliced in Orca, it is a copy of the process preset you sliced with (every process setting in the sliced file, on the same parent) with only those two values changed, named `<your process preset> (Printune)`. A session made on the printer has no process preset to copy: the file is `printune_exports/<name> bridges.json` and holds only those two values on top of Orca's defaults. Import it the same way; to use the values in another process preset, copy them across (Process › Speed › Bridge › *External* and Process › Quality › *Bridge flow ratio*) or pick the imported one.

If you cancel half-way, press **Reset after cancel** in the PRINTUNE menu (or run `_PRINTUNE_RESET`). It puts pressure advance, flow, firmware retraction and the idle timeout back. After a firmware restart there is nothing to put back.

## Tuning the printer

Do these in order: the machine setup, the belts, the Z offset, then each filament (the wizard), then the input shaper. A ringing print or a pressure-advance pick reads better on a printer with the belts right and a tuned filament.

| # | Step | Needs | What happens |
|---|---|---|---|
| 1 | **Machine setup** (required) | a ruler or calipers | Printune prints several tests on one plate, one after the other, so the toolhead and the X gantry move next to and over finished prints. It places the tests with four numbers **you measure**; it assumes none of them. **Bed area**: where the nozzle may print, `x0,y0,x1,y1`; the page offers your `[bed_mesh]` `mesh_min`/`mesh_max` cut to the axis limits (where the probe has been, so on the plate); without a bed mesh you type it. **Toolhead box**: from the nozzle to the furthest point of the toolhead toward -X, +X, -Y and +Y (fans, ducts, probe, carriage); the X gantry is taken to reach as far in Y as this box, so if the beam sticks out further in Y, use that. **Gantry height**: from the bed to the underside of the X gantry with the nozzle at Z=0 (measure at Z=50 and take 50 off). **Nozzle protrusion**: from the nozzle tip up to the lowest other part of the toolhead (fan duct, probe, silicone sock); put the cold nozzle on a flat card on the bed and measure up to that part, or use calipers. A finished print at least 0.2 mm lower than the protrusion may sit under the toolhead (the nozzle never goes below the first layer, so the real gap is bigger); anything taller is kept outside the toolhead box. Then you confirm you measured them yourself and accept that Printune and its authors take zero responsibility for the G-code, collisions or damage (the GPL's no-warranty terms apply anyway). The page explains the measuring with a drawing. Saved under `[printune]` in printer.cfg (`SAVE_CONFIG`, without a restart on Kalico); if other changes are waiting for `SAVE_CONFIG` (a new Z offset, say) it does not save and asks you to run `SAVE_CONFIG` yourself, which saves both and restarts. Change it any time. The acceptance holds a fingerprint of the four values: changing one, on the page, in the console or by hand in printer.cfg, means accepting again. With the plugin loaded, what is enforced: `_PRINTUNE_START` (page, menu or typed) refuses to build a session until the setup is done and the bed area is inside the axis limits, the setup can't be changed while a session is being prepared or printed, and every session file checks at its start (`_PRINTUNE_BEGIN`) that the setup is done and that the session was made for exactly these values, so a session from Orca or an older file made with other numbers does not run. Without the plugin (only `printune.cfg` included) the printer can't check this: the generator then only refuses to build a session without the geometry options. The belts, Z offset, input shaper, recordings and the hands-on tools (they extrude in the air and don't move over the bed) work without it. |
| 2 | **Belt tension** | a CoreXY printer with an accelerometer on the toolhead (`[resonance_tester]`) | Set the target frequency your printer's build guide gives, and pluck the same stretch of belt it measures: the same tension rings lower on a longer stretch. Choose 3 or 5 plucks. The toolhead parks in the middle. For each belt: a second of quiet, then pluck it like a guitar string about once a second; Printune averages the plucks. After both belts it says which belt to turn and about how far. CoreXY belts pull on each other, so both are measured after every turn: first both roughly to 20–10 Hz under the target, then one belt at a time until both are within `belt_tolerance` of the target and of each other. The turn it asks for gets more precise as it learns how much a turn changes your belts. With Shake&Tune, a belt comparison graph checks the result. |
| 3 | **Z offset** | a probe (`PROBE_CALIBRATE`), a Beacon (`BEACON_CALIBRATE`) or a Z endstop (`Z_ENDSTOP_CALIBRATE`) | Sets the starting Z offset with the printer's own paper test: Printune homes, levels the gantry if it can, moves to the middle of the bed and starts the routine; Mainsail or Fluidd shows the Z dialog. Accept, then Save config. Redo it after a nozzle or build plate swap. The first layer test in the wizard fine-tunes it while printing. |
| 4 | **Filament** | | the Tuning Wizard, then Advanced tuning if you want it, see below |
| 5 | **Input shaper** | an accelerometer and `[input_shaper]` | Kalico's `SHAPER_CALIBRATE`. Printune shows the pick for each axis and the highest acceleration that keeps corners sharp. The shaper is on right away; **Save to printer.cfg** runs `SAVE_CONFIG` (Kalico restarts). If the shaper changed, re-check pressure advance. With Shake&Tune, its shaper graphs run from the same step and show on the page. |

The belt measurement is new: the pluck detection is tested on a simulated accelerometer and synthetic recordings (fans, frame resonances, loose and over-tight belts, a belt heard mostly at twice its tone), not yet on many real printers. Fans and motors that run all the time are measured in the quiet second and taken off.

Every belt recording (and its quiet second) is saved in `printune_exports/recordings/` in the config folder, so you can download it from Mainsail's Machine page; the last 40 are kept. To record anything else, run `PRINTUNE_RECORD SECONDS=10 NAME=plucks` (optional `CHIP=` as for Kalico's own commands, e.g. `CHIP=beacon`; at most 30 s; the same `NAME` overwrites the earlier file). Kalico's own `ACCELEROMETER_MEASURE` writes to `/tmp`, which Mainsail can't reach.

## The tests

They run in this order because each one needs the picks before it. The heat soak is done once, and the printer stays hot between tests. Tests 1–4 are the Tuning Wizard, A1–A3 are Advanced tuning.

| # | Test | What prints | How you pick | Goes into Orca as |
|---|---|---|---|---|
| 1 | First layer | 9 patches of 25×25 mm over the whole bed, plus 4 optional extra patches | Babystep with the big ±0.01 / ±0.05 buttons while they print. Then *Squish is good*, or *Print 4 more patches* to check | your Z offset (see below) |
| 2 | Pressure advance | a row of small hollow frames, one per value (0–0.08 in 0.005 steps). Each long edge prints slow, fast, slow, so a wrong PA shows as a bulge or gap where the speed changes. Values are printed under the frames | the frame with the most even edges; then an optional fine pass in 0.001 steps | Filament › Pressure advance (Enable ticked) |
| 3 | Flow | 30×30×3 mm cubes at −4 … +4 % flow, numbered | the smoothest top surface; then an optional fine pass in 0.5 % steps | Filament › Flow ratio |
| 4 | Retraction and temperature | up to 4 towers across your temperature range, hottest first, each with rings at 0–1.0 mm retraction | the cleanest tower, then the ring 1–2 above where stringing stops | Filament › Nozzle temperature and retraction (below) |
| A1 | Cooling | first one small block per fan speed (PLA, PETG, TPU 40/70/100 %; ABS, ASA 20/40/60/80 %; PC, PA 0/20/40/60/80 %), each printed alone at its fan. The front of each block leans out 30°, 40°, 50°, 60° and 70° from vertical, one step every 6 layers, with a groove on the sides at each step; the fan % is printed behind it. Then a round cone, printed alone at that fan and one constant speed, whose layer time falls with height; a small bump on the front marks every 2 s of layer time | the lowest fan whose overhangs look good up to the angle you need; then the bump where the cone starts to melt, sag or curl, or *Fine to the top*. Printune proposes that layer time + 3 s, which you can adjust | Filament › Cooling: one constant fan speed and the minimum layer time (below) |
| A2 | Bridges | three small blocks, one after the other. Each has 4 mm pillars with gaps of 20, 40 and 60 mm, bridged in four labelled 5 mm strips side by side, then one cover layer. Block 1 tries four fans, from the cooling test's fan (or the preset's) up to 100 %; block 2 four speeds (0.6, 1, 1.5 and 2 × your bridge speed, e.g. 15/25/40/50 mm/s); block 3 four flow ratios, 0.85–1.00. Each block uses the picks before it | the strip with the least sag and the cleanest, separate strands, after each block | Filament › Cooling › *Overhangs and external bridges fan speed*; Process › Speed › Bridge › *External*; Process › Quality › *Bridge flow ratio* (below) |
| A3 | Max volumetric flow | a 60 mm round tower, one wall, printed as one spiral. The flow rises 1 mm³/s every 3 mm of height (5–30 mm³/s; TPU 1–12 in 0.5 steps). A small bump on the front marks every 5 mm³/s. Printed at the tower temperature you picked, with the filament's normal fan | the height (or band) where the wall first gets rough or weak. Printune shows the flow there and proposes 90 % of it, which you can adjust. It also shows the same limit in mm/s at a 0.2 mm layer and 0.45 mm line | Filament › Max volumetric speed |

The max flow tower uses 0.75 × nozzle layers and 1.5 × nozzle lines (0.30 × 0.60 mm on a 0.4 nozzle), so 30 mm³/s is 187 mm/s. Speed is flow ÷ the line's cross-section, (w − h)·h + π(h/2)², the same as Orca. Nothing slows it down: no minimum layer time, no flow cap, speed and flow overrides set to 100 %, 1° segments and long, shallow marker bumps that Kalico's corner rule (`square_corner_velocity`, `max_accel`) passes at full speed; Printune checks every corner with Kalico's own formula. If `max_velocity`, `square_corner_velocity` or `max_accel` can't reach the top of the range, the top band is clamped to what they allow and Printune warns. Before the tower starts, your picks so far are written to the Orca preset, so cancelling the tower loses nothing. A max volumetric speed in Orca is just this flow converted: at a 0.2 mm layer and 0.45 mm line, 1 mm³/s is about 12 mm/s.

The cooling test sets one **constant fan** per filament: a fan that changes with the layer time causes banding. The fan is picked on the overhang blocks and becomes the minimum and maximum fan speed and the overhang fan speed, with *Keep fan always on* ticked, so the fan never changes during a print (Orca's overhang fan setting also covers external bridges). A bridge fan picked in the bridge test then replaces the overhang fan: bridges and overhang walls run at it, every other line keeps the constant fan. Every block layer takes the filament's usual minimum layer time (8 s for PLA, PETG and TPU, 15 s for ABS, ASA, PC and PA), so the fan is judged at a realistic layer time and not on a tiny hot block. The outer wall, the overhang, keeps its normal speed (or Orca's overhang speed, if your profile sets one). The inner walls and the fill slow down to fill the time, and if that is not enough the nozzle waits the rest 22 mm behind the block. The cone then prints at the picked fan with nothing slowing it: one speed (60 mm/s, or the outer wall speed if lower), no minimum layer time, speed override 100 %. Each layer is one spiral from the edge in to the centre, then a travel back out, so the edge is printed once per layer and the time between two passes there is the layer time. Its radius shrinks so that the layer time drops by 2 s every 1.2 mm, from 16 s (PLA, PETG, TPU) or 26 s (ABS and the others) at the bottom to about 1 s at the top. The layer times are computed from the G-code moves the way Kalico plans them (acceleration, `square_corner_velocity`, `minimum_cruise_ratio`, Z limits), not as length ÷ speed, which would be about 1 s per layer short; `tools/check_layer_time.py` re-checks them from a session file. The pick plus 3 s goes into Orca as the layer time below which it slows a layer down (*Max fan speed threshold › Layer time*). ABS and ASA usually land near 15 s, and Printune says so when the pick is far from that. A cone that curls at its base at low fan is often a bed adhesion problem, not a cooling one. Like the other tests it runs after the heat soak, at the tower temperature you picked, so the result fits the warm chamber. It is part of Advanced tuning, not the wizard: the fan and layer time matter once the basics are right, and the test takes about 50 min for ABS. It prints before the max flow tower, so the tower is the last and tallest print.

The bridge test tunes Orca's **external bridges**. Its strands follow Orca's own bridge model (`LayerRegion::bridging_flow`, `Flow::with_cross_section`). With *Thick bridges* off (Orca's default) the line starts as a normal line of your bridge line width (else the internal solid infill width) at the layer height; a bridge flow ratio below 1 makes it narrower and moves the lines closer together by the same amount, as Orca does, and a ratio above 1 keeps the spacing and makes the line taller. With *Thick bridges* on it is a round strand of the bridge line width (else the nozzle) times √ratio, spaced at its diameter + 0.05 mm. Your *Bridge density* divides the spacing, as in Orca. Bridge density and thick bridges stay as they are in your profile. The fan block only helps if a bridge fan above your normal fan makes sense: Orca uses the bridge fan only when it is higher than the normal fan, so the strips start at the normal fan and go up to 100 %. When the normal fan is already 100 %, that block is skipped. The pillars print at the cooling blocks' layer time, the bridges at the speed being tested. The fan goes into the filament preset. Bridge speed and flow are print settings, so they go into a separate **process preset**; see [Advanced tuning and the preset](#advanced-tuning-and-the-preset).

Hands-on tools (on the page under *Hands-on tools*, no print): extruder calibration (`rotation_distance`, a printer setting that stays in printer.cfg) and a quick max flow check that extrudes into the air.

Where each result goes if you type it in by hand:

| Result | Orca field |
|---|---|
| Pressure advance | Filament › Pressure advance (tick *Enable*) |
| Flow | Filament › Flow ratio |
| Nozzle temperature | Filament › Nozzle temperature |
| Retraction with firmware retraction | Filament › Advanced › Filament start G-code: `SET_RETRACTION RETRACT_LENGTH=… RETRACT_SPEED=… UNRETRACT_EXTRA_LENGTH=0 UNRETRACT_SPEED=…` (firmware retraction is one setting for the whole printer, so every filament needs its own line) |
| Retraction without it | Filament › Setting overrides › Retraction length and speeds |
| Fan | Filament › Cooling: *Fan speed* under both *Min fan speed threshold* and *Max fan speed threshold*, *Keep fan always on* ticked, *Overhangs and external bridges fan speed* the same (`fan_min_speed`, `fan_max_speed`, `reduce_fan_stop_start_freq`, `overhang_fan_speed`) |
| Minimum layer time | Filament › Cooling › *Max fan speed threshold › Layer time*, with *Slow printing down for better layer cooling* ticked (`slow_down_layer_time`, `slow_down_for_layer_cooling`) |
| Bridge fan | Filament › Cooling › *Overhangs and external bridges fan speed*, with *Force cooling for overhangs and bridges* ticked (`overhang_fan_speed`, `enable_overhang_bridge_fan`). An *Overhang cooling activation threshold* of 0 % would run every outer wall at the bridge fan, so the preset then sets it to 95 % (Orca's default; `overhang_fan_threshold`); any other value is kept |
| Bridge speed | Process › Speed › Bridge › *External* (`bridge_speed`) |
| Bridge flow | Process › Quality › *Bridge flow ratio* (`bridge_flow`) |
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
| `mvs_range` | `5-30/1` (TPU `1-12/0.5`) | max flow tower bands (mm³/s), bottom to top |
| `bed_area`, `toolhead_box`, `gantry_height`, `nozzle_protrusion`, `setup_accepted` | none | the machine setup (`setup_accepted` holds the fingerprint of the four values it was given for). Written by the page or `_PRINTUNE_SETUP` into printer.cfg's SAVE_CONFIG block; don't put them in this file (a value set here can't be saved from the page). `toolhead_radius` is no longer used |
| `orca_settings` | | other Orca settings for the test prints, one `key = value` per line (speeds, accelerations, line widths) |
| `path` | `config/printune_exports` | where the Orca presets are written |
| `belt_target` | `110` | Hz, the belt target the belt step starts with (changeable on the page) |
| `belt_tolerance` | `2` | Hz; belts are done when both are this close to the target and to each other |

The test prints stay inside the machine setup's bed area. A `printable_area` in `orca_settings` still works: the tests then stay inside both. Orca's `extruder_clearance_radius` and `extruder_clearance_height_to_rod` are not used; the toolhead box and gantry height replace them.

The test prints use the printer's own limits: the bed area from the machine setup, the nozzle and filament diameter from `[extruder]`, and speeds and accelerations capped at `max_velocity` and `max_accel`. Layer height is half the nozzle diameter.

## Slicing the session in Orca instead (advanced)

You can also slice the session in Orca with your own process profile, so the test prints use exactly your speeds and widths:

1. Orca › Process › Others › Post-processing scripts: `/usr/bin/python3 /path/to/printune_gen.py --toolhead-box=-X,+X,-Y,+Y --gantry-height=MM --bed-area=X0,Y0,X1,Y1 --nozzle-protrusion=MM --accept-risk;` with your own measurements, the same as the printer's machine setup (with the plugin loaded, the printer refuses a session made for other values) (Windows: `"C:\Path\To\python.exe" "C:\path\to\printune_gen.py" --toolhead-box=… ;`). Write the box with `=`. It touches only slices with an object named `Printune…` on the plate, and refuses to build a session without the five geometry options (they can also go in the object name: `toolhead_box=… gantry_height=… bed_area=… nozzle_protrusion=… accept_risk=1`). The same options are required for the stand-alone command line.
2. Turn on **Label objects**. Put `Printune.stl` on an empty plate, slice and print. Options go in the object name, e.g. `Printune pa=0-0.06/0.005 temps=255,245,235 skip=patches` (`_` works instead of `/`).
3. The exported preset is then a copy of the filament preset you sliced with plus your picks, named "<preset> (Printune)".

Options: `proc` (`wizard`, the default, or `advanced`), `skip` (`patches,pa,em,ret,cool,bridge,mvs`; replaces the procedure's choice, so `skip=patches,pa,em` with nothing else runs retraction, cooling, bridges and max flow), `pa`, `pa_fine`, `em`, `em_fine`, `ret`, `ret_speed`, `temps`, `patches_extra`, `em_infill`, `em_size`, `em_height`, `cool_fans` (cooling blocks' fan speeds, e.g. `cool_fans=20,40,60,80`; default by `filament_type`), `cool_angles` (overhang steps, default `30-70_10`), `cool_time` (cone layer time at the bottom, s), `cool_pad` (block layer time, s; also the bridge pillars'), `bridge_spans` (gaps, default `20,40,60` mm; spans that don't fit are left out), `bridge_fans` (fan block, e.g. `bridge_fans=60,80,100`; default four steps from the picked fan to 100 %), `bridge_speeds` (default 0.6, 1, 1.5, 2 × your bridge speed), `bridge_flows` (default `0.85-1_0.05`, 0.5 to 2: below about 0.42 Orca collapses the bridge line to a thin round thread, so lower values are refused), `mvs` (max flow range, e.g. `mvs=5-30_1`), `mvs_band` (band height, mm), `scv`, `mcr`, `zv`, `za` (your `square_corner_velocity`, `minimum_cruise_ratio`, `max_z_velocity`, `max_z_accel`; defaults 5, 0.5 and your max speed and acceleration), and the required `toolhead_box`, `gantry_height`, `bed_area`, `nozzle_protrusion`, `accept_risk`. Orca only runs the script on export or upload, and not with *Use 3MF instead of G-code* switched on.

## Disclaimer

**Use at your own risk.** Printune is provided as is, without any warranty. The machine setup's numbers are your measurements and your responsibility; Printune lays out the tests from them and does not check them against the real printer. You use it and the G-code it generates at your own risk; the authors take zero responsibility for damage to your printer, property or anything else. Watch the first session from start to finish. The full terms are in the GPL-3.0 licence (sections 15 and 16: no warranty, no liability).

## Caveats

- **Z offset.** The squish test uses live babystepping. Whether `Z_OFFSET_APPLY_PROBE` stores it depends on your probe; with a contact-mode probe, or a start macro that sets its own offset, save it the way your setup expects.
- **Toolhead and gantry clearance.** While a test prints, the toolhead box (from the machine setup) around the nozzle must not reach any finished print taller than the nozzle protrusion minus 0.2 mm; the box is asymmetric, so a toolhead that sticks out on one side gets its tests placed accordingly. Lower prints (first-layer patches, PA frames, when your protrusion allows) may sit under the toolhead. The X gantry is taken to span the bed at the nozzle's Y and to reach as far in Y as the toolhead box: nothing prints while the gantry's Y band covers a finished print taller than the gantry height. In practice the max flow tower prints last, on its own plate. `tools/check_gcode.py SESSION.gcode [--box=-X,+X,-Y,+Y --gantry=MM --protrusion=MM --bed=X0,Y0,X1,Y1]` simulates a session and counts toolhead-box (every move, travels included, sampled at least every 1 mm) and gantry hits; a value left out is read from the session's header.
- **Small beds (down to 120×120 mm).** Printune fits what it can on one plate and uses a coarser pressure advance step (the fine pass fills in). When the next test doesn't fit, the printer pauses with **Clear the plate**: take the prints off (keep them to compare), press continue, and the session goes on with the printer still hot. The cooling blocks and cone print one at a time, so they stay a toolhead box apart. How many plates a session needs depends on the bed and your toolhead box. With a box of -30,+30,-40,+25 mm, a 3 mm nozzle protrusion and every test and optional pass, the Tuning Wizard needs one plate at 300–375 mm, two at 250 mm, three at 220 mm and seven at 120 mm; Advanced tuning needs three plates from 220 to 375 mm and five at 120 mm (the 60 mm bridge gap is left out on small beds). Each extra plate is one more **Clear the plate** pause, where you can also skip the cooling test, the bridge test (or, at a pause before its speed or flow pass, the passes still to come) or the max flow tower; if a later test is on the same plate, Printune asks you to clear it again. Skipped passes need no plate change. Each single test fits on one 120×120 plate, except that the fine passes, the extra first-layer patches, a third retraction tower and the cooling blocks may need more with a large toolhead box.
- **Login.** The page uses Moonraker's API like Mainsail does. On a printer with `force_logins`, add your phone's network to `trusted_clients`.
- **Not tried yet:** importing the preset with the Orca app's File menu (tested with the Orca 2.4.2 command line), KlipperScreen (it can't show web pages), mainline Klipper.
- **During a pause** the toolhead parks above the prints at the back of the bed. Don't babystep while paused.

## Files

| File | |
|---|---|
| `printune_gen.py` | builds the session G-code (also the Orca post-processing script) |
| `printune.cfg` | macros: the PRINTUNE menu, prompts, picks, heat soak, hands-on tests |
| `printune.py` | Kalico plugin: session start (`_PRINTUNE_START`, `PROC=wizard` or `advanced`), heat soak, Orca preset export, belt listening, input shaper results |
| `printune_belts.py` | finds the plucks in an accelerometer recording and plans the next belt turn |
| `moonraker/printune_web.py`, `web/index.html` | the Printune page and the Moonraker component that serves it |
| `install.sh` | installer |
| `Printune.stl` | placeholder object for the Orca route |
| `example_session.gcode` | an example Tuning Wizard session (375 mm bed, ABS), made with example geometry (bed 0,0,375,388, toolhead box -30,30,-40,25, gantry 42 mm, nozzle protrusion 3 mm): not for your printer |
| `tools/` | offline G-code checker, cooling layer-time check (`check_layer_time.py`) and layout renders |
| `test_env/` | a virtual Kalico printer (Docker, 20x speed) for testing, see test_env/README.md |
