# Virtual Kalico printer

A fake printer for testing macros, tuning sessions and Moonraker plugins. It runs the real Kalico (klippy), Moonraker and
Mainsail, so prompts, buttons, the console, pause/resume and exclude_object behave as on a real printer. Nothing is
physical: the MCU is virtual and temperatures come from a simple thermal model. **Everything runs 20x faster than real
time**, heat-up, heat soak and printing alike. The full default ECAL session (about 2 h 10 of printing) finishes in
a few minutes.

It is laid out like a Raspberry Pi OS Lite + KIAUH install: user `pi`, `~/kalico`, `~/klippy-env`, `~/moonraker`,
`~/printer_data`.

## Start it

Needs Docker with the compose plugin. From this folder:

```
docker compose up -d --build      # first build takes a few minutes
```

Then open **http://localhost:8080** (Mainsail). It is only reachable from your own PC, and Moonraker's API is on the same
port. If your user id is not 1000 (`id -u`), set `PUID`/`PGID` in docker-compose.yml so the files in `gcodes/` and
`logs/` stay yours.

The virtual MCU keeps one CPU core busy while the printer runs. Stop it with `docker compose down` when you are done;
it does not start again by itself after a reboot.

| Want to | Do |
|---|---|
| Stop / start | `docker compose down` / `docker compose up -d` |
| Change the speed-up | edit `SPEED` in docker-compose.yml (20 is stable, 1 is real time), then `docker compose up -d` |
| Edit the ECAL macros | edit `../ellis_calibration.cfg` (the parent folder is mounted read-only), then `FIRMWARE_RESTART` in Mainsail |
| Reset printer.cfg, saved variables and history | `docker compose down -v` (they are kept in Docker volumes otherwise) |
| Add print files | drop them in `gcodes/` or upload in Mainsail |
| Read logs | `logs/klippy.log`, `logs/moonraker.log` |
| Peek at the thermal model | `SIM_THERMAL` in the console (`SIM_THERMAL CHAMBER=45` sets a value) |

## Run a session without clicking

`tools/drive_session.py` uploads a G-code file, starts it, and answers every prompt by "pressing" a button. Without a rule
it presses the middle button. It logs each prompt and the printer state (PA, flow, retraction, temperatures, position)
to a `.run.jsonl` file.

```
python3 tools/drive_session.py ../example_session.gcode --pick "Retraction towers\?=Print"
```

## What is simulated, and what is not

| Part | How |
|---|---|
| MCU | Kalico's own simulator board, patched by `vmcu/patch_simulator.py` to talk serial and to declare pins. It accepts every command and drives nothing. Its real-time "too late" shutdowns are switched off, because a time speed-up multiplies scheduling jitter. |
| Speed-up | libfaketime runs klippy and the virtual MCU on the same faster clock. Moonraker and the browser stay on real time. |
| Homing | Endstop pins always read "triggered", so homing completes instantly at the endstop position. |
| Temperatures | `sim_thermal` (a klippy extra, test-only): lumped bed, hotend and chamber nodes, driven by the heaters' real PWM output, with sensor lag and a little noise. A cold nozzle near a hot bed reads the bed, not the chamber, which falls off with nozzle height. `[temperature_sensor chamber]` shows the model's true chamber air. |
| Not simulated | Filament, extrusion, print quality, probing, input shaper, TMC drivers, real step timing. |

Timeouts run on the fast clock too: a 30 min idle timeout lasts 90 s of real time at 20x. Use `SPEED=1` when you want to
click through a session at your own pace. klippy.log also rotates at "midnight" on the fast clock, about every 70
minutes of real time at 20x.

## Files

| Path | What |
|---|---|
| `Dockerfile`, `docker-compose.yml` | the image and how to run it |
| `vmcu/` | virtual MCU patch and pty launcher |
| `printer_config/` | printer.cfg (Nebula-370-sized CoreXY), macros.cfg, moonraker.conf, sim_thermal.py |
| `config/` | supervisord, nginx and Kalico build config |
| `tools/drive_session.py` | scripted prompt answering |
| `netfix/` | empty; only for networks behind a TLS-inspecting proxy (see Dockerfile) |

Kalico and Moonraker are pinned to the commits this was tested with (build args `KALICO_REF`, `MOONRAKER_REF`).
Mainsail is the latest release.
