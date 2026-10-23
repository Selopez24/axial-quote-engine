"""Pydantic request/response models.

The message shape is the assignment's own format: no webhook parsing, no
normalization layer (Decision 8).
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class MessageIn(BaseModel):
    """Single message at POST /messages (production shape)."""

    message_id: str
    conversation_id: str
    customer_id: str
    direction: Literal["inbound", "outbound"]
    text: str
    customer_name: str | None = None
    sender_name: str | None = None
    timestamp: datetime | None = None


class HistoryMessage(BaseModel):
    """One entry of a batch conversation_history array."""

    direction: Literal["inbound", "outbound"]
    text: str
    timestamp: datetime | None = None
    message_id: str | None = None
    sender_name: str | None = None


class ConversationIn(BaseModel):
    """Batch conversation at POST /conversations (testing shape)."""

    customer_id: str
    conversation_id: str
    conversation_history: list[HistoryMessage]
    customer_name: str | None = None
    rep_name: str | None = None


class ExtractedLine(BaseModel):
    sku: str
    quantity: int


class ExtractionResult(BaseModel):
    """Structured output the LLM must produce.

    `reference` is a fourth intent beyond the spec's three: Event 2 asks about
    an existing quote without changing any line, which is neither a new quote
    nor a revision, and must still create a conversation link.

    `other` is a fifth: messages with no quote action at all (greetings,
    thanks, pure availability questions about products on no open quote).
    A live real-model probe beyond the guide found these being parsed as
    unclear — parked, and then swallowing every later message of the thread
    into that review. Inert intent, no state, no review.

    `cancel` is recognized but not implemented: when a customer says "cancelalo"
    or similar, the LLM can now return this intent. It routes to pending review
    (like unclear) because cancellation logic — which quote? partial or full?
    — is not yet wired up. A human decides.
    """

    intent: Literal["new_quote", "revision", "reference", "other", "cancel", "unclear"]
    confidence: float = Field(ge=0.0, le=1.0)
    target_quote_id: str | None = None
    line_items: list[ExtractedLine] = Field(default_factory=list)
    mentions_new_request: bool = False
    reason: str | None = None


class PendingReviewOut(BaseModel):
    review_id: str
    conversation_id: str
    customer_id: str
    status: str
    original_message: dict
    llm_extraction: dict | None = None
    resolution_issues: list[str] = Field(default_factory=list)
    candidate_quotes: list[str] = Field(default_factory=list)
    follow_up_messages: list[dict] = Field(default_factory=list)
    resolution: dict | None = None


class ResolveIn(BaseModel):
    """Human decision on a pending review.

    action: "apply" (apply extracted lines to target_quote_id), "new_quote",
    or "discard".
    """

    action: Literal["apply", "new_quote", "discard"]
    target_quote_id: str | None = None
    line_items: list[ExtractedLine] | None = None
