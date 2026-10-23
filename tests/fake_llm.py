"""A deterministic stand-in for the LLM.

This is not a mock that rubber-stamps the expected answer: it re-implements the
rules the real prompt asks for (content decides the target quote, the chat of
arrival does not) and then the production code under test still runs its own
catalog validation, confidence gate, dedup and linking logic.

The target quote is resolved by parsing the customer context block the pipeline
built, exactly the way the model is told to: find the quote whose lines contain
the product the message mentions.
"""

import re

from app.schemas import ExtractionResult

# product word in the message -> the SKU it points at
KEYWORDS = {
    "lonas": "ALFBTC-013-320100",
    "lona": "ALFBTC-013-320100",
    "mesh": "ALMCLC-0909-320100",
    "microperforado": "AVMICS-1A-137050",
    "acrilic": "ARACBL-03-122244",
    "acrílic": "ARACBL-03-122244",
}

# one context line looks like:
#   - Q-001 [draft] lineas: ALFBTC-013-320100 qty 20, AVMICS-1A-137050 qty 5 | chats ...
CONTEXT_LINE = re.compile(r"- (?P<qid>Q-\d+)\s.*?lineas:\s(?P<items>[^|]*)")


def quotes_from_context(customer_context: str) -> list[tuple[str, list[str]]]:
    found = []
    for match in CONTEXT_LINE.finditer(customer_context):
        skus = re.findall(r"[A-Z]{6}-\S+? qty", match.group("items"))
        skus = [s.replace(" qty", "").strip() for s in skus]
        found.append((match.group("qid"), skus))
    return found


def mentioned_skus(text: str) -> list[str]:
    lowered = text.lower()
    return [sku for word, sku in KEYWORDS.items() if word in lowered]


def qty_change(text: str) -> int | None:
    """"mejor que sean 15, no 20" -> 15. Takes the number before 'no'."""
    match = re.search(r"(?:que sean|mejor|dejen|queden)\s+(\d+)", text.lower())
    if match:
        return int(match.group(1))
    return None


def extract(catalog: dict[str, str], customer_context: str, history: list[dict], new_message: dict) -> ExtractionResult:
    # catalog is ignored: this fake only ever emits SKUs from the fixed set, and
    # the production validator is what decides whether they are legal.
    text = (new_message.get("text") or "").lower()
    quotes = quotes_from_context(customer_context)
    skus = mentioned_skus(text)

    def target_for(sku: str) -> str | None:
        for qid, items in quotes:
            if sku in items:
                return qid
        return None

    # "aparte cotizame 2 acrilicos blancos" -> a separate, new quote
    if "aparte" in text or "separado" in text or "otra cotizacion" in text:
        lines = []
        for sku in skus:
            qty = _first_qty_for(text, sku) or 1
            lines.append({"sku": sku, "quantity": qty})
        if not lines:
            for sku, qty in _PHASE1_ITEMS:
                lines.append({"sku": sku, "quantity": qty})
        return ExtractionResult(
            intent="new_quote",
            confidence=0.9,
            target_quote_id=None,
            line_items=lines,
            mentions_new_request=True,
            reason="cliente pide cotizacion aparte",
        )

    # first-ever request in a thread with no open quotes -> new quote
    if not quotes and ("cotizar" in text or "cotizame" in text or "ayudas" in text):
        return ExtractionResult(
            intent="new_quote",
            confidence=0.9,
            target_quote_id=None,
            line_items=[{"sku": s, "quantity": q} for s, q in _PHASE1_ITEMS],
            mentions_new_request=True,
            reason="solicitud inicial",
        )

    # quantity revision: "de las lonas mejor que sean 15, no 20"
    new_qty = qty_change(text)
    if new_qty is not None and skus:
        target = target_for(skus[0])
        if target:
            return ExtractionResult(
                intent="revision",
                confidence=0.92,
                target_quote_id=target,
                line_items=[{"sku": skus[0], "quantity": new_qty}],
                mentions_new_request=False,
                reason="cambio de cantidad",
            )

    # adding a line to an existing quote: "Necesitamos 5 rollos" answering the rep
    if re.search(r"\b(\d+)\b", text) and any(q for q, _ in quotes):
        qty = int(re.search(r"\b(\d+)\b", text).group(1))
        # the pending product is whatever the conversation was asking about
        pending = _pending_product(history)
        if pending:
            target = target_for(pending) or (quotes[0][0] if quotes else None)
            if target:
                return ExtractionResult(
                    intent="revision",
                    confidence=0.8,
                    target_quote_id=target,
                    line_items=[{"sku": pending, "quantity": qty}],
                    mentions_new_request=False,
                    reason="cantidad pendiente respondida",
                )

    # "me confirmas que lleva la cotizacion de las lonas?" -> reference, no change
    if skus and any(w in text for w in ("confirmas", "lleva", "estado", "pas")):
        target = target_for(skus[0])
        if target:
            return ExtractionResult(
                intent="reference",
                confidence=0.85,
                target_quote_id=target,
                line_items=[],
                mentions_new_request=False,
                reason="consulta sin cambio",
            )

    # genuinely ambiguous: "mejor cancelalo" with more than one open quote
    if "cancel" in text:
        return ExtractionResult(
            intent="unclear",
            confidence=0.3,
            target_quote_id=None,
            line_items=[],
            mentions_new_request=False,
            reason=f"hay {len(quotes)} cotizaciones abiertas, no se cual es",
        )

    return ExtractionResult(
        intent="unclear",
        confidence=0.4,
        target_quote_id=None,
        line_items=[],
        mentions_new_request=False,
        reason="no clasificable",
    )


def _first_qty_for(text: str, sku: str) -> int | None:
    match = re.search(r"(\d+)\s*(?:acrilic|acrílic)", text)
    if match and sku == "ARACBL-03-122244":
        return int(match.group(1))
    match = re.search(r"(\d+)\s+(?:rollos|piezas|unidades)", text)
    return int(match.group(1)) if match else None


def _pending_product(history: list[dict]) -> str | None:
    """The product the last outbound message was asking about."""
    for message in reversed(history):
        if message.get("direction") != "outbound":
            continue
        for sku in mentioned_skus(message.get("text", "")):
            return sku
    return None


# what the customer asked for in the Phase 1 opening message, before the
# microperforado quantity was known
_PHASE1_ITEMS = [
    ("ALFBTC-013-320100", 20),
    ("ALMCLC-0909-320100", 5),
]


def patch(monkeypatch):
    """Install this fake as the pipeline's extractor."""
    monkeypatch.setattr("app.pipeline.extract", extract)
