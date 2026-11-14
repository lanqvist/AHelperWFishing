"""Per-cast bobber localisation, tracking and bite detection.

Ownership: our bobber is the single compact object that is absent from the
frames taken right after our own cast started (HUD cast serial) and then stays
in place. Objects that were already there (another player's bobber, boats,
lanterns) are not ours. Two new objects, none, a moving camera or a change
away from our bobber block the signal.

Bite: within a window around our bobber, the frame is compared with the calm
water/bobber of the same cast (running median). Scores are robust z-scores of
change energy and new bright (foam) pixels relative to this cast's own calm
frames - they are not probabilities. A signal needs two consecutive anomalous
fresh frames located at the bobber. Thresholds are calibrated on labelled casts.
"""

import math
from collections import deque
from dataclasses import asdict, dataclass, field

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

DETECTOR_VERSION = '0.3.2'  # bump with any change that alters detector output


def validate_region(region, screen):
    if not isinstance(region, dict):
        raise ValueError('Выдели область заброса')
    for key in ('x', 'y', 'width', 'height'):
        if type(region.get(key)) is not int or region[key] < 0:
            raise ValueError('Некорректная область заброса')
    if not 64 <= region['width'] <= 2400 or not 64 <= region['height'] <= 1600:
        raise ValueError('Размер области: от 64 до 2400×1600 пикселей')
    if region['x'] + region['width'] > screen['width'] or region['y'] + region['height'] > screen['height']:
        raise ValueError('Область заброса за пределами окна')
    return region


@dataclass
class VisionParams:
    work_side: int = 720  # long side of the search image, px (if pixel_scale is unknown)
    pixel_scale: int = 0  # search downscale from the screen: round(height / 1080); 0 = from zone size
    reference_from: float = 0.3  # s after cast start: previous bobber gone (~0.45 s),
    reference_until: float = 0.65  # new bobber not yet in the zone (~0.75 s)
    search_from: float = 1.0
    search_until: float = 4.0
    persist: int = 3  # frames an object must stay in place
    settle: float = 0.7  # s after detection (landing splash) before calm baseline
    baseline_frames: int = 15  # ~1.5 s of calm water after the cast splash before any decision
    threshold: float = 8.0  # anomaly score needed on two consecutive frames
    low_ratio: float = 0.4  # the neighbouring frame must reach threshold * low_ratio
    intact_drop: float = 0.12  # template match below the calm level that counts as "bobber hidden"
    max_gap: float = 0.35  # s between frames inside one decision
    stale: float = 0.3  # s; older frames never produce a signal
    fps: int = 30  # live processing rate (VisionSession); decisions run on every frame
    sample_interval: float = 0.09  # s; slow-path cadence (search, reference, calm baseline) as validated
    pair_dt: float = 0.1  # s; a decision compares a frame with the one ~0.1 s earlier
    history_window: float = 4.5  # s of calm frames behind the z-scores (validated: 40 frames at ~9 fps)
    diff_level: int = 30  # gray-level change counted as "changed pixel"
    warm_gain: float = 10.0  # R-B increase of a new object over the water
    decide_after: int = 2  # extra frames after the first stable object

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        known = {k: v for k, v in (data or {}).items() if k in cls.__dataclass_fields__}
        return cls(**known)


def rgb_array(image):
    return np.asarray(image.convert('RGB'), dtype=np.int16)


def gray(a):
    return (a[..., 0] * 0.299 + a[..., 1] * 0.587 + a[..., 2] * 0.114).astype(np.float32)


def box_blur(a, k):
    if k <= 1:
        return a
    pad = k // 2
    p = np.pad(a.astype(np.float32), ((pad, pad), (pad, pad)) + ((0, 0),) * (a.ndim - 2), mode='edge')
    c = p.cumsum(0).cumsum(1)
    c = np.pad(c, ((1, 0), (1, 0)) + ((0, 0),) * (a.ndim - 2))
    h, w = a.shape[:2]
    return (c[k : k + h, k : k + w] - c[:h, k : k + w] - c[k : k + h, :w] + c[:h, :w]) / (k * k)


def components(mask, limit=40000):
    """4-connected components; returns [(area, x0, y0, x1, y1, cx, cy)]."""
    ys, xs = np.nonzero(mask)
    if len(xs) > limit:
        return None
    w = mask.shape[1]
    todo_set = set((ys * w + xs).tolist())
    out = []
    while todo_set:
        seed = todo_set.pop()
        stack, members = [seed], []
        while stack:
            n = stack.pop()
            members.append(n)
            _, x = divmod(n, w)
            for m in ((n - 1) if x > 0 else -1, (n + 1) if x < w - 1 else -1, n - w, n + w):
                if m in todo_set:
                    todo_set.remove(m)
                    stack.append(m)
        arr = np.array(members)
        yy, xx = arr // w, arr % w
        out.append(
            (
                len(arr),
                int(xx.min()),
                int(yy.min()),
                int(xx.max()) + 1,
                int(yy.max()) + 1,
                float(xx.mean()),
                float(yy.mean()),
            )
        )
    return out


def ncc_search(window, template):
    """Best normalised cross-correlation offset of template inside window."""
    th, tw = template.shape
    if window.shape[0] < th or window.shape[1] < tw:
        return None
    view = sliding_window_view(window, (th, tw))
    t = template - template.mean()
    tn = math.sqrt(float((t * t).sum())) or 1.0
    vm = view.mean(axis=(2, 3), keepdims=True)
    vz = view - vm
    num = (vz * t).sum(axis=(2, 3))
    den = np.sqrt((vz * vz).sum(axis=(2, 3))) * tn
    score = np.where(den > 1e-3, num / np.maximum(den, 1e-3), 0)
    iy, ix = np.unravel_index(int(np.argmax(score)), score.shape)
    return int(ix), int(iy), float(score[iy, ix])


@dataclass
class VisionResult:
    phase: str
    reason: str
    box: tuple | None = None  # our bobber, region pixel coords
    others: list = field(default_factory=list)
    score: float | None = None
    anomalous: bool = False
    fire: bool = False
    features: dict = field(default_factory=dict)


def robust(values, floor):
    v = np.asarray(values, dtype=np.float64)
    med = float(np.median(v))
    mad = float(np.median(np.abs(v - med))) * 1.4826
    return med, max(mad, floor)


# Calm level of the bite score: frames below it refresh the per-cast baseline. Fixed and
# independent of the threshold, so a threshold change never changes what counts as calm.
CALM = 3.0

FLOORS = {'energy': 0.5, 'bright': 0.008, 'foam': 1.5, 'outside': 0.004, 'ring_energy': 0.4, 'disp': 0.12, 'ncc': 0.05}


class CastVision:
    """Feed region frames of one cast (identified by the HUD cast serial)."""

    def __init__(self, cast, started_at, params=None):
        self.cast, self.started_at = cast, started_at
        self.p = params or VisionParams()
        self.phase = 'reference'
        self.refs = []
        self.reference = None
        self.factor = None
        self.tracks = []  # [cx, cy, size, hits, last_seen_index]
        self.recent = deque(maxlen=5)
        self.first_stable = None
        self.shifted_run = 0
        self.shifted_since = None
        self.sample_last = None  # time of the last slow-path sample
        self.search_index = 0
        self.search_last = None
        self.scores = deque()  # (t, score, located, intact) of the last ~0.4 s
        self.bg_median = None
        self.stats_cache = None
        self.last_track = None
        self.refs_native = []
        self.ref_noise = 0.0
        self.frame_index = 0
        self.found_at = None
        self.center = self.size = None
        self.template = None
        self.template_at = None
        self.window_box = None  # fixed analysis window (x0, y0, x1, y1), set once the bobber settled
        self.window_shape = None  # shape of the window the template was taken from
        self.core = self.ring = self.core_shape = None
        self.others = []
        self.bg = deque(maxlen=12)
        self.history = {k: deque() for k in FLOORS}  # calm-frame features of the last history_window s
        self.history_t = deque()
        self.baseline_count = 0
        self.last_at = None
        self.prev = None
        self.prev_small = None
        self.reason = 'Жду начала заброса'

    # -- search ------------------------------------------------------------
    def _small(self, a):
        if self.factor is None:
            # The bobber's size on screen follows the screen resolution, not the zone:
            # searching at ~1080p scale keeps it ~25 px whatever zone was drawn.
            by_zone = math.ceil(max(a.shape[0], a.shape[1]) / self.p.work_side)
            self.factor = max(1, self.p.pixel_scale or by_zone, by_zone if by_zone > 4 else 1)
        f = self.factor
        h, w = a.shape[0] // f * f, a.shape[1] // f * f
        return a[:h, :w].reshape(h // f, f, w // f, f, 3).mean(axis=(1, 3)).astype(np.float32)

    def _candidates(self, small):
        """New, stable objects: median of recent frames vs. median before landing,
        divided by the local water noise (temporal spread of recent frames)."""
        self.recent.append(small)
        if len(self.recent) < 4:
            return []
        stack = np.stack(self.recent)
        now = np.median(stack, axis=0)
        noise = np.median(np.abs(stack - now), axis=0).sum(axis=2)
        diff = np.abs(now - self.reference).sum(axis=2)
        z = diff / (box_blur(noise, 5) + self.ref_noise + 8.0)
        mask = (z > 2.5) & (diff > 30)
        # Opening on the unblurred mask removes the thin fishing line attached
        # to the bobber and isolated noisy pixels; the solid body survives.
        mask = box_blur(box_blur(mask.astype(np.float32), 3) > 0.7, 3) > 0.05
        small = now
        comps = components(mask)
        if comps is None:
            return None
        h, w = mask.shape
        g_now, g_ref = gray(small), gray(self.reference)
        out = []
        for area, x0, y0, x1, y1, cx, cy in comps:
            bw, bh = x1 - x0, y1 - y0
            if area < 4 or bw > w * 0.3 or bh > h * 0.4 or not 0.2 <= bw / max(bh, 1) <= 5:
                continue
            pad = max(2, max(bw, bh) // 2)
            sl = (slice(max(0, y0 - pad), min(h, y1 + pad)), slice(max(0, x0 - pad), min(w, x1 + pad)))
            # A new object adds local contrast; a vanished one (someone's bobber
            # reeled in) removes it. Only additions can be our bobber.
            if float(g_now[sl].std()) < 1.15 * float(g_ref[sl].std()) + 1:
                continue
            # Bobbers are red/brown: the new object must be warmer (R-B) than the
            # water it covers. Dark wave crests and foam are not.
            box = (slice(y0, y1), slice(x0, x1))
            gain = (small[box][..., 0] - small[box][..., 2]) - (
                self.reference[box][..., 0] - self.reference[box][..., 2]
            )
            if float(np.percentile(gain, 75)) < self.p.warm_gain:
                continue
            out.append((cx, cy, max(bw, bh), area))
        return out

    def _search(self, a, t):
        small = self._small(a)
        cands = self._candidates(small)
        if cands is None:
            return VisionResult('lost', 'Слишком много изменений в зоне: камера сдвинулась или зона велика')
        self.search_index += 1
        idx = self.search_index
        for cx, cy, size, _ in cands:
            for track in self.tracks:
                if math.hypot(track[0] - cx, track[1] - cy) <= max(3, track[2]) and track[4] == idx - 1:
                    track[0] = (track[0] + cx) / 2
                    track[1] = (track[1] + cy) / 2
                    track[2] = max(track[2] * 0.7 + size * 0.3, 2)
                    track[3] += 1
                    track[4] = idx
                    break
            else:
                self.tracks.append([cx, cy, size, 1, idx])
        self.tracks = [tr for tr in self.tracks if tr[4] >= idx - 1]
        stable = [tr for tr in self.tracks if tr[3] >= self.p.persist and tr[4] == idx]
        f = self.factor
        # Body and feather of one bobber can be two pieces: merge pieces closer
        # than 1.5x their size; separate groups are separate objects.
        groups = []
        for tr in stable:
            for gr in groups:
                if any(math.hypot(tr[0] - o[0], tr[1] - o[1]) <= max(tr[2], o[2]) * 1.5 + 3 for o in gr):
                    gr.append(tr)
                    break
            else:
                groups.append([tr])
        merged = []
        for gr in groups:
            x0 = min(o[0] - o[2] / 2 for o in gr)
            x1 = max(o[0] + o[2] / 2 for o in gr)
            y0 = min(o[1] - o[2] / 2 for o in gr)
            y1 = max(o[1] + o[2] / 2 for o in gr)
            merged.append([(x0 + x1) / 2, (y0 + y1) / 2, max(x1 - x0, y1 - y0)])
        stable = merged
        if stable and self.first_stable is None:
            self.first_stable = idx
        if stable and idx - self.first_stable < self.p.decide_after:
            return VisionResult('searching', 'Объект найден; проверяю, нет ли второго')
        if len(stable) > 1:
            boxes = [self._box(tr[0] * f, tr[1] * f, tr[2] * f) for tr in stable]
            self.phase = 'ambiguous'
            return VisionResult(
                'ambiguous', 'Появились два новых объекта: чужой заброс или помеха. Подсечки не будет', others=boxes
            )
        if stable:
            tr = stable[0]
            self.center = ((tr[0] + 0.5) * f, (tr[1] + 0.5) * f)
            self.size = max(6.0, tr[2] * f)
            self.found_at = t
            self.phase = 'settling'
            # Objects present before the cast (other bobbers, boats) are remembered
            # as possible interference near ours.
            return VisionResult(
                'settling', 'Поплавок найден; жду окончания всплеска заброса', box=self._box(*self.center, self.size)
            )
        if t > self.p.search_until:
            self.phase = 'lost'
            return VisionResult('lost', 'Поплавок не найден после заброса: проверь зону и камеру')
        return VisionResult('searching', 'Ищу новый поплавок после заброса')

    def _box(self, cx, cy, size):
        h = size / 2
        return (int(cx - h), int(cy - h), int(cx + h), int(cy + h))

    def _refine(self, a):
        """Measure the settled bobber at full resolution.

        First against the pre-landing frames; if drifting textured water hides
        the bobber in that difference, by colour: the bobber is far warmer (R-B)
        than water, which drift does not change."""
        r = int(max(70, self.size * 3))
        cx, cy = int(self.center[0]), int(self.center[1])
        x0, y0 = max(0, cx - r), max(0, cy - r)
        x1, y1 = min(a.shape[1], cx + r), min(a.shape[0], cy + r)
        crop = a[y0:y1, x0:x1].astype(np.float32)
        ref = np.median(np.stack([f[y0:y1, x0:x1] for f in self.refs_native]), axis=0)
        self.refs_native = []
        diff = box_blur(np.abs(crop - ref).sum(axis=2), 3)
        warm = box_blur(crop[..., 0] - crop[..., 2], 3)
        # (values, strong floor, weak floor, strong k, weak k): pink water varies in R-B too,
        # so the colour pass uses a lower spread multiple.
        for values, floor_strong, floor_weak, k_strong, k_weak in (
            (diff, 40.0, 25.0, 6, 4),
            (warm - float(np.median(warm)), 25.0, 15.0, 4.5, 2.5),
        ):
            level = float(np.median(values))
            spread = float(np.median(np.abs(values - level))) * 1.4826
            # Opening: the 1-2 px fishing line and ripples vanish, the bobber body stays.
            solid = box_blur((values > max(floor_strong, level + k_strong * spread)).astype(np.float32), 5) > 0.55
            solid = box_blur(solid.astype(np.float32), 5) > 0.05
            box = self._measure(
                components(solid & (values > max(floor_weak, level + k_weak * spread))) or [], cx - x0, cy - y0
            )
            if box:
                bx0, by0, bx1, by1 = box
                self.center = (x0 + (bx0 + bx1) / 2, y0 + (by0 + by1) / 2)
                self.size = float(min(200, max(8, bx1 - bx0, by1 - by0)))
                return True
        return False

    def _measure(self, comps, lx, ly):
        def touches(c, x, y, radius):
            return math.hypot(min(max(x, c[1]), c[3]) - x, min(max(y, c[2]), c[4]) - y) <= radius

        near = [c for c in comps if c[0] >= 6 and touches(c, lx, ly, max(20, self.size * 1.5))]
        if not near:
            return None
        # The bobber plus its feather can be split; merge pieces close to the centre.
        for _ in range(2):
            bx0 = min(c[1] for c in near)
            by0 = min(c[2] for c in near)
            bx1 = max(c[3] for c in near)
            by1 = max(c[4] for c in near)
            mx, my, ms = (bx0 + bx1) / 2, (by0 + by1) / 2, max(bx1 - bx0, by1 - by0)
            near = [c for c in comps if c[0] >= 15 and touches(c, mx, my, ms * 0.3 + 4)] or near
        return (min(c[1] for c in near), min(c[2] for c in near), max(c[3] for c in near), max(c[4] for c in near))

    # -- tracking ----------------------------------------------------------
    def _window(self, a):
        """Fixed analysis window around the settled bobber, clipped to the zone.
        Only the core (1.6 sizes around the bobber) must lie inside the zone."""
        if self.window_box is None:
            half = int(min(160, max(28, self.size * 2.6)))
            cx, cy = int(self.center[0]), int(self.center[1])
            core = int(self.size * 1.6) + 2
            if cx - core < 0 or cy - core < 0 or cx + core > a.shape[1] or cy + core > a.shape[0]:
                return None, None
            self.window_box = (
                max(0, cx - half),
                max(0, cy - half),
                min(a.shape[1], cx + half),
                min(a.shape[0], cy + half),
            )
        x0, y0, x1, y1 = self.window_box
        if x1 > a.shape[1] or y1 > a.shape[0]:
            return None, None
        return gray(a[y0:y1, x0:x1]), (x0, y0)

    def _take_template(self, g, origin):
        half = int(min(20, max(5, self.size * 0.7)))
        cx, cy = int(self.center[0] - origin[0]), int(self.center[1] - origin[1])
        patch = g[max(0, cy - half) : cy + half, max(0, cx - half) : cx + half]
        if patch.shape[0] < 6 or patch.shape[1] < 6 or float(patch.std()) < 3:
            return False
        self.template = patch.copy()
        self.template_at = (cx - min(cx, half), cy - min(cy, half))
        self.window_shape = g.shape
        return True

    def _features(self, g, origin):
        """Change of this frame against the calm frames of the same cast.

        core: disc of 1.3 bobber sizes around the bobber - the splash rises here;
        ring: the rest of the window - waves, ripples, other players' bobbers.
        """
        if self.bg_median is None and len(self.bg):
            self.bg_median = np.median(np.stack(self.bg), axis=0)
        bg = self.bg_median if len(self.bg) else g
        d = g - bg
        if self.core_shape != g.shape:
            yy, xx = np.mgrid[: g.shape[0], : g.shape[1]]
            r = np.hypot(xx - (self.center[0] - origin[0]), yy - (self.center[1] - origin[1]))
            self.core, self.ring, self.core_shape = r <= self.size * 1.3, r > self.size * 1.6, g.shape
        dc = d[self.core]
        energy = float(np.abs(dc).mean())
        bright = float((dc > 15).mean())
        foam = float(np.percentile(dc, 95))
        outside = float((d[self.ring] > 15).mean())
        ring_energy = float(np.abs(d[self.ring]).mean()) if self.ring.any() else 0.0
        # Brightening density at the bobber versus the water around it (area-normalised).
        near = bright / (bright + outside) if bright + outside > 0 else 1.0
        return {
            'energy': energy,
            'bright': bright,
            'foam': foam,
            'outside': outside,
            'ring_energy': ring_energy,
            'near': near,
        }

    def _track(self, g):
        """Template match of the bobber around its calm place: (disp, ncc, dx, dy)."""
        th, tw = self.template.shape
        reach = int(max(4, min(90, self.size * 1.2)))
        x0, y0 = max(0, self.template_at[0] - reach), max(0, self.template_at[1] - reach)
        area = g[y0 : self.template_at[1] + th + reach, x0 : self.template_at[0] + tw + reach]
        step = 2 if self.size > 24 else 1  # half resolution keeps the wide search cheap
        found = ncc_search(area[::step, ::step], self.template[::step, ::step])
        disp, ncc, dx, dy = 0.0, 0.0, 0, 0
        if found:
            ix, iy, ncc = found
            dx, dy = ix * step + x0 - self.template_at[0], iy * step + y0 - self.template_at[1]
            disp = math.hypot(dx, dy) / self.size
        self.last_track = {'disp': disp, 'ncc': ncc, 'dx': dx, 'dy': dy}
        return self.last_track

    def _stats(self, refresh):
        """Robust calm statistics; refreshed at the slow cadence (cheap at 30 fps)."""
        if refresh or self.stats_cache is None:
            self.stats_cache = {
                key: robust(self.history[key], floor) if self.history[key] else (0.0, floor)
                for key, floor in FLOORS.items()
            }
            self.stats_cache['ncc_base'] = float(np.median(self.history['ncc'])) if self.history['ncc'] else None
        return self.stats_cache

    def _score(self, feats, refresh=True):
        zs = {}
        stats = self._stats(refresh)
        for key in FLOORS:
            if key in feats:
                med, spread = stats[key]
                zs[key] = (feats[key] - med) / spread
        # A splash brightens MANY core pixels STRONGLY; ripples and drifting colour
        # bands brighten many pixels weakly, glints a few strongly. Geometric mean.
        return math.sqrt(max(zs['bright'], 0) * max(zs['foam'], 0)), zs

    def observe(self, image, captured_at, now=None):
        now = captured_at if now is None else now
        t = captured_at - self.started_at
        a = rgb_array(image) if not isinstance(image, np.ndarray) else image
        self.frame_index += 1
        gap_bad = self.last_at is not None and (
            captured_at <= self.last_at or captured_at - self.last_at > self.p.max_gap
        )
        self.last_at = captured_at
        # Slow path (search, reference, calm baseline) keeps the validated ~10 Hz cadence
        # whatever the capture rate; the bite decision itself runs on every frame.
        slow = self.sample_last is None or captured_at - self.sample_last >= self.p.sample_interval
        if slow:
            self.sample_last = captured_at
        if gap_bad:
            self.scores.clear()
        if self.phase in ('lost', 'ambiguous', 'fired', 'no_reference'):
            return VisionResult(self.phase, self.reason, box=self.center and self._box(*self.center, self.size))
        if self.phase == 'reference':
            if t < self.p.reference_from:
                return VisionResult('reference', 'Жду, пока исчезнет прежний поплавок')
            if t > self.p.reference_until + 0.35 and not self.refs:
                self.phase = 'no_reference'
                self.reason = 'Наблюдение началось после заброса: этот заброс пропускается'
                return VisionResult(self.phase, self.reason)
            if t <= self.p.reference_until:
                if slow:
                    self.refs.append(self._small(a))
                    self.refs_native.append(a)
                return VisionResult('reference', 'Запоминаю воду до появления поплавка')
            stack = np.stack(self.refs)
            self.reference = np.median(stack, axis=0)
            # Median, not max: the flying bobber and line cross single reference frames.
            self.ref_noise = box_blur(np.median(np.abs(stack - self.reference), axis=0).sum(axis=2), 5)
            self.refs = []
            self.phase = 'searching'
        if self.phase == 'searching':
            if t < self.p.search_from:
                return VisionResult('searching', 'Поплавок летит')
            if not slow and self.search_last is not None:
                return self.search_last
            r = self._search(a, t)
            self.reason = r.reason
            self.search_last = r
            return r
        g, origin = self._window(a)
        if g is None:
            self.phase, self.reason = 'lost', 'Поплавок у края зоны: расширь область'
            return VisionResult(self.phase, self.reason)
        if self.window_shape is not None and g.shape != self.window_shape:
            self.phase, self.reason = 'lost', 'Окно поплавка изменилось'
            return VisionResult(self.phase, self.reason)
        box = self._box(*self.center, self.size)
        if self.phase == 'settling':
            if t - self.found_at < self.p.settle:
                return VisionResult('settling', 'Жду окончания всплеска заброса', box=box)
            if not self._refine(a):
                self.phase, self.reason = 'lost', 'Не удалось измерить поплавок после всплеска заброса'
                return VisionResult(self.phase, self.reason, box=box)
            self.window_box = None
            g, origin = self._window(a)
            box = self._box(*self.center, self.size)
            if g is None:
                self.phase, self.reason = 'lost', 'Поплавок у края зоны: расширь область'
                return VisionResult(self.phase, self.reason)
            if not self._take_template(g, origin):
                self.phase, self.reason = 'lost', 'Поплавок слишком мал или не контрастен'
                return VisionResult(self.phase, self.reason, box=box)
            self.phase = 'baseline'
        if self.phase == 'baseline' and not slow:
            return VisionResult(
                'baseline', f'Набираю спокойные кадры {self.baseline_count}/{self.p.baseline_frames}', box=box
            )
        feats = self._features(g, origin)
        if self.phase == 'baseline':
            feats.update(self._track(g))
            if gap_bad:
                return VisionResult(
                    'baseline', 'Разрыв кадров; продолжаю набор спокойных кадров', box=box, features=feats
                )
            if len(self.bg) >= 3:
                score, _ = self._score(feats, True) if len(self.history['energy']) >= 3 else (0, {})
                if score > 2 * CALM:
                    return VisionResult(
                        'baseline',
                        'Заметное движение до готовности; жду спокойную воду',
                        box=box,
                        score=score,
                        features=feats,
                    )
            self.bg.append(g)
            self.bg_median = None
            self._remember(feats, captured_at)
            self.baseline_count += 1
            if self.baseline_count >= self.p.baseline_frames:
                self.phase = 'armed'
                return VisionResult('armed', 'Готов: жду поклёвку', box=box, features=feats)
            return VisionResult(
                'baseline',
                f'Набираю спокойные кадры {self.baseline_count}/{self.p.baseline_frames}',
                box=box,
                features=feats,
            )
        score, zs = self._score(feats, slow)
        # Template tracking (the costliest step) at the slow cadence, and on every frame
        # whose score already matters for a decision.
        if slow or score >= self.p.threshold * self.p.low_ratio or self.last_track is None:
            feats.update(self._track(g))
        else:
            feats.update(self.last_track)
        # Camera/character moved: the bobber is clearly visible elsewhere AND the
        # water around it changed too. A bobber that moved over calm water may
        # be a bite and is left to the bite score.
        ring_med, ring_spread = self._stats(False)['ring_energy']
        ring_z = (feats['ring_energy'] - ring_med) / ring_spread
        shifted = feats['disp'] >= 0.45 and feats['ncc'] >= 0.75 and ring_z >= 3
        self.shifted_run = self.shifted_run + 1 if shifted else 0
        if shifted and self.shifted_since is None:
            self.shifted_since = captured_at
        elif not shifted:
            self.shifted_since = None
        if self.shifted_run >= 2 and captured_at - self.shifted_since >= self.p.sample_interval:
            # Twice in a row the bobber is clearly visible but far from its calm
            # place: camera or character moved. Measure calm water again around it.
            self.center = (self.center[0] + feats['dx'], self.center[1] + feats['dy'])
            self.window_box, self.core_shape, self.prev, self.shifted_run = None, None, None, 0
            self.shifted_since, self.bg_median = None, None
            self.scores.clear()
            self.bg.clear()
            for key in FLOORS:
                self.history[key].clear()
            self.history_t.clear()
            self.baseline_count = 0
            self.stats_cache, self.last_track = None, None
            g, origin = self._window(a)
            if g is None or not self._take_template(g, origin):
                self.phase, self.reason = 'lost', 'Камера сдвинулась: поплавок потерян'
                return VisionResult(self.phase, self.reason)
            self.phase = 'baseline'
            return VisionResult(
                'baseline',
                'Камера или поплавок сместились: заново набираю спокойные кадры',
                box=self._box(*self.center, self.size),
                features=feats,
            )
        calm = self._stats(False)
        for key in ('disp', 'ncc'):
            zs[key] = (feats[key] - calm[key][0]) / calm[key][1]
        feats = dict(feats, **{'z_' + k: v for k, v in zs.items()})
        stale = not 0 <= now - captured_at <= self.p.stale
        # Change away from the bobber (other player's splash, waves) is not our bite.
        # A splash changes the bobber's surroundings far more than the water
        # further away; a camera jump changes everything alike.
        local = ring_z < 6 or feats['ring_energy'] < 0.8 * feats['energy']
        feats['ring_z'] = ring_z
        located = bool(feats['near'] >= 0.35 and not shifted and local)
        feats['located'] = bool(located)
        # Ripples and glints brighten the water while the bobber stays clearly
        # visible in place; a bite hides or jerks it. "Clearly visible" is judged
        # against this bobber's own calm match: a small, far bobber never matches
        # its template as well as a close one (seen: 0.73 calm, 0.64-0.69 in ripples).
        base = calm['ncc_base']
        feats['ncc_base'] = base if base is not None else feats['ncc']
        intact = bool(feats['disp'] <= 0.35 and feats['ncc'] >= min(0.7, feats['ncc_base'] - self.p.intact_drop))
        feats['intact'] = intact
        reason = 'Готов: жду поклёвку'
        if score >= self.p.threshold * self.p.low_ratio and not located:
            reason = 'Изменение в стороне от поплавка — не поклёвка'
        # Partner frame: the one closest to pair_dt earlier (at 10 fps simply the previous
        # frame), so the decision spans the same ~0.1 s at any capture rate.
        prev = None
        for entry in self.scores:
            dt = captured_at - entry[2]
            if 0.6 * self.p.pair_dt <= dt <= self.p.max_gap and (
                prev is None or abs(dt - self.p.pair_dt) < abs(captured_at - prev[2] - self.p.pair_dt)
            ):
                prev = (entry[0], entry[1], entry[2], entry[3])
        self.prev = (score, located, captured_at, intact)
        self.scores.append(self.prev)
        while self.scores and captured_at - self.scores[0][2] > self.p.max_gap:
            self.scores.popleft()
        if gap_bad or stale:
            reason = 'Разрыв или старый кадр: последовательность сброшена'
        elif prev is not None and decide(prev[0], score, self.p, prev[3], intact) and prev[1] and located:
            self.phase = 'fired'
            self.reason = 'ПОКЛЁВКА'
            return VisionResult('fired', 'ПОКЛЁВКА', box=box, score=score, anomalous=True, fire=True, features=feats)
        anomalous = score >= self.p.threshold * self.p.low_ratio and located
        if anomalous:
            reason = 'Похоже на поклёвку, жду подтверждения следующим кадром'
        # Calm frames (fixed level, independent of the threshold) refresh the baseline.
        # Calm frames refresh the baseline: every one feeds the feature statistics (a
        # better spread estimate at higher rates), the median water image keeps ~10 Hz.
        if score < CALM:
            self._remember(feats, captured_at)
            if slow:
                self.bg.append(g)
                self.bg_median = None
        return VisionResult('armed', reason, box=box, score=score, anomalous=anomalous, features=feats)

    def _remember(self, feats, at):
        self.history_t.append(at)
        for key in FLOORS:
            self.history[key].append(feats[key])
        while self.history_t and at - self.history_t[0] > self.p.history_window:
            self.history_t.popleft()
            for key in FLOORS:
                self.history[key].popleft()


def decide(previous, current, params, previous_intact=False, current_intact=False):
    """Two consecutive fresh frames: one reaches the threshold with the bobber
    hidden or jerked (not intact), the other reaches low_ratio of it. A single
    bright frame (ripple, glare) or brightening around an intact bobber never fires."""
    high, low = params.threshold, params.threshold * params.low_ratio
    return (current >= high and not current_intact and previous >= low) or (
        previous >= high and not previous_intact and current >= low
    )


class VisionSession:
    """Routes frames to a CastVision per HUD cast serial; drops stale casts."""

    def __init__(self, params=None):
        self.params = params or VisionParams()
        self.current = None
        self.last = None

    def reset(self):
        self.current = self.last = None

    def rearm(self, cast):
        """After a false hook on this cast: keep watching it for the real bite."""
        if self.current is not None and self.current.cast == cast and self.current.phase == 'fired':
            self.current.phase, self.current.prev = 'armed', None
            self.current.scores.clear()
            self.current.reason = 'Ложная подсечка — снова жду поклёвку в этом забросе'
            self.last = None
            return True
        return False

    def observe(self, cast, cast_started_at, image, captured_at, now=None):
        if self.current is None or self.current.cast != cast:
            self.current = CastVision(cast, cast_started_at, self.params)
            self.last = None
        # Faster capture than the chosen rate: reuse the last decision (never a new signal).
        if self.last is not None and 0 <= captured_at - self.last[0] < 0.9 / max(1, self.params.fps):
            r = self.last[1]
            return VisionResult(r.phase, r.reason, r.box, r.others, r.score, False, False, r.features)
        result = self.current.observe(image, captured_at, now)
        self.last = (captured_at, result)
        return result
