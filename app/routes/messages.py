"""POST /messages — the production entry point (one message per call)."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.pipeline import process_message
from app.schemas import MessageIn

router = APIRouter()


@router.post("/messages")
def ingest_message(payload: MessageIn, db: Session = Depends(get_db)):
    """Run one message through the pipeline in a single transaction.

    Validation of required fields happens in FastAPI via MessageIn; the app-level
    handler in main.py renders those as 400 with field details, so callers branch
    on one status for every bad input.
    """
    try:
        result = process_message(db, payload)
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"pipeline error: {exc}") from exc

    db.commit()
    return result
