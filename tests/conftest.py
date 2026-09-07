"""Make the takcore package importable from tests.

The application lives in app/takcore, so tests need app/ on sys.path.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
