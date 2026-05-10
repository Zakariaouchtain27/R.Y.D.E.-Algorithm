"""
Tiny thread-safe pub/sub bus.

The bot publishes price-change events here. The web layer (or anyone
else) subscribes. Keeps `ryde/` decoupled from `web/` — the bot has no
idea WebSockets exist.
"""
import logging
from threading import Lock
from typing import Any, Callable, Dict, List

log = logging.getLogger(__name__)

_listeners: List[Callable[[Dict[str, Any]], None]] = []
_lock = Lock()


def subscribe(callback: Callable[[Dict[str, Any]], None]) -> None:
    with _lock:
        _listeners.append(callback)


def unsubscribe(callback: Callable[[Dict[str, Any]], None]) -> None:
    with _lock:
        if callback in _listeners:
            _listeners.remove(callback)


def publish(event: Dict[str, Any]) -> None:
    """Fire-and-forget. Listener exceptions are swallowed and logged."""
    with _lock:
        snapshot = list(_listeners)
    for cb in snapshot:
        try:
            cb(event)
        except Exception as exc:
            log.warning("event listener failed: %s", exc)
