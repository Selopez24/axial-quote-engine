"""Quote retrieval.

Three shapes:
  GET /quotes?customer=C-10029              -> all quotes for the company
  GET /quotes?conversation=wa-thread-662    -> all quotes that thread touches
  GET /quotes/Q-001                         -> one quote

`?history=true` adds per-line provenance (which message created the line, which
message last changed its quantity) plus, for a single quote, the change records
stored against those messages — so a human can read "what changed and why".
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import repositories as repo
from app.database import get_db
from app.models import ProcessedMessage

router = APIRouter()


def _change_log(db: Session, quote) -> list[dict]:
    message_ids = [
        li.last_changed_by_message_id
        for li in quote.line_items
        if li.last_changed_by_message_id
    ]
    if not message_ids:
        return []

    records = db.scalars(
        select(ProcessedMessage).where(ProcessedMessage.message_id.in_(message_ids))
    ).all()
    log = []
    for record in records:
        result = record.processing_result or {}
        log.append(
            {
                "message_id": record.message_id,
                "intent": result.get("intent"),
                "quote_id": result.get("quote_id"),
                "changes": result.get("changes", []),
            }
        )
    return log


@router.get("/quotes")
def list_quotes(
    customer: str | None = Query(None, description="customer_id, e.g. C-10029"),
    conversation: str | None = Query(None, description="conversation_id, e.g. wa-thread-662"),
    history: bool = Query(False),
    db: Session = Depends(get_db),
):
    """Quotes by company or by conversation — the many-to-many works both ways.

    Narrowing by conversation is what lets a rep see "what is this chat about?"
    without knowing the customer id, which is the other half of the linkage
    requirement.
    """
    if customer is None and conversation is None:
        raise HTTPException(status_code=400, detail="customer or conversation is required")

    if conversation is not None:
        quotes = repo.get_quotes_for_conversation(db, conversation)
        if customer is not None:
            quotes = [q for q in quotes if q.customer_id == customer]
    else:
        quotes = repo.get_all_quotes_for_customer(db, customer)

    return {"quotes": [repo.quote_to_dict(db, q, include_history=history) for q in quotes]}


@router.get("/quotes/{quote_id}")
def get_quote(quote_id: str, history: bool = Query(False), db: Session = Depends(get_db)):
    quote = repo.get_quote(db, quote_id)
    if quote is None:
        raise HTTPException(status_code=404, detail=f"quote {quote_id} not found")

    data = repo.quote_to_dict(db, quote, include_history=history)
    if history:
        data["change_log"] = _change_log(db, quote)
    return data
