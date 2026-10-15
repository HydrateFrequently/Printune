# Simulated accelerometer for the virtual printer (test only).
#
# It "measures" what a real accelerometer on the toolhead would see: the commanded
# toolhead acceleration (read back from the motion history) passed through one or two
# resonances per axis, plus a little noise. SHAPER_CALIBRATE, TEST_RESONANCES,
# MEASURE_AXES_NOISE, ACCELEROMETER_MEASURE and Shake&Tune then find the resonances
# set here.
#
# Belts: SIM_PLUCK BELT=A|B plucks a belt (a decaying tone at the belt's frequency,
# along that belt's diagonal). While Printune listens for plucks, the belt it asks for
# is plucked by itself, as many times as it asks. SIM_BELTS A=110 B=105 sets the belt
# tones; SIM_BELTS TURN_A=0.25 turns A's tensioner a quarter turn (+6 Hz on A, and
# +1.8 Hz on B: CoreXY belts tension each other).
#
# [sim_accel]
# x_modes: 52.0:0.08, 87:0.12:0.3   # freq Hz : damping ratio [: relative strength]
# y_modes: 41.0:0.10
# noise: 30                          # mm/s^2 rms
# belt_a: 96                         # Hz, starting belt tones
# belt_b: 91
# harmonic_a: 1.5                   # belt A shows mostly at twice its tone
# harmonic_b: 0.4
#
# [resonance_tester]
# accel_chip: sim_accel
import logging
import math
import random

import numpy as np

from . import adxl345

RATE = 3200.0
HZ_PER_TURN = 24.0      # belt tone change per full turn of a tensioner
COUPLING = 0.3          # share of that change the other belt sees
PLUCK_ACCEL = 1500.0    # mm/s^2 at the toolhead, first peak of a pluck
PLUCK_DECAY = 0.35      # s, belt ringing time constant


def parse_modes(text):
    modes = []
    for part in text.split(","):
        f = [float(v) for v in part.strip().split(":") if v.strip()]
        if f:
            modes.append((f[0], f[1] if len(f) > 1 else 0.1, f[2] if len(f) > 2 else 1.0))
    return modes


def base_excited(a_cmd, freq, zeta):
    """Acceleration of a mass on a spring-damper whose base follows a_cmd
    (bilinear-transform biquad of (2 z w s + w^2) / (s^2 + 2 z w s + w^2))."""
    w = 2 * math.pi * freq
    k = w / math.tan(w / (2 * RATE))          # prewarped
    a0 = k * k + 2 * zeta * w * k + w * w
    b0 = (2 * zeta * w * k + w * w) / a0
    b1 = (2 * w * w) / a0
    b2 = (w * w - 2 * zeta * w * k) / a0
    a1 = (2 * w * w - 2 * k * k) / a0
    a2 = (k * k - 2 * zeta * w * k + w * w) / a0
    out = np.empty_like(a_cmd)
    x1 = x2 = y1 = y2 = 0.0
    for i, x in enumerate(a_cmd.tolist()):
        y = b0 * x + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        out[i] = y
        x2, x1, y2, y1 = x1, x, y1, y
    return out


class SimClient(adxl345.AccelQueryHelper):
    def __init__(self, chip):
        adxl345.AccelQueryHelper.__init__(self, chip.printer)
        self.chip = chip
        self.moves = {}
        self.timer = chip.reactor.register_timer(self._pull, chip.reactor.NOW)
        chip.clients.append(self)
        # Printune listening for plucks: pluck the belt it asks for, once a second or so
        pt = chip.printer.lookup_object("printune", None)
        belt = getattr(pt, "belt_now", None)
        if belt in ("A", "B"):
            n = getattr(pt, "belt_plucks", 5)
            for i in range(n):
                chip.pluck(belt, self.request_start_time + 0.8 + 1.2 * i + random.uniform(-0.15, 0.15))

    def _pull(self, eventtime):
        mr = self.chip.printer.lookup_object("motion_report")
        moves, _ = mr.trapqs["toolhead"].extract_trapq(self.request_start_time - 1.0)
        for m in moves:
            self.moves[m.print_time] = (m.print_time, m.move_t, m.start_v, m.accel, m.x_r, m.y_r, m.z_r)
        return eventtime + 1.0

    def finish_measurements(self):
        adxl345.AccelQueryHelper.finish_measurements(self)
        self._pull(0.0)
        self.chip.reactor.unregister_timer(self.timer)
        if self in self.chip.clients:
            self.chip.clients.remove(self)
        t0, t1 = self.request_start_time, self.request_end_time
        n = max(2, int((t1 - t0) * RATE))
        t = t0 + np.arange(n) / RATE
        ax, ay, az = np.zeros(n), np.zeros(n), np.zeros(n)
        for pt, mt, v0, acc, xr, yr, zr in sorted(self.moves.values()):
            i0, i1 = np.searchsorted(t, [pt, pt + mt])
            if i1 <= i0 or not acc:
                continue
            ax[i0:i1] += acc * xr
            ay[i0:i1] += acc * yr
            az[i0:i1] += acc * zr
        c = self.chip
        out = []
        for a, modes in ((ax, c.x_modes), (ay, c.y_modes)):
            r = np.zeros(n)
            for f, z, s in modes:
                r += s * (base_excited(a, f, z) - a)
            out.append(a + r + np.random.normal(0.0, c.noise, n))
        out.append(az + 9806.65 + np.random.normal(0.0, c.noise, n))
        # belt plucks: the swinging belt pulls the carriage sideways at its tone, and
        # along the belt at twice it (the pull changes twice per swing; either can be
        # the stronger). The knock of the finger rings the frame.
        for pt, belt, freq, amp, h2 in c.plucks:
            if pt > t1 or pt + 6 * PLUCK_DECAY < t0:
                continue
            i0 = int(np.searchsorted(t, pt))
            tt = t[i0:] - pt
            env = np.exp(-tt / PLUCK_DECAY) * np.minimum(1.0, tt / 0.004)
            side = amp * env * np.sin(2 * math.pi * freq * tt)
            along = amp * h2 * env * env * np.sin(4 * math.pi * freq * tt)
            ux, uy = (0.71, 0.71) if belt == "A" else (0.71, -0.71)     # along the belt
            vx, vy = -uy, ux                                             # across it
            knock = np.zeros(min(n - i0, int(RATE)))      # the frame rings out within a second
            knock[:int(0.003 * RATE)] = 4.0 * amp
            for k, (u_, v_, modes) in enumerate(((ux, vx, c.x_modes), (uy, vy, c.y_modes))):
                frame = sum(m[2] * (base_excited(knock, m[0], m[1]) - knock) for m in modes)
                out[k][i0:] += u_ * along + 0.5 * v_ * side
                out[k][i0:i0 + len(knock)] += u_ * frame
            out[2][i0:] += 0.5 * side
        self.msgs = [{"data": list(zip(t.tolist(), out[0].tolist(), out[1].tolist(), out[2].tolist()))}]
        logging.info("sim_accel: %d samples over %.1f s from %d moves", n, t1 - t0, len(self.moves))


class SimAccel:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.name = config.get_name().split()[-1]
        self.x_modes = parse_modes(config.get("x_modes", "52.0:0.08, 87:0.12:0.3"))
        self.y_modes = parse_modes(config.get("y_modes", "41.0:0.10"))
        self.noise = config.getfloat("noise", 30.0, minval=0.0)
        self.belts = {"A": config.getfloat("belt_a", 96.0, above=0.0),
                      "B": config.getfloat("belt_b", 91.0, above=0.0)}
        self.plucks = []
        self.clients = []
        # how strongly each belt shows at twice its tone (differs per printer)
        self.h2 = {"A": config.getfloat("harmonic_a", 1.5, minval=0.0),
                   "B": config.getfloat("harmonic_b", 0.4, minval=0.0)}
        random.seed()
        adxl345.AccelCommandHelper(config, self)
        gcode = self.printer.lookup_object("gcode")
        gcode.register_command("SIM_PLUCK", self.cmd_SIM_PLUCK,
                               desc="Virtual printer: pluck belt A or B (BELT=A|B)")
        gcode.register_command("SIM_BELTS", self.cmd_SIM_BELTS,
                               desc="Virtual printer: set belt tones (A= B=) or turn a tensioner (TURN_A= TURN_B=)")

    def start_internal_client(self):
        return SimClient(self)

    # ADXL345 register access, for ACCELEROMETER_DEBUG_READ/WRITE
    def read_reg(self, reg):
        return 0

    def set_reg(self, reg, val, minclock=0):
        pass

    def pluck(self, belt, print_time):
        # a person never plucks exactly the same way twice
        freq = self.belts[belt] + random.uniform(-0.3, 0.3)
        amp = PLUCK_ACCEL * random.uniform(0.6, 1.3)
        self.plucks.append((print_time, belt, freq, amp, self.h2[belt]))
        self.plucks = [p for p in self.plucks if p[0] > print_time - 120.0]

    def cmd_SIM_PLUCK(self, gcmd):
        belt = gcmd.get("BELT", "A").upper()
        if belt not in self.belts:
            raise gcmd.error("BELT must be A or B")
        mcu = self.printer.lookup_object("mcu")
        self.pluck(belt, mcu.estimated_print_time(self.reactor.monotonic()))
        gcmd.respond_info("plucked belt %s (%.1f Hz)" % (belt, self.belts[belt]))

    def cmd_SIM_BELTS(self, gcmd):
        for b, o in (("A", "B"), ("B", "A")):
            v = gcmd.get_float(b, None, above=0.0)
            if v is not None:
                self.belts[b] = v
        for b, o in (("A", "B"), ("B", "A")):
            turn = gcmd.get_float("TURN_" + b, 0.0)
            self.belts[b] += HZ_PER_TURN * turn
            self.belts[o] += COUPLING * HZ_PER_TURN * turn
        gcmd.respond_info("belt A %.1f Hz, belt B %.1f Hz" % (self.belts["A"], self.belts["B"]))

    def get_status(self, eventtime):
        return {"belt_a": round(self.belts["A"], 1), "belt_b": round(self.belts["B"], 1)}


def load_config(config):
    return SimAccel(config)


def load_config_prefix(config):
    return SimAccel(config)
