"""
Schedule lookup in the Top Academy student journal (journal.top-academy.ru, MyStat API).

Credentials live in journal.json next to this file (see journal.json.example).
The access token is cached in journal_token.json and refreshed on 401.

CLI:
  python journal.py             # print today's schedule and the lesson the recording would go to
  python journal.py 2026-09-05  # same for another date
"""

import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.request

from paths import BASE_DIR as HERE
CONFIG = os.path.join(HERE, "journal.json")
TOKEN_CACHE = os.path.join(HERE, "journal_token.json")
APP_KEY = "6a56a5df2667e65aab73ce76d1dd737f7d1faef9c52e8b8c55ac75f565d8e8a6"
AFTER_GRACE = dt.timedelta(minutes=15)   # recording later than this after the last lesson -> separate file


class JournalError(Exception):
    pass


def _load_config():
    if not os.path.exists(CONFIG):
        raise JournalError(f"no {CONFIG} (copy journal.json.example and fill in username/password)")
    with open(CONFIG, encoding="utf-8") as f:
        cfg = json.load(f)
    if not cfg.get("username") or not cfg.get("password"):
        raise JournalError("journal.json: username/password are empty")
    cfg.setdefault("host", "msapi.top-academy.ru")
    cfg.setdefault("origin", "https://journal.top-academy.ru")
    return cfg


def _request(cfg, path, body=None, token=None):
    url = f"https://{cfg['host']}/api/v2/{path}"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": cfg["origin"],
        "Referer": cfg["origin"] + "/",
        "User-Agent": "Mozilla/5.0",
        "x-language": "ru",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8") or "null")
        except Exception:
            payload = None
        return e.code, payload


def _login(cfg):
    status, data = _request(cfg, "auth/login", {
        "application_key": APP_KEY, "id_city": None,
        "username": cfg["username"], "password": cfg["password"],
    })
    if status != 200 or not isinstance(data, dict) or "access_token" not in data:
        raise JournalError(f"login failed ({status}): {data}")
    with open(TOKEN_CACHE, "w", encoding="utf-8") as f:
        json.dump({"access_token": data["access_token"], "user": cfg["username"]}, f)
    return data["access_token"]


def _token(cfg, force=False):
    if not force and os.path.exists(TOKEN_CACHE):
        try:
            with open(TOKEN_CACHE, encoding="utf-8") as f:
                c = json.load(f)
            if c.get("user") == cfg["username"] and c.get("access_token"):
                return c["access_token"]
        except Exception:
            pass
    return _login(cfg)


def get_schedule(day):
    """Lessons for a date, sorted by start time. Each: dict(subject, teacher, room, start, end) with datetimes."""
    cfg = _load_config()
    path = f"schedule/operations/get-by-date?date_filter={day:%Y-%m-%d}"
    status, data = _request(cfg, path, token=_token(cfg))
    if status == 401:
        status, data = _request(cfg, path, token=_token(cfg, force=True))
    if status != 200 or not isinstance(data, list):
        raise JournalError(f"schedule request failed ({status}): {data}")
    lessons = []
    for it in data:
        try:
            d = dt.datetime.strptime(it["date"], "%Y-%m-%d").date()
            if d != day:
                continue
            start = dt.datetime.combine(d, dt.datetime.strptime(it["started_at"][:5], "%H:%M").time())
            end = dt.datetime.combine(d, dt.datetime.strptime(it["finished_at"][:5], "%H:%M").time())
        except (KeyError, ValueError):
            continue
        lessons.append({
            "number": it.get("lesson"),
            "subject": (it.get("subject_name") or "").strip() or "Без названия",
            "teacher": (it.get("teacher_name") or "").strip(),
            "room": (it.get("room_name") or "").strip(),
            "lesson": it.get("lesson"),
            "start": start, "end": end,
        })
    lessons.sort(key=lambda x: x["start"])
    return lessons


_VISITS_CACHE = {"t": 0.0, "data": None}
MARK_LABELS = [("class_work_mark", "Кл"), ("home_work_mark", "ДЗ"), ("lab_work_mark", "Лаб"),
               ("practical_work_mark", "Пр"), ("control_work_mark", "КР"), ("final_work_mark", "Итог")]
WAS_LABELS = {1: "был", 0: "не был", 2: "опоздал", None: "нет отметки"}


def get_visits(day, max_age=60):
    """Attendance + marks for a date, keyed by lesson number (schedule 'lesson' field).
    Uses progress/operations/student-visits (whole history, so cached for max_age seconds)."""
    import time
    cfg = _load_config()
    now = time.time()
    if _VISITS_CACHE["data"] is None or now - _VISITS_CACHE["t"] > max_age:
        path = "progress/operations/student-visits"
        status, data = _request(cfg, path, token=_token(cfg))
        if status == 401:
            status, data = _request(cfg, path, token=_token(cfg, force=True))
        if status != 200 or not isinstance(data, list):
            raise JournalError(f"visits request failed ({status}): {data}")
        _VISITS_CACHE.update(t=now, data=data)
    out = {}
    ds = f"{day:%Y-%m-%d}"
    for v in _VISITS_CACHE["data"]:
        if v.get("date_visit") != ds:
            continue
        marks = {label: v[k] for k, label in MARK_LABELS if v.get(k) is not None}
        theme = (v.get("lesson_theme") or "").strip()
        if len(theme) < 3 or theme.replace(".", "").replace(" ", "").isdigit():
            theme = ""  # teachers sometimes put "1" or "-" as the theme: not worth showing
        out[v.get("lesson_number")] = {
            "was": v.get("status_was"), "was_text": WAS_LABELS.get(v.get("status_was"), "?"),
            "marks": marks, "theme": theme, "subject": v.get("spec_name", ""),
        }
    return out


def pick_lesson(lessons, now):
    """
    Which lesson a recording started at `now` belongs to.
    Returns (lesson or None, reason). Rules:
      - inside a lesson                      -> that lesson
      - between lessons                      -> the previous one
      - before the first lesson of the day   -> the first one (started early)
      - later than 15 min after the last one -> None (separate file)
      - no lessons today                     -> None
    """
    if not lessons:
        return None, "no-lessons"
    for l in lessons:
        if l["start"] <= now <= l["end"]:
            return l, "in-lesson"
    if now < lessons[0]["start"]:
        return lessons[0], "before-first"
    last = lessons[-1]
    if now > last["end"] + AFTER_GRACE:
        return None, "after-lessons"
    prev = [l for l in lessons if l["end"] < now]
    return (prev[-1] if prev else last), "between"


def _slug(text, limit=70):
    text = re.sub(r"[\\/:*?\"<>|]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:  # cut on a word boundary
        text = text[:limit].rsplit(" ", 1)[0]
    return text.rstrip(" .,-")


def session_name(now=None):
    """
    File base name for a recording starting at `now`:
      2026-09-03_09-00_Название пары          (attached to a lesson: date + lesson start time + subject)
      2026-09-03_18-42_вне-пар                (more than 15 min after the last lesson: recording start time)
      2026-09-03_18-42_без-пар                (no lessons in the journal for that day)
    Returns (name, info string).
    """
    now = now or dt.datetime.now()
    lessons = get_schedule(now.date())
    lesson, why = pick_lesson(lessons, now)
    if lesson is None:
        tag = "без-пар" if why == "no-lessons" else "вне-пар"
        meta = {"subject": tag, "why": why, "date": f"{now:%Y-%m-%d}", "lessons_today": len(lessons)}
        return f"{tag}_{now:%Y-%m-%d_%H-%M}", f"{why}: {len(lessons)} lessons today", meta
    name = f"{_slug(lesson['subject'])}_{lesson['start']:%Y-%m-%d_%H-%M}"
    info = f"{why}: {lesson['start']:%H:%M}-{lesson['end']:%H:%M} {lesson['subject']}"
    if lesson["teacher"]:
        info += f" ({lesson['teacher']})"
    meta = {"subject": lesson["subject"], "teacher": lesson["teacher"], "room": lesson["room"],
            "why": why, "date": f"{lesson['start']:%Y-%m-%d}",
            "time": f"{lesson['start']:%H:%M}-{lesson['end']:%H:%M}", "lessons_today": len(lessons)}
    return name, info, meta


if __name__ == "__main__":
    day = dt.datetime.strptime(sys.argv[1], "%Y-%m-%d").date() if len(sys.argv) > 1 else dt.date.today()
    try:
        ls = get_schedule(day)
    except JournalError as e:
        print("error:", e)
        sys.exit(1)
    if not ls:
        print(f"{day}: no lessons")
    for l in ls:
        print(f"{l['start']:%H:%M}-{l['end']:%H:%M}  {l['subject']}  [{l['teacher']}] {l['room']}")
    now = dt.datetime.now() if day == dt.date.today() else dt.datetime.combine(day, dt.time(12, 0))
    lesson, why = pick_lesson(ls, now)
    print(f"\nnow {now:%H:%M} -> {why}: " + (lesson["subject"] if lesson else "separate file"))
    if day == dt.date.today():
        print("file name:", session_name(now)[0])
