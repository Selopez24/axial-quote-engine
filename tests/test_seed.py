"""The seed is reference data only, and the two seed paths cannot disagree.

db/seed.sql exists so a reviewer can prefill with psql. app/seed.py exists so the
app prefills itself. Two representations of the same rows is a drift hazard, so
these tests are the guard: the SQL file must load against the current schema and
must produce exactly what CATALOG and CUSTOMERS declare.
"""

import re
from pathlib import Path

import pytest
from sqlalchemy import text

from app.catalog import CATALOG
from app.seed import CUSTOMERS, seed_reference_data

SEED_SQL = Path(__file__).resolve().parent.parent / "db" / "seed.sql"


def apply_seed_sql(session) -> None:
    """psycopg2 runs the file's statements as one multi-statement string."""
    session.execute(text(SEED_SQL.read_text()))
    session.flush()


@pytest.fixture()
def cleared(tx):
    """Empty reference tables, rolled back with the test.

    Clearing first is what makes the parity meaningful: ON CONFLICT DO NOTHING
    would otherwise silently hide a description that drifted.
    """
    session = tx.factory()
    try:
        session.execute(text("DELETE FROM line_items"))
        session.execute(text("DELETE FROM pending_reviews"))
        session.execute(text("DELETE FROM processed_messages"))
        session.execute(text("DELETE FROM quote_conversations"))
        session.execute(text("DELETE FROM quotes"))
        session.execute(text("DELETE FROM conversations"))
        session.execute(text("DELETE FROM customers"))
        session.execute(text("DELETE FROM catalog_items"))
        session.flush()
        yield session
    finally:
        session.close()


def test_seed_sql_loads_against_the_current_schema(cleared):
    """Catches a renamed column or a new NOT NULL the SQL file never learned about."""
    apply_seed_sql(cleared)


def test_seed_sql_matches_the_python_catalog_definition(cleared):
    from app.repositories import catalog_items

    apply_seed_sql(cleared)
    assert catalog_items(cleared) == CATALOG


def test_seed_sql_matches_the_python_customer_master(cleared):
    from app.models import Customer

    apply_seed_sql(cleared)

    seeded = {
        (c.customer_id, c.customer_name, c.company_name)
        for c in cleared.query(Customer)
    }
    assert seeded == {(cid, name, company) for cid, name, company in CUSTOMERS}

    # C-10029 is known as a company before anyone messages; the person is not
    # guessed. The first message that carries a name fills it in.
    acme = cleared.get(Customer, "C-10029")
    assert acme.company_name == "ACME INDUSTRIAL"
    assert acme.customer_name is None


def test_both_seed_paths_produce_the_same_rows(cleared):
    """app/seed.py and db/seed.sql must be interchangeable."""
    seed_reference_data(cleared)
    cleared.flush()
    via_python = (
        {r.sku: r.description for r in cleared.execute(text("SELECT sku, description FROM catalog_items")).all()},
        {
            (r[0], r[1], r[2])
            for r in cleared.execute(text("SELECT customer_id, customer_name, company_name FROM customers")).all()
        },
    )

    cleared.execute(text("DELETE FROM catalog_items"))
    cleared.execute(text("DELETE FROM customers"))
    apply_seed_sql(cleared)
    via_sql = (
        {r[0]: r[1] for r in cleared.execute(text("SELECT sku, description FROM catalog_items")).all()},
        {
            (r[0], r[1], r[2])
            for r in cleared.execute(text("SELECT customer_id, customer_name, company_name FROM customers")).all()
        },
    )

    assert via_python == via_sql


def test_seeding_twice_changes_nothing(cleared):
    """Boots run the seed on every start, so it has to be idempotent."""
    seed_reference_data(cleared)
    cleared.flush()
    first = (
        cleared.execute(text("SELECT count(*) FROM catalog_items")).scalar(),
        cleared.execute(text("SELECT count(*) FROM customers")).scalar(),
    )

    seed_reference_data(cleared)
    apply_seed_sql(cleared)

    second = (
        cleared.execute(text("SELECT count(*) FROM catalog_items")).scalar(),
        cleared.execute(text("SELECT count(*) FROM customers")).scalar(),
    )
    assert first == second == (len(CATALOG), len(CUSTOMERS))


def test_no_conversation_or_quote_state_is_seeded(cleared):
    """The point of the seed depth decision: transcripts and quotes are the
    thing under evaluation, so they must come from the endpoints, not a fixture."""
    apply_seed_sql(cleared)

    for table in ("conversations", "quotes", "line_items", "processed_messages", "pending_reviews"):
        assert cleared.execute(text(f"SELECT count(*) FROM {table}")).scalar() == 0, f"{table} must not be seeded"


def test_startup_seeding_makes_a_sku_legal(cleared):
    """Before seeding, no SKU validates; after, the seeded ones do.

    This documents the one behavior the catalog-as-table changes: an unseeded
    database is the safe direction (everything routes to review), not a crash.
    """
    from app.validator import validate
    from app.schemas import ExtractionResult

    result = ExtractionResult(
        intent="new_quote",
        confidence=0.9,
        target_quote_id=None,
        line_items=[{"sku": "ALFBTC-013-320100", "quantity": 10}],
        mentions_new_request=True,
        reason="test",
    )

    empty = validate(cleared, "C-10029", result)
    assert "unknown_sku" in empty["issues"]
    assert empty["clean_lines"] == []

    seed_reference_data(cleared)
    cleared.flush()
    seeded = validate(cleared, "C-10029", result)
    assert "unknown_sku" not in seeded["issues"]
    assert [line.sku for line in seeded["clean_lines"]] == ["ALFBTC-013-320100"]


def test_seed_file_has_only_expected_statements():
    """A reviewer reads this file. It should contain INSERTs, nothing else.

    No CREATE TABLE: the SQLAlchemy models own the schema, and duplicating DDL
    here is exactly the drift this whole file is meant to avoid.
    """
    sql = SEED_SQL.read_text()
    statements = [
        line.strip()
        for line in re.split(r";\s*\n", re.sub(r"--[^\n]*", "", sql))
        if line.strip()
    ]
    assert all(s.upper().startswith("INSERT INTO") for s in statements), statements
    assert not re.search(r"CREATE|ALTER|DROP|DELETE|TRUNCATE", sql, re.I)
