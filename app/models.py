from sqlalchemy import Column, String, Integer, DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base


class Customer(Base):
    __tablename__ = "customers"
    
    customer_id = Column(String, primary_key=True)
    customer_name = Column(String, nullable=True)
    # Company is known from the customer master before anyone messages; the
    # person is whoever the first message names (Alice, later Mario).
    company_name = Column(String, nullable=True)
    metadata_ = Column("metadata", JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    
    conversations = relationship("Conversation", back_populates="customer")
    quotes = relationship("Quote", back_populates="customer")


class CatalogItem(Base):
    """The assignment's "mock DB" of sellable SKUs.

    This is the only authority on what a valid SKU is: the validator filters
    against these rows and the prompt is built from them, so a hallucinated SKU
    cannot be written to line_items.
    """

    __tablename__ = "catalog_items"

    sku = Column(String, primary_key=True)
    description = Column(String, nullable=False)


class Conversation(Base):
    __tablename__ = "conversations"
    
    conversation_id = Column(String, primary_key=True)
    customer_id = Column(String, ForeignKey("customers.customer_id"), nullable=False)
    messages = Column(JSONB, default=list)  # Array of message objects
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    
    customer = relationship("Customer", back_populates="conversations")
    quotes = relationship("Quote", secondary="quote_conversations", back_populates="conversations")


class Quote(Base):
    __tablename__ = "quotes"
    
    quote_id = Column(String, primary_key=True)
    customer_id = Column(String, ForeignKey("customers.customer_id"), nullable=False)
    document_type = Column(String, default="Oferta_de_Venta")
    # Only "draft" is ever written here: the engine extracts and tracks quotes,
    # it does not approve, confirm or cancel them (design.md Non-Goals).
    # What counts as open lives in repositories.OPEN_QUOTE_STATUSES.
    status = Column(String, default="draft")
    metadata_ = Column("metadata", JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    
    customer = relationship("Customer", back_populates="quotes")
    line_items = relationship("LineItem", back_populates="quote", cascade="all, delete-orphan")
    conversations = relationship("Conversation", secondary="quote_conversations", back_populates="quotes")


class LineItem(Base):
    __tablename__ = "line_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    quote_id = Column(String, ForeignKey("quotes.quote_id"), nullable=False)
    sku = Column(String, nullable=False)
    quantity = Column(Integer, nullable=False)
    unit = Column(String, default="units")
    notes = Column(Text, nullable=True)
    # Provenance (Option B): which message created this line and last changed its qty
    created_by_message_id = Column(String, nullable=True)
    last_changed_by_message_id = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    quote = relationship("Quote", back_populates="line_items")

    # One row per SKU per quote: makes duplicate lines for the same SKU unrepresentable
    __table_args__ = (UniqueConstraint("quote_id", "sku", name="uq_line_items_quote_sku"),)


class QuoteConversation(Base):
    """Many-to-many relationship between quotes and conversations"""
    __tablename__ = "quote_conversations"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    quote_id = Column(String, ForeignKey("quotes.quote_id"), nullable=False)
    conversation_id = Column(String, ForeignKey("conversations.conversation_id"), nullable=False)
    metadata_ = Column("metadata", JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    # Read-only mirrors of the secondary relationship above: the links are
    # written explicitly through link_quote_conversation, not via these.
    quote = relationship("Quote", viewonly=True)
    conversation = relationship("Conversation", viewonly=True)

    # Idempotent linking: a quote-conversation pair can only be linked once
    __table_args__ = (UniqueConstraint("quote_id", "conversation_id", name="uq_quote_conversation"),)


class PendingReview(Base):
    __tablename__ = "pending_reviews"
    
    review_id = Column(String, primary_key=True)
    conversation_id = Column(String, ForeignKey("conversations.conversation_id"), nullable=False)
    customer_id = Column(String, ForeignKey("customers.customer_id"), nullable=False)
    status = Column(String, default="pending")  # pending, resolved, discarded, self_resolved
    original_message = Column(JSONB, nullable=False)
    llm_extraction = Column(JSONB, nullable=True)
    resolution_issues = Column(JSONB, default=list)
    candidate_quotes = Column(JSONB, default=list)
    follow_up_messages = Column(JSONB, default=list)
    resolution = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    
    conversation = relationship("Conversation")
    customer = relationship("Customer")


class ProcessedMessage(Base):
    """Tracks processed message_ids for deduplication"""
    __tablename__ = "processed_messages"
    
    message_id = Column(String, primary_key=True)
    conversation_id = Column(String, ForeignKey("conversations.conversation_id"), nullable=False)
    processed_at = Column(DateTime(timezone=True), server_default=func.now())
    processing_result = Column(JSONB, nullable=True)
    
    conversation = relationship("Conversation")
