"""Restore product image links and durable message media snapshots."""
from alembic import op
import sqlalchemy as sa

revision = "v2_0002"
down_revision = "v2_0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("v2_products", sa.Column("image_url", sa.Text(), nullable=False, server_default=""))
    op.add_column("v2_products", sa.Column("product_url", sa.Text(), nullable=False, server_default=""))
    op.add_column("v2_messages", sa.Column("media", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))


def downgrade():
    op.drop_column("v2_messages", "media")
    op.drop_column("v2_products", "product_url")
    op.drop_column("v2_products", "image_url")
