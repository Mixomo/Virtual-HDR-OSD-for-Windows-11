"""Bounded recovery window; no growing callback queue or wall-clock dependency."""
from __future__ import annotations


class RecoveryWindow:
    delays = (0.7, 2.0, 5.0)

    def __init__(self) -> None:
        self.target: tuple[str, str] | None = None
        self.started = 0.0
        self.attempt = 0

    def start(self, key: str, mode: str, now: float) -> None:
        self.target = (key, mode)
        self.started = now
        self.attempt = 0

    def cancel(self) -> None:
        self.target = None

    def due(self, key: str, mode: str, now: float) -> bool:
        if self.target != (key, mode):
            self.cancel()
            return False
        if now - self.started < self.delays[self.attempt]:
            return False
        self.attempt += 1
        if self.attempt == len(self.delays):
            self.cancel()
        return True
