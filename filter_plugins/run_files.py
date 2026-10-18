"""Controller-side paths shared with the Python migration helpers."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from run_layout import run_path


class FilterModule:
    def filters(self):
        return {'run_file': lambda root, name: str(run_path(root, name))}
