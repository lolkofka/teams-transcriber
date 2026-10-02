"""Writable app data and bundled read-only resources."""
import os
import sys

FROZEN = bool(getattr(sys, "frozen", False))
RESOURCE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
if FROZEN and sys.platform == "darwin":
    BASE_DIR = os.path.expanduser("~/Library/Application Support/TeamsTranscriber")
    os.makedirs(BASE_DIR, exist_ok=True)
elif FROZEN:
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
