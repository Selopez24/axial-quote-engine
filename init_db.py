#!/usr/bin/env python3
"""Initialize database tables"""
from app.database import init_db, engine
from sqlalchemy import text

print("Creating database tables...")
init_db()
print("✓ Tables created successfully")

print("\nVerifying connection...")
with engine.connect() as conn:
    result = conn.execute(text("SELECT current_database(), current_user"))
    db_name, user = result.fetchone()
    print(f"✓ Connected to database: {db_name} as user: {user}")

print("\nListing tables:")
with engine.connect() as conn:
    result = conn.execute(text("""
        SELECT table_name 
        FROM information_schema.tables 
        WHERE table_schema = 'public' 
        ORDER BY table_name
    """))
    tables = [row[0] for row in result]
    for table in tables:
        print(f"  - {table}")

print(f"\n✓ Total tables created: {len(tables)}")
