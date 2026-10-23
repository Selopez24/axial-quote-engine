"""POST /conversations — batch entry point for testing/demonstration.

Accepts the assignment's Phase 1 shape (a full conversation_history array) and
feeds each message through the same pipeline used in production, in timestamp
order. It is a convenience wrapper, not a second implementation: the per-message
logic lives entirely in app/pipeline.py.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import repositories as repo
from app.database import get_db
from app.extraction import display_name
from app.pipeline import process_message
from app.schemas import ConversationIn, MessageIn

router = APIRouter()


def _to_messages(payload: ConversationIn) -> list[MessageIn]:
    customer = display_name(payload.customer_name, "Cliente")
    rep = payload.rep_name or "Vendedor"

    ordered = sorted(
        payload.conversation_history,
        key=lambda m: (m.timestamp is None, m.timestamp),
    )

    messages = []
    for index, item in enumerate(ordered):
        message_id = item.message_id or f"wamid.{payload.conversation_id}.{index + 1:04d}"
        messages.append(
            MessageIn(
                message_id=message_id,
                conversation_id=payload.conversation_id,
                customer_id=payload.customer_id,
                customer_name=payload.customer_name,
                sender_name=item.sender_name or (customer if item.direction == "inbound" else rep),
                direction=item.direction,
                text=item.text,
                timestamp=item.timestamp,
            )
        )
    return messages


@router.post("/conversations")
def ingest_conversation(payload: ConversationIn, db: Session = Depends(get_db)):
    results = []
    try:
        for message in _to_messages(payload):
            results.append(process_message(db, message))
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"pipeline error: {exc}") from exc

    db.commit()
    # Spec (message-processing): the batch endpoint returns the resulting quote
    # state, not just per-message outcomes — the assignment's Phase 1 "expected
    # output" should be readable straight from this response.
    quotes = [repo.quote_to_dict(db, q) for q in repo.get_quotes_for_conversation(db, payload.conversation_id)]
    return {
        "conversation_id": payload.conversation_id,
        "processed": len(results),
        "results": results,
        "quotes": quotes,
    }
