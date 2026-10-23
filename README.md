# Quote & State Engine

Headless API that turns fragmented WhatsApp threads into structured `Oferta de Venta`
quotes and keeps them correct as messages keep arriving — quantity revisions, a second
chat asking about the same quote, new requests mixed into old ones. No UI: you `POST`
messages, you `GET` quotes.

## Run it

```bash
# Option A: One-command Docker (API + DB)
docker compose up --build            # http://localhost:8000/docs

# Option B: Local dev (DB in Docker, API local)
docker compose up -d db
pip install -r requirements.txt
uvicorn app.main:app --reload        # http://localhost:8000/docs

# Tests
pytest -q                            # 57 tests pass with no API key
```

The app creates its schema on boot and seeds **reference data only** (the 4-SKU catalog,
customer `C-10029`). Transcripts, quotes and reviews are never seeded — that state is
the thing under evaluation, so it must come out of the pipeline, not from a fixture.

**To start with the assignment scenario pre-loaded** (Phase 1 batch already processed):
set `SEED_SCENARIO=1` along with `OPENAI_API_KEY` before starting. The seed runs the
Phase 1 batch through the pipeline (requires the LLM for extraction). Without an API key,
the seed is skipped and the app starts with an empty state.

- Docker: `SEED_SCENARIO=1 OPENAI_API_KEY=*** docker compose up --build`
- Local: `SEED_SCENARIO=1 OPENAI_API_KEY=*** uvicorn app.main:app --reload`

After the seed completes, query `GET /quotes?customer=C-10029` — you'll see Q-001 with the
Phase 1 line items, ready to receive Events 1–4 via `POST /messages`. Payloads for all
events are in `tests/test_integration_llm.py` (`PHASE1`, `EVENTS`); with an
`OPENAI_API_KEY` set, `pytest tests/test_integration_llm.py` runs the full flow through
the real model (7/7, verified live on `gpt-6-luna` via the OpenAI Responses API).

| Endpoint | |
|---|---|
| `POST /conversations` | batch import (Phase 1 format); processes chronologically, returns resulting state |
| `POST /messages` | one live event; deduped by `message_id` |
| `GET /quotes?customer={id}` / `?conversation={id}` | quotes with line items and linked chats — retrieval in both directions |
| `GET /quotes/{id}?history=true` | per-line provenance: which message created / last changed each qty |
| `GET /pending-reviews` · `POST /pending-reviews/{id}/resolve` | the human queue, and the human decision |

## The data model

The four events in the assignment kill both shortcuts: a conversation holding a `quote_id`
breaks at Event 3 (one chat feeding two quotes); a quote holding a `conversation_id`
breaks at Event 2 (one quote fed by two chats). So the link is its own table:

```
customers ──< conversations >──┐
   │                           │
   │                           │
   └──< quotes >───────────────┘
         │
         │ (via quote_conversations: many-to-many)
         │
         └──< line_items
```

| table | key fields | why it's shaped this way |
|---|---|---|
| `customers` | `customer_id` (PK), `company_name`, `customer_name` | `company_name` is known from the master before anyone messages; `customer_name` is whoever the first inbound message names. Never inferred. |
| `conversations` | `conversation_id` (PK), `customer_id` (FK), `messages` (JSONB array) | One row per WhatsApp thread. JSONB array keeps reads trivial at this scale; per-message rows are the first migration under real traffic. |
| `quotes` | `quote_id` (PK), `customer_id` (FK), `status` | `status` is always `draft` — the engine extracts and tracks, it does not approve or cancel. A quote's identity is `(customer, content)`, never a chat. |
| `quote_conversations` | `quote_id` (FK), `conversation_id` (FK), `metadata` (JSONB) | The many-to-many join. `metadata` stores the `message_id` that created the link, so you can answer "which message linked this chat to this quote?" A quote can have many chats; a chat can feed many quotes. This is what survives all four events. |
| `line_items` | `quote_id` (FK), `sku`, `quantity`, `created_by_message_id`, `last_changed_by_message_id` | One row per `(quote, sku)` — the `UNIQUE` constraint makes duplicate lines unrepresentable. Provenance fields trace every quantity back to the message that wrote it. |
| `catalog_items` | `sku` (PK), `description` | The assignment's "mock DB". The validator filters every extracted line against these rows, so a hallucinated SKU is impossible in the output. |
| `pending_reviews` | `review_id` (PK), `original_message`, `llm_extraction`, `resolution_issues`, `candidate_quotes`, `follow_up_messages`, `resolution` | Parked messages. Nothing is pushed toward the ERP while a review is open. `follow_up_messages` collects later messages that clarify; `resolution` is the human's decision. |
| `processed_messages` | `message_id` (PK), `processing_result` | Dedupe gate. Re-delivering the same message is a no-op. |


## What happens when a message arrives

Mario, in `wa-thread-662`: *"perfecto. y de los acrilicos que cotizó Alice, mejor que sean 4."*

1. **Dedupe** on `message_id`. Outbound messages are stored as context only.
2. **Load context** for the customer, not just the arriving chat: open quotes with line items and linked conversations, plus the last 20 messages of each thread. Because Event 2 linked Mario's chat to Q-001, Q-002's acrylics are visible, so Mario's chat can see a quote born in Alice's.
3. **LLM translates.** One structured call returns `{intent, confidence, target_quote_id, line_items, reason}`. Instructions force routing by content: *"los acrilicos"* names a product, so it binds to the quote containing that SKU (Q-002). With no content clue and several open quotes it must emit `unclear`, escalating to human supervision.
4. **Deterministic validation.** Every SKU must exist in the catalog, quantities must be sane, confidence must clear 0.7, targets must exist and be open. A hallucinated SKU is *impossible* because the validator rechecks every line against `catalog_items` before a row is touched. The LLM understands, code decides.
5. **Apply or park.** Valid: line items are upserted, provenance stamped, conversation↔quote links written. Invalid: nothing changes; the message goes to the review queue.

State lives in Postgres; a restart mid conversation loses nothing.

## Cancellation

Cancellation is a first class intent, but it only fires when the target is unambiguous. *"cancela la Q-002"* names a quote; *"quita las lonas"* names a product. Both execute immediately: status flips to `cancelled` and line items are removed (or just the named SKUs for partial cancellation). *"Mejor cancelalo"* with Q-001 and Q-002 both open is genuinely ambiguous, so the model returns `unclear` and creates a pending review. No quote state changes on a guess.

The rule: if the customer names the target, cancel. If not, park it. Canceling the wrong quote is worse than asking one clarifying question.

A later message that clarifies a parked request resolves it automatically under the original message's id. Non quotes (greetings, pure availability questions) classify as `intent: other` and pass through inert.

## Trade-offs

- **LLM for meaning, code for state.** The model sees one job (classify + extract) and
  its output is re-validated before any write. Model errors become review
  items, never corrupt quotes. A smaller model with low effort is being used to optimize token 
  usage.
- **Simple SQL DB.** The relationship is a join table; the audit
  trail rides on the rows that change (per-line message provenance) instead of a second
  write path/table, to keep it simpler and self-contained. 
- **Prompt shape instead of caching infrastructure.** The static instructions + catalog ride in a leading developer message with one cache breakpoint. Stable turns read the whole thread back at 0.1× cost, no cache administration needed. `store=False` keeps customer transcripts out of server-side response storage. We also avoided using any other application layer caching yet and we are using default caching TTL on LLM side, assuming conversation will flow and conclude rapidly, simplifying caching strategy. 
- **Accept the assignment's payload shape directly.** No webhook parser for WhatsApp's
  raw format; `direction`/`timestamp`/`message_id` are taken as given. Pragmatic for a
  take-home, would be the first real adapter in production.
- **JSONB transcripts over a messages table.** One row per conversation keeps reads
  trivial at this scale; per-message rows are the first migration under real traffic.
- **Model selection**. OpenAI was selected for pricing and cache strategy, system prompt was hardened to work on less capable model (Luna) to reduce token usage. This required more prompt engineering but saves costs long term. 
- **Embed speaker names in content text**. Adding more metadata in the context we reduce hallucination and increase accuracy despite adding more tokens in the input.
- **Final validation and cache**. Final validation step prevents a quote of being corrupted, it also allows to simplify dynamic data added to the context to reduce cache invlidation, for example by omitting the quantity of each item from the LLM input, instead validating it deterministically. Only new SKUs or patching their name would invalidate most of the initial context cache.
- **Adding SKUs to context instead of tool**. For smaller and stable catalogs is acceptable to add the list of SKUs to the context, but if the list is larger and/or changes constantly probably a tool call or another previous step to extract and match items can be more efficient on token usage but may increase latency. 
- **Every message is processed** To simplify design and UX of the product every message is processed, this causes more output token costs but keeps operations simpler. A trigger based strategy to process a conversation could help to reduce costs but also can complicate how the pipeline works. 

## If Acme processed 1,000 conversations a day

1,000/day is under one message per minute — and the honest answer is that **nothing
breaks first**: Postgres idles, the single-process app copes, correctness doesn't
degrade. What you'd actually change, in order of pressure:

1. **Token cost, cache and latency** already dominate the profile (~20 messages × full context
   per classification). The prompt-cache shape above is the mitigation; the last-20
   window and per-customer context build are the knobs. We can fine-tune parameters to build a more accurate context at this scale, this includes revisiting the strategy of adding the SKUs to the system prompt and the TTL config.
2. **Ordering under concurrent delivery.** Two messages of one thread processed
   interleaved could double-apply a revision. Partition one worker per
   `conversation_id` (queue + key-based dispatch); different chats are naturally
   parallel. The dedupe table already makes retries safe.
3. **Write races**: `message_id` PK and FKs are the backstop; quote-id allocation moves
   to a DB sequence; quote updates take a row lock per quote.
4. **Transcript growth**: JSONB → messages table with retention; context loads a sliding
   window from indexed rows.
5. **Observability of the ambiguity rate**: the review queue's depth and what humans
   fix there is the metric that tells you whether the prompts are worth more tuning —
   which is why usage (input/cached/write tokens) is logged per call today.
6. **Model Responds to user**: At this scale if a lot of reviews happen can be hard for a representative to resolve them in time, a new path on the workflow can be added to allow the model to ask the customer for clarification to force a self heal in a future message, reducing human intervention if not needed since the model is able to make a follow up question.
7. **Optimize further** so token usage is under control, especially output tokens. Use structured output and revisit extraction strategy. 
8. **Improve read speed** with application level caching or other strategies to improve response times and reduce load on the main DB. 
9. **Keep in mind API rate limits on LLM provider** to find re-routing strategies if needed. 


## How I used AI

AI to help me in the whole process, I used OpenSpec to work on this using spec-driven development which helped me to validate behavior and keep state of the requirements and decisions. An LLM agent (OpenCode) wrote the implementation, the
tests and the docs drafts, I wrote most of the Readme file. Every behavior
is pinned by a test, and the parts only a real model can prove ran live through
`gpt-6-luna` via the OpenAI Responses API. The human decisions were the ones that matter and came from me:
the data model, "when not to guess" and the self-heal policy, keeping prompt caching
shaped as a property of the prompt rather than infrastructure, and trimming scope, but the model helped me validate architecture and iterate over tradeoffs and decisions. The
live runs earned their keep twice: they exposed classification rules no stub could
surface and a probe beyond the happy path caught the
model parking pure questions which helped catch some edge cases and improve the prompts and debugging better. 

## Extra credit — how to see it

- **Duplicate delivery**: send the same message twice (same `message_id`). First call processes it, second returns `status: duplicate` with no state changes with a quick DB lookup. Tests: `test_duplicate_event4_is_inert` (keyed) + offline redelivery suite.

- **Cancellation**: two modes depending on clarity:
  - *Clear target* → `"cancela la Q-002"` or `"quita las lonas"` executes immediately. Quote status flips to `cancelled`, line items removed.
  - *Ambiguous* → `"mejor cancelalo"` with multiple open quotes parks in review. Human resolves via `POST /pending-reviews/{id}/resolve` with `apply` / `new_quote` / `discard`.
  
  Tests: `test_cancel_entire_quote_with_clear_target`, `test_cancel_specific_products_from_quote`, `test_cancelalo_with_two_open_quotes_goes_to_human_review`.
