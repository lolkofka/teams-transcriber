"""
External audio processor: an HTTP server that runs faster-whisper and returns segments, so the desktop app can
offload transcription to a stronger machine (GPU box, spare PC). Same speaker labelling still happens in the app;
only the heavy Whisper step moves here.

Run on the processing machine (Python 3.10+, `pip install faster-whisper`):

    python processor_server.py --model large-v3-turbo --device cuda --port 8756 --token СЕКРЕТ

Then in the app: Настройки -> Обработчик -> Внешний, URL http://<host>:8756 , тот же токен.

Protocol (one endpoint):
  POST /transcribe   headers: X-Token: <token>   body JSON:
    {"sr":16000,"pcm16":"<base64 of int16 mono>","language":"ru"|null,"beam":5,"initial_prompt":null,"no_speech_threshold":0.6}
  -> {"language":"ru","segments":[{"start":0.0,"end":1.2,"text":"...","no_speech_prob":0.01}, ...]}
  GET /health -> {"ok":true,"model":"...","device":"..."}
"""

import argparse
import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

MODELS = {}          # name -> WhisperModel, loaded on demand
ARGS = None
LOCK = threading.Lock()  # serialize decode calls (one GPU / one CPU pool)

ALLOWED = ("large-v3", "large-v3-turbo", "medium", "small", "base", "tiny")


def cuda_available():
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def get_model(name=None):
    """Load (and cache) a model by name. Unknown names fall back to the server default."""
    from faster_whisper import WhisperModel
    name = (name or ARGS.model)
    if name not in ALLOWED:
        name = ARGS.model
    if not ARGS.allow_model_switch:
        name = ARGS.model
    m = MODELS.get(name)
    if m is None:
        print(f"[server] loading whisper '{name}' on {ARGS.device} ({ARGS.compute_type}) ...", flush=True)
        m = WhisperModel(name, device=ARGS.device, compute_type=ARGS.compute_type,
                         cpu_threads=ARGS.threads if ARGS.device == "cpu" else 0)
        MODELS[name] = m
        print(f"[server] '{name}' ready", flush=True)
    return m


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # quiet

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self):
        return not ARGS.token or self.headers.get("X-Token") == ARGS.token

    def do_GET(self):
        if self.path.startswith("/health"):
            if not self._auth_ok():
                self._send(401, {"error": "bad token"}); return
            import os as _os
            self._send(200, {"ok": True, "model": ARGS.model, "device": ARGS.device,
                             "compute_type": ARGS.compute_type, "threads": ARGS.threads,
                             "cores": _os.cpu_count(), "cuda": cuda_available(),
                             "models": sorted(MODELS.keys()),
                             "allow_model_switch": bool(ARGS.allow_model_switch),
                             "allowed_models": list(ALLOWED) if ARGS.allow_model_switch else [ARGS.model],
                             "max_beam": ARGS.max_beam})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.startswith("/transcribe"):
            self._send(404, {"error": "not found"}); return
        if not self._auth_ok():
            self._send(401, {"error": "bad token"}); return
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n).decode("utf-8"))
            pcm = np.frombuffer(base64.b64decode(req["pcm16"]), dtype=np.int16).astype(np.float32) / 32768.0
        except Exception as e:
            self._send(400, {"error": f"bad request: {e}"}); return
        lang = req.get("language") or None
        beam = max(1, min(int(ARGS.max_beam), int(req.get("beam", 5))))
        t0 = time.time()
        try:
            model = get_model(req.get("model"))
            with LOCK:
                segments, info = model.transcribe(
                    pcm, language=lang, beam_size=beam, vad_filter=False,
                    condition_on_previous_text=False, initial_prompt=req.get("initial_prompt"),
                    no_speech_threshold=float(req.get("no_speech_threshold", 0.6)))
                segs = [{"start": s.start, "end": s.end, "text": s.text, "no_speech_prob": s.no_speech_prob} for s in segments]
        except Exception as e:
            self._send(500, {"error": f"decode failed: {e}"}); return
        self._send(200, {"language": info.language, "segments": segs, "proc": round(time.time() - t0, 2),
                         "model": getattr(model, "_teams_name", req.get("model") or ARGS.model), "beam": beam})


def main():
    global ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="large-v3-turbo")
    ap.add_argument("--device", default="cpu", help="cpu | cuda")
    ap.add_argument("--compute-type", default=None, help="default: int8 on cpu, float16 on cuda")
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8756)
    ap.add_argument("--token", default="", help="shared secret; clients must send it as X-Token")
    ap.add_argument("--max-beam", type=int, default=5, help="ceiling for the beam width clients may ask for")
    ap.add_argument("--no-model-switch", dest="allow_model_switch", action="store_false",
                    help="ignore the model requested by clients, always use --model")
    ap.set_defaults(allow_model_switch=True)
    ARGS = ap.parse_args()
    if ARGS.compute_type is None:
        ARGS.compute_type = "float16" if ARGS.device == "cuda" else "int8"
    if ARGS.device == "cuda" and not cuda_available():
        print("[server] CUDA не найдена, перехожу на cpu", flush=True)
        ARGS.device, ARGS.compute_type = "cpu", "int8"
    get_model(ARGS.model)
    srv = ThreadingHTTPServer((ARGS.host, ARGS.port), Handler)
    print(f"[server] listening on {ARGS.host}:{ARGS.port}" + (" (token required)" if ARGS.token else " (no token!)"), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
