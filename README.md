# Quote & State Engine

Headless API that turns fragmented WhatsApp threads into structured `Oferta de Venta`
quotes and keeps them correct as messages keep arriving — revisions, a second chat
asking about the same quote, new requests mixed into old ones.

No UI. It's `POST` a message, `GET` the quotes.

```bash
docker compose up -d && pip install -r requirements.txt
uvicorn app.main:app --reload          # http://localhost:8000/docs
pytest -q                              # 55 tests pass, no API key needed
```

## What's in the database on first boot (and what isn't)

Starting the app creates the schema and loads **reference data only**:

```bash
curl 'http://localhost:8000/quotes?customer=C-10029'   # -> {"quotes": []}  nothing seeded
```

| seeded | what |
|---|---|
| `catalog_items` | the 4 SKUs from the assignment — the "mock DB" the validator checks against |
| `customers` | `C-10029` = ACME INDUSTRIAL, company known, person **not** guessed (`customer_name` NULL until a message names one) |

Both `init_db()` on boot (idempotent, `ON CONFLICT DO NOTHING`) and `db/seed.sql` carry
the same rows, and `tests/test_seed.py` asserts the two cannot drift.

Transcripts, quotes and line items are **deliberately not seeded**: that state is the
thing under evaluation, so it has to come out of the pipeline, not from a fixture. Load
the example thread yourself and watch it appear (**needs `OPENAI_API_KEY`** — the inbound
messages go through the model):

```bash
curl -X POST http://localhost:8000/conversations -H 'content-type: application/json' -d '{
  "customer_id": "C-10029", "conversation_id": "wa-thread-411",
  "customer_name": "Alice - ACME INDUSTRIAL", "rep_name": "Izzy",
  "conversation_history": [
    {"direction":"inbound","timestamp":"2026-02-10T09:43:00Z","text":"Hola buenos dias, me ayudas a cotizar 20 rollos lona 3.20 x 100 m., 5 rollos de mesh de 3.20 x 100 m. y si cuentas con microperforado de 1.37 y 1.50 m."},
    {"direction":"outbound","timestamp":"2026-02-10T09:45:00Z","text":"Hola buen día. Si. Cuantas piezas de microperforado"},
    {"direction":"outbound","timestamp":"2026-02-10T09:45:30Z","text":"De momento sería de 1.37x50"},
    {"direction":"inbound","timestamp":"2026-02-10T09:46:00Z","text":"Necesitamos 5 rollos"}
  ]}'
# with a key: the POST response itself carries the quote under "quotes" — Q-001 at
# 20 / 5 / 5 — and GET /quotes?customer=C-10029 says the same
# without one: {"quotes":[]} and the thread parks in GET /pending-reviews, because
# refusing to invent a quote is the same rule that catches a hallucinated SKU
```

An unseeded database fails *safe*: with no rows in `catalog_items` every SKU is unknown,
so everything routes to review rather than inventing a catalog.

---

## Data model

The two shortcuts the assignment warns about both break, so the connection between
quotes and conversations is its own table:

```
customers ──1:N──> conversations                customers ──1:N──> quotes
                          │                                              │
                          │        quote_conversations (join)            │
                          └──────────────────< >─────────────────────────┘
                                                 │
                                     quotes ──1:N──> line_items (sku, qty)
```

| table | key columns | why |
|---|---|---|
| `customers` | `customer_id`, `customer_name`, `company_name` | identity reused across chats (Alice and Mario are both C-10029); the company is seeded, the person arrives with the first message |
| `catalog_items` | `sku`, `description` | the assignment's "mock DB"; the only authority on what a valid SKU is |
| `conversations` | `conversation_id`, `customer_id`, `messages JSONB` | a thread holds its transcript; it does **not** hold a quote_id |
| `quotes` | `quote_id`, `customer_id`, `status` | a quote does **not** hold a conversation_id |
| `quote_conversations` | `quote_id` + `conversation_id`, `UNIQUE` pair | many-to-many: one chat feeds many quotes, one quote lives in many chats |
| `line_items` | `quote_id` + `sku`, `qty`, `UNIQUE(quote_id, sku)` | current state; one row per SKU per quote |
| `pending_reviews` | `review_id`, `original_message`, `candidate_quotes`, `resolution` | the "don't guess" queue |
| `processed_messages` | `message_id` PK, `processing_result` | dedup + audit trail |

Neither obvious shortcut is possible here: there is no `quote_id` column on
`conversations` (event 3 would need two of them) and no `conversation_id` column on
`quotes` (event 2 would need two of those). Every event is an insert into the join
table or an update to a line item — no workaround, no array-append special case.

`line_items` carries provenance in place of a history table:
`created_by_message_id` and `last_changed_by_message_id`, which join to
`processed_messages.processing_result` where the actual change (`from: 20, to: 15`)
is recorded. `GET /quotes/Q-001?history=true` returns both.

Read state back in whichever direction you're coming from — the join table makes both
retrievals real, not just the customer one:

```bash
curl 'http://localhost:8000/quotes?customer=C-10029'       # what this company has open
curl 'http://localhost:8000/quotes?conversation=wa-thread-662'  # what this chat is about
curl 'http://localhost:8000/quotes/Q-001'                  # one quote, both ways
```

---

## How a new message finds its quote

`POST /messages` runs one sequential pipeline, one transaction
(`app/pipeline.py`):

1. **Dedup** — `message_id` already in `processed_messages`? return, change nothing.
2. **Store** — customer if new, conversation if new, message appended to the thread.
3. **Open-review gate** — if this conversation has a `pending` review, the message is
   appended to it as context. It is *not* reinterpreted as its own state change — but
   if it's **inbound**, it is retried as a *clarification* of the review's parked
   message, and if that combined result validates, the change applies under the
   parked message's provenance and the review closes itself as `self_resolved`.
   A follow-up never mutates a quote on its own; it only ever completes the parked one.
4. **Direction gate** — outbound is context only; it never mutates a quote.
5. **Load context by `customer_id`** — the company, not the chat. Every open quote for
   C-10029 plus the last 20 messages of *every* conversation linked to those quotes.
   So when Mario writes in a brand-new thread, the model is already looking at
   Alice's conversation and at Q-001's lines.
6. **Extract** — one LLM call, structured output: `intent`, `confidence`,
   `target_quote_id`, `line_items`.
7. **Validate deterministically** — SKU in the `catalog_items` table, quantity a positive
   int under a sane ceiling, confidence ≥ 0.7, target quote exists and is open. With no
   `target_quote_id`, a target-less revision/reference resolves to the customer's single
   open quote, or failing that the arriving conversation's single open quote — one
   possible referent is a fact, two is a guess, so two goes to review.
8. **Apply or park** — clean → update state. Anything failing → `pending_review`,
   quote state untouched.

**The important asymmetry (event 4):** the message says *"de los acrilicos que cotizó
Alice, mejor que sean 4"* and arrives in **Mario's** chat, on a quote that **Alice's**
chat created. The quote is chosen by **content** — `target_quote_id`, resolved against
the lines the customer mentioned — and then the arriving conversation is *linked* to
it. The chat it came in on is never an input to that decision, it's only ever added as
a second step. Event 2 is the same code path with `intent=reference` and no line
changes: the thread still gets attached to Q-001, which is exactly what the assignment
asks the system to "remember".

---

## "mejor cancelalo"

With Q-001 and Q-002 both open, *"mejor cancelalo"* is genuinely ambiguous — the chat
doesn't disambiguate it and neither does the text. The model is instructed to return
`intent=unclear` with low confidence, and the deterministic layer turns
`confidence < 0.7`, `intent=unclear`, an unknown SKU, a bad quantity, or an
unresolvable quote reference into a **pending review**: the original message, the
extraction the model attempted, the issues found, and the candidate quotes
(`["Q-001","Q-002"]`). **No quote state changes.** Nothing is pushed toward the ERP on
a guess; a human decides through `GET /pending-reviews` →
`POST /pending-reviews/{id}/resolve` with `apply`, `new_quote`, or `discard`. If the
customer sends more messages first, they're appended to the open review as context
rather than triggering another guess — and the parked message is retried with that new
context, because a follow-up often *is* the clarification ("de las lonas, 15"): the
review then resolves itself and never reaches a human. *"mejor cancelalo"* does not
self-heal, deliberately — cancelling is not an intent the engine writes, so the retry
confirms unclear and the review waits.

The same rule decides when a message is *not* ambiguous: with two quotes open, a
revision that names no product still resolves if the arriving conversation works only
one of them — the chat is a fact about context, not a guess. It only becomes a review
when the conversation itself touches both.

And not everything is a decision at all: a pure availability question about a product
on no open quote (*"si cuentan con manguera?"*) or a plain thank-you (*"muchas gracias,
buen fin de semana!"*) classifies as `intent=other` and passes through inert — no quote
touched, no review opened, nothing for a human to triage. `unclear` is reserved for
messages that genuinely demand a quote decision. Parking anything else has a hidden
cost: while a review is open, later messages of that thread append to it as context,
so one wrongly-parked greeting can quietly swallow a whole conversation until acted on.

---

## Trade-offs

- **LLM understands, code decides.** Every write to `line_items` goes through the
  catalog check in `app/validator.py`, so an invented SKU becomes a review, not a row.
  The model cannot corrupt state even when it's wrong.
- **Postgres over graph/event-sourcing.** The relationship is a join table; at this
  volume nothing justifies another system. We keep current state plus per-line
  provenance rather than a full event log — enough for a human to see what changed and
  why, without replay machinery.
- **Synchronous processing.** One message in, one answer out. Simple, and correct while
  volume is low. The cost is that a worker blocks on model latency.
- **JSONB transcript on the conversation row.** Cheap to store, but every append
  rewrites the array and every call re-reads it. Fine here, the first thing to change at
  scale (below).
- **Prompt shape as the caching strategy.** No caching infrastructure: prompts are
  ordered so the stable content is a reusable prefix — static instructions + catalog as a
  leading developer message carrying one explicit cache breakpoint, then the customer's
  open-quote structure, then the chat as append-only lines of
  `[timestamp] speaker (company): text`, newest last. Each line renders from its stored
  data alone — no role markers that mutate as a message ages — so a message contributes
  identical bytes to every call that contains it. That is the shape GPT-5.6+ caching
  rewards: reads bill at 0.1x input, writes cost 1.25x, and reuse only aligns at message
  boundaries — a flattened, ever-growing history block would miss even with a perfect
  prefix. We stay on the API's default implicit mode because it is what caches the
  *growing thread*, not only the static base: its automatic breakpoint at the newest
  message makes each turn a small delta write that the next turn reads back. Measured
  live on a three-turn thread — stable turn: `cached=1359, cache_write=37`; a turn that
  changes quote state mutates the customer-context line, so it falls back to the shared
  developer-prefix read (`cached=1226`) and rewrites the thread delta, which the following
  turns reuse again. The tuned instructions block measures 1,226 tokens — above the
  1,024-token cacheable minimum — even with the 4-SKU seed, so every conversation shares
  that read at 0.1x from the first turn. Long back-and-forth conversations get *cheaper*
  per message as they grow, which is the point: we traded cache administration for prompt
  discipline and zero moving parts.
- **Stateless by choice; `store=False`.** Every call re-sends the full input and we never
  fetch responses back, so we opt out of server-side response storage — the Responses API
  default would retain each rendered customer transcript for 30+ days for no benefit here.
- **Direct normalized input, no webhook parser.** We accept the assignment's message
  shape (`customer_id`, `conversation_id`, `direction`, `text`) as the API contract. In
  production a thin handler would map `wa_id` → `customer_id` upstream; building that
  here would spend time on data plumbing that Phase 2 doesn't care about.

### At 1,000 conversations/day

1,000/day is under one message per minute — CPU, connections and Postgres are not the
problem. What actually breaks first, in order:

1. **Prompt size and token cost.** Re-sending a whole transcript per message grows
   linearly with thread length and dominates spend. Fix: last-N window (we cap at 20),
   the prefix ordering above, and move the transcript into its own `messages` table
   indexed by `(conversation_id, created_at)` so we read a window instead of rewriting
   a JSONB blob per message.
2. **Synchronous model calls.** One slow call holds a worker; a timeout during a burst
   becomes a backlog. Fix: accept → enqueue → process, with the `message_id` we already
   store as the idempotency key, retries, and a dead-letter path. This is exactly why
   dedup is built in from day one.
3. **Per-customer row contention.** Mario and Alice can revise different quotes at the
   same second; `next_quote_id` and line updates take row locks. Fix: `SELECT ... FOR
   UPDATE` on the quote being touched, or serialize per `customer_id`.
4. **Human review queue.** Ambiguity doesn't get cheaper with volume. Fix: surface
   reviews into the rep's workflow (Slack/WhatsApp ping) and measure the queue's age
   rather than adding confidence heuristics that guess wrong.
5. Later and cheaply: PgBouncer, read replicas for `GET /quotes`, partitioning
   `processed_messages` by month as the audit table grows.

We'd change none of the data model for this. That's the point of the join table.

---

## Extra credit

**Duplicate delivery** — dedup is on `message_id`, never on text, so a genuine repeat
request still counts as a new instruction:

```bash
curl -X POST http://localhost:8000/messages -H 'content-type: application/json' \
  -d '{"message_id":"wamid.662.0002","conversation_id":"wa-thread-662","customer_id":"C-10029","direction":"inbound","timestamp":"2026-02-11T10:40:00Z","text":"perfecto. y de los acrilicos que cotizó Alice, mejor que sean 4"}'
# replay it byte-identical -> {"status":"duplicate", ...} and GET /quotes is unchanged
pytest tests/test_phase2.py -k "duplicate or same_text"
```

**"cancelalo" as a feature** — the ambiguous message lands in a queue with its
candidates and nothing moves until a person decides:

```bash
curl http://localhost:8000/pending-reviews
curl -X POST http://localhost:8000/pending-reviews/REV-wamid.411.0600/resolve \
  -H 'content-type: application/json' \
  -d '{"action":"apply","target_quote_id":"Q-002","line_items":[{"sku":"ARACBL-03-122244","quantity":4}]}'
```

---

## Running the whole assignment scenario

```bash
export OPENAI_API_KEY=***   # only needed for the real model path
pytest -q                                  # deterministic suite, always green
pytest tests/test_integration_llm.py -v    # same 4 events through the real model
```

The default model is `gpt-6-luna`, served by both OpenAI and OpenCode Go's
`/v1/responses` endpoint. To run the real-model tests through a Go subscription
instead of a direct OpenAI key, the key goes in `OPENAI_API_KEY` and:

```bash
export OPENAI_BASE_URL=https://opencode.ai/inference/go/openai/v1
export QUOTE_ENGINE_MODEL=gpt-6-luna   # the default; gpt-5.6-luna also works
```

The four-event scenario was verified live through `gpt-6-luna` (OpenAI Responses API)
on 2026-10-22: 7/7 integration tests green, final state exactly as `ASSIGNMENT.md` shows.
The suite includes out-of-guide cases on purpose — a pure availability question and a
thank-you must pass inert (`intent=other`) — and one of those probes earned its place:
with `reasoning.effort=low` the model read a loose rule-5 clause literally, parked pure
questions as unclear, and each parked review then quietly swallowed every later message
of that thread. The rules were tightened and `other` added; effort was kept.

`tests/test_phase2.py` proves the state machine with the extractor stubbed, because the
engineering under test — linking, revisions, dedup, the review gate — is deterministic
and shouldn't depend on a network call to be checked. `tests/test_spec_coverage.py`
covers the narrower spec requirements (speaker attribution, quantity validity, batch
ordering, failure rollback, retrieval in both link directions, retryable extraction
failure), and `tests/test_seed.py` holds `db/seed.sql` and `app/seed.py` to the same
rows. `tests/test_integration_llm.py` runs the identical four-event scenario through
OpenAI and asserts the assignment's expected final state. All four run against a separate
`quote_engine_test` database and roll back per test, so the suite never touches dev data.

---

## AI tool usage

Used honestly: an agent (Claude Code / OpenCode) did the first draft of this
repo's OpenSpec artifacts — proposal, design decisions, spec deltas, task list — and
most of the implementation and test scaffolding, with me reviewing each step. The
direction changed because of that review, not because the tool volunteered it: I
started with a webhook-normalization layer and explicit cache-breakpoint endpoints,
pushed back on both as over-engineering for a 6-hour box, and they were cut — which is
why this README describes prompt ordering instead of a caching subsystem. Two
corrections came from running things rather than reading them: SQLAlchemy 2.x silently
does not track in-place mutation of a JSONB column (an append needed reassignment), and
`.env` holding a placeholder API key made "key present" tests run against a fake
credential. A third came from arguing about scope: the first proposal for prefilling the
database was to seed the finished assignment state so a reviewer could see the expected
answer with no API key, and that was wrong — a fixture presenting itself as model output
hides the thing being evaluated. Seeding was cut back to reference data (catalog,
company), which also turned the catalog from a Python dict into the `catalog_items` table
the assignment calls for. Where a judgment call was the model's, I flagged it: the fourth
intent value (`reference`), the two provenance columns on `line_items`, and the Option-A
vs. Option-B history trade-off were raised for discussion rather than assumed. The Spanish
prompt, the catalog, and the whole interpretation of what event 4 is testing came from
the assignment, not from me asking the model to invent a design.
