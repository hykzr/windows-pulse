from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON_SOURCE = ROOT / "python"
sys.path.insert(0, str(PYTHON_SOURCE))


# The state-machine tests deliberately do not require a compiled Rust extension.
# Individual recorder tests monkeypatch this module with a deterministic backend.
native = types.ModuleType("windowpulse._native")
native.is_supported = lambda: True
native.has_permission = lambda: True
native.request_permission = lambda: True
native.backend_name = lambda: "test"
native.list_windows_native = lambda: []


class _MissingNativeCapturer:
    def __init__(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("a test must monkeypatch NativeCapturer before starting a recorder")


native.NativeCapturer = _MissingNativeCapturer
sys.modules.setdefault("windowpulse._native", native)
