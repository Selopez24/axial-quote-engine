# Design

## Context

This is a greenfield project to build a headless API that manages sales quotes extracted from WhatsApp conversations. The system must handle the complexity of fragmented conversations where multiple chat threads can relate to a single quote, and a single conversation can spawn multiple quotes over time.

The system operates as a message processing pipeline: incoming WhatsApp messages are ingested, deduplicated, processed through an LLM-based extraction flow, validated against business rules, and used to create or update quote state. All state must persist across API calls and survive service restarts.

Key constraints:
- No UI required (API-only)
- Spanish language input
- Must handle ambiguous message-to-quote mappings gracefully
- Must keep LLM cost bounded through prompt design, not caching infrastructure
- State must be consistent and recoverable

## Goals / Non-Goals

**Goals:**
- Design a stateless API architecture that can scale horizontally
- Implement a robust data model supporting many-to-many quote-conversation relationships
- Build a message processing pipeline with at-least-once delivery semantics
- Create an LLM integration layer with structured output validation and caching
- Design an ambiguity resolution system that queues uncertain cases for human review
- Order prompts so repeated calls in an active conversation share a cached prefix
- Provide clear separation between extraction logic and business validation

**Non-Goals:**
- Building a user interface or dashboard
- Implementing WhatsApp Business API integration (we consume messages, don't send)
- Creating a full quote lifecycle management system (we extract and track, not approve/reject)
- Supporting multiple languages beyond Spanish
- Real-time streaming or WebSocket connections
- Implementing actual pricing calculations (quotes contain SKUs and quantities only)
- Building a general-purpose chatbot or conversational AI

## Decisions

### 1. Data Model: PostgreSQL with JSONB Hybrid Approach

**Decision:** Use PostgreSQL with a hybrid schema approach - structured tables for core entities (quotes, line_items, conversations) with JSONB columns for flexible message storage and metadata.

**Rationale:**
- Structured tables provide query performance and referential integrity for frequently accessed data
- JSONB for messages allows flexible schema evolution without migrations
- JSONB for metadata (quote_conversations.metadata, pending_reviews.resolution) accommodates varying data structures
- PostgreSQL's JSONB indexing supports efficient queries on nested data
- Single database simplifies deployment and transactions

**Alternatives Considered:**
- **Pure JSON document database (MongoDB):** Rejected because we need strong referential integrity for quote-conversation relationships and transactional guarantees across multiple tables
- **Pure relational with normalized messages:** Rejected because message schemas vary and evolve; JSONB provides flexibility without sacrificing query performance on core entities
- **Event sourcing:** Rejected as over-engineering for this use case; we need current state, not full history replay

### 2. Many-to-Many Relationship: Explicit Join Table

**Decision:** Use an explicit `quote_conversations` join table with metadata to track the relationship between quotes and conversations, including creation timestamps and relationship context.

**Rationale:**
- Explicit join table allows querying in both directions efficiently (quote→conversations, conversation→quotes)
- Metadata column supports tracking relationship context (e.g., "quote referenced from different channel")
- Supports the complex scenario where Mario's conversation (wa-thread-662) links to Alice's quote (Q-001)
- Allows a single conversation to spawn multiple quotes (Event 3: Alice's conversation creates both Q-001 and Q-002)
- Enables audit trail of when relationships were established

**Alternatives Considered:**
- **Embed conversation_ids in quote document:** Rejected because it prevents efficient reverse queries (find all quotes for a conversation) and doesn't support metadata
- **Embed quote_ids in conversation document:** Rejected because conversations can spawn new quotes over time, requiring constant updates and creating update anomalies
- **Separate graph database:** Rejected as over-engineering; PostgreSQL join table handles this complexity adequately

### 3. Message Processing Pipeline: Simple Sequential Orchestration

**Decision:** Implement a straightforward sequential pipeline using plain Python functions. All business state is persisted to PostgreSQL. The pipeline consists of discrete stages called in sequence, with structured logging for debugging and audit trails in the database for replay/debugging.

**Rationale:**
- Simple sequential pipeline is easier to understand and maintain than a graph framework
- Stateless processing allows horizontal scaling of API servers
- Database-backed state ensures consistency and recoverability
- Single transaction per message prevents partial updates and race conditions
- Deduplication via message_id tracking prevents duplicate processing from webhook retries
- Pipeline stages (dedupe → store → pending check → extract → validate → update) provide clear separation of concerns
- Structured logging + database audit trail provides sufficient debug/replay capability

**Pipeline Flow:**
```
Incoming Message (pre-normalized format from assignment)
    ↓
[1] Validate Input (check required fields: message_id, conversation_id, customer_id, direction, text)
    ↓
[2] Deduplication Check (message_id exists?)
    ↓
[3] Store Customer Identity (create customer record if not exists, with customer_id, customer_name)
    ↓
[4] Store Message (append to conversation JSONB, direction: inbound or outbound)
    ↓
[5] Pending Review Check (conversation has open review?)
    ↓ (if yes, append to review; if no, continue)
[6] Direction Gate: If outbound message → store only, return. If inbound → continue.
    ↓
[7] Load Context: Find all open quotes for this customer_id. Load all conversations linked to those quotes (not just current conversation). Include customer identity.
    ↓
[8] LLM Extraction: Build prompt with system instructions, catalog, customer context (open quote structures), conversation history from all linked conversations, and new message. Send to OpenAI with structured output.
    ↓
[9] Deterministic Validation: Check SKUs against catalog. Validate quantities. Check confidence threshold. Determine if pending review needed.
    ↓
[10] State Update: Create or update quote, create/update line items, link conversation to quote, record message processing
    ↓
[11] Return Response
```

**Context Loading Strategy (Step 7):**
- Load by `customer_id` (company), not by individual user or conversation
- Find all open quotes for that customer_id
- Load all conversations linked to those quotes (last 20 messages each)
- This allows cross-conversation references: when Mario's message arrives (different user, same company), the LLM sees Alice's conversations and can resolve "Alice me pasó con ustedes, me confirmas qué lleva la cotización de las lonas?" to Q-001

**Implementation Approach:**
- Each pipeline stage is a simple Python function with typed inputs/outputs
- A main orchestrator function calls stages in sequence
- Structured logging captures each stage's execution for debugging
- Database state provides full audit trail for replay

**Alternatives Considered:**
- **LangGraph orchestration:** Rejected as unnecessary complexity; we're not using state management or checkpointing (Postgres handles all state), and a sequential pipeline doesn't need graph-based orchestration
- **Message queue (Redis/RabbitMQ):** Rejected as unnecessary complexity; we're processing single messages synchronously, not batching or distributing work
- **Event-driven architecture:** Rejected as over-engineering; synchronous processing with database transactions is simpler and sufficient

### 4. LLM Integration: OpenAI Responses API with Structured Output

**Decision:** Use OpenAI Responses API with structured output validation via Pydantic models, on `gpt-6-luna` by default (`QUOTE_ENGINE_MODEL`, with `OPENAI_BASE_URL` for OpenAI-compatible gateways such as OpenCode Go). The LLM extracts intent, target quotes, and line item modifications, which are then validated against deterministic business rules before state updates.

**Rationale:**
- Structured output (json_schema with strict mode) ensures predictable extraction format
- On GPT-5.6+ no custom temperature is accepted, so determinism leans on the strict
  schema plus the validator; the legacy gpt-4o-mini shape still sends temperature=0
- Pydantic validation catches malformed LLM output before it reaches the database
- Deterministic validation layer (catalog check, quantity bounds) catches hallucinations
- Separation of LLM extraction (probabilistic) and validation (deterministic) follows "LLM for understanding, code for state" principle

**LLM Output Schema:**
```python
{
  "intent": "new_quote" | "revision" | "reference" | "other" | "unclear",
  "confidence": float,  # 0.0-1.0
  "target_quote_id": str | null,
  "line_items": [
    {"sku": str, "quantity": int}
  ],
  "mentions_new_request": bool,
  "reason": str | null
}
```

`reference` exists because Event 2 asks about a quote without changing any line, which is
neither a new quote nor a revision — and it must still create the conversation link.
"revision with empty line_items" was the alternative, but conflating a question with a
change makes the audit trail lie about what happened.

`other` exists because messages with no quote action — greetings, thanks, pure
availability questions about products on no open quote — must not park: while a review
is open, every later inbound of that thread appends to it as context, so one
wrongly-parked "gracias" silently swallows the rest of the conversation. `unclear` is
therefore reserved for messages that genuinely demand a quote decision.

**Validation Rules (Deterministic):**
- Every SKU must exist in the catalog; anything else is dropped and the message becomes a pending review, so an invented SKU cannot become a line item
- Quantity must be a positive integer within a sane ceiling
- Confidence < 0.7 → pending review
- Intent "unclear" → pending review
- Intent "other" → inert pass-through: no line items, no link, no review; an "other" that
  contradicts itself by attaching line items or a target parks as inconsistent_extraction
- Target quote must exist and be open (for revision and reference); if no target is given and exactly one quote is open we use it, if several are open the reference is ambiguous → pending review
- "revision" that yields no valid line items, or "new_quote" with nothing to quote → pending review

**Alternatives Considered:**
- **LangChain:** Rejected as unnecessary abstraction; direct OpenAI SDK is simpler and keeps the prompt layout under our control, which is what the caching strategy in Decision 5 depends on
- **Multiple LLM calls (extract then validate):** Rejected as slower and more expensive; single call with post-validation is sufficient
- **Function calling:** Rejected in favor of structured output; function calling adds complexity without benefit for this use case

### 5. Caching Strategy: Prompt Ordering, One Breakpoint, No Caching Infrastructure

**Decision:** Order each request so stable content forms a reusable prefix, and place a
single declarative cache breakpoint at the end of that prefix: static system instructions
+ catalog as the leading developer message (carrying `prompt_cache_breakpoint` on models
that accept it), then the customer's open-quote context, then the conversation history as
one message per line (append-only), then the new message last. No cache endpoints, no
invalidation code, no `prompt_cache_key`. On pre-5.6 models (e.g. gpt-4o-mini) the same
text goes through the legacy `instructions` parameter with `temperature=0`; GPT-5.6 and
later take neither (their only temperature is the default — determinism comes from the
strict response schema plus the validator).

**Rationale (per the OpenAI prompt-caching docs):**
- GPT-5.6+ charges cache writes (1.25x input at the 30-minute default lifetime) and
  discounts reads to 0.1x — a cache that misses now costs more than no cache, so the
  breakpoint belongs only on content that truly never changes
- Implicit breakpoints land only at message endings, so the history went from one
  flattened, ever-growing message (the docs' "extending a message prevents prefix reuse"
  gotcha) to one append-only message per line
- Top-level `instructions` cannot carry a breakpoint on these models, hence the leading
  developer message; `prompt_cache_key` is no longer needed for routing, keeping cache
  administration at zero moving parts
- Requests stay on the API's default implicit mode: the automatic breakpoint at the
  newest message writes only the *delta* beyond the last cached prefix, so the next turn
  reads the whole thread back at 0.1x — what keeps long back-and-forth conversations
  cheap. Measured live: stable turn `cached=1359, cache_write=37`; when a quote change
  rewrites the customer-context line the thread prefix misses and the lookup falls back
  to the developer-prefix boundary (`cached=1226`), and that delta rewrite is amortized
  back by the next stable turn. The static prefix itself never invalidates
- The single explicit breakpoint marks the developer prefix — measured 1,226 tokens,
  above the 1,024-token cacheable minimum even with the 4-SKU seed, so it is writable
  from turn 1, shared across ALL conversations, and serves as the fallback boundary.
  No per-thread breakpoints: the ctx line mutates on every state change, so they would
  keep rewriting prefixes that never get read back
- `store=False`: the pipeline is fully stateless (full input re-sent, responses never
  fetched back), and the API default would retain each rendered customer transcript
  server-side for 30+ days
- `reasoning.effort: "low"` — classification against a strict schema is shallow-reasoning
  work; measured output tokens halved (168 -> 72) with identical payloads and the full
  live suite green. Effort participates in the cached prefix, so the value is constant
  across calls
- At <1 message/minute, spend is dominated by whether we re-send whole transcripts, not
  by hit rates; the last-20 window matters more than the cache

**Prompt Structure (stable → volatile):**
```
[developer]  static instructions + catalog, explicit cache breakpoint  ← never changes
[input 1]    customer open-quote context                               ← changes when a quote changes
[input 2..N] chat lines "[ts] speaker (company): text", oldest first   ← grows by append, prefix stays
```
Each chat line renders from its stored dict alone, so a message contributes
byte-identical tokens to every call that contains it — the property that lets a
breakpoint written on turn k align at a message ending on turn k+1. The message being
classified needs no marker: the instructions read the LAST line, and the leading
timestamp carries the ordering semantics (a later clarification re-reads an earlier ask).

**Alternatives Considered:**
- **Cache metrics/warming endpoints:** Rejected as over-engineering; it added five
  endpoints and a monitoring concern. The one breakpoint that remains is a field in the
  prompt, not a subsystem
- **Relying on implicit caching with a flattened history block:** Was the original shape;
  superseded because on GPT-5.6+ its breakpoints never align with a rewritten message
  body, so the claimed prefix reuse silently stops existing
- **Separate cache layer (Redis) for extracted quote structure:** Rejected; Postgres is
  the cache, and the context is a couple of small rows
- **Caching conversation quantities:** Pointless — they change on every revision, which
  is the thing we are processing

### 6. Ambiguity Resolution: Pending Review Queue with Context

**Decision:** When the LLM cannot confidently resolve a message (low confidence, unclear intent, ambiguous quote reference), create a pending review record with full context (original message, LLM extraction, resolution issues, candidate quotes). Provide API endpoints for humans to review and resolve these cases. The record deliberately carries facts only — no "suggested action" field — because a suggestion derived from a low-confidence guess is still a guess.

**Rationale:**
- Prevents incorrect quote updates when intent is unclear (e.g., "cancelalo" with multiple open quotes)
- Preserves full context for human decision-making (what did the LLM think? what are the options?)
- Allows follow-up messages to be appended to pending reviews, providing additional context
- Self-heal: an inbound follow-up is classified as a clarification of the parked
  request (its text and issues surfaced in context; the follow-up is never a state
  change on its own) — the gate blocks guessing, not progress. A clarification that
  makes the retry validate applies the combined result under the original message's
  provenance and marks the review `self_resolved` (closed to humans, so a stale human
  decision can't land on top). Ambiguity the intent vocabulary cannot express (a
  cancellation) retries to unclear and waits for a human, which is the "cancelalo"
  scenario by construction
- Separates automated processing from human judgment
- Supports three resolution paths: (1) apply to specific quote, (2) create new quote, (3) discard

**Pending Review Schema:**
```python
{
  "review_id": str,
  "conversation_id": str,
  "customer_id": str,
  "status": "pending" | "resolved" | "discarded",
  "original_message": dict,
  "llm_extraction": dict,
  "resolution_issues": [str],  # e.g., ["ambiguous_quote_reference", "low_confidence"]
  "candidate_quotes": [str],   # quote_ids that might be relevant
  "follow_up_messages": [dict],  # subsequent messages in same conversation
  "resolution": dict | null,   # human decision
  "created_at": datetime,
  "resolved_at": datetime | null
}
```

**Trigger Conditions:**
- LLM confidence < 0.7
- Intent = "unclear"
- Target quote unresolvable: no `target_quote_id`, the customer has more than one open
  quote, *and* the arriving conversation does not touch exactly one of them. A fallback is
  allowed only when there is a single possible referent — one is a fact, two is a guess
- SKU not found in catalog
- Quantity validation fails

**Alternatives Considered:**
- **Auto-resolve with lowest confidence:** Rejected because it pushes incorrect data to the ERP; human judgment is required
- **Ask user for clarification via WhatsApp:** Rejected because we're building a headless API; we don't control the WhatsApp interface
- **Reject message entirely:** Rejected because we lose context; pending review preserves the message and allows later resolution

### 7. Speaker Identification: Embedded Names in Content

**Decision:** Embed speaker names directly in the message content text (e.g., "Alice: Hola", "Izzy: Buen día") since OpenAI's Responses API does not support a `name` field on messages (unlike Chat Completions API). All human messages use `role: "user"` regardless of whether they're from customers or sales reps.

**Rationale:**
- OpenAI Responses API lacks the `name` field that Chat Completions API provides
- Embedding names in content is the only way to identify speakers in Responses API
- All humans are `role: "user"` — the LLM is a backend processor, not a conversation participant
- Supports multi-speaker conversations (Alice, Mario, Izzy all as `role: "user"`)
- Makes cross-conversation references resolvable (e.g., "Alice me pasó con ustedes")

**Message Format:**
```python
{
  "role": "user",
  "content": "Alice: Hola buenos dias, me ayudas a cotizar 20 rollos lona"
}
```

**Formatting Convention:**
- Prefix each message with `"{speaker_name}: "`
- Speaker names are extracted from the WhatsApp message metadata
- Consistent formatting allows the LLM to reliably identify speakers
- Outbound fallback: a message with `direction: "outbound"` and no explicit
  `sender_name` is labeled `"Vendedor"`, **never** the customer. Borrowing
  `customer_name` there would put the rep's words in Alice's mouth, and quote
  resolution is content-driven by who said what (`app/extraction.py::speaker_prefix`,
  covered by `tests/test_spec_coverage.py`)

**Alternatives Considered:**
- **Use Chat Completions API for `name` field:** Rejected because Responses API provides better performance (40-80% cache improvement) and is the recommended path forward
- **Separate messages per speaker:** Rejected because it fragments the conversation flow and loses context
- **No speaker identification:** Rejected because we need to track who said what for cross-conversation references

### 8. Input Format: Direct Message Processing

**Decision:** The system accepts messages in a pre-normalized format (as shown in the assignment). No webhook parsing or normalization layer is needed — the input already contains customer_id, conversation_id, customer_name, message_id, direction, timestamp, and text fields.

**Rationale:**
- Assignment provides messages in a clean, structured format for simplicity
- Phase 1 extraction should focus on understanding conversation context, not parsing webhook payloads
- 80/20 principle: spend time on the complex logic (quote linking, revision tracking), not data transformation
- In production, this normalization would happen in middleware or a separate service
- For this take-home, we assume the input is already normalized

**Input Format:**
```json
{
  "message_id": "wamid.xxx",
  "conversation_id": "wa-thread-411",
  "customer_id": "C-10029",
  "customer_name": "Alice - ACME INDUSTRIAL",
  "direction": "inbound",
  "timestamp": "2026-02-11T10:14:00Z",
  "text": "Oye de las lonas mejor que sean 15, no 20"
}
```

**Two Entry Points:**

1. **POST /conversations** (batch endpoint for testing/demonstration)
   - Accepts the assignment's initial conversation format with full `conversation_history` array
   - Processes each message sequentially through the pipeline
   - Builds up quote state from a complete conversation
   - Used for Phase 1: extracting initial quotes from conversation history
   
   ```json
   {
     "customer_id": "C-10029",
     "conversation_id": "wa-thread-411",
     "customer_name": "Alice - ACME INDUSTRIAL",
     "conversation_history": [
       {"direction": "inbound", "timestamp": "...", "text": "..."},
       {"direction": "outbound", "timestamp": "...", "text": "..."}
     ]
   }
   ```

2. **POST /messages** (individual endpoint for production use)
   - Accepts single message payloads in the format shown above
   - Handles both inbound (triggers extraction) and outbound (stores only) messages
   - Used for Phase 2: processing ongoing messages and revisions
   - Represents how the system would work in production (one message at a time)

**Processing Flow (POST /messages):**
```
POST /messages (single message)
    ↓
[1] Validate input format
    ↓
[2] Deduplication Check (message_id exists?)
    ↓
[3] Store Customer Identity (create customer record if not exists)
    ↓
[4] Store Message (append to conversation JSONB)
    ↓
[5] Pending Review Check (conversation has open review?)
    ↓
[6] Direction Gate: If outbound → store only, return. If inbound → continue.
    ↓
[7] Load Context (all open quotes for customer_id, linked conversations)
    ↓
[8] LLM Extraction → Validation → State Update
    ↓
[9] Return Response
```

**Production Note:** In a real system, you'd have a separate webhook handler that receives raw WhatsApp payloads, normalizes them (mapping phone numbers to customer IDs, building conversation IDs), and calls this API. For this assignment, we skip that layer and work directly with the normalized format.

**Alternatives Considered:**
- **Parse raw WhatsApp webhooks:** Rejected because it adds complexity without improving the core solution; the assignment provides normalized input for a reason
- **Build normalization layer:** Rejected as over-engineering for a 6-hour take-home; focus on the actual business logic instead

### 9. Customer Identity Storage

**Decision:** Persist customer identity information (customer_id, customer_name, and other available metadata) in a dedicated customers table, allowing the system to store and reuse customer information across multiple conversations.

**Rationale:**
- Assignment input includes customer_id and customer_name as available metadata
- Customer identity is independent of conversations (one customer can have many conversations)
- Customer identity provides valuable context for LLM extraction (understanding organizational context, speaker relationships)
- Allows querying by customer across all conversations and quotes
- Survives service restarts and provides audit trail of customer interactions

**Data Model:**
```sql
CREATE TABLE customers (
  customer_id VARCHAR PRIMARY KEY,
  customer_name VARCHAR,      -- the person; filled by the first message that names one
  company_name VARCHAR,       -- the company; reference data, known before anyone writes
  metadata JSONB,  -- Flexible field for additional identity info
  created_at TIMESTAMP DEFAULT NOW(),
  updated_at TIMESTAMP DEFAULT NOW()
);
```

**Integration Points:**
- Pipeline calls `get_or_create_customer` on every message: identity is stored on first encounter, reused afterwards
- Conversation creation links to the customer record via `customer_id`
- LLM context includes customer identity so the model can resolve "Mario de compras de ACME" against the same company's quotes
- Customer is the unit of context loading, which is what makes cross-conversation references work

**Alternatives Considered:**
- **Store customer identity only on conversations:** Rejected because customer is independent entity; multiple conversations can reference same customer
- **Embed customer metadata in each message:** Rejected because it duplicates data; customer identity should be stored once and referenced
- **No customer identity storage:** Rejected because customer_name and other metadata provide valuable context for extraction and reporting
- **Separate contacts table for individual speakers:** Rejected as over-engineering for this use case; individual speakers are identified per-message via speaker identification, not as separate entities

### 10. Reference Data Seeding: Catalog as a Table, Nothing Else

**Decision:** Seed exactly two kinds of data at startup — the SKU catalog (now a
`catalog_items` table, not a Python dict) and the customer master (`C-10029` /
ACME INDUSTRIAL). Conversation, quote, line-item and review state are never seeded.
Seeding is idempotent and runs on every boot; `db/seed.sql` carries the same rows for
reviewers who prefer psql.

**Rationale:**
- The assignment calls the catalog a "mock DB". Making it a table means the validator and
  the prompt read the same authority, so "what is a valid SKU" has exactly one answer and
  adding a SKU is data, not a code change
- `company_name` separates the company (known up front) from the person (whoever the
  first message names), while Decision 9's rule still holds: identity is recorded once
  and then kept stable, so the seeded row carries `customer_name = NULL`
- Quote state is the thing under evaluation. Seeding the finished scenario would let a
  reviewer see the expected answer without an API key, but it presents a fixture as model
  output and hides the pipeline. The reviewer's own `POST /conversations` must produce it

**Consequences:**
- An unseeded database fails safe: no `catalog_items` rows means every SKU is unknown and
  every message routes to review, rather than inventing a catalog
- `app/catalog.py` keeps `CATALOG` as the seed *definition*; runtime truth is the table.
  Two representations is a drift hazard, so `tests/test_seed.py` loads `db/seed.sql`
  against the live schema and asserts it equals `CATALOG`, that both seed paths produce
  identical rows, and that no state tables get populated
- Auto-init is gated by `QUOTE_ENGINE_AUTOINIT` (default on); the test suite turns it off
  and seeds its own database, so running tests never writes to dev data

## Risks / Trade-offs

**[Risk] LLM Hallucination → Mitigation:** Deterministic validation layer validates all LLM output against catalog and business rules before state updates. SKUs must exist in catalog, quantities must be valid, confidence scores trigger pending reviews. Invalid output never reaches the database.

**[Risk] Cache Misses on Customer Context → Mitigation:** Prompt ordering does the caching work: instructions + catalog, then customer context, then history, then the new message. Accept that a conversation idle long enough to lose its cache pays one full-price call. What actually controls cost is the last-20 window, not cache hit rates.

**[Risk] Ambiguity Resolution Backlog → Mitigation:** Pending reviews carry the facts a human needs — original message, attempted extraction, the issues that blocked it, candidate quotes — to speed resolution. Follow-up messages are appended to provide additional context. Monitor pending review queue length and age.

**[Risk] Performance with Many Conversations → Mitigation:** Load only relevant context (last 20 messages from linked conversations, not entire history). Use PostgreSQL indexes on conversation_id, customer_id, quote_id for efficient queries. JSONB indexing for nested queries.

**[Risk] State Consistency Under Concurrent Updates → Mitigation:** Single transaction per message processing ensures atomic updates. Use PostgreSQL's row-level locking for quote updates. Deduplication via message_id prevents duplicate processing.

**[Risk] Prompt Size Growth → Mitigation:** Limit conversation history to last 20 messages. Cache quote structure (not quantities) to keep customer context small. Monitor token usage per request.

**[Trade-off] JSONB Flexibility vs. Query Performance:** JSONB for messages provides schema flexibility but is slower to query than normalized tables. Acceptable because we primarily query conversation metadata (conversation_id, customer_id), not message contents. If message content queries become frequent, we can add a full-text search index.

**[Trade-off] Synchronous Processing vs. Throughput:** Synchronous message processing simplifies the architecture but limits throughput. Acceptable for this use case because we're processing single messages, not batching. If throughput becomes an issue, we can add a message queue later without changing the data model.

**[Trade-off] Prompt Ordering vs. Explicit Cache Control:** We get most of the caching benefit from stable prefixes without any cache administration, but we cannot measure hit rates or force a warm entry. Acceptable because the alternative was five endpoints and a monitoring concern; if cost becomes a real problem, explicit breakpoints are a change to `app/extraction.py` alone, not to the data model.

**[Trade-off] Pending Review Queue vs. Auto-Resolution:** Pending reviews require human intervention, which slows processing. Acceptable because incorrect quote updates are worse than delays. The queue prevents pushing bad data to the ERP.
