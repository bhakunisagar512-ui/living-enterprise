"""The run that is currently active, so tools (called by CrewAI, not by us) can write to its trace.

CrewAI calls tools from its own worker threads, where context variables are not always carried
over, so this is a simple process-wide slot guarded by a lock. The web server allows only one
run at a time, which is what makes this safe.
"""
import threading
from typing import Optional

_lock = threading.Lock()
_active = None


def set_active_run(run) -> None:
    global _active
    with _lock:
        _active = run


def active_run() -> Optional[object]:
    with _lock:
        return _active
