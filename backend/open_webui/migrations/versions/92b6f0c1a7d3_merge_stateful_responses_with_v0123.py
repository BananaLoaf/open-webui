"""Merge the fork's Responses storage with the upstream v0.12.3 schema.

Both branches extend d4c1a8e37b62 and change disjoint columns/tables. Keep their
original revision IDs and parents: deployed fork databases may already be at
f7d8e9a0b1c2. Alembic applies the missing branch before reaching this no-op merge.
"""

revision = '92b6f0c1a7d3'
down_revision = ('f7d8e9a0b1c2', '0bd252b206fb')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
