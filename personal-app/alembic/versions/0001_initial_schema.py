"""Create the initial QuantumOrb personal schema.

Revision ID: 0001_initial_schema
"""

from __future__ import annotations

from alembic import op
from app.db import models  # noqa: F401
from app.db.base import Base

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
