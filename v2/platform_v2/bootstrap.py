"""One-time local provisioning. No HTTP bootstrap/password-reset bypass."""
import getpass
from .db import Config, factory
from .models import User, Workspace
from .security import passwords

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
