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
    """events: (time, kind, freq, amp, ratio2[, tau]) with kind pluck | hard | harm |
    tap | ring. harm: amp is (F, 2F, 4F). ring: one decaying tone (a frame mode, a click)."""
    t = np.arange(int(secs * FS)) / FS
    ax, ay, az = (rng.normal(0, noise, len(t)) for _ in range(3))
    if fan:
        for a in (ax, ay, az):
            a += 3 * noise * np.sin(2 * np.pi * fan * t)
    for ev in events:
        t0, kind, fr, amp, r2 = ev[:5]
        tau = ev[5] if len(ev) > 5 else 0.3
        tt = t - t0
        on = tt >= 0
        env = np.where(on, np.exp(-np.clip(tt, 0, None) / tau), 0.0)
        if kind == "hard":   # a pluck with a screwdriver: a click, and overtones up to 4F
            ax[np.argmax(on)] += 20 * amp
            for h, g in ((1, 0.3), (2, r2 * 0.7), (3, 0.5), (4, 0.4)):
                ax += g * amp * env ** h * np.sin(2 * np.pi * h * fr * tt)
                ay += g * amp * env ** h * np.sin(2 * np.pi * h * fr * tt + 1.0)
        elif kind == "harm":   # F, 2F and 4F with set amplitudes
            for h, g in zip((1, 2, 4), amp):
                for a, w in zip((ax, ay, az), (0.5, 0.5, 0.4)):
                    a += w * g * env ** min(h, 2) * np.sin(2 * np.pi * h * fr * tt)
        elif kind == "ring":
            for a, w in zip((ax, ay, az), (0.6, 0.6, 0.3)):
                a += w * amp * env * np.sin(2 * np.pi * fr * tt)
        elif kind == "pluck":
            side = amp * env * np.sin(2 * np.pi * fr * tt)
            along = amp * r2 * env * env * np.sin(4 * np.pi * fr * tt)
            ax += 0.7 * along + 0.3 * side
            ay += 0.7 * along - 0.3 * side
            az += 0.4 * side
        else:   # the toolhead ringing by itself after a tap
            ring = amp * np.where(on, np.exp(-np.clip(tt, 0, None) / 0.15), 0.0) * np.sin(2 * np.pi * fr * tt)
            if r2:   # the belts attached to the toolhead ring a little too: (F, amp F, amp 2F)
                bf, b1, b2 = r2
                ring = ring + b1 * env * np.sin(2 * np.pi * bf * tt)
                ring = ring + b2 * env * env * np.sin(4 * np.pi * bf * tt)
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
    check("plucks 86-94 Hz: inconsistent", r["verdict"] in ("spread", "spans") and r["freq"] is None, r)
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
    octaves(q)
    knocks(q)
    series_rules(q)


def near(r, fr, tol=0.4):
    return r["verdict"] == "good" and r["freq"] is not None and abs(r["freq"] - fr) < tol


def octaves(q):
    """The tone, not twice it or half it."""
    # a frame mode near F/2 ringing with each pluck, 2F weak (F-strong sensor)
    for tgt in (110.0, None):
        ev = series([91.0] * 5, r2=0.12, extra=[(1.0 + 1.2 * i, "ring", 45.6, 1500.0, 0, 0.06) for i in range(5)])
        r = belts.analyse(*rec(ev), quiet=q, target=tgt)
        check("frame mode at F/2, target %s" % tgt, near(r, 91.0), r)
    # a 2F sensor (2F 25 dB over F) with 4F stronger than F
    for tgt in (110.0, 150.0, None):
        ev = [(1.0 + 1.2 * i, "harm", 91.0, (60.0, 1070.0, 250.0), 0) for i in range(5)]
        r = belts.analyse(*rec(ev), quiet=q, target=tgt)
        check("4F over F on a 2F sensor, target %s" % tgt, near(r, 91.0), r)
    # a loose belt in the rough phase, heard mostly at 2F: read at its tone, not 2x it
    ev = [(1.0 + 1.2 * i, "harm", 62.0, (80.0, 1070.0, 0.0), 0) for i in range(5)]
    r = belts.analyse(*rec(ev), quiet=q, target=110.0)
    check("loose belt 62 Hz, 2F sensor, target 110", near(r, 62.0), r)
    # the last reading breaks a tie the ringing can't settle (no timing: pluck_freq alone)
    f = np.fft.rfftfreq(1 << 15, 1.0 / FS)
    df = f[1] - f[0]
    p = np.ones(len(f))
    for fc, lv in ((45.5, 10.0), (91.0, 10.0 ** 3.5), (182.0, 10.0)):
        p[np.abs(f - fc) < 0.3] = lv
    fr = belts.pluck_freq(f, p, np.ones(len(f)), df, 25.0, 700.0, prev=91.0)[0]
    ok = fr is not None and abs(fr - 91.0) < 0.3
    print("%-4s %-46s %s" % ("ok" if ok else "FAIL", "tie: the last reading (91) decides", fr))
    if not ok:
        FAILS.append("prev tie")


def knocks(q):
    """A knock on the toolhead is not a belt, with or without a tap learned."""
    # a tool tap on the toolhead (104 Hz, loud) among plucks, nothing learned
    ev = series([88.0] * 4, extra=[(2.8, "tap", 104.0, 8000.0, 0)])
    r = belts.analyse(*rec(ev), quiet=q, target=110.0, expect=4)
    check("loud knock at 104 Hz, no tap learned", near(r, 88.0) and r["used"] == 4
          and any(p["why"] == "a knock, not a belt" for p in r["plucks"]), r)
    # a tap that also rings the belt (2F strong, a 2F sensor): only the toolhead is learned
    tr = belts.analyse_tap(*rec([(1.0, "tap", 104.5, 3000.0, (88.2, 150.0, 450.0))], 3.0), quiet=q)
    ok = len(tr["modes"]) == 1 and abs(tr["modes"][0] - 104.5) < 0.5
    print("%-4s %-46s %s" % ("ok" if ok else "FAIL", "tap rings the belt too: learns 104.5 only", tr))
    if not ok:
        FAILS.append("tap rings belt")
    ev = [(1.0 + 1.2 * i, "harm", 88.2, (60.0, 1070.0, 0.0), 0) for i in range(5)]
    r = belts.analyse(*rec(ev), quiet=q, target=110.0, avoid=tr["modes"])
    check("then that belt's plucks", near(r, 88.2), r)
    # a belt whose 2F sits on a learned toolhead mode: the guard is for F only
    ev = [(1.0 + 1.2 * i, "harm", 81.8, (60.0, 1070.0, 0.0), 0) for i in range(5)]
    r = belts.analyse(*rec(ev), quiet=q, target=110.0, avoid=[104.5, 163.6])
    check("belt 2F on a learned mode (163.6)", near(r, 81.8), r)
    # a bump on the frame before the plucks: PLUCKS=3 still gets its 3 plucks
    ev = [(0.4, "ring", 41.0, 3000.0, 0, 0.1)] + [(1.2 + 1.2 * i, "pluck", fr, 1000.0, 1.0)
                                                  for i, fr in enumerate((88.2, 88.0, 88.3))]
    r = belts.analyse(*rec(ev, 6.0), quiet=q, target=110.0, expect=3)
    check("bump before 3 plucks, expect=3", near(r, 88.2) and r["used"] == 3, r)


def series_rules(q):
    """Which plucks of a series count."""
    # a stray removed, and the rest checked again: still too far apart
    ev = [(1.0 + 1.2 * i, "pluck", fr, 1000.0, 1.0) for i, fr in enumerate((88.0, 88.0, 88.8, 89.7, 92.0))]
    r = belts.analyse(*rec(ev), quiet=q, target=110.0)
    check("stray out, the rest checked again", r["freq"] is None and r["verdict"] == "spread", r)
    # plucks 1 s apart that ring long, no quiet recording and hardly a gap before them
    ev = [(0.3 + 1.0 * i, "pluck", 88.2, 600.0, 0.5, 1.0) for i in range(5)]
    r = belts.analyse(*rec(ev, 6.5), target=110.0)
    check("long ringing 1 s apart, no quiet", near(r, 88.2) and r["used"] == 5, r)
    # no quiet recording and a fan: the fan stays in the noise, not taken for a belt
    r = belts.analyse(*rec(series([91.0] * 5, start=0.3), fan=137.0), target=110.0)
    check("fan at 137 Hz, no quiet", near(r, 91.0), r)
    # a pluck 22 dB below the loudest is still found and counted
    r = belts.analyse(*rec(series([88.0] * 5, amps=[3000, 3000, 240, 3000, 3000])), quiet=q, target=110.0)
    check("one pluck 22 dB softer", near(r, 88.0) and r["heard"] == 5 and r["used"] == 5, r)
    # like the blind test: good plucks mixed with other stretches of belt (72, 60 Hz),
    # two knocks on the toolhead and a loud 439 Hz click: no reading
    kinds = [("pluck", 90.5), ("pluck", 72.0), ("tap", 104.0), ("pluck", 60.0), ("pluck", 90.5),
             ("ring", 439.0), ("pluck", 72.0), ("pluck", 90.5), ("tap", 104.0), ("pluck", 60.0)]
    ev = [(1.0 + 1.2 * i, k, fr, 6000.0 if k == "tap" else 1500.0, 0 if k == "tap" else 1.0,
           0.05 if k == "ring" else 0.3)
          for i, (k, fr) in enumerate(kinds)]
    r = belts.analyse(*rec(ev, 14.0), quiet=q, target=110.0)
    why = [p["why"] for p in r["plucks"]]
    check("blind test mix: no reading, spans", r["freq"] is None and r["verdict"] == "spans"
          and why.count("a knock, not a belt") == 2
          and not any(p["ok"] and p["freq"] > 200 for p in r["plucks"]), r)
    # most plucks agree: the ones on another stretch are left out
    kinds = [90.5, 72.0, 90.5, 90.5, 60.0, 90.5]
    ev = [(1.0 + 1.2 * i, "pluck", fr, 1500.0, 1.0) for i, fr in enumerate(kinds)]
    r = belts.analyse(*rec(ev), quiet=q, target=110.0)
    check("4 of 6 agree: 72 and 60 left out", near(r, 90.5) and r["used"] == 4
          and sum(p["why"].startswith("rang at") for p in r["plucks"]) == 2, r)
    # all plucks on the same wrong stretch: a reading all the same (the judge can't know)
    r = belts.analyse(*rec(series([72.0] * 5)), quiet=q, target=110.0)
    check("5 plucks at 72 Hz: a reading", near(r, 72.0), r)


def tensioning():
    """plan(): just which way, and done only from 5 good plucks each within tol."""
    cases = [((105.0, 110.1, 5, 5, None), "Tighten the left (A) belt", False),
             ((110.3, 112.0, 5, 5, None), "Loosen the left (A) belt", False),
             ((110.3, 112.0, 5, 5, "A"), "Loosen the right (B) belt", False),          # taking turns
             ((110.3, 112.0, 5, 5, "B"), "Loosen the left (A) belt", False),
             ((110.1, 112.0, 5, 5, "B"), "Loosen the right (B) belt", False),          # A is good already
             ((110.2, 109.8, 5, 5, None), "Both belts good", True),
             ((110.2, 109.8, 5, 4, None), "not from 5 good plucks", False)]
    for (fa, fb, ua, ub, last), want, done in cases:
        b = {"target": 110.0, "tol": 0.25, "plucks": 5, "last_belt": last,
             "A": {"freq": fa, "used": ua}, "B": {"freq": fb, "used": ub}}
        adv, upd = belts.plan(b)
        # "good" and "prev" in the belt state belong to the plugin (last readings)
        ok = want in adv and upd.get("done") == done and not {"good", "prev"} & set(upd)
        print("%-4s %-46s %s" % ("ok" if ok else "FAIL", "plan %s / %s" % (fa, fb), adv))
        if not ok:
            FAILS.append("plan %s/%s" % (fa, fb))


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
        def calm(a, b):
            """(t, ax, ay, az) from a to b, and the stretch before its first event as quiet."""
            seg = cut(a, b)
            fs = (len(seg[0]) - 1) / (seg[0][-1] - seg[0][0])
            on = belts.find_plucks(seg[1:], fs, belts.SEARCH[0], 0.45 * fs / 2.0, None)
            n = (on[0] - int(0.05 * fs)) if on else 0
            return seg, ([x[:n] for x in seg[1:]] if n > int(0.5 * fs) else None)
        seg, q = calm(2.3, 5.5)
        tap = belts.analyse_tap(*seg, quiet=q)
        avoid = tap["modes"]
        seg, q = calm(5.5, 19.5)
        right = belts.analyse(*seg, quiet=q, target=110.0, avoid=avoid)
        seg, q = calm(23.5, 37.0)
        left = belts.analyse(*seg, quiet=q, target=110.0, avoid=avoid, other=right["freq"])
        print(name, "tap", tap)
        check(name + " right 90.9", right["verdict"] == "good" and abs(right["freq"] - 90.9) < 0.6, right)
        check(name + " left 88.2", left["verdict"] == "good" and abs(left["freq"] - 88.2) < 0.6, left)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", help="folder with the Neb370 plucks2 recordings")
    a = ap.parse_args()
    synthetic()
    tensioning()
    if a.real:
        real(a.real)
    print("FAILED: " + ", ".join(FAILS) if FAILS else "all passed")
    sys.exit(1 if FAILS else 0)
