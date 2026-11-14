"""Bite zone placement: a fixed-size zone centred on the bobber instead of a hand-drawn one.

Measured on the recorded sessions (4K, 13 sessions, ~80 casts): the bobber lands within
~230-380 x 220-275 px of its usual spot and the detector needs ~1.6 bobber sizes
(<= ~80 px at 4K) of water between the bobber and the zone edge. Zones of ~700 x 650 at
4K never lost a bobber at the edge; a 723 x 608 zone drawn by hand lost two (bobber
72 px from the bottom). The default is therefore 360 x 320 at 1080p, scaled with the
screen height, centred on the bobber, kept inside the window and away from the HUD.
"""

import statistics

BASE_SIZE = (360, 320)  # at 1080p
MIN_SCALE, MAX_SCALE = 0.7, 1.6  # mouse-wheel range relative to the default size
EDGE_WARNING = 0.18  # bobber closer to an edge than this share of the zone: suggest re-centring
MIN_SIDE, MAX_WIDTH, MAX_HEIGHT = 64, 2400, 1600  # same limits as vision.validate_region
HUD_GAP = 8  # px kept between the zone and the HUD strip


def default_size(screen, scale=1.0):
    """Zone size for this window: BASE_SIZE scaled by screen height and ``scale``."""
    factor = screen['height'] / 1080 * max(MIN_SCALE, min(MAX_SCALE, scale))
    width = round(BASE_SIZE[0] * factor)
    height = round(BASE_SIZE[1] * factor)
    width = max(MIN_SIDE, min(width, MAX_WIDTH, screen['width']))
    height = max(MIN_SIDE, min(height, MAX_HEIGHT, screen['height']))
    return width, height


def _overlaps(a, b, gap=0):
    return not (
        a['x'] + a['width'] + gap <= b['x']
        or b['x'] + b['width'] + gap <= a['x']
        or a['y'] + a['height'] + gap <= b['y']
        or b['y'] + b['height'] + gap <= a['y']
    )


def _clamp(region, screen):
    region['x'] = max(0, min(region['x'], screen['width'] - region['width']))
    region['y'] = max(0, min(region['y'], screen['height'] - region['height']))
    return region


def place(center, size, screen, hud=None):
    """Region of ``size`` centred on ``center`` (window pixels), inside the window, not on the HUD.

    If the centred zone would cover the HUD it is moved the shortest way off it. Raises
    ValueError when the zone cannot fit beside the HUD.
    """
    width, height = size
    if width > screen['width'] or height > screen['height']:
        raise ValueError('Зона больше окна игры')
    region = _clamp(
        {'x': round(center[0] - width / 2), 'y': round(center[1] - height / 2), 'width': width, 'height': height},
        screen,
    )
    if hud is None or not _overlaps(region, hud, HUD_GAP):
        return region
    candidates = []
    for x, y in (
        (hud['x'] - HUD_GAP - width, region['y']),  # left of the HUD
        (hud['x'] + hud['width'] + HUD_GAP, region['y']),  # right
        (region['x'], hud['y'] - HUD_GAP - height),  # above
        (region['x'], hud['y'] + hud['height'] + HUD_GAP),  # below
    ):
        moved = {'x': x, 'y': y, 'width': width, 'height': height}
        if 0 <= x <= screen['width'] - width and 0 <= y <= screen['height'] - height and not _overlaps(moved, hud):
            candidates.append((abs(x - region['x']) + abs(y - region['y']), moved))
    if not candidates:
        raise ValueError('Зона не помещается рядом с HUD рыбалки: передвинь HUD в игре')
    return min(candidates, key=lambda c: c[0])[1]


def edge_share(region, point):
    """Smallest distance from ``point`` (window px) to a zone edge, as a share of the zone side."""
    x, y = point[0] - region['x'], point[1] - region['y']
    return min(x / region['width'], (region['width'] - x) / region['width'],
               y / region['height'], (region['height'] - y) / region['height'])  # fmt: skip


def recentred(region, points, screen, hud=None):
    """Same-size zone centred on the median of recent bobber landing points (window px)."""
    if len(points) < 3:
        raise ValueError('Нужно хотя бы 3 заброса с найденным поплавком')
    center = (statistics.median(p[0] for p in points), statistics.median(p[1] for p in points))
    return place(center, (region['width'], region['height']), screen, hud)
