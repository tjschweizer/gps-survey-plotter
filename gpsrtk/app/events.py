"""Change notification without a GUI toolkit.

The desktop application had the state emit Qt signals that widgets were wired
to. A browser cannot be wired to anything in-process; what it can do is ask
what has changed since it last looked. So each signal here does two things:
it bumps a revision counter for its topic, which the server hands to the
browser so it knows what to fetch again, and it calls any in-process observers
that are connected - which is how Python code, tests included, can still watch
the state without polling it.
"""

from __future__ import annotations

from typing import Callable


class Signal:
    """A named change with a revision counter and optional observers."""

    def __init__(self, topic: str, revisions: dict[str, int]):
        self.topic = topic
        self._revisions = revisions
        self._slots: list[Callable] = []
        revisions.setdefault(topic, 0)

    def connect(self, slot: Callable) -> None:
        self._slots.append(slot)

    def disconnect(self, slot: Callable) -> None:
        self._slots.remove(slot)

    def emit(self, *args) -> None:
        self._revisions[self.topic] += 1
        for slot in list(self._slots):
            slot(*args)
