"""
Lesson analyst: every few minutes sends the recent transcript to an OpenAI-compatible chat API
(DeepSeek by default: cheap, https://api.deepseek.com, model deepseek-chat) and keeps a short state:
current topic, current assignment, key points.

    an = LessonAnalyst(base_url, api_key, model, interval_s, on_result)
    an.start(); an.add(text, speaker); an.analyze_now(); an.stop()
"""

import json
import threading
import time
import urllib.error
import urllib.request

SYSTEM = (
    "Ты помощник студента, который слушает запись пары (лекции или практики). "
    "По фрагменту транскрипта определи, что происходит прямо сейчас. Транскрипт автоматический, с ошибками распознавания, "
    "речь преподавателя и студентов вперемешку; реплики студента-пользователя помечены как «Вы». "
    "Отвечай строго одним JSON-объектом без пояснений: "
    "{\"topic\": \"текущая тема одной фразой\", "
    "\"task\": \"актуальное задание, что нужно сделать студентам сейчас или к следующему разу; если задания не было, пустая строка\", "
    "\"points\": [\"2-4 ключевых тезиса за последние минуты\"], "
    "\"deadline\": \"срок или пустая строка\"}. "
    "Пиши по-русски, кратко, без воды. Если тема не изменилась, повтори предыдущую формулировку."
)


def _extra_opts(base_url, model):
    """Thinking models (Gemini 3.x, o-series) burn the output budget on reasoning: ask for little of it."""
    m = (model or "").lower(); b = (base_url or "").lower()
    if "gemini" in m or "googleapis" in b or "openai.com" in b:
        return {"reasoning_effort": "low"}
    return {}


def _content_json(content):
    """Parse a JSON object out of a model reply (tolerates ```json fences and prose around it)."""
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        s, e = content.find("{"), content.rfind("}")
        if s < 0 or e < 0:
            raise RuntimeError("модель вернула не JSON: " + content[:120].replace("\n", " "))
        return json.loads(content[s:e + 1])


class LessonAnalyst(threading.Thread):
    def __init__(self, base_url, api_key, model="deepseek-chat", interval=180, on_result=None, on_log=None,
                 window_words=2500, min_new_words=40):
        super().__init__(daemon=True)
        self.base_url = (base_url or "https://api.deepseek.com").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or "deepseek-chat"
        self.interval = max(45, int(interval or 180))
        self.on_result = on_result
        self.on_log = on_log or (lambda m: None)
        self.window_words = window_words
        self.min_new_words = min_new_words
        self.entries = []          # (t, speaker, text)
        self.words_since = 0
        self.state = {"topic": "", "task": "", "points": [], "deadline": "", "updated": None}
        self._lock = threading.Lock()
        self._kick = threading.Event()
        self._stop = threading.Event()
        self.last_error = None

    # ---------------------------------------------------------------- input
    def add(self, text, speaker="", t=None):
        with self._lock:
            self.entries.append((t or time.time(), speaker, text))
            self.words_since += len(text.split())
            if len(self.entries) > 4000:
                self.entries = self.entries[-3000:]

    def analyze_now(self):
        self._kick.set()

    def ask(self, question, on_answer, window_words=9000):
        """Answer a free-form question about the lesson using the transcript as context (runs in a thread)."""
        def work():
            try:
                if not self.api_key:
                    raise RuntimeError("не задан API-ключ (Настройки → Анализ пары)")
                with self._lock:
                    ents = list(self.entries)
                out, n = [], 0
                for t, spk, text in reversed(ents):
                    n += len(text.split()); out.append((t, spk, text))
                    if n >= window_words:
                        break
                out.reverse()
                lines = []
                t0 = out[0][0] if out else time.time()
                for t, spk, text in out:
                    who = "Вы" if spk == "Вы" else (spk or "").replace("SPEAKER_", "Спикер ")
                    lines.append(f"[{int((t - t0) // 60):02d} мин] {who + ': ' if who else ''}{text}")
                st = self.state
                ctx = ("Текущее состояние: " + json.dumps({k: st.get(k) for k in ("topic", "task", "deadline")}, ensure_ascii=False) +
                       "\n\nТранскрипт пары (автоматический, с ошибками распознавания):\n" + ("\n".join(lines) if lines else "(пока пусто)"))
                sys_prompt = ("Ты помощник студента на паре. Отвечай на вопрос по содержанию транскрипта: что говорил преподаватель, "
                              "какое задание, что нужно сделать, объясни тему проще, и т.п. Если в транскрипте этого нет, так и скажи, "
                              "но помоги общими знаниями по теме. Отвечай по-русски, кратко и по делу, markdown не используй.")
                body = {"model": self.model, "temperature": 0.3, "max_tokens": 4000, **_extra_opts(self.base_url, self.model),
                        "messages": [{"role": "system", "content": sys_prompt},
                                     {"role": "user", "content": ctx + "\n\nВопрос: " + question}]}
                req = urllib.request.Request(self.base_url + "/chat/completions", data=json.dumps(body).encode("utf-8"),
                                             headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}, method="POST")
                t_req = time.time()
                try:
                    with urllib.request.urlopen(req, timeout=90) as r:
                        data = json.loads(r.read().decode("utf-8"))
                except urllib.error.HTTPError as e:
                    raise RuntimeError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}")
                answer = (data["choices"][0]["message"].get("content") or "").strip()
                if not answer:
                    raise RuntimeError(f"пустой ответ (finish_reason={data['choices'][0].get('finish_reason')}, usage={data.get('usage')})")
                on_answer({"ok": True, "q": question, "a": answer, "tokens": data.get("usage", {}).get("total_tokens"),
                           "latency": round(time.time() - t_req, 1)})
            except Exception as e:
                on_answer({"ok": False, "q": question, "error": str(e)})
        threading.Thread(target=work, daemon=True).start()

    def quick_answer(self, context, word, on_answer, model=None):
        """You were just called by name: was there a question for you in the last ~200 words? Give a short answer.
        Small context, small budget, optionally a faster model. Runs in a thread."""
        def work():
            try:
                if not self.api_key:
                    raise RuntimeError("не задан API-ключ")
                sys_prompt = (
                    "Ты суфлёр студента на паре. Тебе дан короткий кусок автоматического транскрипта (с ошибками распознавания), "
                    "в конце которого студента позвали по имени. Определи, был ли в этом куске вопрос или просьба, обращённые к нему "
                    "(ответить, повторить, что-то сказать, выполнить). Ответь строго JSON: "
                    "{\"question\": \"суть вопроса одной фразой или пустая строка, если вопроса нет\", "
                    "\"answer\": \"краткий правильный ответ или что сказать, 1-3 предложения; пусто если вопроса нет\", "
                    "\"confidence\": число от 0 до 1}. По-русски, без markdown.")
                st = self.state
                topic = st.get("topic") or ""
                user = ((f"Тема пары: {topic}\n" if topic else "") + f"Студента зовут: {word}\n\nТранскрипт последних минут:\n{context}")
                body = {"model": model or self.model, "temperature": 0.2, "max_tokens": 2500,
                        "messages": [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user}],
                        "response_format": {"type": "json_object"}, **_extra_opts(self.base_url, model or self.model)}
                req = urllib.request.Request(self.base_url + "/chat/completions", data=json.dumps(body).encode("utf-8"),
                                             headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}, method="POST")
                t_req = time.time()
                try:
                    with urllib.request.urlopen(req, timeout=45) as r:
                        data = json.loads(r.read().decode("utf-8"))
                except urllib.error.HTTPError as e:
                    raise RuntimeError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}")
                content = data["choices"][0]["message"].get("content") or ""
                if not content.strip():
                    raise RuntimeError(f"пустой ответ (finish_reason={data['choices'][0].get('finish_reason')})")
                res = _content_json(content)
                on_answer({"ok": True, "word": word, "question": (res.get("question") or "").strip(),
                           "answer": (res.get("answer") or "").strip(), "confidence": res.get("confidence"),
                           "latency": round(time.time() - t_req, 1), "tokens": data.get("usage", {}).get("total_tokens")})
            except Exception as e:
                on_answer({"ok": False, "word": word, "error": str(e)})
        threading.Thread(target=work, daemon=True).start()

    def stop(self):
        self._stop.set()
        self._kick.set()

    # ---------------------------------------------------------------- loop
    def run(self):
        while not self._stop.is_set():
            forced = self._kick.wait(self.interval)
            self._kick.clear()
            if self._stop.is_set():
                break
            if not forced and self.words_since < self.min_new_words:
                continue
            try:
                self._analyze()
            except Exception as e:
                self.last_error = str(e)
                self.on_log(f"[analyst] ошибка: {e}")

    def _window(self):
        with self._lock:
            ents = list(self.entries)
            self.words_since = 0
        out, n = [], 0
        for t, spk, text in reversed(ents):
            n += len(text.split())
            out.append((t, spk, text))
            if n >= self.window_words:
                break
        out.reverse()
        return out

    def _analyze(self):
        if not self.api_key:
            raise RuntimeError("не задан API-ключ (Настройки → Анализ пары)")
        win = self._window()
        if not win:
            self.on_log("[analyst] пока нет текста для анализа: подожди первых реплик")
            if self.on_result:
                self.on_result(dict(self.state, empty=True))
            return
        t0 = win[0][0]
        lines = []
        for t, spk, text in win:
            mm = int((t - t0) // 60)
            who = "Вы" if spk == "Вы" else (spk or "").replace("SPEAKER_", "Спикер ")
            lines.append(f"[{mm:02d} мин] {who + ': ' if who else ''}{text}")
        prev = {k: self.state.get(k) for k in ("topic", "task", "deadline")}
        user = ("Предыдущее состояние: " + json.dumps(prev, ensure_ascii=False) +
                "\n\nТранскрипт последних минут:\n" + "\n".join(lines))
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
            "temperature": 0.2,
            "max_tokens": 3000,
            "response_format": {"type": "json_object"},
            **_extra_opts(self.base_url, self.model),
        }
        req = urllib.request.Request(
            self.base_url + "/chat/completions", data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}, method="POST")
        t_req = time.time()
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}")
        content = data["choices"][0]["message"].get("content") or ""
        if not content.strip():
            raise RuntimeError(f"пустой ответ модели (finish_reason={data['choices'][0].get('finish_reason')}, usage={data.get('usage')})")
        res = _content_json(content)
        usage = data.get("usage", {})
        self.state = {
            "topic": (res.get("topic") or "").strip(),
            "task": (res.get("task") or "").strip(),
            "points": [str(p).strip() for p in (res.get("points") or []) if str(p).strip()][:5],
            "deadline": (res.get("deadline") or "").strip(),
            "updated": time.time(),
            "tokens": usage.get("total_tokens"),
            "latency": round(time.time() - t_req, 1),
            "words": sum(len(x[2].split()) for x in win),
        }
        self.last_error = None
        self.on_log(f"[analyst] обновлено: {self.state['topic'][:60]} ({usage.get('total_tokens', '?')} токенов, {self.state['latency']} с)")
        if self.on_result:
            self.on_result(dict(self.state))


if __name__ == "__main__":
    import sys
    key = sys.argv[1] if len(sys.argv) > 1 else ""
    an = LessonAnalyst("https://api.deepseek.com", key, on_result=print, on_log=print)
    an.add("Сегодня разбираем маршрутизацию в ASP.NET. К следующей паре сделайте контроллер с тремя маршрутами.", "SPEAKER_00")
    an.start(); an.analyze_now(); time.sleep(20)
