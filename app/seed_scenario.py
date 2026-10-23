"""Optional scenario seeder for demos and reviewer convenience.

Loads db/scenario_phase1.json and processes it through the pipeline, so a
reviewer can start the app with SEED_SCENARIO=1 and immediately query quotes
without manually POSTing the Phase 1 batch.

Requires OPENAI_API_KEY to be set (the pipeline needs the LLM for extraction).
If the key is not set, the seed is skipped with a warning.
"""

import json
import os
from pathlib import Path

from sqlalchemy.orm import Session

from app.routes.conversations import _to_messages
from app.schemas import ConversationIn


def seed_scenario(db: Session) -> None:
    """Load and process the Phase 1 scenario fixture.

    Idempotent: the pipeline dedupes on message_id, so re-running this on a
    database that already has the scenario state is a no-op.

    Requires OPENAI_API_KEY. If not set, skips with a warning.
    """
    if not os.getenv("OPENAI_API_KEY"):
        print("SEED_SCENARIO: OPENAI_API_KEY not set — skipping scenario seed (LLM extraction requires it)")
        return

    try:
        fixture_path = Path(__file__).parent.parent / "db" / "scenario_phase1.json"
        with open(fixture_path) as f:
            data = json.load(f)

        payload = ConversationIn(**data)
        for message in _to_messages(payload):
            from app.pipeline import process_message
            process_message(db, message)
        db.commit()
        print("SEED_SCENARIO: Phase 1 scenario loaded successfully")
    except Exception as e:
        print(f"SEED_SCENARIO: Failed to seed scenario: {e}")
        db.rollback()
        raise
