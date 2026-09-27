"""
bot.py — magicpin AI Challenge submission.

Exposes the 5 required endpoints (challenge-testing-brief.md §2):
  POST /v1/context   POST /v1/tick   POST /v1/reply
  GET  /v1/healthz   GET  /v1/metadata

Composition logic lives in composer.py (stateless, deterministic).
Conversation/reply logic lives in reply_engine.py.
This file is just wiring: in-memory storage + request/response shapes.

Run locally:
    pip install fastapi uvicorn
    uvicorn bot:app --host 0.0.0.0 --port 8080

Then in another terminal, from the challenge zip:
    export BOT_URL=http://localhost:8080
    python judge_simulator.py
"""

from __future__ import annotations
import time
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI
from pydantic import BaseModel

from composer import compose
from reply_engine import ConversationState, respond

app = FastAPI(title="magicpin-ai-challenge-bot")
START = time.time()

# ---------------------------------------------------------------------------
# in-memory stores — fine per the brief ("storing in memory is fine; just
# don't restart between calls"). Swap for Redis/SQLite if you need real
# persistence across restarts.
# ---------------------------------------------------------------------------

contexts: dict[tuple[str, str], dict] = {}          # (scope, context_id) -> {version, payload}
conversations: dict[str, ConversationState] = {}    # conversation_id -> state
sent_suppression_keys: set[str] = set()             # avoid duplicate sends per suppression_key

TEAM_NAME = "Disha Bisht"
TEAM_MEMBERS = ["Disha Bisht"]
CONTACT_EMAIL = "REPLACE_ME@example.com"


def _get(scope: str, context_id: str) -> Optional[dict]:
    entry = contexts.get((scope, context_id))
    return entry["payload"] if entry else None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# GET /v1/healthz
# ---------------------------------------------------------------------------

@app.get("/v1/healthz")
async def healthz():
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for (scope, _), _ in contexts.items():
        counts[scope] = counts.get(scope, 0) + 1
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START),
        "contexts_loaded": counts,
    }


# ---------------------------------------------------------------------------
# GET /v1/metadata
# ---------------------------------------------------------------------------

@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": TEAM_NAME,
        "team_members": TEAM_MEMBERS,
        "model": "rules-based-deterministic-composer (no LLM call)",
        "approach": (
            "Deterministic template composer grounded strictly in pushed "
            "context (category/merchant/trigger/customer). Trigger kind is "
            "routed to a 'framing family' via keyword matching rather than "
            "a hardcoded switch, so unseen trigger kinds injected mid-test "
            "still route sensibly. Reply handling covers auto-reply "
            "detection (keyword + verbatim-repeat), explicit intent "
            "handoff, not-interested exit, and hostile/off-topic "
            "de-escalation. No hallucination risk since the composer can "
            "only reference fields actually present in the context."
        ),
        "contact_email": CONTACT_EMAIL,
        "version": "1.0.0",
        "submitted_at": _now_iso(),
    }


# ---------------------------------------------------------------------------
# POST /v1/context
# ---------------------------------------------------------------------------

class CtxBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: str


@app.post("/v1/context")
async def push_context(body: CtxBody):
    key = (body.scope, body.context_id)
    cur = contexts.get(key)
    if cur and cur["version"] >= body.version:
        return {"accepted": False, "reason": "stale_version",
                "current_version": cur["version"]}
    contexts[key] = {"version": body.version, "payload": body.payload}
    return {
        "accepted": True,
        "ack_id": f"ack_{body.context_id}_v{body.version}",
        "stored_at": _now_iso(),
    }


# ---------------------------------------------------------------------------
# POST /v1/tick
# ---------------------------------------------------------------------------

class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = []


@app.post("/v1/tick")
async def tick(body: TickBody):
    actions = []

    for trg_id in body.available_triggers[:20]:  # respect 20 actions/tick cap
        trigger = _get("trigger", trg_id)
        if not trigger:
            continue

        suppression_key = trigger.get("suppression_key", trg_id)
        if suppression_key in sent_suppression_keys:
            continue  # already messaged for this exact reason — don't spam

        merchant_id = trigger.get("merchant_id")
        merchant = _get("merchant", merchant_id) if merchant_id else None
        if not merchant:
            continue
        category = _get("category", merchant.get("category_slug"))
        if not category:
            continue

        customer = None
        if trigger.get("scope") == "customer" and trigger.get("customer_id"):
            customer = _get("customer", trigger["customer_id"])
            if not customer:
                continue  # can't compose a customer-facing message without it

        composed = compose(category, merchant, trigger, customer)

        conversation_id = f"conv_{merchant_id}_{trg_id}"
        state = ConversationState(
            conversation_id=conversation_id,
            merchant_id=merchant_id,
            customer_id=trigger.get("customer_id"),
            trigger_id=trg_id,
            first_body=composed["body"],
        )
        conversations[conversation_id] = state
        sent_suppression_keys.add(suppression_key)

        actions.append({
            "conversation_id": conversation_id,
            "merchant_id": merchant_id,
            "customer_id": trigger.get("customer_id"),
            "send_as": composed["send_as"],
            "trigger_id": trg_id,
            "template_name": f"vera_{composed['send_as']}_v1",
            "template_params": [merchant.get("identity", {}).get("name", "")],
            "body": composed["body"],
            "cta": composed["cta"],
            "suppression_key": composed["suppression_key"],
            "rationale": composed["rationale"],
        })

        if len(actions) >= 20:
            break

    return {"actions": actions}


# ---------------------------------------------------------------------------
# POST /v1/reply
# ---------------------------------------------------------------------------

class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: str
    turn_number: int


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    state = conversations.get(body.conversation_id)
    if state is None:
        # judge started a conversation thread we don't recognize — create a
        # minimal state so we can still respond sensibly rather than 500ing
        state = ConversationState(
            conversation_id=body.conversation_id,
            merchant_id=body.merchant_id,
            customer_id=body.customer_id,
            trigger_id=None,
            first_body="",
        )
        conversations[body.conversation_id] = state

    result = respond(state, body.message)
    if result.get("action") == "send" and result.get("body"):
        state.register_sent(result["body"])
    return result


# ---------------------------------------------------------------------------
# POST /v1/teardown (optional, per testing-brief §11 — wipe state)
# ---------------------------------------------------------------------------

@app.post("/v1/teardown")
async def teardown():
    contexts.clear()
    conversations.clear()
    sent_suppression_keys.clear()
    return {"status": "wiped"}
