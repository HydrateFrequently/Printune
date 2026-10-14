# Fake accelerometer for the virtual printer (test only).
#
# It "measures" what a real accelerometer on the toolhead would see: the commanded
# toolhead acceleration (read back from the motion history) passed through one or two
# resonances per axis, plus a little noise. SHAPER_CALIBRATE, TEST_RESONANCES and
# MEASURE_AXES_NOISE then find the resonances set here.
#
# [fake_accel]
# x_modes: 52.0:0.08, 87:0.12:0.3   # freq Hz : damping ratio [: relative strength]
# y_modes: 41.0:0.10
# noise: 30                          # mm/s^2 rms
#
# [resonance_tester]
# accel_chip: fake_accel
import logging
import math
import random

import numpy as np

from . import adxl345

RATE = 3200.0


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


class FakeClient(adxl345.AccelQueryHelper):
    def __init__(self, chip):
        adxl345.AccelQueryHelper.__init__(self, chip.printer)
        self.chip = chip
        self.moves = {}
        self.timer = chip.reactor.register_timer(self._pull, chip.reactor.NOW)

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
        self.msgs = [{"data": list(zip(t.tolist(), out[0].tolist(), out[1].tolist(), out[2].tolist()))}]
        logging.info("fake_accel: %d samples over %.1f s from %d moves", n, t1 - t0, len(self.moves))


class FakeAccel:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.name = config.get_name().split()[-1]
        self.x_modes = parse_modes(config.get("x_modes", "52.0:0.08, 87:0.12:0.3"))
        self.y_modes = parse_modes(config.get("y_modes", "41.0:0.10"))
        self.noise = config.getfloat("noise", 30.0, minval=0.0)
        random.seed()

    def start_internal_client(self):
        return FakeClient(self)


def load_config(config):
    return FakeAccel(config)


def load_config_prefix(config):
    return FakeAccel(config)
