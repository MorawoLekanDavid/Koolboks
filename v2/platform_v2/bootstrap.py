"""One-time local provisioning. No HTTP bootstrap/password-reset bypass."""
import getpass
import os
from sqlalchemy import text
from .db import Config, factory
from .models import User, Workspace
from .security import passwords

def provision_initial_admin(config):
    """Seed once from deployment secrets. Never reset or replace an existing account."""
    secret = os.environ.get("ITURA_INITIAL_ADMIN_PASSWORD", "")
    email = os.environ.get("ITURA_INITIAL_ADMIN_EMAIL", "").strip().lower()
    if not secret:
        return
    if len(secret) < 12 or "@" not in email:
        raise RuntimeError("Initial administrator configuration is invalid")
    engine, sessions = factory(config)
    try:
        with sessions() as db:
            if db.bind.dialect.name == "postgresql":
                db.execute(text("SELECT pg_advisory_xact_lock(73320, 1)"))
            if db.query(User).first() or db.query(Workspace).first():
                return
            workspace = Workspace(name="ITURA", settings={"welcome":"", "instruction":"", "knowledge":"", "model":"", "ai_secret_ref":""})
            db.add(workspace); db.flush()
            db.add(User(workspace_id=workspace.id, email=email, name="Administrator", role="admin", password_hash=passwords.hash(secret)))
            db.commit()
    finally:
        engine.dispose()

def run():
    _, sessions = factory(Config.from_env())
    name = input("Workspace name: ").strip()
    email = input("Administrator email: ").strip().lower()
    person = input("Administrator name: ").strip()
    password = getpass.getpass("Password (12+ characters): ")
    if not name or "@" not in email or not person or len(password) < 12:
        raise SystemExit("Valid workspace, name, email and password required")
    with sessions() as db:
        if db.query(User).filter_by(email=email).first(): raise SystemExit("Account already exists")
        workspace = Workspace(name=name, settings={"welcome": "", "instruction": "", "knowledge": "", "model": "", "ai_secret_ref": ""})
        db.add(workspace); db.flush()
        db.add(User(workspace_id=workspace.id, email=email, name=person, role="admin", password_hash=passwords.hash(password)))
        db.commit()
    print("Workspace created. Automation is disabled until approved configuration is published.")

if __name__ == "__main__": run()
