"""Shared test paths; production code never imports this package."""
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
