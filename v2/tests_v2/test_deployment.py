import asyncio
import time
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from platform_v2 import deployment, queue_worker
from platform_v2.bootstrap import provision_initial_admin
from platform_v2.db import Config, factory
from platform_v2.models import Base, Channel, Event, User, Workspace
from platform_v2.security import passwords

def test_unconfigured_backend_returns_json(monkeypatch):
    monkeypatch.delenv("ITURA_DATABASE_URL", raising=False)
    client = TestClient(deployment.build_app())
    assert client.get("/health/live").status_code == 200
    response = client.post("/api/auth/login", json={})
    assert response.status_code == 503
    assert "configuration" in response.json()["detail"]

def test_database_failure_returns_json(monkeypatch):
    monkeypatch.setenv("ITURA_LOCAL_DEVELOPMENT", "1")
    monkeypatch.setenv("ITURA_DATABASE_URL", "sqlite://")
    monkeypatch.setenv("ITURA_ORIGIN", "http://localhost")
    monkeypatch.delenv("ITURA_INITIAL_ADMIN_PASSWORD", raising=False)
    client = TestClient(deployment.build_app(), base_url="http://localhost")
    response = client.get("/api/auth/me")
    assert response.status_code == 401
    response = client.post("/api/auth/login", json={"email":"admin@example.com", "password":"example-password"}, headers={"origin":"http://localhost"})
    assert response.status_code == 503
    assert "Database service" in response.json()["detail"]

def test_initial_admin_is_created_once_without_reset(tmp_path, monkeypatch):
    config = Config("sqlite:///" + str(tmp_path / "bootstrap.db"), "http://localhost", False)
    engine, sessions = factory(config)
    Base.metadata.create_all(engine)
    monkeypatch.setenv("ITURA_INITIAL_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("ITURA_INITIAL_ADMIN_PASSWORD", "synthetic-password-only")
    provision_initial_admin(config)
    monkeypatch.setenv("ITURA_INITIAL_ADMIN_PASSWORD", "changed-synthetic-password")
    provision_initial_admin(config)
    with sessions() as db:
        assert db.query(User).count() == 1
        assert db.query(Workspace).count() == 1
        user = db.query(User).one()
        assert passwords.verify(user.password_hash, "synthetic-password-only")
        assert not db.query(Workspace).one().automation_enabled
    engine.dispose()

def test_cloud_startup_provisions_a_working_login(tmp_path, monkeypatch):
    url = "sqlite:///" + str(tmp_path / "cloud.db")
    engine, _ = factory(Config(url, "http://localhost", False))
    Base.metadata.create_all(engine)
    engine.dispose()
    monkeypatch.setenv("ITURA_DATABASE_URL", url)
    monkeypatch.setenv("ITURA_ORIGIN", "http://localhost")
    monkeypatch.setenv("ITURA_LOCAL_DEVELOPMENT", "1")
    monkeypatch.setenv("ITURA_INITIAL_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("ITURA_INITIAL_ADMIN_PASSWORD", "synthetic-password-only")
    with TestClient(deployment.build_app(), base_url="http://localhost") as client:
        response = client.post("/api/auth/login", json={"email":"admin@example.com", "password":"synthetic-password-only"}, headers={"origin":"http://localhost"})
        assert response.status_code == 200
        assert client.get("/api/auth/me").status_code == 200
        assert "HttpOnly" in response.headers["set-cookie"]

def test_queue_wakeup_respects_due_time_and_empty_state(tmp_path, monkeypatch):
    engine, sessions = factory(Config("sqlite:///" + str(tmp_path / "queue.db"), "http://localhost", False))
    Base.metadata.create_all(engine)
    send = AsyncMock()
    monkeypatch.setattr(queue_worker, "send", send)
    asyncio.run(queue_worker.wake_if_needed(sessions))
    send.assert_not_awaited()
    with sessions() as db:
        workspace = Workspace(name="Synthetic")
        db.add(workspace); db.flush()
        channel = Channel(workspace_id=workspace.id, name="Web", provider="web", config={})
        db.add(channel); db.flush()
        db.add(Event(channel_id=channel.id, provider_id="one", kind="message", payload={}, state="pending", available_at=time.time() + 45))
        db.commit()
    asyncio.run(queue_worker.wake_if_needed(sessions))
    assert send.await_count == 1
    assert 40 <= send.call_args.kwargs["delay"].total_seconds() <= 46
    assert send.call_args.kwargs["retention"].total_seconds() == 86400
    engine.dispose()
