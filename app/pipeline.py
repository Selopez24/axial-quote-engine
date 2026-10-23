"""Pipeline orchestration (Decision 3).

Plain sequential functions, one transaction per message. Stage order matters:
dedupe first (so retries cost nothing), then store, then the cheap gates
(pending review, direction), and only then the one expensive thing — the LLM.
"""

from sqlalchemy.orm import Session

from app import repositories as repo
from app.extraction import build_customer_context, extract
from app.schemas import MessageIn
from app.state import apply_validated, route_to_review
from app.validator import validate


def load_context(db: Session, customer_id: str, conversation_id: str) -> tuple[list[dict], str, dict[str, str]]:
    """Everything the LLM needs to know before the new message.

    Loaded by customer_id (the company), not by conversation: that is what lets
    a message in Mario's thread be resolved against a quote that started in
    Alice's thread. Current conversation is included even when unlinked.

    Also returns the catalog, read from the database, so "what is a valid SKU"
    has exactly one answer in this system.
    """
    quotes = [repo.quote_to_dict(db, q) for q in repo.get_open_quotes_for_customer(db, customer_id)]
    customer = repo.get_customer(db, customer_id)

    convs = repo.get_conversations_for_customer_quotes(db, customer_id)
    linked_ids = {c.conversation_id for c in convs}
    if conversation_id not in linked_ids:
        current = repo.get_conversation(db, conversation_id)
        if current is not None:
            convs.append(current)

    history: list[dict] = []
    for c in convs:
        history.extend(repo.recent_messages(c, limit=20))

    customer_context = build_customer_context(
        quotes,
        customer_name=customer.customer_name if customer else None,
        company_name=customer.company_name if customer else None,
    )
    return history, customer_context, repo.catalog_items(db)


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def retry_review_with_new_context(db: Session, review, *, trigger_message: dict) -> dict | None:
    """Self-heal (Decision 6): the follow-up widens the parked message's window,
    so try the pair again. The FOLLOW-UP is what the model classifies — it is
    genuinely the newest message; making the model classify an older one while
    its clarification lists as history reads the exchange backwards, and the
    model rightly refuses to connect them. The instruction is to COMBINE: if
    the new message clarifies the parked one, emit the parked request with its
    now-clear destination; if it does not, unclear. Either way the change —
    applied under the ORIGINAL message id — keeps provenance on the message
    that asked for it, not the one that merely clarified. Returns the audit on
    a clean retry, None when still ambiguous (review stays pending, its stored
    extraction refreshed to the latest attempt). Reviews whose ambiguity is not
    expressible as an intent the engine writes (a cancellation) retry to
    unclear by design and wait.

    Raises nothing: a model/network error here leaves the review exactly as the
    follow-up found it.
    """
    original = review.original_message or {}
    message_id = original.get("message_id")
    if not message_id:
        return None

    history, customer_context, catalog = load_context(db, review.customer_id, review.conversation_id)
    issues = ", ".join(review.resolution_issues or ["no_clasificable"])
    customer_context += (
        "\n\nREVIEW: un mensaje anterior de este hilo esta en revision porque no se pudo asignar"
        f" ({issues}): \"{original.get('text', '')}\". Si el mensaje mas reciente (la ultima linea)"
        " aclara ESE mensaje (nombrando el producto o la cotizacion a la que apuntaba), emite la"
        " SOLICITUD COMBINADA: la intent, target_quote_id y line_items del mensaje en revision con"
        " el destino ya claro. Recuerda: una respuesta explicita a la aclaracion del vendedor es"
        " pista; la mera recencia del tema no. Si el mensaje mas reciente no aclara ese mensaje,"
        " devuelve intent unclear."
    )
    try:
        result = extract(catalog, customer_context, history, trigger_message)
    except Exception:
        return None

    validation = validate(db, review.customer_id, result, conversation_id=review.conversation_id)
    if not validation["ok"]:
        review.llm_extraction = result.model_dump(mode="json")
        review.resolution_issues = validation["issues"]
        db.flush()
        return None

    audit = apply_validated(
        db,
        customer_id=review.customer_id,
        conversation_id=review.conversation_id,
        message_id=message_id,
        result=result,
        validation=validation,
    )
    if not repo.is_message_processed(db, message_id):
        repo.mark_message_processed(db, message_id, review.conversation_id, audit)
    return audit


def process_message(db: Session, payload: MessageIn) -> dict:
    """Full pipeline for one message. Caller owns the transaction."""

    # [2] Deduplication
    if repo.is_message_processed(db, payload.message_id):
        return {
            "status": "duplicate",
            "message_id": payload.message_id,
            "detail": "message_id already processed; state unchanged",
        }

    # [3] Customer identity
    repo.get_or_create_customer(db, payload.customer_id, payload.customer_name)

    # [4] Store message in conversation history
    conv = repo.get_or_create_conversation(db, payload.conversation_id, payload.customer_id)
    message = payload.model_dump(mode="json")
    repo.append_message(db, conv, message)

    # [5] Pending review check: follow-ups enrich the open review, they do not
    # get guessed at. An inbound follow-up, though, is also new context for the
    # *original* message — the sliding window overlaps it now — so extraction is
    # retried for the parked message before anything else (Decision 6, self-heal).
    # The exception is the review's own message: a message whose extraction
    # failed is left unprocessed deliberately so it can be retried, and
    # re-delivery must re-enter extraction rather than become a follow-up of
    # itself.
    open_review = repo.get_open_review_for_conversation(db, payload.conversation_id)
    is_its_own_review = (open_review.original_message or {}).get("message_id") == payload.message_id if open_review else False
    if open_review is not None and not is_its_own_review:
        repo.append_follow_up_message(db, open_review, message)

        audit = None
        if payload.direction == "inbound":
            audit = retry_review_with_new_context(db, open_review, trigger_message=message)

        if audit is not None:
            heal = {
                "applied": {"intent": audit["intent"], "quote_id": audit["quote_id"], "changes": audit["changes"]},
                "triggered_by_message_id": payload.message_id,
                "clarifies_message_id": (open_review.original_message or {}).get("message_id"),
                "decided_at": _now(),
            }
            repo.resolve_review(db, open_review, heal, status="self_resolved")
            repo.mark_message_processed(
                db,
                payload.message_id,
                payload.conversation_id,
                {"review_self_resolved": open_review.review_id, "direction": payload.direction},
            )
            return {
                "status": "processed",
                "message_id": payload.message_id,
                "direction": payload.direction,
                "review_id": open_review.review_id,
                "self_resolved": True,
                **audit,
            }

        repo.mark_message_processed(
            db,
            payload.message_id,
            payload.conversation_id,
            {"routed_to_review": open_review.review_id, "direction": payload.direction},
        )
        return {
            "status": "appended_to_review",
            "review_id": open_review.review_id,
            "message_id": payload.message_id,
        }

    # [6] Direction gate: outbound is context only, never a quote change
    if payload.direction == "outbound":
        repo.mark_message_processed(
            db, payload.message_id, payload.conversation_id, {"stored_only": True}
        )
        return {"status": "stored", "message_id": payload.message_id, "direction": "outbound"}

    # [7] Load context / [8] LLM extraction
    history, customer_context, catalog = load_context(db, payload.customer_id, payload.conversation_id)
    try:
        result = extract(catalog, customer_context, history, message)
    except Exception as exc:  # LLM unavailable / malformed output
        candidates = [q.quote_id for q in repo.get_open_quotes_for_customer(db, payload.customer_id)]
        audit = route_to_review(
            db,
            conversation_id=payload.conversation_id,
            customer_id=payload.customer_id,
            message=message,
            result=None,
            validation={"issues": [f"extraction_error: {exc}"], "candidates": candidates},
        )
        # The message is NOT marked processed, so it stays retryable once the
        # model is reachable again. append_message dedupes by message_id, so the
        # redelivery cannot double the transcript, and route_to_review keys the
        # review on the message id, so it cannot stack a second review.
        return {
            "status": "processed",
            "message_id": payload.message_id,
            "direction": payload.direction,
            **audit,
        }

    # [9] Deterministic validation
    validation = validate(db, payload.customer_id, result, conversation_id=payload.conversation_id)

    # [10] State update
    if validation["ok"]:
        audit = apply_validated(
            db,
            customer_id=payload.customer_id,
            conversation_id=payload.conversation_id,
            message_id=payload.message_id,
            result=result,
            validation=validation,
        )
    else:
        audit = route_to_review(
            db,
            conversation_id=payload.conversation_id,
            customer_id=payload.customer_id,
            message=message,
            result=result,
            validation=validation,
        )

    repo.mark_message_processed(db, payload.message_id, payload.conversation_id, audit)

    return {
        "status": "processed",
        "message_id": payload.message_id,
        "direction": payload.direction,
        **audit,
    }
