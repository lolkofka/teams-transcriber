"""Writable app data and bundled read-only resources."""
import os
import sys

FROZEN = bool(getattr(sys, "frozen", False))
if FROZEN and sys.platform == "darwin":
    BASE_DIR = os.path.expanduser("~/Library/Application Support/TeamsTranscriber")
    RESOURCE_DIR = sys._MEIPASS
    os.makedirs(BASE_DIR, exist_ok=True)
elif FROZEN:
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
    RESOURCE_DIR = BASE_DIR  # Windows onedir: build_dist.py copies ui/ next to the exe.
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    RESOURCE_DIR = BASE_DIR

if FROZEN and sys.platform == "win32":
    USER_DATA_DIR = os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~/AppData/Local")),
        "TeamsTranscriber")
    os.makedirs(USER_DATA_DIR, exist_ok=True)
else:
    USER_DATA_DIR = BASE_DIR
