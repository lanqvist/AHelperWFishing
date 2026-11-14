"""Windows GUI wiring tests; all captures and input pulses are mocked."""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_fishing import render


def make_app(module, attempts=4):
    """Create the window; retry when Tcl's library lookup fails transiently.

    Creating many Tcl interpreters in one process on Windows sometimes fails with
    "Can't find a usable init.tcl" / "tcl_findLibrary" (also with the 0.3.x code). The app
    itself creates exactly one interpreter, so only the test suite needs this.
    """
    import tkinter

    for attempt in range(attempts):
        try:
            return module.FishingApp()
        except tkinter.TclError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.2)
    return None


@unittest.skipUnless(sys.platform == 'win32', 'Windows desktop integration')
class AppTests(unittest.TestCase):
    def setUp(self):
        from ahelperw_fishing import app as fishing_app

        self.module = fishing_app
        self.temp = tempfile.TemporaryDirectory()
        self.root_patch = patch.dict(os.environ, {'AHWF_DATA_DIR': self.temp.name})
        self.root_patch.start()
        self.updates_patch = patch.object(fishing_app.updates, 'check_async')
        self.updates_patch.start()
        self.addCleanup(self.updates_patch.stop)
        self.app = make_app(fishing_app)
        self.app.withdraw()
        self.app.mode.set('По отметке шкалы')
        self.app.same_key.set(False)
        self.app.cfg = dict(
            schema='AHelperWForever.Fishing',
            version=1,
            screen=dict(width=1920, height=1080),
            rect=dict(x=0, y=0, width=312, height=48),
            mark=65,
        )
        self.app.capture = Mock()
        self.hud = render([1, 0, 0, 10, 0, 0, 1, 0, 0, 11, 0])
        self.app.capture.grab.return_value = self.hud
        self.send_patch = patch.object(fishing_app, 'pulse', return_value='sent')
        self.send = self.send_patch.start()
        self.focus_patch = patch.object(fishing_app, 'game_window', return_value=(123, 0, 0, 1920, 1080))
        self.focus = self.focus_patch.start()
        self.stop_patch = patch.object(fishing_app, 'stop_pressed', return_value=False)
        self.stop_key = self.stop_patch.start()

    def tearDown(self):
        self.app.close()
        for p in (self.stop_patch, self.focus_patch, self.send_patch, self.root_patch):
            p.stop()
        self.temp.cleanup()

    def vision_setup(self, ready=False):
        from fishing_synthetic import frame

        self.app.mode.set('По изображению поплавка')
        self.app.region = dict(x=600, y=300, width=420, height=300)
        if ready:
            model = {'version': 1, 'params': {'threshold': 6}, 'ready': [True, 'ok'], 'summary': 'test'}
            self.app.set_model(model)

        def grab(bounds):
            if bounds[0] == 600:
                return frame(3.0)
            return self.hud

        self.app.capture.grab.side_effect = grab

    def test_observation_never_sends(self):
        self.assertFalse(self.app.auto.get())
        self.app.start()
        self.app.sample()
        self.send.assert_not_called()
        self.assertTrue(self.app.running)

    def test_action_sent_and_acknowledged(self):
        self.app.auto.set(True)
        self.app.start()
        controller = Mock(phase='wait_mark', reason='wait', cycles=0, mode='mark')
        controller.step.return_value = 'interact'
        self.app.controller = controller
        self.app.sample()
        self.send.assert_called_once()
        self.assertEqual(self.send.call_args.args[0], self.module.binding('F'))
        self.assertTrue(self.send.call_args.args[1]())
        self.assertEqual(controller.sent.call_args.args[0], 'interact')
        self.assertEqual(len(self.app.timing['press']), 1)

    def test_focus_loss_after_acquisition_stops(self):
        self.app.auto.set(True)
        self.app.start()
        self.app.sample()
        self.focus.side_effect = ValueError('lost focus')
        self.app.sample()
        self.assertFalse(self.app.running)
        self.send.assert_not_called()

    def test_observation_pauses_on_focus_loss(self):
        self.app.start()
        self.app.sample()
        self.focus.side_effect = ValueError('Forever не в фокусе')
        self.app.sample()
        self.assertTrue(self.app.running)
        self.assertIn('Пауза', self.app.status.get())
        self.focus.side_effect = None
        self.app.sample()
        self.assertTrue(self.app.running)
        self.send.assert_not_called()

    def test_f8_stops_without_input(self):
        self.app.auto.set(True)
        self.app.start()
        self.stop_key.return_value = True
        self.app.sample()
        self.assertFalse(self.app.running)
        self.send.assert_not_called()

    def test_failed_send_not_acknowledged(self):
        self.app.auto.set(True)
        self.app.start()
        controller = Mock(phase='idle', reason='ready', cycles=0, mode='mark')
        controller.step.return_value = 'cast'
        self.app.controller = controller
        self.send.return_value = 'focus_or_stop'
        self.app.sample()
        self.assertFalse(self.app.running)
        controller.sent.assert_not_called()

    def background_setup(self):
        self.app.background.set(True)
        self.app.same_key.set(False)
        self.app.cast_key.set('0')
        self.app.interact_key.set('9')
        self.post_patch = patch.object(self.module, 'post_pulse', return_value='sent')
        self.post = self.post_patch.start()
        self.addCleanup(self.post_patch.stop)
        self.vis_patch = patch.object(self.module, 'visible', return_value=True)
        self.vis = self.vis_patch.start()
        self.addCleanup(self.vis_patch.stop)
        self.fg_patch = patch.object(self.module, 'foreground_window', return_value=999)
        self.fg_patch.start()
        self.addCleanup(self.fg_patch.stop)

    def test_background_keeps_running_and_posts_to_game_window(self):
        self.background_setup()
        self.app.auto.set(True)
        self.app.start()
        self.app.sample()
        self.focus.side_effect = lambda screen=None, hwnd=None: (
            (123, 0, 0, 1920, 1080) if hwnd == 123 else (_ for _ in ()).throw(ValueError('Forever не в фокусе'))
        )
        controller = Mock(phase='wait_bite', reason='wait', cycles=0, mode='mark')
        controller.step.return_value = 'interact'
        self.app.controller = controller
        self.app.sample()
        self.assertTrue(self.app.running)
        self.post.assert_called_once()
        self.assertEqual(self.post.call_args.args[0], 123)
        self.send.assert_not_called()

    def test_background_covered_window_stops_auto(self):
        self.background_setup()
        self.app.auto.set(True)
        self.app.start()
        self.app.sample()
        self.vis.return_value = False
        self.app.sample()
        self.assertFalse(self.app.running)
        self.post.assert_not_called()

    def test_background_rejects_modifier_keys(self):
        self.background_setup()
        self.app.cast_key.set('Shift+Q')
        self.app.auto.set(True)
        with patch.object(self.module.messagebox, 'showerror') as error:
            self.app.start()
        error.assert_called_once()
        self.assertFalse(self.app.running)

    def test_held_modifier_delays_press_instead_of_stopping(self):
        self.app.auto.set(True)
        self.app.start()
        controller = Mock(phase='wait_mark', reason='wait', cycles=0, mode='mark')
        controller.step.return_value = 'interact'
        self.app.controller = controller
        self.send.return_value = 'physical_modifier_held'
        self.app.sample()
        self.assertTrue(self.app.running)
        controller.sent.assert_not_called()

    def test_f8_cannot_be_bound(self):
        with self.assertRaises(ValueError):
            self.module.binding('F8')
        self.assertEqual(self.module.binding('F')['vk'], 70)
        self.assertEqual(self.module.binding('F12')['vk'], 123)

    def test_modifier_bindings(self):
        self.assertEqual(self.module.binding('Shift+Q'), {'device': 'keyboard', 'vk': ord('Q'), 'modifiers': ['SHIFT']})
        self.assertEqual(self.module.binding('Ctrl+F2')['modifiers'], ['CTRL'])
        self.assertEqual(self.module.binding('9')['modifiers'], [])
        with self.assertRaises(ValueError):
            self.module.binding('Shift+F8')

    def test_separate_cast_and_interact_keys(self):
        self.app.same_key.set(False)
        self.app.cast_key.set('Shift+Q')
        self.app.interact_key.set('9')
        self.app.start()
        self.assertEqual(self.app.bindings['cast']['modifiers'], ['SHIFT'])
        self.assertEqual(self.app.bindings['interact'], self.module.binding('9'))

    def test_one_key_for_both_actions(self):
        self.app.same_key.set(True)
        self.app.cast_key.set('G')
        self.app.start()
        self.assertEqual(self.app.bindings['cast'], self.app.bindings['interact'])
        self.assertEqual(self.app.bindings['interact']['vk'], ord('G'))

    def test_unchecked_vision_allows_auto_with_warning(self):
        self.vision_setup(ready=False)
        self.app.auto.set(True)
        self.app.start()
        self.assertTrue(self.app.running and self.app.automating)
        self.assertTrue(any('проверка распознавания не пройдена' in e for e in self.app.events.get(0, 'end')))
        self.send.assert_not_called()

    def test_checked_model_allows_auto_start(self):
        self.vision_setup(ready=True)
        self.app.auto.set(True)
        self.app.start()
        self.assertTrue(self.app.running and self.app.automating)
        self.assertEqual(self.app.params.threshold, 6)

    def test_observation_records_session_and_crops_region(self):
        self.vision_setup()
        self.app.start()
        self.assertIsNotNone(self.app.recorder)
        directory = self.app.recorder.directory
        fishing = render([1, 1, 0, 10, 50, 0, 2, 0, 0, 12, 0])

        def grab(bounds):
            return __import__('fishing_synthetic').frame(0.1) if bounds[0] == 600 else fishing

        self.app.sample()  # idle frame
        self.app.capture.grab.side_effect = grab
        self.app.sample()  # cast 2 starts, observed
        bounds = [c.args[0] for c in self.app.capture.grab.call_args_list]
        self.assertIn((600, 300, 1020, 600), bounds)
        self.assertEqual(self.app.cast_started.get(2) is not None, True)
        self.app.stop('test')
        rows = [json.loads(l) for l in (directory / 'frames.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertTrue(any(r.get('file') for r in rows))
        self.assertTrue(all(r['session'] == directory.name for r in rows))
        self.send.assert_not_called()

    def test_hud_auto_located(self):
        with patch.object(self.module, 'locate_hud', return_value=dict(x=10, y=20, width=312, height=48)):
            self.app.find_hud()
        self.assertEqual(self.app.cfg['rect']['x'], 10)
        self.assertEqual(self.app.cfg['screen'], {'width': 1920, 'height': 1080})

    def test_label_window_opens_with_sessions(self):
        from ahelperw_fishing.training import CastLabelWindow

        window = CastLabelWindow(
            self.app, Path(self.temp.name) / 'fishing-sessions', self.app.params, self.app.on_report
        )
        window.withdraw()
        self.assertEqual(window.tree.get_children(), ())
        window.destroy()


if __name__ == '__main__':
    unittest.main()
