# Printune

3D printer and filament profile tuner for Kalico and Moonraker. It runs the tuning prints on the printer, asks for your picks in Mainsail or Fluidd, and writes the results into OrcaSlicer profiles.

Its first part, **ECAL**, is described below.

## ECAL: Ellis's tuning guide as one heat-soaked print

ECAL turns Ellis's Print Tuning Guide into **one G-code session**. The printer heat-soaks once, then prints these tests in the guide's order:

1. First-layer squish
2. Pressure advance
3. Extrusion multiplier
4. Retraction

Between tests the printer pauses, and Mainsail/Fluidd shows one button per tested value. The value you pick is applied straight away, so every later test is printed with your earlier picks, just as the guide intends. The chamber never cools between tests.

It plugs into OrcaSlicer as a post-processing script, so it uses your printer, filament and process profiles, plus your own start and end G-code.

| What | |
|---|---|
| Prints | 1 session (all optional parts included): about 2 h 10–25 min of printing plus heating and your decision time (Orca's own estimate leaves out the pauses); about 17 m of filament (43 g ABS) |
| Firmware | Klipper or Kalico, with `[exclude_object]`, `[pause_resume]`, `[respond]`, and Mainsail or Fluidd for the prompts |
| Slicer | OrcaSlicer 2.x (tested with the 2.4.2 Linux build); standalone use also works |
| Files | `ecal.py` (generator / Orca script), `ellis_calibration.cfg` (macros), `ecal_export.py` (Orca preset export, optional), `ECAL.stl` (placeholder object) |

### What's in the session, and why it is combined like this

Ellis's order is extruder → first layer → PA → EM → (PA/EM oddities, cooling) → retraction → overlap/stepover. Every test depends on the ones before it, so ECAL keeps that order. Each test is still printed exactly as Ellis prints it. Only the waiting in between is removed.

| # | Test (Ellis section) | What prints | How you choose | Applied to later tests as |
|---|---|---|---|---|
| 0 | Extruder calibration | nothing (hands-on, do it during the heat soak) | `ECAL_EXTRUDE_TEST`, then `ECAL_EXTRUDE_RESULT LEFT=<mm>` | live `rotation_distance` |
| 1 | First layer squish | 9 patches, 25×25 mm, over the bed (corners, edges, centre), plus 4 optional extra patches | Babystep live while they print. Then press *Squish is good*, or *Print 4 more patches* to check the new offset | your Z offset |
| 2 | Pressure advance | Ellis's pattern: 0–0.08 in 0.005 steps, 3 walls, 30 mm, 90° corners, 4 layers, numbered tab | Press the button for the sharpest corner. Then press *Fine pass* (±0.005 in 0.001 steps around your pick) or *Skip* | `SET_PRESSURE_ADVANCE` |
| 3 | Extrusion multiplier | 30×30×3 mm cubes at flow ratio −4/−2/0/+2/+4 %. Each has 2 bottom layers, 30 % infill and a 100 % width monotonic top. Numbered tabs | Press the button for the best top surface. Then do an optional fine pass (±1 % in 0.5 % steps) | `M221` (EM) |
| 4 | Retraction (optional) | 3 towers at nozzle +10 / 0 / −10 °C, rings 0–1.0 mm in 0.1 mm steps at 30 mm/s. Printed one at a time, hottest first, front to back | Before the towers a prompt asks whether to print them (Ellis: often unnecessary) and reminds you to brush the nozzle. After the last tower, press the cleanest temperature, then the ring number. Ellis: take 1–2 rings above where stringing stops | end-of-session summary (towers are printed with `SET_RETRACTION` per ring) |
| - | Overlap (infill/perimeter) | nothing extra | Look at the EM cubes' top edges (Ellis does this with the same cubes) | - |
| - | Max volumetric flow (Advanced) | nothing (extrude in air) | `ECAL_FLOW_TEST FLOW=<mm³/s>` | - |
| - | PA/EM oddities, cooling, stepover | Guidance only in Ellis; no print | - | - |

**Why not more in parallel?** Every test needs the value picked in the one before:

- EM cubes printed with the wrong PA have bad edges and seams.
- Retraction depends on both PA and EM.

So the tests must stay sequential. ECAL makes them sequential within one heat soak instead of four separate prints.

**Why one tower per temperature?** Ellis prints the towers one at a time from hottest to coolest so that the temperature can drop between them without a wait. The towers stand in a column from front to back, with the hottest at the front. The nozzle therefore never travels over a finished tower.

**Where the results go.** The session changes PA, flow and retraction only while it runs; at the end the printer's own values are back. Everything that depends on the filament goes into the **Orca filament profile**, so swapping filament can never leave a stale firmware value behind.

With `ecal_export.py` installed (see Installing), the printer writes that profile for you at the end of the session: `config/ecal_exports/<filament name>.json`, an Orca filament preset holding the profile you sliced with plus your picks (flow ratio, pressure advance, nozzle temperature, retraction). Download it in Mainsail (Machine › ecal_exports) and import it in Orca with **File › Import › Import Configs**.

- A filament preset you made yourself keeps its name, so the import replaces it (Orca asks first).
- A system preset such as *Generic ABS @System* cannot be overwritten, so the export is a new preset named *Generic ABS (ECAL)* that inherits from it.
- The preset's notes list every value that changed and the date.

The summary prompt also lists each value with its field, for typing them in by hand:

| Result | Where it goes |
|---|---|
| Pressure advance | Filament › Pressure advance (tick *Enable*); Orca then sends it at the start of every print with that filament |
| Extrusion multiplier | Filament › Flow ratio |
| Nozzle temperature (from the towers) | Filament › Nozzle temperature |
| Retraction, firmware retraction | Filament › Advanced › Filament start G-code: `SET_RETRACTION RETRACT_LENGTH=… RETRACT_SPEED=… UNRETRACT_SPEED=…`. Give every filament profile its own line, because firmware retraction is one setting for the whole printer |
| Retraction, slicer retraction | Filament › Setting overrides › Retraction length and speeds |
| Max volumetric flow | Filament › Max volumetric speed |
| Extruder `rotation_distance` | printer.cfg `[extruder]` (hardware, the same for every filament, as Ellis says) |
| First-layer squish | your printer's Z offset (`Z_OFFSET_APPLY_PROBE` + `SAVE_CONFIG` after the print, or however your Beacon setup stores it) |

### Installing

1. Copy `ellis_calibration.cfg` next to `printer.cfg` and add `[include ellis_calibration.cfg]`. You also need `[exclude_object]`, `[pause_resume]` and `[respond]` (most Mainsail/Fluidd configs already have them). Restart.
2. In Orca, go to **Process › Others › Post-processing scripts** and add one line:
   `/usr/bin/python3 /path/to/ecal.py;`
   On Windows: `"C:\Path\To\python.exe" "C:\path\to\ecal.py";`
   Python 3.8 or newer is enough; the script uses only the standard library.
3. Make sure **Label objects** is enabled in Orca for this printer. ECAL reads the options in the object name from the labels Orca writes.

The script does nothing to any slice that has no object named `ECAL…` on the plate, so it is safe to leave it enabled in your normal process profile.

4. Optional, recommended: the Orca preset export. Link `ecal_export.py` into Kalico's plugin folder and add `[ecal_export]` to printer.cfg, then restart:
   `ln -s ~/printer_data/config/ecal_export.py ~/kalico/klippy/plugins/`
   (mainline Klipper: `~/klipper/klippy/extras/`). Without it the session works the same and the summary lists the values to type in.
5. Optional: with `[save_variables]` in printer.cfg every pick is also logged to that file as you tap it, with no restart. It is a record only; nothing is loaded back. `ECAL_SAVED` lists it.

### Running a session

1. Load your usual filament and process profile. Put `ECAL.stl` on an empty plate; its position does not matter. Slice and send. The file is saved as `…_ECAL.gcode`.
   - To change test ranges, rename the object in Orca's object list (options are separated by spaces; a trailing `.stl` is ignored), e.g.
     `ECAL pa=0-0.06/0.005 temps=255,245,235 skip=patches`
   - All options (with defaults):

     | Option | Default | Meaning |
     |---|---|---|
     | `skip` | – | any of `patches,pa,em,ret` |
     | `pa` | `0-0.08/0.005` | coarse PA range/step (Ellis default for direct drive) |
     | `pa_fine` | `0.005/0.001` | ± range/step around your pick; `0` = no fine pass |
     | `em` | profile flow −4…+4 % / 2 % | e.g. `0.92-1.00/0.02` |
     | `em_fine` | `0.01/0.005` | ± range/step around your pick; `0` = none |
     | `em_infill`, `em_size`, `em_height` | `30`, `30`, `3` | cube settings |
     | `ret` | `0-1.0/0.1` | rings (mm); max 1 mm for direct drive per Ellis |
     | `ret_speed` | `30` | retract/unretract speed, mm/s |
     | `temps` | nozzle +10, 0, −10 | tower temperatures, hottest first |
     | `patches_extra` | `4` | optional second round of squish patches |
     | `clearance` | Orca's extruder clearance radius | toolhead radius for the collision check |

2. Start the print. Your `PRINT_START` heats and heat-soaks as usual. Do the **hands-on tests during the soak**: `ECAL_EXTRUDE_TEST` / `ECAL_EXTRUDE_RESULT`, and `ECAL_FLOW_TEST` if you want it.
   Ellis asks for at least an hour of heat soak on a big enclosed printer before the first-layer patches; if your `PRINT_START` soaks less, let the chamber settle first.
3. Follow the prompts. Each prompt pauses the printer with the hotend and bed held hot; the idle timeout is lifted to 3 h during the session. Pick a value. The printer resumes, prints the next test with that value and pauses again.
4. After the towers, the summary prompt appears and your original PA, flow and retraction settings are restored. To see the summary again, run `_ECAL_SUMMARY`.
5. If you cancel mid-session, run `ECAL_RESET` to restore PA, flow, retraction and the idle timeout. If you forget, the next session notices the unfinished one and keeps your original values. After a firmware restart there is nothing to restore.

Standalone, without Orca:

```
python3 ecal.py --from any_sliced_by_orca.gcode -o session.gcode   # profile from an Orca G-code
python3 ecal.py -o session.gcode --set nozzle_temperature=245 --start "PRINT_START BED=100 HOTEND=245"
```

### Assumptions and caveats

- **Bed size.** Standalone defaults assume a 375×388 mm bed, the ZeroG Nebula 370. In Orca the printable area of your printer profile is used. A layout check refuses beds that are too small.
- **Toolhead clearance.** The EVA toolhead geometry is not modelled. The check uses Orca's *extruder clearance radius*, and the example was checked with 65 mm.
  - Results: no travel crosses printed material, and nothing goes off the bed.
  - The PA patterns sit within 65 mm of the towers. They are under 1 mm tall, so this only matters if something on the toolhead hangs lower than 1.2 mm.
- **Z offset.** Squish uses live babystepping. Whether `Z_OFFSET_APPLY_PROBE` / `SAVE_CONFIG` stores the babystep depends on your probe setup. With a Beacon in contact mode, or with a start macro that sets its own offset, save it the way your macro expects.
- **Retraction.** With firmware retraction (`[firmware_retraction]`, Orca *Use firmware retraction*), the towers use G10/G11 with `SET_RETRACTION` per ring. Without it, ECAL's own `_ECAL_R` / `_ECAL_U` macros do the ring retractions. Either way, your normal retraction is restored at the end.
- **How much fits.** On a 375×388 mm bed one pass holds up to 6 EM cubes, about 30 PA patterns and 3 towers. Larger ranges are refused with a message instead of being squeezed in.
- **Temperatures.** The patches print at your first-layer temperature, everything after them at the normal nozzle temperature (Ellis tunes at printing conditions). The towers use nozzle +10/0/−10 °C.
- **During a pause** the toolhead parks 10 mm above the tallest print at the back of the bed, so it never rests on a test piece. Do not babystep while paused; RESUME returns to the saved Z.
- **Mainline Klipper.** Tested against Kalico's sources. Mainline's firmware retraction does not ignore G10/G11 inside a skipped object, so the first object after a skipped one can start about one retraction length short; its prime line takes most of that up.
- **No tool changes.** A `T0` or `ACTIVATE_EXTRUDER` resets Klipper's flow multiplier, so don't put one in a pause macro.
- **Orca pitfalls.** Orca runs post-processing scripts only when it exports or uploads the G-code: its preview, time estimate and the project file show the plain placeholder. With the printer option *Use 3MF instead of G-code* switched on, Orca skips post-processing scripts on upload; switch it off for this print. Orca puts the full name into a `; printing object` comment, which is where ECAL reads the options from.
- **The Orca plugin system** (main branch, not in any release yet) is not used. The post-processing script works on released versions.
- **Preset export.** It needs the session to be sliced by Orca (the settings block at the end of the G-code is where the profile comes from); a standalone session gets the summary only. Orca writes slicer-retraction overrides of a user preset into the printer's retraction fields, not as filament overrides, so the export only carries retraction values you picked in the session. The preset is written for the Orca version that sliced the file.
- **Step separator.** A file name cannot hold `/`, so `_` works too: `ECAL pa=0-0.06_0.005.stl`.
- **Pressure advance.** If PA is disabled in the filament profile, ECAL keeps whatever PA the printer currently has as the baseline.
- **What is copied from Ellis, and what is not.** Ellis's own PA tool and his EM/retraction instructions are the source for every test parameter. The cooling, PA/EM oddities and stepover sections have no print to make.

### Files

| File | |
|---|---|
| `ecal.py` | generator and Orca post-processing script (GPL-3.0-or-later, because the PA pattern is ported from Ellis's GPL-3.0 tool) |
| `ellis_calibration.cfg` | Klipper/Kalico macros: prompts, pick handling, hands-on tests |
| `ecal_export.py` | Kalico plugin: writes the Orca filament preset at the end of a session (`ECAL_EXPORT`) |
| `ECAL.stl` | 20×20×0.6 mm placeholder that triggers the script |
| `example_session.gcode` | a standalone example (Nebula 370 defaults, generic ABS) |
| `tools/check_gcode.py` | offline simulator: off-bed moves, widths, travel crashes, toolhead clearance |
| `tools/plot_layout.py`, `tools/render_shaded.py` | the rough layout plot and the shaded render (`renders/`) |
| `testenv/` | virtual Kalico printer (Docker, 20x speed) for testing the session and the macros, see testenv/README.md |
