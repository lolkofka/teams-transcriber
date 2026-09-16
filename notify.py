"""
Lesson-start notifications from the journal schedule: at the start of every lesson (and optionally N minutes before)
show a Windows toast, play the chime on the alert device, and tell the page.

    n = LessonNotifier(get_lessons, on_event, play=chime_fn, lead_min=5); n.start()
get_lessons() -> list of dicts with datetime "start"/"end" and "subject" (journal.get_schedule(today)).
"""

import datetime as dt
import threading
import time

from paths import BASE_DIR
import os

ICON = os.path.join(BASE_DIR, "ui", "icon.ico")


def toast(title, text):
    try:
        from winotify import Notification, audio
        n = Notification(app_id="Teams transcriber", title=title, msg=text, icon=ICON if os.path.exists(ICON) else "", duration="long")
        n.set_audio(audio.Default, loop=False)
        n.show()
        return True
    except Exception:
        return False


class LessonNotifier(threading.Thread):
    def __init__(self, get_lessons, on_event=None, play=None, lead_min=0, enabled=True, interval=15):
        super().__init__(daemon=True)
        self.get_lessons = get_lessons
        self.on_event = on_event
        self.play = play
        self.lead_min = int(lead_min or 0)
        self.enabled = enabled
        self.interval = interval
        self._stop = threading.Event()
        self._fired = set()          # (date, start, kind)
        self._lessons = []
        self._loaded_day = None
        self._loaded_at = 0.0
        self.error = None

    def stop(self):
        self._stop.set()

    def _refresh(self):
        today = dt.date.today()
        if self._loaded_day == today and time.time() - self._loaded_at < 300:
            return
        try:
            self._lessons = self.get_lessons(today) or []
            self._loaded_day, self._loaded_at, self.error = today, time.time(), None
        except Exception as e:
            self.error = str(e)
            self._loaded_at = time.time() - 240  # retry in a minute

    def _fire(self, lesson, kind, minutes):
        key = (lesson["start"].date().isoformat(), lesson["start"].strftime("%H:%M"), kind)
        if key in self._fired:
            return
        self._fired.add(key)
        subj = lesson["subject"]
        when = lesson["start"].strftime("%H:%M")
        if kind == "start":
            title, text = "Пара началась", f"{when}  {subj}"
        elif kind == "end":
            title, text = "Пара закончилась", f"{subj}"
        else:
            title, text = f"Пара через {minutes} мин", f"{when}  {subj}"
        if kind != "end":
            toast(title, text)
            if self.play:
                try:
                    self.play()
                except Exception:
                    pass
        if self.on_event:
            self.on_event({"kind": "lesson_start", "phase": kind, "subject": subj, "start": when,
                           "end": lesson["end"].strftime("%H:%M"), "minutes": minutes, "teacher": lesson.get("teacher", "")})

    def run(self):
        # do not fire for lessons that started before the program was launched
        boot = dt.datetime.now()
        while not self._stop.is_set():
            if self.enabled:
                self._refresh()
                now = dt.datetime.now()
                for l in self._lessons:
                    st = l["start"]
                    if st < boot - dt.timedelta(seconds=self.interval):
                        continue
                    if self.lead_min > 0:
                        lead = st - dt.timedelta(minutes=self.lead_min)
                        if lead <= now < st and (now - lead).total_seconds() < 120:
                            self._fire(l, "lead", self.lead_min)
                    if st <= now and (now - st).total_seconds() < 120:
                        self._fire(l, "start", 0)
                    en = l["end"]
                    if en <= now and (now - en).total_seconds() < 120:
                        self._fire(l, "end", 0)
            self._stop.wait(self.interval)


if __name__ == "__main__":
    print("toast:", toast("Пара началась", "10:00  Тестовая пара"))
