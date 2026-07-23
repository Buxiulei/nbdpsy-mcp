"""note_deletions 台账表(删除不可逆,终态必须可追溯)

Revision ID: a7d3e1f52c90
Revises: cb468a963422
Create Date: 2026-07-23 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7d3e1f52c90'
down_revision: Union[str, Sequence[str], None] = 'cb468a963422'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema:建 note_deletions 台账表(镜像内存台账,重启后终态仍可查)。"""
    op.create_table(
        'note_deletions',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('account_id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('deleted', sa.Integer(), nullable=False),
        sa.Column('remaining', sa.Integer(), nullable=True),
        sa.Column('reason', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['account_id'], ['xhs_accounts.id']),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    """Downgrade schema:删 note_deletions 表。"""
    op.drop_table('note_deletions')
