from dataclasses import dataclass
import os
import re

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

@dataclass(frozen=True)
class Config:
    database_url: str
    origin: str
    secure_cookies: bool = True
    session_hours: int = 12
    max_body_bytes: int = 1048576
    database_schema: str | None = None
    additional_origins: tuple[str, ...] = ()

    def __post_init__(self):
        if self.database_schema and not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", self.database_schema):
            raise ValueError("Invalid database schema")
        if self.database_schema in {"public", "auth", "storage", "kpi_private", "pg_catalog"}:
            raise ValueError("Use a dedicated chatbot schema")

    @classmethod
    def from_env(cls):
        url = os.environ.get("ITURA_DATABASE_URL", "")
        origin = os.environ.get("ITURA_ORIGIN", "")
        deployment_hosts = []
        if os.environ.get("VERCEL") == "1":
            for key in ("VERCEL_URL", "VERCEL_BRANCH_URL"):
                host = os.environ.get(key, "")
                if re.fullmatch(r"[a-z0-9-]+\.vercel\.app", host):
                    deployment_hosts.append("https://" + host)
            if not origin and deployment_hosts:
                origin = deployment_hosts[0]
        if not url or not origin:
            raise RuntimeError("ITURA_DATABASE_URL and ITURA_ORIGIN are required")
        secure = os.environ.get("ITURA_LOCAL_DEVELOPMENT") != "1"
        if secure and (not url.startswith("postgresql") or not origin.startswith("https://")):
            raise RuntimeError("Production requires PostgreSQL and an HTTPS origin")
        schema = os.environ.get("ITURA_DATABASE_SCHEMA", "itura" if secure else "") or None
        if secure and not schema:
            raise RuntimeError("Production requires a dedicated database schema")
        return cls(url, origin.rstrip("/"), secure, database_schema=schema,
                   additional_origins=tuple(deployment_hosts))

def factory(config):
    options = {"pool_pre_ping": True}
    if config.database_url.startswith("sqlite"):
        options["connect_args"] = {"check_same_thread": False}
    elif config.database_schema:
        # Qualify ORM tables explicitly: poolers may ignore startup search_path options.
        options["execution_options"] = {"schema_translate_map": {None: config.database_schema}}
        options["pool_size"] = 2
        options["max_overflow"] = 2
    engine = create_engine(config.database_url, **options)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
    elif config.database_schema:
        @event.listens_for(engine, "connect")
        def private_search_path(connection, _):
            previous = connection.autocommit
            connection.autocommit = True
            try:
                with connection.cursor() as cursor:
                    cursor.execute(f"SET SESSION search_path TO {config.database_schema}, pg_catalog")
            finally:
                connection.autocommit = previous
    return engine, sessionmaker(engine, expire_on_commit=False)
