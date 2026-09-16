"""
Desktop UI for the transcriber: a native window (pywebview on Edge WebView2) showing ui/index.html.

The page talks to Python through window.pywebview.api (class Api below); the pipeline pushes events
(draft words with timestamps, final Whisper utterances, levels, logs) back with window.evaluate_js.

  app.bat            -> opens the window
  python app.py      -> same, with a console for debugging
"""

import datetime as dt
import json
import os
import queue
import sys
import threading
import time
import traceback
import types

from paths import BASE_DIR as HERE, FROZEN  # noqa: E402
os.chdir(HERE)
sys.path.insert(0, HERE)
SETTINGS = os.path.join(HERE, "gui_settings.json")
LOG = os.path.join(HERE, "app.log")

import webview  # noqa: E402
import transcribe as T  # noqa: E402
import vcable  # noqa: E402
from alerts import NameAlert  # noqa: E402
from analyst import LessonAnalyst  # noqa: E402
from slides import SlideCapture  # noqa: E402
import report  # noqa: E402
from notify import LessonNotifier  # noqa: E402
import vcam  # noqa: E402

MODELS = ["large-v3-turbo", "medium", "small"]


def _thumb_data_url(path, max_w=1100):
    """JPEG data URL of a slide for the page (the page is served over pywebview's http server, so file:// is blocked)."""
    try:
        import base64, io
        from PIL import Image
        with Image.open(path) as im:
            im = im.convert("RGB")
            if im.width > max_w:
                im = im.resize((max_w, int(im.height * max_w / im.width)))
            buf = io.BytesIO(); im.save(buf, "JPEG", quality=80, optimize=True)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None


def log_exc():
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(dt.datetime.now().isoformat() + "\n" + traceback.format_exc() + "\n")


class Sink:
    """Pipeline -> UI bridge. Everything lands in a queue; a dispatcher thread forwards it to the page.
    Also feeds the name alert (drafts + finals) and the lesson analyst (finals)."""

    def __init__(self):
        self.q = queue.Queue()
        self.alert = None      # NameAlert
        self.analyst = None    # LessonAnalyst
        self.quick = False     # answer hints on a name ping
        self.quick_model = ""  # optional faster model for hints
        self.quick_words = 200
        self._recent = []      # rolling draft text (Vosk, instant): [(t, text)]
        self._last_quick = 0.0

    def _recent_text(self, extra=""):
        words = []
        for _, t in self._recent:
            words.extend(t.split())
        if extra:
            words.extend(extra.split())
        return " ".join(words[-self.quick_words:])

    def event(self, kind, payload):
        d = dict(payload); d["kind"] = kind
        try:
            if kind == "draft" and d.get("source", "teams") != "mic" and d.get("text"):
                self._recent.append((time.time(), d["text"]))
                self._recent = self._recent[-60:]
            if kind in ("draft", "final", "partial") and self.alert is not None:
                hit = self.alert.check(d.get("text", ""), d.get("source", "teams"))
                if hit:
                    d["ping"] = hit
                    self._maybe_quick(hit, d.get("text", "") if kind == "partial" else "")
            if kind == "final" and self.analyst is not None and d.get("text"):
                self.analyst.add(d["text"], d.get("speaker", ""))
        except Exception:
            log_exc()
        self.q.put(d)

    def _maybe_quick(self, word, partial_text):
        if not self.quick or self.analyst is None:
            return
        now = time.time()
        if now - self._last_quick < 20:
            return
        self._last_quick = now
        ctx = self._recent_text(partial_text)
        if len(ctx.split()) < 5:
            return
        self.q.put({"kind": "quick", "word": word, "pending": True})
        self.analyst.quick_answer(ctx, word, lambda res: self.q.put(dict(res, kind="quick")), model=self.quick_model or None)

    def final(self, line, dim=False):
        self.q.put({"kind": "log", "text": line, "dim": dim})

    def partial(self, text):
        self.q.put({"kind": "partial", "text": text, "t": time.time()})

    def draft_done(self, text):
        self.q.put({"kind": "draft", "text": text, "words": [], "t0": time.time(), "t1": time.time()})


class Api:
    """Only public methods are exposed to the page. Everything else is underscored: pywebview walks every public
    attribute recursively to build the JS bridge, and walking the Window/queues/threads deadlocks the UI."""

    def __init__(self):
        self._window = None
        self._sink = Sink()
        T.CON.sink = self._sink
        self._pipe = None
        self._inputs, self._outputs = [], []
        self._stop_flag = threading.Event()
        self._slides = None
        self._last_folder = None
        self._cfg = {}
        self._notifier = None
        self._vcam = vcam.VirtualCam(on_log=lambda m: self._sink.q.put({"kind": "log", "text": m, "dim": False}),
                                     on_state=lambda st: self._sink.q.put(dict(st, kind="vcam")))

    # ---------------------------------------------------------------- lifecycle
    def attach(self, window):
        self._window = window
        threading.Thread(target=self._dispatch, daemon=True).start()

    def _push(self, obj):
        if self._window is None:
            return
        try:
            self._window.evaluate_js(f"window.__evt({json.dumps(obj, ensure_ascii=False)})")
        except Exception:
            pass

    def _dispatch(self):
        """The only bridge from Python to the page. Must survive anything: a dead dispatcher = frozen page
        while recording continues (that was the 'no live text after a restart' bug: self._pipe went None mid-iteration)."""
        last_level = -1.0
        last_mic = -1.0
        errors = 0
        while True:
            try:
                try:
                    obj = self._sink.q.get(timeout=0.1)
                    self._push(obj)
                    continue
                except queue.Empty:
                    pass
                pipe = self._pipe
                if pipe is None or pipe.rec is None:
                    continue
                lvl = float(getattr(pipe.rec, "level", 0.0))
                mic = getattr(pipe, "mic_rec", None)
                mlvl = float(getattr(mic, "level", 0.0)) if mic is not None else 0.0
                asr = pipe.asr
                backlog = asr.in_q.qsize() if asr else 0
                if abs(lvl - last_level) > 0.002 or abs(mlvl - last_mic) > 0.002:
                    self._push({"kind": "level", "v": lvl, "mic": mlvl, "backlog": backlog})
                    last_level, last_mic = lvl, mlvl
                if asr is not None and not asr.is_alive() and not self._stop_flag.is_set() and self._pipe is pipe:
                    self._push({"kind": "log", "text": "[error] конвейер остановился, см. app.log", "dim": False})
                    self._stop_flag.set()
            except Exception:
                errors += 1
                if errors <= 3:
                    log_exc()
                time.sleep(0.2)

    # ---------------------------------------------------------------- devices / settings
    def devices(self):
        try:
            self._inputs = T.list_input_devices()
            self._outputs = T.list_output_devices()
            cap, ren = vcable.find(self._inputs, self._outputs)
            return {"inputs": self._inputs, "outputs": self._outputs,
                    "cable": {"capture": cap, "render": ren} if cap and ren else None}
        except Exception as e:
            log_exc()
            return {"inputs": [], "outputs": [], "cable": None, "error": str(e)}

    def settings(self):
        try:
            with open(SETTINGS, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def save_settings(self, s):
        """Merge into the stored settings: the page only knows its form fields, keys it doesn't carry
        (vcam_file, ...) must survive an autosave."""
        try:
            cur = self.settings()
            cur.update(s or {})
            with open(SETTINGS, "w", encoding="utf-8") as f:
                json.dump(cur, f, ensure_ascii=False, indent=2)
        except Exception:
            log_exc()
        return True

    def journal_today(self):
        """Today's lessons for the side panel (needs journal.json)."""
        try:
            import journal
            now = dt.datetime.now()
            lessons = journal.get_schedule(now.date())
            cur, why = journal.pick_lesson(lessons, now)
            try:
                visits = journal.get_visits(now.date())
            except Exception as e:
                visits = {}
                self._sink.q.put({"kind": "log", "text": f"[journal] посещения недоступны: {e}", "dim": True})
            return {"ok": True, "why": why, "current": cur["subject"] if cur else None,
                    "lessons": [{"subject": l["subject"], "teacher": l["teacher"], "room": l["room"],
                                 "start": l["start"].strftime("%H:%M"), "end": l["end"].strftime("%H:%M"),
                                 "current": cur is l, "visit": visits.get(l.get("number"))} for l in lessons]}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def journal_creds(self):
        """Username + whether a password is stored (the password itself is never sent back to the page)."""
        try:
            import journal
            with open(journal.CONFIG, encoding="utf-8") as f:
                c = json.load(f)
            return {"username": c.get("username", ""), "has_password": bool(c.get("password")), "host": c.get("host", "")}
        except Exception:
            return {"username": "", "has_password": False, "host": ""}

    def save_journal(self, username, password):
        """Write journal.json (empty password keeps the stored one), drop the cached token, test the login."""
        try:
            import journal
            cfg = {}
            try:
                with open(journal.CONFIG, encoding="utf-8") as f:
                    cfg = json.load(f)
            except Exception:
                pass
            cfg.setdefault("host", "msapi.top-academy.ru")
            cfg.setdefault("origin", "https://journal.top-academy.ru")
            cfg["username"] = (username or "").strip()
            if password:
                cfg["password"] = password
            with open(journal.CONFIG, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            try:
                os.remove(journal.TOKEN_CACHE)
            except OSError:
                pass
            lessons = journal.get_schedule(dt.date.today())
            return {"ok": True, "lessons": len(lessons)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def open_recordings(self):
        os.makedirs(os.path.join(HERE, "recordings"), exist_ok=True)
        os.startfile(os.path.join(HERE, "recordings"))
        return True

    # ---------------------------------------------------------------- alerts + analyst
    def apply_extras(self, cfg):
        """(Re)configure the name alert and the lesson analyst from the settings dict. Safe to call anytime."""
        names = [n for n in (cfg.get("alert_names") or "").replace(";", ",").split(",") if n.strip()]
        dev = cfg.get("alert_device")
        dev = None if dev in (None, "", "none", "default") else int(dev)
        if self._sink.alert is None:
            self._sink.alert = NameAlert(names, dev, on_hit=lambda w, t: self._sink.q.put({"kind": "ping", "word": w, "text": t}))
        else:
            self._sink.alert.set_names(names); self._sink.alert.device_index = dev
        self._sink.alert.enabled = bool(cfg.get("alert_enabled", True)) and bool(names)
        # lesson-start notifications (independent of recording)
        if self._notifier is None:
            import journal
            self._notifier = LessonNotifier(journal.get_schedule, on_event=self._on_lesson_event,
                                            play=lambda: self._sink.alert.play() if self._sink.alert else None,
                                            lead_min=int(cfg.get("notify_lead") or 0), enabled=bool(cfg.get("notify_enabled", True)))
            self._notifier.start()
        else:
            self._notifier.lead_min = int(cfg.get("notify_lead") or 0)
            self._notifier.enabled = bool(cfg.get("notify_enabled", True))
        self._sink.quick = bool(cfg.get("quick_enabled", True))
        self._sink.quick_model = (cfg.get("quick_model") or "").strip()
        self._sink.quick_words = max(60, int(cfg.get("quick_words") or 200))

        want = bool(cfg.get("llm_enabled")) and bool(cfg.get("llm_key"))
        an = self._sink.analyst
        if an is not None and (not want or an.base_url != (cfg.get("llm_base") or "https://api.deepseek.com").rstrip("/")
                               or an.api_key != cfg.get("llm_key") or an.model != (cfg.get("llm_model") or "deepseek-chat")):
            an.stop(); self._sink.analyst = an = None
        if want and an is None:
            an = LessonAnalyst(cfg.get("llm_base"), cfg.get("llm_key"), cfg.get("llm_model") or "deepseek-chat",
                               int(cfg.get("llm_interval") or 180),
                               on_result=lambda st: self._sink.q.put(dict(st, kind="analysis")),
                               on_log=lambda m: self._sink.q.put({"kind": "log", "text": m, "dim": False}))
            an.start(); self._sink.analyst = an
            if self._pipe is not None and self._pipe.txt_path:   # enabled mid-lesson: feed what is already recorded
                try:
                    n = self._load_history(self._pipe, push=False, feed=True)
                    self._sink.q.put({"kind": "log", "text": f"[analyst] в контекст загружено реплик: {n}", "dim": False})
                except Exception:
                    log_exc()
        elif an is not None:
            an.interval = max(45, int(cfg.get("llm_interval") or 180))
        return {"alert": self._sink.alert.enabled, "analyst": self._sink.analyst is not None}

    def _on_lesson_event(self, e):
        """Lesson started/ended (from the notifier): auto start / restart / stop recording per settings."""
        self._push(e)
        cfg = self.settings()
        phase = e.get("phase")
        if phase == "start" and cfg.get("autostart", True):
            threading.Thread(target=self._auto_start, args=(cfg,), daemon=True).start()
        elif phase == "end" and cfg.get("autostop", False) and self._pipe is not None:
            self._push({"kind": "log", "text": "[auto] пара закончилась, останавливаю запись", "dim": False})
            self.stop()

    def _auto_start(self, cfg):
        try:
            if not cfg.get("input") or cfg.get("input") == "none":
                self._push({"kind": "log", "text": "[auto] автозапуск: не выбран вход, пропускаю", "dim": False}); return
            if self._pipe is not None:
                # already recording (previous lesson): restart so the new lesson gets its own folder
                self._push({"kind": "log", "text": "[auto] новая пара: перезапускаю запись в её папку", "dim": False})
                pipe, self._pipe = self._pipe, None
                self._stop_flag.set()
                if self._slides is not None:
                    self._slides.stop(); self._slides = None
                pipe.stop(timeout=180)
                self._push({"kind": "stopped"})
            self._push({"kind": "log", "text": "[auto] пара началась, запускаю запись", "dim": False})
            self._push({"kind": "autostart"})
            r = self.start(cfg)
            if not r.get("ok"):
                self._push({"kind": "log", "text": "[auto] автозапуск не удался: " + str(r.get("error")), "dim": False})
        except Exception as e:
            log_exc(); self._push({"kind": "log", "text": f"[auto] ошибка автозапуска: {e}", "dim": False})

    def analyze_now(self):
        an = self._sink.analyst
        if an is None:
            return {"ok": False, "error": "анализ выключен или не задан ключ"}
        an.analyze_now()
        return {"ok": True}

    def analysis_state(self):
        an = self._sink.analyst
        return an.state if an else None

    def test_alert(self):
        if self._sink.alert is None:
            self._sink.alert = NameAlert([], None)
        self._sink.alert.play()
        return True

    # ---------------------------------------------------------------- run control
    def start(self, cfg):
        if self._pipe is not None:
            return {"ok": False, "error": "уже запущено"}
        try:
            self.apply_extras(cfg)
            args = types.SimpleNamespace(
                list_devices=False, device=int(cfg["input"]), file=None, process=None,
                model=cfg.get("model", MODELS[0]), language=cfg.get("language") or "ru", compute_type="int8",
                threads=max(1, (os.cpu_count() or 8) // 2), beam=5, out="recordings",
                no_diar=not cfg.get("diar", True), spk_threshold=0.45, min_silence=700, max_utt=25.0,
                no_save_audio=False, initial_prompt=None, vosk_model=None, no_vosk=not cfg.get("vosk", True),
                journal=bool(cfg.get("journal", True)),
                monitor=int(cfg["output"]) if cfg.get("output") not in (None, "", "none") else None,
                monitor_volume=float(cfg.get("volume", 100)) / 100.0,
                mic=int(cfg["mic"]) if cfg.get("mic") not in (None, "", "none") else None,
                mic_mode=cfg.get("mic_mode", "off"),
                proc_mode=cfg.get("proc_mode", "local"), proc_url=(cfg.get("proc_url") or "").strip(),
                proc_token=(cfg.get("proc_token") or "").strip(),
                proc_model=(cfg.get("proc_model") or "").strip(),
            )
            self._stop_flag.clear()
            self._cfg = dict(cfg)
            self._pipe = T.Pipeline(args)
            threading.Thread(target=self._start_thread, daemon=True).start()
            return {"ok": True}
        except Exception as e:
            log_exc()
            return {"ok": False, "error": str(e)}

    def _load_history(self, pipe, push=True, feed=True):
        """Utterances already in the lesson file (previous runs + this run so far): push to the page and/or to the analyst."""
        path = os.path.splitext(pipe.txt_path)[0] + ".jsonl"
        if not os.path.exists(path):
            return 0
        items = []
        today = dt.date.today()
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if not d.get("text"):
                    continue
                ts = d.get("ts")
                if ts is None and d.get("wall"):
                    try:
                        hh, mm, ss = (int(x) for x in d["wall"].split(":"))
                        ts = dt.datetime.combine(today, dt.time(hh, mm, ss)).timestamp()
                    except Exception:
                        ts = None
                if ts is None:
                    continue
                dur = max(0.5, float(d.get("end", 0)) - float(d.get("start", 0)))
                items.append({"t0": ts, "t1": ts + dur, "speaker": d.get("speaker", ""), "text": d["text"], "source": d.get("source", "teams")})
        if items:
            items.sort(key=lambda x: x["t0"])
            if feed and self._sink.analyst is not None:
                for it in items:
                    self._sink.analyst.add(it["text"], it["speaker"], it["t0"])
            if push:
                self._push({"kind": "history", "items": items})
        if push:
            self._load_slides_history(os.path.dirname(path))
        return len(items)

    def _load_slides_history(self, folder):
        """Slides saved earlier in this lesson folder -> 'slide' events (thumbnails for the last 40)."""
        idx = os.path.join(folder, "slides.jsonl")
        if not os.path.exists(idx):
            return 0
        recs = []
        with open(idx, encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                p = os.path.join(folder, d.get("file", ""))
                if os.path.exists(p) and d.get("t"):
                    recs.append((d, p))
        for i, (d, p) in enumerate(recs):
            self._push({"kind": "slide", "path": p, "file": d.get("file"), "wall": d.get("wall"), "t": d["t"], "n": i + 1,
                        "presenter": d.get("presenter"), "w": d.get("w"), "h": d.get("h"),
                        "data": _thumb_data_url(p, 900) if i >= len(recs) - 40 else None})
        if recs:
            self._push({"kind": "log", "text": f"[history] подгружено слайдов из прошлого запуска: {len(recs)}", "dim": True})
        return len(recs)

    def ask(self, question):
        an = self._sink.analyst
        if an is None:
            return {"ok": False, "error": "анализ выключен или не задан ключ (Настройки → Анализ пары)"}
        q = (question or "").strip()
        if not q:
            return {"ok": False, "error": "пустой вопрос"}
        an.ask(q, lambda res: self._push(dict(res, kind="answer")))
        return {"ok": True}

    def _start_thread(self):
        try:
            n = 0
            try:
                self._pipe.start()
                n = self._load_history(self._pipe)
            except Exception:
                raise
            self._last_folder = os.path.dirname(self._pipe.txt_path)
            self._push({"kind": "started", "file": self._pipe.txt_path, "info": self._pipe.info, "history": n})
            self._start_slides(self._last_folder)
        except Exception as e:
            log_exc()
            self._pipe = None
            self._push({"kind": "stopped", "error": str(e)})

    def _start_slides(self, folder):
        cfg = self._cfg
        if not cfg.get("slides_enabled", True):
            return
        try:
            self._slides = SlideCapture(
                folder, interval=float(cfg.get("slides_interval") or 3), threshold=int(cfg.get("slides_threshold") or 10),
                on_slide=lambda r: self._push({"kind": "slide", "path": r["path"], "file": r["file"], "wall": r["wall"], "t": r["t"],
                                               "n": r["n"], "presenter": r.get("presenter"), "w": r["w"], "h": r["h"],
                                               "data": _thumb_data_url(r["path"])}),
                on_log=lambda m: self._push({"kind": "log", "text": m, "dim": True}),
                on_state=lambda st: self._push(dict(st, kind="sharing")))
            self._slides.start()
        except Exception as e:
            log_exc(); self._push({"kind": "log", "text": f"[slides] не запустился: {e}", "dim": False})

    # ---------------------------------------------------------------- virtual camera
    def vcam_status(self):
        st = self._vcam.status()
        st["available"] = [d for _, d in vcam.available_backends()] if not st["on"] else [st["device"]]
        return st

    def vcam_toggle(self, file=None):
        if self._vcam.running():
            self._vcam.stop()
            return self._vcam.status()
        f = file or self._cfg.get("vcam_file") or self.settings().get("vcam_file")
        if not f:
            return {"ok": False, "error": "сначала выбери видео"}
        self._vcam.start(f)
        return self._vcam.status()

    def vcam_pick(self):
        """Native file dialog -> chosen video path (saved into settings)."""
        try:
            res = self._window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=False,
                                                  file_types=("Видео (*.mp4;*.mkv;*.mov;*.avi;*.webm)", "Все файлы (*.*)"))
        except Exception as e:
            return {"ok": False, "error": str(e)}
        if not res:
            return {"ok": False}
        f = res[0] if isinstance(res, (list, tuple)) else res
        s = self.settings(); s["vcam_file"] = f; self.save_settings(s); self._cfg["vcam_file"] = f
        return {"ok": True, "file": f}

    def proc_test(self, url, token=""):
        """Ping an external processor: GET /health."""
        try:
            import transcribe as _T
            r = _T.RemoteWhisper((url or "").strip(), (token or "").strip()).health()
            return {"ok": True, "model": r.get("model"), "device": r.get("device"), "cuda": r.get("cuda"),
                    "cores": r.get("cores"), "threads": r.get("threads"), "compute_type": r.get("compute_type"),
                    "allowed_models": r.get("allowed_models") or [], "loaded": r.get("models") or [],
                    "allow_model_switch": r.get("allow_model_switch", True), "max_beam": r.get("max_beam")}
        except Exception as e:
            return {"ok": False, "error": str(e)[:160]}

    def cam_devices(self):
        return vcam.list_cameras(self.settings().get("vcam_name") or vcam.DEFAULT_NAME)

    def cam_record(self, index, seconds=30):
        """Record a clip from a physical camera into recordings/camera/ and make it the virtual camera video."""
        if getattr(self, "_camrec", None) is not None and self._camrec.is_alive():
            return {"ok": False, "error": "запись уже идёт"}
        if self._vcam.running():
            self._vcam.stop()  # the same physical camera may be needed; also keeps CPU free
        out = os.path.join(HERE, "recordings", "camera", f"camera_{dt.datetime.now():%Y-%m-%d_%H-%M-%S}.mp4")

        def progress(st):
            self._push(dict(st, kind="camrec"))
            if st.get("state") == "done":
                s = self.settings(); s["vcam_file"] = st["file"]; self.save_settings(s); self._cfg["vcam_file"] = st["file"]
                self._push({"kind": "log", "text": f"[camrec] записано {st['frames']} кадров, {st['fps']} fps -> {st['file']}", "dim": False})
                self._push(dict(self._vcam.status(), kind="vcam", file=st["file"]))
            elif st.get("state") == "error":
                self._push({"kind": "log", "text": "[error] запись с камеры: " + st.get("error", ""), "dim": False})
        self._camrec = vcam.CamRecorder(int(index), int(seconds or 30), out, on_progress=progress)
        self._camrec.start()
        return {"ok": True, "file": out}

    def vcam_install(self, name=None):
        name = (name or self.settings().get("vcam_name") or vcam.DEFAULT_NAME)
        def work():
            try:
                vcam.install_unity_capture(log=lambda m: self._sink.q.put({"kind": "log", "text": "[vcam] " + m, "dim": False}), name=name)
            except Exception as e:
                self._sink.q.put({"kind": "log", "text": f"[error] установка камеры: {e}", "dim": False})
            self._sink.q.put(dict(self.vcam_status(), kind="vcam"))
        threading.Thread(target=work, daemon=True).start()
        return True

    def make_report(self, fmt="docx"):
        """Build the lesson document (docx/pdf/both) for the current or last lesson folder and open it."""
        folder = self._last_folder
        if not folder or not os.path.isdir(folder):
            return {"ok": False, "error": "нет папки пары: сначала запусти запись"}
        try:
            an = self._sink.analyst.state if self._sink.analyst else None
            if an and (an.get("topic") or an.get("task")):
                with open(os.path.join(folder, "analysis.json"), "w", encoding="utf-8") as f:
                    json.dump(an, f, ensure_ascii=False, indent=1)
            out = report.build(folder, fmt)
            first = out[0] if isinstance(out, tuple) else out
            try:
                os.startfile(first)
            except Exception:
                pass
            return {"ok": True, "path": first, "all": list(out) if isinstance(out, tuple) else [out]}
        except Exception as e:
            log_exc()
            return {"ok": False, "error": str(e)}

    def stop(self):
        if self._pipe is None:
            return {"ok": False}
        pipe, self._pipe = self._pipe, None
        self._stop_flag.set()
        if self._slides is not None:
            self._slides.stop(); self._slides = None

        def work():
            pipe.stop(timeout=180)
            self._push({"kind": "stopped"})
        threading.Thread(target=work, daemon=True).start()
        return {"ok": True}

    def teams_status(self):
        w = getattr(self._pipe, "watcher", None) if self._pipe else None
        return w.status() if w else None

    def set_volume(self, v):
        if self._pipe and getattr(self._pipe.rec, "monitor", None):
            self._pipe.rec.monitor.volume = float(v) / 100.0
        return True

    # ---------------------------------------------------------------- virtual device
    def install_cable(self):
        def work():
            try:
                vcable.install(log=lambda m: self._sink.final("[cable] " + m))
            except Exception as e:
                self._sink.final(f"[error] установка кабеля: {e}")
            self._push({"kind": "devices_changed"})
        threading.Thread(target=work, daemon=True).start()
        return True

    def rename_cable(self):
        def work():
            try:
                vcable.rename_endpoints(log=lambda m: self._sink.final("[cable] " + m))
            except Exception as e:
                self._sink.final(f"[error] переименование: {e}")
            self._push({"kind": "devices_changed"})
        threading.Thread(target=work, daemon=True).start()
        return True


APP_TITLE = "Teams transcriber"
ICON = os.path.join(HERE, "ui", "icon.ico")


def _brand_window():
    """Own taskbar identity: without an explicit AppUserModelID Windows maps pythonw.exe windows onto the
    'IDLE (Python ...)' Start-menu shortcut (its icon and name). Also put our icon on the window."""
    import ctypes
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("TeamsTranscriber.App")
    except Exception:
        pass

    def set_icon():
        u = ctypes.windll.user32
        for _ in range(100):
            hwnd = u.FindWindowW(None, APP_TITLE)
            if hwnd:
                break
            time.sleep(0.1)
        else:
            return
        if not os.path.exists(ICON):
            return
        LR_LOADFROMFILE = 0x10
        for size, which in ((16, 0), (48, 1)):  # ICON_SMALL, ICON_BIG
            h = u.LoadImageW(None, ICON, 1, size, size, LR_LOADFROMFILE)
            if h:
                u.SendMessageW(hwnd, 0x0080, which, h)  # WM_SETICON
    threading.Thread(target=set_icon, daemon=True).start()


def _raise_priority():
    """Above-normal process priority: when a game or a build eats the CPU, Windows still schedules us first."""
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.SetPriorityClass.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        ABOVE_NORMAL_PRIORITY_CLASS = 0x8000
        k32.SetPriorityClass(ctypes.c_void_p(-1), ABOVE_NORMAL_PRIORITY_CLASS)  # -1 = current process pseudo-handle
    except Exception:
        pass


def main():
    api = Api()
    _raise_priority()
    _brand_window()
    window = webview.create_window(APP_TITLE, os.path.join(HERE, "ui", "index.html"), js_api=api,
                                   width=1240, height=800, min_size=(900, 600), background_color="#0f1117",
                                   text_select=True)  # allow selecting/copying text; the page limits it to text areas
    api.attach(window)

    def on_closing():
        try:
            api._vcam.stop()
        except Exception:
            pass
        if api._pipe is not None:
            try:
                api._pipe.rec.stop_event.set()
            except Exception:
                pass
    window.events.closing += on_closing
    webview.start(debug="--debug" in sys.argv)


if __name__ == "__main__":
    if "--rename-cable" in sys.argv:          # elevated helper mode (frozen build): rename the virtual cable endpoints
        import runpy
        sys.argv = [sys.argv[0]] + [a for a in sys.argv[1:] if a != "--rename-cable"]
        runpy.run_module("vcable_rename", run_name="__main__")
        sys.exit(0)
    if "--selftest" in sys.argv:              # headless check of the bundled pipeline on a wav (writes selftest.log)
        import types as _t
        wav = next((a for a in sys.argv[1:] if a.lower().endswith(".wav")), os.path.join(HERE, "test_meeting.wav"))
        with open(os.path.join(HERE, "selftest.log"), "w", encoding="utf-8") as f:
            class _S:
                def event(self, k, p): f.write(f"{k}: {json.dumps(p, ensure_ascii=False)[:160]}\n"); f.flush()
                def final(self, line, dim=False): f.write(line + "\n"); f.flush()
                def partial(self, t): pass
                def draft_done(self, t): pass
            T.CON.sink = _S()
            try:
                a = _t.SimpleNamespace(list_devices=False, device=None, file=wav, process=None, model="small", language="ru", compute_type="int8",
                                       threads=max(1, (os.cpu_count() or 8) // 2), beam=5, out=os.path.join(HERE, "recordings"), no_diar=False, spk_threshold=0.45,
                                       min_silence=700, max_utt=25.0, no_save_audio=True, initial_prompt=None, vosk_model=None, no_vosk=False, journal=False)
                p = T.Pipeline(a).start()
                while p.alive():
                    time.sleep(0.5)
                f.write("SELFTEST OK\n")
            except Exception:
                f.write(traceback.format_exc())
        sys.exit(0)
    try:
        main()
    except Exception:
        log_exc()
        raise
