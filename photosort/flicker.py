"""Light flicker in video: LED and fluorescent light on mains power pulses at twice the mains frequency (100 Hz
in India, 120 Hz on 60 Hz grids). A camera samples that at its frame rate, so the pulse folds down to a slow
beat: 100 Hz at 24 fps shows as 4 Hz, at 30 fps as 10 Hz, 120 Hz at 25 fps as 5 Hz. A shutter that is a whole
number of light periods (1/100, 1/50) averages it out; a rolling shutter turns what is left into bands that
crawl up or down the frame.

Only ever reads the source file. risk() needs metadata alone; read_run() decodes one short run of consecutive
frames at 160x96, because the stored sample frames are seconds apart and cannot show a 4 Hz beat; analyse() is
pure numpy and cv2; check_clip() ties them together and never raises.

Every threshold below was calibrated on synthetic frames and synthetic clips only, never on real footage shot
under real lights. Expect to retune them on the first real shoots. Mains frequency is all this looks for:
PWM dimmers, LED walls and screens run at their own rates and are not detected unless they land on an alias."""
from __future__ import annotations
import re, subprocess, time
from pathlib import Path
import cv2
import numpy as np
from . import video as _video
from .video import VideoUnreadable

MAINS = (50, 60)
RUN_COLS, RUN_ROWS = 160, 96    # a fixed size, aspect ignored: tiles and rows only need counting, and it spares a probe
RUN_SECONDS = 3.0               # 0.33 Hz bins, enough to tell a 4 Hz line from the slow drift around it
MAX_RUN_FRAMES = 240            # the decode cap: 3 s at 80 fps, 2 s at 120 fps. 240 frames is 7 MB of grey
RUN_TIMEOUT = 30                # seconds for one run
MAX_RUNS = 3
CLIP_BUDGET_S = 45.0            # no further run starts once a clip has cost this much
MIN_FRAMES = 16                 # 2.7 cycles of the slowest common beat (4 Hz at 24 fps)

SHUTTER_TOL = 0.05              # exposure x light frequency this close to a whole number cancels the pulse
LOCK_HZ = 0.05                  # a beat below this is frame-locked: every frame samples the same point of the pulse
MIN_ALIAS_HZ = 0.75             # below this a beat cannot be told from drift in a 3 s run (120 Hz at 23.976 is 0.12 Hz)
MIN_CYCLES = 2.5                # a span must hold this many cycles of a beat before it can be tested for it
SEARCH_HZ = 1.0                 # the grid wanders by up to 0.5 Hz and the light by twice that
LOW_HZ = 0.7                    # bins under this are what the detrend left behind
FLANK_HZ = 3.0                  # the noise floor is measured this far either side of the search window. It has to
                                # be local: a brightness step falls as 1/f and beats any floor taken from the top bins
TILE_ROWS, TILE_COLS = 6, 8
MIN_STATIC_TILES = 0.3          # share of tiles that must be static, exposed and usable, else the run says unknown
SNR_MIN = 5.0                   # per-tile peak over floor. By the Rayleigh tail, noise passes about one tile in a million
TILE_MAJORITY = 0.5             # more than this share of static tiles must show the peak
COHERENCE_MIN = 0.7             # phase agreement along a tile row. Light hits a whole sensor row at one instant, so
                                # phases agree along a row and only drift down the frame; random phases over 8 tiles give 0.35
HALVES_MIN = 0.4                # the weaker half of the span against the stronger: a real beat lasts, a burst does not
COMB_MIN = 0.6                  # a line at half or a third of the beat this strong means a comb, not one pure line
MIN_STRENGTH = 0.004            # modulation depth below this (0.4%) is under what anyone sees
BAND_STRIPS = 4                 # column strips the row profiles are taken over
BAND_MAX_CYCLES = 16            # cycles per frame height searched. A 30 ms readout at 120 Hz is under 4
BAND_SNR = 6.0                  # higher than SNR_MIN: the band search covers far more bins
BAND_ASYMMETRY = 2.0            # a crawling band has energy on one sign of the beat only. Pulsing through a tone
                                # curve leaves a scene-shaped standing pattern with both signs equal
MOVE_PX = 0.5                   # frame-to-frame shift, at 160 px wide, that makes a pair unusable
DRIFT_PX = 3.0                  # total walk across a span. Catches the slow pan that never trips MOVE_PX
DRIFT_CHECKS = 6                # frames of the span compared with its first
MOTION_RESPONSE = 0.3           # measured: noise alone stays under 0.12, faint texture is over 0.8. Below this, nothing to shift
MOTION_BLUR = 5.0               # px, the high-pass ahead of the correlation
CUT_BLOCK = 8
CUT_REACH = 8                   # frames either side a difference is judged against
CUT_SIGMAS = 6.0                # a frame difference this many deviations over the median is a cut or a light switching
CUT_FLOOR = 0.15                # the deviation is never taken as less than this share of the median
CUT_MIN = 1.0 / 255             # nor is a jump under one 8-bit level ever a cut
LUMA_LO, LUMA_HI = 0.04, 0.96   # tiles out here are crushed or clipped and carry no flicker
FRAME_GAP_TOL = 0.15            # a frame interval this far off the median breaks a run: variable frame rate phones
SLOWMO_TOL = 0.05               # capture and format rates this far apart mean the clip was shot slow motion

def _result(flicker: str, reason: str, hz: float | None = None, strength: float = 0.0) -> dict:
    return {"flicker": flicker, "hz": None if hz is None else round(float(hz), 2), "strength": round(float(strength), 4), "reason": reason}

def risk(fps: float, exposure_s: float | None = None, mains: tuple[int, ...] = MAINS) -> dict:
    """What the metadata predicts, nothing decoded. Per mains frequency: light_hz, alias_hz (the beat at this
    frame rate), locked (the beat is zero: frames all sample one point of the pulse, so no pulsing, though a
    fixed band can still sit in the frame), testable (long enough a beat for analyse to look for), and
    shutter_cancels, with None when the exposure is not known, which is not the same as False. at_risk is True
    when some mains would beat visibly, False when none would, None when the frame rate is unusable."""
    if not fps or fps <= 0:
        return {"fps": fps, "exposure_s": exposure_s, "mains": {}, "at_risk": None}
    out = {}
    for m in mains:
        light = 2.0 * m
        alias = abs(light - round(light / fps) * fps)
        cancels = None
        if exposure_s and exposure_s > 0:
            periods = exposure_s * light
            cancels = round(periods) >= 1 and abs(periods - round(periods)) <= SHUTTER_TOL
        out[m] = {"light_hz": light, "alias_hz": round(alias, 3), "locked": alias < LOCK_HZ,
                  "testable": alias >= MIN_ALIAS_HZ, "shutter_cancels": cancels}
    return {"fps": fps, "exposure_s": exposure_s, "mains": out,
            "at_risk": any(not v["locked"] and not v["shutter_cancels"] for v in out.values())}

def read_run(path: Path, start_s: float, seconds: float = RUN_SECONDS, key: tuple[str, str] | None = None,
             fps: float | None = None, max_frames: int = MAX_RUN_FRAMES) -> tuple[np.ndarray, float] | None:
    """Consecutive frames from start_s as (frames, fps): float32 (n, 96, 160) in 0..1, and the rate measured from
    the frames' own timestamps, falling back to fps when they cannot be read. None when ffmpeg gives nothing or
    times out; raises VideoUnreadable when ffmpeg is missing, as sample_frames does. key is probe()'s (codec, pix_fmt).

    Three choices that matter. The output is passthrough, because a constant-rate muxer duplicates and drops
    frames and that alone puts a false beat in the series. It is 16-bit, because swscale averages 4K down to
    160 px at high precision and a 1% flicker on a dark log clip is under one 8-bit level. And -noautorotate
    keeps the sensor's orientation: a phone held upright stores its frames sideways, and rolling-shutter bands
    only run down the rows of the sensor, not of the picture. A variable frame rate clip keeps its longest
    evenly spaced stretch, since an FFT assumes even spacing."""
    cap = max(1, int(max_frames))
    try:
        out = _video._decode(["-noautorotate", "-ss", f"{max(start_s, 0.0):.3f}", "-t", f"{seconds:.3f}"], path,
                             ["-v", "info", "-an", "-frames:v", str(cap), "-fps_mode", "passthrough",
                              "-vf", f"scale={RUN_COLS}:{RUN_ROWS}:flags=area,format=gray16le,showinfo", "-f", "rawvideo", "-"],
                             timeout=RUN_TIMEOUT, key=key)
    except subprocess.TimeoutExpired:
        return None
    per = RUN_COLS * RUN_ROWS * 2
    n = len(out.stdout or b"") // per
    if out.returncode != 0 or n == 0:
        return None
    frames = np.frombuffer(out.stdout, dtype="<u2", count=n * per // 2).reshape(n, RUN_ROWS, RUN_COLS)
    times = [float(t) for t in re.findall(rb"pts_time:\s*(-?[0-9]+(?:\.[0-9]+)?)", out.stderr or b"")]
    rate = float(fps or 0.0)
    a, b = 0, n
    if len(times) >= n >= 3:                       # the filter can run a frame or two past the -frames:v cap
        gaps = np.diff(times[:n])
        step = float(np.median(gaps))
        if step > 0:
            rate = 1.0 / step
            a, b = _even_stretch(gaps, step)
    if rate <= 0:
        return None
    return frames[a:b].astype(np.float32) / 65535.0, rate

def _even_stretch(gaps: np.ndarray, step: float) -> tuple[int, int]:
    """The longest [a, b) of frames whose intervals all sit within FRAME_GAP_TOL of step."""
    best = (0, 1); a = 0
    for i, ok in enumerate(np.abs(gaps / step - 1.0) <= FRAME_GAP_TOL):
        if not ok:
            a = i + 1
        elif i + 2 - a > best[1] - best[0]:
            best = (a, i + 2)
    return best

def _as_unit(frames) -> np.ndarray | None:
    """(n, h, w) frames as float32 in 0..1, or None when they are not that shape."""
    f = np.asarray(frames)
    if f.ndim != 3:
        return None
    if f.dtype == np.uint8:
        return f.astype(np.float32) / 255.0
    if f.dtype == np.uint16:
        return f.astype(np.float32) / 65535.0
    f = f.astype(np.float32)
    return f / 255.0 if f.size and float(f.max()) > 1.5 else f

def _shift(a: np.ndarray, b: np.ndarray, win: np.ndarray) -> float:
    """How far b sits from a, in pixels, by phase correlation, which ignores a change of brightness. Frames with
    no texture to lock on to report zero."""
    (dx, dy), resp = cv2.phaseCorrelate(a, b, win)
    return float(np.hypot(dx, dy)) if resp >= MOTION_RESPONSE else 0.0

def _cut_after(f: np.ndarray) -> np.ndarray:
    """True at i where frame i+1 jumps away from frame i: a hard cut, a flash, or a light switched. They need no
    telling apart, because each is handled by not letting a span run across it. The bar is the median and
    deviation of the neighbouring frames, so a steady flicker raises it instead of tripping it, and a flickering
    shot next to a clean one is judged against itself and not the clean shot. Differences are taken on CUT_BLOCK
    px block means: on a static shot an encoder skips every frame and then redraws all the noise at each
    keyframe, and read pixel by pixel that was a cut every 12 frames, measured on x264 at crf 23."""
    n, h, w = f.shape
    b = CUT_BLOCK
    blocks = f[:, :h // b * b, :w // b * b].reshape(n, h // b, b, w // b, b).mean(axis=(2, 4))
    d = np.abs(np.diff(blocks, axis=0)).mean(axis=(1, 2))
    out = np.zeros(d.shape, bool)
    for i in range(d.size):
        near = d[max(i - CUT_REACH, 0):i + CUT_REACH + 1]
        med = float(np.median(near))
        spread = max(1.4826 * float(np.median(np.abs(near - med))), CUT_FLOOR * med)
        out[i] = d[i] > max(med + CUT_SIGMAS * spread, CUT_MIN)
    return out

def _spans(f: np.ndarray) -> tuple[list[tuple[int, int]], int, int]:
    """The [a, b) stretches of at least MIN_FRAMES with no cut, no moving pair and no slow drift, plus how many
    cuts and how many moving pairs were found, for the reason."""
    n, h, w = f.shape
    # Each row over its own mean first: on a blank wall a crawling band IS a clean vertical translation, and a
    # band is the one thing constant along a row. The blur then takes out what is left of the broad shading.
    rowed = f / np.maximum(f.mean(axis=2, keepdims=True), 1 / 255)
    hp = [np.ascontiguousarray(x - cv2.GaussianBlur(x, (0, 0), MOTION_BLUR)) for x in rowed]
    win = cv2.createHanningWindow((w, h), cv2.CV_32F)
    cut = _cut_after(f)
    moved = np.array([_shift(hp[i], hp[i + 1], win) > MOVE_PX for i in range(n - 1)]) & ~cut    # a cut correlates to anything
    spans = []; a = 0
    for i in range(len(cut) + 1):
        if i == len(cut) or cut[i] or moved[i]:
            if i + 1 - a >= MIN_FRAMES:
                # Measured against the span's first frame, not summed pair by pair: the sum walks on noise alone.
                if all(_shift(hp[a], hp[j], win) <= DRIFT_PX for j in np.linspace(a, i, DRIFT_CHECKS).astype(int)[1:]):
                    spans.append((a, i + 1))
            a = i + 1
    return spans, int(cut.sum()), int(moved.sum())

def _detrend(x: np.ndarray) -> np.ndarray:
    """x (n, ...) with a quadratic in time removed, which takes out an exposure ramp and a slow fade."""
    n = x.shape[0]
    q, _ = np.linalg.qr(np.polynomial.legendre.legvander(np.linspace(-1, 1, n), 2))
    flat = x.reshape(n, -1)
    return (flat - q @ (q.T @ flat)).reshape(x.shape)

def _floor_bins(freqs: np.ndarray, alias: float, window: float, step: float) -> np.ndarray:
    """Bins to measure the noise on: the flanks of the search window, or every bin outside it when a short span
    leaves too few there."""
    gap = np.abs(freqs - alias)
    near = (gap > window + step) & (gap <= window + step + FLANK_HZ) & (freqs >= LOW_HZ)
    return near if near.sum() >= 3 else (gap > window) & (freqs >= LOW_HZ)

def _static_tiles(f: np.ndarray, med: np.ndarray) -> np.ndarray:
    """A (rows, cols) mask of tiles fit to measure: exposed, and with nothing moving through them. A brightness
    change moves a tile as one piece, so what is left inside a tile once its own mean is removed is movement."""
    n = f.shape[0]
    th, tw = f.shape[1] // TILE_ROWS, f.shape[2] // TILE_COLS
    ratio = f[:, :th * TILE_ROWS, :tw * TILE_COLS] / np.maximum(med[:th * TILE_ROWS, :tw * TILE_COLS], 1 / 255)
    t = ratio.reshape(n, TILE_ROWS, th, TILE_COLS, tw)
    churn = t.std(axis=(2, 4)).mean(axis=0)
    level = med[:th * TILE_ROWS, :tw * TILE_COLS].reshape(TILE_ROWS, th, TILE_COLS, tw).mean(axis=(1, 3))
    lit = (level > LUMA_LO) & (level < LUMA_HI)
    if not lit.any():
        return lit
    return lit & (churn <= 2.5 * float(np.median(churn[lit])))

def _tile_line(f: np.ndarray, fps: float, alias: float, static: np.ndarray) -> tuple[float, float] | None:
    """(hz, strength) of a line near alias across the static tiles, or None. A pass needs all of: the peak over the
    local floor in a majority of tiles, phases agreeing along tile rows, the line present in both halves of the
    span, and no stronger line under it. Each test removes a different impostor: noise, handheld shake against
    texture, a burst of light, and the keyframe pop some encoders put in at a fixed interval (a comb of lines
    at 2 Hz, 4 Hz, 6 Hz)."""
    n = f.shape[0]
    th, tw = f.shape[1] // TILE_ROWS, f.shape[2] // TILE_COLS
    tiles = f[:, :th * TILE_ROWS, :tw * TILE_COLS].reshape(n, TILE_ROWS, th, TILE_COLS, tw).mean(axis=(2, 4))
    rel = _detrend(tiles / np.maximum(tiles.mean(axis=0), 1 / 255) - 1.0)[:, static]      # (n, static tiles)
    w = np.hanning(n + 2)[1:-1]
    spec = np.fft.rfft(rel * w[:, None], axis=0)
    amp = 2.0 * np.abs(spec) / w.sum()
    freqs = np.fft.rfftfreq(n, 1.0 / fps)
    step = float(freqs[1])
    window = max(SEARCH_HZ, step)
    inside = np.flatnonzero((np.abs(freqs - alias) <= window) & (freqs >= LOW_HZ))
    floor_bins = _floor_bins(freqs, alias, window, step)
    if inside.size == 0 or floor_bins.sum() < 3:
        return None
    k = int(inside[np.argmax(amp[inside].mean(axis=1))])
    floor = np.maximum(np.median(amp[floor_bins], axis=0), 1e-7)
    near = amp[max(k - 1, 0):k + 2].max(axis=0)
    hit = near / floor >= SNR_MIN
    if hit.mean() <= TILE_MAJORITY:
        return None
    strength = float(np.median(near[hit]))
    if strength < MIN_STRENGTH:
        return None
    # Phase agreement along each tile row, weighted by how many static tiles the row has.
    phase = np.full(static.shape, np.nan, np.complex64)
    phase[static] = spec[k] / np.maximum(np.abs(spec[k]), 1e-12)
    rows = [(abs(np.nanmean(r)), np.count_nonzero(~np.isnan(r))) for r in phase if np.count_nonzero(~np.isnan(r)) >= 2]
    if rows and sum(c * m for c, m in rows) / sum(m for _, m in rows) < COHERENCE_MIN:
        return None
    if n >= 2 * MIN_FRAMES:
        t = np.arange(n) / fps
        wave = np.exp(-2j * np.pi * freqs[k] * t)[:, None]
        half = n // 2
        wh = np.hanning(half + 2)[1:-1, None]
        one = np.median(np.abs((rel[:half] * wh * wave[:half]).sum(axis=0))[hit])
        two = np.median(np.abs((rel[-half:] * wh * wave[-half:]).sum(axis=0))[hit])
        if min(one, two) < HALVES_MIN * max(one, two):
            return None
    typical = np.median(amp, axis=1)
    for div in (2, 3):
        sub = int(round(k / div))
        if freqs[sub] >= LOW_HZ and k - sub >= 3 and typical[max(sub - 1, 0):sub + 2].max() >= COMB_MIN * typical[k]:
            return None
    return float(freqs[k]), strength

def _band_line(f: np.ndarray, med: np.ndarray, fps: float, alias: float) -> tuple[float, float, float] | None:
    """(hz, strength, cycles per frame) of bands crawling at alias, or None. Each frame over the temporal median
    leaves only what changed; averaging across column strips gives a row profile per strip per time, and a 2D
    FFT over (time, rows) puts a crawling band on one point. Frame-wide pulsing is removed first by taking the
    mean off each profile, so this sees only structure down the rows. Strips are averaged as complex numbers, so a
    subject moving in one strip cancels rather than adds. The temporal median also cancels a band that does not
    crawl, which is why a frame-locked rate cannot be tested here."""
    n, h, w = f.shape
    sw = w // BAND_STRIPS
    prof = (f / np.maximum(med, 1 / 255))[:, :, :sw * BAND_STRIPS].reshape(n, h, BAND_STRIPS, sw).mean(axis=3)
    wy = np.hanning(h + 2)[1:-1]
    prof = prof - (prof * wy[None, :, None]).sum(axis=1, keepdims=True) / wy.sum()
    prof = _detrend(prof)
    wt = np.hanning(n + 2)[1:-1]
    spec = np.fft.fft(np.fft.rfft(prof * wt[:, None, None] * wy[None, :, None], axis=1), axis=0)
    scale = 2.0 / (wt.sum() * wy.sum())
    top = min(BAND_MAX_CYCLES, h // 6)
    spec = spec[:, 1:top + 1, :]                                        # (signed time freq, cycles, strips)
    joint = np.abs(spec.mean(axis=2)) * scale
    freqs = np.fft.fftfreq(n, 1.0 / fps)
    step = float(abs(freqs[1]))
    window = max(SEARCH_HZ, step)
    mag = np.abs(freqs)
    inside = np.flatnonzero((np.abs(mag - alias) <= window) & (mag >= LOW_HZ))
    floor_bins = _floor_bins(mag, alias, window, step)
    if inside.size == 0 or floor_bins.sum() < 3:
        return None
    i, c = np.unravel_index(np.argmax(joint[inside]), joint[inside].shape)
    ti = int(inside[i])
    peak = float(joint[ti, c])
    floor = max(float(np.median(joint[floor_bins])), 1e-7)
    if peak / floor < BAND_SNR or peak < MIN_STRENGTH:
        return None
    if peak < BAND_ASYMMETRY * float(joint[-ti % n, c]):
        return None
    if peak < COHERENCE_MIN * float(np.abs(spec[ti, c]).mean()) * scale:
        return None
    return float(abs(freqs[ti])), peak, float(c + 1)

def _span(f: np.ndarray, fps: float, aliases: dict[float, float]) -> dict | None:
    """One clean span against every beat that could be tested on it, as a result, or None for no static tiles
    or no beat long enough to test."""
    n = f.shape[0]
    testable = {light: a for light, a in aliases.items() if a >= MIN_ALIAS_HZ and a * n / fps >= MIN_CYCLES}
    if not testable:
        return None
    med = np.median(f, axis=0)
    static = _static_tiles(f, med)
    if static.mean() < MIN_STATIC_TILES:
        return None
    for light, alias in sorted(testable.items(), key=lambda kv: kv[1]):
        band = _band_line(f, med, fps, alias)
        if band:
            return _result("banding", f"bands crawling at {band[0]:.1f} Hz, {band[2]:.0f} per frame: {light:.0f} Hz light at {fps:.2f} fps",
                           band[0], band[1])
        line = _tile_line(f, fps, alias, static)
        if line:
            return _result("pulsing", f"brightness beating at {line[0]:.1f} Hz: {light:.0f} Hz light at {fps:.2f} fps", line[0], line[1])
    beats = ", ".join(f"{a:.1f} Hz" for a in sorted(testable.values()))
    # A clean verdict must say which light it never looked at: at 25 fps the 100 Hz of Indian mains is frame-locked.
    skipped = "".join(f"; {light:.0f} Hz light untested, its {a:.2f} Hz beat is too slow to see" for light, a in sorted(aliases.items()) if light not in testable)
    return _result("none", f"no line at {beats} in {n} frames{skipped}")

def analyse(frames, fps: float, mains: tuple[int, ...] = MAINS, exposure_s: float | None = None) -> dict:
    """{"flicker": "none"|"pulsing"|"banding"|"unknown", "hz", "strength", "reason"} for one run of consecutive
    frames (n, h, w). hz is the measured beat in the footage, not the light; strength is modulation depth (0.02
    is plus or minus 2%) in code values, so a log clip reads weaker than the light really is. "unknown" means
    there was not enough to go on: too few frames, no span free of motion and cuts, too few static tiles, or no
    beat that this frame rate lets a run this long resolve."""
    f = _as_unit(frames)
    if f is None or not fps or fps <= 0:
        return _result("unknown", "not a run of frames at a known rate")
    if f.shape[0] < MIN_FRAMES:
        return _result("unknown", f"{f.shape[0]} frames, need {MIN_FRAMES}")
    pred = risk(fps, exposure_s, mains)["mains"]
    live = {v["light_hz"]: v["alias_hz"] for v in pred.values() if not v["shutter_cancels"]}
    if not live:
        return _result("none", "the shutter is a whole number of light periods")
    spans, cuts, moved = _spans(f)
    if not spans:
        return _result("unknown", f"no still stretch of {MIN_FRAMES} frames ({moved} moving pairs, {cuts} cuts)")
    got = [r for r in (_span(f[a:b], fps, live) for a, b in spans) if r]
    found = [r for r in got if r["flicker"] != "none"]
    if found:
        return max(found, key=lambda r: r["strength"])
    if got:
        return got[0]
    return _result("unknown", "too little static footage, or no beat at this frame rate can be resolved")

def _combine(runs: list[dict]) -> dict:
    """One verdict from up to MAX_RUNS. All clean is none. Flicker needs a majority of the usable runs to agree on
    the beat, or the only usable run to show it. A lone positive outvoted by clean runs may be one lit setup in
    a mixed clip or may be a false alarm, and a sample of three cannot say which, so that is unknown."""
    usable = [r for r in runs if r["flicker"] != "unknown"]
    pos = [r for r in usable if r["flicker"] != "none"]
    if not usable:
        return _result("unknown", (runs[0]["reason"] if runs else "no run could be decoded"))
    if not pos:
        return _result("none", f"{len(usable)} of {len(runs)} runs clean: {usable[0]['reason']}")
    best: list[dict] = []
    for r in pos:
        agree = [o for o in pos if abs(o["hz"] - r["hz"]) <= SEARCH_HZ]
        if len(agree) > len(best):
            best = agree
    if len(best) * 2 > len(usable) or len(usable) == 1:
        kinds = [r["flicker"] for r in best]
        kind = max(set(kinds), key=kinds.count)
        same = [r for r in best if r["flicker"] == kind]
        return _result(kind, f"{len(best)} of {len(usable)} usable runs: {same[0]['reason']}", float(np.median([r["hz"] for r in best])),
                       float(np.median([r["strength"] for r in best])))
    return _result("unknown", f"only {len(best)} of {len(usable)} usable runs show it: {best[0]['reason']}")

def _start_times(duration: float, seconds: float) -> list[float]:
    """Up to MAX_RUNS run starts spread through the clip and clear of its ends, which are often slates."""
    n = max(1, min(MAX_RUNS, int(duration // seconds)))
    return [max(0.0, min(duration * (i + 1) / (n + 1) - seconds / 2, duration - seconds)) for i in range(n)]

def _slowmo(path: Path) -> float:
    """How many times faster than it plays back the clip was shot, from Sony's sidecar; 1.0 when it does not say.
    100p stored as a 25p file holds frames 1/100 s apart that the container calls 1/25 s apart, and the beat
    follows the capture rate. Phones keep no such record, so their slow motion clips read at the container's rate."""
    side = _video.read_sidecar(path)
    cap, fmt = side["capture_fps"], side["format_fps"]
    if cap and fmt and abs(cap / fmt - 1.0) > SLOWMO_TOL:
        return cap / fmt
    return 1.0

def check_clip(path: Path, duration: float, fps: float, exposure_s: float | None = None, key: tuple[str, str] | None = None,
               mains: tuple[int, ...] = MAINS) -> dict:
    """analyse()'s dict for a whole clip, plus runs: each run's own verdict. Decodes up to MAX_RUNS runs spread
    through it and combines them. Never raises: a clip that cannot be read is "unknown". key is probe()'s
    (codec, pix_fmt); without it the runs decode in software, and for this path that is the better default, the
    opposite of the frame and scene passes. Measured on an M1 over a 10 s 4K30 testsrc2 clip, three runs: h264
    1.6 s software, 7.0 s videotoolbox; HEVC 10-bit 2.9 s and 3.2 s. A run pulls every full frame back across
    the bus and a session is set up per run. Synthetic content decodes cheaply, so recheck on camera files.
    A shutter that cancels every candidate answers without a decode. fps is only the fallback: each run's own
    timestamps are believed first, scaled by the sidecar when the clip was shot slow motion."""
    began = time.monotonic()
    path = Path(path)
    speed = _slowmo(path)
    pred = risk(fps * speed, exposure_s, mains)
    if pred["mains"] and all(v["shutter_cancels"] for v in pred["mains"].values()):
        return {**_result("none", "the shutter is a whole number of light periods"), "runs": []}
    if not duration or duration <= 0 or not fps or fps <= 0:
        return {**_result("unknown", "no duration or frame rate"), "runs": []}
    if duration * fps < MIN_FRAMES:
        return {**_result("unknown", f"clip is under {MIN_FRAMES} frames"), "runs": []}
    runs: list[dict] = []
    for start in _start_times(duration, RUN_SECONDS):
        if runs and time.monotonic() - began > CLIP_BUDGET_S:
            break
        try:
            got = read_run(path, start, RUN_SECONDS, key=key, fps=fps)
        except VideoUnreadable as e:
            return {**_result("unknown", str(e)), "runs": runs}
        if got is None:
            runs.append(_result("unknown", f"nothing decoded at {start:.1f} s"))
            continue
        frames, rate = got
        runs.append(analyse(frames, rate * speed, mains, exposure_s))
    return {**_combine(runs), "runs": runs}
