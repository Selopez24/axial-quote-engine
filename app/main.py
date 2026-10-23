import os

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from app.database import init_db
from app.routes import messages, conversations, quotes, pending_reviews

app = FastAPI(
    title="Quote State Engine",
    description="WhatsApp conversation to structured quote pipeline",
    version="0.1.0"
)


@app.exception_handler(RequestValidationError)
async def validation_to_400(request, exc: RequestValidationError):
    """The spec asks for 400 + field details on malformed payloads.

    FastAPI's default is 422, which is arguably more correct HTTP, but the
    contract we wrote says 400 so callers get one status to branch on.
    """
    return JSONResponse(
        status_code=400,
        content={"detail": [{"field": list(err.get("loc", [])[1:]), "msg": err.get("msg")} for err in exc.errors()]},
    )

# Create tables and load reference data (catalog + customer master) on boot, so a
# fresh checkout is ready to take messages immediately. Opt out with
# QUOTE_ENGINE_AUTOINIT=0 — the test suite does exactly that and seeds its own
# database instead, so running tests never writes to dev data.
if os.getenv("QUOTE_ENGINE_AUTOINIT", "1") != "0":
    init_db()

# Include routers
app.include_router(messages.router, tags=["messages"])
app.include_router(conversations.router, tags=["conversations"])
app.include_router(quotes.router, tags=["quotes"])
app.include_router(pending_reviews.router, tags=["pending-reviews"])

@app.get("/")
def root():
    return {"status": "ok", "service": "Quote State Engine"}
