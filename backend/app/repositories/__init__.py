"""Data access layer (repository pattern).

Repositories own queries and never commit - the request-scoped transaction in
:func:`app.db.session.get_db` decides that. Project-scoped repositories require a
``project_id`` on every method, so the isolation boundary cannot be omitted by
accident.
"""

from app.repositories.base import BaseRepository, ProjectScopedRepository
from app.repositories.chunk import ChunkRepository

__all__ = ["BaseRepository", "ChunkRepository", "ProjectScopedRepository"]
