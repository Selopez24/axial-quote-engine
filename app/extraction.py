"""OpenAI Responses API integration.

One call per inbound message. The LLM only *understands* the message and
classifies it; it never writes state. Everything it returns is re-validated
deterministically in app/validator.py before any row is touched.

Request shape (Responses API, verified against developers.openai.com):
- Stateless: the full input is re-sent every call and `store=False` keeps the
  response out of server-side storage (default is 30+ days; we never fetch
  responses back, and these are customer transcripts).
- Prompt layout follows Decision 5: the static instructions + catalog ride in a
  leading developer message carrying an explicit cache breakpoint (on GPT-5.6+
  top-level `instructions` cannot hold one), then customer context, then the
  chat as "[timestamp] speaker (company): text" lines, newest last.
- Caching runs in the API's default implicit mode on GPT-5.6+: OpenAI also
  places a breakpoint at the newest message, so a thread's history is written
  as small deltas (1.25x) and read back at 0.1x on the next turn — long
  back-and-forth conversations get cheaper as they grow. Our explicit
  developer-prefix breakpoint is the boundary every request shares.
Legacy pre-5.6 models (gpt-4o-mini era) keep the old shape: top-level
`instructions` + temperature=0, no breakpoint field.
"""

import json
import logging
import os
import re

from openai import OpenAI

from app.schemas import ExtractionResult

logger = logging.getLogger(__name__)

MODEL = os.getenv("QUOTE_ENGINE_MODEL", "gpt-6-luna")

# GPT-5.6 and later changed the prompt-caching rules (OpenAI docs): implicit
# breakpoints only land at message endings, cache writes are charged 1.25x,
# reusable instructions must live in a developer message to hold an explicit
# breakpoint, no prompt_cache_key is needed for routing, and a custom
# temperature is not accepted. Models before that (e.g. gpt-4o-mini) predate
# all of it, so the legacy request shape is kept for them behind one flag.
_IS_GPT56_PLUS = re.match(r"gpt-(?:[6-9](?:\.\d+)?|5\.[6-9])", MODEL) is not None

_client: OpenAI | None = None


def _gateway_session_id() -> str:
    """Stable per-process id; OpenCode Go wants one per conversation so it can
    route requests sharing a prompt prefix to the same cache (its docs ask
    non-agent clients to send it; without it inference returns
    400 MissingSessionID). Reuses OPENCODE_SESSION_ID when the host app
    already has one."""
    import uuid

    sid = os.getenv("OPENCODE_SESSION_ID")
    if not sid:
        sid = f"quote-engine-{uuid.uuid4()}"
        os.environ["OPENCODE_SESSION_ID"] = sid
    return sid


def get_client() -> OpenAI:
    global _client
    if _client is None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Export it or add it to .env before "
                "running the pipeline (it is only needed for inbound extraction)."
            )
        # Optional OpenAI-compatible gateway (e.g. OpenCode Go:
        # https://opencode.ai/inference/go/openai/v1 with QUOTE_ENGINE_MODEL=gpt-6-luna).
        # Unset base_url means api.openai.com, the default for this project.
        # Go requires non-agents clients to send a stable x-opencode-session id
        # (400 MissingSessionID otherwise) and to identify themselves.
        headers = {}
        if os.getenv("OPENAI_BASE_URL"):
            headers = {
                "user-agent": "axial-quote-engine/0.1",
                "x-opencode-session": _gateway_session_id(),
            }
        _client = OpenAI(api_key=api_key, base_url=os.getenv("OPENAI_BASE_URL"),
                         default_headers=headers)
    return _client


# ---------- static prefix: system instructions + catalog ----------

def build_instructions(catalog: dict[str, str]) -> str:
    if catalog:
        catalog_lines = "\n".join(f"- {sku}: {desc}" for sku, desc in sorted(catalog.items()))
    else:
        catalog_lines = "(catalogo vacio: NINGUN SKU es valido, devuelve intent unclear)"
    return f"""Eres un extractor de cotizaciones ("Oferta de Venta") a partir de conversaciones de WhatsApp en español entre un cliente de una distribuidora de materiales industriales y un vendedor.

CATALOGO VALID (unica fuente de SKUs; es IMPOSIBLE emitir un SKU que no este aqui):
{catalog_lines}

INSTRUCCIONES:
1. Lee TODO el historial en ambas direcciones (inbound = cliente, outbound = vendedor). Los detalles de una linea pueden estar repartidos: el producto en un mensaje, la medida en otro dicho por el vendedor, la cantidad en un tercero.
2. Clasifica el ULTIMO mensaje del input (el de marca de tiempo mas reciente; los mensajes llevan su hora al inicio, del mas antiguo al mas reciente) en una intencion:
   - "new_quote": el cliente pide explicitamente una cotizacion aparte ("aparte", "separado", "otra cotizacion"), o pide un producto nuevo SIN ninguna conversacion pendiente sobre ese producto en el hilo.
   - "revision": cambia una cantidad de una cotizacion existente (p.ej. "mejor que sean 15, no 20") O completa una pregunta de disponibilidad abierta sobre esa cotizacion: si en este mismo hilo el cliente pregunto "si cuentas con X?" y ahora llega la cantidad de X (aunque la confirme el vendedor entre medio), esa cantidad es una revision que AGREGA la linea de X a la cotizacion en curso, NO una cotizacion nueva.
   - "reference": pregunta o confirma sobre una cotizacion existente SIN cambiar nada (p.ej. "me confirmas que lleva la cotizacion de las lonas?").
   - "other": el mensaje NO tiene ninguna accion de cotizacion: saludos, agradecimientos, chitchat, o SOLO una pregunta de disponibilidad sobre un producto que no esta en ninguna cotizacion abierta (p.ej. "si cuentan con manguera?"). line_items vacia, target_quote_id null. NO es unclear: un mensaje "other" no cambia nada y no va a revision.
   - "unclear": hay una ORDEN FIRME o una decision de cotizacion real y no se puede determinar con seguridad a que cotizacion aplica (p.ej. "mejor cancelalo" con dos cotizaciones abiertas), o la orden firme es de un producto fuera del catalogo.
3. target_quote_id: el ID de la cotizacion a la que se refiere el mensaje. DEBE elegirse segun el CONTENIDO del mensaje, no segun en que chat llego. Un mismo chat puede alimentarse de varias cotizaciones y una cotizacion puede llegar por chats distintos. Si el mensaje menciona un producto ("los acrilicos", "las lonas") usa el contexto para identificar la cotizacion que contiene ese producto, aun si esa cotizacion nacio en otro chat. Si el mensaje NO da ninguna pista de contenido (ni producto, ni medida) y hay mas de una cotizacion abierta, NO elijas target por recencia ni por suposicion: usa intent unclear o confidence < 0.7. Una opcion es un hecho; dos son una suposicion. Ojo con la diferencia: un intercambio de aclaracion ADYACENTE si es pista (el vendedor pregunta cual cotizacion y el cliente responde "a los acrilicos": esa respuesta define el destino aunque el mensaje que clasificas no nombre el producto). La mera RECENCIA del tema NO es pista: que el ultimo intercambio del hilo haya hablado de acrilicos no convierte "subelo a 10" en acrilicos. Sin mencion explicita ni respuesta a una pregunta abierta, mantén unclear o confidence < 0.7.
4. line_items: solo los SKUs y cantidades que el mensaje implica. Para "revision" incluye la cantidad NUEVA. Para "new_quote" incluye todos los items de la cotizacion nueva. Para "reference" devuelve lista vacia.
5. NO inventes precios. NO inventas SKUs. Un mensaje puede mezclar ordenes con cantidad firme y preguntas de disponibilidad ("si cuentas con X?", "tendran Y?"): las preguntas NO son lineas de cotizacion (la cantidad puede llegar en otro mensaje) y NO obligan a usar unclear; extrae solo las ordenes firmes. Usa unclear SOLO cuando hay una orden firme problematica: un producto que no esta en el catalogo, o una decision real entre varias cotizaciones que no puedes resolver; explicalo en reason. Un mensaje SIN ninguna orden firme (solo saludos, gracias, o preguntas) NUNCA es unclear: es "other", o "reference" si la pregunta apunta a una cotizacion abierta que contiene ese producto.
6. Si un mensaje dice "cancelalo" o similar y hay mas de una cotizacion abierta, NO adivines: intent "unclear".
7. confidence: 0.0-1.0. Usa menos de 0.7 cuando la asignacion a una cotizacion no sea clara.
8. "aparte" / "separado" / "otra cotizacion" indica cotizacion nueva, no revision de la existente.

FORMATO DE LA ENTRADA: el primer mensaje es CONTEXTO DE COTIZACIONES; luego el chat, un mensaje por linea en orden cronologico, cada uno como "[fecha-hora] Emisor (Empresa, si es cliente): texto". NO hay etiquetas de historial: el mensaje a clasificar es la ULTIMA linea, la mas reciente en el tiempo. Un mensaje posterior que aclara o corrige uno anterior manda sobre el: "a los acrilicos" dicho despues de "subelo a 10" resuelve "subelo a 10".
"""


# ---------- per-customer prefix: open quote structure ----------

def build_customer_context(
    quotes: list[dict],
    customer_name: str | None = None,
    company_name: str | None = None,
) -> str:
    """Per-customer prefix: who we are talking to, then what is open.

    Customer identity is included because the resolution of "soy Mario de
    compras de ACME" depends on knowing whose quotes are in play. Before any
    message has named a person, the seeded company name is still useful context.
    """
    who = customer_name or company_name or "(cliente sin nombre registrado)"
    identity = f"CLIENTE: {who}"

    if not quotes:
        quotes_block = "COTIZACIONES ABIERTAS DEL CLIENTE: ninguna todavia."
    else:
        blocks = []
        for q in quotes:
            items = ", ".join(f"{li['sku']} qty {li['qty']}" for li in q["line_items"]) or "(sin lineas)"
            convs = ", ".join(q["conversations"])
            blocks.append(f"- {q['quote_id']} [{q['status']}] lineas: {items} | chats vinculados: {convs}")
        quotes_block = (
            "COTIZACIONES ABIERTAS DEL CLIENTE (usa el contenido para decidir a cual apunta el mensaje, "
            "no el chat de origen):\n" + "\n".join(blocks)
        )
    return f"{identity}\n{quotes_block}"


def display_name(full_name: str | None, fallback: str) -> str:
    """'Alice - ACME INDUSTRIAL' -> 'Alice'. Speakers are named by person, and
    messages refer to people ('las lonas que cotizó Alice'), not by company."""
    if not full_name:
        return fallback
    return full_name.split(" - ")[0].strip() or fallback


def speaker_prefix(message: dict) -> str:
    """Decision 7: Responses API has no `name` field, so the speaker goes in content."""
    return f"{speaker_name(message)}: {message.get('text', '')}"


def speaker_name(message: dict) -> str:
    if message.get("sender_name"):
        name = message["sender_name"]
    elif message.get("direction") == "inbound":
        name = display_name(message.get("customer_name"), "Cliente")
    else:
        # An outbound message is the sales rep talking. Without an explicit
        # sender_name it must never borrow the customer's name — that would put
        # words in Alice's mouth that Izzy wrote, and the model resolves quotes
        # by who said what.
        name = "Vendedor"
    return name


def company_of(message: dict) -> str | None:
    """'Alice - ACME INDUSTRIAL' -> 'ACME INDUSTRIAL'. Only inbound: the company
    is the buyer's; labeling the rep with it would misattribute the seat."""
    full = message.get("customer_name")
    if message.get("direction") != "inbound" or not full or " - " not in full:
        return None
    return full.split(" - ", 1)[1].strip() or None


def message_line(message: dict) -> str:
    """[timestamp] speaker (company): text — and then it NEVER changes again.

    This is the cache-stable form: a message renders from its stored dict alone,
    so the same message contributes byte-identical tokens to every future call
    that includes it. Prefix-cache reuse lives or dies on that (a marker that
    mutates when a message ages out of the 'newest' slot breaks every boundary
    alignment). The timestamp is also the semantic load-bearer: later lines are
    later in time, and a later clarification re-reads an earlier ask.
    """
    who = speaker_name(message)
    company = company_of(message)
    if company:
        who = f"{who} ({company})"
    stamp = message.get("timestamp")
    prefix = f"[{stamp}] " if stamp else ""
    return f"{prefix}{who}: {message.get('text', '')}"


def build_input(
    customer_context: str,
    history: list[dict],
    new_message: dict,
) -> list[dict]:
    """Chronological message lines, newest last; the message being classified
    is the last line, not a marked one. The newest message is usually already
    in `history` (stored before extraction) — render it once, in the final
    slot. Append-only + stable lines: every earlier turn's cache breakpoint
    still lands on a message ending of the next turn."""
    messages = [
        {"role": "user", "content": f"CONTEXTO DE COTIZACIONES:\n{customer_context}"},
    ]
    new_id = new_message.get("message_id")
    for m in history:
        if new_id and m.get("message_id") == new_id:
            continue
        messages.append({"role": "user", "content": message_line(m)})
    messages.append({"role": "user", "content": message_line(new_message)})
    return messages


def instructions_message(catalog: dict[str, str]) -> dict:
    """Static system instructions + catalog as the leading developer message.
    On GPT-5.6 and later it carries the explicit cache breakpoint (top-level
    `instructions` cannot hold one); on earlier models extract() sends the
    same text through the legacy `instructions` parameter instead."""
    block: dict = {"type": "input_text", "text": build_instructions(catalog)}
    if _IS_GPT56_PLUS:
        block["prompt_cache_breakpoint"] = {"mode": "explicit"}
    return {"role": "developer", "content": [block]}


RESPONSE_SCHEMA = {
    "type": "json_schema",
    "name": "quote_extraction",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "intent": {
                "type": "string",
                "enum": ["new_quote", "revision", "reference", "other", "unclear"],
            },
            "confidence": {"type": "number"},
            "target_quote_id": {"type": ["string", "null"]},
            "line_items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "sku": {"type": "string"},
                        "quantity": {"type": "integer"},
                    },
                    "required": ["sku", "quantity"],
                },
            },
            "mentions_new_request": {"type": "boolean"},
            "reason": {"type": ["string", "null"]},
        },
        "required": [
            "intent",
            "confidence",
            "target_quote_id",
            "line_items",
            "mentions_new_request",
            "reason",
        ],
    },
}


def extract(
    catalog: dict[str, str],
    customer_context: str,
    history: list[dict],
    new_message: dict,
) -> ExtractionResult:
    """One structured call. The catalog is passed in from the database by the
    caller, so this module holds no SKU truth of its own and tests can inject
    whatever they need."""
    client = get_client()
    input_items = build_input(customer_context, history, new_message)
    kwargs: dict = {
        "model": MODEL,
        "input": input_items,
        "text": {"format": RESPONSE_SCHEMA},
        # We never fetch responses back — but the API default is store=true,
        # which keeps the rendered customer transcript server-side for 30+ days
        # (docs: create-response `store`). Stateless + store=False is the
        # documented shape for a one-shot extraction pipeline.
        "store": False,
    }
    if _IS_GPT56_PLUS:
        # Reusable instructions go in the leading developer message, where the
        # explicit cache breakpoint lives (Decision 5). No temperature: GPT-5.6
        # and later only take the default; determinism is carried by the strict
        # schema plus app/validator.py, which re-checks every extraction.
        input_items.insert(0, instructions_message(catalog))
        # No prompt_cache_options override: the default implicit mode places
        # its own breakpoint at the newest message, so each turn writes just
        # the thread's new tokens at 1.25x and the next turn reads the whole
        # thread prefix back at 0.1x — that is what keeps long conversations
        # cheap (the docs' multi-turn-agent pattern). Our explicit breakpoint
        # on the developer prefix (measured 1,226 tokens, above the 1,024
        # minimum) is the boundary every request shares; when a quote change
        # rewrites the customer-context line the thread prefix misses and the
        # lookup falls back to it.
        # Strict-schema classification is a shallow-reasoning task: effort
        # "low" halved output tokens (168 -> 72 measured) with identical
        # payloads on the live suite, and the validator + review queue guard
        # every misread. Effort participates in the cached prefix, so the
        # value stays constant across calls.
        kwargs["reasoning"] = {"effort": "low"}
        response = client.responses.create(**kwargs)
    else:
        # Legacy shape (gpt-4o-mini era): top-level instructions, temperature
        # accepted, implicit interval-based caching without breakpoints.
        response = client.responses.create(
            **kwargs,
            instructions=build_instructions(catalog),
            temperature=0,
        )
    if getattr(response, "status", "completed") != "completed":
        # "incomplete" (token cap) / "failed" — output_text would be a
        # truncated JSON at best. Raise so the pipeline parks the message for
        # review instead of parsing half an answer.
        raise RuntimeError(f"LLM response status={response.status}")
    usage = getattr(response, "usage", None)
    if usage is not None:
        details = getattr(usage, "input_tokens_details", None)
        logger.info(
            "responses call model=%s in=%s cached=%s cache_write=%s out=%s",
            MODEL,
            usage.input_tokens,
            getattr(details, "cached_tokens", 0),
            getattr(details, "cache_write_tokens", 0),
            getattr(usage, "output_tokens", 0),
        )
    payload = json.loads(response.output_text)
    return ExtractionResult(**payload)
