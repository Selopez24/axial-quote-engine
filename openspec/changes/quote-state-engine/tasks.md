# Tasks

## 1. Project Setup & Database

- [x] 1.1 Initialize FastAPI project with basic structure (app/, tests/) and verify it runs
- [x] 1.2 Set up PostgreSQL with tables: customers, conversations, quotes, line_items,
      quote_conversations, pending_reviews, processed_messages (SQLAlchemy create_all)
- [x] 1.3 Add database configuration and verify connection works
- [x] 1.4 Seed reference data only (catalog_items + customer master) idempotently on boot,
      mirrored in db/seed.sql; conversation/quote state is deliberately never seeded

## 2. Core Data Models

- [x] 2.1 Implement SQLAlchemy models for Customer, Conversation, Quote, LineItem,
      QuoteConversation, PendingReview, ProcessedMessage with relationships
- [x] 2.2 Implement basic repository methods: create/read per model, append message to
      conversation, link quote-conversation

## 3. Message Processing Pipeline

- [x] 3.1 Implement POST /messages endpoint with request validation (Pydantic)
- [x] 3.2 Implement message deduplication (check message_id, skip if already processed)
- [x] 3.3 Implement conversation creation/update logic (create if new, append if exists)
- [x] 3.4 Implement customer identity storage (create customer record if not exists)
- [x] 3.5 Implement message direction handling: inbound triggers extraction, outbound stores only
- [x] 3.6 Create pipeline orchestration: dedup → store customer → store message →
      extract (if inbound) → validate → update state
- [x] 3.7 Implement POST /conversations endpoint (testing utility) that accepts the batch
      conversation format and processes each message sequentially

## 4. LLM Integration & Quote Extraction

- [x] 4.1 Set up OpenAI client with API key and basic configuration
- [x] 4.2 Implement prompt construction with conversation history, catalog, and quote context
- [x] 4.3 Implement LLM output parsing (intent, quote_id, line_items)
- [x] 4.4 Implement catalog validation: reject SKUs not in catalog (the 4 SKUs from assignment)
- [x] 4.5 Implement confidence threshold: if < 0.7 or intent unclear, create pending review

## 5. Conversation Linking & Quote Management

- [x] 5.1 Implement quote resolution: determine which quote a message belongs to based
      on content and context
- [x] 5.2 Implement quote-conversation linking: create QuoteConversation records when
      messages reference quotes
- [x] 5.3 Implement quote creation (new_quote intent) and revision (revision intent) logic
- [x] 5.4 Implement cross-conversation reference detection (Event 2: Mario references Alice's quote)

## 6. Ambiguity Resolution

- [x] 6.1 Implement pending review creation for ambiguous messages (low confidence, unclear intent)
- [x] 6.2 Implement GET /pending-reviews and POST /pending-reviews/{id}/resolve endpoints
- [x] 6.3 Implement follow-up message handling: append new messages to existing pending reviews

## 7. API Endpoints

- [x] 7.1 Implement GET /quotes?customer={customer_id} that returns all quotes with line
      items and conversations
- [x] 7.2 Implement GET /quotes/{quote_id} that returns specific quote with full state

## 8. Testing & README

- [x] 8.1 Write integration test that processes all 4 assignment events and verifies final
      state:
      - Q-001: qty 15 (ALFBTC-013-320100), qty 5 (ALMCLC-0909-320100), qty 5 (AVMICS-1A-137050),
        conversations: [wa-thread-411, wa-thread-662]
      - Q-002: qty 4 (ARACBL-03-122244), conversations: [wa-thread-411, wa-thread-662]
      - Verified live on gpt-6-luna (OpenCode Go gateway); see 10.6–10.8 for what the
        real-model runs exposed
- [x] 8.2 Create README.md with the required sections: data model diagram, message-to-quote
      walkthrough (especially Event 4), "mejor cancelalo" paragraph, architectural
      trade-offs, scaling considerations (1,000 conversations/day), AI-tool-usage paragraph,
      extra-credit testing guide

## 9. Extra Credit

- [x] 9.1 Duplicate delivery detection (re-send Event 4 with same message_id, verify state
      doesn't change)
- [x] 9.2 "cancelalo" as a real feature: the ambiguous message lands in the pending review
      queue with candidate quotes; a human resolves via POST /pending-reviews/{id}/resolve

## 10. Review & Hardening Passes

Spec-vs-implementation audits and real-model verification. Design decisions and the
measurements live in design.md; every behavior here is pinned by tests
(55 offline / 7 keyed on the real model):

- [x] 10.1 Attribution & idempotency: an outbound is never labeled with the customer's
      name; JSONB appends dedupe by message_id, so a retried message cannot double the
      transcript or a review's follow-ups
- [x] 10.2 Quote-resolution rules: conversation-level single-referent fallback for
      target-less revision/reference (guesses go to a human), reverse retrieval
      GET /quotes?conversation={id}, context-window tests (last-20 per linked chat,
      arriving chat included while unlinked)
- [x] 10.3 Spec/code alignment: batch endpoint returns the resulting quote state and honors
      per-message `sender_name`; document columns are JSONB as Decision 1 says; quote-status
      vocabulary and seeding scope trimmed to what the engine actually writes; added the
      suite's silent spec scenarios (invalid quantity, batch timestamp order, failure 500 +
      rollback, resolve-as-new-quote, review survives restart, redelivery after failure)
- [x] 10.4 Responses API request shape per the official docs: static instructions + catalog
      as the leading developer message carrying one explicit cache breakpoint (top-level
      `instructions` cannot hold one on GPT-5.6+), default implicit caching (per-turn writes
      are deltas, so threads cache incrementally — measured), `store=False`,
      `reasoning.effort: "low"`, no temperature on GPT-5.6+ (legacy instructions+temperature
      shape kept for gpt-4o-mini), gateway support via OPENAI_BASE_URL
- [x] 10.5 Cache-stable rendering: chat lines render as "[timestamp] speaker (company):
      text" from stored data alone — byte-stable forever, append-only, the classified
      message is simply the last line; message bytes never change, so cross-turn cache
      breakpoints stay aligned
- [x] 10.6 Real-model runs exposed prompt rules the offline fake could not: availability
      questions are not line items and never force unclear (rule 5); a later quantity
      answering a pending availability question is a revision that adds the line, not a
      new_quote (rule 2); topic recency is not a content clue — only explicit mention or an
      adjacent clarification exchange is (rule 3)
- [x] 10.7 Review self-heal: an inbound follow-up retries the parked request — the
      follow-up is classified with the parked message's text and issues in context, and if
      it clarifies, the combined result applies under the ORIGINAL message id (provenance
      stays on the message that asked). Non-clarifying follow-ups only enrich context;
      ambiguity that is not an intent the engine writes ("cancelalo") never self-heals and
      keeps waiting for a human
- [x] 10.8 Out-of-guide live probes added `intent: "other"`: pure availability questions,
      greetings, and thanks pass through inert (parking them would swallow the rest of the
      thread; `unclear` now demands a real quote decision, and a self-contradictory "other"
      parks as inconsistent_extraction). Firm off-catalog orders and a standalone
      "mejor cancelalo" park in their own reviews. Re-gated live: 7/7 keyed tests +
      the full self-heal sequence on gpt-6-luna
