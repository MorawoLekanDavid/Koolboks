from alembic import context
from platform_v2.db import Config, factory
from platform_v2.models import Base

config = Config.from_env()
engine, _ = factory(config)
with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata, version_table="v2_schema_versions")
    with context.begin_transaction():
        context.run_migrations()
