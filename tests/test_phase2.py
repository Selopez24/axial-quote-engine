"""The four Phase 2 events, end to end, against a deterministic extractor.

This is where the data model gets proven: a thread that already feeds one quote
must be able to feed a second one, and a revision arriving on the other thread
must still land on the right quote.

The extractor is stubbed (tests/fake_llm.py) on purpose. The part under test is
what happens around the model: validation, linking, revision application,
dedup. tests/test_integration_llm.py runs the same scenario through the real
model.
"""

import pytest

from app import repositories as repo
from app.catalog import CATALOG
from tests import fake_llm

CUSTOMER = "C-10029"
ALICE = "wa-thread-411"
MARIO = "wa-thread-662"

PHASE1 = {
    "customer_id": CUSTOMER,
    "conversation_id": ALICE,
    "customer_name": "Alice - ACME INDUSTRIAL",
    "rep_name": "Izzy",
    "conversation_history": [
        {
            "direction": "inbound",
            "timestamp": "2026-02-10T09:43:00Z",
            "text": "Hola buenos dias, me ayudas a cotizar 20 rollos lona 3.20 x 100 m., "
            "5 rollos de mesh de 3.20 x 100 m. y si cuentas con microperforado de 1.37 y 1.50 m.",
        },
        {
            "direction": "outbound",
            "timestamp": "2026-02-10T09:45:00Z",
            "text": "Hola buen día. Si. Cuantas piezas de microperforado",
        },
        {
            "direction": "outbound",
            "timestamp": "2026-02-10T09:45:30Z",
            "text": "De momento sería de 1.37x50",
        },
        {
            "direction": "inbound",
            "timestamp": "2026-02-10T09:46:00Z",
            "text": "Necesitamos 5 rollos",
        },
    ],
}

LONA = "ALFBTC-013-320100"
MESH = "ALMCLC-0909-320100"
MICRO = "AVMICS-1A-137050"
ACRILICO = "ARACBL-03-122244"


@pytest.fixture(autouse=True)
def llm(monkeypatch):
    fake_llm.patch(monkeypatch)


def message(client, *, message_id, conversation_id, text, direction="inbound", customer_name=None):
    payload = {
        "message_id": message_id,
        "conversation_id": conversation_id,
        "customer_id": CUSTOMER,
        "direction": direction,
        "timestamp": "2026-02-11T10:14:00Z",
        "text": text,
    }
    if customer_name:
        payload["customer_name"] = customer_name
    response = client.post("/messages", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def phase1(client):
    response = client.post("/conversations", json=PHASE1)
    assert response.status_code == 200, response.text
    return response.json()


def test_phase1_builds_q001_from_both_directions(client):
    """The micro line only exists if you read the rep's answer + the qty reply."""
    phase1(client)

    body = client.get(f"/quotes?customer={CUSTOMER}").json()
    assert len(body["quotes"]) == 1

    quote = body["quotes"][0]
    assert quote["quote_id"] == "Q-001"
    assert quote["status"] == "draft"
    assert quote["conversations"] == [ALICE]
    assert {li["sku"]: li["qty"] for li in quote["line_items"]} == {LONA: 20, MESH: 5, MICRO: 5}


def test_event1_revision_in_original_chat(client):
    phase1(client)

    result = message(
        client,
        message_id="wamid.411.0231",
        conversation_id=ALICE,
        text="Oye de las lonas mejor que sean 15, no 20",
    )

    assert result["status"] == "processed"
    assert result["intent"] == "revision"
    assert result["quote_id"] == "Q-001"

    quote = client.get("/quotes/Q-001").json()
    quantities = {li["sku"]: li["qty"] for li in quote["line_items"]}
    assert quantities[LONA] == 15
    # nothing else changed
    assert quantities[MESH] == 5 and quantities[MICRO] == 5


def test_event2_second_chat_links_to_existing_quote_without_changing_lines(client):
    phase1(client)

    message(client, message_id="wamid.411.0231", conversation_id=ALICE,
            text="Oye de las lonas mejor que sean 15, no 20")

    result = message(
        client,
        message_id="wamid.662.0001",
        conversation_id=MARIO,
        text="Buen dia, soy Mario de compras de ACME. Alice me pasó con ustedes, "
        "me confirmas qué lleva la cotización de las lonas?",
    )

    assert result["intent"] == "reference"
    assert result["quote_id"] == "Q-001"

    quote = client.get("/quotes/Q-001").json()
    # no line changes
    assert {li["sku"]: li["qty"] for li in quote["line_items"]} == {LONA: 15, MESH: 5, MICRO: 5}
    # but the new chat is now attached to the same quote
    assert sorted(quote["conversations"]) == sorted([ALICE, MARIO])


def test_event3_one_chat_feeds_two_quotes(client):
    phase1(client)
    message(client, message_id="wamid.411.0231", conversation_id=ALICE,
            text="Oye de las lonas mejor que sean 15, no 20")
    message(client, message_id="wamid.662.0001", conversation_id=MARIO,
            text="Buen dia, soy Mario de compras de ACME. Alice me pasó con ustedes, "
            "me confirmas qué lleva la cotización de las lonas?")

    result = message(
        client,
        message_id="wamid.411.0240",
        conversation_id=ALICE,
        text="y aparte cotizame 2 acrilicos blancos de 3mm porfa",
    )

    assert result["intent"] == "new_quote"
    assert result["quote_id"] == "Q-002"

    q1 = client.get("/quotes/Q-001").json()
    q2 = client.get("/quotes/Q-002").json()
    assert {li["sku"]: li["qty"] for li in q2["line_items"]} == {ACRILICO: 2}
    # Q-002 is untouched by the earlier lona revision
    assert {li["sku"]: li["qty"] for li in q1["line_items"]} == {LONA: 15, MESH: 5, MICRO: 5}
    # and Alice's single thread now feeds two quotes
    assert ALICE in q1["conversations"] and ALICE in q2["conversations"]


def test_event4_revision_arrives_from_the_other_conversation(client):
    """The decisive case: content picks the quote, the chat does not."""
    phase1(client)
    message(client, message_id="wamid.411.0231", conversation_id=ALICE,
            text="Oye de las lonas mejor que sean 15, no 20")
    message(client, message_id="wamid.662.0001", conversation_id=MARIO,
            text="Buen dia, soy Mario de compras de ACME. Alice me pasó con ustedes, "
            "me confirmas qué lleva la cotización de las lonas?")
    message(client, message_id="wamid.411.0240", conversation_id=ALICE,
            text="y aparte cotizame 2 acrilicos blancos de 3mm porfa")

    result = message(
        client,
        message_id="wamid.662.0002",
        conversation_id=MARIO,
        text="perfecto. y de los acrilicos que cotizó Alice, mejor que sean 4",
    )

    assert result["intent"] == "revision"
    assert result["quote_id"] == "Q-002"

    final = client.get(f"/quotes?customer={CUSTOMER}").json()
    quotes = {q["quote_id"]: q for q in final["quotes"]}

    assert {li["sku"]: li["qty"] for li in quotes["Q-001"]["line_items"]} == {LONA: 15, MESH: 5, MICRO: 5}
    assert {li["sku"]: li["qty"] for li in quotes["Q-002"]["line_items"]} == {ACRILICO: 4}
    assert sorted(quotes["Q-001"]["conversations"]) == sorted([ALICE, MARIO])
    assert sorted(quotes["Q-002"]["conversations"]) == sorted([ALICE, MARIO])


def test_line_item_provenance_records_the_causing_message(client):
    """Assignment: "see what changed and which message caused it"."""
    phase1(client)
    message(client, message_id="wamid.411.0231", conversation_id=ALICE,
            text="Oye de las lonas mejor que sean 15, no 20")

    quote = client.get("/quotes/Q-001?history=true").json()
    lona = next(h for h in quote["line_item_history"] if h["sku"] == LONA)

    assert lona["created_by_message_id"] == "wamid.wa-thread-411.0001"
    assert lona["last_changed_by_message_id"] == "wamid.411.0231"

    change = next(c for c in quote["change_log"] if c["message_id"] == "wamid.411.0231")
    assert {"sku": LONA, "action": "update", "from": 20, "to": 15} in change["changes"]


def test_outbound_message_is_stored_but_never_extracts(client):
    phase1(client)

    result = message(
        client,
        message_id="wamid.411.0300",
        conversation_id=ALICE,
        direction="outbound",
        text="Ya quedó la corrección de las lonas a 15",
    )

    assert result["status"] == "stored"
    quote = client.get("/quotes/Q-001").json()
    assert {li["sku"]: li["qty"] for li in quote["line_items"]} == {LONA: 20, MESH: 5, MICRO: 5}


def test_duplicate_delivery_of_event4_changes_nothing(client):
    """Extra credit: same message_id twice must be a no-op on state."""
    phase1(client)
    message(client, message_id="wamid.411.0231", conversation_id=ALICE,
            text="Oye de las lonas mejor que sean 15, no 20")
    message(client, message_id="wamid.662.0001", conversation_id=MARIO,
            text="Buen dia, soy Mario de compras de ACME. Alice me pasó con ustedes, "
            "me confirmas qué lleva la cotización de las lonas?")
    message(client, message_id="wamid.411.0240", conversation_id=ALICE,
            text="y aparte cotizame 2 acrilicos blancos de 3mm porfa")

    event4 = {
        "message_id": "wamid.662.0002",
        "conversation_id": MARIO,
        "customer_id": CUSTOMER,
        "direction": "inbound",
        "timestamp": "2026-02-11T11:00:00Z",
        "text": "perfecto. y de los acrilicos que cotizó Alice, mejor que sean 4",
    }
    first = client.post("/messages", json=event4)
    assert first.status_code == 200
    before = client.get(f"/quotes?customer={CUSTOMER}").json()

    again = client.post("/messages", json=event4)
    assert again.status_code == 200
    assert again.json()["status"] == "duplicate"

    assert again.json() == {"status": "duplicate", "message_id": "wamid.662.0002",
                            "detail": "message_id already processed; state unchanged"}
    assert client.get(f"/quotes?customer={CUSTOMER}").json() == before


def test_same_text_twice_is_not_a_duplicate(client):
    """Dedupe by message_id only: a genuine repeat request still counts."""
    phase1(client)

    first = message(client, message_id="wamid.411.0500", conversation_id=ALICE,
                    text="Oye de las lonas mejor que sean 15, no 20")
    second = message(client, message_id="wamid.411.0501", conversation_id=ALICE,
                     text="Oye de las lonas mejor que sean 15, no 20")

    assert first["status"] == "processed"
    assert second["status"] == "processed"


def test_cancelalo_with_two_open_quotes_goes_to_human_review(client):
    """Never guess when two quotes are plausible."""
    phase1(client)
    message(client, message_id="wamid.411.0240", conversation_id=ALICE,
            text="y aparte cotizame 2 acrilicos blancos de 3mm porfa")

    result = message(
        client,
        message_id="wamid.411.0600",
        conversation_id=ALICE,
        text="mejor cancelalo",
    )

    assert result["status"] == "processed"
    assert result["requires_review"] is True
    assert set(result["issues"]) >= {"unclear_intent", "low_confidence"}
    assert set(result["candidates"]) == {"Q-001", "Q-002"}

    # the guess was not pushed anywhere: quote state is exactly as it was
    final = client.get(f"/quotes?customer={CUSTOMER}").json()
    quotes = {q["quote_id"]: q for q in final["quotes"]}
    assert {li["sku"]: li["qty"] for li in quotes["Q-001"]["line_items"]} == {LONA: 20, MESH: 5, MICRO: 5}
    assert {li["sku"]: li["qty"] for li in quotes["Q-002"]["line_items"]} == {ACRILICO: 2}

    reviews = client.get("/pending-reviews").json()["pending_reviews"]
    assert len(reviews) == 1
    review = reviews[0]
    assert review["original_message"]["text"] == "mejor cancelalo"
    assert review["candidate_quotes"] == result["candidates"]
    assert review["status"] == "pending"


def test_cancel_entire_quote_with_clear_target(client):
    """Cancel a full quote when the target is unambiguous."""
    phase1(client)
    message(client, message_id="wamid.411.0240", conversation_id=ALICE,
            text="y aparte cotizame 2 acrilicos blancos de 3mm porfa")

    # Verify Q-002 was created
    after_phase2 = client.get(f"/quotes?customer={CUSTOMER}").json()
    quote_ids = [q["quote_id"] for q in after_phase2["quotes"]]
    assert "Q-002" in quote_ids, f"Q-002 not found in {quote_ids}"

    result = message(
        client,
        message_id="wamid.411.0700",
        conversation_id=ALICE,
        text="cancela la Q-002",
    )

    assert result["status"] == "processed"
    assert result["intent"] == "cancel"
    assert result["quote_id"] == "Q-002"

    final = client.get(f"/quotes?customer={CUSTOMER}").json()
    quotes = {q["quote_id"]: q for q in final["quotes"]}
    assert quotes["Q-001"]["status"] == "draft"
    assert quotes["Q-002"]["status"] == "cancelled"
    assert quotes["Q-002"]["line_items"] == []


def test_cancel_specific_products_from_quote(client):
    """Cancel only specific SKUs, leaving the rest of the quote intact."""
    phase1(client)

    result = message(
        client,
        message_id="wamid.411.0700",
        conversation_id=ALICE,
        text="quita las lonas de la cotizacion",
    )

    assert result["status"] == "processed"
    assert result["intent"] == "cancel"
    assert result["quote_id"] == "Q-001"

    final = client.get(f"/quotes?customer={CUSTOMER}").json()
    quote = [q for q in final["quotes"] if q["quote_id"] == "Q-001"][0]
    remaining_skus = [li["sku"] for li in quote["line_items"]]
    assert LONA not in remaining_skus
    assert MESH in remaining_skus
    assert MICRO in remaining_skus


def test_follow_up_enriches_the_open_review_instead_of_bypassing_it(client):
    phase1(client)
    message(client, message_id="wamid.411.0240", conversation_id=ALICE,
            text="y aparte cotizame 2 acrilicos blancos de 3mm porfa")

    message(client, message_id="wamid.411.0600", conversation_id=ALICE, text="mejor cancelalo")
    review_id = client.get("/pending-reviews").json()["pending_reviews"][0]["review_id"]

    follow = message(client, message_id="wamid.411.0601", conversation_id=ALICE,
                     text="el acrílico, no la lona")

    assert follow["status"] == "appended_to_review"
    assert follow["review_id"] == review_id

    review = client.get(f"/pending-reviews/{review_id}").json()
    assert [m["text"] for m in review["follow_up_messages"]] == ["el acrílico, no la lona"]
    # still untouched
    assert {li["sku"]: li["qty"] for li in client.get("/quotes/Q-002").json()["line_items"]} == {ACRILICO: 2}


def test_inbound_follow_up_can_self_resolve_an_open_review(client, monkeypatch):
    """The gate blocks guessing, not progress. A parked message whose ambiguity
    the follow-up resolves is re-extracted with the widened window; the review
    self-resolves and the change keeps the ORIGINAL message's provenance."""
    from app.schemas import ExtractionResult

    phase1(client)
    message(client, message_id="wamid.411.0240", conversation_id=ALICE,
            text="y aparte cotizame 2 acrilicos blancos de 3mm porfa")

    def staged(catalog, customer_context, history, new_message):
        mid = new_message.get("message_id")
        if mid == "wamid.411.0250":
            return ExtractionResult(
                intent="unclear", confidence=0.4, target_quote_id=None, line_items=[],
                mentions_new_request=False, reason="no se cual cotizacion es",
            )
        if mid == "wamid.411.0252" and any(m.get("message_id") == "wamid.411.0250" for m in history):
            return ExtractionResult(
                intent="revision", confidence=0.9, target_quote_id="Q-001",
                line_items=[{"sku": LONA, "quantity": 15}],
                mentions_new_request=False, reason="la aclaracion combina con la solicitud en revision",
            )
        return fake_llm.extract(catalog, customer_context, history, new_message)

    monkeypatch.setattr("app.pipeline.extract", staged)

    parked = message(client, message_id="wamid.411.0250", conversation_id=ALICE,
                     text="cambialo a como te digo luego")
    assert parked["requires_review"] is True
    review_id = parked["review_id"]

    # outbound enriches the review but never triggers a retry: the customer's
    # voice is what decides
    out = message(client, message_id="wamid.411.0251", conversation_id=ALICE,
                  text="reviso con el almacen", direction="outbound")
    assert out["status"] == "appended_to_review"
    assert client.get(f"/pending-reviews/{review_id}").json()["status"] == "pending"

    # the inbound clarification re-extracts the parked message, and it passes
    heal = message(client, message_id="wamid.411.0252", conversation_id=ALICE,
                   text="de las lonas, cambia la cantidad")
    assert heal["status"] == "processed"
    assert heal["self_resolved"] is True
    assert heal["review_id"] == review_id
    assert heal["quote_id"] == "Q-001"

    review = client.get(f"/pending-reviews/{review_id}").json()
    assert review["status"] == "self_resolved"
    assert review["resolution"]["triggered_by_message_id"] == "wamid.411.0252"
    assert review["resolution"]["clarifies_message_id"] == "wamid.411.0250"
    assert review["resolution"]["applied"]["changes"][0]["to"] == 15
    assert [m["text"] for m in review["follow_up_messages"]] == [
        "reviso con el almacen", "de las lonas, cambia la cantidad"]

    # the change is attributed to the parked message, not the clarifying one,
    # and nothing else moved
    assert {li["sku"]: li["qty"] for li in client.get("/quotes/Q-001").json()["line_items"]} == {
        LONA: 15, MESH: 5, MICRO: 5}
    hist = client.get("/quotes/Q-001?history=true").json()["line_item_history"]
    lona = next(h for h in hist if h["sku"] == LONA)
    assert lona["last_changed_by_message_id"] == "wamid.411.0250"
    assert {li["sku"]: li["qty"] for li in client.get("/quotes/Q-002").json()["line_items"]} == {ACRILICO: 2}

    # a healed review is closed to humans: the resolve API only accepts pending
    late = client.post(f"/pending-reviews/{review_id}/resolve", json={"action": "discard"})
    assert late.status_code == 409


def test_human_resolution_applies_the_decision(client):
    phase1(client)
    message(client, message_id="wamid.411.0240", conversation_id=ALICE,
            text="y aparte cotizame 2 acrilicos blancos de 3mm porfa")
    message(client, message_id="wamid.411.0600", conversation_id=ALICE, text="mejor cancelalo")

    review_id = client.get("/pending-reviews").json()["pending_reviews"][0]["review_id"]

    resolved = client.post(
        f"/pending-reviews/{review_id}/resolve",
        json={"action": "apply", "target_quote_id": "Q-002", "line_items": [{"sku": ACRILICO, "quantity": 9}]},
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["status"] == "resolved"

    assert {li["sku"]: li["qty"] for li in client.get("/quotes/Q-002").json()["line_items"]} == {ACRILICO: 9}

    again = client.post(
        f"/pending-reviews/{review_id}/resolve",
        json={"action": "discard"},
    )
    assert again.status_code == 409


def test_resolution_by_discard_touches_no_state(client):
    phase1(client)
    message(client, message_id="wamid.411.0600", conversation_id=ALICE, text="mejor cancelalo")

    review_id = client.get("/pending-reviews").json()["pending_reviews"][0]["review_id"]
    before = client.get(f"/quotes?customer={CUSTOMER}").json()

    response = client.post(f"/pending-reviews/{review_id}/resolve", json={"action": "discard"})

    assert response.status_code == 200
    assert response.json()["status"] == "discarded"
    assert client.get(f"/quotes?customer={CUSTOMER}").json() == before
    assert client.get("/pending-reviews").json()["pending_reviews"] == []


def test_hallucinated_sku_never_reaches_the_database(client, tx):
    """Catalog is the hard gate: an invalid SKU becomes a review, not a row."""
    phase1(client)

    from app.schemas import ExtractionResult

    import app.pipeline as pipeline

    def rogue(catalog, customer_context, history, new_message):
        return ExtractionResult(
            intent="new_quote",
            confidence=0.95,
            target_quote_id=None,
            line_items=[{"sku": "ALFAKE-999-999999", "quantity": 3}],
            mentions_new_request=True,
            reason="model invented a sku",
        )

    original = pipeline.extract
    pipeline.extract = rogue
    try:
        result = message(client, message_id="wamid.411.0700", conversation_id=ALICE,
                         text="cotízame 3 del producto inventado")
    finally:
        pipeline.extract = original

    assert result["status"] == "processed"
    assert "unknown_sku" in result["issues"]

    from app.models import LineItem

    check = tx.factory()
    try:
        assert check.query(LineItem).filter(LineItem.sku == "ALFAKE-999-999999").count() == 0
    finally:
        check.close()


def test_low_confidence_reference_is_reviewed(client):
    phase1(client)

    import app.pipeline as pipeline
    from app.schemas import ExtractionResult

    def shy(catalog, customer_context, history, new_message):
        return ExtractionResult(
            intent="revision",
            confidence=0.4,
            target_quote_id="Q-001",
            line_items=[{"sku": LONA, "quantity": 99}],
            mentions_new_request=False,
            reason="not sure",
        )

    original = pipeline.extract
    pipeline.extract = shy
    try:
        result = message(client, message_id="wamid.411.0800", conversation_id=ALICE,
                         text="cambia las lonas")
    finally:
        pipeline.extract = original

    assert result["requires_review"] is True
    assert "low_confidence" in result["issues"]
    assert {li["sku"]: li["qty"] for li in client.get("/quotes/Q-001").json()["line_items"]} == {
        LONA: 20, MESH: 5, MICRO: 5
    }


def test_llm_context_carries_customer_identity_and_all_linked_chats(client):
    """Context is loaded by company, so a second thread's history is visible."""
    phase1(client)
    message(client, message_id="wamid.662.0001", conversation_id=MARIO,
            text="Buen dia, soy Mario de compras de ACME. Alice me pasó con ustedes, "
            "me confirmas qué lleva la cotización de las lonas?")

    import app.pipeline as pipeline

    captured = {}
    original = pipeline.extract

    def spy(catalog, customer_context, history, new_message):
        captured["catalog"] = catalog
        captured["context"] = customer_context
        captured["history"] = history
        return original(catalog, customer_context, history, new_message)

    pipeline.extract = spy
    try:
        message(client, message_id="wamid.411.1000", conversation_id=ALICE, text="seguimos luego")
    finally:
        pipeline.extract = original

    assert "Alice - ACME INDUSTRIAL" in captured["context"]
    assert "Q-001" in captured["context"]

    seen = [m["text"] for m in captured["history"]]
    assert any("Mario de compras" in text for text in seen), "Mario's chat must be in context"
    assert any("me ayudas a cotizar" in text for text in seen), "Alice's chat must be in context"
    # the catalog handed to the extractor is the one in the database
    assert captured["catalog"] == CATALOG


def test_customer_identity_is_backfilled_then_kept_stable(client, tx):
    """state-management: identity not previously stored gets stored when it
    arrives. Decision 9: once we have a name, later messages do not rewrite it."""
    from app import repositories as inner_repo

    fresh = "C-77000"

    def send(message_id, text, name=None):
        payload = {
            "message_id": message_id,
            "conversation_id": "wa-thread-999",
            "customer_id": fresh,
            "direction": "inbound",
            "timestamp": "2026-02-12T09:00:00Z",
            "text": text,
        }
        if name:
            payload["customer_name"] = name
        response = client.post("/messages", json=payload)
        assert response.status_code == 200, response.text

    send("wamid.999.0001", "hola")  # first sighting: no name available yet

    session = tx.factory()
    try:
        assert inner_repo.get_customer(session, fresh).customer_name is None
    finally:
        session.close()

    send("wamid.999.0002", "somos ACME", name="Nina - ACME INDUSTRIAL")

    session = tx.factory()
    try:
        assert inner_repo.get_customer(session, fresh).customer_name == "Nina - ACME INDUSTRIAL"
    finally:
        session.close()

    # a third message with a different name must not overwrite the stored one
    send("wamid.999.0003", "cambio de contacto", name="Oscar - OTRA EMPRESA")

    session = tx.factory()
    try:
        assert inner_repo.get_customer(session, fresh).customer_name == "Nina - ACME INDUSTRIAL"
    finally:
        session.close()


def test_missing_required_fields_return_400(client):
    response = client.post("/messages", json={"conversation_id": ALICE})
    assert response.status_code == 400
    assert response.json()["detail"]


def test_unknown_quote_returns_404(client):
    assert client.get("/quotes/Q-999").status_code == 404


def test_customer_with_no_quotes_gets_empty_list(client):
    assert client.get("/quotes?customer=C-99999").json() == {"quotes": []}


def test_state_survives_a_restart(client, tx):
    """Everything the pipeline knows lives in Postgres, not in process memory.

    A fresh Session (empty identity map, no cached objects) is the closest a
    rolled-back test can get to a cold process: it has to re-read every row.
    """
    phase1(client)
    message(client, message_id="wamid.411.0231", conversation_id=ALICE,
            text="Oye de las lonas mejor que sean 15, no 20")

    fresh = tx.factory()
    try:
        quotes = repo.get_all_quotes_for_customer(fresh, CUSTOMER)
        assert {q.quote_id for q in quotes} == {"Q-001"}
        assert repo.get_conversation_ids_for_quote(fresh, "Q-001") == [ALICE]
        assert fresh.query(repo.LineItem).count() == 3
        assert fresh.query(repo.ProcessedMessage).count() == 5
    finally:
        fresh.close()
