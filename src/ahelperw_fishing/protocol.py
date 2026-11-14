"""HUD pixel protocol of the AHelperWFishing addon: decoding, locating and export validation.

The addon paints 24 data columns x 4 rows of black/white cells between a magenta and a
cyan anchor column. Each row carries three bytes (MSB first); byte 12 is a weighted
checksum. Unknown, mixed or covered pixels never decode to a default idle/zero value.
"""

import math
from dataclasses import dataclass

import numpy as np

STATES = {0: 'idle', 1: 'fishing', 2: 'other_cast', 3: 'loot', 4: 'blocked', 5: 'unknown'}
ERROR_CODES = {1: 'other', 2: 'not_hooked', 3: 'escaped', 4: 'bags_full'}
ERROR_REASONS = {
    'other': 'Игра показала ошибку (красное сообщение). Проверь его в игре',
    'not_hooked': 'Рыба ещё не клюнула: нажатие было раньше поклёвки (ложный сигнал)',
    'escaped': 'Рыбка уплыла',
    'bags_full': 'Игра сообщила, что инвентарь заполнен',
    'latched': 'Ошибка игры (HUD v1). /ahwfish reset и запусти заново',
}
EXPORT_SCHEMA = 'AHelperWForever.Fishing'
MIN_WIDTH, MIN_HEIGHT = 156, 24
_COLUMNS, _ROWS = 26, 4
_MAGENTA, _CYAN = (255, 0, 255), (0, 255, 255)


@dataclass(frozen=True)
class Observation:
    captured_at: float
    state: str
    free: int | None
    progress: float
    cast: int
    loot: int
    heartbeat: int
    error: bool = False  # v1: latched UI error
    protocol: int = 1
    loot_cast: int | None = None  # v2: low byte of the cast whose catch was confirmed
    error_serial: int | None = None
    error_code: int = 0


@dataclass(frozen=True)
class BiteSignal:
    captured_at: float
    cast: int


def validate_export(cfg):
    if not isinstance(cfg, dict) or cfg.get('schema') != EXPORT_SCHEMA or cfg.get('version') != 1:
        raise ValueError('Нужен экспорт /ahwfish export версии 1')
    screen, rect = cfg.get('screen', {}), cfg.get('rect', {})
    for obj, keys in ((screen, ('width', 'height')), (rect, ('x', 'y', 'width', 'height'))):
        for key in keys:
            value = obj.get(key)
            if type(value) is not int or value < 0 or value > 32768:
                raise ValueError('Некорректные координаты HUD')
    if rect['width'] < MIN_WIDTH or rect['height'] < MIN_HEIGHT:
        raise ValueError('HUD слишком мал для чтения')
    if rect['x'] + rect['width'] > screen['width'] or rect['y'] + rect['height'] > screen['height']:
        raise ValueError('HUD за границей окна')
    mark = cfg.get('mark', 65)
    if type(mark) not in (float, int) or not math.isfinite(mark) or not 1 <= mark <= 95:
        raise ValueError('Отметка должна быть от 1 до 95%')
    return cfg


def auto_export(screen_size, rect, mark=65):
    return validate_export(
        {
            'schema': EXPORT_SCHEMA,
            'version': 1,
            'screen': {'width': screen_size[0], 'height': screen_size[1]},
            'rect': rect,
            'mark': mark,
            'source': 'auto',
        }
    )


def decode(image, captured_at):
    """Decode one HUD strip image into an Observation (raises ValueError if unreadable)."""
    if image.width < MIN_WIDTH or image.height < MIN_HEIGHT:
        raise ValueError('Маленький кадр HUD')
    image = image.convert('RGB')

    def sample(col, row):
        x, y = int((col + 0.5) * image.width / _COLUMNS), int((row + 0.5) * image.height / _ROWS)
        pixels = [image.getpixel((x + dx, y + dy)) for dx, dy in ((0, 0), (-1, 0), (1, 0), (0, -1), (0, 1))]
        if any(max(abs(p[k] - pixels[0][k]) for k in range(3)) > 25 for p in pixels):
            raise ValueError('Перекрытый или смещённый HUD')
        return pixels[0]

    bits = []
    for row in range(_ROWS):
        for col, color in ((0, _MAGENTA), (_COLUMNS - 1, _CYAN)):
            if max(abs(a - b) for a, b in zip(sample(col, row), color, strict=True)) > 40:
                raise ValueError('HUD не найден; проверь экспорт и видимость')
        for col in range(1, _COLUMNS - 1):
            rgb = sample(col, row)
            if min(rgb) >= 215:
                bits.append(1)
            elif max(rgb) <= 40:
                bits.append(0)
            else:
                raise ValueError('Нечитаемая ячейка HUD')
    data = [sum(bits[i + b] << (7 - b) for b in range(8)) for i in range(0, 96, 8)]
    if data[0] not in (1, 2) or sum((i + 1) * v for i, v in enumerate(data[:11])) % 256 != data[11]:
        raise ValueError('Неполный кадр или неизвестная версия HUD')
    if data[1] not in STATES:
        raise ValueError('Неизвестное состояние HUD')
    free = data[2] * 256 + data[3]
    free = None if free == 65535 else free
    if data[0] == 1:
        if data[10] not in (0, 1):
            raise ValueError('Неизвестное состояние HUD')
        return Observation(
            captured_at,
            STATES[data[1]],
            free,
            data[4] / 255,
            data[5] * 256 + data[6],
            data[7] * 256 + data[8],
            data[9],
            bool(data[10]),
        )
    code = data[10] & 15
    if code and code not in ERROR_CODES:
        raise ValueError('Неизвестный код ошибки HUD')
    return Observation(
        captured_at,
        STATES[data[1]],
        free,
        data[4] / 255,
        data[5] * 256 + data[6],
        data[7],
        data[9],
        False,
        2,
        data[8],
        data[10] >> 4,
        code,
    )


def _components(points):
    """4-connected components of a set of (x, y) pixels; returns bounding boxes with pixel counts."""
    boxes = []
    while points:
        seed = points.pop()
        todo, x0, y0, x1, y1, count = [seed], seed[0], seed[1], seed[0], seed[1], 0
        while todo:
            x, y = todo.pop()
            count += 1
            x0, y0, x1, y1 = min(x0, x), min(y0, y), max(x1, x), max(y1, y)
            for n in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if n in points:
                    points.remove(n)
                    todo.append(n)
        boxes.append((x0, y0, x1 + 1, y1 + 1, count))
    return boxes


def locate_hud(image):
    """Find the fishing strip in a client-area screenshot; returns a rect dict.

    The strip is a solid magenta column (4 stacked cells) and a cyan column of the same
    size 25 cells to the right. Every candidate must also decode with a valid checksum,
    so other magenta/cyan UI elements are not accepted.
    """
    a = np.asarray(image.convert('RGB'), dtype=np.int16)

    def mask(color):
        r, g, b = color
        m = (np.abs(a[:, :, 0] - r) <= 40) & (np.abs(a[:, :, 1] - g) <= 40) & (np.abs(a[:, :, 2] - b) <= 40)
        ys, xs = np.nonzero(m)
        return set(zip(xs.tolist(), ys.tolist(), strict=True))

    magenta, cyan = mask(_MAGENTA), mask(_CYAN)
    if not magenta or not cyan or len(magenta) > 400000:
        raise ValueError('HUD рыбалки не найден на экране: /ahwfish, HUD должен быть виден')
    cyan_boxes = [b for b in _components(cyan) if b[4] >= 16]
    found = []
    for x0, y0, x1, y1, count in _components(magenta):
        w, h = x1 - x0, y1 - y0
        if w < 4 or not 3.6 <= h / w <= 4.4 or count < w * h * 0.9:
            continue
        for cx0, cy0, cx1, cy1, _ in cyan_boxes:
            if (
                abs(cy0 - y0) <= 2
                and abs(cy1 - y1) <= 2
                and abs(cx1 - cx0 - w) <= 2
                and abs(cx0 - (x0 + 25 * (cx1 - x0) / 26)) <= 3
            ):
                rect = {'x': int(x0), 'y': int(y0), 'width': int(cx1 - x0), 'height': int(y1 - y0)}
                try:
                    decode(image.crop((rect['x'], rect['y'], rect['x'] + rect['width'], rect['y'] + rect['height'])), 0)
                except ValueError:
                    continue
                found.append(rect)
    if len(found) != 1:
        raise ValueError('HUD рыбалки не найден на экране' if not found else 'Найдено несколько HUD рыбалки')
    return found[0]
