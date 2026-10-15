# Simulated thermal model for a virtual Kalico printer.
#
# Test environment only: replaces real thermistors with a lumped model of
# bed, hotend and chamber air, driven by the heaters' own PWM output, so
# M109/M190, PID, verify_heater and heat-soak logic behave as on a real
# enclosed printer.
#
# Model (each node: C dT/dt = P_in - losses):
#   bed:     C_b dT_b = P_b*u_b - k_b (T_b - T_ch)
#   chamber: C_ch dT_ch = k_b (T_b - T_ch) + k_h (T_h - T_loc) - k_ch (T_ch - T_amb)
#   hotend:  C_h dT_h = P_h*u_h - k_h (1 + f_fan*fan) (T_h - T_loc)
# T_loc, the air around the nozzle, is chamber air plus a share of the bed
# temperature that falls off with nozzle height: T_ch + (T_b - T_ch) e^(-z/z0).
# That is why a cold nozzle parked near a hot bed reads the bed, not the chamber.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import math
import random

STEP_TIME = 0.25
REPORT_TIME = 0.5

# Survives RESTART / FIRMWARE_RESTART (the module stays loaded), so a
# config reload does not throw away a heat soak.
_STATE = {}


class SimThermal:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.amb = config.getfloat("ambient", 20.0)
        self.bed_p = config.getfloat("bed_power", 800.0, above=0.0)
        self.bed_c = config.getfloat("bed_capacity", 3400.0, above=0.0)
        self.bed_k = config.getfloat("bed_loss", 4.0, above=0.0)
        self.ch_c = config.getfloat("chamber_capacity", 30000.0, above=0.0)
        self.ch_k = config.getfloat("chamber_loss", 8.0, above=0.0)
        self.he_p = config.getfloat("hotend_power", 60.0, above=0.0)
        self.he_c = config.getfloat("hotend_capacity", 10.0, above=0.0)
        self.he_k = config.getfloat("hotend_loss", 0.15, above=0.0)
        self.fan_f = config.getfloat("fan_cooling", 0.3, minval=0.0)
        self.z0 = config.getfloat("bed_influence_height", 25.0, above=0.0)
        self.noise = config.getfloat("noise", 0.05, minval=0.0)
        # Fast-forward the slow chamber node only, so a heat soak takes
        # minutes instead of an hour; bed and hotend stay real time.
        self.ch_speed = config.getfloat("chamber_speedup", 1.0, minval=1.0)
        # Thermistor lag behind the part it measures (first order), which
        # gives the heater loop the delay a real one has.
        self.lag = {
            "bed": config.getfloat("bed_sensor_lag", 8.0, minval=0.0),
            "hotend": config.getfloat("hotend_sensor_lag", 3.0, minval=0.0),
            "chamber": 0.0,
        }
        if not _STATE:
            _STATE["t"] = {n: self.amb for n in ("bed", "hotend", "chamber")}
            _STATE["s"] = dict(_STATE["t"])
        self.t = _STATE["t"]
        self.s = _STATE["s"]
        self.sensors = []
        self.heaters = {}
        self.toolhead = self.fan = None
        self.last = None
        self.timer = self.reactor.register_timer(self._step)
        self.printer.register_event_handler("klippy:connect", self._connect)
        pheaters = self.printer.load_object(config, "heaters")
        pheaters.add_sensor_factory(
            "sim_thermal", lambda cfg: SimSensor(cfg, self)
        )
        gcode = self.printer.lookup_object("gcode")
        gcode.register_command(
            "SIM_THERMAL", self.cmd_SIM_THERMAL, desc=self.cmd_SIM_THERMAL_help
        )

    def _connect(self):
        pheaters = self.printer.lookup_object("heaters")
        for s in self.sensors:
            if s.heater_name is not None:
                self.heaters[s.node] = pheaters.lookup_heater(s.heater_name)
        self.toolhead = self.printer.lookup_object("toolhead")
        self.fan = self.printer.lookup_object("fan", None)
        self.last = self.reactor.monotonic()
        self.reactor.update_timer(self.timer, self.reactor.NOW)

    def _pwm(self, node):
        if self.printer.is_shutdown():
            return 0.0  # a shut-down printer has its heaters off
        h = self.heaters.get(node)
        return h.last_pwm_value if h is not None else 0.0

    def _nozzle_height(self):
        try:
            return max(0.0, self.toolhead.get_position()[2])
        except Exception:
            return 0.0

    def _step(self, eventtime):
        dt = min(eventtime - self.last, 2.0)
        self.last = eventtime
        fan = 0.0
        if self.fan is not None:
            fan = self.fan.get_status(eventtime).get("speed", 0.0)
        t = self.t
        n = max(1, int(math.ceil(dt / 0.05)))
        h = dt / n
        for _ in range(n):
            loc = t["chamber"] + (t["bed"] - t["chamber"]) * math.exp(
                -self._nozzle_height() / self.z0
            )
            q_bed = self.bed_k * (t["bed"] - t["chamber"])
            q_he = self.he_k * (1.0 + self.fan_f * fan) * (t["hotend"] - loc)
            q_ch = self.ch_k * (t["chamber"] - self.amb)
            t["bed"] += h * (self.bed_p * self._pwm("bed") - q_bed) / self.bed_c
            t["hotend"] += h * (self.he_p * self._pwm("hotend") - q_he) / self.he_c
            t["chamber"] += (
                h * self.ch_speed * (q_bed + q_he - q_ch) / self.ch_c
            )
        for node, lag in self.lag.items():
            a = 1.0 if lag <= 0.0 else 1.0 - math.exp(-dt / lag)
            self.s[node] += a * (t[node] - self.s[node])
        for s in self.sensors:
            s.report(eventtime)
        return eventtime + STEP_TIME

    def read(self, node):
        return self.s[node] + random.gauss(0.0, self.noise)

    cmd_SIM_THERMAL_help = "Show or set the simulated temperatures"

    def cmd_SIM_THERMAL(self, gcmd):
        for node in ("bed", "hotend", "chamber"):
            v = gcmd.get_float(node.upper(), None)
            if v is not None:
                self.t[node] = self.s[node] = v
        self.amb = gcmd.get_float("AMBIENT", self.amb)
        self.ch_speed = gcmd.get_float("CHAMBER_SPEEDUP", self.ch_speed, minval=1.0)
        gcmd.respond_info(
            "sim_thermal: bed %.2f hotend %.2f chamber %.2f ambient %.1f"
            " (chamber speedup x%g, nozzle z %.1f)"
            % (
                self.t["bed"],
                self.t["hotend"],
                self.t["chamber"],
                self.amb,
                self.ch_speed,
                self._nozzle_height(),
            )
        )

    def get_status(self, eventtime):
        return {k: round(v, 2) for k, v in self.t.items()}


class SimSensor:
    def __init__(self, config, model):
        self.printer = config.get_printer()
        self.model = model
        name = config.get_name()
        default = {"extruder": "hotend", "heater_bed": "bed"}.get(
            name.split()[-1] if name.startswith("extruder") else name,
            "chamber",
        )
        self.node = config.getchoice(
            "sim_node",
            {"bed": "bed", "hotend": "hotend", "chamber": "chamber"},
            default,
        )
        is_heater = name.startswith("extruder") or name == "heater_bed" or (
            name.startswith("heater_generic")
        )
        self.heater_name = name if is_heater else None
        self.min_temp = self.max_temp = 0.0
        self.callback = None
        self.next_report = 0.0
        self.temp = model.amb
        model.sensors.append(self)

    def setup_minmax(self, min_temp, max_temp):
        self.min_temp, self.max_temp = min_temp, max_temp

    def setup_callback(self, cb):
        self.callback = cb

    def get_report_time_delta(self):
        return REPORT_TIME

    def report(self, eventtime):
        if eventtime < self.next_report or self.callback is None:
            return
        self.next_report = eventtime + REPORT_TIME
        self.temp = self.model.read(self.node)
        if not (self.min_temp <= self.temp <= self.max_temp):
            self.printer.invoke_shutdown(
                "sim_thermal %s temperature %.1f outside range %.1f:%.1f"
                % (self.node, self.temp, self.min_temp, self.max_temp)
            )
            return
        mcu = self.printer.lookup_object("mcu")
        self.callback(mcu.estimated_print_time(eventtime), self.temp)

    def get_status(self, eventtime):
        return {"temperature": round(self.temp, 2)}


def load_config(config):
    return SimThermal(config)
