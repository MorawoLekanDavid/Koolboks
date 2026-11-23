import pytest
from platform_v2.db import Config
from platform_v2.db import factory
from sqlalchemy.pool import NullPool

def test_vercel_does_not_retain_postgres_session_connections(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    engine, _ = factory(Config("postgresql://unused:unused@localhost/db", "https://example.com", database_schema="itura"))
    assert isinstance(engine.pool, NullPool)
    engine.dispose()

def test_production_defaults_to_private_schema(monkeypatch):
    monkeypatch.setenv("ITURA_DATABASE_URL", "postgresql://user:password@localhost/db")
    monkeypatch.setenv("ITURA_ORIGIN", "https://console.example.com")
    monkeypatch.delenv("ITURA_LOCAL_DEVELOPMENT", raising=False)
    monkeypatch.delenv("ITURA_DATABASE_SCHEMA", raising=False)
    assert Config.from_env().database_schema == "itura"

@pytest.mark.parametrize("schema", ["public", "auth", "storage", "kpi_private", "pg_catalog", "itura;drop schema public", "itura,public"])
def test_shared_or_unsafe_schema_rejected(schema):
    with pytest.raises(ValueError):
        Config("postgresql://localhost/db", "https://example.com", database_schema=schema)

def test_production_rejects_empty_schema(monkeypatch):
    monkeypatch.setenv("ITURA_DATABASE_URL", "postgresql://localhost/db")
    monkeypatch.setenv("ITURA_ORIGIN", "https://example.com")
    monkeypatch.setenv("ITURA_DATABASE_SCHEMA", "")
    monkeypatch.delenv("ITURA_LOCAL_DEVELOPMENT", raising=False)
    with pytest.raises(RuntimeError, match="dedicated database schema"):
        Config.from_env()
