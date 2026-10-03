"""Helpers for this Craftarr instance's stable node identity."""

import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import CraftarrInstance


def get_node_id(db: Session) -> str:
    """Return this installation's persisted UUID, creating it if absent."""
    identity = db.get(CraftarrInstance, 1)
    if identity is not None:
        return identity.node_id

    db.add(CraftarrInstance(id=1, node_id=str(uuid.uuid4())))
    try:
        db.commit()
    except IntegrityError:
        # Another request may have initialized the singleton concurrently.
        db.rollback()

    identity = db.get(CraftarrInstance, 1)
    if identity is None:
        raise RuntimeError("Craftarr instance identity could not be initialized")
    return identity.node_id


def server_reference(node_id: str, server_id: int) -> str:
    """Build a stable reference without changing a node's local integer ID."""
    return f"{node_id}:{server_id}"
