from __future__ import annotations

from uuid import UUID, uuid4


def new_id() -> UUID:
    """Stable platform identity. External system ids are references, never identity."""
    return uuid4()
