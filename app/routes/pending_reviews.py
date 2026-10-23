"""Pending review API.

This is the "knowing when not to guess" surface. A message the pipeline cannot
assign lands here with its candidate quotes, and nothing in quote state moves
until a person decides through POST /pending-reviews/{id}/resolve.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import repositories as repo
from app.database import get_db
from app.schemas import PendingReviewOut, ResolveIn

router = APIRouter()


def _serialize(review) -> dict:
    return {
        "review_id": review.review_id,
        "conversation_id": review.conversation_id,
        "customer_id": review.customer_id,
        "status": review.status,
        "original_message": review.original_message,
        "llm_extraction": review.llm_extraction,
        "resolution_issues": review.resolution_issues or [],
        "candidate_quotes": review.candidate_quotes or [],
        "follow_up_messages": review.follow_up_messages or [],
        "resolution": review.resolution,
        "created_at": review.created_at,
        "resolved_at": review.resolved_at,
    }


@router.get("/pending-reviews")
def list_reviews(db: Session = Depends(get_db)):
    reviews = repo.list_pending_reviews(db, status="pending")
    return {"pending_reviews": [_serialize(r) for r in reviews]}


@router.get("/pending-reviews/{review_id}", response_model=PendingReviewOut)
def get_review(review_id: str, db: Session = Depends(get_db)):
    review = repo.get_pending_review(db, review_id)
    if review is None:
        raise HTTPException(status_code=404, detail=f"review {review_id} not found")
    return _serialize(review)


@router.post("/pending-reviews/{review_id}/resolve")
def resolve_review(review_id: str, payload: ResolveIn, db: Session = Depends(get_db)):
    """Apply a human decision to the review's original message."""
    review = repo.get_pending_review(db, review_id)
    if review is None:
        raise HTTPException(status_code=404, detail=f"review {review_id} not found")
    if review.status != "pending":
        raise HTTPException(status_code=409, detail=f"review already {review.status}")

    message_id = (review.original_message or {}).get("message_id") or f"review.{review_id}"
    extraction = review.llm_extraction or {}
    lines = (
        [li.model_dump() for li in payload.line_items]
        if payload.line_items
        else extraction.get("line_items", [])
    )

    if payload.action == "discard":
        repo.resolve_review(
            db,
            review,
            resolution={"action": "discard", "decided_at": datetime.now(timezone.utc).isoformat()},
            status="discarded",
        )
        db.commit()
        return {"status": "discarded", "review_id": review_id, "changes": []}

    if payload.action == "new_quote":
        quote = repo.create_quote(db, review.customer_id, created_by_message_id=message_id)
        quote_id = quote.quote_id
    else:  # apply
        quote_id = payload.target_quote_id
        if quote_id is None:
            raise HTTPException(status_code=400, detail="target_quote_id is required for action=apply")
        if repo.get_quote(db, quote_id) is None:
            raise HTTPException(status_code=404, detail=f"quote {quote_id} not found")

    changes = []
    valid_skus = set(repo.catalog_items(db))
    for line in lines:
        if line["sku"] not in valid_skus:
            raise HTTPException(status_code=400, detail=f"sku {line['sku']} not in catalog")
        changes.append(
            repo.upsert_line_item(
                db, quote_id, line["sku"], int(line["quantity"]), message_id=message_id
            )
        )

    repo.link_quote_conversation(
        db, quote_id, review.conversation_id, reason=f"human resolution of {review_id}"
    )
    repo.resolve_review(
        db,
        review,
        resolution={
            "action": payload.action,
            "target_quote_id": quote_id,
            "changes": changes,
            "decided_at": datetime.now(timezone.utc).isoformat(),
        },
        status="resolved",
    )
    db.commit()

    return {"status": "resolved", "review_id": review_id, "quote_id": quote_id, "changes": changes}
