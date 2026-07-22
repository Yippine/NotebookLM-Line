from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from cryptography.fernet import Fernet


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def backend_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every database-aware singleton at an isolated SQLite file."""

    import database
    from config import settings
    from routers import auth, webhook
    from services import course_account_service, nlm_service
    from services.crypto_service import reset_encryption_cache
    from services.rate_limit_service import limiter

    db_path = tmp_path / "backend.db"
    monkeypatch.setattr(settings, "db_path", str(db_path))
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(
        settings, "encryption_key", Fernet.generate_key().decode("ascii")
    )
    monkeypatch.setattr(database, "DB", str(db_path))
    monkeypatch.setattr(auth, "DB", str(db_path))
    monkeypatch.setattr(webhook, "DB", str(db_path))
    monkeypatch.setattr(nlm_service, "DB", str(db_path))
    monkeypatch.setattr(nlm_service.channel_binding_repository, "db_path", str(db_path))
    monkeypatch.setattr(course_account_service, "DB", str(db_path))
    monkeypatch.setattr(
        course_account_service.course_account_repository, "db_path", str(db_path)
    )
    monkeypatch.setattr(
        course_account_service.course_account_service.repository,
        "db_path",
        str(db_path),
    )
    reset_encryption_cache()
    asyncio.run(database.init_db(str(db_path)))
    asyncio.run(limiter.reset())

    yield db_path

    reset_encryption_cache()
    asyncio.run(limiter.reset())


@pytest.fixture
def asgi_app(backend_db: Path):
    del backend_db
    from main import app

    return app
