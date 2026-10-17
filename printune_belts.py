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
    """Mask function for strong spectral lines in a recording without plucks (fans,
    motors). Only each line's core is masked (within 6 dB of its top, +-1 Hz for a fan's
    speed wandering): a line's skirt is in the noise level anyway, and a wide mask would
    hide a belt a few Hz from a fan. Long windows (up to 1 s) keep the lines narrow."""
    total = len(axes[0])
    seg = min(int(1.0 * fs), int(total / 1.5))
    if seg < int(0.25 * fs):
        return None
    nfft = 1 << int(math.ceil(math.log2(2 * seg)))
    bg = _background(axes, fs, seg, nfft, 50)
    if bg is None:
        return None
    f = np.fft.rfftfreq(nfft, 1.0 / fs)
    df = f[1] - f[0]
    d = max(1, int(0.5 / df))
    floor = _running_median(bg[::d], int(40.0 / (d * df)) | 1)          # +-20 Hz
    floor = np.interp(np.arange(len(bg)), np.arange(0, len(bg), d), floor)
    tops = [k for k in np.flatnonzero(bg > 8.0 * floor)     # 9 dB: few windows, so noise bins scatter
            if bg[k] >= bg[max(0, k - 1)] and bg[k] >= bg[min(len(bg) - 1, k + 1)]]
    core = np.zeros(len(f), bool)
    kept = []
    for k in sorted(tops, key=lambda k: -bg[k]):
        if any(abs(k - m) * df <= 4.0 and bg[k] * 30.0 < bg[m] for m in kept):
            continue                      # the skirt of a loud line beside it (a fan's wandering speed)
        kept.append(k)
        i = k
        while i > 0 and bg[i - 1] >= bg[k] / 4.0:
            i -= 1
        j = k
        while j < len(bg) - 1 and bg[j + 1] >= bg[k] / 4.0:
            j += 1
        core[i:j + 1] = True
    if not core.any():
        return None
    w = int(1.0 / df)
    hot = np.convolve(core.astype(float), np.ones(2 * w + 1), mode="same") > 0

    def mask(freqs):
        return np.interp(freqs, f, hot.astype(float)) > 0.0
    mask.lines = sorted(round(float(f[k]), 1) for k in kept)
    mask.wide = _wide_lines(axes, fs)
    return mask


def _wide_lines(axes, fs):
    """The strong lines as short windows see them (wide, with their skirts): for finding
    plucks in the band level, which is measured in short windows too."""
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


HUM_WINDOW = 1.0          # s
HUM_GAP = 1.0             # s: a pluck sooner than this after the last can't be judged for hum (its tone still rings)


def steady_in(axes, fs, quiet=None, onsets=()):
    """Lines that hum through a recording with plucks in it (a fan that came on after
    the background was recorded): as loud in the quietest 1 s windows as in the loudest
    (within 3 dB), 6 dB over the floor around them, and at the plucks neither starting nor
    dying away (_hum). A belt rung over and over can look steady in long windows, but its
    tone starts at each of its plucks. Returns (mask function or None, the Hz of the hums
    that are new: 6 dB louder than in the quiet recording, if there is one)."""
    seg = int(HUM_WINDOW * fs)        # long windows: a narrow line, so a belt a few Hz off stays readable
    total = len(axes[0])
    if total < 4 * seg:
        return None, []
    nfft = 1 << int(math.ceil(math.log2(2 * seg)))      # ~0.4 Hz bins: a 1 s window resolves ~1 Hz anyway
    specs = np.array([_spectrum([a[k:k + seg] for a in axes], fs, nfft)[1]
                      for k in range(0, total - seg + 1, seg // 2)])
    lo, hi = np.percentile(specs, [20, 80], axis=0)
    f = np.fft.rfftfreq(nfft, 1.0 / fs)
    df = f[1] - f[0]
    d = max(1, int(0.5 / df))                         # floor on a coarser grid: quicker
    floor = _running_median(lo[::d], int(40.0 / (d * df)) | 1)   # +-20 Hz
    floor = np.interp(np.arange(len(lo)), np.arange(0, len(lo), d), floor)
    hot = (lo > 4.0 * floor) & (hi <= 2.0 * lo) & (f >= SEARCH[0])
    if not hot.any():
        return None, []
    # each run of steady bins is one line; its loudest bin says where it is
    runs, k = [], 0
    idx = np.flatnonzero(hot)
    while k < len(idx):
        m = k
        while m + 1 < len(idx) and idx[m + 1] == idx[m] + 1:
            m += 1
        runs.append((idx[k], idx[m]))
        k = m + 1
    keep = np.zeros(len(f), bool)
    lines = []
    for r0, r1 in sorted(runs, key=lambda r: -lo[r[0]:r[1] + 1].max()):
        pk = r0 + int(np.argmax(lo[r0:r1 + 1]))
        fr = float(f[pk])
        if any(abs(fr - x) <= 5.0 and lo[pk] < 0.01 * lo[k] for x, k in lines):
            keep[r0:r1 + 1] = True            # a loud hum's side lobe (20 dB or more below it): masked with it
            continue
        if not _flat(axes, fs, fr):
            continue                          # it swells and dies in short windows: a belt plucked fast
        votes = [_hum(axes, fs, on, fr) for i, on in enumerate(onsets)
                 if i == 0 or on - onsets[i - 1] >= HUM_GAP * fs]
        if votes and 2 * sum(votes) < len(votes):
            continue                          # it starts at the plucks: a belt, not a hum
        keep[r0:r1 + 1] = True
        lines.append((fr, pk))
    if not keep.any():
        return None, []
    qb = _background(quiet, fs, seg, nfft, 50) if quiet is not None else None
    new = sorted(round(x, 1) for x, pk in lines if qb is None or lo[pk] > 4.0 * qb[pk])
    hot = np.convolve(keep.astype(float), np.ones(int(3.0 / df) | 1), mode="same") > 0

    def mask(freqs):
        return np.interp(freqs, f, hot.astype(float)) > 0.0
    return mask, new


def _flat(axes, fs, fr):
    """The line at fr keeps one level through the recording in 0.25 s slices (within 3 dB
    between the 20th and 80th percentile): a fan. A belt plucked again and again swells at
    each pluck and dies away between them, even when plucked faster than it dies out."""
    n = int(0.25 * fs)
    t = np.arange(n) / fs
    w = np.hanning(n)
    c, s_ = w * np.cos(2 * math.pi * fr * t), w * np.sin(2 * math.pi * fr * t)
    lv = []
    for i in range(0, len(axes[0]) - n + 1, n // 2):
        lv.append(sum(np.dot(a[i:i + n] - a[i:i + n].mean(), c) ** 2 + np.dot(a[i:i + n] - a[i:i + n].mean(), s_) ** 2
                      for a in axes))
    if len(lv) < 4:
        return False
    p20, p80 = np.percentile(lv, [20, 80])
    return p80 <= 2.0 * p20


def _either(m1, m2):
    if m1 is None or m2 is None:
        return m1 or m2
    return lambda freqs: m1(freqs) | m2(freqs)


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


LONG_FRAME = 0.2          # s: frames for finding plucks while the toolhead rings on


def _onsets_beside(axes, fs, lo, hi, mask, modes, quiet=None):
    """Onsets of plucks while a stiff toolhead still rings from a knock: in short spectra
    (LONG_FRAME), leaving out the toolhead's own lines (and the spread of a line in such
    a short frame), so its ringing can't hide a pluck that comes during it. (Cutting
    those lines out of the whole recording instead would smear them ahead of a knock.)"""
    n = int(LONG_FRAME * fs)
    hop = max(1, int(0.01 * fs))
    if len(axes[0]) < 2 * n:
        return [], None
    nfft = 1 << int(math.ceil(math.log2(2 * n)))
    f = np.fft.rfftfreq(nfft, 1.0 / fs)
    keep = (f >= lo) & (f <= hi)
    if mask is not None:
        keep &= ~mask(f)
    spread = 2.0 * fs / n                       # a line spreads this far in a Hann frame
    for fa, g in modes:
        keep &= np.abs(f - fa) > g + spread
    w = np.hanning(n)

    def env(x):
        idx = np.arange(0, len(x[0]) - n + 1, hop)
        e = 0
        for a in x:
            a = np.asarray(a, float)
            fr = np.lib.stride_tricks.as_strided(a, (len(idx), n), (a.strides[0] * hop, a.strides[0]))
            fr = (fr - fr.mean(axis=1, keepdims=True)) * w
            e = e + (np.abs(np.fft.rfft(fr, nfft, axis=1))[:, keep] ** 2).sum(axis=1)
        return np.sqrt(e), idx
    e, idx = env(axes)
    base = None
    if quiet is not None and len(quiet[0]) >= 2 * n:
        base = float(np.median(env(quiet)[0]))
    base = base or float(np.percentile(e, 10)) or 1e-9
    thr = max(5.0 * base, 0.03 * float(e.max()))
    back = max(1, n // hop)
    out, i = [], back
    dead = int(DEAD * fs / hop)
    while i < len(e):
        if e[i] > thr and e[i] > 3.0 * e[i - back]:
            # the frame's end is where the pluck has just come in
            out.append(int(idx[i] + n))
            i += dead
        else:
            i += 1
    return out, e


def _merge_onsets(fine, extra, fs, env):
    """Onsets from both ways; one found only beside the toolhead's lines is where the
    first short frame holding it ends (the whole band's steepest rise can be the
    toolhead's ringing, ahead of the pluck: a window started early reads it)."""
    out = list(fine)
    dead = int(DEAD * fs)
    for o in extra:
        if any(abs(o - x) < dead for x in out):
            continue
        if o < len(env) - 2:
            out.append(o)
    out.sort()
    keep = []
    for o in out:
        if not keep or o - keep[-1] >= dead:
            keep.append(o)
    return keep


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
TAP_GUARD = 2.0           # Hz around the toolhead's own ringing (learned from a tap), at least
GUARD_MAX = 8.0           # Hz: a broad, damped toolhead line is guarded this wide at most
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


def _modes(avoid, taus=False):
    """The toolhead's own lines as (Hz, guard Hz): from a tap's "ring" (each line with
    its own guard: a damped toolhead rings in a broad line, a stiff one in a sharp one)
    or plain Hz (the least guard)."""
    out = []
    for m in avoid or ():
        if isinstance(m, dict):
            out.append((float(m["freq"]), float(m.get("guard", TAP_GUARD)), float(m.get("tau") or 0.0)))
        else:
            out.append((float(m), TAP_GUARD, 0.0))
    return out if taus else [(m, g) for m, g, _ in out]


def _on_mode(fc, modes):
    return any(abs(fc - m) <= g for m, g in modes)


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
    modes = _modes(avoid)
    # at twice the tone a line keeps its full guard (see analyse: "pick_guard")
    modes2 = [(float(m["freq"]), float(m.get("guard2", m.get("guard", TAP_GUARD))))
              if isinstance(m, dict) else (float(m), TAP_GUARD) for m in (avoid or ())]
    if halves is not None:
        # at twice the tone, a toolhead line can't be told from its own skirt closer than
        # the window resolves (a Hann window of T s spreads a line +-2/T Hz): guard that much
        res = 1.0 / halves[3]
        modes2 = [(fa, max(g, res)) for fa, g in modes2]
    ringing2 = [(float(m["freq"]), float(m["guard2"])) for m in (avoid or ()) if isinstance(m, dict) and "guard2" in m]
    bad2 = np.zeros(len(f), bool)
    for fa, g in modes2:
        bad2 |= np.abs(f - fa) <= g
    bad = np.zeros(len(f), bool)
    for fa, g in modes:               # the toolhead ringing by itself is not a belt
        bad |= np.abs(f - fa) <= g
    strong, weak = 10.0 ** (STRONG_DB / 10.0), 10.0 ** (WEAK_DB / 10.0)
    # twice the tone: the bin at 2F and its neighbours (zero padding spreads a line);
    # the tap guard is for the tone only, a belt's 2F may sit on a toolhead mode
    k2 = 2 * sel
    s2 = np.maximum.reduce([snr[np.clip(k2 + d, 0, len(f) - 1)] for d in (-2, -1, 0, 1, 2)])
    # the toolhead's own ringing has nothing at twice it: near it, a tone only counts
    # with a clear line at twice it (so a belt tuned right onto it can still be read)
    s1 = np.where(bad[sel] & (s2 < strong), 0.0, snr[sel])
    # and where twice the tone is the toolhead's own ringing (or its skirt), that line
    # says nothing about a belt: it counts no more than the tone itself
    s2 = np.where(bad2[np.clip(k2, 0, len(f) - 1)], np.minimum(s2, s1), s2)
    score = np.log1p(s1) + np.log1p(s2)
    i = int(np.argmax(score))
    if s1[i] == 0.0 and s2[i] == 0.0:
        return None, None, None

    wo = max(1, int(3.0 / df))

    def own(j):
        """A line of its own at bin j, not the skirt of a strong line close by (a stiff
        toolhead rings loud enough for its skirt to stand above the noise 10 Hz away):
        the top within 1 Hz of it is the top within 3 Hz."""
        j = _near(f, p, float(f[j]), df, 1.0)
        return p[j] >= p[max(0, j - wo):j + wo + 1].max()

    def line(fc):
        j = _near(f, p, fc, df, 0.3)
        return 0.0 if j is None else float(snr[j])

    def sc(fc):
        return math.log1p(line(fc)) + math.log1p(line(2 * fc))

    def guarded(fc):
        return _on_mode(fc, modes)

    def guarded2(fc):
        return _on_mode(fc, modes2)

    def harmonic(fl):
        """fl and 2 fl ring like a belt's tone and twice it: the tone rings longer
        (True), shorter (False: not a belt's tone), or can't tell (None)."""
        if halves is None:
            return None
        if guarded2(2 * fl):
            return None               # twice it is the toolhead ringing: its own time, not the belt's
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
    if (lh >= lo + 2 * df and line(fk) >= strong and line(lh) >= weak and not guarded(lh)
            and own(_near(f, p, lh, df))):     # a line of its own, not a frame mode's skirt
        h = harmonic(lh)
        if h or (halves is None and target and lh >= flo):     # no timing: the target decides
            alt = lh
    if alt is None and 2 * fk <= min(hi, top) - 2 * df:
        # up only on evidence at 4x: then the line near 2fk has its own 2F, and fk
        # isn't its tone (a frame mode near F/2 is seldom exactly half)
        f2 = _peak(f, p, _near(f, p, 2 * fk, df), df)
        # (only within where the tone is looked for: the tone itself could never be picked
        # above fhi, so neither may a jump to it)
        if (f2 <= fhi and not guarded(f2) and line(f2) >= max(strong, line(fk)) and line(2 * f2) >= weak
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
    if bad[k] and (j is None or snr[j] < strong or not own(j)):
        return None, None, None       # the toolhead ringing by itself
    if guarded2(2 * fk) and (snr[k] < strong or not own(k)):
        return None, None, None       # only the toolhead ringing at "twice the tone": no tone of its own
    if j is not None and db2 > db1 and not _on_mode(2 * fk, ringing2):
        fk = _peak(f, p, j, df) / 2.0          # the sharper line: twice the tone (unless a toolhead line pulls it)
    if fk < lo + 2 * df or fk > hi - 2 * df:
        return None, db1, db2         # at the edge of what is searched: not a belt reading
    return fk, db1, db2


LONG_ON = 0.6             # Hz: a reading (with the lines fitted out) this close to a long-ringing line is that line
REFINE_NEAR = 4.0         # Hz: a long-ringing mode this close to the tone (or twice it) pulls the reading


_REFINED = {}


def _refine(axes, fs, a0, a1, fr, long):
    """The tone, with the long-ringing toolhead modes near it taken out: the ringing in
    the window is fitted as a belt (a decaying tone at F and at 2F) plus each mode near
    F or 2F (a decaying tone near the frequency and ring time the tap found; both are
    let move a little, a mode drifts with temperature), and the F that leaves least is
    the reading. A spectrum peak alone is pulled towards a mode that rings on next to it."""
    near = [(m, tau) for m, tau in long
            if min(abs(fr - m), abs(2 * fr - m)) <= REFINE_NEAR and abs(fr - m) > 0.3
            and abs(2 * fr - m) > 0.6]       # (right on twice the tone the two can't be told apart)
    if not near:
        return fr
    t = np.arange(a1 - a0) / fs
    y = np.column_stack([a[a0:a1] - a[a0:a1].mean() for a in axes])

    def tone(F, tau, k=1):
        e = np.exp(-k * t / max(tau, 0.02))
        return [e * np.cos(2 * math.pi * k * F * t), e * np.sin(2 * math.pi * k * F * t)]

    def resid(F, tb, ms):
        A = np.column_stack(tone(F, tb) + tone(F, tb, 2) + [c for m, tau in ms for c in tone(m, tau)])
        r = y - A @ np.linalg.lstsq(A, y, rcond=None)[0]
        return float(np.sum(r * r))

    ms = list(near)
    F, tb = fr, 0.2
    span = REFINE_NEAR - 1.0          # the peak is pulled by up to about this much
    # the belt is never right on a mode (that is read as the mode itself)
    coarse = [fr + d for d in np.arange(-span, span + 1e-6, 0.1)
              if all(abs(fr + d - m) > 0.5 for m, _ in near)]
    # first the belt and the nearest mode together, coarse (one at a time can get stuck:
    # a mode a little off pulls the belt, which then holds the mode there)
    j0 = min(range(len(near)), key=lambda j: abs(fr - near[j][0]))
    best = None
    for dm in np.arange(-0.4, 0.4001, 0.1):
        trial = ms[:j0] + [(near[j0][0] + dm, near[j0][1])] + ms[j0 + 1:]
        for x in coarse:
            for b in (0.1, 0.2, 0.4):
                r = resid(x, b, trial)
                if best is None or r < best[0]:
                    best = (r, x, b, trial)
    F, tb, ms = best[1], best[2], best[3]
    for it in range(3):
        # the belt, for the modes as they are now, then each mode for the belt
        grid = [F + d for d in np.arange(-0.3, 0.3001, 0.02)]
        best = min((resid(x, b, ms), x, b) for x in grid for b in (0.1, 0.2, 0.4))
        F, tb = best[1], best[2]
        # each mode, for the belt as it is now
        for j, (m0, tau0) in enumerate(near):
            cands = [(m0 + d, tau0 * k) for d in np.arange(-0.4, 0.4001, 0.02) for k in (0.6, 1.0, 1.6)]
            cands.append(ms[j])
            ms[j] = min(cands, key=lambda c: resid(F, tb, ms[:j] + [c] + ms[j + 1:]))[0:2]
    if abs(F - fr) >= span - 0.05:
        return fr                     # no clear minimum: keep the spectrum's reading
    # the minimum to a fraction of a step
    r = [resid(F + d, tb, ms) for d in (-0.02, 0.0, 0.02)]
    den = r[0] - 2 * r[1] + r[2]
    off = 0.5 * (r[0] - r[2]) / den if den > 0 else 0.0
    return float(F + max(-0.5, min(0.5, off)) * 0.02)


ATTACK_DB = 6.0           # dB: a pluck's tone is at least this much louder just after its onset than just before
DECAY_DB = 3.0            # dB: a tone still dying from the last pluck is at least this much louder 0.7 s earlier


def _levels(axes, fs, on, fr):
    """Level of the tone at fr in three 0.3 s windows: 1 s before the onset, just before
    it and just after it (None where the recording doesn't reach)."""
    n = int(0.3 * fs)             # long enough that a loud toolhead line 10+ Hz away doesn't leak in
    a0, b0 = on + int(SKIP * fs), on - n - int(0.01 * fs)
    e0 = b0 - int(0.7 * fs)
    t = np.arange(n) / fs
    w = np.hanning(n)
    c, s_ = w * np.cos(2 * math.pi * fr * t), w * np.sin(2 * math.pi * fr * t)

    def level(i):
        if i < 0 or i + n > len(axes[0]):
            return None
        return sum(np.dot(a[i:i + n] - a[i:i + n].mean(), c) ** 2 + np.dot(a[i:i + n] - a[i:i + n].mean(), s_) ** 2
                   for a in axes)
    return level(e0), level(b0), level(a0)


def _fresh(axes, fs, on, fr):
    """The tone at fr comes in at this onset (a pluck), rather than ringing on from
    before it (the last pluck still ringing when a knock came)."""
    _, before, after = _levels(axes, fs, on, fr)
    return before is None or after is None or after >= 2.0 * before


def _rises(axes, fs, on, fr, db=ATTACK_DB):
    """The tone at fr starts at this onset: db louder just after it than just before (a
    pluck hidden in a loud fan's line lifts it by less: 3 dB is asked there)."""
    _, before, after = _levels(axes, fs, on, fr)
    return before is None or after is None or after >= 10 ** (db / 10.0) * before


def _hum(axes, fs, on, fr):
    """The tone at fr hums through this onset (a fan): no attack (a pluck starts its tone:
    real plucks 12-40 dB up) and no decay before it (the last pluck dying away). A fan
    is on or off: the same level before, at and after the onset."""
    earlier, before, after = _levels(axes, fs, on, fr)
    if before is None or after is None:
        return False
    if after >= 10 ** (ATTACK_DB / 10.0) * before:
        return False
    return earlier is None or earlier < 10 ** (DECAY_DB / 10.0) * before


def _knock(f, df, halves, fr, d1, d2, avoid):
    """A knock on the toolhead or the frame, not a pluck: a clear tone with no pull at
    twice it (or only the toolhead's own ringing there, learned from a tap) that dies
    fast. Belts ring long, so this needs no tap learned first."""
    on_mode = _on_mode(2 * fr, _modes(avoid))
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
    if t.ndim != 1 or any(a.ndim != 1 or len(a) != len(t) for a in axes):
        raise ValueError("recording must contain equally sized one-dimensional time and acceleration arrays")
    if not np.isfinite(t).all() or any(not np.isfinite(a).all() for a in axes):
        raise ValueError("recording contains non-finite samples")
    if len(t) > 1 and not np.all(np.diff(t) > 0):
        raise ValueError("recording timestamps must increase strictly")
    if len(t) < 100:
        return None
    fs = (len(t) - 1) / (t[-1] - t[0])
    if fs <= 2 * SEARCH[0] / 0.45:
        raise ValueError("accelerometer sample rate is too low for belt analysis")
    q = None
    if quiet is not None:
        if len(quiet) != 3:
            raise ValueError("quiet recording must contain three acceleration arrays")
        qa = [np.asarray(a, float) for a in quiet]
        if any(a.ndim != 1 or len(a) != len(qa[0]) or not np.isfinite(a).all() for a in qa):
            raise ValueError("quiet recording contains invalid acceleration samples")
        if len(qa[0]) > int(0.5 * fs):
            q = qa
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


def analyse(t, ax, ay, az, target=None, expect=None, quiet=None, avoid=(), other=None, prev=None, free=False):
    """Belt tone from a recording with several plucks. quiet: (ax, ay, az) of a short
    recording without plucks, to tell fans and motors from the belt. target: the
    target tone (bounds where the tone is looked for). avoid: the toolhead's own
    ringing (the tap's "ring", or plain Hz). other: the other belt's last reading. prev: this belt's
    last reading (decides between a tone and its octave when nothing else can).

    Each pluck is accepted or thrown out; only accepted plucks are averaged. Returns a
    dict: freq (None unless the series is good), plucks (every pluck heard: freq, dB at
    the tone and at twice it, ok, why), used, heard, verdict (good, again, spread,
    spans, none), note."""
    for name, value in (("target", target), ("other", other), ("prev", prev)):
        if value is not None and (not math.isfinite(value) or value <= 0):
            raise ValueError("%s frequency must be positive and finite" % name)
    if expect is not None and (isinstance(expect, bool) or not isinstance(expect, (int, np.integer)) or expect < 1):
        raise ValueError("expected pluck count must be a positive integer")
    avoid = tuple(avoid)
    try:
        modes = _modes(avoid, taus=True)
    except (TypeError, ValueError, KeyError) as error:
        raise ValueError("invalid toolhead modes") from error
    if any(not math.isfinite(freq) or freq <= 0
           or not math.isfinite(guard) or guard < 0
           or not math.isfinite(tau) or tau < 0 for freq, guard, tau in modes):
        raise ValueError("toolhead modes require a positive finite frequency and finite nonnegative guard/tau")
    st = _setup(t, ax, ay, az, quiet)
    metadata = {"sample_count": len(t), "sample_rate": None, "duration": 0.0}

    hum_at, quiet_at = [], []

    def out(verdict, note, plucks=(), freq=None, used=0):
        frequencies = [p["freq"] for p in plucks if p["ok"] and p["freq"] is not None]
        # a new hum on the belt's note (within SPAN of the target or of its last reading) hides
        # the belt: the page says to turn fans off. Hums elsewhere are masked and harmless.
        # Only said when most plucks failed: plucks heard clearly but too few are not a fan's doing.
        near = [x for x in (target, prev) if x]
        hum = [x for x in hum_at if any(abs(x - y) <= SPAN * y for y in near)] if near else \
            [x for x in hum_at if x <= F_MAX]
        # a steady line (in the background too) that hid most plucks: the belt sits on it
        hid = [pk.get("hidden") for pk in plucks if pk.get("hidden")]
        for x in sorted(set(hid)):
            if 2 * hid.count(x) >= len(plucks) and all(abs(x - y) > 1.0 for y in hum):
                hum.append(x)
        # or one right beside the few plucks that got through it (the rest were lost in it)
        if freq is None and len(frequencies) < MIN_GOOD:
            for x in quiet_at + list(hum_at):
                if any(abs(y - x) <= 2.0 or abs(y - 2 * x) <= 2.0 for y in frequencies) \
                        and all(abs(x - y) > 1.0 for y in hum):
                    hum.append(x)
        elif 2 * len(frequencies) >= len(plucks) and plucks:
            hum = []
        if hum and freq is None:
            note = "steady hum at %s Hz on the belt's note (a fan?): turn fans off, then measure again" % (
                " and ".join("%.0f" % x for x in hum))
        return {"freq": freq, "plucks": list(plucks), "used": used, "heard": len(plucks), "hum": hum,
                "verdict": verdict, "note": note, "metadata": dict(metadata),
                "spread": round(max(frequencies) - min(frequencies), 3) if frequencies else None,
                "stddev": round(float(np.std(frequencies)), 3) if frequencies else None}
    if st is None:
        return out("none", "no data from the accelerometer")
    axes, fs, q = st
    metadata.update(sample_rate=round(float(fs), 3), duration=round(float(t[-1] - t[0]), 3))
    lo, hi = SEARCH[0], min(SEARCH[1], 0.45 * fs / 2.0)
    mask = steady_lines(q, fs) if q is not None else None
    quiet_at = list(getattr(mask, "lines", []))
    bmask = getattr(mask, "wide", None)       # for the band level: short windows, wide lines
    base = float(np.median(_band(q, fs, lo, hi, bmask))) if q is not None else None
    env = _band(axes, fs, lo, hi, bmask)
    onsets = find_plucks(axes, fs, lo, hi, bmask, base, env)
    # a hum that started after the background was recorded (a fan came on): never the belt
    hum, hum_at = steady_in(axes, fs, q, onsets)
    if hum is not None:
        mask = _either(mask, hum)
        bmask = _either(bmask, hum)
        env = _band(axes, fs, lo, hi, bmask)
        onsets = find_plucks(axes, fs, lo, hi, bmask, base, env)
    # a toolhead that rings long (stiff, all metal) may still ring from a knock when the
    # next pluck comes: look for plucks beside its own lines too
    longm = [(m, g) for m, g, tau in _modes(avoid, True) if tau >= KNOCK_TAU]
    ringing = [(m, tau) for m, g, tau in _modes(avoid, True) if tau >= KNOCK_TAU]
    # a line that rings on is sharp and is fitted out of each pluck (_refine), so it only
    # needs a narrow guard while the tone is picked: a belt 1-2 Hz from it can be read
    pick_avoid = [dict(m, guard=0.3, guard2=m.get("guard", TAP_GUARD)) if isinstance(m, dict) and (m.get("tau") or 0) >= KNOCK_TAU else m
                  for m in (avoid or ())]
    if longm:
        extra, _ = _onsets_beside(axes, fs, lo, hi, bmask, longm, q)
        onsets = _merge_onsets(onsets, extra, fs, env)
    if not onsets:
        return out("none", "no pluck heard")
    if q is None and onsets[0] >= int(0.6 * fs):
        # no quiet recording: the stretch before the first pluck is the next best
        q = [a[:onsets[0] - int(0.05 * fs)] for a in axes]
        sl = steady_lines(q, fs)
        quiet_at = list(getattr(sl, "lines", []))
        mask = _either(sl, hum)
        bmask = _either(getattr(sl, "wide", None), hum)
    nfft = _nfft(fs)
    f = np.fft.rfftfreq(nfft, 1.0 / fs)
    df = f[1] - f[0]
    cache = {}
    plucks = []
    # a second attack in a pluck's window (the band level up 3x within 40 ms, the way
    # find_plucks finds an onset, after this pluck's own attack has peaked): two plucks
    # in one window don't ring like one, so its lines can't be judged
    bk = max(1, int(0.04 * fs))
    rise = np.zeros(len(env), bool)
    rise[bk:] = (env[bk:] > max(5.0 * ((float(np.percentile(env, 10)) if base is None else base) or 1e-9), 0.03 * float(env.max()))) \
        & (env[bk:] > 3.0 * env[:-bk])
    for on, a0, a1 in _windows(axes, fs, onsets):
        k = onsets.index(on)
        judge_hum = k == 0 or on - onsets[k - 1] >= HUM_GAP * fs
        sp = _spectra(axes, q, fs, nfft, f, mask, a0, a1, cache)
        if sp is None:
            continue
        p, nz, halves = sp
        fr, d1, d2 = pluck_freq(f, p, nz, df, lo, hi, pick_avoid, target, prev, halves)
        tone_only = False
        if fr is not None and ringing:
            # a toolhead line that rings on next to the tone pulls the spectrum's peak:
            # fit it out (the live count analyses the recording again every second, so a
            # pluck heard to the end of its window is fitted once)
            key = (a1 - a0, float(axes[0][a0]), float(axes[1][a0]), float(axes[2][a1 - 1]), round(fr, 2),
                   tuple(ringing))
            if key not in _REFINED:
                if len(_REFINED) > 200:
                    _REFINED.clear()
                _REFINED[key] = _refine(axes, fs, a0, a1, fr, ringing)
            fr = _REFINED[key]
            if any(abs(fr - m) <= LONG_ON for m, _ in ringing):
                fr = None                     # the toolhead's own line ringing on, not a belt
            elif any(abs(2 * fr - m) <= TAP_GUARD for m, _ in ringing):
                d2 = None                     # twice the tone is that line: the tone alone decides
                tone_only = True
        pk = {"t": round(float(on / fs), 2), "freq": None if fr is None else round(fr, 2),
              "db": None if d1 is None else round(d1, 1), "db2": None if d2 is None else round(d2, 1),
              "ok": False, "why": ""}
        if fr is not None and bmask is not None and bool(bmask(np.array([2 * fr]))[0]):
            # twice the tone sits on a steady line (a fan, the sensor's own): it can't
            # be told from the noise there, so the tone alone decides
            d2 = None
            pk["db2"] = None
        if fr is None:
            pk["why"] = "no belt tone"
        elif d2 is None:
            if d1 < STRONG_DB:
                pk["why"] = "too weak"
            elif _tau(halves, f, df, fr) == 0.0:
                pk["why"] = "no belt tone"        # no line of its own there
            elif tone_only and not _fresh(axes, fs, on, fr):
                pk["why"] = "a knock, not a belt"  # the last pluck still ringing: the knock rang the toolhead
            elif _knock(f, df, halves, fr, d1, d1, avoid):
                pk["why"] = "a knock, not a belt"
            elif judge_hum and _hum(axes, fs, on, fr):
                pk["why"] = "steady hum, not a pluck"
            else:
                pk["ok"] = True
        elif _knock(f, df, halves, fr, d1, d2, avoid):
            pk["why"] = "a knock, not a belt"
        elif max(d1, d2) < STRONG_DB or min(d1, d2) < WEAK_DB:
            pk["why"] = "too weak"
        elif judge_hum and _hum(axes, fs, on, fr):
            pk["why"] = "steady hum, not a pluck"
        else:
            pk["ok"] = True
        if not pk["why"].startswith("a knock"):
            # its tone hidden in a steady line (a fan on the belt's note): the pluck starts
            # that line's level (a fan alone hums the same before and after). A thrown-out pluck
            # that did is hidden, not missing; a reading just beside the line, where the
            # spectrum still climbs towards it, is the hidden tone's skirt.
            for x in quiet_at + list(hum_at):
                if pk["ok"]:
                    if not (SPREAD < abs(fr - x) <= 6.0) or \
                            p[np.abs(f - x) <= 0.5].max() <= p[int(round(fr / df))]:
                        continue
                if _rises(axes, fs, on, x, 3.0):
                    pk["ok"] = False
                    pk["hidden"] = x
                    pk["why"] = "hidden by a steady hum at %.0f Hz" % x
                    break
        if pk["ok"]:
            s0 = max(a0, on + int(np.argmax(env[on:on + int(0.1 * fs)]))) + bk
            if s0 < a1:
                # and it climbs above where this pluck already was, allowing for a slow decay
                # (1 s): two belts beating (the other one ringing along) dip and come back, but
                # never above the level before the dip, as both decay
                b0 = min(on, a0)                  # from the attack, which a0 may skip
                k = np.arange(a1 - b0) / fs
                top = np.exp(-k) * np.maximum.accumulate(env[b0:a1] * np.exp(k))
                # Only after the first 0.2 s: an attack still rising through the first beat
                # makes that beat low, and the next one would look new (two plucks that close
                # ring as one anyway)
                s1 = max(s0, b0 + int(0.2 * fs))
                hit = np.flatnonzero(rise[s1:a1]) + s1
                if any(env[i] > 1.1 * top[i - bk - b0] for i in hit):
                    pk["ok"], pk["why"] = False, "two plucks at once"
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
    if free:
        # free tune: the belts change between plucks on purpose, so each clear pluck stands alone.
        # Nothing else checks it, so it must lie where the tone is looked for (F_BAND x target):
        # an octave step (pluck_freq) may leave that band; there it is no reading of this belt
        if target:
            flo, fhi = F_BAND[0] * target, min(F_BAND[1] * target, max(F_MAX, 1.2 * target))
            for x in ok:
                if not flo <= x["freq"] <= fhi:
                    x["ok"], x["why"] = False, "outside %.0f-%.0f Hz" % (flo, fhi)
            ok = [x for x in plucks if x["ok"]]
        return out("free" if ok else "none", "%d of %d plucks clear" % (len(ok), len(plucks)), plucks,
                   freq=ok[-1]["freq"] if ok else None, used=len(ok))
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
    res = out("good", "%d of %d plucks counted" % (len(ok), len(plucks)), plucks,
              freq=round(float(np.mean(fr)), 1), used=len(ok))
    return res


def _width(f, p, k, df):
    """Full width (Hz) of the line at bin k where it is 6 dB down (a quarter of its power)."""
    lvl = p[k] / 4.0
    i = k
    while i > 0 and p[i] > lvl:
        i -= 1
    j = k
    while j < len(p) - 1 and p[j] > lvl:
        j += 1
    return (j - i) * df


def analyse_tap(t, ax, ay, az, quiet=None):
    """The toolhead's own ringing from a light tap: the strongest lines (up to 5; a hard
    tap brings out more of them) that stand clearly above the noise and are not a belt
    ringing along: no line at twice them or at half them.

    Toolheads differ a lot: plastic and loosely mounted ones ring briefly in broad lines,
    stiff all metal ones ring long in sharp lines. How long a line rings doesn't tell it
    from a belt (belts ring 0.1-0.2 s, and so may a toolhead), so a line is never thrown
    out for ringing long; the harmonics tell. Each line's ring time and width are
    learned, and each line is guarded as wide as it is.

    Returns modes (Hz), ring (per line: freq, tau = s to 1/e, width = Hz at -6 dB,
    guard = Hz either side ignored as a belt tone), tau (the longest), note."""
    st = _setup(t, ax, ay, az, quiet)
    if st is None:
        return {"modes": [], "ring": [], "tau": None, "note": "no data from the accelerometer"}
    axes, fs, q = st
    lo, hi = SEARCH[0], min(SEARCH[1], 0.45 * fs)
    onsets = find_plucks(axes, fs, lo, hi, None, None)
    if not onsets:
        return {"modes": [], "ring": [], "tau": None, "note": "no tap heard"}
    nfft = _nfft(fs)
    f = np.fft.rfftfreq(nfft, 1.0 / fs)
    df = f[1] - f[0]
    on, a0, a1 = next(_windows(axes, fs, onsets[:1]), (None, None, None))
    if on is None:
        return {"modes": [], "ring": [], "tau": None, "note": "the tap was too close to the end"}
    sp = _spectra(axes, q, fs, nfft, f, None, a0, a1, {})
    if sp is None:
        return {"modes": [], "ring": [], "tau": None, "note": "not enough background data for the tap"}
    modes, ring = _tap_lines(f, sp, df, lo, hi)
    longest = max((r["tau"] for r in ring), default=None)
    return {"modes": modes, "ring": ring, "tau": longest,
            "note": "" if modes else "the tap rang nothing clearly"}


def _tap_lines(f, sp, df, lo, hi, rises=None):
    """The toolhead's own lines in one tap's window (see analyse_tap), strongest first."""
    p, nz, halves = sp
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
    modes, ring = [], []
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
        # the 2F), or not dying at all (a steady line)
        # (a line of its own: not the skirt of a strong toolhead line close by)
        j2, jh = _near(f, p, 2 * fk, df), _near(f, p, fk / 2.0, df)

        def own(j):
            # (and it starts with the tap: a fan's line at twice the toolhead's note doesn't count)
            return j is not None and _db(snr[j]) >= WEAK_DB and p[j] >= p[max(0, j - w):j + w + 1].max() \
                and (rises is None or rises(float(f[j])))
        if own(j2):
            continue
        if fk / 2.0 >= lo and own(jh):
            continue
        tau = _tau(halves, f, df, fk)
        if math.isinf(tau):
            continue
        wd = _width(f, p, k, df)
        modes.append(round(fk, 1))
        ring.append({"freq": round(fk, 1), "tau": round(float(tau), 2), "width": round(float(wd), 1),
                     "guard": round(float(min(GUARD_MAX, max(TAP_GUARD, wd / 2.0))), 1)})
    return modes, ring


# ---------------- the toolhead's fingerprint: three light taps on the same spot ----------------
# One tap finds the toolhead's note; three that agree show it is steady and that it is the
# toolhead, not something else that happened to ring. A tap only counts when it is heard,
# alone (not inside the last tap's window: a double tap muddles the ringing) and rings
# loudest at the same note as the first good tap, within that note's own width (the same
# spot: another spot brings out other notes). No fixed numbers: each toolhead sets its own. How hard is not judged: even a light finger tap on
# a stiff extruder hits a +-16 g sensor's limit for an instant (seen on a Nebula 370), and
# the ringing after it, which is all that is used, is fine.
TAP_NEED = 3              # good taps for a fingerprint
TAP_FAILS = 9             # bad taps: stop and tell the user to look for what shakes the printer


def analyse_taps(t, ax, ay, az, quiet=None, need=TAP_NEED):
    """Judge each tap in a recording and learn the toolhead's fingerprint from the first
    `need` good ones. Returns taps (each: t, ok, why, freq = its loudest note), good (count),
    bad (count), main (the toolhead's note: freq, guard, ...), modes, ring and tau (as
    analyse_tap), and note. A tap whose window is not complete yet is left for later."""
    out = {"taps": [], "good": 0, "bad": 0, "main": None, "modes": [], "ring": [], "tau": None, "note": ""}
    st = _setup(t, ax, ay, az, quiet)
    if st is None:
        return out
    axes, fs, q = st
    t0 = float(t[0])
    lo, hi = SEARCH[0], min(SEARCH[1], 0.45 * fs)
    onsets = find_plucks(axes, fs, lo, hi, None, None)
    nfft = _nfft(fs)
    f = np.fft.rfftfreq(nfft, 1.0 / fs)
    df = f[1] - f[0]
    n_all = len(axes[0])
    good, ref, prev, cache = [], None, None, {}
    steady = []                           # steady lines (fans): looked for when a tap is too soft

    def hum_lines():
        if not steady:
            sl = steady_lines(q, fs) if q is not None else None
            steady.extend(list(getattr(sl, "lines", [])) + list(steady_in(axes, fs, q, onsets)[1]) or [None])
        return [x for x in steady if x is not None]
    for i, on in enumerate(onsets):
        nxt = onsets[i + 1] if i + 1 < len(onsets) else None
        if nxt is None and on + int((SKIP + WINDOW) * fs) > n_all:
            break                         # still ringing at the end: judged on the next look
        tap = {"t": round(float(t[on]) - t0, 2), "ok": False, "why": "", "freq": None}
        w = next(_windows(axes, fs, [on] + ([nxt] if nxt is not None else [])), None)
        sp = None if w is None else _spectra(axes, q, fs, nfft, f, None, w[1], w[2], cache)
        if prev is not None and on - prev < (SKIP + WINDOW) * fs:
            tap["why"] = "two taps at once"
        elif w is None:
            tap["why"] = "two taps at once"
        elif sp is None:
            tap["why"] = "not enough background"
        else:
            modes, ring = _tap_lines(f, sp, df, lo, hi, lambda fr, on=on: _rises(axes, fs, on, fr))
            # a tap starts the toolhead's ringing: a line already there just before (a fan
            # humming) is not the toolhead
            ring = [r for r in ring if _rises(axes, fs, on, r["freq"])]
            modes = [r["freq"] for r in ring]
            if not modes:
                tap["why"] = "too soft"
                # unless it rang a steady line (a fan on the toolhead's note): hidden by it
                for x in hum_lines():
                    if _rises(axes, fs, on, x, 3.0):
                        tap["why"] = "hidden by a steady hum at %.0f Hz" % x
                        break
            else:
                tap["freq"] = ring[0]["freq"]
                if ref is not None and abs(ring[0]["freq"] - ref["freq"]) > ref["width"] / 2.0:
                    tap["why"] = "another spot"
                else:
                    ref = ref or ring[0]
                    good.append(ring)
                    tap["ok"] = True
        prev = on
        out["taps"].append(tap)
        if len(good) >= need:
            break
    out["good"] = len(good)
    out["bad"] = sum(1 for x in out["taps"] if not x["ok"])
    if not good:
        out["note"] = "no tap heard" if not out["taps"] else "no good tap"
        return out
    # the fingerprint: the main note, and the other lines that rang in at least two good taps
    rings = sorted(((r, k) for k, g in enumerate(good) for r in g), key=lambda x: x[0]["freq"])
    groups = []
    for r, k in rings:
        if groups and r["freq"] - groups[-1][-1][0]["freq"] <= 2 * TAP_GUARD:
            groups[-1].append((r, k))
        else:
            groups.append([(r, k)])
    ring = []
    for g in groups:
        has_main = any(r is gd[0] for r, k in g for gd in good)
        if len({k for _, k in g}) < min(2, len(good)) and not has_main:
            continue
        fr = float(np.median([r["freq"] for r, _ in g]))
        ring.append({"freq": round(fr, 1), "tau": max(r["tau"] for r, _ in g),
                     "width": max(r["width"] for r, _ in g), "guard": max(r["guard"] for r, _ in g)})
    mains = [g[0]["freq"] for g in good]
    mf = float(np.median(mains))
    main = min(ring, key=lambda r: abs(r["freq"] - mf))
    ring.sort(key=lambda r: r is not main)
    out.update(main=main, ring=ring, modes=[r["freq"] for r in ring],
               tau=max(r["tau"] for r in ring))
    return out


def toolhead_knock(t, ax, ay, az, at, main, belt, quiet=None, until=None):
    """Was the event at `at` (s after t[0]) a knock on the toolhead rather than a pluck? Its
    note (main, from the setup taps) must be clearly heard (STRONG_DB over the noise) and
    louder than the belt's note (belt: Hz, belt A's reading). until: when the next event
    starts (s), so a double tap's second knock isn't heard in the first one's window.
    Returns (yes, dB of the toolhead note, dB of the belt note)."""
    st = _setup(t, ax, ay, az, quiet)
    if st is None or not main:
        return False, None, None
    axes, fs, q = st
    on = int(round(at * fs))
    w = next(_windows(axes, fs, [on] + ([int(round(until * fs))] if until is not None else [])), None)
    if w is None:
        return False, None, None
    nfft = _nfft(fs)
    f = np.fft.rfftfreq(nfft, 1.0 / fs)
    sp = _spectra(axes, q, fs, nfft, f, None, w[1], w[2], {})
    if sp is None:
        return False, None, None
    p, nz, _ = sp
    snr = 10.0 * np.log10(np.maximum(p / np.maximum(nz, 1e-30), 1e-30))
    near = np.abs(f - main["freq"]) <= main["guard"]
    if not np.any(near):
        return False, None, None
    dm = float(np.max(snr[near]))
    # a knock starts the toolhead's note; a fan humming on (or beside) that note was there
    # just before the event as well: no attack, no knock
    _, before, after = _levels(axes, fs, on, main["freq"])
    rise = None if before is None or after is None else _db(after / max(before, 1e-30))
    if rise is not None and rise < ATTACK_DB:
        return False, round(dm, 1), None
    # the belt rings as a line: a peak within SPREAD of its reading, not the slope of the
    # toolhead's broad note beside it. A belt inside the toolhead's guard can't be told apart.
    do = -99.0
    if belt and abs(belt - main["freq"]) > main["guard"] + SPREAD:
        k1 = max(1, int(1.0 / (f[1] - f[0])))
        for k in np.flatnonzero(np.abs(f - belt) <= SPREAD):
            if k1 <= k < len(p) - k1 and p[k] >= p[k - k1:k + k1 + 1].max():
                do = max(do, float(snr[k]))
    return dm >= STRONG_DB and dm > do, round(dm, 1), round(do, 1)


def plan(b):
    """What to tell the user after both belts were measured. b is the belt state
    (target, tol, A, B = belt A and B (the user picks which is which), last_belt: the one changed last); returns
    (advice, updates for b). Which belt is which doesn't matter: they are tensioned
    equally.
    Just which way, and one belt per round, taking turns, so the two are brought up
    together and never end far apart."""
    tgt, tol = b["target"], b["tol"]
    fa = (b.get("A") or {}).get("freq")
    fb = (b.get("B") or {}).get("freq")
    upd = {}
    if fa is None or fb is None:
        return "", upd
    need = b.get("plucks", 5)
    def enough(r):
        return r.get("enough", (r.get("used") or 0) >= need)
    good = {k: abs(f - tgt) <= tol and enough(b.get(k) or {})
            for k, f in (("A", fa), ("B", fb))}
    upd["belt_good"] = good
    upd["done"] = good["A"] and good["B"]
    if upd["done"]:
        return "Both belts good. Done.", upd
    off = [k for k, f in (("A", fa), ("B", fb)) if abs(f - tgt) > tol]
    if not off:
        return "On target, but not from %d good plucks each: measure both again" % need, upd
    last = b.get("last_belt")
    # take turns: the belt not changed last, if it still needs it
    if len(off) == 1:
        k = off[0]
    elif last in ("A", "B"):
        k = "B" if last == "A" else "A"
    else:
        k = "A"                        # belt A first
    f = fa if k == "A" else fb
    upd["last_belt"] = k
    return "%s belt %s a little: %.1f Hz %s. Then measure both again" % (
        "Tighten" if f < tgt else "Loosen", k, abs(f - tgt),
        "low" if f < tgt else "high"), upd
