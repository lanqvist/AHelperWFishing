"""User settings (fishing.json) and the checked detector model (fishing-model.json).

Keys are compatible with earlier versions. Every value is validated on load; an invalid
value falls back to its default and is reported, it never prevents the app from starting.
"""

import json
from dataclasses import asdict, dataclass, field, fields

from .keys import binding
from .paths import model_file, settings_file, write_atomic
from .protocol import validate_export

FPS_CHOICES = (10, 20, 30)
MODES = ('vision', 'mark')


@dataclass
class Settings:
    export: dict | None = None
    region: dict | None = None
    mark: float = 65.0
    reserve: int = 0
    cast: str = '1'
    interact: str = 'F'
    mode: str = 'vision'
    same_key: bool = True
    auto_recast: bool = False
    background: bool = False
    fps: int = 30
    check_updates: bool = True
    problems: list = field(default_factory=list, repr=False, compare=False)

    def to_dict(self):
        data = asdict(self)
        data.pop('problems')
        return data


def _validators():
    def number(lo, hi, kind):
        def check(v):
            if type(v) not in (int, float) or not lo <= v <= hi:
                raise ValueError
            return kind(v)

        return check

    def boolean(v):
        if not isinstance(v, bool):
            raise ValueError
        return v

    def key(v):
        binding(v)
        return v

    def choice(options):
        def check(v):
            if v not in options:
                raise ValueError
            return v

        return check

    def export(v):
        return validate_export(v) if v is not None else None

    def region(v):
        if v is not None and not isinstance(v, dict):
            raise ValueError
        return v

    return {
        'export': export,
        'region': region,
        'mark': number(1, 95, float),
        'reserve': number(0, 100, int),
        'cast': key,
        'interact': key,
        'mode': choice(MODES),
        'same_key': boolean,
        'auto_recast': boolean,
        'background': boolean,
        'fps': lambda v: choice(FPS_CHOICES)(int(v)),  # older versions stored a string
        'check_updates': boolean,
    }


_VALIDATE = _validators()


def from_dict(doc):
    settings = Settings()
    if not isinstance(doc, dict):
        settings.problems.append('файл настроек повреждён')
        return settings
    for f in fields(Settings):
        if f.name == 'problems' or f.name not in doc:
            continue
        try:
            setattr(settings, f.name, _VALIDATE[f.name](doc[f.name]))
        except (ValueError, TypeError, KeyError):
            settings.problems.append(f.name)
    return settings


def load():
    path = settings_file()
    try:
        doc = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return Settings()
    except (OSError, ValueError) as exc:
        settings = Settings()
        settings.problems.append(f'файл не прочитан ({exc})')
        return settings
    return from_dict(doc)


def save(settings):
    write_atomic(settings_file(), json.dumps(settings.to_dict(), ensure_ascii=False, indent=2))


def load_model():
    """The checked detector model, or None if there is none yet. Raises ValueError if unusable."""
    try:
        model = json.loads(model_file().read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    if not isinstance(model, dict) or model.get('version') != 1:
        raise ValueError('Неизвестная версия модели')
    return model


def save_model(model):
    write_atomic(model_file(), json.dumps(model, ensure_ascii=False, indent=1, default=str))
