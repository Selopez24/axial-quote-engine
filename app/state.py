"""State application: turns a validated extraction into rows.

This is where the many-to-many model earns its keep. Note what does *not*
happen here: the arriving conversation never decides which quote is touched.
The validated `target_quote_id` does, and then we add a link from whatever
conversation the message came in on. That single asymmetry is what makes
events 2, 3 and 4 fall out of the same code path instead of needing
special cases.
"""

from sqlalchemy.orm import Session

from app.repositories import (
    create_pending_review,
    create_quote,
    get_pending_review,
    link_quote_conversation,
    upsert_line_item,
)
from app.schemas import ExtractionResult


def apply_validated(
    db: Session,
    *,
    customer_id: str,
    conversation_id: str,
    message_id: str,
    result: ExtractionResult,
    validation: dict,
) -> dict:
    """Apply a message that passed validation; returns the audit record.

    The target quote's existence is not re-checked here: the validator confirmed
    it is open, in this same transaction, and the FKs on line_items and
    quote_conversations are the backstop. A rejected write rolls the message
    back, leaving it unprocessed and retryable.
    """
    intent = result.intent
    quote_id: str | None = None

    if intent == "new_quote":
        quote_id = create_quote(db, customer_id, created_by_message_id=message_id).quote_id
    elif intent in ("revision", "reference"):
        quote_id = validation["target_quote_id"]

    changes: list[dict] = []
    linked = False
    if quote_id:
        for line in validation["clean_lines"]:
            changes.append(
                upsert_line_item(db, quote_id, line.sku, line.quantity, message_id=message_id)
            )
        linked = link_quote_conversation(
            db,
            quote_id,
            conversation_id,
            reason=f"message {message_id} classified as {intent}",
        )

    return {
        "intent": intent,
        "quote_id": quote_id,
        "changes": changes,
        "new_link": linked,
        "confidence": result.confidence,
    }


def route_to_review(
    db: Session,
    *,
    conversation_id: str,
    customer_id: str,
    message: dict,
    result: ExtractionResult | None,
    validation: dict,
) -> dict:
    """Don't guess: park the message for a human, change nothing else.

    The review id is derived from the message id, so a webhook redelivery of a
    message we could not classify reuses the same review instead of stacking
    duplicates. Failed extraction deliberately leaves the message unprocessed,
    so it can be retried once the model is reachable again.
    """
    message_id = message.get("message_id") or "unknown"
    review_id = f"REV-{message_id}"

    existing = get_pending_review(db, review_id)
    if existing is not None:
        extraction = existing.llm_extraction or {}
        return {
            "intent": extraction.get("intent", "not_extracted"),
            "quote_id": None,
            "changes": [],
            "requires_review": True,
            "review_id": review_id,
            "issues": existing.resolution_issues or [],
            "candidates": existing.candidate_quotes or [],
            "already_reviewed": True,
        }

    create_pending_review(
        db,
        review_id=review_id,
        conversation_id=conversation_id,
        customer_id=customer_id,
        original_message=message,
        llm_extraction=result.model_dump() if result else None,
        resolution_issues=validation["issues"],
        candidate_quotes=validation["candidates"],
    )
    return {
        "intent": result.intent if result else "not_extracted",
        "quote_id": None,
        "changes": [],
        "requires_review": True,
        "review_id": review_id,
        "issues": validation["issues"],
        "candidates": validation["candidates"],
    }
