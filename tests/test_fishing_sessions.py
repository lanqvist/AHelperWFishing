"""Session recording, per-cast labels and grouped hold-out evaluation."""

import tempfile
import time
import unittest
from pathlib import Path

from ahelperw_fishing.sessions import (
    Session,
    SessionRecorder,
    SessionStore,
    evaluate,
    first_signal,
    outcome,
    ready,
    replay,
    scores,
)
from ahelperw_fishing.vision import VisionParams
from fishing_synthetic import frame, times


def record(root, casts, t0=100.0):
    """casts: list of dicts(cast=, bite=, ripples=, observed=True). Returns session id."""
    rec = SessionRecorder(root, {'region': {'x': 0, 'y': 0, 'width': 420, 'height': 300}}, fps=10)
    t, loot = t0, 4
    for spec in casts:
        start = t
        rec.add(
            {
                'at': t - 0.5,
                'state': 'idle',
                'cast': spec['cast'] - 1,
                'loot': loot,
                'loot_cast': 0,
                'error_serial': 1,
                'error_code': 0,
                'cast_at': None,
            }
        )
        until = spec.get('until', (spec['bite'] + 1.0) if spec.get('bite') else 12)
        for dt in times(until):
            row = {
                'at': start + dt,
                'state': 'fishing',
                'cast': spec['cast'],
                'loot': loot,
                'loot_cast': 0,
                'error_serial': 1,
                'error_code': 0,
                'cast_at': start if spec.get('observed', True) else None,
            }
            rec.add(row, frame(dt, bite=spec.get('bite'), ripples=spec.get('ripples', ())))
        t = start + until + 0.1
        if spec.get('bite'):
            loot += 1
        rec.add(
            {
                'at': t,
                'state': 'idle',
                'cast': spec['cast'],
                'loot': loot,
                'loot_cast': spec['cast'] % 256 if spec.get('bite') else 0,
                'error_serial': 1,
                'error_code': 0,
                'cast_at': None,
            }
        )
        t += 1.5
    rec.close()
    return rec.id


class SessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.a = record(
            cls.root,
            [
                dict(cast=10, bite=7.0),
                dict(cast=11, bite=9.5),
                dict(cast=12, ripples=(5, 8)),
                dict(cast=13, bite=6.0, observed=False),
            ],
        )
        time.sleep(1.1)  # distinct session ids (second resolution)
        cls.b = record(cls.root, [dict(cast=20, bite=8.0), dict(cast=21, bite=6.5)], t0=500.0)
        cls.cache = {}

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def session(self, sid):
        return next(s for s in SessionStore(self.root).sessions() if s.id == sid)

    def test_casts_frames_and_catch_from_hud_rows(self):
        s = self.session(self.a)
        self.assertEqual(sorted(s.casts), [10, 11, 12])  # 13: start not observed -> unusable
        self.assertTrue(s.casts[10].catch)
        self.assertFalse(s.casts[12].catch)
        self.assertGreater(len(s.casts[10].frames), 50)
        self.assertTrue(all(r['session'] == self.a for r in s.casts[10].frames))

    def test_labels_persist_and_validate(self):
        s = self.session(self.a)
        with self.assertRaises(ValueError):
            s.label(10, 'bite', None)
        with self.assertRaises(ValueError):
            s.label(10, 'maybe')
        s.label(12, 'unsure')
        self.assertEqual(Session(s.directory).labels[12]['status'], 'unsure')
        self.assertEqual(Session(s.directory).labels[12]['by'], 'user')
        s.label(12, None)
        self.assertNotIn(12, Session(s.directory).labels)

    def test_offline_decision_equals_live_detector(self):
        s = self.session(self.a)
        record_ = s.casts[11]
        params = VisionParams(threshold=6)
        live = [t for t, r in replay(s, record_, params) if r.fire]
        offline = first_signal(scores(s, record_, params, self.cache), 6)
        self.assertEqual(len(live), 1)
        self.assertAlmostEqual(live[0], offline, places=6)

    def test_grouped_evaluation_by_session(self):
        a, b = self.session(self.a), self.session(self.b)
        a.label(10, 'bite', 7.0)
        a.label(11, 'bite', 9.5)
        a.label(12, 'no_bite')
        b.label(20, 'bite', 8.0)
        b.label(21, 'unsure')
        report = evaluate([a, b], VisionParams(), self.cache)
        self.assertEqual(report['split'], 'по сессиям')
        self.assertEqual((report['casts'], report['bite_casts'], report['no_bite_casts']), (4, 3, 1))
        self.assertEqual(report['hits'], 3, report['rows'])
        self.assertEqual(report['false_signals'], 0)
        self.assertFalse(report['ready'][0])  # fewer than 8 labelled bites
        self.assertTrue({r['cast'].split('/')[0] for r in report['rows']} == {self.a, self.b})
        for s in (a, b):
            for cast in list(s.labels):
                s.label(cast, None)

    def test_outcome_rules(self):
        bite = {'status': 'bite', 'bite_at': 10.0}
        self.assertEqual(outcome(bite, 9.9), 'hit')
        self.assertEqual(outcome(bite, 9.5), 'false')
        self.assertEqual(outcome(bite, 11.6), 'late')
        self.assertEqual(outcome(bite, None), 'miss')
        self.assertEqual(outcome({'status': 'no_bite'}, 3.0), 'false')
        self.assertEqual(outcome({'status': 'no_bite'}, None), 'quiet')
        self.assertFalse(ready(dict(bite_casts=9, false_signals=1, hits=9))[0])
        self.assertTrue(ready(dict(bite_casts=10, false_signals=0, hits=9))[0])
        self.assertFalse(ready(dict(bite_casts=10, false_signals=0, hits=8))[0])
        self.assertFalse(ready(dict(bite_casts=10, false_signals=0, hits=10, user_bites=3))[0])
        self.assertTrue(ready(dict(bite_casts=10, false_signals=0, hits=10, user_bites=8))[0])

    def test_delete_session_only_inside_root(self):
        store = SessionStore(self.root)
        with self.assertRaises(ValueError):
            store.delete('..')
        with self.assertRaises(ValueError):
            store.delete('missing')


if __name__ == '__main__':
    unittest.main()
