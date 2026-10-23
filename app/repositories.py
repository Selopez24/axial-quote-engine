"""Repository helpers: the only code that touches SQLAlchemy objects.

Each function takes an open session and is deliberately simple — no
unit-of-work abstraction, no repository classes. The pipeline orchestrator
calls these in order inside a single transaction.
"""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    CatalogItem,
    Conversation,
    Customer,
    LineItem,
    PendingReview,
    ProcessedMessage,
    Quote,
    QuoteConversation,
)

OPEN_QUOTE_STATUSES = ("draft", "pending_review")


# ---------- Customer ----------

def get_or_create_customer(db: Session, customer_id: str, customer_name: str | None = None) -> Customer:
    """Store identity on first encounter, reuse afterwards.

    We never overwrite a name we already have (Decision 9: identity is recorded
    once and stays stable), but we do backfill when the first sighting came in
    without one — state-management requires identity that was not previously
    stored to be stored when it does arrive.
    """
    customer = db.get(Customer, customer_id)
    if customer is None:
        customer = Customer(customer_id=customer_id, customer_name=customer_name)
        db.add(customer)
        db.flush()
    elif customer_name and not customer.customer_name:
        customer.customer_name = customer_name
        db.flush()
    return customer


def get_customer(db: Session, customer_id: str) -> Customer | None:
    return db.get(Customer, customer_id)


# ---------- Catalog (the assignment's mock DB) ----------

def catalog_items(db: Session) -> dict[str, str]:
    """{sku: description}. Empty means unseeded, which makes every SKU invalid."""
    return {item.sku: item.description for item in db.scalars(select(CatalogItem))}


# ---------- Conversation ----------

def get_or_create_conversation(db: Session, conversation_id: str, customer_id: str) -> Conversation:
    conv = db.get(Conversation, conversation_id)
    if conv is None:
        conv = Conversation(conversation_id=conversation_id, customer_id=customer_id, messages=[])
        db.add(conv)
        db.flush()
    return conv


def append_message(db: Session, conv: Conversation, message: dict) -> Conversation:
    """Append to the JSONB array.

    Reassignment is required: SQLAlchemy does not track in-place mutation of a
    JSON column, so `conv.messages.append(...)` would never be flushed.

    Guarded by message_id: a message whose extraction failed is deliberately
    left unprocessed so it can be retried, and a redelivery must not append the
    same text twice and feed the prompt a fake duplicate.
    """
    existing = list(conv.messages or [])
    message_id = message.get("message_id")
    if message_id and any(m.get("message_id") == message_id for m in existing):
        return conv
    conv.messages = existing + [message]
    db.flush()
    return conv


def get_conversation(db: Session, conversation_id: str) -> Conversation | None:
    return db.get(Conversation, conversation_id)


def recent_messages(conv: Conversation, limit: int = 20) -> list[dict]:
    """Last N messages of a conversation, in chronological order."""
    return list(conv.messages or [])[-limit:]


# ---------- Deduplication / audit ----------

def is_message_processed(db: Session, message_id: str) -> bool:
    return db.get(ProcessedMessage, message_id) is not None


def mark_message_processed(db: Session, message_id: str, conversation_id: str, result: dict) -> ProcessedMessage:
    record = ProcessedMessage(
        message_id=message_id,
        conversation_id=conversation_id,
        processing_result=result,
    )
    db.add(record)
    db.flush()
    return record


# ---------- Quotes ----------

def next_quote_id(db: Session, customer_id: str) -> str:
    """Sequential per-customer id: Q-001, Q-002, ..."""
    existing = db.scalars(select(Quote.quote_id).where(Quote.customer_id == customer_id)).all()
    used = set(existing)
    n = len(used) + 1
    while True:
        candidate = f"Q-{n:03d}"
        if candidate not in used:
            return candidate
        n += 1


def create_quote(db: Session, customer_id: str, created_by_message_id: str | None = None) -> Quote:
    quote = Quote(
        quote_id=next_quote_id(db, customer_id),
        customer_id=customer_id,
        status="draft",
        metadata_={"created_by_message_id": created_by_message_id},
    )
    db.add(quote)
    db.flush()
    return quote


def get_quote(db: Session, quote_id: str) -> Quote | None:
    return db.get(Quote, quote_id)


def get_open_quotes_for_customer(db: Session, customer_id: str) -> list[Quote]:
    stmt = (
        select(Quote)
        .where(Quote.customer_id == customer_id, Quote.status.in_(OPEN_QUOTE_STATUSES))
        .order_by(Quote.created_at)
    )
    return list(db.scalars(stmt))


def get_all_quotes_for_customer(db: Session, customer_id: str) -> list[Quote]:
    stmt = select(Quote).where(Quote.customer_id == customer_id).order_by(Quote.quote_id)
    return list(db.scalars(stmt))


# ---------- Line items ----------

def upsert_line_item(
    db: Session,
    quote_id: str,
    sku: str,
    quantity: int,
    message_id: str | None = None,
) -> dict:
    """Set the quantity for `sku` on `quote_id`, creating the row if needed.

    Returns the change record so the caller can persist it as audit trail:
    {"sku", "action": "add"|"update"|"noop", "from", "to"}
    """
    item = db.scalars(
        select(LineItem).where(LineItem.quote_id == quote_id, LineItem.sku == sku)
    ).first()

    if item is None:
        db.add(
            LineItem(
                quote_id=quote_id,
                sku=sku,
                quantity=quantity,
                created_by_message_id=message_id,
                last_changed_by_message_id=message_id,
            )
        )
        db.flush()
        return {"sku": sku, "action": "add", "from": None, "to": quantity}

    if item.quantity == quantity:
        return {"sku": sku, "action": "noop", "from": quantity, "to": quantity}

    old = item.quantity
    item.quantity = quantity
    item.last_changed_by_message_id = message_id
    db.flush()
    return {"sku": sku, "action": "update", "from": old, "to": quantity}


def cancel_quote(db: Session, quote_id: str, message_id: str | None = None) -> dict:
    """Cancel an entire quote by setting status to 'cancelled' and removing all line items.
    
    Returns audit record: {"quote_id", "action": "cancel", "line_items_deleted": count}
    """
    quote = db.get(Quote, quote_id)
    if quote is None:
        return {"quote_id": quote_id, "action": "cancel", "error": "not_found"}
    
    # Delete all line items before cancelling
    line_items_count = len(quote.line_items)
    for item in quote.line_items:
        db.delete(item)
    
    old_status = quote.status
    quote.status = "cancelled"
    if quote.metadata_ is None:
        quote.metadata_ = {}
    quote.metadata_["cancelled_by_message_id"] = message_id
    db.flush()
    return {
        "quote_id": quote_id, 
        "action": "cancel", 
        "from": old_status, 
        "to": "cancelled",
        "line_items_deleted": line_items_count
    }


def delete_line_items(
    db: Session,
    quote_id: str,
    skus: list[str],
    message_id: str | None = None,
) -> list[dict]:
    """Delete specific line items from a quote by SKU.
    
    Returns list of change records: [{"sku", "action": "delete", "from": qty, "to": None}]
    """
    changes = []
    for sku in skus:
        item = db.scalars(
            select(LineItem).where(LineItem.quote_id == quote_id, LineItem.sku == sku)
        ).first()
        
        if item is not None:
            old_qty = item.quantity
            db.delete(item)
            changes.append({"sku": sku, "action": "delete", "from": old_qty, "to": None})
    
    db.flush()
    return changes


# ---------- Quote <-> conversation links ----------

def link_quote_conversation(
    db: Session,
    quote_id: str,
    conversation_id: str,
    reason: str | None = None,
) -> bool:
    """Idempotent link. Returns True if a new link was created."""
    existing = db.scalars(
        select(QuoteConversation).where(
            QuoteConversation.quote_id == quote_id,
            QuoteConversation.conversation_id == conversation_id,
        )
    ).first()
    if existing is not None:
        return False

    db.add(
        QuoteConversation(
            quote_id=quote_id,
            conversation_id=conversation_id,
            metadata_={"reason": reason} if reason else None,
        )
    )
    db.flush()
    return True


def get_conversation_ids_for_quote(db: Session, quote_id: str) -> list[str]:
    stmt = (
        select(QuoteConversation.conversation_id)
        .where(QuoteConversation.quote_id == quote_id)
        .order_by(QuoteConversation.created_at)
    )
    return list(db.scalars(stmt))


def get_quote_ids_for_conversation(db: Session, conversation_id: str) -> list[str]:
    stmt = select(QuoteConversation.quote_id).where(
        QuoteConversation.conversation_id == conversation_id
    )
    return list(db.scalars(stmt))


def get_quotes_for_conversation(db: Session, conversation_id: str) -> list[Quote]:
    """Reverse of get_conversation_ids_for_quote — spec: navigate both ways."""
    stmt = (
        select(Quote)
        .join(QuoteConversation, QuoteConversation.quote_id == Quote.quote_id)
        .where(QuoteConversation.conversation_id == conversation_id)
        .order_by(Quote.quote_id)
    )
    return list(db.scalars(stmt).unique())


def get_conversations_for_customer_quotes(db: Session, customer_id: str) -> list[Conversation]:
    """All conversations linked to any open quote of this customer.

    This is the cross-conversation context load (Decision 3, step 7): Mario's
    thread and Alice's thread both come back, which is what lets a message in
    one thread be resolved against a quote born in the other.
    """
    open_ids = [q.quote_id for q in get_open_quotes_for_customer(db, customer_id)]
    if not open_ids:
        return []

    conv_ids = db.scalars(
        select(QuoteConversation.conversation_id).where(QuoteConversation.quote_id.in_(open_ids))
    ).unique()

    convs = []
    for cid in conv_ids:
        conv = db.get(Conversation, cid)
        if conv is not None:
            convs.append(conv)
    return convs


# ---------- Pending reviews ----------

def create_pending_review(
    db: Session,
    review_id: str,
    conversation_id: str,
    customer_id: str,
    original_message: dict,
    llm_extraction: dict | None = None,
    resolution_issues: list[str] | None = None,
    candidate_quotes: list[str] | None = None,
) -> PendingReview:
    review = PendingReview(
        review_id=review_id,
        conversation_id=conversation_id,
        customer_id=customer_id,
        status="pending",
        original_message=original_message,
        llm_extraction=llm_extraction,
        resolution_issues=resolution_issues or [],
        candidate_quotes=candidate_quotes or [],
        follow_up_messages=[],
    )
    db.add(review)
    db.flush()
    return review


def get_pending_review(db: Session, review_id: str) -> PendingReview | None:
    return db.get(PendingReview, review_id)


def list_pending_reviews(db: Session, status: str = "pending") -> list[PendingReview]:
    stmt = select(PendingReview).where(PendingReview.status == status).order_by(PendingReview.created_at)
    return list(db.scalars(stmt))


def get_open_review_for_conversation(db: Session, conversation_id: str) -> PendingReview | None:
    stmt = (
        select(PendingReview)
        .where(PendingReview.conversation_id == conversation_id, PendingReview.status == "pending")
        .order_by(PendingReview.created_at.desc())
    )
    return db.scalars(stmt).first()


def append_follow_up_message(db: Session, review: PendingReview, message: dict) -> PendingReview:
    """Also guarded by message_id, for the same reason as append_message."""
    existing = list(review.follow_up_messages or [])
    message_id = message.get("message_id")
    if message_id and any(m.get("message_id") == message_id for m in existing):
        return review
    review.follow_up_messages = existing + [message]
    db.flush()
    return review


def resolve_review(
    db: Session,
    review: PendingReview,
    resolution: dict,
    status: str = "resolved",
) -> PendingReview:
    review.resolution = resolution
    review.status = status
    review.resolved_at = datetime.now(timezone.utc)
    db.flush()
    return review


# ---------- Serialization ----------

def quote_to_dict(db: Session, quote: Quote, include_history: bool = False) -> dict:
    """Shape a quote like the assignment's expected output."""
    data = {
        "document_type": quote.document_type,
        "quote_id": quote.quote_id,
        "status": quote.status,
        "conversations": get_conversation_ids_for_quote(db, quote.quote_id),
        "line_items": [
            {"sku": li.sku, "qty": li.quantity} for li in quote.line_items
        ],
    }
    if include_history:
        data["line_item_history"] = [
            {
                "sku": li.sku,
                "qty": li.quantity,
                "created_by_message_id": li.created_by_message_id,
                "last_changed_by_message_id": li.last_changed_by_message_id,
            }
            for li in quote.line_items
        ]
    return data
