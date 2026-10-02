from __future__ import annotations


class DomainError(Exception):
    """Base class for every error the domain and application layers raise."""


class InvalidArgument(DomainError):
    """A value violates a domain rule."""


class NotFound(DomainError):
    def __init__(self, entity: str, key: object) -> None:
        super().__init__(f"{entity} {key!r} not found")
        self.entity = entity
        self.key = key


class AlreadyExists(DomainError):
    def __init__(self, entity: str, key: object) -> None:
        super().__init__(f"{entity} {key!r} already exists")
        self.entity = entity
        self.key = key


class Conflict(DomainError):
    """The request conflicts with existing state and is not a safe replay."""


class IllegalTransition(DomainError):
    def __init__(self, machine: str, source: str, target: str) -> None:
        super().__init__(f"{machine}: illegal transition {source} -> {target}")
        self.machine = machine
        self.source = source
        self.target = target
