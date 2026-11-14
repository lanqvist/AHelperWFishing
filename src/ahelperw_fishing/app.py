"""Main window. Start explicitly; F8 always stops. Reads screen pixels only."""

import argparse
import json
import os
import statistics
import sys
import threading
import time
import tkinter as tk
import webbrowser
from collections import deque
from itertools import pairwise
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import ImageTk

from . import APP_NAME, __version__, addon, logs, settings, updates
from .capture import HudCapture
from .controller import FishingController
from .keys import KEYS, STOP_KEY, binding
from .paths import data_dir, sessions_dir
from .protocol import ERROR_CODES, ERROR_REASONS, BiteSignal, auto_export, decode, locate_hud, validate_export
from .sessions import SessionRecorder, summary
from .training import PHASE_NAMES, CastLabelWindow, RegionSelector, overlay
from .vision import VisionParams, VisionSession, validate_region
from .win32 import (
    SingleInstance,
    enable_dpi_awareness,
    foreground_window,
    game_window,
    message_box,
    set_dark_title_bar,
    system_uses_dark_theme,
    visible,
)
from .winput import post_pulse, pulse, stop_pressed

log = logs.log
MODES = {'По изображению поплавка': 'vision', 'По отметке шкалы': 'mark'}
MODE_LABELS = {v: k for k, v in MODES.items()}
FPS_CHOICES = tuple(str(f) for f in settings.FPS_CHOICES)
ACQUIRE_SECONDS = 20  # time to switch to the game after "Начать"


def apply_theme(root):
    """Windows 11 look (Sun Valley ttk theme) following the system light/dark mode."""
    dark = system_uses_dark_theme()
    try:
        import sv_ttk

        sv_ttk.set_theme('dark' if dark else 'light', root)
    except Exception:  # theme package missing or Tk too old: plain ttk still works
        log.warning('sv_ttk theme unavailable', exc_info=True)
        return False
    set_dark_title_bar(root, dark)
    return dark


class CastLog:
    """Per-cast outcome of a run, from HUD facts only (cast serial, catch bound to cast)."""

    def __init__(self):
        self.casts = {}
        self.order = []

    def start(self, cast, at, observed_start):
        if cast not in self.casts:
            self.casts[cast] = {
                'cast': cast,
                'start': at,
                'observed_start': observed_start,
                'signal': None,
                'end': None,
                'catch': False,
                'sent': None,
                'errors': [],
                'phase': None,
            }
            self.order.append(cast)
        return self.casts[cast]

    def summary(self):
        done = [c for c in self.casts.values() if c['end'] is not None]
        catches = sum(c['catch'] for c in done)
        signals = sum(c['signal'] is not None for c in done)
        ahead = sum(c['signal'] is not None and c['catch'] and c['signal'] <= c['end'] for c in done)
        return (
            f'Забросов завершено {len(done)} · с сигналом {signals} · улов подтверждён {catches} · '
            f'сигнал до конца заброса с уловом {ahead}'
        )


class FishingApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f'{APP_NAME} {__version__}')
        icon = Path(__file__).with_name('icon.ico')
        if icon.exists():
            self.iconbitmap(default=str(icon))
        self.dark = apply_theme(self)
        self.report_callback_exception = self.on_callback_error
        scale = self.winfo_fpixels('1i') / 96
        self.geometry(
            f'{int(min(self.winfo_screenwidth() * 0.9, 1220 * scale))}'
            f'x{int(min(self.winfo_screenheight() * 0.9, 980 * scale))}'
        )
        self.minsize(int(900 * scale), int(700 * scale))
        self.cfg = None
        self.running = False
        self.automating = False
        self.checked = False
        self.halt = threading.Event()
        self.capture = HudCapture('gdi')
        self.controller = None
        self.bindings = {}
        self.session_window = None
        self.label_window = None
        self.photo = self.vision_photo = None
        self.region = None
        self.params = VisionParams()
        self.model = None
        self.vision = VisionSession(self.params)
        self.recorder = None
        self.selection_pending = False
        self.prev_obs = None
        self.cast_started = {}
        self.cast_log = CastLog()
        self.scores = deque(maxlen=150)
        self.timing = {name: deque(maxlen=200) for name in ('grab', 'vision', 'loop')}
        self.timing['press'] = deque(maxlen=50)
        self.last_ui = 0
        self.last_relocate = 0
        self.started_at = 0
        self.acquire_deadline = 0
        self.tick_times = deque(maxlen=90)
        self.update_url = None
        self.auto = tk.BooleanVar(value=False)
        self.record = tk.BooleanVar(value=True)
        self.background = tk.BooleanVar(value=False)
        self.check_updates = tk.BooleanVar(value=True)
        self.fps = tk.StringVar(value='30')
        self.same_key = tk.BooleanVar(value=True)
        self.recasts = tk.BooleanVar(value=False)
        self.mode = tk.StringVar(value=MODE_LABELS['vision'])
        self.hud_status = tk.StringVar(value='HUD не найден')
        self.region_status = tk.StringVar(value='Зона не выбрана')
        self.model_status = tk.StringVar(value='Распознавание не проверено')
        self.addon_status = tk.StringVar(value='')
        self.update_text = tk.StringVar(value='')
        self.mark = tk.StringVar(value='65')
        self.reserve = tk.StringVar(value='0')
        self.cast_key = tk.StringVar(value='1')
        self.interact_key = tk.StringVar(value='F')
        self.status = tk.StringVar(value='Остановлено')
        self.observed = tk.StringVar(value='HUD ещё не считан')
        self.vision_status = tk.StringVar(value='')
        self.stats_text = tk.StringVar(value='')
        self.timing_text = tk.StringVar(value='')
        self.build()
        self.load()
        self.sync_key_controls()
        self.refresh_addon_status()
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.bind_all(f'<{STOP_KEY}>', lambda _e: self.stop('Остановлено клавишей F8'))
        self.after(50, self.tick)
        if self.check_updates.get() and '--smoke-test' not in sys.argv:
            updates.check_async(lambda tag, url: self.after(0, self.show_update, tag, url))

    # -- layout ----------------------------------------------------------------
    def build(self):
        outer = ttk.Frame(self, padding=14)
        outer.pack(fill='both', expand=True)
        head = ttk.Frame(outer)
        head.pack(fill='x')
        ttk.Label(head, text='Рыбалка до заполнения сумок', font=('Segoe UI Variable Display', 18, 'bold')).pack(
            side='left'
        )
        self.update_link = ttk.Label(head, textvariable=self.update_text, foreground='#2f8cff', cursor='hand2')
        self.update_link.pack(side='right')
        self.update_link.bind('<Button-1>', lambda _e: self.update_url and webbrowser.open(self.update_url))
        ttk.Label(
            outer,
            wraplength=1160,
            justify='left',
            text=(
                '1. В игре /ahwfish (HUD рыбалки), включи автосбор добычи.  2. «Найти HUD» и «Выделить зону».  '
                '3. Наблюдение без автонажатий: рыбачь вручную, кадры записываются.  4. «Разметить и проверить»: '
                'отметь первый кадр поклёвки в каждом забросе и запусти проверку.  5. Снова наблюдение на новых '
                'забросах.  6. Только после этого — автонажатия. F8 — стоп.'
            ),
        ).pack(anchor='w', pady=(6, 8))
        top = ttk.Frame(outer)
        top.pack(fill='x')
        top.columnconfigure(0, weight=3)
        top.columnconfigure(1, weight=2)
        prep = ttk.LabelFrame(top, text='Подготовка', padding=8)
        prep.grid(row=0, column=0, sticky='nsew')
        self.hud_button = ttk.Button(prep, text='Найти HUD через 3 с', command=lambda: self.delayed(self.find_hud))
        self.hud_button.grid(row=0, column=0, sticky='w')
        ttk.Label(prep, textvariable=self.hud_status).grid(row=0, column=1, sticky='w', padx=8)
        self.paste_button = ttk.Button(prep, text='Вставить экспорт', command=self.paste)
        self.paste_button.grid(row=0, column=2, sticky='e')
        self.select_button = ttk.Button(
            prep, text='Выделить зону через 3 с', command=lambda: self.delayed(self.select_region)
        )
        self.select_button.grid(row=1, column=0, sticky='w', pady=4)
        ttk.Label(prep, textvariable=self.region_status).grid(row=1, column=1, columnspan=2, sticky='w', padx=8)
        self.controls = []
        for row, (label, variable) in enumerate(
            (('Клавиша заброса', self.cast_key), ('Клавиша взаимодействия', self.interact_key)), 2
        ):
            ttk.Label(prep, text=label).grid(row=row, column=0, sticky='w', pady=2)
            widget = ttk.Combobox(prep, values=KEYS, textvariable=variable, state='readonly', width=10, height=24)
            widget.grid(row=row, column=1, sticky='w', padx=8)
            self.controls.append(widget)
        self.same_check = ttk.Checkbutton(
            prep,
            text='Одна клавиша для заброса и взаимодействия',
            variable=self.same_key,
            command=self.sync_key_controls,
        )
        self.same_check.grid(row=2, column=2, sticky='w')
        self.recast_check = ttk.Checkbutton(
            prep, text='Эта кнопка сама перезабрасывает после улова', variable=self.recasts
        )
        self.recast_check.grid(row=3, column=2, sticky='w')
        ttk.Label(prep, text='Оставить свободных ячеек').grid(row=4, column=0, sticky='w')
        reserve = ttk.Spinbox(prep, from_=0, to=100, textvariable=self.reserve, width=8)
        reserve.grid(row=4, column=1, sticky='w', padx=8)
        self.controls.append(reserve)
        ttk.Label(prep, text='Режим').grid(row=5, column=0, sticky='w')
        self.mode_choice = ttk.Combobox(prep, values=list(MODES), textvariable=self.mode, state='readonly', width=26)
        self.mode_choice.grid(row=5, column=1, columnspan=2, sticky='w', padx=8)
        ttk.Label(prep, text='Кадров в секунду').grid(row=6, column=0, sticky='w')
        self.fps_choice = ttk.Combobox(prep, values=FPS_CHOICES, textvariable=self.fps, state='readonly', width=8)
        self.fps_choice.grid(row=6, column=1, sticky='w', padx=8)
        side = ttk.Frame(top)
        side.grid(row=0, column=1, sticky='nsew', padx=(10, 0))
        model = ttk.LabelFrame(side, text='Распознавание поклёвки', padding=8)
        model.pack(fill='x')
        ttk.Label(model, textvariable=self.model_status, wraplength=440, justify='left').pack(anchor='w')
        self.train_button = ttk.Button(model, text='Разметить и проверить', command=self.training)
        self.train_button.pack(anchor='w', pady=6)
        game = ttk.LabelFrame(side, text='Игра и программа', padding=8)
        game.pack(fill='x', pady=(8, 0))
        ttk.Label(game, textvariable=self.addon_status, wraplength=440, justify='left').pack(anchor='w')
        buttons = ttk.Frame(game)
        buttons.pack(anchor='w', pady=(6, 0))
        self.addon_button = ttk.Button(buttons, text='Установить аддон в игру…', command=self.install_addon)
        self.addon_button.pack(side='left')
        ttk.Button(buttons, text='Папка данных', command=lambda: self.open_path(data_dir())).pack(side='left', padx=6)
        ttk.Button(buttons, text='Журнал', command=self.open_log).pack(side='left')
        ttk.Checkbutton(game, text='Сообщать о новой версии', variable=self.check_updates).pack(anchor='w', pady=4)
        run = ttk.Frame(outer)
        run.pack(fill='x', pady=8)
        self.record_check = ttk.Checkbutton(run, text='Записывать кадры забросов для разметки', variable=self.record)
        self.record_check.pack(side='left')
        self.auto_check = ttk.Checkbutton(run, text='Автонажатия (без галочки — только наблюдение)', variable=self.auto)
        self.auto_check.pack(side='left', padx=16)
        self.start_button = ttk.Button(run, text='Начать', command=self.start, style='Accent.TButton')
        self.start_button.pack(side='left')
        ttk.Button(run, text='Стоп · F8', command=lambda: self.stop('Остановлено пользователем')).pack(
            side='left', padx=8
        )
        self.background_check = ttk.Checkbutton(
            outer,
            variable=self.background,
            text='Работать в фоне: игра видна на экране, фокус может быть в другом окне '
            '(клавиши без Shift/Ctrl/Alt; перекрытое окно — стоп)',
        )
        self.background_check.pack(anchor='w')
        self.session_controls = [
            self.same_check, self.recast_check, self.mode_choice, self.select_button, self.hud_button,
            self.paste_button, self.record_check, self.auto_check, self.background_check, self.fps_choice,
            self.start_button, self.train_button, self.addon_button,
        ]  # fmt: skip
        ttk.Label(outer, textvariable=self.status, wraplength=1160, font=('Segoe UI', 11, 'bold')).pack(anchor='w')
        ttk.Label(outer, textvariable=self.observed, wraplength=1160).pack(anchor='w')
        live = ttk.Frame(outer)
        live.pack(fill='both', expand=True, pady=6)
        left = ttk.Frame(live)
        left.pack(side='left', fill='y')
        self.vision_preview = ttk.Label(left)
        self.vision_preview.pack(anchor='nw')
        self.plot = tk.Canvas(left, width=560, height=120, background='#15181c', highlightthickness=0)
        self.plot.pack(anchor='nw', pady=4)
        self.preview = ttk.Label(left)
        self.preview.pack(anchor='nw')
        right = ttk.Frame(live)
        right.pack(side='left', fill='both', expand=True, padx=10)
        ttk.Label(right, textvariable=self.vision_status, wraplength=540, justify='left',
                  font=('Segoe UI', 10, 'bold')).pack(anchor='w')  # fmt: skip
        ttk.Label(right, textvariable=self.stats_text, wraplength=540, justify='left').pack(anchor='w', pady=4)
        ttk.Label(right, textvariable=self.timing_text, wraplength=540, justify='left').pack(anchor='w')
        ttk.Label(right, text='События (новые сверху):').pack(anchor='w', pady=(6, 0))
        self.events = tk.Listbox(right, height=16, borderwidth=0, highlightthickness=0)
        self.events.pack(fill='both', expand=True)

    def sync_key_controls(self):
        if not self.running:
            self.controls[1].configure(state='disabled' if self.same_key.get() else 'readonly')

    # -- errors, files, updates --------------------------------------------------
    def on_callback_error(self, exc_type, exc, tb):
        log.error('UI callback failed', exc_info=(exc_type, exc, tb))
        self.stop(f'Ошибка: {exc}')
        messagebox.showerror(APP_NAME, f'{exc}\n\nПодробности в журнале:\n{logs.log.file}', parent=self)

    @staticmethod
    def open_path(path):
        path = Path(path)
        if not path.suffix:
            path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)

    def open_log(self):
        if logs.log.file and Path(logs.log.file).exists():
            self.open_path(logs.log.file)
        else:
            messagebox.showinfo(APP_NAME, 'Журнал ещё не создан', parent=self)

    def show_update(self, tag, url):
        self.update_url = url
        self.update_text.set(f'Доступна версия {tag.lstrip("v")} — скачать')
        log.info('update available: %s', tag)

    # -- addon -----------------------------------------------------------------
    def refresh_addon_status(self):
        game = addon.find_game()
        if not game:
            self.addon_status.set('Папка игры _forever_ не найдена автоматически — укажи её при установке аддона.')
            return
        installed = addon.addon_version(addon.installed_addon(game))
        bundled = addon.addon_version(addon.bundled_addon())
        if installed is None:
            text = f'Аддон в игре не установлен ({game}).'
        elif installed != bundled:
            text = f'В игре аддон {installed}, в программе {bundled} — обнови аддон и сделай /reload.'
        else:
            text = f'Аддон {installed} установлен в игре.'
        self.addon_status.set(text)

    def install_addon(self):
        found = addon.find_game()
        game = filedialog.askdirectory(
            parent=self,
            title='Папка игры _forever_ (в ней Wow.exe и Interface)',
            initialdir=str(found) if found else None,
            mustexist=True,
        )
        if not game:
            return
        try:
            result = addon.install(Path(game))
        except PermissionError:
            if messagebox.askyesno(
                'Аддон',
                'Папка игры защищена от записи. Установить аддон с правами администратора?',
                parent=self,
            ):
                self.install_addon_elevated(game)
            return
        except (OSError, ValueError) as exc:
            messagebox.showerror('Аддон', str(exc), parent=self)
            return
        log.info('addon: %s', result.message)
        self.refresh_addon_status()
        messagebox.showinfo('Аддон', result.message + '\n\nВ игре: /reload, затем /ahwfish.', parent=self)

    def install_addon_elevated(self, game):
        import ctypes

        if getattr(sys, 'frozen', False):
            exe, params = sys.executable, f'--install-addon "{game}"'
        else:
            exe, params = sys.executable, f'-m ahelperw_fishing --install-addon "{game}"'
        code = ctypes.windll.shell32.ShellExecuteW(None, 'runas', exe, params, None, 0)
        if code <= 32:
            messagebox.showerror('Аддон', 'Установка с правами администратора отменена', parent=self)
        else:
            self.after(3000, self.refresh_addon_status)

    # -- settings ----------------------------------------------------------------
    def load(self):
        prefs = settings.load()
        if prefs.problems:
            self.status.set('Часть настроек не загружена: ' + ', '.join(prefs.problems))
            log.warning('settings problems: %s', prefs.problems)
        if prefs.export:
            self.cfg = prefs.export
            self.hud_status.set('HUD из прошлого запуска; при старте проверяется')
        self.reserve.set(str(prefs.reserve))
        self.mark.set(f'{prefs.mark:g}')
        self.same_key.set(prefs.same_key)
        self.recasts.set(prefs.auto_recast)
        self.background.set(prefs.background)
        self.check_updates.set(prefs.check_updates)
        self.fps.set(str(prefs.fps))
        self.mode.set(MODE_LABELS[prefs.mode])
        self.cast_key.set(prefs.cast)
        self.interact_key.set(prefs.interact)
        if prefs.region and self.cfg:
            try:
                self.set_region(prefs.region)
            except ValueError as exc:
                self.region_status.set(f'Зона из прошлого запуска не подходит: {exc}')
        try:
            model = settings.load_model()
        except (OSError, ValueError) as exc:
            self.model_status.set('Модель не загружена: ' + str(exc))
            return
        if model is None:
            self.model_status.set(
                'Распознавание не проверено: запиши ручную рыбалку, разметь забросы и нажми «Проверить»'
            )
        else:
            self.set_model(model)

    def current_settings(self):
        return settings.Settings(
            export=self.cfg,
            region=self.region,
            mark=float(self.mark.get()),
            reserve=int(self.reserve.get()),
            cast=self.cast_key.get(),
            interact=self.interact_key.get(),
            mode=MODES[self.mode.get()],
            same_key=self.same_key.get(),
            auto_recast=self.recasts.get(),
            background=self.background.get(),
            fps=int(self.fps.get()),
            check_updates=self.check_updates.get(),
        )

    def save_settings(self):
        settings.save(self.current_settings())

    def set_model(self, model):
        if model.get('version') != 1:
            raise ValueError('Неизвестная версия модели')
        self.model = model
        self.params = VisionParams.from_dict(model.get('params'))
        self.vision = VisionSession(self.params)
        ready, why = model.get('ready', (False, ''))
        self.model_status.set(
            f'{model.get("summary", "")}\nПроверка: {"пройдена" if ready else "не пройдена — " + why}. '
            'Автонажатия работают в любом случае; без пройденной проверки чаще ошибаются'
        )

    def on_report(self, report):
        model = {
            'version': 1,
            'params': dict(self.params.to_dict(), threshold=report['threshold']),
            'ready': list(report['ready']),
            'summary': summary(report),
            'evaluated_at': time.time(),
            'report': {k: v for k, v in report.items() if k != 'rows'},
            'rows': report['rows'],
        }
        settings.save_model(model)
        self.set_model(model)
        log.info('model applied: threshold %s, ready %s', report['threshold'], report['ready'][0])

    # -- preparation ------------------------------------------------------------
    def delayed(self, action):
        self.stop('Переключись в игру: снимок через 3 секунды')
        if self.selection_pending:
            return
        self.selection_pending = True
        for control in (self.select_button, self.hud_button, self.start_button):
            control.configure(state='disabled')

        def run():
            self.selection_pending = False
            for control in (self.select_button, self.hud_button, self.start_button):
                control.configure(state='normal')
            try:
                action()
            except Exception as exc:
                log.info('preparation failed: %s', exc)
                messagebox.showerror('Подготовка', str(exc), parent=self)

        self.after(3000, run)

    def snapshot(self):
        _, x, y, w, h = game_window()
        return self.capture.grab((x, y, x + w, y + h)), (w, h)

    def find_hud(self):
        shot, size = self.snapshot()
        rect = locate_hud(shot)
        self.cfg = auto_export(size, rect, int(float(self.mark.get())))
        self.hud_status.set(
            f'HUD найден: {rect["width"]}×{rect["height"]} в ({rect["x"]}, {rect["y"]}), окно {size[0]}×{size[1]}'
        )
        if self.region:
            try:
                validate_region(self.region, self.cfg['screen'])
            except ValueError:
                self.region = None
                self.region_status.set('Зона не подходит к новому окну; выдели заново')
        self.save_settings()
        return shot

    def paste(self):
        self.stop('Остановлено для импорта')
        try:
            self.cfg = validate_export(json.loads(self.clipboard_get().strip()))
            self.mark.set(str(self.cfg['mark']))
            self.hud_status.set('HUD из экспорта /ahwfish export')
            self.save_settings()
        except Exception as exc:
            messagebox.showerror('Экспорт рыбалки', str(exc), parent=self)

    def set_region(self, region):
        self.region = validate_region(region, self.cfg['screen'])
        self.region_status.set(f'Зона: {region["width"]}×{region["height"]} в ({region["x"]}, {region["y"]})')
        self.vision.reset()

    def select_region(self):
        shot = self.find_hud()

        def selected(region):
            r = self.cfg['rect']
            if not (
                region['x'] + region['width'] <= r['x']
                or r['x'] + r['width'] <= region['x']
                or region['y'] + region['height'] <= r['y']
                or r['y'] + r['height'] <= region['y']
            ):
                raise ValueError('Зона пересекает HUD рыбалки; выдели только воду')
            self.set_region(region)
            self.save_settings()

        RegionSelector(self, shot, selected)

    def training(self):
        self.stop('Разметка; автонажатия остановлены')
        if self.label_window is not None and self.label_window.winfo_exists():
            self.label_window.lift()
            self.label_window.focus_force()
            return
        self.label_window = CastLabelWindow(self, sessions_dir(), self.params, self.on_report)

    # -- run ---------------------------------------------------------------------
    def start(self):
        try:
            if self.cfg is None:
                raise ValueError('Сначала «Найти HUD» (или «Вставить экспорт»)')
            validate_export(self.cfg)
            mode = MODES[self.mode.get()]
            if mode == 'vision':
                validate_region(self.region, self.cfg['screen'])
            self.checked = bool(self.model and self.model.get('ready', [False])[0])
            self.controller = FishingController(float(self.mark.get()), int(self.reserve.get()), mode,
                                                self.recasts.get())  # fmt: skip
            interact = self.cast_key.get() if self.same_key.get() else self.interact_key.get()
            self.bindings = {'cast': binding(self.cast_key.get()), 'interact': binding(interact)}
            if self.background.get() and self.auto.get() and any(b['modifiers'] for b in self.bindings.values()):
                raise ValueError(
                    'В фоновом режиме нужны клавиши без Shift/Ctrl/Alt: назначь в игре, например, '
                    '0 или F6 для рыбалки и выбери её здесь'
                )
            self.save_settings()
        except Exception as exc:
            messagebox.showerror('Настройка рыбалки', str(exc), parent=self)
            return
        self.automating = self.auto.get()
        self.params.pixel_scale = max(1, round(self.cfg['screen']['height'] / 1080))
        self.params.fps = int(self.fps.get())
        self.tick_times.clear()
        self.vision = VisionSession(self.params)
        self.prev_obs = None
        self.cast_started = {}
        self.cast_log = CastLog()
        self.scores.clear()
        for values in self.timing.values():
            values.clear()
        self.events.delete(0, 'end')
        self.recorder = None
        if self.record.get() and mode == 'vision':
            try:
                self.recorder = SessionRecorder(
                    sessions_dir(),
                    fps=self.params.fps,
                    meta={
                        'region': self.region,
                        'hud': self.cfg['rect'],
                        'screen': self.cfg['screen'],
                        'mode': 'auto' if self.automating else 'observe',
                        'params': self.params.to_dict(),
                        'keys': {
                            'cast': self.cast_key.get(),
                            'same_key': self.same_key.get(),
                            'auto_recast': self.recasts.get(),
                        },
                        'fps': self.params.fps,
                        'app_version': __version__,
                    },
                )
            except OSError as exc:
                messagebox.showwarning('Запись', 'Запись кадров недоступна: ' + str(exc), parent=self)
        self.halt.clear()
        self.running = True
        self.session_window = None
        self.started_at = time.monotonic()
        self.acquire_deadline = self.started_at + ACQUIRE_SECONDS
        for control in self.controls + self.session_controls:
            control.configure(state='disabled')
        if self.automating and mode == 'vision' and not self.checked:
            self.log('Внимание: проверка распознавания не пройдена — возможны ложные подсечки и пропуски')
        self.log(
            'Запуск: '
            + ('АВТОНАЖАТИЯ' if self.automating else 'наблюдение без нажатий')
            + (', запись ' + self.recorder.id if self.recorder else '')
        )
        log.info('run started: mode=%s auto=%s fps=%s background=%s', mode, self.automating, self.params.fps,
                 self.background.get())  # fmt: skip
        self.status.set(f'Переключись в игру за {ACQUIRE_SECONDS} секунд. F8 — отмена.')

    def stop(self, reason):
        was = self.running
        self.halt.set()
        self.running = False
        if self.controller:
            self.controller.stop(reason)
        if was:
            self.log('Стоп: ' + reason)
            log.info('run stopped: %s', reason)
        if self.recorder:
            # The stop reason stays with the recording for later analysis.
            self.recorder.add({'at': time.monotonic(), 'event': 'stop', 'reason': reason,
                               'cast': self.prev_obs.cast if self.prev_obs else None})  # fmt: skip
            self.recorder.close()
            self.log(f'Запись сохранена: {self.recorder.id}, кадров {self.recorder.frames}')
            self.recorder = None
        self.status.set(reason)
        for i, control in enumerate(self.controls):
            control.configure(state='readonly' if i < 2 else 'normal')
        for control in self.session_controls:
            control.configure(state='readonly' if control in (self.mode_choice, self.fps_choice) else 'normal')
        self.sync_key_controls()

    def log(self, text):
        stamp = time.monotonic() - self.started_at if self.started_at else 0
        self.events.insert(0, f'{stamp:7.2f} с  {text}')
        if self.events.size() > 300:
            self.events.delete(300, 'end')

    def focus_guard(self):
        try:
            if self.background.get():
                window = game_window(self.cfg['screen'], self.session_window[0])
                return window == self.session_window and visible(window[0], self.watch_points(window))
            return game_window(self.cfg['screen']) == self.session_window
        except Exception:
            return False

    def watch_points(self, window):
        """Screen points that must show the game: HUD centre and the zone's corners/centre."""
        _, ox, oy, _, _ = window
        r = self.cfg['rect']
        points = [(ox + r['x'] + r['width'] / 2, oy + r['y'] + r['height'] / 2)]
        a = self.region if self.controller and self.controller.mode == 'vision' and self.region else None
        if a:
            for fx, fy in ((0.05, 0.05), (0.95, 0.05), (0.5, 0.5), (0.05, 0.95), (0.95, 0.95)):
                points.append((ox + a['x'] + a['width'] * fx, oy + a['y'] + a['height'] * fy))
        return points

    def tick(self):
        started = time.perf_counter()
        try:
            if self.running:
                self.tick_times.append(started)
                self.sample()
        except Exception as exc:
            log.exception('sample failed')
            self.stop('Ошибка: ' + str(exc))
        finally:
            delay = 100
            if self.running:
                spent = (time.perf_counter() - started) * 1000
                self.timing['loop'].append(spent)
                # Target the chosen rate; a slow frame (bobber search) simply delays the next one.
                delay = max(1, int(1000 / max(1, self.params.fps) - spent))
            self.after(delay, self.tick)

    def acquire_window(self, now):
        """The game window for this frame, or None (paused/stopped)."""
        try:
            if self.background.get() and self.session_window is not None:
                # Background mode: keep the acquired window; it only has to stay visible.
                window = game_window(self.cfg['screen'], self.session_window[0])
                if not visible(window[0], self.watch_points(window)):
                    raise ValueError('Окно игры перекрыто другим окном')
            else:
                window = game_window(self.cfg['screen'])
        except ValueError as exc:
            if not self.automating and self.session_window is not None:
                # Observation pauses instead of stopping; the interrupted cast is skipped.
                if self.prev_obs is not None:
                    self.log('Пауза: ' + str(exc))
                self.prev_obs = None
                self.vision.reset()
                self.status.set('Пауза наблюдения: ' + str(exc) + '. Вернись в игру — продолжу со следующего заброса')
                return None
            if self.session_window is not None or now >= self.acquire_deadline:
                self.stop(str(exc))
            return None
        if self.session_window is None:
            self.session_window = window
            self.log('Окно Forever захвачено')
        elif window != self.session_window:
            if self.automating:
                self.stop('Окно перемещено или изменилось; запусти заново')
                return None
            self.session_window = window
            self.prev_obs = None
            self.vision.reset()
            self.log('Окно игры изменилось — продолжаю со следующего заброса')
        return window

    def grab(self, window, visual):
        """(HUD image, zone image or None) of one frame."""
        _, ox, oy, _, _ = window
        r = self.cfg['rect']
        area = self.region if visual else r
        left, top = min(r['x'], area['x']), min(r['y'], area['y'])
        right = max(r['x'] + r['width'], area['x'] + area['width'])
        bottom = max(r['y'] + r['height'], area['y'] + area['height'])
        if visual and (bottom - top) * (right - left) > (area['width'] * area['height'] + r['width'] * r['height']) * 2:
            # Far apart: two small grabs are cheaper than one huge rectangle.
            image = self.capture.grab((ox + r['x'], oy + r['y'], ox + r['x'] + r['width'], oy + r['y'] + r['height']))
            crop = self.capture.grab(
                (ox + area['x'], oy + area['y'], ox + area['x'] + area['width'], oy + area['y'] + area['height'])
            )
            return image, crop
        shot = self.capture.grab((ox + left, oy + top, ox + right, oy + bottom))
        image = shot.crop((r['x'] - left, r['y'] - top, r['x'] - left + r['width'], r['y'] - top + r['height']))
        crop = None
        if visual:
            crop = shot.crop(
                (area['x'] - left, area['y'] - top, area['x'] - left + area['width'],
                 area['y'] - top + area['height'])
            )  # fmt: skip
        return image, crop

    def sample(self):
        now = time.monotonic()
        if stop_pressed():
            self.stop('Остановлено клавишей F8')
            return
        window = self.acquire_window(now)
        if window is None:
            return
        visual = self.controller.mode == 'vision'
        grab_started = time.perf_counter()
        image, crop = self.grab(window, visual)
        captured_at = time.monotonic()
        self.timing['grab'].append((time.perf_counter() - grab_started) * 1000)
        try:
            obs = decode(image, captured_at)
        except ValueError as exc:
            self.observed.set('HUD не читается: ' + str(exc))
            if not self.automating and now - self.last_relocate > 2:
                self.last_relocate = now
                self.relocate_hud(window)
            self.controller.step(None, time.monotonic())
            if self.automating and self.controller.phase == 'stopped':
                self.stop(self.controller.reason)
            return
        self.track_casts(obs, captured_at)
        bite, result = None, None
        if visual:
            vision_started = time.perf_counter()
            bite, result = self.observe_vision(crop, obs, captured_at)
            self.timing['vision'].append((time.perf_counter() - vision_started) * 1000)
        if self.recorder:
            self.recorder.add(
                self.row(obs, result, bite), crop if obs.state in ('fishing', 'loot') and result else None
            )
        sent = None
        if self.automating:
            sent = self.automate(obs, bite, captured_at)
            if sent is False:
                return
        else:
            self.status.set('Наблюдение — клавиши не отправляются')
        self.prev_obs = obs
        if time.monotonic() - self.last_ui > 0.2 or sent or (result and result.fire):
            self.last_ui = time.monotonic()
            self.update_live(image, crop, obs, result)

    def automate(self, obs, bite, captured_at):
        """Run the controller on this frame and send its key. Returns the action sent,
        None if nothing was sent, or False if the frame must end here (stopped/waiting)."""
        action = self.controller.step(obs, time.monotonic(), bite=bite)
        if self.controller.rearm is not None:
            if self.vision.rearm(self.controller.rearm):
                self.log(f'Ложная подсечка в забросе {self.controller.rearm} — продолжаю ждать поклёвку')
            self.controller.rearm = None
        if self.controller.phase == 'stopped':
            self.stop(self.controller.reason)
            return False
        sent = None
        if action:
            if time.monotonic() - obs.captured_at > 0.3:
                self.stop('Кадр устарел перед нажатием')
                return False
            if self.background.get() and foreground_window() != self.session_window[0]:
                result_send = post_pulse(self.session_window[0], self.bindings[action], self.focus_guard, self.halt)
            else:
                result_send = pulse(self.bindings[action], self.focus_guard, self.halt)
            if result_send in ('physical_modifier_held', 'physical_key_held'):
                # Never combine our key with one the user holds; retry on the next fresh frame.
                self.status.set('Отпусти Shift/Ctrl/Alt или клавишу рыбалки — нажатие ждёт')
                return False
            if result_send != 'sent':
                self.stop('Нажатие отменено: ' + result_send)
                return False
            pressed = time.monotonic()
            self.controller.sent(action, pressed)
            self.timing['press'].append((pressed - captured_at) * 1000)
            self.log(
                ('ЗАБРОС' if action == 'cast' else 'ПОДСЕЧКА')
                + f' — клавиша отправлена через {(pressed - captured_at) * 1000:.0f} мс после кадра'
            )
            sent = action
            if action == 'interact' and obs.cast in self.cast_log.casts:
                self.cast_log.casts[obs.cast]['sent'] = pressed
            if self.recorder:
                self.recorder.add({'at': pressed, 'event': 'sent', 'action': action, 'cast': obs.cast,
                                   'frame_at': captured_at})  # fmt: skip
        self.status.set('Автонажатия · ' + self.controller.reason)
        return sent

    def relocate_hud(self, window):
        _, x, y, w, h = window
        try:
            rect = locate_hud(self.capture.grab((x, y, x + w, y + h)))
        except ValueError:
            return
        if rect != self.cfg['rect']:
            self.cfg = auto_export((w, h), rect, self.cfg.get('mark', 65))
            self.log('HUD перемещён — найден заново')
            self.save_settings()

    def track_casts(self, obs, at):
        prev = self.prev_obs
        if obs.state == 'fishing' and (prev is None or prev.state != 'fishing' or prev.cast != obs.cast):
            observed = prev is not None
            self.cast_started[obs.cast] = at if observed else None
            self.cast_log.start(obs.cast, at, observed)
            self.log(f'Заброс {obs.cast} начался' + ('' if observed else ' до запуска — пропускаю его'))
        if prev is not None and prev.state == 'fishing' and (obs.state != 'fishing' or obs.cast != prev.cast):
            entry = self.cast_log.casts.get(prev.cast)
            if entry and entry['end'] is None:
                entry['end'] = at
                self.log(f'Заброс {prev.cast} закончился')
        if prev is not None and obs.loot != prev.loot:
            target = obs.loot_cast if obs.protocol == 2 else prev.cast
            for entry in self.cast_log.casts.values():
                if (obs.protocol == 1 and entry['cast'] == target) or (
                    obs.protocol == 2 and entry['cast'] % 256 == target
                ):
                    entry['catch'] = True
            self.log(f'Улов подтверждён аддоном (заброс {target})')
        if (
            prev is not None
            and obs.error_serial is not None
            and prev.error_serial is not None
            and obs.error_serial != prev.error_serial
        ):
            kind = ERROR_CODES.get(obs.error_code, 'other')
            self.log('Сообщение игры: ' + ERROR_REASONS[kind])
            entry = self.cast_log.casts.get(obs.cast) or self.cast_log.casts.get(prev.cast)
            if entry:
                entry['errors'].append(kind)

    def observe_vision(self, crop, obs, captured_at):
        started = self.cast_started.get(obs.cast)
        if obs.state != 'fishing' or obs.error or started is None:
            reason = 'Нет активного заброса' if obs.state != 'fishing' else 'Начало заброса не видно — жду следующий'
            self.vision_status.set(reason)
            return None, None
        result = self.vision.observe(obs.cast, started, crop, captured_at, time.monotonic())
        entry = self.cast_log.casts.get(obs.cast)
        if entry:
            entry['phase'] = result.phase
        if result.score is not None:
            self.scores.append((captured_at, result.score))
        text = f'{PHASE_NAMES.get(result.phase, result.phase)}: {result.reason}'
        if result.score is not None:
            text += f' · оценка {result.score:.1f} / порог {self.params.threshold:g} (не вероятность)'
        self.vision_status.set(text)
        if result.fire:
            if entry and entry['signal'] is None:
                entry['signal'] = captured_at
            self.log(f'СИГНАЛ ПОКЛЁВКИ (заброс {obs.cast}, {captured_at - started:.2f} с, оценка {result.score:.1f})')
            return BiteSignal(captured_at, obs.cast), result
        if result.phase in ('ambiguous', 'lost', 'no_reference') and entry and not entry.get('reported'):
            entry['reported'] = True
            self.log(f'Заброс {obs.cast}: {result.reason}')
        return None, result

    def row(self, obs, result, bite):
        row = {
            'at': obs.captured_at,
            'state': obs.state,
            'cast': obs.cast,
            'loot': obs.loot,
            'free': obs.free,
            'progress': round(obs.progress, 3),
            'protocol': obs.protocol,
            'loot_cast': obs.loot_cast,
            'error_serial': obs.error_serial,
            'error_code': obs.error_code,
            'cast_at': self.cast_started.get(obs.cast) if obs.state in ('fishing', 'loot') else None,
        }
        if result is not None:
            row.update(
                phase=result.phase,
                score=None if result.score is None else round(result.score, 2),
                box=result.box,
                signal=bool(bite),
            )
        return row

    # -- live diagnostics -------------------------------------------------------
    def update_live(self, hud, crop, obs, result):
        self.photo = ImageTk.PhotoImage(hud.resize((max(1, hud.width // 2), max(1, hud.height // 2))))
        self.preview.configure(image=self.photo)
        if crop is not None:
            self.vision_photo = ImageTk.PhotoImage(overlay(crop, result, (560, 330)))
            self.vision_preview.configure(image=self.vision_photo)
        stats = self.controller.stats
        self.observed.set(
            f'HUD: {obs.state} · свободно {obs.free if obs.free is not None else "?"} · заброс {obs.cast}'
            f' · улов {obs.loot}'
            + (f' (для заброса {obs.loot_cast})' if obs.protocol == 2 else ' · HUD v1')
            + f' · прогресс {obs.progress:.0%}'
        )
        text = self.cast_log.summary()
        if self.automating:
            text += (
                f'\nАвто: забросов {stats.casts}, подсечек {stats.signals}, улов {stats.catches}, '
                f'ложных {stats.false_signals}, уплыло {stats.escaped}, без сигнала {stats.no_signal}'
            )
        self.stats_text.set(text)

        def med(name):
            values = self.timing[name]
            return f'{statistics.median(values):.0f}' if values else '—'

        ticks = self.tick_times
        fps = (len(ticks) - 1) / (ticks[-1] - ticks[0]) if len(ticks) > 1 and ticks[-1] > ticks[0] else 0
        self.timing_text.set(
            f'Захват {med("grab")} мс · распознавание {med("vision")} мс · цикл {med("loop")} мс · '
            f'≈{fps:.0f} кадров/с · кадр→нажатие {med("press")} мс · захват: {self.capture.backend}'
        )
        self.draw_plot()

    def draw_plot(self):
        c = self.plot
        c.delete('all')
        if not self.scores:
            return
        w, h = int(c['width']), int(c['height'])
        end = self.scores[-1][0]
        span = 12.0
        top = max(*(s for _, s in self.scores), self.params.threshold * 1.5)

        def x(t):
            return w - 6 - (end - t) / span * (w - 12)

        def y(s):
            return h - 6 - (h - 12) * max(0, min(s, top)) / top

        c.create_line(0, y(self.params.threshold), w, y(self.params.threshold), fill='#ff6b6b', dash=(4, 3))
        pts = [(x(t), y(s)) for t, s in self.scores if end - t <= span]
        for a, b in pairwise(pts):
            c.create_line(*a, *b, fill='#7fc8ff')
        c.create_text(6, 8, text='оценка всплеска за 12 с; пунктир — порог', anchor='w', fill='#8a9099',
                      font=('Segoe UI', 8))  # fmt: skip

    def close(self):
        self.stop('Закрыто')
        try:
            if self.cfg:
                self.save_settings()
        except (OSError, ValueError):
            log.warning('settings not saved on close', exc_info=True)
        self.capture.close()
        log.info('closed')
        # Release Tk images while their interpreter is alive (a PhotoImage collected after
        # destroy() calls into a deleted interpreter).
        self.photo = self.vision_photo = None
        if self.label_window is not None and self.label_window.winfo_exists():
            self.label_window.destroy()
        self.destroy()


# -- entry point -----------------------------------------------------------------
def parse_args(argv):
    parser = argparse.ArgumentParser(prog='AHelperWFishing', description=APP_NAME)
    parser.add_argument('--version', action='version', version=f'{APP_NAME} {__version__}')
    parser.add_argument('--install-addon', nargs='?', const='', metavar='GAME',
                        help='install the bundled HUD addon into the _forever_ folder and exit')  # fmt: skip
    parser.add_argument('--result-file', help='write the --install-addon message to this file')
    parser.add_argument('--smoke-test', action='store_true', help='open the window and close it (build check)')
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    logs.setup()
    if args.install_addon is not None:
        code, message = addon.install_cli(args.install_addon or None)
        log.info('--install-addon %r -> %s %s', args.install_addon, code, message)
        if args.result_file:
            try:
                Path(args.result_file).write_text(message, encoding='utf-8')
            except OSError:
                log.warning('result file not written', exc_info=True)
        return code
    enable_dpi_awareness()
    with SingleInstance() as instance:
        if not instance.acquired:
            message_box('Программа уже запущена (или открыт боевой помощник AHelperW). Закрой её и повтори.', APP_NAME)
            return 1
        app = FishingApp()
        if args.smoke_test:
            app.after(400, app.close)
        app.mainloop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
