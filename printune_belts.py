# Printune: belt tension from plucks, measured with the toolhead accelerometer.
#
# A plucked belt span rings at f = sqrt(T / mu) / (2 L). The belt pulls on the
# carriage at that frequency, so the accelerometer on the toolhead picks it up. This
# finds each pluck in a recording, skips the first few milliseconds (the frame rings
# too, but dies out much sooner than the belt), and takes the peak of the spectrum.
# Spectral lines that are there without a pluck (fans, motors) are divided out.
#
# Copyright (C) 2026  Printune contributors
# This file may be distributed under the terms of the GNU GPLv3 license.
import math

import numpy as np

BAND = (30.0, 500.0)      # Hz, what can be a belt
SKIP = 0.03               # s after the onset: let the frame ring out
WINDOW = 0.6              # s of ringing used per pluck
DEAD = 0.45               # s: a new pluck can't start sooner than this


def _smooth(x, n):
    if n <= 1:
        return x
    k = np.ones(n) / n
    return np.convolve(x, k, mode="same")


def _spectrum(seg, fs, nfft):
    w = np.hanning(len(seg[0]))
    p = 0
    for s in seg:
        s = (s - s.mean()) * w
        p = p + np.abs(np.fft.rfft(s, nfft)) ** 2
    return np.fft.rfftfreq(nfft, 1.0 / fs), p


def steady_lines(axes, fs, quiet=False):
    """Mask function for spectral lines that are there all the time (fans, motors).
    quiet: the recording has no plucks in it (a background recording)."""
    seg = 1 << int(math.ceil(math.log2((0.25 if quiet else 0.5) * fs)))
    n = len(axes[0])
    if n < 4 * seg:
        return None
    specs = []
    for k in range(0, n - seg, seg // 2):
        specs.append(_spectrum([a[k:k + seg] for a in axes], fs, seg)[1])
    # a line is there in (nearly) every segment; plucks are not. Plucks a second
    # apart ring through most segments, so a quiet recording is the better guide.
    med = np.percentile(np.array(specs), 50 if quiet else 20, axis=0)
    f = np.fft.rfftfreq(seg, 1.0 / fs)
    floor = _smooth(med, int(20.0 / (f[1] - f[0])) | 1)
    hot = _smooth((med > 8.0 * floor).astype(float), 3) > 0

    def mask(freqs):
        return np.interp(freqs, f, hot.astype(float)) > 0.0
    return mask


def find_plucks(axes, fs, lo, hi, mask):
    """Onset indices of the plucks: the energy in the belt band jumps up."""
    xb = []
    for a in axes:
        spec = np.fft.rfft(a - a.mean())
        f = np.fft.rfftfreq(len(a), 1.0 / fs)
        keep = (f >= lo) & (f <= hi)
        if mask is not None:
            keep &= ~mask(f)
        xb.append(np.fft.irfft(np.where(keep, spec, 0), len(a)))
    env = _smooth(np.sqrt(sum(x * x for x in xb)), max(1, int(0.01 * fs)))
    base = float(np.median(env)) or 1e-9
    thr = max(4.0 * base, 0.15 * float(env.max()))
    # an onset: above the threshold AND a sharp rise (not the tail of the last pluck)
    onsets, i, n = [], 0, len(env)
    dead, back = int(DEAD * fs), max(1, int(0.04 * fs))
    while i < n:
        if env[i] > thr and env[i] > 3.0 * env[max(0, i - back)]:
            j0 = max(0, i - back)
            j1 = min(n - 1, i + back)
            onsets.append(j0 + int(np.argmax(np.diff(env[j0:j1 + 1]))))
            i += dead
        else:
            i += 1
    return onsets


def analyse(t, ax, ay, az, target=None, expect=None, quiet=None):
    """Belt frequency from a recording with several plucks. quiet: (ax, ay, az) of a
    short recording without plucks, to tell fans and motors from the belt.
    Returns dict: freq (average), plucks (each pluck's peak), used (count), note."""
    t = np.asarray(t, float)
    axes = [np.asarray(a, float) for a in (ax, ay, az)]
    if len(t) < 100:
        return {"freq": None, "plucks": [], "used": 0, "note": "no data from the accelerometer"}
    fs = (len(t) - 1) / (t[-1] - t[0])
    lo, hi = BAND[0], min(BAND[1], 0.45 * fs)
    if target:
        lo, hi = max(lo, 0.45 * target), min(hi, 1.7 * target)
    mask = None
    if quiet is not None and len(quiet[0]) > 100:
        mask = steady_lines([np.asarray(a, float) for a in quiet], fs, quiet=True)
    if mask is None:
        mask = steady_lines(axes, fs)
    onsets = find_plucks(axes, fs, lo, hi, mask)
    if not onsets:
        return {"freq": None, "plucks": [], "used": 0, "note": "no pluck heard"}
    nfft = 1 << int(math.ceil(math.log2(8 * fs)))       # ~0.125 Hz bins after zero padding
    win = int(WINDOW * fs)
    freqs = []
    for j, on in enumerate(onsets):
        a0 = on + int(SKIP * fs)
        a1 = min(a0 + win, len(t), (onsets[j + 1] - int(0.01 * fs)) if j + 1 < len(onsets) else len(t))
        if a1 - a0 < int(0.15 * fs):
            continue
        f, p = _spectrum([a[a0:a1] for a in axes], fs, nfft)
        sel = (f >= lo) & (f <= hi)
        if mask is not None:
            sel &= ~mask(f)
        if not sel.any():
            continue
        fs_, ps = f[sel], p[sel]
        k = int(np.argmax(ps))
        # a loose belt: its 2nd harmonic may sit near the target; prefer the fundamental
        half = (fs_ > 0.47 * fs_[k]) & (fs_ < 0.53 * fs_[k])
        if half.any() and ps[half].max() > 0.3 * ps[k]:
            k = int(np.flatnonzero(half)[np.argmax(ps[half])])
        if 0 < k < len(ps) - 1 and fs_[k + 1] - fs_[k - 1] < 3 * (f[1] - f[0]):
            y0, y1, y2 = np.log(ps[k - 1:k + 2] + 1e-30)     # parabolic peak on the log spectrum
            d = 0.5 * (y0 - y2) / (y0 - 2 * y1 + y2) if (y0 - 2 * y1 + y2) else 0.0
            freqs.append(float(fs_[k] + d * (f[1] - f[0])))
        else:
            freqs.append(float(fs_[k]))
    if not freqs:
        return {"freq": None, "plucks": [], "used": 0, "note": "plucks too short or too close together"}
    if expect:
        freqs = freqs[:expect]
    med = float(np.median(freqs))
    good = [f for f in freqs if abs(f - med) <= max(3.0, 0.05 * med)]
    note = ""
    if len(good) < len(freqs):
        note = "%d pluck(s) left out (far from the others)" % (len(freqs) - len(good))
    return {"freq": round(float(np.mean(good)), 1), "plucks": [round(f, 1) for f in freqs],
            "used": len(good), "note": note}


def advice(freq, target, tol=2.0):
    if freq is None or not target:
        return ""
    d = freq - target
    if abs(d) <= tol:
        return "on target"
    return "%.0f Hz %s, %s" % (abs(d), "low" if d < 0 else "high", "tighten" if d < 0 else "loosen")


_EIGHTHS = {1: "1/8", 2: "1/4", 3: "3/8", 4: "1/2", 5: "5/8", 6: "3/4", 7: "7/8", 8: "1"}


def _turn_text(t):
    n = int(round(abs(t) * 8))
    if n < 1:
        return "a tiny bit (less than 1/8 turn)"
    return "about %s turn" % _EIGHTHS[min(n, 8)]


def plan(b):
    """Next instruction for tensioning a CoreXY pair. b is the belt state (target, tol,
    phase, A, B and what was learned so far); returns (advice, updates for b).

    Coarse: both belts to about 90-100 Hz (for a 110 Hz target). Fine: one belt at a
    time, re-measuring both after each turn. How many Hz a turn gives, and how much of
    that the other belt sees, differ per printer: they are learned from the turns asked
    for and the change measured, so the turns asked for shrink as the belts get close.
    """
    tgt, tol, phase = b["target"], b["tol"], b.get("phase", "coarse")
    fa = (b.get("A") or {}).get("freq")
    fb = (b.get("B") or {}).get("freq")
    upd = {}
    if fa is None or fb is None:
        return "", upd
    k, c = b.get("hz_per_turn"), b.get("coupling", 0.3)
    last = b.get("last")
    if last and last.get("A") is not None:
        own, other = (fa, fb) if last["belt"] == "A" else (fb, fa)
        p_own, p_other = (last["A"], last["B"]) if last["belt"] == "A" else (last["B"], last["A"])
        d_own, d_other = own - p_own, other - p_other
        if last["turns"] * d_own > 0 and abs(d_own) > 0.5:
            k_new = min(100.0, max(2.0, d_own / last["turns"]))
            k = k_new if k is None else 0.5 * (k + k_new)
            c = 0.5 * (c + min(0.8, max(0.0, d_other / d_own)))
            upd.update(hz_per_turn=round(k, 2), coupling=round(c, 3))
    upd["last"] = None
    lo, hi = tgt - 20, tgt - 10
    if phase == "coarse":
        if min(fa, fb) < lo - 1:
            which = "both belts" if max(fa, fb) < lo - 1 else ("belt A" if fa < fb else "belt B")
            upd["phase"] = "coarse"
            return ("Rough first: tighten %s until each reads about %.0f-%.0f Hz, then measure both again"
                    % (which, lo, hi)), upd
        phase = upd["phase"] = "fine"
    da, db = fa - tgt, fb - tgt
    if abs(da) <= tol and abs(db) <= tol and abs(fa - fb) <= tol:
        upd["done"] = True
        return "Both belts are on target and match. Done.", upd
    upd["done"] = False
    if k is None:
        # nothing learned yet: a small first step
        belt, d = ("A", da) if abs(da) >= abs(db) else ("B", db)
        t = (-0.25 if d > 0 else 0.25) * (2 if abs(d) >= 8 else 1)
        how = "a little (about 1/4 turn)" if abs(t) < 0.5 else "about 1/2 turn"
    else:
        # turns on A and B that would bring both to the target, given the coupling
        det = 1.0 - c * c
        ta = (-da + c * db) / (k * det)
        tb = (-db + c * da) / (k * det)
        belt, t = ("A", ta) if abs(ta) >= abs(tb) else ("B", tb)
        t = max(-1.0, min(1.0, t))
        how = _turn_text(t)
    upd["last"] = {"belt": belt, "turns": t, "A": fa, "B": fb}
    return ("%s belt %s %s, then measure both again"
            % ("Tighten" if t > 0 else "Loosen", belt, how)), upd
