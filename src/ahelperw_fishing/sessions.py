"""Recorded fishing sessions, per-cast user labels and grouped evaluation.

Layout: <root>/<session_id>/session.json, frames.jsonl, frames/*.jpg, labels.json.
Every frame row keeps the session id, the HUD cast serial and the time since that
cast started, so evaluation can hold out whole casts or whole sessions: adjacent
near-identical frames of one bite never appear on both sides of a split.

Labels are only what the user set in the labelling window:
  {"bite_at": <seconds since cast start>, "status": "bite"} - first bite frame,
  {"status": "no_bite"} - nothing bit while frames were recorded,
  {"status": "unsure"}  - excluded from training and evaluation.
Detector output is stored separately ("signal" in frames.jsonl) and is never a label.
"""

import hashlib
import json
import math
import queue
import shutil
import statistics
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from . import vision as detector
from .vision import CastVision, VisionParams, decide

HIT_BEFORE = 0.15  # s: a signal slightly before the labelled frame is still the bite
HIT_AFTER = 1.5  # s: later signals count as missed (fish may escape)
THRESHOLDS = (4, 4.5, 5, 5.5, 6, 6.5, 7, 7.5, 8, 9, 10, 11, 12, 14, 17, 20, 25)


def new_session_id():
    return time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6]


class SessionRecorder:
    """Appends HUD rows and region frames; JPEG encoding runs on a writer thread
    so a slow disk never delays the capture/decision loop."""

    def __init__(self, root, meta, fps=10, max_bytes=6_000_000_000):
        self.id = new_session_id()
        self.directory = Path(root) / self.id
        (self.directory / 'frames').mkdir(parents=True, exist_ok=True)
        self.meta = dict(meta, session=self.id, version=1, started=time.time())
        (self.directory / 'session.json').write_text(
            json.dumps(self.meta, ensure_ascii=False, indent=1), encoding='utf-8'
        )
        self.rows = (self.directory / 'frames.jsonl').open('a', encoding='utf-8')
        self.interval = 1 / fps
        self.last_saved = -1e9
        self.bytes = 0
        self.max_bytes = max_bytes
        self.frames = 0
        self.index = 0
        self.dropped = 0
        self.queue = queue.Queue(maxsize=60)
        self.writer = threading.Thread(target=self._write, daemon=True)
        self.writer.start()

    def _write(self):
        while True:
            item = self.queue.get()
            if item is None:
                return
            image, path = item
            try:
                image.save(path, quality=90)
                self.bytes += path.stat().st_size
            except OSError:
                self.dropped += 1

    def add(self, row, image=None):
        row = dict(row, i=self.index, session=self.id)
        self.index += 1
        if image is not None and row['at'] - self.last_saved >= self.interval * 0.9 and self.bytes < self.max_bytes:
            name = f'c{row["cast"]:05d}_{row["i"]:07d}.jpg'
            try:
                self.queue.put_nowait((image, self.directory / 'frames' / name))
                self.frames += 1
                self.last_saved = row['at']
                row['file'] = name
            except queue.Full:
                self.dropped += 1
        self.rows.write(json.dumps(row, ensure_ascii=False) + chr(10))
        if self.index % 50 == 0:
            self.rows.flush()  # an interrupted run keeps its rows
        return row

    def flush(self):
        self.rows.flush()

    def close(self):
        if not self.rows.closed:
            self.queue.put(None)
            self.writer.join(timeout=10)
            self.rows.close()
            if self.frames == 0:
                # Nothing was fished while recording: leave no empty session behind.
                shutil.rmtree(self.directory, ignore_errors=True)


@dataclass
class CastRecord:
    session: str
    cast: int
    started_at: float
    frames: list = field(default_factory=list)  # rows with 'file'
    ended_at: float | None = None
    catch: bool | None = None  # addon confirmed a catch for this cast
    signal_at: float | None = None  # live detector signal (diagnostics only)
    errors: list = field(default_factory=list)
    v2: bool = False  # HUD protocol v2 rows seen
    v1_catch: bool = False  # v1 counter rose after this cast (not bound to it)

    @property
    def key(self):
        return f'{self.session}/{self.cast}'


class Session:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.id = self.directory.name
        self.meta = json.loads((self.directory / 'session.json').read_text(encoding='utf-8'))
        self.labels_path = self.directory / 'labels.json'
        self.labels = {}
        if self.labels_path.exists():
            doc = json.loads(self.labels_path.read_text(encoding='utf-8'))
            if doc.get('version') != 1:
                raise ValueError('Неизвестный формат меток')
            self.labels = {int(k): v for k, v in doc['casts'].items()}
        self.casts = self._casts()

    def _casts(self):
        casts, current, last = {}, None, None
        path = self.directory / 'frames.jsonl'
        if not path.exists():
            return casts
        for line in path.read_text(encoding='utf-8').splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue  # last line of an interrupted session
            state, cast = row.get('state'), row.get('cast')
            if state == 'fishing' and (current is None or current.cast != cast):
                if current is not None and current.ended_at is None:
                    current.ended_at = row['at']
                if 'cast_at' in row and row['cast_at'] is None:
                    current = None  # start of this cast was not observed: not usable
                    last = row
                    continue
                current = casts.setdefault(cast, CastRecord(self.id, cast, row.get('cast_at', row['at'])))
            if current is not None:
                if row.get('file') and cast == current.cast and state in ('fishing', 'loot'):
                    current.frames.append(row)
                if row.get('signal') and current.signal_at is None and cast == current.cast:
                    current.signal_at = row['at']
                if last is not None and row.get('loot') != last.get('loot'):
                    if row.get('loot_cast') is not None:  # HUD v2: catch names its cast
                        if row['loot_cast'] == current.cast % 256:
                            current.catch = True
                    elif current.ended_at is not None:  # HUD v1: counter rose after this cast
                        current.v1_catch = True
                if row.get('protocol') == 2 or row.get('loot_cast') is not None:
                    current.v2 = True
                if (
                    row.get('error_serial') is not None
                    and last is not None
                    and row['error_serial'] != last.get('error_serial')
                ):
                    current.errors.append(row.get('error_code'))
                if state != 'fishing' and cast == current.cast and current.ended_at is None:
                    current.ended_at = row['at']
            last = row
        for record in casts.values():
            # HUD v1 counted catches without naming the cast: stays unknown (None).
            if record.catch is None and record.ended_at is not None and record.v2:
                record.catch = False
        return {k: v for k, v in casts.items() if v.frames}

    def label(self, cast, status, bite_at=None):
        if status not in ('bite', 'no_bite', 'unsure', None):
            raise ValueError('Неизвестная метка')
        if status is None:
            self.labels.pop(cast, None)
        else:
            if status == 'bite' and (bite_at is None or not math.isfinite(bite_at)):
                raise ValueError('Для поклёвки выбери первый кадр всплеска')
            self.labels[cast] = {
                'status': status,
                'bite_at': bite_at if status == 'bite' else None,
                'by': 'user',
                'at': time.time(),
            }
        tmp = self.labels_path.with_suffix('.tmp')
        tmp.write_text(
            json.dumps(
                {'version': 1, 'casts': {str(k): v for k, v in self.labels.items()}}, ensure_ascii=False, indent=1
            ),
            encoding='utf-8',
        )
        tmp.replace(self.labels_path)

    def image(self, row):
        with Image.open(self.directory / 'frames' / row['file']) as image:
            crop = self.meta.get('crop')
            return (image.crop(tuple(crop)) if crop else image).convert('RGB')


class SessionStore:
    def __init__(self, root):
        self.root = Path(root)

    def sessions(self):
        if not self.root.exists():
            return []
        out = []
        for d in sorted(self.root.iterdir(), reverse=True):
            if (d / 'session.json').exists():
                try:
                    out.append(Session(d))
                except (OSError, ValueError, KeyError):
                    continue
        return out

    def delete(self, session_id):
        target = self.root / session_id
        if target.parent != self.root or not (target / 'session.json').exists():
            raise ValueError('Нет такой записи')
        shutil.rmtree(target)


# -- replay & evaluation ------------------------------------------------------


class TraceCache(dict):
    """Per-cast score traces persisted next to each session, keyed by the detector
    source and parameters, so repeated checks do not replay unchanged casts."""

    def __init__(self, sessions, params):
        super().__init__()
        source = Path(detector.__file__).with_suffix('.py')
        # A frozen build has no .py sources: the detector version string keys the cache.
        code = source.read_bytes() if source.exists() else detector.DETECTOR_VERSION.encode()
        trace_params = VisionParams.from_dict(dict(params.to_dict(), threshold=1e9))
        self.tag = hashlib.sha256(code + json.dumps(trace_params.to_dict(), sort_keys=True).encode()).hexdigest()[:12]
        self.files = {}
        for s in sessions:
            path = s.directory / f'traces-{self.tag}.json'
            self.files[s.id] = path
            if path.exists():
                try:
                    for key, trace in json.loads(path.read_text(encoding='utf-8')).items():
                        super().__setitem__(key, [(t, _Stored(**r)) for t, r in trace])
                except (OSError, ValueError, TypeError):
                    pass

    @staticmethod
    def _key(key):
        return key[0] if isinstance(key, tuple) else key

    def __contains__(self, key):
        return super().__contains__(self._key(key))

    def __getitem__(self, key):
        return super().__getitem__(self._key(key))

    def __setitem__(self, key, value):
        super().__setitem__(self._key(key), value)

    def save(self):
        by_session = {}
        for key, trace in self.items():
            by_session.setdefault(key.split('/')[0], {})[key] = [
                (t, {'phase': r.phase, 'score': r.score, 'features': r.features}) for t, r in trace
            ]
        for sid, data in by_session.items():
            if sid in self.files:
                try:
                    self.files[sid].write_text(json.dumps(data), encoding='utf-8')
                except OSError:
                    pass


class _Stored:
    def __init__(self, phase, score, features):
        self.phase, self.score, self.features = phase, score, features


def session_params(session, params):
    """Search scale follows the recorded screen resolution (see VisionParams.pixel_scale)."""
    screen = session.meta.get('screen') or {}
    if params.pixel_scale or not screen.get('height'):
        return params
    return VisionParams.from_dict(dict(params.to_dict(), pixel_scale=max(1, round(screen['height'] / 1080))))


def replay(session, record, params, cache=None):
    """Run the detector over a stored cast; returns per-frame results."""
    params = session_params(session, params)
    key = (record.key, tuple(sorted(params.to_dict().items())))
    if cache is not None and key in cache:
        return cache[key]
    vision = CastVision(record.cast, record.started_at, params)
    out = []
    for row in record.frames:
        result = vision.observe(session.image(row), row['at'], row['at'])
        out.append((row['at'] - record.started_at, result))
    if cache is not None:
        cache[key] = out
    return out


def trace_of(results):
    """(t, score, phase, located, intact) per frame from detector results."""
    return [
        (
            t,
            r.score,
            r.phase,
            bool(r.features.get('located', r.features.get('near', 1.0) >= 0.35)),
            bool(r.features.get('intact', False)),
        )
        for t, r in results
    ]


def scores(session, record, params, cache=None):
    """Threshold-free trace of a stored cast."""
    p = VisionParams.from_dict(dict(params.to_dict(), threshold=1e9))
    return trace_of(replay(session, record, p, cache))


def first_signal(trace, threshold, params=None):
    """Same decision as CastVision.observe for a given threshold, from a score trace:
    each armed frame is paired with the armed frame closest to pair_dt earlier."""
    p = VisionParams.from_dict(dict((params or VisionParams()).to_dict(), threshold=threshold))
    recent, last_t = [], None
    for t, score, phase, located, intact in trace:
        if last_t is not None and t - last_t > p.max_gap:
            recent = []
        last_t = t
        if phase != 'armed' or score is None:
            recent = []
            continue
        prev = None
        for entry in recent:
            dt = t - entry[2]
            if 0.6 * p.pair_dt <= dt <= p.max_gap and (
                prev is None or abs(dt - p.pair_dt) < abs(t - prev[2] - p.pair_dt)
            ):
                prev = entry
        if prev is not None and prev[1] and located and decide(prev[0], score, p, prev[3], intact):
            return t
        recent = [e for e in recent if t - e[2] <= p.max_gap] + [(score, located, t, intact)]
    return None


def outcome(label, signal):
    if label['status'] == 'no_bite':
        return 'false' if signal is not None else 'quiet'
    onset = label['bite_at']
    if signal is None:
        return 'miss'
    if signal < onset - HIT_BEFORE:
        return 'false'
    if signal > onset + HIT_AFTER:
        return 'late'
    return 'hit'


def labelled(sessions):
    items = []
    for s in sessions:
        for cast, label in s.labels.items():
            if label.get('status') in ('bite', 'no_bite') and cast in s.casts:
                items.append((s, s.casts[cast], label))
    return items


def choose_threshold(traces):
    """Fewest false signals first, then most found bites; among the thresholds
    that tie, take the middle of that range - the largest margin both to false
    signals (lower thresholds) and to missed weak bites (higher thresholds)."""
    results = []
    for threshold in THRESHOLDS:
        res = [outcome(label, first_signal(trace, threshold)) for trace, label in traces]
        results.append(((-res.count('false'), res.count('hit')), threshold))
    if not results:
        return VisionParams().threshold
    best = max(key for key, _ in results)
    feasible = [threshold for key, threshold in results if key == best]
    return feasible[len(feasible) // 2]


def evaluate(sessions, params=None, cache=None):
    """Grouped cross-validation. Unit of hold-out: session if >=2 sessions have
    labelled bites, otherwise single cast. Returns a report dict."""
    params = params or VisionParams()
    items = labelled(sessions)
    traces = [(scores(s, r, params, cache), label, s.id, r) for s, r, label in items]
    bite_sessions = {sid for _, label, sid, _ in traces if label['status'] == 'bite'}
    by_session = len(bite_sessions) >= 2
    group = (lambda item: item[2]) if by_session else (lambda item: item[3].key)
    rows = []
    for g in sorted({group(item) for item in traces}):
        train = [(tr, lb) for tr, lb, sid, r in traces if group((tr, lb, sid, r)) != g]
        test = [(tr, lb, sid, r) for tr, lb, sid, r in traces if group((tr, lb, sid, r)) == g]
        threshold = choose_threshold(train) if train else params.threshold
        for tr, lb, _sid, r in test:
            signal = first_signal(tr, threshold)
            phases = {item[2] for item in tr}
            rows.append(
                {
                    'cast': r.key,
                    'label': lb['status'],
                    'bite_at': lb.get('bite_at'),
                    'signal': signal,
                    'threshold': threshold,
                    'result': outcome(lb, signal),
                    'undecided': not ('armed' in phases or 'fired' in phases),
                    'latency': None if signal is None or lb['status'] != 'bite' else signal - lb['bite_at'],
                }
            )
    final = choose_threshold([(tr, lb) for tr, lb, _, _ in traces]) if traces else params.threshold
    bites = [r for r in rows if r['label'] == 'bite']
    latency = [r['latency'] for r in bites if r['result'] == 'hit']
    report = {
        'split': 'по сессиям' if by_session else 'по забросам',
        'casts': len(rows),
        'bite_casts': len(bites),
        'no_bite_casts': len(rows) - len(bites),
        'hits': sum(r['result'] == 'hit' for r in rows),
        'missed': sum(r['result'] in ('miss', 'late') for r in rows),
        'missed_undecided': sum(r['result'] == 'miss' and r['undecided'] for r in rows),
        'late': sum(r['result'] == 'late' for r in rows),
        'false_signals': sum(r['result'] == 'false' for r in rows),
        'latency_median': statistics.median(latency) if latency else None,
        'threshold': final,
        'rows': rows,
        'sessions': len({r['cast'].split('/')[0] for r in rows}),
    }
    report['labels_by'] = {}
    report['user_bites'] = 0
    for _, _, label in items:
        by = label.get('by', 'user')
        report['labels_by'][by] = report['labels_by'].get(by, 0) + 1
        report['user_bites'] += by == 'user' and label['status'] == 'bite'
    report['ready'] = ready(report)
    return report


def ready(report):
    """Gate for automatic presses: enough held-out evidence and no false signal."""
    if report['bite_casts'] < 8:
        return False, f'Мало размеченных поклёвок: {report["bite_casts"]} из 8'
    if report['false_signals']:
        return False, f'Ложных сигналов на отложенных забросах: {report["false_signals"]}'
    if report['hits'] < math.ceil(report['bite_casts'] * 0.9):
        return False, f'Найдено поклёвок {report["hits"]} из {report["bite_casts"]} — меньше 90%'
    if report.get('user_bites', report['bite_casts']) < 8:
        # Labels proposed by someone else (e.g. the assistant) are not ground truth yet.
        return False, (
            f'Подтверждено тобой поклёвок {report.get("user_bites", 0)} из 8: открой заброс, проверь кадр и нажми B'
        )
    return True, 'Проверка на отложенных забросах пройдена; проверь в игре в режиме наблюдения'


def summary(report):
    lines = [
        f'Проверка {report["split"]}: забросов {report["casts"]} (с поклёвкой {report["bite_casts"]}, '
        f'без поклёвки {report["no_bite_casts"]}).',
        f'Найдено {report["hits"]}, пропущено {report["missed"]}'
        + (
            f' (из них поплавок не определён: {report["missed_undecided"]}, поздно: {report["late"]})'
            if report['missed']
            else ''
        )
        + f', ложных сигналов {report["false_signals"]}.',
    ]
    if report['latency_median'] is not None:
        lines.append(f'Медианная задержка сигнала после первого кадра поклёвки: {report["latency_median"]:.2f} с.')
    by = report.get('labels_by', {})
    if any(k != 'user' for k in by):
        lines.append(
            f'Метки: пользователя {by.get("user", 0)}, '
            f'другие {sum(v for k, v in by.items() if k != "user")} — проверь их в списке.'
        )
    lines.append(f'Порог: {report["threshold"]}. {report["ready"][1]}')
    return '\n'.join(lines)
