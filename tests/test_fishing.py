import unittest

from PIL import Image, ImageDraw

from ahelperw_fishing.controller import FishingController
from ahelperw_fishing.protocol import BiteSignal, Observation, decode, validate_export


def observation(t, **kw):
    defaults = dict(captured_at=t, state='idle', free=10, progress=0, cast=3, loot=4, heartbeat=int(t * 20) % 256)
    defaults.update(kw)
    return Observation(**defaults)


def render(data, scale=12):
    data = list(data)
    data.append(sum((i + 1) * v for i, v in enumerate(data)) % 256)
    image = Image.new('RGB', (26 * scale, 4 * scale))
    draw = ImageDraw.Draw(image)

    def cell(c, r, color):
        draw.rectangle((c * scale, r * scale, (c + 1) * scale - 1, (r + 1) * scale - 1), fill=color)

    for r in range(4):
        cell(0, r, (255, 0, 255))
        cell(25, r, (0, 255, 255))
    for i, byte in enumerate(data):
        for b in range(8):
            cell(1 + (i * 8 + b) % 24, (i * 8 + b) // 24, (255, 255, 255) if byte & (1 << (7 - b)) else (0, 0, 0))
    return image


class ProtocolTests(unittest.TestCase):
    def test_round_trip_at_different_scales(self):
        for size in (6, 9, 12, 17):
            obs = decode(render([1, 1, 1, 44, 128, 1, 7, 2, 8, 100, 0], size), 3)
            self.assertEqual((obs.free, obs.cast, obs.loot, obs.heartbeat), (300, 263, 520, 100))
            self.assertAlmostEqual(obs.progress, 128 / 255)
            self.assertEqual(obs.state, 'fishing')

    def test_unknown_is_not_empty(self):
        self.assertIsNone(decode(render([1, 5, 255, 255, 0, 0, 0, 0, 0, 9, 0]), 3).free)

    def test_corruption_and_occlusion_rejected(self):
        img = render([1, 0, 0, 10, 0, 0, 0, 0, 0, 9, 0])
        draw = ImageDraw.Draw(img)
        draw.rectangle((12, 0, 23, 11), fill='white')  # Mutated version bit.
        with self.assertRaises(ValueError):
            decode(img, 3)
        img = render([1, 0, 0, 10, 0, 0, 0, 0, 0, 9, 0])
        ImageDraw.Draw(img).rectangle((0, 0, 11, 11), fill='gray')
        with self.assertRaises(ValueError):
            decode(img, 3)

    def test_export_bounds(self):
        cfg = dict(
            schema='AHelperWForever.Fishing',
            version=1,
            screen=dict(width=1920, height=1080),
            rect=dict(x=0, y=0, width=312, height=48),
            mark=65,
        )
        validate_export(cfg)
        cfg['rect']['x'] = 1900
        with self.assertRaises(ValueError):
            validate_export(cfg)
        cfg['rect']['x'] = 0
        cfg['mark'] = float('nan')
        with self.assertRaises(ValueError):
            validate_export(cfg)


class CycleTests(unittest.TestCase):
    def ready(self, **kw):
        controller = FishingController(**kw)
        for t in (0, 0.1, 0.2):
            self.assertIsNone(controller.step(observation(t), t))
        self.assertEqual(controller.step(observation(0.8), 0.8), 'cast')
        controller.sent('cast', 0.8)
        return controller

    def fishing(self):
        c = self.ready()
        c.step(observation(0.9, state='fishing', cast=4, progress=0.01), 0.9)
        return c

    def test_complete_cycle_and_repeat(self):
        c = self.fishing()
        self.assertIsNone(c.step(observation(1, state='fishing', cast=4, progress=0.60), 1))
        self.assertEqual(c.step(observation(1.1, state='fishing', cast=4, progress=0.66), 1.1), 'interact')
        c.sent('interact', 1.1)
        self.assertIsNone(c.step(observation(1.2, state='fishing', cast=4, progress=0.7), 1.2))
        self.assertIsNone(c.step(observation(1.3, state='loot', cast=4), 1.3))
        c.step(observation(1.4, cast=4, loot=5), 1.4)
        self.assertEqual(c.cycles, 1)
        c.step(observation(2.3, cast=4, loot=5), 2.3)
        self.assertEqual(c.step(observation(2.9, cast=4, loot=5), 2.9), 'cast')

    def test_bag_full_and_reserve_stop(self):
        for reserve, free in ((0, 0), (3, 3)):
            c = self.ready(reserve=reserve)
            self.assertIsNone(c.step(observation(0.9, free=free), 0.9))
            self.assertEqual(c.phase, 'stopped')
            self.assertIsNone(c.step(observation(1), 1))

    def test_unknown_bags_and_blocked_input_stop(self):
        for kw in (
            dict(free=None),
            dict(state='blocked'),
            dict(error=True),
            dict(state='unknown'),
            dict(state='other_cast'),
        ):
            c = self.ready()
            c.step(observation(0.9, **kw), 0.9)
            self.assertEqual(c.phase, 'stopped')

    def test_no_cast_ack_no_retry(self):
        c = self.ready()
        for t in (1, 2, 3, 4, 5):
            c.step(observation(t), t)
        self.assertEqual(c.phase, 'stopped')

    def test_old_cast_cannot_confirm_new_press(self):
        c = self.ready()
        c.step(observation(1, state='fishing', cast=3, progress=0.7), 1)
        self.assertEqual(c.phase, 'wait_cast')

    def test_no_loot_ack_no_recast(self):
        c = self.fishing()
        c.step(observation(1, state='fishing', cast=4, progress=0.65), 1)
        c.sent('interact', 1)
        for t in range(2, 13):
            self.assertIsNone(c.step(observation(t, cast=4), t))
        self.assertEqual(c.phase, 'stopped')
        self.assertEqual(c.cycles, 0)

    def test_interrupted_or_missed_mark(self):
        for kw in (dict(state='fishing', cast=5), dict(state='fishing', cast=4, progress=0.9)):
            c = self.fishing()
            self.assertIsNone(c.step(observation(1, **kw), 1))
            self.assertEqual(c.phase, 'stopped')

    def test_cast_without_signal_recasts_then_stops(self):
        c = self.fishing()
        self.assertIsNone(c.step(observation(1, cast=4), 1))  # channel ended, no press
        self.assertEqual((c.phase, c.stats.no_signal), ('cooldown', 1))
        c.step(observation(1.9, cast=4), 1.9)
        self.assertEqual(c.step(observation(2.5, cast=4), 2.5), 'cast')
        c.sent('cast', 2.5)
        c.step(observation(2.6, state='fishing', cast=5), 2.6)
        c.step(observation(3, cast=5), 3)
        self.assertEqual(c.phase, 'stopped')
        self.assertIn('без сигнала', c.reason)

    def test_freeze_stale_and_missing_hud(self):
        c = self.fishing()
        c.step(observation(2, state='fishing', cast=4, heartbeat=18), 2)
        self.assertEqual(c.phase, 'stopped')
        for sample in (None, observation(0)):
            c = self.fishing()
            self.assertIsNone(c.step(sample, 2))
            self.assertEqual(c.phase, 'stopped')

    def test_start_during_channel_waits_for_it_to_end(self):
        c = FishingController()
        for t in (0, 0.1, 0.2, 0.3):
            self.assertIsNone(c.step(observation(t, state='fishing'), t))
        self.assertEqual(c.phase, 'warmup')
        self.assertIn('Жду окончания', c.reason)
        for t in (0.4, 0.5, 1.0):
            c.step(observation(t, state='idle'), t)
        self.assertEqual(c.phase, 'idle')
        self.assertEqual(c.step(observation(1.0, state='idle'), 1.0), 'cast')

    def test_start_wait_has_a_limit(self):
        c = FishingController()
        for t in (0, 0.1, 0.2):
            c.step(observation(t, state='loot'), t)
        for t in [0.3 + 0.2 * i for i in range(310)]:
            c.step(observation(t, state='loot'), t)
        self.assertEqual(c.phase, 'stopped')
        self.assertIn('Начни с завершённого заброса', c.reason)

    def test_rollover_and_no_unsent_transition(self):
        c = self.ready()
        c.cast = 65535
        c.step(observation(1, state='fishing', cast=0, progress=0.1), 1)
        self.assertEqual(c.phase, 'wait_mark')
        with self.assertRaises(ValueError):
            c.sent('interact', 1)


class ProtocolV2Tests(unittest.TestCase):
    def test_v2_fields(self):
        obs = decode(render([2, 1, 0, 9, 10, 1, 4, 7, 4, 50, 3 * 16 + 2]), 1)
        self.assertEqual(
            (obs.protocol, obs.cast, obs.loot, obs.loot_cast, obs.error_serial, obs.error_code), (2, 260, 7, 4, 3, 2)
        )
        self.assertFalse(obs.error)
        with self.assertRaises(ValueError):
            decode(render([2, 1, 0, 9, 10, 1, 4, 7, 4, 50, 9]), 1)
        with self.assertRaises(ValueError):
            decode(render([3, 1, 0, 9, 10, 1, 4, 7, 4, 50, 0]), 1)


def v2(t, **kw):
    defaults = dict(protocol=2, loot=4, loot_cast=0, error_serial=1, error_code=0)
    defaults.update(kw)
    return observation(t, **defaults)


class V2CycleTests(unittest.TestCase):
    def armed_cast(self):
        c = FishingController(mode='vision')
        for t in (0, 0.1, 0.2):
            c.step(v2(t), t)
        self.assertEqual(c.step(v2(0.8), 0.8), 'cast')
        c.sent('cast', 0.8)
        c.step(v2(0.9, state='fishing', cast=4), 0.9)
        return c

    def test_manual_cast_is_adopted(self):
        c = FishingController(mode='vision')
        for t in (0, 0.1, 0.2):
            c.step(v2(t), t)
        # The player casts before our 0.5 s idle pause ends.
        self.assertIsNone(c.step(v2(0.4, state='fishing', cast=4), 0.4))
        self.assertEqual((c.phase, c.cast, c.stats.casts), ('wait_bite', 4, 1))
        self.assertEqual(c.step(v2(1, state='fishing', cast=4), 1, BiteSignal(1, 4)), 'interact')

    def test_pending_cast_dropped_when_player_casts(self):
        c = FishingController(mode='vision')
        for t in (0, 0.1, 0.2):
            c.step(v2(t), t)
        self.assertEqual(c.step(v2(0.8), 0.8), 'cast')  # not sent (e.g. a key is held)
        self.assertIsNone(c.step(v2(0.9, state='fishing', cast=4), 0.9))
        self.assertEqual((c.phase, c.cast, c.pending), ('wait_bite', 4, None))
        self.assertTrue(any(e['kind'] == 'press_dropped' for e in c.stats.events))

    def test_hook_not_sent_in_time_is_dropped(self):
        c = self.armed_cast()
        self.assertEqual(c.step(v2(1, state='fishing', cast=4), 1, BiteSignal(1, 4)), 'interact')
        self.assertEqual(c.step(v2(1.3, state='fishing', cast=4), 1.3), 'interact')  # still in time
        self.assertIsNone(c.step(v2(1.7, state='fishing', cast=4), 1.7))  # fish is gone
        self.assertEqual((c.phase, c.pending), ('wait_bite', None))
        with self.assertRaises(ValueError):
            c.sent('interact', 1.7)

    def test_hook_dropped_when_cast_ends(self):
        c = self.armed_cast()
        self.assertEqual(c.step(v2(1, state='fishing', cast=4), 1, BiteSignal(1, 4)), 'interact')
        self.assertIsNone(c.step(v2(1.1, state='idle', cast=4), 1.1))
        self.assertEqual((c.phase, c.stats.no_signal), ('cooldown', 1))

    def wait_loot(self, auto_recast=False):
        c = FishingController(mode='vision', auto_recast=auto_recast)
        for t in (0, 0.1, 0.2):
            c.step(v2(t), t)
        self.assertEqual(c.step(v2(0.8), 0.8), 'cast')
        c.sent('cast', 0.8)
        c.step(v2(0.9, state='fishing', cast=4), 0.9)
        self.assertEqual(c.step(v2(1, state='fishing', cast=4), 1, BiteSignal(1, 4)), 'interact')
        c.sent('interact', 1)
        return c

    def test_catch_must_name_this_cast(self):
        c = self.wait_loot()
        c.step(v2(1.1, cast=4, loot=5, loot_cast=3), 1.1)  # belongs to an older cast
        self.assertEqual(c.phase, 'wait_loot')
        c.step(v2(1.2, cast=4, loot=5, loot_cast=4), 1.2)
        self.assertEqual((c.phase, c.stats.catches), ('cooldown', 1))

    def test_new_cast_alone_is_not_a_catch(self):
        c = self.wait_loot(auto_recast=True)
        self.assertIsNone(c.step(v2(1.1, state='fishing', cast=5), 1.1))
        self.assertEqual((c.phase, c.stats.catches), ('wait_loot', 0))
        for t in (2, 3, 4, 5.1):
            c.step(v2(t, state='fishing', cast=5), t)
        self.assertEqual(c.phase, 'stopped')
        self.assertEqual(c.stats.false_signals, 1)

    def test_auto_recast_adopted_without_second_press(self):
        c = self.wait_loot(auto_recast=True)
        self.assertIsNone(c.step(v2(1.1, state='fishing', cast=5, loot=5, loot_cast=4), 1.1))
        self.assertEqual((c.phase, c.cast, c.stats.catches), ('wait_bite', 5, 1))

    def test_recast_key_waits_before_casting_itself(self):
        c = self.wait_loot(auto_recast=True)
        c.step(v2(1.1, cast=4, loot=5, loot_cast=4), 1.1)
        self.assertEqual(c.phase, 'await_recast')
        for t in (1.5, 2.5, 3.4):
            self.assertIsNone(c.step(v2(t, cast=4, loot=5, loot_cast=4), t))
        # Nothing was pressed since the catch, so a self-recast is adopted, not doubled.
        self.assertIsNone(c.step(v2(3.5, state='fishing', cast=5, loot=5, loot_cast=4), 3.5))
        self.assertEqual((c.phase, c.cast), ('wait_bite', 5))
        c = self.wait_loot(auto_recast=True)
        c.step(v2(1.1, cast=4, loot=5, loot_cast=4), 1.1)
        c.step(v2(3.7, cast=4, loot=5, loot_cast=4), 3.7)
        self.assertEqual(c.phase, 'idle')
        self.assertEqual(c.step(v2(4.3, cast=4, loot=5, loot_cast=4), 4.3), 'cast')

    def test_escape_after_hook_continues_then_stops_after_three(self):
        c = self.wait_loot()
        c.step(v2(1.1, state='fishing', cast=4, error_serial=2, error_code=3), 1.1)
        self.assertEqual((c.phase, c.stats.escaped), ('cooldown', 1))
        c.step(v2(1.5, cast=4, error_serial=2, error_code=3), 1.5)
        c.step(v2(2.2, cast=4, error_serial=2, error_code=3), 2.2)
        self.assertEqual(c.step(v2(2.8, cast=4, error_serial=2, error_code=3), 2.8), 'cast')
        serial = 2
        for k in range(2):
            c.sent('cast', 3 + k * 10)
            base = 3 + k * 10
            c.step(v2(base + 0.1, state='fishing', cast=5 + k, error_serial=serial, error_code=3), base + 0.1)
            self.assertEqual(
                c.step(
                    v2(base + 0.5, state='fishing', cast=5 + k, error_serial=serial, error_code=3),
                    base + 0.5,
                    BiteSignal(base + 0.5, 5 + k),
                ),
                'interact',
            )
            c.sent('interact', base + 0.5)
            serial += 1
            c.step(v2(base + 0.6, state='fishing', cast=5 + k, error_serial=serial, error_code=3), base + 0.6)
            if k == 0:
                for t in (base + 1, base + 2, base + 2.6):
                    action = c.step(v2(t, cast=5 + k, error_serial=serial, error_code=3), t)
                self.assertEqual(action, 'cast')
        self.assertEqual(c.phase, 'stopped')
        self.assertEqual(c.stats.escaped, 3)

    def test_false_hook_rearms_same_cast_then_three_stop(self):
        c = self.wait_loot()
        c.step(v2(1.1, state='fishing', cast=4, error_serial=2, error_code=2), 1.1)
        self.assertEqual((c.phase, c.cast, c.rearm, c.stats.false_signals), ('wait_bite', 4, 4, 1))
        c.rearm = None
        self.assertEqual(
            c.step(v2(3, state='fishing', cast=4, error_serial=2, error_code=2), 3, BiteSignal(3, 4)), 'interact'
        )
        c.sent('interact', 3)
        c.step(v2(3.1, state='fishing', cast=4, error_serial=3, error_code=2), 3.1)
        self.assertEqual(c.phase, 'wait_bite')
        c.step(v2(5, state='fishing', cast=4, error_serial=3, error_code=2), 5, BiteSignal(5, 4))
        c.sent('interact', 5)
        c.step(v2(5.1, state='fishing', cast=4, error_serial=4, error_code=2), 5.1)
        self.assertEqual(c.phase, 'stopped')
        self.assertEqual(c.stats.false_signals, 3)

    def test_catch_resets_false_hook_run(self):
        c = self.wait_loot()
        c.step(v2(1.1, state='fishing', cast=4, error_serial=2, error_code=2), 1.1)
        c.step(v2(2, state='fishing', cast=4, error_serial=2, error_code=2), 2, BiteSignal(2, 4))
        c.sent('interact', 2)
        c.step(v2(2.2, cast=4, loot=5, loot_cast=4, error_serial=2, error_code=2), 2.2)
        self.assertEqual((c.false_run, c.stats.catches), (0, 1))

    def test_game_errors_stop_and_are_classified(self):
        for code, counter in ((4, None), (1, None)):
            c = self.wait_loot()
            c.step(v2(1.1, cast=4, error_serial=2, error_code=code), 1.1)
            self.assertEqual(c.phase, 'stopped')
            if counter:
                self.assertEqual(getattr(c.stats, counter), 1)

    def test_escape_without_press_is_a_miss_not_a_stop(self):
        c = FishingController(mode='vision')
        for t in (0, 0.1, 0.2):
            c.step(v2(t), t)
        c.step(v2(0.8), 0.8)
        c.sent('cast', 0.8)
        c.step(v2(0.9, state='fishing', cast=4), 0.9)
        c.step(v2(5, state='fishing', cast=4, error_serial=2, error_code=3), 5)
        self.assertEqual(c.phase, 'wait_bite')
        c.step(v2(5.2, cast=4, error_serial=2, error_code=3), 5.2)
        self.assertEqual((c.phase, c.stats.no_signal, c.stats.escaped), ('cooldown', 1, 0))

    def test_press_that_does_not_end_cast_stops(self):
        c = self.wait_loot()
        for t in (1.5, 2.5, 3.1):
            c.step(v2(t, state='fishing', cast=4), t)
        self.assertEqual(c.phase, 'stopped')

    def test_stale_or_pre_cast_signal_ignored(self):
        c = FishingController(mode='vision')
        for t in (0, 0.1, 0.2):
            c.step(v2(t), t)
        c.step(v2(0.8), 0.8)
        c.sent('cast', 0.8)
        c.step(v2(0.9, state='fishing', cast=4), 0.9)
        self.assertIsNone(c.step(v2(1.0, state='fishing', cast=4), 1.0, BiteSignal(0.85, 4)))
        self.assertIsNone(c.step(v2(1.5, state='fishing', cast=4), 1.5, BiteSignal(1.1, 4)))


if __name__ == '__main__':
    unittest.main()
