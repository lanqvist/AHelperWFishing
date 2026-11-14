"""Deterministic fishing cycle: decides when to cast and when to hook. Sends no input itself.

Every action the controller asks for must be acknowledged with ``sent()`` by the input
layer, and every press must then be confirmed by the addon (a new cast serial, a catch
bound to that cast) before the next one. Anything unexpected stops the cycle.
"""

import math
from dataclasses import dataclass, field

from .protocol import ERROR_CODES, ERROR_REASONS

FRESH = 0.3  # s: an observation older than this is never acted on
HEARTBEAT_TIMEOUT = 0.8  # s without a new HUD heartbeat -> stop
CAST_TIMEOUT = 40.0  # s: longest fishing channel plus margin
PRESS_TIMEOUT = 4.0  # s for the game to confirm a press
STOP_AFTER_RUN = 3  # escapes / false hooks in a row that stop the run

PHASE_REASONS = {
    'idle': 'Готов к забросу',
    'wait_cast': 'Жду начала рыбалки',
    'wait_mark': 'Жду выбранной отметки',
    'wait_bite': 'Жду поклёвки по изображению',
    'wait_loot': 'Жду подтверждения добычи этого заброса',
    'await_recast': 'Добыча подтверждена; жду автоматический перезаброс',
    'cooldown': 'Пауза перед следующим забросом',
}


@dataclass
class CycleStats:
    casts: int = 0  # casts started by our cast key (or adopted auto-recasts)
    signals: int = 0  # interact presses on a bite signal / mark
    catches: int = 0  # catches confirmed by the addon for that cast
    false_signals: int = 0  # press before a bite (game: not hooked / no catch)
    escaped: int = 0  # game: fish escaped after our press
    no_signal: int = 0  # cast ended with no signal (missed or undecided)
    events: list = field(default_factory=list)


class FishingController:
    def __init__(self, mark=65, reserve=0, mode='mark', auto_recast=False, max_no_signal=2):
        if not math.isfinite(mark) or not 1 <= mark <= 95:
            raise ValueError('Отметка: 1–95%')
        if type(reserve) is not int or not 0 <= reserve <= 100:
            raise ValueError('Запас ячеек: 0–100')
        if mode not in ('mark', 'vision'):
            raise ValueError('Неизвестный режим рыбалки')
        self.mark, self.reserve = mark / 100, reserve
        self.mode, self.auto_recast = mode, auto_recast
        self.max_no_signal = max_no_signal
        self.phase = 'warmup'
        self.reason = 'Ожидание свежего HUD'
        self.started = self.heartbeat_at = self.idle_at = None
        self.heartbeat = None
        self.heartbeat_changes = 0
        self.cast = self.loot = None
        self.cast_seen_at = None
        self.sent_at = None
        self.error_serial = None
        self.deadline = None
        self.cycles = 0
        self.pending = None
        self.no_signal_run = 0
        self.escaped_run = 0
        self.false_run = 0
        self.rearm = None  # cast whose detector should watch again (after a false hook)
        self.stats = CycleStats()

    @property
    def waiting_for_bite(self):
        return self.phase in ('wait_bite', 'wait_mark')

    def event(self, now, kind, **detail):
        self.stats.events.append(dict(t=round(now, 3), kind=kind, cast=self.cast, **detail))

    def stop(self, reason):
        if self.phase != 'stopped':
            self.event(self.heartbeat_at or self.started or 0, 'stop', reason=reason)
        self.phase, self.reason, self.pending = 'stopped', reason, None

    def _confirmed(self, obs, cast):
        if obs.loot == self.loot:
            return False
        return obs.protocol == 1 or obs.loot_cast == cast % 256

    def _begin_wait(self, obs, now):
        self.cast, self.cast_seen_at = obs.cast, now
        self.phase = 'wait_bite' if self.mode == 'vision' else 'wait_mark'
        self.deadline = now + CAST_TIMEOUT
        self.stats.casts += 1
        self.event(now, 'cast_started', serial=obs.cast)

    def _cooldown(self, now, seconds=0.8):
        self.phase, self.idle_at, self.deadline = 'cooldown', None, now + seconds

    # -- per-frame step ----------------------------------------------------------
    def step(self, obs, now, bite=None):
        """Feed one HUD observation; returns the pending action ('cast' / 'interact') or None."""
        if self.phase == 'stopped':
            return None
        if self.started is None:
            self.started = now
        if not self._fresh(obs, now) or not self._game_errors(obs, now) or not self._guards(obs):
            return None
        if self.phase == 'warmup':
            if self.heartbeat_changes < 3:
                return None
            if obs.state != 'idle':
                self.stop('Начни с завершённого заброса и закрытого окна добычи')
                return None
            self.phase = 'idle'
        if self.pending:
            return self.pending
        self._advance(obs, now, bite)
        self.reason = PHASE_REASONS.get(self.phase, self.reason)
        return self.pending

    def _fresh(self, obs, now):
        if obs is None or not 0 <= now - obs.captured_at <= FRESH:
            self.idle_at = None
            if now - (self.heartbeat_at if self.heartbeat_at is not None else self.started) > HEARTBEAT_TIMEOUT:
                self.stop('HUD потерян или кадр устарел')
            return False
        if obs.heartbeat != self.heartbeat:
            self.heartbeat = obs.heartbeat
            self.heartbeat_at = now
            self.heartbeat_changes += 1
        elif now - self.heartbeat_at > HEARTBEAT_TIMEOUT:
            self.stop('HUD перестал обновляться')
            return False
        return True

    def _game_errors(self, obs, now):
        """Handle a new game error from the HUD. False: the step ends here."""
        if obs.error:
            self.stop(ERROR_REASONS['latched'])
            return False
        if obs.error_serial is None:
            return True
        if self.error_serial is None:
            self.error_serial = obs.error_serial
            return True
        if obs.error_serial == self.error_serial:
            return True
        self.error_serial = obs.error_serial
        kind = ERROR_CODES.get(obs.error_code, 'other')
        self.event(now, 'game_error', code=kind)
        if kind == 'escaped' and self.waiting_for_bite:
            # Bite missed without a press: the cast ends; counted as "no signal" below.
            return True
        if self.phase == 'wait_loot' and kind == 'escaped':
            # Forever answers our hook with "Рыбка уплыла!" within ~0.1-0.2 s when the catch
            # roll fails (seen at low skill). Counted, then the next cast; three in a row stop.
            self.stats.escaped += 1
            self.escaped_run += 1
            if self.escaped_run >= STOP_AFTER_RUN:
                self.stop('Рыба уплыла после подсечки 3 раза подряд. Проверь навык, место и задержку')
            else:
                self._cooldown(now, 1.0)
            return False
        if self.phase == 'wait_loot' and kind == 'not_hooked':
            # Pressed before the bite ("ещё не клюнуло"): the cast goes on. Watch the same
            # cast again for the real bite; three false hooks in a row stop.
            self.stats.false_signals += 1
            self.false_run += 1
            if self.false_run >= STOP_AFTER_RUN:
                self.stop('3 ложные подсечки подряд. Проверь зону, камеру и разметку')
            else:
                self.phase = 'wait_bite' if self.mode == 'vision' else 'wait_mark'
                self.rearm = self.cast
            return False
        self.stop(ERROR_REASONS[kind])
        return False

    def _guards(self, obs):
        if obs.free is None or obs.state in ('blocked', 'unknown', 'other_cast'):
            self.stop('Нет достоверных данных или ввод запрещён: чат, меню, бой, движение, другой каст')
            return False
        if obs.free <= self.reserve:
            self.stop('Сумки заполнены или достигнут запас свободных ячеек')
            return False
        return True

    def _advance(self, obs, now, bite):
        if self.phase == 'idle':
            if obs.state != 'idle':
                self.stop('Неожиданное изменение состояния перед забросом')
                return
            if self.idle_at is None:
                self.idle_at = now
            if now - self.idle_at >= 0.5:
                self.cast, self.loot = obs.cast, obs.loot
                self.pending = 'cast'
        elif self.phase == 'wait_cast':
            if obs.state == 'fishing' and obs.cast != self.cast:
                self._begin_wait(obs, now)
            elif now > self.deadline:
                self.stop('Заброс не подтверждён за 4 секунды; проверь клавишу')
        elif self.phase == 'await_recast':
            if obs.state == 'fishing' and obs.cast != self.cast:
                self._begin_wait(obs, now)
            elif obs.state == 'idle' and now > self.deadline:
                self.event(now, 'no_auto_recast')
                self.phase, self.idle_at = 'idle', now
            elif obs.state not in ('idle', 'loot'):
                self.stop('Неожиданное состояние после добычи')
        if self.waiting_for_bite:
            self._wait_bite(obs, now, bite)
        elif self.phase == 'wait_loot':
            self._wait_loot(obs, now)
        elif self.phase == 'cooldown' and now >= self.deadline:
            if obs.state != 'idle':
                self.stop('Ожидалось завершение рыбалки')
            else:
                self.phase, self.idle_at = 'idle', now

    def _wait_bite(self, obs, now, bite):
        if obs.state != 'fishing' or obs.cast != self.cast:
            self.stats.no_signal += 1
            self.no_signal_run += 1
            self.event(now, 'ended_without_signal', state=obs.state)
            if self.no_signal_run >= self.max_no_signal:
                self.stop(f'Забросов подряд без сигнала поклёвки: {self.no_signal_run}. Проверь зону и разметку')
            elif obs.state == 'idle':
                self._cooldown(now)
            else:
                self.stop('Рыбалка прервана до сигнала подсечки')
        elif now > self.deadline:
            self.stop('Превышено время ожидания подсечки')
        elif self.mode == 'vision':
            if (
                bite is not None
                and bite.cast == self.cast
                and 0 <= now - bite.captured_at <= FRESH
                and bite.captured_at >= self.cast_seen_at
            ):
                self.loot = obs.loot
                self.pending = 'interact'
        elif obs.progress > self.mark + 0.05:
            self.stop('Отметка пропущена; проверь скорость захвата и настройку')
        elif obs.progress >= self.mark:
            self.loot = obs.loot
            self.pending = 'interact'

    def _wait_loot(self, obs, now):
        if self._confirmed(obs, self.cast):
            self.cycles += 1
            self.stats.catches += 1
            self.no_signal_run = self.escaped_run = self.false_run = 0
            self.event(now, 'catch_confirmed', loot=obs.loot)
            if obs.state == 'fishing' and obs.cast != self.cast:
                if not self.auto_recast:
                    self.stop('Игра сама начала новый заброс. Включи «Эта кнопка сама перезабрасывает после улова»')
                    return
                self._begin_wait(obs, now)
            elif obs.state == 'fishing':
                self.stop('Добыча подтверждена, но заброс продолжается')
            elif self.auto_recast:
                self.phase, self.deadline = 'await_recast', now + 2.5
            else:
                self._cooldown(now)
        elif obs.state == 'fishing' and obs.cast != self.cast and not self.auto_recast:
            self.stop('Неожиданный новый заброс до подтверждения добычи')
        elif obs.state == 'fishing' and obs.cast == self.cast and now > self.sent_at + 2:
            self.stop('Нажатие не завершило рыбалку; проверь клавишу взаимодействия')
        elif now > self.deadline:
            self.stats.false_signals += 1
            self.event(now, 'catch_missing')
            self.stop('Добыча не подтверждена. Проверь взаимодействие и автосбор')

    def sent(self, action, now):
        if action != self.pending or self.phase == 'stopped':
            raise ValueError('Непредусмотренное нажатие')
        self.pending = None
        self.sent_at = now
        self.event(now, 'sent', action=action)
        if action == 'interact':
            self.stats.signals += 1
        self.phase = 'wait_cast' if action == 'cast' else 'wait_loot'
        self.deadline = now + PRESS_TIMEOUT
