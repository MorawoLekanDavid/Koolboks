import hashlib
import hmac
import secrets
import time

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError
from fastapi import HTTPException
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .models import RateLimit

passwords = PasswordHasher()

def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()

def token():
    return secrets.token_urlsafe(32)

def verify_password(stored, candidate):
    try:
        return passwords.verify(stored, candidate)
    except (VerificationError, InvalidHashError):
        return False

def signature_ok(body, supplied, secret):
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return bool(secret and supplied and hmac.compare_digest(expected, supplied))

def throttle(db, key, limit, seconds):
    now = time.time()
    key = digest(key + ":" + str(int(now // seconds)))
    insert = pg_insert if db.bind.dialect.name == "postgresql" else sqlite_insert
    statement = insert(RateLimit).values(key=key, count=1, expires_at=now + seconds)
    statement = statement.on_conflict_do_update(index_elements=["key"], set_={"count": RateLimit.count + 1})
    db.execute(statement)
    row = db.get(RateLimit, key)
    db.commit()
    if row.count > limit:
        raise HTTPException(429, "Too many requests; please try again later")
