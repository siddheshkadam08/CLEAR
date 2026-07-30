"""Pydantic request/response contracts.

Kept separate from the SQLAlchemy models so the API surface can evolve without
being dictated by table shape, and so no ORM object is ever serialised directly.
"""

from app.schemas.common import (
    BoundingBox,
    Citation,
    ErrorResponse,
    Evidence,
    MessageResponse,
    Paginated,
)

__all__ = [
    "BoundingBox",
    "Citation",
    "ErrorResponse",
    "Evidence",
    "MessageResponse",
    "Paginated",
]
