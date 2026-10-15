# Printune: belt tension from plucks, measured with the toolhead accelerometer.
#
# A plucked belt span rings at f = sqrt(T / mu) / (2 L). The belt pulls on the
# carriage at that tone and at twice it, so the accelerometer on the toolhead picks it
# up. This finds each pluck in a recording, skips the first few milliseconds (the frame
# rings too, but dies out much sooner than the belt), and takes the tone where the
# tone and twice the tone together stand highest above the noise. Each pluck is then
# judged (loud enough, not a knock on the toolhead, not the other belt, not another
# stretch of belt) and only the plucks that pass are averaged.
#
# Copyright (C) 2026  Printune contributors
# This file may be distributed under the terms of the GNU GPLv3 license.
import math

import numpy as np

SEARCH = (25.0, 500.0)    # Hz, where lines are looked at (twice the tone included)
F_BAND = (0.55, 1.5)      # x target: where the tone itself is looked for
F_MAX = 200.0             # Hz: no belt tone above this unless the target is that high
SKIP = 0.03               # s after the onset: let the frame ring out
WINDOW = 0.6              # s of ringing used per pluck (less if the next pluck comes sooner)
DEAD = 0.45               # s: a new pluck can't start sooner than this
STEP = 0.05               # s: window lengths are rounded down to this (fewer noise spectra)


def _smooth(x, n):
    if n <= 1:
        return x
    k = np.ones(n) / n
    return np.convolve(x, k, mode="same")


def _running_median(x, n):
    h = n // 2
    pad = np.pad(x, h, mode="edge")
    try:
        view = np.lib.stride_tricks.sliding_window_view(pad, n)
    except AttributeError:            # numpy < 1.20
        return np.array([np.median(pad[i:i + n]) for i in range(len(x))])
    return np.median(view, axis=1)


def _nfft(fs):
    return 1 << int(math.ceil(math.log2(8 * fs)))       # ~0.1 Hz bins after zero padding


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


def find_plucks(axes, fs, lo, hi, mask, base=None, env=None):
    """Onset indices of the plucks: the energy in the band jumps up.
    base: the band level without plucks (from a quiet recording), if there is one."""
    if env is None:
        env = _band(axes, fs, lo, hi, mask)
    if base is None:
        base = float(np.percentile(env, 10))
    base = base or 1e-9
    # relative to the noise; the loudest event only sets a very low floor, so a soft
    # pluck among loud ones (-30 dB) is still found
    thr = max(5.0 * base, 0.03 * float(env.max()))
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
    """Parabolic peak position around bin k (on the log spectrum), at most half a bin
    from it."""
    if 0 < k < len(p) - 1:
        y0, y1, y2 = np.log(p[k - 1:k + 2] + 1e-30)
        den = y0 - 2 * y1 + y2
        if den < 0:
            off = max(-0.5, min(0.5, 0.5 * (y0 - y2) / den))
            return float(f[k] + off * df)
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
SPAN = 0.05               # share: plucks this far from the rest rang on another stretch of belt
MIN_GOOD = 3              # fewer accepted plucks than this: no reading
# How long a line rings tells a belt from the toolhead or the frame: a knocked toolhead
# rings out within ~0.15-0.3 s, a belt much longer. The pull at twice the tone goes
# with the swing squared, so it dies twice as fast as the tone itself.
KNOCK_TAU = 0.2           # s: a tone dying faster than this (1/e of its level) is a knock
HARM_TAU = 1.4            # a tone and twice it: the lower rings at least this much longer
OCT_TIE = 0.7             # scores this close: the one nearer the belt's last reading wins


def _db(x):
    return 10.0 * math.log10(max(x, 1e-30))


def _near(f, p, fc, df, half=0.6):
    """The highest bin within +-half Hz of fc."""
    sel = np.flatnonzero(np.abs(f - fc) <= max(half, 1.5 * df))
    return int(sel[np.argmax(p[sel])]) if len(sel) else None


def _tau(halves, f, df, fc):
    """How long the line at fc rings (s, 1/e of its level), from the first and the
    second half of the window. halves: (first, second, noise for a half, half in s)."""
    pe, pl, nh, h = halves
    ke, kl = _near(f, pe, fc, df, 0.3), _near(f, pl, fc, df, 0.3)
    if ke is None:
        return 0.0
    ee = pe[ke] / max(nh[ke], 1e-30) - 1.0         # above the noise only
    el = max(pl[kl] / max(nh[kl], 1e-30) - 1.0, 0.1)
    if ee + 1.0 < 10.0 ** (WEAK_DB / 10.0):
        return 0.0                    # no line there
    if ee <= el:
        return float("inf")           # still as loud: rings on
    return 2.0 * h / math.log(ee / el)             # power: e^(-2t/tau)


def _late(halves, f, df, fc):
    """The line at fc is still there (WEAK) in the second half of the window."""
    pe, pl, nh, h = halves
    k = _near(f, pl, fc, df, 0.3)
    return k is not None and pl[k] / max(nh[k], 1e-30) >= 10.0 ** (WEAK_DB / 10.0)


def pluck_freq(f, p, noise, df, lo, hi, avoid=(), target=None, prev=None, halves=None):
    """Belt tone from one pluck's spectrum p, given the noise spectrum (same window
    length, no pluck). The carriage feels a belt at its tone and at twice it (the pull
    changes twice per swing), so the tone F is where F and 2F together stand highest
    above the noise. target bounds where F is looked for; prev (the belt's last
    reading) breaks a tie between F and an octave of it; halves (see _tau) tell how
    long each line rings. Returns (freq or None, dB at F, dB at 2F)."""
    snr = p / np.maximum(noise, 1e-30)
    top = f[-1] / 2.0 - 2.0
    flo, fhi = lo, min(hi, top, F_MAX)
    if target:
        flo = max(lo, F_BAND[0] * target)
        fhi = min(hi, top, F_BAND[1] * target, max(F_MAX, 1.2 * target))
    sel = np.flatnonzero((f >= flo) & (f <= fhi))
    if not len(sel):
        return None, None, None
    bad = np.zeros(len(f), bool)
    for fa in avoid:                  # the toolhead ringing by itself is not a belt
        bad |= np.abs(f - fa) <= TAP_GUARD
    strong, weak = 10.0 ** (STRONG_DB / 10.0), 10.0 ** (WEAK_DB / 10.0)
    # twice the tone: the bin at 2F and its neighbours (zero padding spreads a line);
    # the tap guard is for the tone only, a belt's 2F may sit on a toolhead mode
    k2 = 2 * sel
    s2 = np.maximum.reduce([snr[np.clip(k2 + d, 0, len(f) - 1)] for d in (-2, -1, 0, 1, 2)])
    # the toolhead's own ringing has nothing at twice it: near it, a tone only counts
    # with a clear line at twice it (so a belt tuned right onto it can still be read)
    s1 = np.where(bad[sel] & (s2 < strong), 0.0, snr[sel])
    score = np.log1p(s1) + np.log1p(s2)
    i = int(np.argmax(score))
    if s1[i] == 0.0 and s2[i] == 0.0:
        return None, None, None

    def line(fc):
        j = _near(f, p, fc, df, 0.3)
        return 0.0 if j is None else float(snr[j])

    def sc(fc):
        return math.log1p(line(fc)) + math.log1p(line(2 * fc))

    def guarded(fc):
        return bool(np.any(np.abs(np.asarray(avoid, float) - fc) <= TAP_GUARD)) if len(avoid) else False

    def harmonic(fl):
        """fl and 2 fl ring like a belt's tone and twice it: the tone rings longer
        (True), shorter (False: not a belt's tone), or can't tell (None)."""
        if halves is None:
            return None
        t1, t2 = _tau(halves, f, df, fl), _tau(halves, f, df, 2 * fl)
        if t1 == 0.0 or t2 == 0.0:
            return None
        if t1 < t2 and _late(halves, f, df, 2 * fl):
            return False              # (noise in a second half only lengthens a time)
        # longer, and the lower line heard in both halves: two looks, not a noise blip
        return True if t1 >= HARM_TAU * t2 and _late(halves, f, df, fl) else None

    # the true local maximum of the spectrum near the best score
    k = _near(f, p, float(f[sel[i]]), df)
    fk = _peak(f, p, k, df)
    # octave check: a tone picked at twice the belt's (a 2F-strong sensor, a strong 4F),
    # or at half it (a frame mode near F/2)
    alt = None
    lh = fk / 2.0
    if lh >= lo + 2 * df and line(fk) >= strong and line(lh) >= weak and not guarded(lh):
        h = harmonic(lh)
        if h or (halves is None and target and lh >= flo):     # no timing: the target decides
            alt = lh
    if alt is None and 2 * fk <= min(hi, top) - 2 * df:
        # up only on evidence at 4x: then the line near 2fk has its own 2F, and fk
        # isn't its tone (a frame mode near F/2 is seldom exactly half)
        f2 = _peak(f, p, _near(f, p, 2 * fk, df), df)
        if (not guarded(f2) and line(f2) >= max(strong, line(fk)) and line(2 * f2) >= weak
                and harmonic(fk) is False):
            alt = f2
    if alt is None and harmonic(fk) is False:
        return None, None, None       # dies faster than the line at twice it: not a belt's tone
    if alt is None and prev:
        # can't tell (or nearly a tie): the octave nearer the belt's last reading
        for c in (fk / 2.0, 2 * fk):
            if lo + 2 * df <= c <= min(hi, top) - 2 * df:
                c = _peak(f, p, _near(f, p, c, df), df)      # the line itself
            fits = harmonic(c) is not False if c < fk else harmonic(fk) is not True
            if (lo + 2 * df <= c <= min(hi, top) - 2 * df and fits and not guarded(c)
                    and line(c) >= weak and line(2 * c) >= weak and sc(c) >= sc(fk) - OCT_TIE
                    and abs(math.log(c / prev)) < abs(math.log(fk / prev))):
                alt = c
                break
    if alt is not None:
        k = _near(f, p, alt, df)
        fk = _peak(f, p, k, df)
    j = _near(f, p, 2 * fk, df)
    db1 = _db(snr[k])
    db2 = _db(snr[j]) if j is not None else -99.0
    if bad[k] and (j is None or snr[j] < strong):
        return None, None, None       # the toolhead ringing by itself
    if j is not None and db2 > db1:
        fk = _peak(f, p, j, df) / 2.0          # the sharper line: twice the tone
    if fk < lo + 2 * df or fk > hi - 2 * df:
        return None, db1, db2         # at the edge of what is searched: not a belt reading
    return fk, db1, db2


def _knock(f, df, halves, fr, d1, d2, avoid):
    """A knock on the toolhead or the frame, not a pluck: a clear tone with no pull at
    twice it (or only the toolhead's own ringing there, learned from a tap) that dies
    fast. Belts ring long, so this needs no tap learned first."""
    on_mode = any(abs(2 * fr - m) <= TAP_GUARD for m in avoid)
    if not on_mode and (d1 < STRONG_DB or (d2 >= WEAK_DB and d1 < d2 + 20.0)):
        return False                  # a belt's pull at twice the tone is there
    return _tau(halves, f, df, fr) < KNOCK_TAU


def _windows(axes, fs, onsets):
    win, step = int(WINDOW * fs), max(1, int(STEP * fs))
    n_all = len(axes[0])
    for j, on in enumerate(onsets):
        a0 = on + int(SKIP * fs)
        a1 = min(a0 + win, n_all, (onsets[j + 1] - int(0.01 * fs)) if j + 1 < len(onsets) else n_all)
        a1 = a0 + (a1 - a0) // step * step
        if a1 - a0 >= int(0.15 * fs):
            yield on, a0, a1


def _noise(axes, q, fs, n, nfft, cache):
    """Noise spectrum for a window of n samples: the quiet recording if there is one,
    else what is there most of the time in this recording (the 20th percentile: the
    moment just before a pluck is still the last pluck ringing)."""
    if q is not None and len(q[0]) >= n:
        if n not in cache:
            cache[n] = _background(q, fs, n, nfft, 50)
        return cache[n]
    key = ("rec", n)
    if key not in cache:
        cache[key] = _steady(axes, fs, n, nfft)
    return cache[key]


def _steady(axes, fs, n, nfft):
    """Noise from a recording full of plucks: the 20th percentile over windows. Where
    that still holds belt ringing (a line much louder in some windows than in others)
    the floor around it (+-20 Hz) is used; a steady line (a fan) stays in."""
    total = len(axes[0])
    if total < n:
        return None
    specs = np.array([_spectrum([a[k:k + n] for a in axes], fs, nfft)[1]
                      for k in range(0, total - n + 1, max(1, n // 4))])
    lo, hi = np.percentile(specs, [20, 80], axis=0)
    df = fs / nfft
    d = max(1, int(0.5 / df))                         # floor on a coarser grid: quicker
    floor = _running_median(lo[::d], int(40.0 / (d * df)) | 1)
    floor = np.interp(np.arange(len(lo)), np.arange(0, len(lo), d), floor)
    return np.where(hi > 2.0 * lo, np.minimum(lo, floor), lo)


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


def _spectra(axes, q, fs, nfft, f, mask, a0, a1, cache):
    """Spectrum, noise, and the two halves (for how long lines ring) of one window."""
    df = f[1] - f[0]
    sm = max(1, int(2.0 / df)) | 1                  # noise level +-1 Hz, not single bins
    out = []
    for b0, b1 in ((a0, a1), (a0, (a0 + a1) // 2), ((a0 + a1) // 2, a0 + 2 * ((a1 - a0) // 2))):
        p = _spectrum([a[b0:b1] for a in axes], fs, nfft)[1]
        nz = _noise(axes, q, fs, b1 - b0, nfft, cache)
        if nz is None:
            return None
        nz = _smooth(nz, sm)
        if mask is not None:
            nz = np.where(mask(f), np.maximum(nz, p), nz)      # fans and motors: never the belt
        out.append((p, nz))
    (p, nz), (pe, ne), (pl, _) = out
    return p, nz, (pe, pl, ne, ((a1 - a0) // 2) / fs)


def _clusters(fr):
    """Groups of readings within SPREAD of each other, largest first: (centre, members)."""
    best = []
    for x in fr:
        mem = [y for y in fr if abs(y - x) <= SPREAD]
        best.append((float(np.median(mem)), mem))
    best.sort(key=lambda c: -len(c[1]))
    return best


def analyse(t, ax, ay, az, target=None, expect=None, quiet=None, avoid=(), other=None, prev=None):
    """Belt tone from a recording with several plucks. quiet: (ax, ay, az) of a short
    recording without plucks, to tell fans and motors from the belt. target: the
    target tone (bounds where the tone is looked for). avoid: the toolhead's own
    ringing (Hz, from a tap). other: the other belt's last reading. prev: this belt's
    last reading (decides between a tone and its octave when nothing else can).

    Each pluck is accepted or thrown out; only accepted plucks are averaged. Returns a
    dict: freq (None unless the series is good), plucks (every pluck heard: freq, dB at
    the tone and at twice it, ok, why), used, heard, verdict (good, again, spread,
    spans, none), note."""
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
    if q is None and onsets[0] >= int(0.6 * fs):
        # no quiet recording: the stretch before the first pluck is the next best
        q = [a[:onsets[0] - int(0.05 * fs)] for a in axes]
        mask = steady_lines(q, fs)
    nfft = _nfft(fs)
    f = np.fft.rfftfreq(nfft, 1.0 / fs)
    df = f[1] - f[0]
    cache = {}
    plucks = []
    for on, a0, a1 in _windows(axes, fs, onsets):
        sp = _spectra(axes, q, fs, nfft, f, mask, a0, a1, cache)
        if sp is None:
            continue
        p, nz, halves = sp
        fr, d1, d2 = pluck_freq(f, p, nz, df, lo, hi, avoid, target, prev, halves)
        pk = {"t": round(float(on / fs), 2), "freq": None if fr is None else round(fr, 2),
              "db": None if d1 is None else round(d1, 1), "db2": None if d2 is None else round(d2, 1),
              "ok": False, "why": ""}
        if fr is None:
            pk["why"] = "no belt tone"
        elif _knock(f, df, halves, fr, d1, d2, avoid):
            pk["why"] = "a knock, not a belt"
        elif max(d1, d2) < STRONG_DB or min(d1, d2) < WEAK_DB:
            pk["why"] = "too weak"
        else:
            pk["ok"] = True
        plucks.append(pk)
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
    # plucks far from the rest rang on another stretch of belt: left out if most agree,
    # else no reading
    fr = [x["freq"] for x in ok]
    cl = _clusters(fr)
    c, mem = cl[0]
    if len(mem) >= MIN_GOOD and 2 * len(mem) > len(fr):
        for x in ok:
            if abs(x["freq"] - c) > SPAN * c:
                x["ok"], x["why"] = False, "rang at %.1f Hz instead of %.1f Hz" % (x["freq"], c)
        ok = [x for x in ok if x["ok"]]
    elif max(fr) - min(fr) > SPAN * float(np.median(fr)):
        return out("spans", "the plucks don't agree (%.1f-%.1f Hz)" % (min(fr), max(fr)),
                   plucks, used=len(ok))
    # the rest must agree within SPREAD; one stray among agreeing ones is thrown out
    stray = False
    while True:
        fr = [x["freq"] for x in ok]
        med = float(np.median(fr))
        far = [x for x in ok if abs(x["freq"] - med) > SPREAD]
        if not far:
            break
        if stray or len(far) > 1 or len(ok) - 1 < MIN_GOOD:
            return out("spread", "the plucks disagree (%.1f-%.1f Hz)" % (min(fr), max(fr)),
                       plucks, used=len(ok))
        far[0]["ok"], far[0]["why"] = False, "far from the others"
        ok = [x for x in ok if x["ok"]]
        stray = True
    if expect and len(ok) > expect:
        # more good plucks than asked for: the first ones count
        for x in ok[expect:]:
            x["ok"], x["why"] = False, "more than needed"
        ok = ok[:expect]
        fr = [x["freq"] for x in ok]
    return out("good", "%d of %d plucks counted" % (len(ok), len(plucks)), plucks,
               freq=round(float(np.mean(fr)), 1), used=len(ok))


def analyse_tap(t, ax, ay, az, quiet=None):
    """The toolhead's own ringing from a light tap: the strongest lines (up to 5; a hard
    tap brings out more of them) that stand clearly above the noise and are not a belt
    ringing along: no line at twice them or at half them, and rung out quickly."""
    st = _setup(t, ax, ay, az, quiet)
    if st is None:
        return {"modes": [], "note": "no data from the accelerometer"}
    axes, fs, q = st
    lo, hi = SEARCH[0], min(SEARCH[1], 0.45 * fs)
    onsets = find_plucks(axes, fs, lo, hi, None, None)
    if not onsets:
        return {"modes": [], "note": "no tap heard"}
    nfft = _nfft(fs)
    f = np.fft.rfftfreq(nfft, 1.0 / fs)
    df = f[1] - f[0]
    on, a0, a1 = next(_windows(axes, fs, onsets[:1]), (None, None, None))
    if on is None:
        return {"modes": [], "note": "the tap was too close to the end"}
    p, nz, halves = _spectra(axes, q, fs, nfft, f, None, a0, a1, {})
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
        # a belt the tap set ringing: a line at twice it (its 2F) or at half it (this is
        # the 2F), or still ringing long after the tap
        j2, jh = _near(f, p, 2 * fk, df), _near(f, p, fk / 2.0, df)
        if j2 is not None and _db(snr[j2]) >= WEAK_DB:
            continue
        if fk / 2.0 >= lo and jh is not None and _db(snr[jh]) >= WEAK_DB:
            continue
        if _tau(halves, f, df, fk) >= KNOCK_TAU:
            continue
        modes.append(round(fk, 1))
    return {"modes": modes, "note": "" if modes else "the tap rang nothing clearly"}


def plan(b):
    """What to tell the user after both belts were measured. b is the belt state
    (target, tol, A, B); returns (advice, updates for b). Just the numbers and which
    way: the user reads them and tightens or loosens."""
    tgt, tol = b["target"], b["tol"]
    fa = (b.get("A") or {}).get("freq")
    fb = (b.get("B") or {}).get("freq")
    upd = {}
    if fa is None or fb is None:
        return "", upd
    need = b.get("plucks", 5)
    full = all(((b.get(k) or {}).get("used") or 0) >= need for k in ("A", "B"))
    on = [abs(f - tgt) <= tol for f in (fa, fb)]
    upd["done"] = all(on) and full
    if upd["done"]:
        return "Both belts are on target. Done.", upd
    if all(on):
        return "On target, but not from %d good plucks each: measure both again" % need, upd
    todo = ["%s belt %s" % ("tighten" if f < tgt else "loosen", k)
            for k, f, o in (("A", fa, on[0]), ("B", fb, on[1])) if not o]
    adv = " and ".join(todo)
    return adv[0].upper() + adv[1:] + ", then measure both again", upd
