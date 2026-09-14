"""Vercel serverless function entrypoint for SIH26122 FastAPI backend."""

import os
import sys
from pathlib import Path

# Add backend directory to sys.path so 'app' package is discoverable
root_dir = Path(__file__).resolve().parent.parent
backend_dir = root_dir / "backend"
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

from app.main import app  # noqa: E402

