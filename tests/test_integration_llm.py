"""The assignment scenario run through the real model.

Skipped when OPENAI_API_KEY is absent, so `pytest` stays green offline. This is
the file that proves Phase 1 extraction is good enough to feed Phase 2, and
that Event 4 resolves the right quote from a different chat — the two things a
stub cannot prove.

Run with (either a direct OpenAI key, or any OpenAI-compatible gateway by
setting OPENAI_BASE_URL and a QUOTE_ENGINE_MODEL that gateway serves):
    export OPENAI_API_KEY=***   # plus OPENAI_BASE_URL + QUOTE_ENGINE_MODEL for a gateway
    pytest tests/test_integration_llm.py -v -s
"""

import os

import pytest

requires_openai = pytest.mark.skipif(
    not os.getenv("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set; run tests/test_phase2.py for deterministic coverage",
)

CUSTOMER = "C-10029"
ALICE = "wa-thread-411"
MARIO = "wa-thread-662"

LONA = "ALFBTC-013-320100"
MESH = "ALMCLC-0909-320100"
MICRO = "AVMICS-1A-137050"
ACRILICO = "ARACBL-03-122244"

PHASE1 = {
    "customer_id": CUSTOMER,
    "conversation_id": ALICE,
    "customer_name": "Alice - ACME INDUSTRIAL",
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

EVENTS = [
    {
        "message_id": "wamid.411.0231",
        "conversation_id": ALICE,
        "customer_id": CUSTOMER,
        "direction": "inbound",
        "timestamp": "2026-02-11T10:14:00Z",
        "text": "Oye de las lonas mejor que sean 15, no 20",
    },
    {
        "message_id": "wamid.662.0001",
        "conversation_id": MARIO,
        "customer_id": CUSTOMER,
        "direction": "inbound",
        "timestamp": "2026-02-11T10:20:00Z",
        "customer_name": "Mario - ACME INDUSTRIAL",
        "text": "Buen dia, soy Mario de compras de ACME. Alice me pasó con ustedes, "
        "me confirmas qué lleva la cotización de las lonas?",
    },
    {
        "message_id": "wamid.411.0240",
        "conversation_id": ALICE,
        "customer_id": CUSTOMER,
        "direction": "inbound",
        "timestamp": "2026-02-11T10:30:00Z",
        "text": "y aparte cotizame 2 acrilicos blancos de 3mm porfa",
    },
    {
        "message_id": "wamid.662.0002",
        "conversation_id": MARIO,
        "customer_id": CUSTOMER,
        "direction": "inbound",
        "timestamp": "2026-02-11T10:40:00Z",
        "customer_name": "Mario - ACME INDUSTRIAL",
        "text": "perfecto. y de los acrilicos que cotizó Alice, mejor que sean 4",
    },
]


def _quantities(quote):
    return {li["sku"]: li["qty"] for li in quote["line_items"]}


@pytest.fixture()
def llm_scenario(client):
    """Phase 1 + the four events, exactly as the assignment delivers them."""
    response = client.post("/conversations", json=PHASE1)
    assert response.status_code == 200, response.text

    for event in EVENTS:
        response = client.post("/messages", json=event)
        assert response.status_code == 200, response.text

    yield client


@requires_openai
def test_phase1_extraction_is_good_enough_to_feed_phase2(client):
    """The micro line needs the rep's size answer + Alice's quantity reply."""
    response = client.post("/conversations", json=PHASE1)
    assert response.status_code == 200, response.text

    quote = client.get("/quotes/Q-001").json()
    assert quote["status"] == "draft"
    assert quote["conversations"] == [ALICE]
    assert _quantities(quote) == {LONA: 20, MESH: 5, MICRO: 5}


@requires_openai
def test_expected_final_state_after_four_events(llm_scenario):
    """The assignment's expected final state, verbatim."""
    body = llm_scenario.get(f"/quotes?customer={CUSTOMER}").json()
    quotes = {q["quote_id"]: q for q in body["quotes"]}

    assert set(quotes) == {"Q-001", "Q-002"}

    assert _quantities(quotes["Q-001"]) == {LONA: 15, MESH: 5, MICRO: 5}
    assert sorted(quotes["Q-001"]["conversations"]) == sorted([ALICE, MARIO])

    assert _quantities(quotes["Q-002"]) == {ACRILICO: 4}
    assert sorted(quotes["Q-002"]["conversations"]) == sorted([ALICE, MARIO])


@requires_openai
def test_event4_revises_the_other_threads_quote(llm_scenario):
    """Content picks the quote, not the chat it arrived in."""
    body = llm_scenario.get(f"/quotes?customer={CUSTOMER}").json()
    quotes = {q["quote_id"]: q for q in body["quotes"]}

    # Q-001's lona line is still 15: event 4 touched Q-002 only
    assert quotes["Q-001"]["line_items"] and _quantities(quotes["Q-001"])[LONA] == 15
    assert _quantities(quotes["Q-002"])[ACRILICO] == 4


@requires_openai
def test_no_hallucinated_sku_can_appear(llm_scenario):
    from app.catalog import VALID_SKUS

    body = llm_scenario.get(f"/quotes?customer={CUSTOMER}").json()
    for quote in body["quotes"]:
        for line in quote["line_items"]:
            assert line["sku"] in VALID_SKUS


@requires_openai
def test_every_change_traces_back_to_a_message(llm_scenario):
    quote = llm_scenario.get("/quotes/Q-001?history=true").json()
    assert quote["line_item_history"]
    for line in quote["line_item_history"]:
        assert line["created_by_message_id"], line
    # event 1 rewrote the lona line, so the causing message is not the creating one
    lona = next(h for h in quote["line_item_history"] if h["sku"] == LONA)
    assert lona["last_changed_by_message_id"] == "wamid.411.0231"


@requires_openai
def test_duplicate_event4_is_inert(llm_scenario):
    before = llm_scenario.get(f"/quotes?customer={CUSTOMER}").json()

    response = llm_scenario.post("/messages", json=EVENTS[3])

    assert response.status_code == 200
    assert response.json()["status"] == "duplicate"
    assert llm_scenario.get(f"/quotes?customer={CUSTOMER}").json() == before


@requires_openai
def test_non_quote_messages_pass_inertly(llm_scenario):
    """Out-of-guide probe cases, on the real model: a pure availability
    question about a product on NO open quote, and a thank-you. Both must
    come back intent other with zero side effects — the live probe found the
    old rules parsing these as unclear under effort=low, parking them and
    then swallowing every later message of the thread into that review."""
    before = llm_scenario.get(f"/quotes?customer={CUSTOMER}").json()

    for mid, ts, text in [
        ("wamid.411.9301", "2026-02-11T13:00:00Z",
         "una consulta, si cuentan con manguera de 1 pulgada?"),
        ("wamid.411.9302", "2026-02-11T13:01:00Z",
         "muchas gracias, buen fin de semana!"),
    ]:
        response = llm_scenario.post("/messages", json={
            "conversation_id": ALICE, "customer_id": CUSTOMER,
            "message_id": mid, "direction": "inbound",
            "timestamp": ts, "text": text,
        })
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "processed", body
        assert body["intent"] == "other", body
        assert not body.get("requires_review"), body

    assert llm_scenario.get(f"/quotes?customer={CUSTOMER}").json() == before
    assert llm_scenario.get("/pending-reviews").json()["pending_reviews"] == []
