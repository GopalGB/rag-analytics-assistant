"""Thread-local token accounting. Providers report usage; the model router captures it per call."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass

_local = threading.local()


@dataclass(eq=False)  # captures are unwound by identity; equal counts must not collide
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


def add(input_tokens: int | None, output_tokens: int | None) -> None:
    for u in getattr(_local, "stack", []):
        u.input_tokens += int(input_tokens or 0)
        u.output_tokens += int(output_tokens or 0)


@contextmanager
def capture():
    stack = getattr(_local, "stack", None)
    if stack is None:
        stack = _local.stack = []
    u = Usage()
    stack.append(u)
    try:
        yield u
    finally:
        stack.remove(u)
