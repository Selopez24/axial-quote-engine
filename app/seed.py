"""Reference data for the mock catalog and known customers.

Two things are deliberately the only things seeded:

- the SKU catalog (the assignment's "mock DB"), which the deterministic
  validation layer reads before any line item is written
- the customer master, so the system knows C-10029 is ACME INDUSTRIAL before a
  single message arrives

Transcripts, quotes and line items are never seeded. That state is the thing
under evaluation, so it has to come out of the pipeline: `POST /conversations`
loads the assignment's example thread, `POST /messages` runs the four events.

`db/seed.sql` carries the same rows for reviewers who prefer psql. tests/test_seed.py
loads that file and asserts it agrees with CATALOG, so the two cannot drift.
"""

from sqlalchemy import text

from app.catalog import CATALOG

# customer_id, person name (filled in by the first message that carries one),
# company name
CUSTOMERS = [
    ("C-10029", None, "ACME INDUSTRIAL"),
]


def seed_reference_data(session) -> None:
    """Idempotent: safe to run on every boot, never overwrites real work."""
    for sku, description in sorted(CATALOG.items()):
        session.execute(
            text(
                "INSERT INTO catalog_items (sku, description) "
                "VALUES (:sku, :description) ON CONFLICT (sku) DO NOTHING"
            ),
            {"sku": sku, "description": description},
        )

    for customer_id, customer_name, company_name in CUSTOMERS:
        session.execute(
            text(
                "INSERT INTO customers (customer_id, customer_name, company_name) "
                "VALUES (:customer_id, :customer_name, :company_name) "
                "ON CONFLICT (customer_id) DO NOTHING"
            ),
            {
                "customer_id": customer_id,
                "customer_name": customer_name,
                "company_name": company_name,
            },
        )

    session.flush()
