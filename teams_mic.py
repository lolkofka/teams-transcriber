"""
Watches whether the user's microphone is live in a Microsoft Teams call.

Source of truth: the call window's microphone button (UI Automation id "microphone-button").
Its name flips with the state: "Выключить микрофон" / "Mute" = mic is ON, "Включить микрофон" / "Unmute" = mic is OFF.
No button in any Teams window = not in a call.

    w = TeamsMicWatcher(); w.start()
    w.mic_on()  -> bool      w.in_call() -> bool      w.status() -> dict
"""

import re
import threading
import time

ON_WORDS = re.compile(r"^(выключить|отключить|mute)\b", re.I)     # button offers to mute  -> mic currently on
OFF_WORDS = re.compile(r"^(включить|unmute)\b", re.I)               # button offers to unmute -> mic currently off


class TeamsMicWatcher(threading.Thread):
    def __init__(self, interval=0.7, on_change=None):
        super().__init__(daemon=True)
        self.interval = interval
        self.on_change = on_change
        self._in_call = False
        self._mic_on = False
        self._button = None
        self._error = None
        self._stop = threading.Event()
        self.checked_at = 0.0

    def mic_on(self):
        return self._mic_on

    def in_call(self):
        return self._in_call

    def status(self):
        return {"in_call": self._in_call, "mic_on": self._mic_on, "button": self._button, "error": self._error,
                "age": round(time.time() - self.checked_at, 1) if self.checked_at else None}

    def stop(self):
        self._stop.set()

    # ------------------------------------------------------------------
    def _probe(self, ui, cond, auto):
        """Return (in_call, mic_on, button_name)."""
        for w in auto.GetRootControl().GetChildren():
            if w.ClassName != "TeamsWebView":
                continue
            try:
                el = w.Element.FindFirst(auto.TreeScopeDescendants, cond)
            except Exception:
                el = None
            if el:
                name = el.CurrentName or ""
                if ON_WORDS.search(name):
                    return True, True, name
                if OFF_WORDS.search(name):
                    return True, False, name
                return True, False, name  # unknown wording: stay safe, treat as muted
        return False, False, None

    def run(self):
        try:
            import uiautomation as auto
            auto.InitializeUIAutomationInCurrentThread()
            from uiautomation import uiautomation as U
            ui = U._AutomationClient.instance().IUIAutomation
            cond = ui.CreatePropertyCondition(auto.PropertyId.AutomationIdProperty, "microphone-button")
            auto.TreeScopeDescendants = getattr(getattr(U, "TreeScope", None), "Descendants", 4)
        except Exception as e:
            self._error = f"UI Automation unavailable: {e}"
            return
        while not self._stop.is_set():
            try:
                in_call, mic_on, name = self._probe(ui, cond, auto)
                changed = (in_call, mic_on) != (self._in_call, self._mic_on)
                self._in_call, self._mic_on, self._button, self._error = in_call, mic_on, name, None
                self.checked_at = time.time()
                if changed and self.on_change:
                    try:
                        self.on_change(self.status())
                    except Exception:
                        pass
            except Exception as e:
                self._error = str(e)
            self._stop.wait(self.interval)


if __name__ == "__main__":
    w = TeamsMicWatcher(on_change=lambda s: print("changed:", s))
    w.start()
    time.sleep(1.5)
    print(w.status())
    time.sleep(2)
