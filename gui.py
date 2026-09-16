"""
Lightweight Tk front-end for transcribe.py: pick where the audio comes from, where to play it on,
start/stop, watch the live draft and the final transcript.

Chain for Teams:  Teams -> [virtual device: Sonar Aux / VB-Cable] -> this program -> [any output you choose] -> headphones
"""

import json
import os
import queue
import sys
import threading
import traceback
import types

import tkinter as tk
from tkinter import ttk, scrolledtext

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)
SETTINGS = os.path.join(HERE, "gui_settings.json")
LOG = os.path.join(HERE, "gui.log")

import transcribe as T  # noqa: E402
import vcable  # noqa: E402

NO_OUTPUT = "— не играть —"
MODELS = ["large-v3-turbo", "medium", "small"]


class Sink:
    """Receives console events from the pipeline threads; the GUI drains the queue on the Tk thread."""

    def __init__(self):
        self.q = queue.Queue()

    def final(self, line, dim=False):
        self.q.put(("final", line, dim))

    def partial(self, text):
        self.q.put(("partial", text, False))

    def draft_done(self, text):
        self.q.put(("draft", text, False))


class App:
    def __init__(self, root):
        self.root = root
        root.title("Teams transcriber")
        root.minsize(760, 520)
        self.pipe = None
        self.sink = Sink()
        T.CON.sink = self.sink
        self.inputs, self.outputs = [], []
        self.settings = self._load_settings()
        self._build()
        self.refresh_devices()
        self._apply_settings()
        self.root.after(100, self._poll)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ------------------------------------------------------------------ UI
    def _build(self):
        pad = {"padx": 6, "pady": 3}
        f = ttk.Frame(self.root); f.pack(fill="x", **pad)

        ttk.Label(f, text="Вход (откуда брать звук):").grid(row=0, column=0, sticky="w")
        self.cb_in = ttk.Combobox(f, state="readonly", width=70); self.cb_in.grid(row=0, column=1, sticky="we", columnspan=2)
        ttk.Button(f, text="Обновить", command=self.refresh_devices, width=10).grid(row=0, column=3, padx=4)

        ttk.Label(f, text="Выход (куда играть после программы):").grid(row=1, column=0, sticky="w")
        self.cb_out = ttk.Combobox(f, state="readonly", width=70); self.cb_out.grid(row=1, column=1, sticky="we", columnspan=2)
        self.vol = tk.IntVar(value=100)
        self.sc_vol = ttk.Scale(f, from_=0, to=100, variable=self.vol, command=self._on_volume)
        self.sc_vol.grid(row=1, column=3, sticky="we", padx=4)

        v = ttk.Frame(self.root); v.pack(fill="x", **pad)
        self.vdev_var = tk.StringVar(value="Своё виртуальное устройство: проверяю ...")
        ttk.Label(v, textvariable=self.vdev_var).pack(side="left")
        self.btn_cable = ttk.Button(v, text="Установить своё устройство", command=self.install_cable)
        self.btn_cable.pack(side="right")

        g = ttk.Frame(self.root); g.pack(fill="x", **pad)
        ttk.Label(g, text="Модель:").pack(side="left")
        self.cb_model = ttk.Combobox(g, state="readonly", values=MODELS, width=15); self.cb_model.pack(side="left", padx=(2, 10))
        self.cb_model.set(MODELS[0])
        ttk.Label(g, text="Язык:").pack(side="left")
        self.en_lang = ttk.Entry(g, width=6); self.en_lang.insert(0, "ru"); self.en_lang.pack(side="left", padx=(2, 10))
        self.v_journal = tk.BooleanVar(value=True)
        self.v_vosk = tk.BooleanVar(value=True)
        self.v_diar = tk.BooleanVar(value=True)
        ttk.Checkbutton(g, text="Имя файла из журнала", variable=self.v_journal).pack(side="left", padx=4)
        ttk.Checkbutton(g, text="Черновик Vosk", variable=self.v_vosk).pack(side="left", padx=4)
        ttk.Checkbutton(g, text="Спикеры", variable=self.v_diar).pack(side="left", padx=4)
        self.btn = ttk.Button(g, text="Старт", command=self.toggle, width=12); self.btn.pack(side="right")
        self.meter = ttk.Progressbar(g, length=120, maximum=100); self.meter.pack(side="right", padx=8)
        ttk.Label(g, text="Уровень:").pack(side="right")

        self.txt = scrolledtext.ScrolledText(self.root, wrap="word", height=18, font=("Segoe UI", 10))
        self.txt.pack(fill="both", expand=True, padx=6, pady=(3, 0))
        self.txt.tag_config("dim", foreground="#888888")
        self.txt.tag_config("hdr", foreground="#3a6ea5")
        self.txt.configure(state="disabled")

        self.draft_var = tk.StringVar(value="")
        ttk.Label(self.root, textvariable=self.draft_var, foreground="#777777", anchor="w", wraplength=740).pack(fill="x", padx=8)
        self.status_var = tk.StringVar(value="Готово. Выбери вход и нажми Старт.")
        ttk.Label(self.root, textvariable=self.status_var, anchor="w", relief="sunken").pack(fill="x", side="bottom")

    # ------------------------------------------------------------------ devices / settings
    def refresh_devices(self):
        try:
            self.inputs = T.list_input_devices()
            self.outputs = T.list_output_devices()
        except Exception as e:
            self.status_var.set(f"Не удалось получить список устройств: {e}")
            return
        self.cb_in["values"] = [self._in_label(d) for d in self.inputs]
        self.cb_out["values"] = [NO_OUTPUT] + [self._out_label(d) for d in self.outputs]
        cap, ren = vcable.find(self.inputs, self.outputs)
        self.cable = cap
        if cap and ren:
            self.vdev_var.set(f"Своё виртуальное устройство: есть. В Teams выбери вывод «{ren['name']}».")
            self.btn_cable.configure(text="Переименовать устройство", command=self.rename_cable)
            saved = self.settings.get("input")
            if not self.cb_in.get() and (not saved or saved not in [d["name"] for d in self.inputs]):
                self.cb_in.current(self.inputs.index(cap))
        else:
            self.vdev_var.set("Своё виртуальное устройство: не установлено (нужен драйвер VB-Cable, ~1 МБ).")
            self.btn_cable.configure(text="Установить своё устройство", command=self.install_cable)
        if not self.cb_in.get() and self.inputs:
            self.cb_in.current(0)
        if not self.cb_out.get():
            self.cb_out.current(0)

    def install_cable(self):
        self.btn_cable.configure(state="disabled")
        self._append("=== установка своего виртуального устройства ===", "hdr")
        def work():
            try:
                vcable.install(log=lambda m: self.sink.final("[cable] " + m))
            except Exception as e:
                self.sink.final(f"[error] установка кабеля: {e}")
            self.sink.q.put(("refresh", "", False))
        threading.Thread(target=work, daemon=True).start()

    def rename_cable(self):
        self.btn_cable.configure(state="disabled")
        def work():
            try:
                vcable.rename_endpoints(log=lambda m: self.sink.final("[cable] " + m))
            except Exception as e:
                self.sink.final(f"[error] переименование: {e}")
            self.sink.q.put(("refresh", "", False))
        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _in_label(d):
        kind = "loopback" if d["loopback"] else "вход"
        return f"[{kind}] {d['name']}  ({d['rate']} Гц, {d['channels']} кан.)"

    @staticmethod
    def _out_label(d):
        return f"{d['name']}  ({d['rate']} Гц)" + ("  [по умолчанию]" if d["default"] else "")

    def _selected_input(self):
        i = self.cb_in.current()
        return self.inputs[i] if 0 <= i < len(self.inputs) else None

    def _selected_output(self):
        i = self.cb_out.current() - 1
        return self.outputs[i] if 0 <= i < len(self.outputs) else None

    def _load_settings(self):
        try:
            with open(SETTINGS, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _apply_settings(self):
        s = self.settings
        for i, d in enumerate(self.inputs):
            if d["name"] == s.get("input"):
                self.cb_in.current(i)
        for i, d in enumerate(self.outputs):
            if d["name"] == s.get("output"):
                self.cb_out.current(i + 1)
        self.vol.set(int(s.get("volume", 100)))
        if s.get("model") in MODELS:
            self.cb_model.set(s["model"])
        if s.get("language"):
            self.en_lang.delete(0, "end"); self.en_lang.insert(0, s["language"])
        self.v_journal.set(bool(s.get("journal", True)))
        self.v_vosk.set(bool(s.get("vosk", True)))
        self.v_diar.set(bool(s.get("diar", True)))

    def _save_settings(self):
        d_in, d_out = self._selected_input(), self._selected_output()
        s = {"input": d_in["name"] if d_in else None, "output": d_out["name"] if d_out else None,
             "volume": int(self.vol.get()), "model": self.cb_model.get(), "language": self.en_lang.get().strip() or "ru",
             "journal": self.v_journal.get(), "vosk": self.v_vosk.get(), "diar": self.v_diar.get()}
        try:
            with open(SETTINGS, "w", encoding="utf-8") as f:
                json.dump(s, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ------------------------------------------------------------------ run control
    def _on_volume(self, _=None):
        if self.pipe and getattr(self.pipe.rec, "monitor", None):
            self.pipe.rec.monitor.volume = self.vol.get() / 100.0

    def toggle(self):
        if self.pipe is None:
            self.start()
        else:
            self.stop()

    def start(self):
        d_in = self._selected_input()
        if d_in is None:
            self.status_var.set("Выбери входное устройство."); return
        d_out = self._selected_output()
        self._save_settings()
        args = types.SimpleNamespace(
            list_devices=False, device=d_in["index"], file=None, process=None,
            model=self.cb_model.get(), language=self.en_lang.get().strip() or "ru", compute_type="int8",
            threads=max(1, (os.cpu_count() or 8) // 2), beam=5, out="recordings",
            no_diar=not self.v_diar.get(), spk_threshold=0.45, min_silence=700, max_utt=25.0,
            no_save_audio=False, initial_prompt=None, vosk_model=None, no_vosk=not self.v_vosk.get(),
            journal=self.v_journal.get(), monitor=d_out["index"] if d_out else None, monitor_volume=self.vol.get() / 100.0,
        )
        self._append(f"=== старт: вход «{d_in['name']}», выход «{d_out['name'] if d_out else 'не играть'}» ===", "hdr")
        self.btn.configure(text="Стоп")
        self.status_var.set("Загрузка моделей ...")
        self._set_controls("disabled")
        self.pipe = T.Pipeline(args)
        threading.Thread(target=self._start_thread, daemon=True).start()

    def _start_thread(self):
        # worker thread: never touch Tk here, report through the sink queue (drained by _poll on the Tk thread)
        try:
            self.pipe.start()
        except Exception:
            self.sink.final("[error] " + traceback.format_exc().strip().splitlines()[-1])
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(traceback.format_exc())
            self.pipe = None
            self.sink.q.put(("stopped", "Не удалось запустить, см. gui.log", False))

    def stop(self):
        if self.pipe is None:
            return
        self.status_var.set("Останавливаю, дописываю очередь ...")
        self.btn.configure(state="disabled")
        pipe, self.pipe = self.pipe, None
        def _stop():
            pipe.stop(timeout=180)
            self.sink.q.put(("stopped", "Остановлено.", False))
        threading.Thread(target=_stop, daemon=True).start()

    def _stopped(self, message="Остановлено."):
        self.btn.configure(text="Старт", state="normal")
        self._set_controls("readonly")
        self.draft_var.set("")
        self.meter["value"] = 0
        self.status_var.set(message)
        self._append("=== стоп ===", "hdr")

    def _set_controls(self, state):
        for w in (self.cb_in, self.cb_out, self.cb_model):
            w.configure(state=state)
        self.en_lang.configure(state="normal" if state == "readonly" else "disabled")

    def on_close(self):
        self._save_settings()
        if self.pipe is not None:
            pipe, self.pipe = self.pipe, None
            pipe.rec.stop_event.set()
        self.root.after(200, self.root.destroy)

    # ------------------------------------------------------------------ output
    def _append(self, line, tag=None):
        self.txt.configure(state="normal")
        self.txt.insert("end", line + "\n", tag)
        self.txt.see("end")
        self.txt.configure(state="disabled")

    def _poll(self):
        try:
            while True:
                kind, text, dim = self.sink.q.get_nowait()
                if kind == "final":
                    if text.startswith("[ready]"):
                        self.status_var.set("Слушаю." + (f"  {self.pipe.info}" if self.pipe and self.pipe.info else ""))
                    elif text.startswith("[out]"):
                        self.status_var.set("Файл: " + text[6:])
                    elif text.startswith("[error]"):
                        self.status_var.set(text)
                    self._append(text.rstrip(), "dim" if dim or text.startswith(("[", "    .")) else None)
                elif kind == "partial":
                    self.draft_var.set("~ " + text[-300:])
                elif kind == "draft":
                    self.draft_var.set("")
                    if text:
                        self._append("~ " + text, "dim")
                elif kind == "stopped":
                    self._stopped(text)
                elif kind == "refresh":
                    self.btn_cable.configure(state="normal")
                    self.refresh_devices()
        except queue.Empty:
            pass
        if self.pipe is not None and self.pipe.rec is not None:
            lvl = getattr(self.pipe.rec, "level", 0.0)
            self.meter["value"] = min(100, int(lvl * 300))
            if self.pipe.asr is not None and not self.pipe.asr.is_alive():
                self.status_var.set("Конвейер остановился, см. gui.log")
        self.root.after(100, self._poll)


def main():
    try:
        root = tk.Tk()
        try:
            root.tk.call("tk", "scaling", 1.25)
        except Exception:
            pass
        App(root)
        root.mainloop()
    except Exception:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
