"""Windows Teams calendar and meeting automation.

Only the Teams window owned by this process is inspected. The calendar is an
Outlook iframe, so its events are read through Windows UI Automation rather
than JavaScript in the top-level Teams page. All actions fail closed when a
control or its state cannot be identified unambiguously.
"""

import datetime as dt
import re
import threading
import time


MONTHS_RU = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}
MONTHS_EN = {name.lower(): number for number, name in enumerate((
    "January", "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December"), 1)}
EVENT_TIME = re.compile(r",\s*(\d{1,2}:\d{2})\s*(?:[—–-]|to)\s*(\d{1,2}:\d{2})\s*,", re.I)
EVENT_DATE = re.compile(r"(\d{1,2})\s+([а-яё]+),?\s+(\d{4})", re.I)
EVENT_DATE_EN = re.compile(r"\b([a-z]+)\s+(\d{1,2}),?\s+(\d{4})", re.I)
FAREWELL = re.compile(
    r"\b(?:до\s+свидания|до\s+встречи|всем\s+пока|всем\s+спасибо|"
    r"на\s+сегодня\s+вс[её]|хорошего\s+дня|занятие\s+окончено|"
    r"goodbye|see\s+you|thanks\s+everyone|class\s+is\s+over)\b", re.I)
DEVICE_ON_ACTION = re.compile(r"^(?:выключить|отключить|mute\b|turn\b.*\boff\b)", re.I)
DEVICE_OFF_ACTION = re.compile(r"^(?:включить|unmute\b|turn\b.*\bon\b)", re.I)
DEVICE_ON_STATE = re.compile(r"\b(?:включ[её]н|on)\b", re.I)
DEVICE_OFF_STATE = re.compile(r"\b(?:выключ[её]н|off|muted)\b", re.I)
LEAVE_CONTROL = re.compile(r"^(?:leave(?: meeting)?|покинуть(?: собрание)?|выйти)(?:\s*\(|$)", re.I)


def parse_calendar_event(name):
    """Parse a Teams meeting event button from the Russian Outlook calendar."""
    if "Microsoft Teams Meeting" not in name:
        return None
    match = EVENT_TIME.search(name)
    if not match:
        return None
    remainder = name[match.end():]
    date = EVENT_DATE.search(remainder)
    if date:
        month = MONTHS_RU.get(date.group(2).lower())
        day_number, year = date.group(1), date.group(3)
    else:
        date = EVENT_DATE_EN.search(remainder)
        if not date:
            return None
        month = MONTHS_EN.get(date.group(1).lower())
        day_number, year = date.group(2), date.group(3)
    if not month:
        return None
    try:
        day = dt.date(int(year), month, int(day_number))
        start = dt.datetime.combine(day, dt.datetime.strptime(match.group(1), "%H:%M").time())
        end = dt.datetime.combine(day, dt.datetime.strptime(match.group(2), "%H:%M").time())
    except ValueError:
        return None
    if end <= start:
        end += dt.timedelta(days=1)
    title = name[:match.start()].strip(" ,")
    if not title:
        return None
    return {"key": f"{start.isoformat()}|{title}", "title": title,
            "start": start, "end": end, "button_name": name}


def match_lesson(lesson, events, max_start_delta=dt.timedelta(minutes=10)):
    """Use top-academy time as authority; accept only one matching Teams invite."""
    candidates = [(event, button) for event, button in events
                  if abs(event["start"] - lesson["start"]) <= max_start_delta
                  and event["end"] > lesson["start"]]
    return candidates[0] if len(candidates) == 1 else None


def should_leave(now, end, farewell_at, participant_count):
    """Leave only after all three requested signals have been observed."""
    return now >= end and farewell_at is not None and participant_count == 1


class TeamsControls:
    """UI Automation adapter scoped to our own embedded Teams window."""

    def __init__(self, browser):
        self.browser = browser
        import uiautomation as auto
        auto.InitializeUIAutomationInCurrentThread()
        self.auto = auto
        from uiautomation import uiautomation as U
        self.ui = U._AutomationClient.instance().IUIAutomation
        self.button_condition = self.ui.CreatePropertyCondition(
            auto.PropertyId.ControlTypeProperty, auto.ControlType.ButtonControl)

    def buttons(self):
        handle = self.browser.native_handle()
        if not handle:
            return []
        root = self.auto.ControlFromHandle(handle)
        if root is None:
            return []
        elements = root.Element.FindAll(4, self.button_condition)
        return [self.auto.Control.CreateControlFromElement(elements.GetElement(i))
                for i in range(min(elements.Length, 1000))]

    @staticmethod
    def _name(button):
        return button.Name or ""

    def find(self, predicate, buttons=None):
        return [b for b in (buttons if buttons is not None else self.buttons()) if predicate(self._name(b), b)]

    def one(self, predicate, buttons=None):
        found = self.find(predicate, buttons)
        return found[0] if len(found) == 1 else None

    def invoke(self, button):
        if button is None:
            return False
        scroll = button.GetPattern(self.auto.PatternId.ScrollItemPattern)
        if scroll:
            try:
                scroll.ScrollIntoView()
            except Exception:
                pass
        invoke = button.GetPattern(self.auto.PatternId.InvokePattern)
        if invoke:
            invoke.Invoke()
        else:
            if button.IsOffscreen:
                return False
            button.Click(waitTime=0)
        return True

    def events(self):
        return [(event, button) for button in self.buttons()
                if (event := parse_calendar_event(self._name(button))) is not None]

    def in_call(self, buttons=None):
        return bool(self.find(lambda n, b: (b.AutomationId or "") == "hangup-button" or
                              LEAVE_CONTROL.match(n.strip()), buttons))

    def device_state(self, kind, buttons=None):
        ids = {"mic": ("microphone-button", "mic-button"),
               "camera": ("camera-button", "video-button")}[kind]
        words = ("микрофон", "microphone", "mic") if kind == "mic" else ("камера", "camera", "video")
        control = self.one(lambda n, b: (b.AutomationId or "") in ids or
                           any(w in n.lower() for w in words), buttons)
        if control is None:
            return None, None
        name = self._name(control)
        if DEVICE_OFF_ACTION.search(name):
            return False, control
        if DEVICE_ON_ACTION.search(name):
            return True, control
        if name.lower().startswith("turn "):
            return None, control
        if DEVICE_OFF_STATE.search(name) and not DEVICE_ON_STATE.search(name):
            return False, control
        if DEVICE_ON_STATE.search(name) and not DEVICE_OFF_STATE.search(name):
            return True, control
        toggle = control.GetPattern(self.auto.PatternId.TogglePattern)
        if toggle:
            state = int(toggle.ToggleState)
            if state in (0, 1):
                return bool(state), control
        return None, control

    def participant_count(self, buttons=None):
        for button in buttons if buttons is not None else self.buttons():
            name = self._name(button)
            if re.search(r"(?:участник|participants?|people)", name, re.I):
                count = (re.search(r"(?:участник(?:и|ов)?|participants?|people)\D{0,8}(\d+)", name, re.I)
                         or re.search(r"(\d+)\s*(?:участник(?:а|ов)?|participants?|people)", name, re.I))
                if count:
                    return int(count.group(1))
        return None


class TeamsMeetingAutomation(threading.Thread):
    """Join a calendar event at its start; leave after its end and a finish signal."""

    def __init__(self, browser, settings, get_lessons, log, interval=15):
        super().__init__(daemon=True, name="TeamsMeetingAutomation")
        self.browser, self.settings, self.get_lessons, self.log = browser, settings, get_lessons, log
        self.interval = interval
        self._stop_event = threading.Event()
        self._attempted = set()
        self._missing_logged = set()
        self._active = None
        self._last_speech = None
        self._farewell_at = None
        self._controls = None
        self._opened = False
        self._lessons = []
        self._loaded_day = None
        self._loaded_at = 0.0
        self._calendar_refresh_at = 0.0

    def stop(self):
        self._stop_event.set()

    def on_speech(self, text, source):
        if source == "mic" or not text:
            return
        now = time.monotonic()
        self._last_speech = now
        if (self._active and dt.datetime.now() >= self._active["end"] - dt.timedelta(minutes=15)
                and FAREWELL.search(text)):
            self._farewell_at = now

    def _today_lessons(self, now):
        if self._loaded_day != now.date() or time.monotonic() - self._loaded_at >= 300:
            if self._loaded_day != now.date():
                self._lessons = []
            self._loaded_at = time.monotonic()
            self._loaded_day = now.date()
            try:
                self._lessons = self.get_lessons(now.date()) or []
            except Exception:
                self._loaded_at -= 240  # retry after a minute
                raise
        return self._lessons

    def _ensure_calendar(self):
        controls = self._controls
        if any(event["start"].date() == dt.date.today() for event, _ in controls.events()):
            return
        if time.monotonic() - self._calendar_refresh_at < 60:
            return
        self._calendar_refresh_at = time.monotonic()
        tab = controls.one(lambda n, b: n.startswith("Календарь (Ctrl+") or n.startswith("Calendar (Ctrl+"))
        if tab:
            controls.invoke(tab)
            self._stop_event.wait(1)
        today = controls.one(lambda n, b: n.startswith("Перейти к текущей дате") or
                             n.startswith("Go to today") or n in ("Сегодня", "Today"))
        if today:
            controls.invoke(today)

    def _safe_device_off(self, kind):
        state, button = self._controls.device_state(kind)
        if state is None:
            raise RuntimeError(f"не удалось определить состояние {kind} перед входом")
        if state:
            self._controls.invoke(button)
            self._stop_event.wait(0.4)
            state, _ = self._controls.device_state(kind)
        if state is not False:
            raise RuntimeError(f"{kind} не подтверждён выключенным — вход отменён")

    def _join(self, event, button):
        controls = self._controls
        if not controls.invoke(button):
            raise RuntimeError("не удалось открыть событие календаря")
        self._stop_event.wait(0.6)
        popup = controls.one(lambda n, b: n.strip().lower() in ("присоединиться", "join")
                             and (b.AutomationId or "").endswith("primaryActionButton"))
        if popup is None:
            popup = controls.one(lambda n, b: n.strip().lower() in ("присоединиться", "join"))
        if not controls.invoke(popup):
            raise RuntimeError("в карточке события нет однозначной кнопки входа")
        for _ in range(20):
            if self._stop_event.wait(0.5):
                return
            buttons = controls.buttons()
            if controls.in_call(buttons):
                raise RuntimeError("Teams вошёл без экрана проверки устройств")
            mic, _ = controls.device_state("mic", buttons)
            camera, _ = controls.device_state("camera", buttons)
            if mic is not None and camera is not None:
                break
        else:
            raise RuntimeError("экран проверки устройств не появился")
        self._safe_device_off("mic")
        self._safe_device_off("camera")
        join_now = controls.one(lambda n, b: n.strip().lower() in
                                ("присоединиться сейчас", "join now"))
        if not controls.invoke(join_now):
            raise RuntimeError("кнопка окончательного входа не найдена")
        for _ in range(30):
            if self._stop_event.wait(0.5):
                return
            if controls.in_call():
                self._active = event
                self._last_speech = self._farewell_at = None
                self.log(f"[Teams] вошёл: {event['subject']} (микрофон и камера выключены)")
                return
        raise RuntimeError("не удалось подтвердить вход в собрание")

    def _leave(self):
        button = (self._controls.one(lambda n, b: (b.AutomationId or "") == "hangup-button")
                  or self._controls.one(lambda n, b: LEAVE_CONTROL.match(n.strip())))
        if self._controls.invoke(button):
            self.log(f"[Teams] вышел после завершения: {self._active['subject']}")
            self._active = None
            self._farewell_at = self._last_speech = None

    def _tick(self):
        cfg = self.settings()
        if not (cfg.get("teams_auto_join", True) or cfg.get("teams_auto_leave", True)):
            return
        now = dt.datetime.now()
        lessons = self._today_lessons(now)
        relevant = [lesson for lesson in lessons
                    if lesson["start"] - dt.timedelta(minutes=10) <= now
                    <= lesson["end"] + dt.timedelta(minutes=10)]
        if not relevant and self._active is None:
            return
        if self._opened and not self.browser.is_open():
            self._opened = False
            self._controls = None
            self._active = None
            self._farewell_at = self._last_speech = None
        if not self._opened:
            self.browser.open()
            self.browser.install_meeting_popup_handler()
            self._opened = True
            self._controls = TeamsControls(self.browser)
            self._stop_event.wait(2)
        controls = self._controls
        buttons = controls.buttons()
        in_call = controls.in_call(buttons)
        if self._active:
            if not in_call:
                self.log("[Teams] собрание завершилось или выход выполнен вручную")
                self._active = None
                self._farewell_at = self._last_speech = None
                return
            if cfg.get("teams_auto_leave", True) and should_leave(
                    now, self._active["end"], self._farewell_at,
                    controls.participant_count(buttons)):
                self._leave()
            return
        if in_call:
            candidates = [lesson for lesson in relevant
                          if lesson["start"] <= now <= lesson["end"]]
            if len(candidates) == 1:
                self._active = candidates[0]
                self.log(f"[Teams] отслеживаю вручную открытое собрание: {self._active['subject']}")
            return
        if not cfg.get("teams_auto_join", True):
            return
        due = [lesson for lesson in lessons
               if lesson["start"] <= now < lesson["start"] + dt.timedelta(minutes=5)
               and self._lesson_key(lesson) not in self._attempted]
        if not due:
            self._ensure_calendar()
            return
        if len(due) != 1:
            self.log("[Teams] несколько пар начинаются одновременно — автовход пропущен")
            self._attempted.update(self._lesson_key(lesson) for lesson in due)
            return
        lesson = due[0]
        matched = match_lesson(lesson, controls.events())
        if matched is None:
            key = self._lesson_key(lesson)
            if key not in self._missing_logged:
                self.log(f"[Teams] ищу приглашение к паре {lesson['start']:%H:%M} «{lesson['subject']}»")
                self._missing_logged.add(key)
            self._ensure_calendar()
            return
        self._attempted.add(self._lesson_key(lesson))
        _, button = matched
        self._join(lesson, button)

    @staticmethod
    def _lesson_key(lesson):
        return f"{lesson['start'].isoformat()}|{lesson['subject']}"

    def run(self):
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as exc:
                self.log(f"[Teams] автоматизация: {exc}")
            self._stop_event.wait(self.interval)
