"""Persist ``needs_review`` on chat messages.

The Copilot has always computed a needs-review flag - a fabricated citation, an
answer of real length citing nothing, or low grounding confidence - and shown it
next to the answer. It was never stored. ``chat_messages`` had no such column, and
the session-reload path read it with ``getattr(message, "needs_review", False)``,
which is a constant ``False``.

So a flagged answer arrived correctly marked, and then presented itself as clean
the moment the conversation was reopened. For a repository whose answers are read
by people who act on them, an audit trail that asserts an answer was verified when
the system knew otherwise is worse than one that never made the claim.

The flag is stored rather than recomputed on read because it is a statement about
what was known at answer time: which passages the model was shown, which labels it
emitted, and what the validator made of them. The context package is not retained,
so there is nothing to recompute it from.

Existing rows default to ``false``. That is the honest value: those answers were
generated before the flag was persisted and nothing records what it was. It is not
a claim that they were reviewed - it is the absence of a claim, which matches the
state the column is in.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | None = None
depends_on: str | None = None

TABLE = "chat_messages"
COLUMN = "needs_review"


def upgrade() -> None:
    # Guarded, because revision 0001 builds the schema with
    # ``Base.metadata.create_all`` against *live* model metadata. On a database
    # created from scratch today that already includes this column, and an
    # unguarded ADD COLUMN aborted the whole upgrade before it could reach any
    # later revision. Every other migration in this directory is written the same
    # way for the same reason - see 0003's note about being a no-op.
    op.execute(
        f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS {COLUMN} "
        f"BOOLEAN NOT NULL DEFAULT false"
    )
    # Partial: the flagged answers are the minority and the only ones anyone
    # queries for. A full index would be almost entirely `false` entries nobody
    # looks up.
    op.execute(
        f"CREATE INDEX IF NOT EXISTS ix_{TABLE}_needs_review "
        f"ON {TABLE} ({COLUMN}) WHERE {COLUMN}"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS ix_{TABLE}_needs_review")
    op.drop_column(TABLE, COLUMN)
