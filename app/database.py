from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/quote_engine")

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def init_db():
    """Create all tables and load reference data.

    Seeding runs on every boot and is idempotent, so a fresh checkout has a
    populated catalog and customer master before the first request. Quote and
    conversation state is never seeded — that comes from the endpoints.
    """
    import app.models
    from app.seed import seed_reference_data

    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        seed_reference_data(session)
        session.commit()
    finally:
        session.close()
