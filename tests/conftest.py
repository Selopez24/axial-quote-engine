"""Test fixtures.

Two things worth knowing:

1. Tests run against a separate Postgres database (quote_engine_test) so dev
   data stays untouched, and every test rolls its work back, so cases never
   leak into each other.
2. The LLM is replaced with a deterministic stand-in (tests/fake_llm.py).
   Phase 2's actual engineering is the layer *around* the model: quote
   resolution, conversation linking, revision application, dedup. Those are
   testable without network calls. tests/test_integration_llm.py runs the same
   scenario through the real model and skips when no API key is present.
"""

import os

# The app seeds reference data at import time by default. Tests point at their
# own database and seed it in the `engine` fixture, so opt out before app.main
# is imported — otherwise running pytest writes catalog rows into the dev DB.
os.environ.setdefault("QUOTE_ENGINE_AUTOINIT", "0")

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.main import app

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+psycopg2://postgres:postgres@localhost:5432/quote_engine_test",
)


def _ensure_database():
    admin_url = TEST_DATABASE_URL.rsplit("/", 1)[0] + "/postgres"
    db_name = TEST_DATABASE_URL.rsplit("/", 1)[1]
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": db_name}
        ).scalar()
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    admin.dispose()


@pytest.fixture(scope="session")
def engine():
    _ensure_database()
    eng = create_engine(TEST_DATABASE_URL)
    import app.models  # noqa: F401  (register tables on Base)

    Base.metadata.drop_all(bind=eng)
    Base.metadata.create_all(bind=eng)

    # The app loads reference data on boot; tests skip that (auto-init off), so
    # they seed here or the catalog is empty and every SKU fails validation.
    from app.seed import seed_reference_data

    session = sessionmaker(bind=eng)()
    try:
        seed_reference_data(session)
        session.commit()
    finally:
        session.close()

    yield eng
    eng.dispose()


@pytest.fixture()
def tx(engine):
    """One connection + one open transaction, shared by db/client per test.

    join_transaction_mode="create_savepoint" is what makes the routes' internal
    db.commit() safe inside a test: it releases a savepoint instead of really
    committing, so the outer transaction can roll everything back afterwards.
    Sharing a single connection is what lets a test read back rows the client
    wrote through an endpoint.
    """
    connection = engine.connect()
    transaction = connection.begin()
    factory = sessionmaker(
        bind=connection,
        autoflush=False,
        autocommit=False,
        join_transaction_mode="create_savepoint",
    )
    yield SimpleNamespace(connection=connection, transaction=transaction, factory=factory)
    transaction.rollback()
    connection.close()


@pytest.fixture()
def db(tx):
    """Direct session for reading back state. Open it, use it, close it."""
    session = tx.factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(tx):
    def override_get_db():
        session = tx.factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
