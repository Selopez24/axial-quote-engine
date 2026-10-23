-- Reference data only: the mock catalog and the customer master.
--
-- Deliberately NOT seeded: conversations, quotes, line items, pending reviews.
-- That is the state the assignment asks the pipeline to produce, so it comes
-- from POST /conversations and POST /messages, not from a fixture.
--
-- Idempotent, and applied automatically at app startup (app/seed.py holds the
-- same rows as Python, which is the definition this file mirrors).
--
-- psql -d quote_engine -f db/seed.sql

INSERT INTO catalog_items (sku, description) VALUES
    ('ALFBTC-013-320100', 'Lona Frontlit BTE 13 3.20x100'),
    ('ALMCLC-0909-320100', 'Lona Mesh CLN 9x9 3.20x100'),
    ('AVMICS-1A-137050',   'Vinil Adhesivo Microperforado 1.37x50'),
    ('ARACBL-03-122244',   'Acrilico Blanco 3mm 1.22x2.44')
ON CONFLICT (sku) DO NOTHING;

-- customer_name is left NULL on purpose: the person is whoever the first
-- message names (Alice, then Mario), while the company is known up front.
-- Decision 9: identity is recorded once and then kept stable.
INSERT INTO customers (customer_id, customer_name, company_name) VALUES
    ('C-10029', NULL, 'ACME INDUSTRIAL')
ON CONFLICT (customer_id) DO NOTHING;
