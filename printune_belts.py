# Printune: belt tension from plucks, measured with the toolhead accelerometer.
#
# A plucked belt span rings at f = sqrt(T / mu) / (2 L). The belt pulls on the
# carriage at that tone and at twice it, so the accelerometer on the toolhead picks it
# up. This finds each pluck in a recording, skips the first few milliseconds (the frame
# rings too, but dies out much sooner than the belt), and takes the tone where the
# tone and twice the tone together stand highest above the noise. Each pluck is then
# judged (loud enough, not the toolhead ringing by itself, not the other belt) and only
# the plucks that pass are averaged.
#
# Copyright (C) 2026  Printune contributors
# This file may be distributed under the terms of the GNU GPLv3 license.
import math

import numpy as np

SEARCH = (25.0, 500.0)    # Hz, where a belt is looked for (wider than any target band)
SKIP = 0.03               # s after the onset: let the frame ring out
WINDOW = 0.6              # s of ringing used per pluck
DEAD = 0.45               # s: a new pluck can't start sooner than this


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


# A pluck is judged against the same sensor's own noise, never by absolute levels: a
# stiff or a loose mount, near the belt or far from it, changes both alike. How strong
# the tone is next to twice the tone differs per sensor and mount (on one printer 25 dB
# on one chip and 5 dB on another), so neither is required to be the stronger.
STRONG_DB = 12.0          # dB: the stronger of tone / twice the tone above the noise (like 12 dB SINAD)
WEAK_DB = 6.0             # the weaker one: there at all
TAP_GUARD = 2.0           # Hz around the toolhead's own ringing (learned from a tap)
OTHER_GUARD = 1.0         # Hz: a pluck this close to the other belt is that belt ringing
SPREAD = 1.0              # Hz: accepted plucks must all be this close to their median
MIN_GOOD = 3              # fewer accepted plucks than this: no reading


def _db(x):
    return 10.0 * math.log10(max(x, 1e-30))


def _near(f, p, fc, df, half=0.6):
    """The highest bin within +-half Hz of fc."""
    sel = np.flatnonzero(np.abs(f - fc) <= max(half, 1.5 * df))
    return int(sel[np.argmax(p[sel])]) if len(sel) else None


def pluck_freq(f, p, noise, df, lo, hi, avoid=()):
    """Belt tone from one pluck's spectrum p, given the noise spectrum (same window
    length, no pluck). The carriage feels a belt at its tone and at twice it (the pull
    changes twice per swing), so the tone F is where F and 2F together stand highest
    above the noise. Returns (freq or None, dB at F, dB at 2F)."""
    snr = p / np.maximum(noise, 1e-30)
    top = f[-1] / 2.0 - 2.0
    sel = np.flatnonzero((f >= lo) & (f <= min(hi, top)))
    if not len(sel):
        return None, None, None
    bad = np.zeros(len(f), bool)
    for fa in avoid:                  # the toolhead ringing by itself is not a belt
        bad |= np.abs(f - fa) <= TAP_GUARD
    # twice the tone: the bin at 2F and its neighbours (zero padding spreads a line)
    k2 = 2 * sel
    s2 = np.maximum.reduce([np.where(bad, 0.0, snr)[np.clip(k2 + d, 0, len(f) - 1)]
                            for d in (-2, -1, 0, 1, 2)])
    # the toolhead's own ringing has nothing at twice it: near it, a tone only counts
    # with a clear line at twice it (so a belt tuned right onto it can still be read)
    strong = 10.0 ** (STRONG_DB / 10.0)
    s1 = np.where(bad[sel] & (s2 < strong), 0.0, snr[sel])
    score = np.log1p(s1) + np.log1p(s2)
    i = int(np.argmax(score))
    k = int(sel[i])
    if s1[i] == 0.0 and s2[i] == 0.0:
        return None, None, None
    fk = _peak(f, p, k, df)
    j = _near(f, p, 2 * fk, df)
    db1 = _db(snr[k])
    if bad[k] and s2[i] < strong:
        return None, None, None
    db2 = _db(snr[j]) if j is not None and not bad[j] else -99.0
    if j is not None and db2 > db1 and not bad[j]:
        fk = _peak(f, p, j, df) / 2.0          # the sharper line: twice the tone
    if fk < lo + 2 * df or fk > hi - 2 * df:
        return None, db1, db2         # at the edge of what is searched: not a belt reading
    return fk, db1, db2


def _windows(axes, fs, onsets):
    win = int(WINDOW * fs)
    n_all = len(axes[0])
    for j, on in enumerate(onsets):
        a0 = on + int(SKIP * fs)
        a1 = min(a0 + win, n_all, (onsets[j + 1] - int(0.01 * fs)) if j + 1 < len(onsets) else n_all)
        if a1 - a0 >= int(0.15 * fs):
            yield on, a0, a1


def _noise(axes, q, fs, on, n, nfft, cache):
    """Noise spectrum for a window of n samples: the quiet recording if there is one,
    else the moment just before this pluck (or the quietest part of the recording)."""
    if q is not None and len(q[0]) >= n:
        if n not in cache:
            cache[n] = _background(q, fs, n, nfft, 50)
        return cache[n]
    b0 = on - n - int(0.02 * fs)
    if b0 >= 0:
        return _spectrum([a[b0:b0 + n] for a in axes], fs, nfft)[1]
    key = ("rec", n)
    if key not in cache:
        cache[key] = _background(axes, fs, n, nfft, 20)
    return cache[key]


def _setup(t, ax, ay, az, quiet):
    t = np.asarray(t, float)
    axes = [np.asarray(a, float) for a in (ax, ay, az)]
    if len(t) < 100:
        return None
    fs = (len(t) - 1) / (t[-1] - t[0])
    q = None
    if quiet is not None and len(quiet[0]) > int(0.5 * fs):
        q = [np.asarray(a, float) for a in quiet]
    return axes, fs, q


def analyse(t, ax, ay, az, target=None, expect=None, quiet=None, avoid=(), other=None):
    """Belt tone from a recording with several plucks. quiet: (ax, ay, az) of a short
    recording without plucks, to tell fans and motors from the belt. avoid: the
    toolhead's own ringing (Hz, from a tap). other: the other belt's last reading.

    Each pluck is accepted or thrown out; only accepted plucks are averaged. Returns a
    dict: freq (None unless the series is good), plucks (every pluck heard: freq, dB at
    the tone and at twice it, ok, why), used, heard, verdict (good, again, spread,
    none), note."""
    def out(verdict, note, plucks=(), freq=None, used=0):
        return {"freq": freq, "plucks": list(plucks), "used": used, "heard": len(plucks),
                "verdict": verdict, "note": note}
    st = _setup(t, ax, ay, az, quiet)
    if st is None:
        return out("none", "no data from the accelerometer")
    axes, fs, q = st
    lo, hi = SEARCH[0], min(SEARCH[1], 0.45 * fs / 2.0)
    mask = steady_lines(q, fs) if q is not None else None
    base = float(np.median(_band(q, fs, lo, hi, mask))) if q is not None else None
    onsets = find_plucks(axes, fs, lo, hi, mask, base)
    if not onsets:
        return out("none", "no pluck heard")
    nfft = 1 << int(math.ceil(math.log2(16 * fs)))      # ~0.06 Hz bins after zero padding
    f = np.fft.rfftfreq(nfft, 1.0 / fs)
    df = f[1] - f[0]
    cache = {}
    plucks = []
    for on, a0, a1 in _windows(axes, fs, onsets):
        n = a1 - a0
        p = _spectrum([a[a0:a1] for a in axes], fs, nfft)[1]
        nz = _noise(axes, q, fs, on, n, nfft, cache)
        if nz is None:
            continue
        # the noise level around each frequency (+-1 Hz), not single noisy bins
        nz = _smooth(nz, max(1, int(2.0 / df)) | 1)
        if mask is not None:
            nz = np.where(mask(f), np.maximum(nz, p), nz)      # fans and motors: never the belt
        fr, d1, d2 = pluck_freq(f, p, nz, df, lo, hi, avoid)
        pk = {"t": round(float(on / fs), 2), "freq": None if fr is None else round(fr, 2),
              "db": None if d1 is None else round(d1, 1), "db2": None if d2 is None else round(d2, 1),
              "ok": False, "why": ""}
        if fr is None:
            pk["why"] = "no belt tone"
        elif max(d1, d2) < STRONG_DB or min(d1, d2) < WEAK_DB:
            pk["why"] = "too weak"
        else:
            pk["ok"] = True
        plucks.append(pk)
    if expect:
        plucks = plucks[:expect]
    ok = [x for x in plucks if x["ok"]]
    if other and ok:
        med = float(np.median([x["freq"] for x in ok]))
        for x in ok:
            if abs(x["freq"] - other) <= OTHER_GUARD and abs(x["freq"] - med) > 1.5:
                x["ok"], x["why"] = False, "the other belt was ringing along"
        ok = [x for x in plucks if x["ok"]]
    if not plucks:
        return out("none", "no clear pluck heard")
    if len(ok) < MIN_GOOD:
        return out("again", "%d of %d plucks clear" % (len(ok), len(plucks)), plucks, used=len(ok))
    fr = [x["freq"] for x in ok]
    med = float(np.median(fr))
    if max(abs(x - med) for x in fr) > SPREAD:
        # one stray pluck among agreeing ones is thrown out; more than that: pluck again
        far = [x for x in ok if abs(x["freq"] - med) > SPREAD]
        if len(ok) - len(far) >= MIN_GOOD and len(far) == 1:
            far[0]["ok"], far[0]["why"] = False, "far from the others"
            ok = [x for x in ok if x["ok"]]
            fr = [x["freq"] for x in ok]
        else:
            return out("spread", "the plucks disagree (%.1f-%.1f Hz)" % (min(fr), max(fr)),
                       plucks, used=len(ok))
    return out("good", "%d of %d plucks counted" % (len(ok), len(plucks)), plucks,
               freq=round(float(np.mean(fr)), 1), used=len(ok))


def analyse_tap(t, ax, ay, az, quiet=None):
    """The toolhead's own ringing from a light tap: the strongest lines (up to 5; a hard
    tap brings out more of them) that
    stand clearly above the noise and have no line at twice them (a belt would)."""
    st = _setup(t, ax, ay, az, quiet)
    if st is None:
        return {"modes": [], "note": "no data from the accelerometer"}
    axes, fs, q = st
    lo, hi = SEARCH[0], min(SEARCH[1], 0.45 * fs)
    onsets = find_plucks(axes, fs, lo, hi, None, None)
    if not onsets:
        return {"modes": [], "note": "no tap heard"}
    nfft = 1 << int(math.ceil(math.log2(16 * fs)))
    f = np.fft.rfftfreq(nfft, 1.0 / fs)
    df = f[1] - f[0]
    on, a0, a1 = next(_windows(axes, fs, onsets[:1]), (None, None, None))
    if on is None:
        return {"modes": [], "note": "the tap was too close to the end"}
    p = _spectrum([a[a0:a1] for a in axes], fs, nfft)[1]
    nz = _noise(axes, q, fs, on, a1 - a0, nfft, {})
    nz = _smooth(nz, max(1, int(2.0 / df)) | 1)
    snr = p / np.maximum(nz, 1e-30)
    sel = np.flatnonzero((f >= lo) & (f <= hi))
    # real lines only: a local maximum over +-3 Hz that rises at least 6 dB above the
    # valley on each side (a strong ring's skirt is not a second line)
    w = max(1, int(3.0 / df))
    cands = []
    for k in sel:
        if k - w < 0 or k + w >= len(p) or p[k] < p[k - w:k + w + 1].max():
            continue
        if _db(snr[k]) < STRONG_DB:
            continue
        cands.append(int(k))
    cands.sort(key=lambda k: -p[k])
    modes = []
    for k in cands:
        if len(modes) >= 5:
            break
        fk = _peak(f, p, k, df)
        if any(abs(fk - m) <= 3 * TAP_GUARD for m in modes):
            continue
        # prominence: the dip between this line and every stronger one kept so far
        prom = all(p[k] >= 4.0 * p[min(k, int(round(m / df))):max(k, int(round(m / df))) + 1].min()
                   for m in modes)
        # and towards the outside: at least 6 dB above the level 10 Hz away
        side = min(p[max(0, k - int(10.0 / df))], p[min(len(p) - 1, k + int(10.0 / df))])
        if not prom or p[k] < 4.0 * side:
            continue
        j = _near(f, p, 2 * fk, df)
        if j is None or _db(snr[j]) < WEAK_DB:
            modes.append(round(fk, 1))
    return {"modes": modes, "note": "" if modes else "the tap rang nothing clearly"}


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
