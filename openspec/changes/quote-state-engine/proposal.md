# Proposal

## Why

Sales reps managing quotes via WhatsApp face operational chaos: conversations fragment across multiple chats, customers send revisions from different threads, and context gets lost. This forces reps to manually track which messages belong to which quotes, leading to errors, delays, and cognitive overload. A headless API is needed to transform chaotic WhatsApp threads into structured, maintainable quotes automatically.

## What Changes

- **New headless API**: RESTful endpoints to process incoming WhatsApp messages and maintain quote state
- **Quote extraction pipeline**: Convert unstructured conversational messages into structured quote data with line items, SKUs, and quantities
- **State persistence**: Store quotes, conversations, and their many-to-many relationships in Postgres so state survives between API calls
- **Multi-conversation handling**: Allow a single conversation thread to spawn multiple quotes and allow a single quote to be managed across multiple conversation threads
- **Ambiguity resolution**: When the system cannot confidently determine which quote a message belongs to, create pending reviews for human intervention rather than guessing
- **Speaker identification**: Track which human (customer, sales rep, etc.) sent each message using OpenAI Responses API's `role: "user"` pattern with speaker names embedded in content
- **Message deduplication**: Detect and skip duplicate webhook deliveries to maintain idempotency
- **Cache-friendly prompts**: Stable content is ordered ahead of volatile content — instructions and catalog in a leading developer message carrying one explicit cache breakpoint, then customer context, then append-only history — so repeat calls can bill cached input on models that support it; no caching infrastructure

## Capabilities

### New Capabilities

- `message-processing`: Core pipeline for ingesting WhatsApp messages, including deduplication, validation, and routing through the extraction/classification flow
- `quote-extraction`: Transform unstructured conversational text into structured quote data with line items, SKUs, quantities, and pricing
- `state-management`: Persist and retrieve quote state, conversation state, and the many-to-many relationship between quotes and conversations across API calls
- `conversation-linking`: Resolve which quote(s) a message belongs to, handle cross-conversation references, and manage the bidirectional quote-conversation relationship
- `ambiguity-resolution`: Detect when message intent is unclear, create pending reviews with full context, and provide an API for human review and decision-making

### Modified Capabilities

None (greenfield project)

## Impact

**New code**: 
- FastAPI application with REST endpoints for message ingestion, quote retrieval, and pending review management
- PostgreSQL database schema with tables for customers, conversations, quotes, line_items, quote_conversations (many-to-many), message_quote_mappings, and pending_reviews
- Simple sequential orchestration pipeline for message processing
- OpenAI integration using Responses API for extraction and classification

**Dependencies**:
- FastAPI (web framework)
- PostgreSQL with JSONB support (state persistence)
- OpenAI Python SDK (Responses API for LLM calls)
- Pydantic (data validation)

**APIs**:
- `POST /messages` - Ingest WhatsApp messages and process through quote pipeline
- `GET /quotes?customer={customer_id}` - Retrieve all quotes for a customer
- `GET /quotes/{quote_id}` - Retrieve specific quote with line items and linked conversations
- `GET /pending-reviews` - List pending reviews requiring human attention
- `POST /pending-reviews/{review_id}/resolve` - Human resolution of ambiguous cases

**External systems**:
- WhatsApp Business API (webhook source)
- OpenAI API (LLM for extraction and classification)

**Operational considerations**:
- Prompt prefix ordering keeps repeat calls in one conversation cheap, at the cost of no measurable hit rate
- Idempotent message processing prevents duplicate quotes from webhook retries
- Pending reviews require human intervention when system cannot resolve ambiguity
