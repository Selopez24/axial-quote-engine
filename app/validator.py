"""Deterministic validation: the layer between the LLM and the database.

Nothing from the model reaches state until it passes here. A SKU that is not in
the `catalog_items` table cannot produce a line item, because it is filtered out
here and the filtered set is what the caller applies; any dropped SKU turns the
message into a pending review instead of a silent guess. The catalog is read
from the database, so seeding is what makes a SKU legal — not a code change.
"""

from sqlalchemy.orm import Session

from app.repositories import (
    catalog_items,
    get_open_quotes_for_customer,
    get_quote_ids_for_conversation,
)
from app.schemas import ExtractionResult

CONFIDENCE_THRESHOLD = 0.7
MAX_QUANTITY = 100_000  # sane ceiling: rejects "qty 999999999" style slips


def validate(
    db: Session,
    customer_id: str,
    result: ExtractionResult,
    conversation_id: str | None = None,
) -> dict:
    """Return {"ok": bool, "issues": [...], "candidates": [...], "clean_lines": [...]}"""
    issues: list[str] = []
    open_quotes = get_open_quotes_for_customer(db, customer_id)
    open_ids = [q.quote_id for q in open_quotes]
    valid_skus = set(catalog_items(db))

    # 1. confidence gate
    if result.confidence < CONFIDENCE_THRESHOLD:
        issues.append("low_confidence")

    # 2. intent gate
    if result.intent == "unclear":
        issues.append("unclear_intent")
    elif result.intent == "other":
        # No quote action passes inertly — UNLESS the model contradicted
        # itself by attaching lines or a target to an "other". Those are
        # never applied; a human sees them instead.
        if result.line_items or result.target_quote_id:
            issues.append("inconsistent_extraction")
        return {
            "ok": not issues,
            "issues": issues,
            "candidates": open_ids,
            "clean_lines": [],
            "target_quote_id": None,
        }

    # 3. catalog gate — every SKU must exist, by construction
    clean_lines = []
    unknown: list[str] = []
    for line in result.line_items:
        if line.sku not in valid_skus:
            unknown.append(line.sku)
            continue
        # For cancel intent, we only need SKUs (quantity doesn't matter)
        if result.intent != "cancel":
            if not isinstance(line.quantity, int) or line.quantity <= 0:
                issues.append("invalid_quantity")
                continue
            if line.quantity > MAX_QUANTITY:
                issues.append("invalid_quantity")
                continue
        clean_lines.append(line)
    if unknown:
        issues.append("unknown_sku")

    # 4. quote reference gate
    target = result.target_quote_id
    if result.intent in ("revision", "reference", "cancel"):
        if target is None:
            # A fallback is only allowed when exactly one quote could be meant:
            # first the customer's single open quote, then this conversation's
            # single open quote (a message continuing a chat is about the quote
            # that chat is working on). Two candidates is a guess, so it goes to
            # a human instead.
            conversation_open = [
                qid
                for qid in (get_quote_ids_for_conversation(db, conversation_id) if conversation_id else [])
                if qid in open_ids
            ]
            if len(open_ids) == 1:
                target = open_ids[0]
            elif len(conversation_open) == 1:
                target = conversation_open[0]
            else:
                issues.append("ambiguous_quote_reference")
        elif target not in open_ids:
            issues.append("unknown_quote_reference")

    if result.intent == "revision" and not clean_lines and "unknown_sku" not in issues:
        issues.append("nothing_to_change")

    if result.intent == "new_quote" and not clean_lines:
        issues.append("nothing_to_quote")

    if result.intent == "cancel" and target is None:
        issues.append("cancel_requires_target")

    return {
        "ok": not issues,
        "issues": issues,
        "candidates": open_ids,
        "clean_lines": clean_lines,
        "target_quote_id": target,
    }
