"""Spec scenarios the main suite left uncovered.

Each test here names the requirement it exists for. They are separated from
test_phase2.py because that file proves the four assignment events work; this one
proves the smaller `SHALL`s that are easy to skip: speaker attribution, quantity
validity, ordering, atomic failure, retrieval in both link directions, and the
retry story for a message whose extraction failed.
"""

import copy

import pytest

from app import repositories as repo
from app.extraction import build_input, message_line, speaker_prefix
from app.schemas import ExtractionResult
from tests import fake_llm
from tests.test_phase2 import (
    ACRILICO,
    ALICE,
    CUSTOMER,
    LONA,
    MARIO,
    MESH,
    MICRO,
    PHASE1,
    message,
    phase1,
)


@pytest.fixture(autouse=True)
def llm(monkeypatch):
    fake_llm.patch(monkeypatch)


# ---------- message-processing: speaker identification ----------

def test_speaker_names_are_embedded_in_prompt_content():
    """Role is always "user"; the speaker lives in the content (Decision 7)."""
    inbound = {
        "message_id": "wamid.1",
        "direction": "inbound",
        "text": "Hola buenos dias",
        "customer_name": "Alice - ACME INDUSTRIAL",
    }
    outbound = {
        "message_id": "wamid.2",
        "direction": "outbound",
        "text": "Hola buen día",
        "customer_name": "Alice - ACME INDUSTRIAL",
    }

    assert speaker_prefix(inbound) == "Alice: Hola buenos dias"
    # the customer's name must never label the rep's message
    assert speaker_prefix(outbound) == "Vendedor: Hola buen día"
    assert speaker_prefix({**outbound, "sender_name": "Izzy"}) == "Izzy: Hola buen día"

    prompt = build_input("CTX", [inbound, {**outbound, "sender_name": "Izzy"}], inbound)
    assert all(part["role"] == "user" for part in prompt)
    lines = [part["content"] for part in prompt]
    # the newest message renders ONCE, in the last slot, with the buyer company;
    # reps never get the customer's company — that would misattribute the seat
    assert lines[-1] == "Alice (ACME INDUSTRIAL): Hola buenos dias"
    assert lines.count("Alice (ACME INDUSTRIAL): Hola buenos dias") == 1
    assert "Izzy: Hola buen día" in lines


def test_message_line_is_timed_and_render_stable():
    """[timestamp] speaker (company) — the cache invariant: a message renders
    from its stored dict alone, so its bytes never change between calls, and
    earlier turns' cache breakpoints stay aligned with later ones."""
    msg = {"message_id": "w1", "direction": "inbound", "customer_name": "Alice - ACME INDUSTRIAL",
           "text": "hola", "timestamp": "2026-02-10T09:43:00Z"}
    assert message_line(msg) == "[2026-02-10T09:43:00Z] Alice (ACME INDUSTRIAL): hola"
    assert message_line(msg) == message_line(copy.deepcopy(msg))

    out = {"message_id": "w2", "direction": "outbound", "sender_name": "Izzy", "text": "si",
           "timestamp": "2026-02-10T09:45:00Z", "customer_name": "Alice - ACME INDUSTRIAL"}
    assert message_line(out) == "[2026-02-10T09:45:00Z] Izzy: si"

    undated = {"message_id": "w3", "direction": "inbound", "text": "sin hora"}
    assert message_line(undated) == "Cliente: sin hora"


def test_outbound_via_messages_never_borrows_the_customers_name(client, monkeypatch):
    """The batch endpoint always sets sender_name, so only a raw POST /messages
    would have rendered a rep's line as if Alice wrote it.

    Outbound never reaches extraction, so the damage would show up later, as a
    misattributed line of history fed to the next inbound message.
    """
    phase1(client)

    client.post(
        "/messages",
        json={
            "message_id": "wamid.411.9001",
            "conversation_id": ALICE,
            "customer_id": CUSTOMER,
            "direction": "outbound",
            "timestamp": "2026-02-11T12:00:00Z",
            "text": "Con gusto, cualquier cosa me dicen",
            "customer_name": "Alice - ACME INDUSTRIAL",
        },
    )

    import app.pipeline as pipeline

    captured = {}
    original = pipeline.extract

    def spy(catalog, customer_context, history, new_message):
        captured["history"] = history
        return original(catalog, customer_context, history, new_message)

    monkeypatch.setattr(pipeline, "extract", spy)
    message(client, message_id="wamid.411.9001b", conversation_id=ALICE, text="seguimos luego")

    rep_line = [m for m in captured["history"] if m["text"] == "Con gusto, cualquier cosa me dicen"]
    assert len(rep_line) == 1
    assert speaker_prefix(rep_line[0]) == "Vendedor: Con gusto, cualquier cosa me dicen"


# ---------- quote-extraction: quantity validity ----------

@pytest.mark.parametrize("quantity", [0, -5, 10**9])
def test_invalid_quantities_are_reviewed_not_stored(client, monkeypatch, quantity):
    """zero / negative / absurd numbers cannot become line items, even from a
    confident model. The catalog gate alone would happily store them."""
    phase1(client)

    def broken(catalog, customer_context, history, new_message):
        return ExtractionResult(
            intent="revision",
            confidence=0.95,
            target_quote_id="Q-001",
            line_items=[{"sku": LONA, "quantity": quantity}],
            mentions_new_request=False,
            reason="modelo con cantidad imposible",
        )

    monkeypatch.setattr("app.pipeline.extract", broken)

    result = message(
        client,
        message_id=f"wamid.411.q{quantity}",
        # its own thread, so a review from a previous case cannot swallow this one
        conversation_id=f"wa-thread-q{abs(quantity)}",
        text="cambia las lonas",
    )

    assert result["requires_review"] is True
    assert "invalid_quantity" in result["issues"]
    assert {li["sku"]: li["qty"] for li in client.get("/quotes/Q-001").json()["line_items"]} == {
        LONA: 20, MESH: 5, MICRO: 5
    }


# ---------- conversation-linking: continuation resolves by conversation ----------

def test_continuation_resolves_to_the_conversations_own_open_quote(client, monkeypatch):
    """The company has two open quotes, but each thread works one of them, so a
    message with no product named is about the quote its own chat is on.

    One possible referent is a fact; two is a guess. That is why this rule only
    fires when the conversation touches exactly one open quote.
    """
    phase1(client)  # Q-001 in Alice's thread
    message(client, message_id="wamid.662.0001", conversation_id=MARIO,
            text="y aparte cotizame 2 acrilicos blancos de 3mm porfa")  # Q-002 in Mario's thread

    def targetless(catalog, customer_context, history, new_message):
        return ExtractionResult(
            intent="revision",
            confidence=0.9,
            target_quote_id=None,
            line_items=[{"sku": MESH, "quantity": 7}],
            mentions_new_request=False,
            reason="el cliente no nombra la cotizacion",
        )

    monkeypatch.setattr("app.pipeline.extract", targetless)

    in_alice = message(client, message_id="wamid.411.9100", conversation_id=ALICE,
                       text="anótame 7 mas de mesh")
    assert in_alice["status"] == "processed"
    assert in_alice["quote_id"] == "Q-001", "Alice's thread is working Q-001"
    assert not in_alice.get("requires_review")

    in_mario = message(client, message_id="wamid.662.9100", conversation_id=MARIO,
                       text="anótame 7 mas de mesh")
    assert in_mario["quote_id"] == "Q-002", "same text, other thread, other quote"

    assert {li["sku"]: li["qty"] for li in client.get("/quotes/Q-001").json()["line_items"]} == {
        LONA: 20, MESH: 7, MICRO: 5
    }


def test_two_open_quotes_in_one_conversation_stay_ambiguous(client, monkeypatch):
    """When the arriving chat itself touches both open quotes, there is no
    deterministic answer left, so it goes to a human."""
    phase1(client)
    message(client, message_id="wamid.411.0240", conversation_id=ALICE,
            text="y aparte cotizame 2 acrilicos blancos de 3mm porfa")
    # Alice's thread now links Q-001 and Q-002

    def targetless(catalog, customer_context, history, new_message):
        return ExtractionResult(
            intent="revision",
            confidence=0.9,
            target_quote_id=None,
            line_items=[{"sku": MESH, "quantity": 7}],
            mentions_new_request=False,
            reason="no se cual de las dos",
        )

    monkeypatch.setattr("app.pipeline.extract", targetless)
    before = client.get(f"/quotes?customer={CUSTOMER}").json()

    result = message(client, message_id="wamid.411.9101", conversation_id=ALICE,
                     text="anótame 7 mas de mesh")

    assert result["requires_review"] is True
    assert "ambiguous_quote_reference" in result["issues"]
    assert set(result["candidates"]) == {"Q-001", "Q-002"}
    assert client.get(f"/quotes?customer={CUSTOMER}").json() == before


# ---------- conversation-linking: retrieval in both directions ----------

def test_quotes_can_be_retrieved_by_conversation(client):
    """GET /quotes?conversation= is the reverse navigation the spec asks for:
    a rep in one chat can see what that thread is about."""
    phase1(client)
    message(client, message_id="wamid.662.0001", conversation_id=MARIO,
            text="Buen dia, soy Mario de compras de ACME. Alice me pasó con ustedes, "
            "me confirmas qué lleva la cotización de las lonas?")

    by_conversation = client.get(f"/quotes?conversation={MARIO}")
    assert by_conversation.status_code == 200
    assert [q["quote_id"] for q in by_conversation.json()["quotes"]] == ["Q-001"]

    # narrowing both ways
    assert client.get(
        f"/quotes?conversation={MARIO}&customer={CUSTOMER}"
    ).json() == by_conversation.json()
    assert client.get(
        f"/quotes?conversation={MARIO}&customer=C-99999"
    ).json() == {"quotes": []}
    assert client.get("/quotes?conversation=wa-thread-does-not-exist").json() == {"quotes": []}


def test_list_quotes_requires_a_filter(client):
    """No implicit "everything": an unscoped list is a contract error, 400 like
    every other bad input."""
    phase1(client)
    assert client.get("/quotes").status_code == 400


# ---------- message-processing: failure must not half-apply ----------

def test_pipeline_failure_returns_500_and_leaves_no_trace(client, tx, monkeypatch):
    """Spec: 500 with error details and NOT update any state.

    The append, the conversation row and the audit write all happen before and
    after the failure point, so a rollback test is the only thing that proves the
    transaction boundary is real.
    """
    phase1(client)
    before_quotes = client.get(f"/quotes?customer={CUSTOMER}").json()
    before_messages = tx.factory()
    try:
        stored = len(repo.get_conversation(before_messages, ALICE).messages)
    finally:
        before_messages.close()

    def explode(*args, **kwargs):
        raise RuntimeError("database went away")

    monkeypatch.setattr(repo, "mark_message_processed", explode)

    response = client.post(
        "/messages",
        json={
            "message_id": "wamid.411.9002",
            "conversation_id": ALICE,
            "customer_id": CUSTOMER,
            "direction": "inbound",
            "timestamp": "2026-02-11T13:00:00Z",
            "text": "Oye de las lonas mejor que sean 18, no 20",
        },
    )

    assert response.status_code == 500
    assert "database went away" in response.json()["detail"]

    assert client.get(f"/quotes?customer={CUSTOMER}").json() == before_quotes

    fresh = tx.factory()
    try:
        conv = repo.get_conversation(fresh, ALICE)
        assert len(conv.messages) == stored, "the failed message must not be in history"
        assert fresh.query(repo.ProcessedMessage).count() == 4
    finally:
        fresh.close()


# ---------- ambiguity-resolution: resolve as new quote, 404, restart ----------

def test_resolve_by_creating_a_new_quote(client):
    """Spec scenario: the human decides the message was a request of its own."""
    phase1(client)
    message(client, message_id="wamid.411.0600", conversation_id=ALICE, text="mejor cancelalo")
    review_id = client.get("/pending-reviews").json()["pending_reviews"][0]["review_id"]

    resolved = client.post(
        f"/pending-reviews/{review_id}/resolve",
        json={"action": "new_quote", "line_items": [{"sku": ACRILICO, "quantity": 2}]},
    )
    assert resolved.status_code == 200, resolved.text
    body = resolved.json()
    assert body["quote_id"] == "Q-002"

    created = client.get("/quotes/Q-002").json()
    assert created["conversations"] == [ALICE]
    assert {li["sku"]: li["qty"] for li in created["line_items"]} == {ACRILICO: 2}
    assert {li["sku"]: li["qty"] for li in client.get("/quotes/Q-001").json()["line_items"]} == {
        LONA: 20, MESH: 5, MICRO: 5
    }

    review = client.get(f"/pending-reviews/{review_id}").json()
    assert review["status"] == "resolved"
    assert review["resolution"]["target_quote_id"] == "Q-002"

    # the new quote is no longer waiting on anyone
    assert client.get("/pending-reviews").json()["pending_reviews"] == []


def test_unknown_pending_review_returns_404(client):
    phase1(client)
    assert client.get("/pending-reviews/REV-wamid.does-not-exist").status_code == 404


def test_pending_review_survives_a_restart(client, tx):
    """A review parked for a human is state, so it has to outlive the process."""
    phase1(client)
    message(client, message_id="wamid.411.0600", conversation_id=ALICE, text="mejor cancelalo")

    fresh = tx.factory()
    try:
        review = repo.get_pending_review(fresh, "REV-wamid.411.0600")
        assert review is not None
        assert review.status == "pending"
        assert review.original_message["text"] == "mejor cancelalo"
        assert review.candidate_quotes == ["Q-001"]
        assert review.resolution_issues  # the human sees why it was parked
    finally:
        fresh.close()


# ---------- message-processing: retryable extraction failure ----------

def test_redelivery_after_failed_extraction_retries_without_duplicates(client, tx, monkeypatch):
    """A message the model could not be reached for stays retryable on purpose.

    Redelivery must therefore not double the transcript, not stack a second
    review, and not become a follow-up of its own review — and once the model is
    back, the retry applies exactly once. The review left behind by the outage
    stays pending for a human to discard; it is not silently closed, because the
    system deciding its own failure was a false alarm is a guess.
    """
    phase1(client)

    def offline(catalog, customer_context, history, new_message):
        raise RuntimeError("OPENAI_API_KEY is not set")

    monkeypatch.setattr("app.pipeline.extract", offline)

    payload = {
        "message_id": "wamid.411.9003",
        "conversation_id": ALICE,
        "customer_id": CUSTOMER,
        "direction": "inbound",
        "timestamp": "2026-02-11T14:00:00Z",
        "text": "Oye de las lonas mejor que sean 15, no 20",
    }

    first = client.post("/messages", json=payload)
    assert first.status_code == 200
    assert first.json()["requires_review"] is True
    assert first.json()["review_id"] == "REV-wamid.411.9003"
    assert any(i.startswith("extraction_error") for i in first.json()["issues"])

    second = client.post("/messages", json=payload)
    assert second.status_code == 200
    assert second.json()["already_reviewed"] is True

    check = tx.factory()
    try:
        conv = repo.get_conversation(check, ALICE)
        texts = [m["text"] for m in conv.messages]
        assert texts.count(payload["text"]) == 1, "redelivery doubled the transcript"
        reviews = repo.list_pending_reviews(check)
        assert [r.review_id for r in reviews] == ["REV-wamid.411.9003"]
        assert reviews[0].follow_up_messages == [], "message must not follow up on itself"
        assert check.query(repo.ProcessedMessage).count() == 4, "still retryable"
    finally:
        check.close()

    # model is reachable again
    monkeypatch.setattr("app.pipeline.extract", fake_llm.extract)

    third = client.post("/messages", json=payload)
    assert third.json()["status"] == "processed"
    assert third.json()["intent"] == "revision"
    assert {li["sku"]: li["qty"] for li in client.get("/quotes/Q-001").json()["line_items"]}[LONA] == 15

    check = tx.factory()
    try:
        assert len(repo.get_conversation(check, ALICE).messages) == 5
        assert check.query(repo.ProcessedMessage).count() == 5
    finally:
        check.close()

    # a fourth delivery is now a true duplicate
    fourth = client.post("/messages", json=payload)
    assert fourth.json()["status"] == "duplicate"


# ---------- state application: the database is the backstop ----------

def test_dangling_quote_id_is_rejected_by_the_schema(client, tx, monkeypatch):
    """What enforces "never write a line item onto a quote that isn't there".

    `apply_validated` deliberately does not re-check that the target quote
    exists: the validator confirmed it, in this same transaction, and the foreign
    keys on `line_items` and `quote_conversations` are the real guarantee. This
    test bypasses both code checks to reach the constraint on purpose, and asserts
    the failure is loud *and* atomic: 500, nothing half-applied, and the message
    left unprocessed so a redelivery can still do the work.
    """
    phase1(client)

    from app.models import LineItem, Quote, QuoteConversation

    wipe = tx.factory()
    try:
        wipe.query(LineItem).delete()
        wipe.query(QuoteConversation).delete()
        wipe.query(Quote).delete()
        wipe.commit()
    finally:
        wipe.close()

    def forged(catalog, customer_context, history, new_message):
        return ExtractionResult(
            intent="revision",
            confidence=0.95,
            target_quote_id="Q-001",  # no longer exists
            line_items=[{"sku": LONA, "quantity": 15}],
            mentions_new_request=False,
            reason="simulating a quote deleted mid-flight",
        )

    def rubber_stamp(db, customer_id, result, conversation_id=None):
        return {
            "ok": True,
            "issues": [],
            "candidates": [],
            "clean_lines": result.line_items,
            "target_quote_id": "Q-001",
        }

    monkeypatch.setattr("app.pipeline.extract", forged)
    monkeypatch.setattr("app.pipeline.validate", rubber_stamp)

    response = message_raw(client, message_id="wamid.411.9200", conversation_id=ALICE,
                           text="de las lonas mejor que sean 15")
    assert response.status_code == 500
    detail = response.json()["detail"].lower()
    assert "pipeline error" in detail
    assert "foreign key" in detail, f"expected the schema constraint, got: {detail}"

    check = tx.factory()
    try:
        assert check.query(LineItem).count() == 0, "no orphan line item"
        assert check.query(repo.ProcessedMessage).count() == 4, "message stays retryable"
        assert len(repo.get_conversation(check, ALICE).messages) == 4, "no half-stored message"
    finally:
        check.close()

    # a redelivery is still accepted as new work, not swallowed as a duplicate
    monkeypatch.undo()
    monkeypatch.setattr("app.pipeline.extract", fake_llm.extract)
    retry = message(client, message_id="wamid.411.9200", conversation_id=ALICE,
                    text="de las lonas mejor que sean 15")
    assert retry["status"] in ("processed", "appended_to_review")


def message_raw(client, *, message_id, conversation_id, text):
    """Same payload as tests.test_phase2.message() but without the 200 assertion,
    so the caller can inspect a failure response."""
    return client.post(
        "/messages",
        json={
            "message_id": message_id,
            "conversation_id": conversation_id,
            "customer_id": CUSTOMER,
            "direction": "inbound",
            "timestamp": "2026-02-11T15:00:00Z",
            "text": text,
        },
    )


# ---------- message-processing: batch ordering ----------

def test_batch_is_processed_in_timestamp_order(client, tx):
    """A shuffled conversation_history array cannot change the outcome.

    The micro line only exists if the rep's question precedes "Necesitamos 5
    rollos", so chronological processing is observable in the result, not just
    in the order of the response array.
    """
    shuffled = copy.deepcopy(PHASE1)
    shuffled["conversation_id"] = "wa-thread-777"
    entries = shuffled["conversation_history"]
    for index, entry in enumerate(entries):
        entry["message_id"] = f"wamid.777.{index:04d}"
    expected = [e["message_id"] for e in entries]
    entries.reverse()  # newest first in the payload

    response = client.post("/conversations", json=shuffled)
    assert response.status_code == 200, response.text

    processed = [r["message_id"] for r in response.json()["results"]]
    assert processed == expected, "batch must be processed oldest-first, not array order"

    fresh = tx.factory()
    try:
        stored = [m["message_id"] for m in repo.get_conversation(fresh, "wa-thread-777").messages]
        assert stored == expected
    finally:
        fresh.close()

    quote = client.get(f"/quotes/Q-001").json()
    assert {li["sku"]: li["qty"] for li in quote["line_items"]} == {LONA: 20, MESH: 5, MICRO: 5}
    assert quote["conversations"] == ["wa-thread-777"]


# ---------- message-processing: batch response ----------

def test_batch_returns_the_resulting_quote_state(client):
    """The batch scenario says "process each message and return the resulting
    quote state" — the assignment's Phase 1 expected output must be readable
    straight from the POST /conversations response, not only from a later GET.
    """
    body = phase1(client)

    quote = next(q for q in body["quotes"] if q["quote_id"] == "Q-001")
    assert quote["status"] == "draft"
    assert quote["conversations"] == [ALICE]
    assert {li["sku"]: li["qty"] for li in quote["line_items"]} == {LONA: 20, MESH: 5, MICRO: 5}


def test_batch_honors_per_message_sender_names(client, tx):
    """HistoryMessage carries sender_name; the batch wrapper may not overwrite
    it with the conversation-level names. Attribution feeds quote resolution."""
    payload = copy.deepcopy(PHASE1)
    payload["conversation_id"] = "wa-thread-888"
    payload["conversation_history"][1]["sender_name"] = "Izzy - Distribuidora"

    response = client.post("/conversations", json=payload)
    assert response.status_code == 200, response.text

    check = tx.factory()
    try:
        stored = repo.get_conversation(check, "wa-thread-888").messages
        outbound = [m for m in stored if m["direction"] == "outbound"]
        assert outbound[0]["sender_name"] == "Izzy - Distribuidora"
    finally:
        check.close()


# ---------- conversation-linking: context window ----------

def test_context_is_the_last_twenty_messages_of_each_linked_chat(client, db, monkeypatch):
    """The resolution context is bounded: recent history per linked thread,
    never an unbounded transcript re-sent with every message."""
    phase1(client)  # 4 messages in Alice's chat, linked to Q-001

    conv = repo.get_conversation(db, ALICE)
    conv.messages = list(conv.messages) + [
        {
            "message_id": f"wamid.411.fill{n:03d}",
            "conversation_id": ALICE,
            "direction": "inbound",
            "text": f"relleno {n}",
        }
        for n in range(1, 26)
    ]
    db.flush()

    import app.pipeline as pipeline

    captured = {}
    original = pipeline.extract

    def spy(catalog, customer_context, history, new_message):
        captured["history"] = history
        return original(catalog, customer_context, history, new_message)

    monkeypatch.setattr(pipeline, "extract", spy)
    message(client, message_id="wamid.411.9300", conversation_id=ALICE, text="seguimos luego")

    history = captured["history"]
    # 29 stored + the arriving message, capped at the last 20, oldest first
    assert len(history) == 20
    assert history[0]["text"] == "relleno 7"
    assert history[-1]["message_id"] == "wamid.411.9300"
    assert not any("me ayudas a cotizar" in m["text"] for m in history), "oldest messages must drop out"


def test_unlinked_current_conversation_still_enters_context(client, monkeypatch):
    """A brand-new thread is not yet linked to any quote, but its own history
    is exactly the context the model needs to resolve it against the company."""
    phase1(client)  # Q-001 exists, linked only to Alice's chat

    import app.pipeline as pipeline

    captured = {}
    original = pipeline.extract

    def spy(catalog, customer_context, history, new_message):
        captured["history"] = history
        return original(catalog, customer_context, history, new_message)

    monkeypatch.setattr(pipeline, "extract", spy)
    result = message(client, message_id="wamid.662.0001", conversation_id=MARIO,
                     text="Buen dia, soy Mario de compras de ACME. Alice me pasó con ustedes, "
                     "me confirmas qué lleva la cotización de las lonas?")

    texts = [m["text"] for m in captured["history"]]
    assert result["intent"] == "reference"
    assert "Buen dia, soy Mario de compras de ACME. Alice me pasó con ustedes, " \
           "me confirmas qué lleva la cotización de las lonas?" in texts, "Mario's own message"
    assert any("me ayudas a cotizar" in t for t in texts), "Alice's linked chat as well"


def test_llm_request_shape_matches_responses_api_docs(monkeypatch):
    """Pinned to developers.openai.com's create-response reference:
    store=False (the default retains the transcript server-side for 30+ days
    and we never fetch responses back), NO prompt_cache_options override
    (implicit mode — the default — is what caches the growing thread: its
    breakpoint at the newest message makes each turn a small delta write that
    the next turn reads back at 0.1x), and the static instructions ride as
    the leading developer message carrying the one explicit breakpoint every
    request shares."""
    import json

    from app import extraction

    captured = {}

    class _Details:
        cached_tokens = 0
        cache_write_tokens = 0

    class _Usage:
        input_tokens = 1362
        output_tokens = 156
        input_tokens_details = _Details()

    class _Resp:
        status = "completed"
        usage = _Usage()
        output_text = json.dumps({
            "intent": "reference", "confidence": 0.9, "target_quote_id": "Q-001",
            "line_items": [], "mentions_new_request": False, "reason": None,
        })

    class _Responses:
        def create(self, **kwargs):
            captured.update(kwargs)
            return _Resp()

    class _Client:
        responses = _Responses()

    monkeypatch.setattr(extraction, "get_client", lambda: _Client())
    msg = {"message_id": "m1", "direction": "inbound", "text": "hola", "timestamp": "t"}
    result = extraction.extract({"AAA-1": "lona"}, "CTX", [msg], msg)

    assert result.intent == "reference"
    assert captured["store"] is False
    assert "prompt_cache_options" not in captured, "must stay on the implicit default"
    assert captured["reasoning"] == {"effort": "low"}
    assert "other" in captured["text"]["format"]["schema"]["properties"]["intent"]["enum"]
    leading = captured["input"][0]
    assert leading["role"] == "developer"
    assert leading["content"][0]["prompt_cache_breakpoint"] == {"mode": "explicit"}


def test_incomplete_llm_response_parks_instead_of_parsing(monkeypatch):
    """status "incomplete" means output_text may be half a JSON object (docs:
    max_output_tokens cap). extract must raise — the pipeline then parks the
    message for review instead of trusting a truncated extraction."""
    from app import extraction

    class _Resp:
        status = "incomplete"
        usage = None
        output_text = '{"intent": '

    class _Responses:
        def create(self, **kwargs):
            return _Resp()

    class _Client:
        responses = _Responses()

    monkeypatch.setattr(extraction, "get_client", lambda: _Client())
    with pytest.raises(RuntimeError, match="incomplete"):
        extraction.extract({}, "CTX", [], {"message_id": "m", "direction": "inbound", "text": "x"})


# ---------- quote-extraction: non-quote content passes through inertly ----------

def test_other_intent_passes_inertly(client, monkeypatch):
    """A greeting or a pure availability question must not park. Found by the
    real-model probe beyond the guide: parking such a message swallowed every
    later inbound of the thread into that one review (appended_to_review),
    invisible until a human acted. intent other = no state, no link, no review.
    """
    phase1(client)
    before = client.get(f"/quotes?customer={CUSTOMER}").json()

    def other(catalog, customer_context, history, new_message):
        return ExtractionResult(
            intent="other", confidence=0.95, line_items=[],
            mentions_new_request=False, reason="sin accion de cotizacion",
        )

    monkeypatch.setattr("app.pipeline.extract", other)
    result = message(client, message_id="wamid.411.9201", conversation_id=ALICE,
                     text="una consulta, si cuentan con manguera de 1 pulgada?")

    assert result["status"] == "processed"
    assert result["intent"] == "other"
    assert not result.get("requires_review")
    assert result["quote_id"] is None
    assert result["changes"] == []
    assert client.get(f"/quotes?customer={CUSTOMER}").json() == before
    assert client.get("/pending-reviews").json()["pending_reviews"] == []


def test_other_with_attached_lines_parks(client, monkeypatch):
    """A self-contradictory 'other' (claims inert but attaches a line item)
    goes to a human — never silently applied, never silently dropped."""
    phase1(client)
    before = client.get(f"/quotes?customer={CUSTOMER}").json()

    def contradiction(catalog, customer_context, history, new_message):
        return ExtractionResult(
            intent="other", confidence=0.9,
            line_items=[{"sku": MESH, "quantity": 7}],
            mentions_new_request=False,
        )

    monkeypatch.setattr("app.pipeline.extract", contradiction)
    result = message(client, message_id="wamid.411.9202", conversation_id=ALICE,
                     text="manguera? ah y 7 mas de mesh")

    assert result["requires_review"] is True
    assert "inconsistent_extraction" in result["issues"]
    assert client.get(f"/quotes?customer={CUSTOMER}").json() == before
    reviews = client.get("/pending-reviews").json()["pending_reviews"]
    assert len(reviews) == 1
