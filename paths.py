"""Base directory of the app: the source folder when run from Python, the folder with the .exe when frozen."""
import os
import sys

FROZEN = bool(getattr(sys, "frozen", False))
BASE_DIR = os.path.dirname(os.path.abspath(sys.executable)) if FROZEN else os.path.dirname(os.path.abspath(__file__))
