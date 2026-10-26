# Presentation diagrams

Renders natively on GitHub. Narrative order: **problem → discipline → design decision**.

---

## 1. The problem, told as it happens (sequence)

*Opening slide. Let the audience feel the trap before any solution talk. The
punchline is step 8: the revision arrives in the wrong chat, and the chat does
not decide — the content does.*

```mermaid
sequenceDiagram
    autonumber
    participant A as Alice's chat 💬 (wa-thread-411)
    participant M as Mario's chat 💬 (wa-thread-662)
    participant E as Engine
    participant S as State (Postgres)

    rect rgb(235, 245, 255)
    Note over A,S: Phase 1 — fragmented chat becomes a draft quote
    A->>E: "20 rollos lona 3.20, 5 mesh, 5 microperforado"
    E->>S: creates Q-001 with 3 line items
    Note over E,S: every line stamped with the message that wrote it
    end

    rect rgb(255, 248, 235)
    Note over A,S: Event 1 — a revision, same chat
    A->>E: "de las lonas mejor que sean 15, no 20"
    E->>S: Q-001 · lona qty 20 → 15
    end

    rect rgb(255, 248, 235)
    Note over A,S: Event 2 — a SECOND chat asks about the SAME quote
    M->>E: "soy Mario de compras, ¿qué lleva la cotización de las lonas?"
    E->>S: no lines change — chat is now linked to Q-001
    Note over E,S: Q-001 now has two chats feeding it
    end

    rect rgb(255, 248, 235)
    Note over A,S: Event 3 — "aparte": one chat now feeds TWO quotes
    A->>E: "y aparte cotízame 2 acrílicos blancos"
    E->>S: creates Q-002 with 2× Acrílico
    end

    rect rgb(255, 240, 240)
    Note over A,S: Event 4 — the test: a revision arrives from the OTHER chat
    M->>E: "los acrílicos que cotizó Alice, mejor que sean 4"
    E->>S: Q-002 · acrílico qty 2 → 4
    Note over M,S: it arrived in Mario's chat, but "los acrílicos" names the quote —<br/>the chat it arrived in does NOT decide
    end

    Note over A,S: Final state: both quotes linked to both chats, every quantity correct,<br/>every quantity traceable to the message that wrote it
```

**Presenter notes:** open with Izzy keeping all this "in her head" today; each
numbered arrow is one WhatsApp message. The naive designs die here: a chat
holding a `quote_id` breaks at Event 3; a quote holding a `chat_id` breaks at
Event 2.

---

## 2. The discipline: LLM for meaning, code for state (flowchart)

*The CTO slide. One pipeline, two exits: commit or park. The LLM never writes
state; the validator makes hallucinated SKUs impossible by construction; the
review queue is the "never guess" guarantee.*

```mermaid
flowchart TD
    MSG["📥 Message arrives<br/>(POST /messages)"] --> DUP{"message_id<br/>already processed?"}
    DUP -- "yes: webhook retry" --> INERT["no-op —<br/>state untouched"]
    DUP -- "no" --> CTX["🔍 Build context<br/>customer · open quotes + lines<br/>last 20 messages of each linked chat"]
    CTX --> LLM["🧠 One LLM call<br/>classify + extract:<br/>intent · confidence · target quote · line items"]
    LLM --> VAL{"⚙️ Deterministic validation<br/>SKUs exist in catalog? quantities sane?<br/>confidence ≥ 0.7? target quote open?"}
    VAL -- "unclear intent,<br/>low confidence" --> REVIEW["⏸ Parked for human review<br/>nothing pushed toward the ERP<br/>on a guess"]
    VAL -- "valid" --> APPLY["✍️ Apply with code<br/>upsert lines · stamp provenance<br/>write chat ↔ quote links"]
    APPLY --> DB[("🗄️ Postgres<br/>survives restarts")]
    REVIEW --> HUMAN["👤 Human resolves:<br/>apply · new quote · discard"]
    HUMAN --> DB

    style VAL fill:#fff4e6
    style REVIEW fill:#ffebee
```

**Presenter notes:** the split is the whole argument — *the model understands,
code decides*. Model errors become review items, never corrupt quotes. CEO
framing: the review queue is what makes this deployable — an ambiguous
"mejor cancelalo" never cancels the wrong quote; it asks a human instead.

---

## 3. The design decision that survives all four events (ER)

*Closing slide. Only the tables that matter. The many-to-many is the punchline:
neither obvious shortcut survives, this does — no event needs a workaround.*

```mermaid
erDiagram
    CUSTOMERS ||--o{ CONVERSATIONS : "opens threads"
    CUSTOMERS ||--o{ QUOTES : "receives quotes"
    CONVERSATIONS }o--o{ QUOTES : "🔗 many-to-many:<br/>a quote fed by many chats,<br/>a chat feeding many quotes"
    QUOTES ||--o{ LINE_ITEMS : "contains"
    LINE_ITEMS {
        string sku
        int quantity
        string created_by_message_id "which message created it"
        string last_changed_by_message_id "which message last revised it"
    }
    QUOTE_CONVERSATIONS {
        string message_id "which message created the link"
    }
    QUOTES ||--o{ QUOTE_CONVERSATIONS : ""
    CONVERSATIONS ||--o{ QUOTE_CONVERSATIONS : ""
```

**Presenter notes:** the join table carries provenance too — you can answer
"which message linked Mario's chat to Q-001?" The line items keep per-line
message provenance, so a human sees exactly what changed and why. Two shortcuts
both break; the join table means all four events are just rows.

---

## Optional 4th, only if asked about scale

If the CEO/CTO probe on "what happens at 1,000 conversations/day", have this
one ready — but don't lead with it:

```mermaid
flowchart LR
    subgraph today["Today (1,000/day fits)"]
        W1["single process"] --> PG[(Postgres)]
    end
    subgraph next["First changes under pressure"]
        Q["queue partitioned by<br/>conversation_id"] --> W2["worker per chat<br/>no interleaved revisions"]
        W2 --> PG2[(Postgres)]
        CACHE["prompt cache:<br/>static catalog + rules = 0.1× cost<br/>per-token window"]
    end
    METRIC["📈 metric that matters:<br/>review-queue depth =<br/>how good the prompts are"]
```

Suggested story: cost dominates first (mitigated by prompt-cache shape), then
ordering (one worker per chat), then the review-queue depth metric tells you
when prompts need tuning.
