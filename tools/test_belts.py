#!/usr/bin/env python3
# Printune: tests for the belt pluck judge (printune_belts.analyse / analyse_tap).
#
# Synthetic recordings cover what differs between printers (which of the tone and
# twice the tone is stronger, the toolhead ringing by itself, weak plucks, the other
# belt ringing along). Real recordings are checked too when they are present:
#   tools/test_belts.py [--real DIR]   DIR holds adxl345-plucks2.csv and
#                                      beacon_accel-beacon-plucks2.csv (Neb370, 2026-10-15)
#
# Copyright (C) 2026  Printune contributors
# This file may be distributed under the terms of the GNU GPLv3 license.
import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import printune_belts as belts  # noqa: E402

FS = 3200.0
rng = np.random.default_rng(1)


def rec(events, secs=8.0, noise=30.0, fan=None):
    """events: (time, kind, freq, amp, ratio2) with kind pluck | tap."""
    t = np.arange(int(secs * FS)) / FS
    ax, ay, az = (rng.normal(0, noise, len(t)) for _ in range(3))
    if fan:
        for a in (ax, ay, az):
            a += 3 * noise * np.sin(2 * np.pi * fan * t)
    for t0, kind, fr, amp, r2 in events:
        tt = t - t0
        on = tt >= 0
        env = np.where(on, np.exp(-np.clip(tt, 0, None) / 0.3), 0.0)
        if kind == "hard":   # a pluck with a screwdriver: a click, and overtones up to 4F
            ax[np.argmax(on)] += 20 * amp
            for h, g in ((1, 0.3), (2, r2 * 0.7), (3, 0.5), (4, 0.4)):
                ax += g * amp * env ** h * np.sin(2 * np.pi * h * fr * tt)
                ay += g * amp * env ** h * np.sin(2 * np.pi * h * fr * tt + 1.0)
        elif kind == "pluck":
            side = amp * env * np.sin(2 * np.pi * fr * tt)
            along = amp * r2 * env * env * np.sin(4 * np.pi * fr * tt)
            ax += 0.7 * along + 0.3 * side
            ay += 0.7 * along - 0.3 * side
            az += 0.4 * side
        else:   # the toolhead ringing by itself after a tap
            ring = amp * np.where(on, np.exp(-np.clip(tt, 0, None) / 0.15), 0.0) * np.sin(2 * np.pi * fr * tt)
            ax += ring
            az += 0.5 * ring
    return t, ax, ay, az


def quiet(noise=30.0, fan=None):
    return rec([], 1.5, noise, fan)[1:]


def series(freqs, amps=None, r2=3.0, start=1.0, extra=()):
    amps = amps or [1500.0] * len(freqs)
    ev = [(start + 1.2 * i, "pluck", f + rng.uniform(-0.15, 0.15), a, r2)
          for i, (f, a) in enumerate(zip(freqs, amps))]
    return sorted(ev + list(extra))


FAILS = []


def check(name, cond, res):
    print("%-4s %-46s %s" % ("ok" if cond else "FAIL", name,
                             {k: res[k] for k in ("freq", "verdict", "used", "heard", "note")}))
    if not cond:
        FAILS.append(name)
        for p in res["plucks"]:
            print("       ", p)


def synthetic():
    q = quiet()
    # twice the tone much stronger (like a Beacon), or much weaker (a stiffer mount)
    for r2, label in ((18.0, "2F 25 dB over F"), (1.8, "2F 5 dB over F"), (0.3, "F 10 dB over 2F")):
        r = belts.analyse(*rec(series([91.0] * 5, r2=r2)), quiet=q)
        check("5 good plucks, " + label, r["verdict"] == "good" and abs(r["freq"] - 91.0) < 0.4, r)
    # a pluck that misses the belt (too weak) is thrown out, the rest still count
    r = belts.analyse(*rec(series([88.0] * 5, amps=[1500, 40, 1500, 1500, 30])), quiet=q)
    check("2 of 5 too weak: 3 counted", r["verdict"] == "good" and r["used"] == 3
          and abs(r["freq"] - 88.0) < 0.4, r)
    r = belts.analyse(*rec(series([88.0] * 5, amps=[1500, 40, 30, 1500, 30])), quiet=q)
    check("3 of 5 too weak: pluck again", r["verdict"] == "again" and r["freq"] is None, r)
    # the toolhead's own ringing (a finger knocking the toolhead) is not a belt
    tap = [(3.0, "tap", 104.5, 3000.0, 0.0), (5.5, "tap", 104.5, 3000.0, 0.0)]
    r = belts.analyse(*rec(series([88.0] * 3, start=1.0, extra=tap) + [(6.6, "pluck", 88.1, 1500, 3.0)]),
                      quiet=q, avoid=[104.5])
    check("taps among plucks, tap learned", r["verdict"] == "good" and abs(r["freq"] - 88.0) < 0.4, r)
    # a belt tuned right onto the toolhead's own ringing can still be read (2F is there)
    r = belts.analyse(*rec(series([104.5] * 5, r2=3.0)), quiet=q, avoid=[104.5])
    check("belt at the tap frequency, 2F strong", r["verdict"] == "good" and abs(r["freq"] - 104.5) < 0.4, r)
    # plucked with a screwdriver: a sharp click and overtones
    ev = [(1.0 + 1.2 * i, "hard", 91.0 + rng.uniform(-0.15, 0.15), 1500.0, 3.0) for i in range(5)]
    r = belts.analyse(*rec(ev), quiet=q)
    check("screwdriver plucks (click, 3F, 4F)", r["verdict"] == "good" and abs(r["freq"] - 91.0) < 0.4, r)
    # the other belt ringing along / plucked by mistake
    r = belts.analyse(*rec(series([88.0, 88.0, 91.0, 88.0, 88.0])), quiet=q, other=91.0)
    check("one pluck on the other belt", r["verdict"] == "good" and r["used"] == 4
          and any(p["why"] == "the other belt was ringing along" for p in r["plucks"]), r)
    # plucks that disagree: no reading
    r = belts.analyse(*rec(series([86.0, 88.0, 90.0, 92.0, 94.0])), quiet=q)
    check("plucks 86-94 Hz: inconsistent", r["verdict"] == "spread" and r["freq"] is None, r)
    # a steady fan line between the tone and twice it
    qf = quiet(fan=137.0)
    r = belts.analyse(*rec(series([91.0] * 5), fan=137.0), quiet=qf)
    check("fan at 137 Hz", r["verdict"] == "good" and abs(r["freq"] - 91.0) < 0.4, r)
    # nothing plucked
    r = belts.analyse(*rec([]), quiet=q)
    check("no plucks", r["freq"] is None and r["verdict"] in ("none", "again"), r)
    # tap learning
    tr = belts.analyse_tap(*rec([(1.0, "tap", 104.5, 3000.0, 0.0)], 3.0), quiet=q)
    ok = len(tr["modes"]) == 1 and abs(tr["modes"][0] - 104.5) < 0.5
    # a hard tap (screwdriver) brings out a second toolhead resonance: both learned
    tr2 = belts.analyse_tap(*rec([(1.0, "tap", 104.5, 3000.0, 0.0), (1.0, "tap", 163.0, 1500.0, 0.0)], 3.0),
                            quiet=q)
    ok2 = sorted(round(m) for m in tr2["modes"]) == [104, 163]
    print("%-4s %-46s %s" % ("ok" if ok2 else "FAIL", "hard tap learns 104.5 and 163 Hz", tr2))
    if not ok2:
        FAILS.append("hard tap")
    print("%-4s %-46s %s" % ("ok" if ok else "FAIL", "tap learns 104.5 Hz", tr))
    if not ok:
        FAILS.append("tap")


def load(path):
    d = np.loadtxt(path, delimiter=",", comments="#")
    return d[:, 0], d[:, 1], d[:, 2], d[:, 3]


def real(d):
    """Neb370 plucks2: right belt plucks then left belt plucks, a tap before each.
    Microphone reference: right 90.9 Hz, left 88.2 Hz."""
    for name in ("adxl345-plucks2.csv", "beacon_accel-beacon-plucks2.csv"):
        path = os.path.join(d, name)
        if not os.path.exists(path):
            print("skip", path)
            continue
        t, ax, ay, az = load(path)
        t = t - t[0]
        # tap at 2.81 s of the audio; audio 0 = first sample + 33.68 s
        off = 33.68
        cut = lambda a, b: [x[(t >= a + off) & (t < b + off)] for x in (t, ax, ay, az)]
        tap = belts.analyse_tap(*cut(2.3, 5.5))
        avoid = tap["modes"]
        right = belts.analyse(*cut(5.5, 19.5), avoid=avoid)
        left = belts.analyse(*cut(23.5, 37.0), avoid=avoid, other=right["freq"])
        print(name, "tap", tap)
        check(name + " right 90.9", right["verdict"] == "good" and abs(right["freq"] - 90.9) < 0.6, right)
        check(name + " left 88.2", left["verdict"] == "good" and abs(left["freq"] - 88.2) < 0.6, left)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", help="folder with the Neb370 plucks2 recordings")
    a = ap.parse_args()
    synthetic()
    if a.real:
        real(a.real)
    print("FAILED: " + ", ".join(FAILS) if FAILS else "all passed")
    sys.exit(1 if FAILS else 0)
