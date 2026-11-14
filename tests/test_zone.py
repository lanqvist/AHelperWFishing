"""Fixed-size bite zone placement and re-centring."""

import sys
from types import SimpleNamespace

import pytest

from ahelperw_fishing import zone
from ahelperw_fishing.vision import validate_region

SCREEN_4K = {'width': 3840, 'height': 2160}
SCREEN_HD = {'width': 1920, 'height': 1080}
HUD = {'x': 93, 'y': 745, 'width': 571, 'height': 88}  # the real 4K HUD position


def test_default_size_follows_screen_height():
    assert zone.default_size(SCREEN_HD) == zone.BASE_SIZE
    assert zone.default_size(SCREEN_4K) == (720, 640)  # the size that never lost a bobber at 4K
    assert zone.default_size(SCREEN_4K, scale=5) == zone.default_size(SCREEN_4K, scale=zone.MAX_SCALE)
    assert zone.default_size({'width': 800, 'height': 600}, scale=1.6) == (320, 284)
    assert zone.default_size({'width': 300, 'height': 2160}) == (300, 640)  # never wider than the window


def test_place_centres_and_validates():
    region = zone.place((1950, 600), zone.default_size(SCREEN_4K), SCREEN_4K, HUD)
    assert region == {'x': 1590, 'y': 280, 'width': 720, 'height': 640}
    assert validate_region(region, SCREEN_4K) is region


@pytest.mark.parametrize('center', [(0, 0), (3840, 2160), (5, 2000), (3800, 10)])
def test_place_stays_inside_the_window(center):
    region = zone.place(center, zone.default_size(SCREEN_4K), SCREEN_4K)
    validate_region(region, SCREEN_4K)


def test_place_moves_off_the_hud():
    region = zone.place((380, 790), zone.default_size(SCREEN_4K), SCREEN_4K, HUD)
    assert not zone._overlaps(region, HUD)
    validate_region(region, SCREEN_4K)


def test_place_fails_when_nothing_fits():
    hud = {'x': 0, 'y': 0, 'width': 1900, 'height': 1060}
    with pytest.raises(ValueError, match='HUD'):
        zone.place((960, 540), zone.default_size(SCREEN_HD), SCREEN_HD, hud)


def test_real_case_bobber_at_the_bottom_edge():
    """2026-11-14: a 723x608 zone at (1597, 253) lost casts 4 and 10 (bobber ~72 px from the bottom)."""
    drawn = {'x': 1597, 'y': 253, 'width': 723, 'height': 608}
    landings = [(1900, 640), (1950, 788), (1840, 700), (2018, 560), (1789, 620)]
    assert zone.edge_share(drawn, (1950, 788)) < zone.EDGE_WARNING
    fixed = zone.recentred({'x': 0, 'y': 0, 'width': 720, 'height': 640}, landings, SCREEN_4K, HUD)
    assert all(zone.edge_share(fixed, p) > zone.EDGE_WARNING for p in landings)


def test_recentred_needs_three_points():
    with pytest.raises(ValueError):
        zone.recentred({'x': 0, 'y': 0, 'width': 720, 'height': 640}, [(1, 1), (2, 2)], SCREEN_4K)


@pytest.mark.skipif(sys.platform != 'win32', reason='Tk on Windows')
def test_selector_places_fixed_zone_on_click():
    import tkinter as tk

    from PIL import Image

    from ahelperw_fishing.training import RegionSelector

    root = tk.Tk()
    root.withdraw()
    chosen = []
    try:
        selector = RegionSelector(root, Image.new('RGB', (3840, 2160)), lambda r, s: chosen.append((r, s)), hud=HUD)
        selector.withdraw()
        selector.wheel(SimpleNamespace(delta=120))  # one step bigger
        selector.finish(SimpleNamespace(x=600, y=180))  # preview is 1200 px wide: x2/y2 is 3.2
        region, scale = chosen[0]
        assert scale == pytest.approx(1.1)
        assert (region['width'], region['height']) == zone.default_size(SCREEN_4K, 1.1)
        assert abs(region['x'] + region['width'] / 2 - 1920) <= 1
        assert not selector.winfo_exists()
    finally:
        root.destroy()
