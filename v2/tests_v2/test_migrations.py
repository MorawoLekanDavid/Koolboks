from pathlib import Path
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from platform_v2.models import Base

def test_versioned_migration_creates_exact_schema_and_is_repeatable(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    url = "sqlite:///" + str(tmp_path / "migration.db")
    monkeypatch.setenv("ITURA_DATABASE_URL", url)
    monkeypatch.setenv("ITURA_ORIGIN", "http://localhost:5173")
    monkeypatch.setenv("ITURA_LOCAL_DEVELOPMENT", "1")
    config = Config(str(root / "alembic-v2.ini"))
    config.set_main_option("script_location", str(root / "migrations_v2"))
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    engine = create_engine(url)
    inspector = inspect(engine)
    assert set(inspector.get_table_names()) == set(Base.metadata.tables) | {"v2_schema_versions"}
    for name, table in Base.metadata.tables.items():
        assert {c["name"] for c in inspector.get_columns(name)} == set(table.columns.keys())
