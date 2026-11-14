"""Synthetic fishing frames for unit tests: moving water, a red/brown bobber that
lands ~1 s after the cast, ripples, a splash at the bite, other players' bobbers.

These are NOT a substitute for real recordings (see docs/FISHING_VISION.md for
real-data results); they pin the detector's decision rules."""

import math

import numpy as np
from PIL import Image

W, H = 420, 300
FPS = 1 / 8.3


_TEXTURE = {}


def _texture(w, h, seed):
    """Fixed contrasty wave texture (dark crests), like real water at a distance."""
    key = (w, h, seed)
    if key not in _TEXTURE:
        rng = np.random.default_rng(seed)
        field = rng.normal(0, 1, (h + 160, w + 160)).astype(np.float32)
        for _ in range(3):  # cheap smoothing: elongated wave blobs
            field = (
                field + np.roll(field, 1, 1) + np.roll(field, -1, 1) + np.roll(field, 2, 1) + np.roll(field, 1, 0)
            ) / 5
        _TEXTURE[key] = field / field.std() * 7
    return _TEXTURE[key]


def water(t, w=W, h=H, seed=0):
    """Texture drifting ~6 px/s plus slow swell and sensor-like noise."""
    tex = _texture(w, h, seed)
    ox, oy = int(40 + t * 6) % 120, int(40 + t * 2) % 120
    yy, xx = np.mgrid[:h, :w].astype(np.float32)
    wave = tex[oy : oy + h, ox : ox + w] + 2 * np.sin((xx * 0.9 + yy * 2.2) / 13 + t * 1.2)
    rng = np.random.default_rng(int(t * 1000) + seed)
    noise = rng.normal(0, 1.2, (h, w)).astype(np.float32)
    return np.stack([55 + wave + noise, 53 + wave * 0.9 + noise, 66 + wave * 1.1 + noise], axis=2)


def bobber(a, cx, cy, scale=1.0):
    h, w = a.shape[:2]
    yy, xx = np.mgrid[:h, :w]
    body = ((xx - cx) / (10 * scale)) ** 2 + ((yy - cy) / (13 * scale)) ** 2 <= 1
    a[body] = (104, 77, 58)
    feather = (
        (np.abs((yy - (cy - 12 * scale)) - 0.35 * (xx - cx)) <= 2 * scale)
        & (xx > cx - 26 * scale)
        & (xx < cx)
        & (yy < cy)
    )
    a[feather] = (150, 40, 42)
    line = (np.abs(xx - cx - (yy - cy) * 0.15) <= 0.6) & (yy > cy + 13 * scale)
    a[line] = (20, 20, 24)


def ring(a, cx, cy, radius, gain=12):
    h, w = a.shape[:2]
    yy, xx = np.mgrid[:h, :w]
    r = np.hypot((xx - cx) / 1.0, (yy - cy) / 0.45)
    a[np.abs(r - radius) <= 1.5] += gain


def splash(a, cx, cy, strength=1.0, scale=1.0):
    h, w = a.shape[:2]
    yy, xx = np.mgrid[:h, :w]
    plume = ((xx - cx) / (30 * scale)) ** 2 + ((yy - cy + 8 * scale) / (24 * scale)) ** 2 <= 1
    rng = np.random.default_rng(int(cx * 7 + cy))
    alpha = (0.55 * strength * (rng.random(plume.sum()) > 0.25))[:, None]
    a[plume] = a[plume] * (1 - alpha) + np.array([175, 185, 200]) * alpha


def frame(
    t,
    *,
    ours=(210, 150),
    land=1.0,
    bite=None,
    bite_len=0.35,
    old=(90, 220),
    old_until=0.4,
    others=(),
    other_splash=None,
    ripples=(),
    shift_at=None,
    shift=(0, 0),
    late_bobber=None,
    scale=1.0,
):
    a = water(t)
    if t < old_until and old:
        bobber(a, *old, scale)
    for ox, oy in others:
        bobber(a, ox, oy + 1.2 * math.sin(t * 3.1 + ox), scale)
    if late_bobber and t >= late_bobber[2]:
        bobber(a, late_bobber[0], late_bobber[1], scale)
    if ours and t >= land:
        dip = 4 * scale if bite is not None and bite <= t < bite + bite_len else 0
        cx, cy = ours[0], ours[1] + 1.5 * math.sin(t * 4.2) + dip
        bobber(a, cx, cy, scale)
        for start in ripples:
            if start <= t < start + 0.5:
                ring(a, cx, cy + 10 * scale, 20 * scale + (t - start) * 30 * scale)
        if bite is not None and bite <= t < bite + bite_len:
            splash(a, cx, cy, scale=scale)
    if other_splash and other_splash[2] <= t < other_splash[2] + 0.35:
        splash(a, other_splash[0], other_splash[1], scale=scale)
    if shift_at is not None and t >= shift_at:
        a = np.roll(a, shift, axis=(0, 1))
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))


def times(until, start=0.0, step=FPS):
    t, out = start, []
    while t <= until:
        out.append(round(t, 4))
        t += step
    return out
