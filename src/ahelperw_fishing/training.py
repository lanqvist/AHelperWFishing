"""Region selection and per-cast labelling of recorded fishing frames."""

import queue
import threading
import time
import tkinter as tk
from itertools import pairwise
from tkinter import messagebox, ttk

from PIL import ImageDraw, ImageTk

from .sessions import SessionStore, TraceCache, evaluate, first_signal, labelled, replay, summary, trace_of
from .vision import VisionParams

PHASE_COLORS = {
    'armed': '#38d26b',
    'fired': '#ff3b3b',
    'baseline': '#3fa7ff',
    'settling': '#3fa7ff',
    'searching': '#ffd84a',
    'ambiguous': '#ff8c1a',
    'lost': '#ff8c1a',
}
PHASE_NAMES = {
    'reference': 'запоминаю воду',
    'searching': 'ищу поплавок',
    'settling': 'всплеск заброса',
    'baseline': 'набираю спокойные кадры',
    'armed': 'готов',
    'fired': 'СИГНАЛ ПОКЛЁВКИ',
    'lost': 'поплавок потерян',
    'ambiguous': 'неоднозначно',
    'no_reference': 'начало не видно',
}


def overlay(image, result, max_size=(560, 380)):
    """Thumbnail with the detector's bobber box and analysis core."""
    view = image.copy()
    scale = min(max_size[0] / view.width, max_size[1] / view.height, 1.0)
    view = view.resize((max(1, int(view.width * scale)), max(1, int(view.height * scale))))
    if result is not None:
        draw = ImageDraw.Draw(view)
        color = PHASE_COLORS.get(result.phase, '#ffffff')
        if result.box:
            x0, y0, x1, y1 = (v * scale for v in result.box)
            draw.rectangle((x0, y0, x1, y1), outline=color, width=2)
            cx, cy, r = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) * 1.3
            draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=color)
        for box in result.others or []:
            draw.rectangle(tuple(v * scale for v in box), outline='#ff8c1a', width=2)
    return view


class RegionSelector(tk.Toplevel):
    def __init__(self, parent, screenshot, callback):
        super().__init__(parent)
        self.title('Область заброса — обведи воду, куда падает поплавок')
        self.original = screenshot
        self.callback = callback
        preview = screenshot.copy()
        preview.thumbnail((1200, 700))
        self.scale_x = screenshot.width / preview.width
        self.scale_y = screenshot.height / preview.height
        self.photo = ImageTk.PhotoImage(preview)
        ttk.Label(
            self,
            text='Зажми ЛКМ и обведи воду, куда падает поплавок при забросах, с запасом в 2–3 поплавка '
            'по краям.\nНе включай персонажей, их имена, берег, лодки, фонари и интерфейс. '
            'Чужие поплавки допустимы, но рядом с твоим подсечки не будет. Esc — отмена.',
        ).pack(padx=12, pady=8)
        self.canvas = tk.Canvas(self, width=preview.width, height=preview.height, highlightthickness=0)
        self.canvas.pack(padx=12, pady=8)
        self.canvas.create_image(0, 0, image=self.photo, anchor='nw')
        self.start_point = None
        self.rectangle = None
        self.canvas.bind('<ButtonPress-1>', self.begin)
        self.canvas.bind('<B1-Motion>', self.move)
        self.canvas.bind('<ButtonRelease-1>', self.finish)
        self.bind('<Escape>', lambda _: self.destroy())
        self.lift()
        self.focus_force()

    def point(self, event):
        return max(0, min(event.x, self.photo.width())), max(0, min(event.y, self.photo.height()))

    def begin(self, event):
        self.start_point = self.point(event)
        if self.rectangle:
            self.canvas.delete(self.rectangle)
        self.rectangle = self.canvas.create_rectangle(*self.start_point, *self.start_point, outline='#ffcc00', width=2)

    def move(self, event):
        if self.start_point:
            self.canvas.coords(self.rectangle, *self.start_point, *self.point(event))

    def finish(self, event):
        if not self.start_point:
            return
        x1, y1 = self.start_point
        x2, y2 = self.point(event)
        left, right = sorted((round(x1 * self.scale_x), round(x2 * self.scale_x)))
        top, bottom = sorted((round(y1 * self.scale_y), round(y2 * self.scale_y)))
        region = dict(x=left, y=top, width=right - left, height=bottom - top)
        try:
            self.callback(region)
        except Exception as exc:
            messagebox.showerror('Область', str(exc), parent=self)
            return
        self.destroy()


class CastLabelWindow(tk.Toplevel):
    """Label the first bite frame of each recorded cast and check the detector."""

    def __init__(self, parent, root, params, on_model):
        super().__init__(parent)
        self.title('Разметка забросов и проверка распознавания')
        scale = self.winfo_fpixels('1i') / 96
        self.geometry(
            f'{int(min(self.winfo_screenwidth() * 0.92, 1280 * scale))}'
            f'x{int(min(self.winfo_screenheight() * 0.9, 900 * scale))}'
        )
        self.minsize(int(900 * scale), int(650 * scale))
        self.store, self.params, self.on_model = SessionStore(root), params, on_model
        self.sessions = {}
        self.cache = {}
        self.current = None  # (session, record)
        self.results = []  # [(t, VisionResult)] for the current cast
        self.index = 0
        self.photo = None
        self.report = None
        self.busy = False
        self.results_queue = queue.Queue()
        self.polling = False
        ttk.Label(
            self,
            wraplength=1200,
            justify='left',
            text=(
                'Выбери заброс слева и найди кадр, где впервые виден всплеск поклёвки. '
                'Нажми «Поклёвка с этого кадра» (B). Все кадры до него считаются обычным поплавком: '
                'всплеск заброса, волны, рябь. Если поклёвки не было — «Без поклёвки» (N); '
                'если не уверен — «Сомнительно» (U): такой заброс не участвует в обучении и проверке. '
                'Сигнал детектора показан красным и меткой не является.'
            ),
        ).pack(anchor='w', padx=12, pady=(10, 4))
        body = ttk.Panedwindow(self, orient='horizontal')
        body.pack(fill='both', expand=True, padx=12, pady=4)
        left = ttk.Frame(body)
        body.add(left, weight=1)
        self.tree = ttk.Treeview(left, columns=('dur', 'catch', 'signal', 'label'), height=20, selectmode='browse')
        for col, text, width in (
            ('#0', 'Заброс', 170),
            ('dur', 'Кадров', 60),
            ('catch', 'Улов', 60),
            ('signal', 'Сигнал', 70),
            ('label', 'Метка', 230),
        ):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, stretch=col == '#0')
        scroll = ttk.Scrollbar(left, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.tree.pack(fill='both', expand=True)
        self.tree.bind('<<TreeviewSelect>>', self.select)
        tools = ttk.Frame(left)
        tools.pack(fill='x', pady=6)
        ttk.Button(tools, text='Следующий без метки', command=self.next_unlabelled).pack(side='left')
        ttk.Button(tools, text='Удалить запись', command=self.delete_session).pack(side='right')
        right = ttk.Frame(body)
        body.add(right, weight=3)
        self.preview = ttk.Label(right)
        self.preview.pack(anchor='n', pady=4)
        self.slider = ttk.Scale(right, from_=0, to=0, orient='horizontal', command=self.slide)
        self.slider.pack(fill='x', padx=4)
        self.info = ttk.Label(right, wraplength=760, justify='left')
        self.info.pack(anchor='w', pady=4)
        self.plot = tk.Canvas(right, height=150, background='#15181c', highlightthickness=0)
        self.plot.pack(fill='x', padx=4, pady=4)
        self.plot.bind('<Button-1>', self.plot_click)
        buttons = ttk.Frame(right)
        buttons.pack(fill='x', pady=6)
        ttk.Button(buttons, text='◀ кадр', command=lambda: self.step(-1)).pack(side='left')
        ttk.Button(buttons, text='кадр ▶', command=lambda: self.step(1)).pack(side='left', padx=(4, 16))
        ttk.Button(buttons, text='Поклёвка с этого кадра (B)', command=lambda: self.set_label('bite')).pack(side='left')
        ttk.Button(buttons, text='Без поклёвки (N)', command=lambda: self.set_label('no_bite')).pack(
            side='left', padx=6
        )
        ttk.Button(buttons, text='Сомнительно (U)', command=lambda: self.set_label('unsure')).pack(side='left')
        ttk.Button(buttons, text='Снять метку', command=lambda: self.set_label(None)).pack(side='left', padx=6)
        bottom = ttk.LabelFrame(self, text='Проверка на отложенных забросах', padding=8)
        bottom.pack(fill='x', padx=12, pady=(0, 10))
        self.check_button = ttk.Button(bottom, text='Проверить распознавание', command=self.check)
        self.check_button.pack(side='left')
        self.apply_button = ttk.Button(bottom, text='Использовать этот результат', command=self.apply, state='disabled')
        self.apply_button.pack(side='left', padx=8)
        self.result = ttk.Label(
            bottom,
            wraplength=900,
            justify='left',
            text='Метрики считаются по забросам: найдена, пропущена, ложный сигнал. Не по доле кадров.',
        )
        self.result.pack(side='left', padx=8)
        self.problems = tk.Listbox(self, height=4)
        self.problems.pack(fill='x', padx=12, pady=(0, 10))
        self.problems.bind('<Double-Button-1>', self.jump_problem)
        for key, action in (
            ('<Left>', lambda e: self.step(-1)),
            ('<Right>', lambda e: self.step(1)),
            ('b', lambda e: self.set_label('bite')),
            ('n', lambda e: self.set_label('no_bite')),
            ('u', lambda e: self.set_label('unsure')),
        ):
            self.bind(key, action)
        self.reload()

    def background(self, work):
        """Run ``work`` in a thread; the callable it returns runs on the Tk thread.

        Worker threads never call Tk themselves: results come back through a queue polled
        from the UI thread, and are dropped if the window was closed meanwhile.
        """
        threading.Thread(target=lambda: self.results_queue.put(work()), daemon=True).start()
        if not self.polling:
            self.polling = True
            self.after(50, self.poll)

    def poll(self):
        try:
            while True:
                self.results_queue.get_nowait()()
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(50, self.poll)

    def destroy(self):
        self.photo = None  # release the Tk image while the interpreter is alive
        self.current = None
        super().destroy()

    # -- listing -------------------------------------------------------------
    def reload(self, keep=None):
        self.tree.delete(*self.tree.get_children())
        self.sessions = {s.id: s for s in self.store.sessions()}
        for s in self.sessions.values():
            node = self.tree.insert(
                '', 'end', iid=s.id, text=s.id, open=True, values=('', '', '', f'{len(s.labels)} меток')
            )
            for cast, record in sorted(s.casts.items()):
                self.tree.insert(
                    node, 'end', iid=f'{s.id}/{cast}', text=f'заброс {cast}', values=self.row_values(s, record)
                )
        if keep and self.tree.exists(keep):
            self.tree.selection_set(keep)
            self.tree.see(keep)
        if not self.sessions:
            self.info.configure(text='Записей пока нет. Начни наблюдение без автонажатий и порыбачь вручную.')

    def row_values(self, session, record):
        label = session.labels.get(record.cast)
        text = {'bite': 'поклёвка', 'no_bite': 'без поклёвки', 'unsure': 'сомнительно'}.get(
            (label or {}).get('status'), '—'
        )
        if label and label.get('status') == 'bite':
            text += f' {label["bite_at"]:.2f} с'
        if label and label.get('by') != 'user':
            text += ' (не моя — проверь)'
        catch = {True: 'да', False: 'нет', None: 'v1?' if record.v1_catch else '?'}[record.catch]
        signal = f'{record.signal_at - record.started_at:.1f} с' if record.signal_at else '—'
        return (len(record.frames), catch, signal, text)

    def select(self, _event=None):
        chosen = self.tree.selection()
        if not chosen or '/' not in chosen[0]:
            return
        sid, cast = chosen[0].rsplit('/', 1)
        session = self.sessions[sid]
        record = session.casts[int(cast)]
        self.current = (session, record)
        self.results = []
        self.slider.configure(to=max(0, len(record.frames) - 1))
        label = session.labels.get(record.cast)
        self.index = 0
        if label and label.get('bite_at') is not None:
            self.index = self.frame_at(label['bite_at'])
        self.slider.set(self.index)
        self.show()
        self.analyse(session, record)

    def analyse(self, session, record):
        """Replay the detector for this cast in the background (diagnostics only)."""
        key = record.key

        def work():
            try:
                out = replay(
                    session, record, VisionParams.from_dict(dict(self.params.to_dict(), threshold=1e9)), self.cache
                )
            except Exception as exc:
                out = exc
            return lambda: self.analysed(key, out)

        self.info.configure(text='Анализирую кадры заброса…')
        self.background(work)

    def analysed(self, key, out):
        if not self.current or self.current[1].key != key:
            return
        if isinstance(out, Exception):
            self.info.configure(text='Ошибка анализа: ' + str(out))
            return
        self.results = out
        self.show()
        self.draw_plot()

    def frame_at(self, t):
        _, record = self.current
        return min(range(len(record.frames)), key=lambda i: abs(record.frames[i]['at'] - record.started_at - t))

    # -- viewing -------------------------------------------------------------
    def slide(self, value):
        index = int(float(value))
        if index != self.index:
            self.index = index
            self.show()
            self.draw_plot()

    def step(self, delta):
        if not self.current:
            return
        self.index = max(0, min(len(self.current[1].frames) - 1, self.index + delta))
        self.slider.set(self.index)
        self.show()
        self.draw_plot()

    def show(self):
        if not self.current:
            return
        session, record = self.current
        if not record.frames:
            return
        row = record.frames[self.index]
        result = self.results[self.index][1] if self.index < len(self.results) else None
        try:
            image = session.image(row)
        except OSError as exc:
            self.info.configure(text=str(exc))
            return
        self.photo = ImageTk.PhotoImage(overlay(image, result, (760, 420)))
        self.preview.configure(image=self.photo)
        t = row['at'] - record.started_at
        text = f'Кадр {self.index + 1}/{len(record.frames)} · {t:.2f} с от начала заброса'
        if result is not None:
            text += f' · {PHASE_NAMES.get(result.phase, result.phase)}'
            if result.score is not None:
                text += f' · оценка {result.score:.1f} (порог {self.params.threshold:g})'
            text += f' · {result.reason}'
        label = session.labels.get(record.cast)
        if label:
            text += f' · метка: {label["status"]}' + (
                f' {label["bite_at"]:.2f} с' if label.get('bite_at') is not None else ''
            )
        self.info.configure(text=text)

    def draw_plot(self):
        c = self.plot
        c.delete('all')
        if not self.current:
            return
        session, record = self.current
        w, h = max(200, c.winfo_width()), int(c['height'])
        end = max(1.0, record.frames[-1]['at'] - record.started_at)
        top = max([r.score or 0 for _, r in self.results] + [self.params.threshold * 1.5])

        def x(t):
            return 30 + (w - 40) * t / end

        def y(s):
            return h - 18 - (h - 30) * max(0, min(s, top)) / top

        c.create_line(30, y(self.params.threshold), w - 10, y(self.params.threshold), fill='#ff6b6b', dash=(4, 3))
        c.create_text(4, y(self.params.threshold), text='порог', anchor='w', fill='#ff6b6b', font=('Segoe UI', 7))
        pts = [(x(t), y(r.score)) for t, r in self.results if r.score is not None]
        for a, b in pairwise(pts):
            c.create_line(*a, *b, fill='#7fc8ff')
        signal = first_signal(trace_of(self.results), self.params.threshold)
        if signal is not None:
            c.create_line(x(signal), 4, x(signal), h - 18, fill='#ff3b3b', width=2)
            c.create_text(x(signal) + 3, 10, text='сигнал', anchor='w', fill='#ff3b3b', font=('Segoe UI', 8))
        label = session.labels.get(record.cast)
        if label and label.get('bite_at') is not None:
            c.create_line(x(label['bite_at']), 4, x(label['bite_at']), h - 18, fill='#38d26b', width=2)
            c.create_text(
                x(label['bite_at']) + 3, 24, text='поклёвка (метка)', anchor='w', fill='#38d26b', font=('Segoe UI', 8)
            )
        now = record.frames[self.index]['at'] - record.started_at
        c.create_line(x(now), 0, x(now), h, fill='#ffffff')
        for t in range(0, int(end) + 1, 5):
            c.create_text(x(t), h - 8, text=f'{t} с', fill='#8a9099', font=('Segoe UI', 7))

    def plot_click(self, event):
        if not self.current:
            return
        _, record = self.current
        end = max(1.0, record.frames[-1]['at'] - record.started_at)
        w = max(200, self.plot.winfo_width())
        t = (event.x - 30) / (w - 40) * end
        self.index = self.frame_at(t)
        self.slider.set(self.index)
        self.show()
        self.draw_plot()

    # -- labelling -----------------------------------------------------------
    def set_label(self, status):
        if not self.current:
            return
        session, record = self.current
        bite_at = record.frames[self.index]['at'] - record.started_at if status == 'bite' else None
        try:
            session.label(record.cast, status, bite_at)
        except ValueError as exc:
            messagebox.showerror('Метка', str(exc), parent=self)
            return
        self.tree.item(record.key, values=self.row_values(session, record))
        self.tree.item(session.id, values=('', '', '', f'{len(session.labels)} меток'))
        self.report = None
        self.apply_button.configure(state='disabled')
        self.show()
        self.draw_plot()

    def next_unlabelled(self):
        for s in self.sessions.values():
            for cast in sorted(s.casts):
                if cast not in s.labels:
                    key = f'{s.id}/{cast}'
                    self.tree.selection_set(key)
                    self.tree.see(key)
                    return
        messagebox.showinfo('Разметка', 'Все записанные забросы размечены', parent=self)

    def delete_session(self):
        chosen = self.tree.selection()
        if not chosen:
            return
        sid = chosen[0].split('/')[0]
        if not messagebox.askyesno(
            'Удалить запись', f'Удалить запись {sid} с кадрами и метками без возможности восстановления?', parent=self
        ):
            return
        try:
            self.store.delete(sid)
        except (OSError, ValueError) as exc:
            messagebox.showerror('Удаление', str(exc), parent=self)
        self.current = None
        self.reload()

    # -- evaluation ----------------------------------------------------------
    def check(self):
        if self.busy:
            return
        sessions = list(self.sessions.values())
        if not labelled(sessions):
            messagebox.showinfo('Проверка', 'Сначала разметь хотя бы несколько забросов', parent=self)
            return
        self.busy = True
        self.check_button.configure(state='disabled')
        self.result.configure(
            text='Проверяю: детектор прогоняется по каждому размеченному забросу, '
            'порог выбирается без отложенного заброса…'
        )
        started = time.monotonic()

        def work():
            try:
                cache = TraceCache(sessions, self.params)
                out = evaluate(sessions, self.params, cache)
                cache.save()
            except Exception as exc:
                out = exc
            return lambda: self.checked(out, time.monotonic() - started)

        self.background(work)

    def checked(self, report, seconds):
        self.busy = False
        self.check_button.configure(state='normal')
        if isinstance(report, Exception):
            self.result.configure(text='Ошибка проверки: ' + str(report))
            return
        self.report = report
        self.result.configure(text=summary(report) + f' ({seconds:.0f} с)')
        self.apply_button.configure(state='normal')
        self.problems.delete(0, 'end')
        names = {'miss': 'ПРОПУСК', 'late': 'ПОЗДНО', 'false': 'ЛОЖНЫЙ СИГНАЛ'}
        for row in report['rows']:
            if row['result'] in names:
                sig = 'нет' if row['signal'] is None else f'{row["signal"]:.2f} с'
                bite = '' if row['bite_at'] is None else f', поклёвка {row["bite_at"]:.2f} с'
                self.problems.insert(
                    'end',
                    f'{names[row["result"]]} · {row["cast"]} · сигнал {sig}{bite}'
                    + (' · поплавок не определён' if row['undecided'] else ''),
                )
        if not self.problems.size():
            self.problems.insert('end', 'Ошибок на отложенных забросах нет')

    def jump_problem(self, _event=None):
        chosen = self.problems.curselection()
        if not chosen:
            return
        parts = self.problems.get(chosen[0]).split(' · ')
        if len(parts) > 1 and self.tree.exists(parts[1]):
            self.tree.selection_set(parts[1])
            self.tree.see(parts[1])

    def apply(self):
        if self.report is None:
            return
        self.params.threshold = self.report['threshold']
        self.on_model(self.report)
        self.apply_button.configure(state='disabled')
        self.draw_plot()
        self.show()
