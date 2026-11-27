"""Serial gate for external side-effect checks.

The gate is the trusted proxy's serialization point. Callers cannot mark a
run UNKNOWN by timing out or by declaring themselves to be the proxy.
"""

from __future__ import annotations

import threading
import time


class SerialEffectProxy:
    def __init__(self):
        self._lock = threading.Lock()
        self.spans: list[tuple[str, float, float]] = []

    def check(self, effect_id: str, examiner, hold=None):
        with self._lock:
            started = time.monotonic()
            if hold is not None:
                hold()
            try:
                return examiner()
            finally:
                self.spans.append((effect_id, started, time.monotonic()))
