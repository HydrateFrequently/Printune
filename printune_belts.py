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

SEARCH = (25.0, 500.0)    # Hz, where a belt is looked for (wider than any target band)
SKIP = 0.03               # s after the onset: let the frame ring out
WINDOW = 0.6              # s of ringing used per pluck
DEAD = 0.45               # s: a new pluck can't start sooner than this
MIN_PLUCKS = 2            # fewer heard than this: no reading


def _smooth(x, n):
    if n <= 1:
        return x
    k = np.ones(n) / n
    return np.convolve(x, k, mode="same")


def _running_median(x, n):
    h = n // 2
    pad = np.pad(x, h, mode="edge")
    return np.array([np.median(pad[i:i + n]) for i in range(len(x))])


def _spectrum(seg, fs, nfft):
    w = np.hanning(len(seg[0]))
    p = 0
    for s in seg:
        s = (s - s.mean()) * w
        p = p + np.abs(np.fft.rfft(s, nfft)) ** 2
    return np.fft.rfftfreq(nfft, 1.0 / fs), p


def _background(axes, fs, n, nfft, pct):
    """Spectrum of what is there all the time (fans, motors, noise) for windows of n
    samples: the pct-th percentile over overlapping windows."""
    total = len(axes[0])
    if total < n:
        return None
    specs = [_spectrum([a[k:k + n] for a in axes], fs, nfft)[1]
             for k in range(0, total - n + 1, max(1, n // 4))]
    return np.percentile(np.array(specs), pct, axis=0)


def steady_lines(axes, fs):
    """Mask function for strong spectral lines in a recording without plucks."""
    seg = 1 << int(math.ceil(math.log2(0.25 * fs)))
    nfft = 4 * seg
    bg = _background(axes, fs, seg, nfft, 50)
    if bg is None:
        return None
    f = np.fft.rfftfreq(nfft, 1.0 / fs)
    df = f[1] - f[0]
    floor = _running_median(bg, int(40.0 / df) | 1)          # +-20 Hz
    hot = bg > 4.0 * floor
    hot = np.convolve(hot.astype(float), np.ones(int(3.0 / df) | 1), mode="same") > 0

    def mask(freqs):
        return np.interp(freqs, f, hot.astype(float)) > 0.0
    return mask


def _band(axes, fs, lo, hi, mask):
    out = []
    for a in axes:
        spec = np.fft.rfft(a - a.mean())
        f = np.fft.rfftfreq(len(a), 1.0 / fs)
        keep = (f >= lo) & (f <= hi)
        if mask is not None:
            keep &= ~mask(f)
        out.append(np.fft.irfft(np.where(keep, spec, 0), len(a)))
    return _smooth(np.sqrt(sum(x * x for x in out)), max(1, int(0.01 * fs)))


def find_plucks(axes, fs, lo, hi, mask, base=None):
    """Onset indices of the plucks: the energy in the band jumps up.
    base: the band level without plucks (from a quiet recording), if there is one."""
    env = _band(axes, fs, lo, hi, mask)
    if base is None:
        base = float(np.percentile(env, 10))
    base = base or 1e-9
    thr = max(5.0 * base, 0.1 * float(env.max()))
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


def _peak(f, p, k, df):
    """Parabolic peak position around bin k (on the log spectrum)."""
    if 0 < k < len(p) - 1:
        y0, y1, y2 = np.log(p[k - 1:k + 2] + 1e-30)
        den = y0 - 2 * y1 + y2
        if den < 0:
            return float(f[k] + 0.5 * (y0 - y2) / den * df)
    return float(f[k])


def pluck_freq(f, p, df, lo, hi):
    """Belt frequency from one pluck's spectrum (background already taken off), or None.
    The carriage feels the belt at its tone and at twice it (the pull changes twice
    per swing); either can be the stronger, so a peak with a smaller one at exactly
    half its frequency is the second harmonic."""
    sel = np.flatnonzero((f >= lo) & (f <= hi))
    if not len(sel) or p[sel].max() <= 0:
        return None
    k = int(sel[np.argmax(p[sel])])
    fk = _peak(f, p, k, df)
    half = np.flatnonzero((f >= max(lo, 0.49 * fk)) & (f <= 0.51 * fk))
    if len(half):
        j = int(half[np.argmax(p[half])])
        if p[j] > 0.05 * p[k] and p[j] >= p[max(0, j - 1)] and p[j] >= p[min(len(p) - 1, j + 1)]:
            fj = _peak(f, p, j, df)
            if abs(2 * fj - fk) <= 0.004 * fk + 2 * df:      # exactly half: a harmonic
                k, fk = j, fj
    if fk < lo + 2 * df or fk > hi - 2 * df:
        return None                   # at the edge of what is searched: not a belt reading
    return fk


def analyse(t, ax, ay, az, target=None, expect=None, quiet=None):
    """Belt frequency from a recording with several plucks. quiet: (ax, ay, az) of a
    short recording without plucks, to tell fans and motors from the belt.
    Returns dict: freq (average), plucks (each pluck's peak), used (count), note."""
    t = np.asarray(t, float)
    axes = [np.asarray(a, float) for a in (ax, ay, az)]
    if len(t) < 100:
        return {"freq": None, "plucks": [], "used": 0, "note": "no data from the accelerometer"}
    fs = (len(t) - 1) / (t[-1] - t[0])
    lo, hi = SEARCH[0], min(SEARCH[1], 0.45 * fs)
    q = None
    if quiet is not None and len(quiet[0]) > int(0.5 * fs):
        q = [np.asarray(a, float) for a in quiet]
    mask = steady_lines(q, fs) if q is not None else None
    base = None
    if q is not None:
        base = float(np.median(_band(q, fs, lo, hi, mask)))
    onsets = find_plucks(axes, fs, lo, hi, mask, base)
    if not onsets:
        return {"freq": None, "plucks": [], "used": 0, "note": "no pluck heard"}
    win = int(WINDOW * fs)
    nfft = 1 << int(math.ceil(math.log2(8 * fs)))       # ~0.125 Hz bins after zero padding
    f = np.fft.rfftfreq(nfft, 1.0 / fs)
    df = f[1] - f[0]
    bgs = {}
    freqs = []
    for j, on in enumerate(onsets):
        a0 = on + int(SKIP * fs)
        a1 = min(a0 + win, len(t), (onsets[j + 1] - int(0.01 * fs)) if j + 1 < len(onsets) else len(t))
        n = a1 - a0
        if n < int(0.15 * fs):
            continue
        p = _spectrum([a[a0:a1] for a in axes], fs, nfft)[1]
        # take off what is there without a pluck (same window length), with a margin
        n = int(round(n / (0.05 * fs))) * int(0.05 * fs) or n
        if n not in bgs:
            src = q if q is not None and len(q[0]) >= n else axes
            bgs[n] = _background(src, fs, n, nfft, 50 if src is q else 20)
        if bgs[n] is not None:
            p = np.maximum(p - 2.0 * bgs[n], 0.0)
        fr = pluck_freq(f, p, df, lo, hi)
        if fr is not None:
            freqs.append(fr)
    if expect:
        freqs = freqs[:expect]
    if len(freqs) < MIN_PLUCKS:
        return {"freq": None, "plucks": [round(x, 1) for x in freqs], "used": len(freqs),
                "note": "only %d clear pluck heard" % len(freqs) if freqs else "no clear pluck heard"}
    med = float(np.median(freqs))
    good = [x for x in freqs if abs(x - med) <= max(3.0, 0.05 * med)]
    if len(good) < MIN_PLUCKS:
        return {"freq": None, "plucks": [round(x, 1) for x in freqs], "used": len(good),
                "note": "the plucks disagree"}
    note = ""
    if len(good) < len(freqs):
        note = "%d pluck(s) left out (far from the others)" % (len(freqs) - len(good))
    return {"freq": round(float(np.mean(good)), 1), "plucks": [round(x, 1) for x in freqs],
            "used": len(good), "note": note}


_EIGHTHS = {1: "1/8", 2: "1/4", 3: "3/8", 4: "1/2", 5: "5/8", 6: "3/4", 7: "7/8", 8: "1"}


def _told(t):
    """The turn as told to the user (eighths of a turn; 0: less than 1/8) and its text."""
    n = min(8, int(round(abs(t) * 8)))
    if n < 1:
        return 0.0, "a tiny bit (less than 1/8 turn)"
    return math.copysign(n / 8.0, t), "about %s turn" % _EIGHTHS[n]


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
        # learn only from a real turn that moved the belt the way it should, by more
        # than the scatter between plucks
        if last["turns"] and last["turns"] * d_own > 0 and abs(d_own) > 1.5:
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
        sign = t
    else:
        # turns on A and B that would bring both to the target, given the coupling
        det = 1.0 - c * c
        ta = (-da + c * db) / (k * det)
        tb = (-db + c * da) / (k * det)
        belt, t = ("A", ta) if abs(ta) >= abs(tb) else ("B", tb)
        sign = t
        t, how = _told(max(-1.0, min(1.0, t)))
    upd["last"] = {"belt": belt, "turns": t, "A": fa, "B": fb}
    return ("%s belt %s %s, then measure both again"
            % ("Tighten" if sign > 0 else "Loosen", belt, how)), upd
