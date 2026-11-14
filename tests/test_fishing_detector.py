"""Decision rules of the per-cast bobber detector on synthetic sequences."""

import unittest

from ahelperw_fishing.vision import CastVision, VisionParams, VisionSession, decide, validate_region
from fishing_synthetic import frame, times


def run(until=14, start=0.0, stale_at=(), gap_after=None, params=None, step=None, **scene):
    v = CastVision(7, 0.0, params or VisionParams())
    fires, phases, results = [], [], []
    for t in times(until, start, step) if step else times(until, start):
        if gap_after is not None and gap_after[0] < t < gap_after[1]:
            continue
        now = t + (0.5 if any(abs(t - s) < 0.07 for s in stale_at) else 0)
        r = v.observe(frame(t, **scene), t, now)
        results.append((t, r))
        phases.append(r.phase)
        if r.fire:
            fires.append(t)
    return fires, phases, results, v


class DetectorTests(unittest.TestCase):
    def test_finds_our_new_bobber_and_fires_once_on_splash(self):
        fires, phases, results, v = run(bite=9.0)
        self.assertEqual(len(fires), 1, fires)
        self.assertTrue(9.0 - 0.15 <= fires[0] <= 9.5, fires)
        self.assertIn('armed', phases)
        self.assertLess(abs(v.center[0] - 210), 12)
        self.assertLess(abs(v.center[1] - 150), 22)  # box covers body + feather above it

    def test_ripples_and_waves_without_bite_never_fire(self):
        fires, phases, *_ = run(until=16, ripples=(5.0, 7.5, 11.0, 13.2))
        self.assertEqual(fires, [])
        self.assertEqual(phases[-1], 'armed')

    def test_other_players_bobber_is_not_ours(self):
        # Present before our cast, bobbing, with its own splash far from ours.
        fires, _, _, v = run(until=16, others=[(360, 60)], other_splash=(360, 60, 8.0), bite=13.0)
        self.assertLess(abs(v.center[0] - 210), 12)
        self.assertEqual(len(fires), 1)
        self.assertGreaterEqual(fires[0], 13.0 - 0.15)

    def test_previous_bobber_visible_at_cast_start_is_ignored(self):
        fires, _, _, v = run(old=(110, 230), old_until=0.42, bite=8.0)
        self.assertLess(abs(v.center[0] - 210), 12)
        self.assertEqual(len(fires), 1)

    def test_splash_beside_our_bobber_is_not_a_bite(self):
        fires, *_ = run(until=12, other_splash=(300, 150, 7.0))
        self.assertEqual(fires, [])

    def test_two_new_objects_are_ambiguous_and_never_fire(self):
        fires, phases, *_ = run(late_bobber=(80, 80, 1.0), bite=8.0)
        self.assertEqual(fires, [])
        self.assertIn('ambiguous', phases)

    def test_observation_started_mid_cast_is_skipped(self):
        fires, phases, *_ = run(start=4.0, bite=8.0)
        self.assertEqual(fires, [])
        self.assertEqual(phases[-1], 'no_reference')

    def test_no_bobber_means_no_signal(self):
        fires, phases, *_ = run(ours=None, until=8)
        self.assertEqual(fires, [])
        self.assertEqual(phases[-1], 'lost')

    def test_stale_frames_never_fire(self):
        fires, *_ = run(bite=9.0, stale_at=[9.0 + k * (1 / 8.3) for k in range(-1, 5)])
        self.assertEqual(fires, [])

    def test_gap_in_frames_breaks_the_pair(self):
        # Only one splash frame survives on each side of a 0.5 s hole.
        fires, *_ = run(bite=9.0, bite_len=0.13, gap_after=(9.05, 9.6))
        self.assertEqual(fires, [])

    def test_camera_shift_does_not_fire(self):
        fires, *_ = run(until=12, shift_at=6.0, shift=(0, 30))
        self.assertEqual(fires, [])

    def test_larger_bobber_scale(self):
        fires, *_ = run(bite=9.0, scale=1.6, ours=(210, 140))
        self.assertEqual(len(fires), 1)

    def test_small_far_bobber_ripples_do_not_fire_but_splash_does(self):
        fires, *_ = run(until=14, scale=0.7, ripples=(5.0, 7.0, 9.0, 11.0))
        self.assertEqual(fires, [])
        fires, *_ = run(until=12, scale=0.7, bite=9.0)
        self.assertEqual(len(fires), 1)
        self.assertTrue(9.0 - 0.15 <= fires[0] <= 9.5, fires)

    def test_rearm_after_false_hook_allows_the_real_bite(self):
        s = VisionSession()
        fires = []
        for t_ in times(13):
            r = s.observe(7, 0.0, frame(t_, bite=11.0, ripples=()), t_, t_)
            if r.fire:
                fires.append(t_)
                if len(fires) == 1:
                    self.assertTrue(s.rearm(7))
        self.assertEqual(len(fires), 1)
        self.assertFalse(s.rearm(8))

    def test_decision_rule(self):
        p = VisionParams(threshold=6, low_ratio=0.4)
        self.assertTrue(decide(2.5, 13, p))
        self.assertTrue(decide(13, 2.5, p))
        self.assertFalse(decide(13, 2.3, p))
        self.assertFalse(decide(5.9, 5.9, p))

    def test_session_routes_by_cast_serial(self):
        s = VisionSession()
        a = s.observe(1, 0.0, frame(0), 0.0)
        b = s.observe(2, 5.0, frame(0), 5.0)
        self.assertIsNot(s.current, None)
        self.assertEqual(s.current.cast, 2)
        self.assertEqual((a.phase, b.phase), ('reference', 'reference'))

    def test_session_throttles_to_chosen_rate(self):
        s = VisionSession(VisionParams(fps=30))
        s.observe(1, 0.0, frame(0.30), 0.30)
        again = s.observe(1, 0.0, frame(0.31), 0.31)  # 10 ms later: above 30 fps, not processed
        self.assertEqual(s.current.frame_index, 1)
        self.assertFalse(again.fire)
        s.observe(1, 0.0, frame(0.34), 0.34)
        self.assertEqual(s.current.frame_index, 2)

    def test_same_decisions_at_30_fps(self):
        step = 1 / 30
        fires, phases, *_ = run(bite=9.0, step=step)
        self.assertEqual(len(fires), 1, fires)
        self.assertTrue(9.0 - 0.15 <= fires[0] <= 9.3, fires)
        self.assertEqual(run(until=16, ripples=(5.0, 7.5, 11.0, 13.2), step=step)[0], [])
        self.assertEqual(run(until=12, shift_at=6.0, shift=(0, 30), step=step)[0], [])
        self.assertEqual(run(until=14, scale=0.7, ripples=(5.0, 7.0, 9.0, 11.0), step=step)[0], [])
        self.assertIn('ambiguous', run(late_bobber=(80, 80, 1.0), bite=8.0, until=9, step=step)[1])

    def test_30_fps_signal_not_later_than_10_fps(self):
        slow = run(bite=9.0)[0][0]
        fast = run(bite=9.0, step=1 / 30)[0][0]
        self.assertLessEqual(fast, slow + 1e-9)

    def test_region_limits(self):
        screen = dict(width=3840, height=2160)
        validate_region(dict(x=500, y=300, width=1200, height=600), screen)
        with self.assertRaises(ValueError):
            validate_region(dict(x=3000, y=300, width=1200, height=600), screen)
        with self.assertRaises(ValueError):
            validate_region(dict(x=0, y=0, width=40, height=600), screen)


if __name__ == '__main__':
    unittest.main()
