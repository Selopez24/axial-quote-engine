"""The SKU catalog definition.

At runtime the catalog is the `catalog_items` table — that is what the validator
filters against and what the prompt is built from, so the database is the single
authority on a valid SKU (the assignment's "mock DB").

This dict is the seed source: `app/seed.py` and `db/seed.sql` both derive from
it, and tests/test_seed.py asserts the table and this dict agree, so the two
representations cannot drift apart.
"""

CATALOG = {
    "ALFBTC-013-320100": "Lona Frontlit BTE 13 3.20x100",
    "ALMCLC-0909-320100": "Lona Mesh CLN 9x9 3.20x100",
    "AVMICS-1A-137050": "Vinil Adhesivo Microperforado 1.37x50",
    "ARACBL-03-122244": "Acrilico Blanco 3mm 1.22x2.44",
}

VALID_SKUS = set(CATALOG.keys())
