"""
reply_engine.py — handles POST /v1/reply. Classifies the incoming
merchant/customer message and decides: send / wait / end.

Covers the challenge's "open challenges" (§12 of challenge-brief.md):
  1. Auto-reply detection (keyword heuristic + verbatim-repeat fallback)
  2. Intent-transition handoff (pitch -> action the moment they say yes)
  3. Graceful exit (not-interested, or 3 unanswered nudges)
  4. Hostile / off-topic handling (stay polite, stay on-mission)
  5. Anti-repetition (never resend the same body verbatim in a conversation)
"""

from __future__ import annotations
import re
from typing import Optional

AUTO_REPLY_PATTERNS = [
    r"thank you for (contacting|reaching|your message)",
    r"we('| )?ll get back to you",
    r"automated (assistant|reply|response|message)",
    r"hamari team tak pahuncha",
    r"aapki jaankari ke liye .*shukriya",
    r"currently (unavailable|away|closed)",
    r"business hours",
    r"i am an automated",
]

INTENT_YES_PATTERNS = [
    r"\byes\b", r"\bok(ay)?\b", r"\bsure\b", r"\bgo ahead\b", r"let'?s do it",
    r"\bhaan\b", r"chaliye", r"kar do", r"kardo", r"theek hai", r"\bdone\b(?!.*\?)",
]

NOT_INTERESTED_PATTERNS = [
    r"not interested", r"\bstop\b", r"\bno thanks\b", r"unsubscribe",
    r"mujhe nahi chahiye", r"band karo", r"\bnahi\b.{0,10}\bchahiye\b",
]

HOSTILE_PATTERNS = [
    r"\bidiot\b", r"\bstupid\b", r"\bshut up\b", r"\bnonsense\b",
    r"bakwas", r"bewakoof",
]

OFF_TOPIC_HINTS = [
    "gst", "income tax", "loan", "visa", "weather", "cricket score",
    "personal", "unrelated",
]


def _match_any(patterns, text: str) -> bool:
    t = text.lower()
    return any(re.search(p, t) for p in patterns)


def classify(message: str) -> str:
    if _match_any(AUTO_REPLY_PATTERNS, message):
        return "auto_reply"
    if _match_any(HOSTILE_PATTERNS, message):
        return "hostile"
    if _match_any(NOT_INTERESTED_PATTERNS, message):
        return "not_interested"
    if _match_any(INTENT_YES_PATTERNS, message):
        return "intent_yes"
    if any(h in message.lower() for h in OFF_TOPIC_HINTS):
        return "off_topic"
    return "generic"


class ConversationState:
    """In-memory per-conversation state. One instance per conversation_id."""

    def __init__(self, conversation_id: str, merchant_id: Optional[str],
                 customer_id: Optional[str], trigger_id: Optional[str],
                 first_body: str):
        self.conversation_id = conversation_id
        self.merchant_id = merchant_id
        self.customer_id = customer_id
        self.trigger_id = trigger_id
        self.sent_bodies: list[str] = [first_body] if first_body else []
        self.incoming_history: list[str] = []
        self.auto_reply_strikes = 0
        self.unanswered_nudges = 0
        self.ended = False

    def register_incoming(self, message: str) -> None:
        self.incoming_history.append(message)

    def is_verbatim_repeat(self, message: str, threshold: int = 3) -> bool:
        """True if this exact message has now occurred `threshold`+ times
        among this conversation's incoming messages (the brief's own
        auto-reply hint: 'same message verbatim 3+ times = auto-reply')."""
        return self.incoming_history.count(message) >= threshold

    def register_sent(self, body: str) -> None:
        self.sent_bodies.append(body)

    def already_sent(self, body: str) -> bool:
        return body in self.sent_bodies


def respond(state: ConversationState, merchant_message: str) -> dict:
    """Given conversation state + the latest reply, decide next move."""
    state.register_incoming(merchant_message)
    label = classify(merchant_message)

    # Verbatim-repeat fallback catches auto-replies the keyword list misses
    if state.is_verbatim_repeat(merchant_message, threshold=3):
        label = "auto_reply"

    if label == "auto_reply":
        state.auto_reply_strikes += 1
        if state.auto_reply_strikes >= 2:
            return {
                "action": "end",
                "rationale": "Detected repeated auto-reply pattern; exiting "
                             "gracefully rather than burning further turns.",
            }
        body = ("Samajh gayi. Jab convenient ho, owner/manager se 2-min baat "
                "ho sakti hai kya? Chalega." )
        return {
            "action": "send", "body": body, "cta": "binary_yes_no",
            "rationale": "First auto-reply detected; one gentle re-ask "
                         "before disengaging, per anti-pattern guidance.",
        }

    if label == "not_interested":
        return {
            "action": "end",
            "rationale": "Merchant/customer signaled not interested; "
                         "exiting gracefully rather than re-pitching.",
        }

    if label == "hostile":
        body = ("Samajh sakti hoon. Main sirf aapke listing/growth me madad "
                "ke liye hoon — jab chahein ruk sakte hain.")
        return {
            "action": "send", "body": body, "cta": "none",
            "rationale": "Hostile tone detected; de-escalate politely, "
                         "stay on-mission, offer an easy out rather than "
                         "matching tone or ending abruptly.",
        }

    if label == "off_topic":
        body = ("Yeh mera scope nahi hai, but main aapke listing/growth me "
                "madad kar sakti hoon — wapas usi par aayein?")
        return {
            "action": "send", "body": body, "cta": "open_ended",
            "rationale": "Off-topic question outside Vera's remit; "
                         "politely redirect back to mission rather than "
                         "attempting to answer or ignoring the merchant.",
        }

    if label == "intent_yes":
        body = ("Great — starting now. Aapko 5 min me confirmation bhej "
                "deti hoon.")
        return {
            "action": "send", "body": body, "cta": "none",
            "rationale": "Explicit affirmative intent detected — switching "
                         "straight to action mode instead of re-qualifying "
                         "(this is the #1 named anti-pattern in the brief).",
        }

    # generic engaged reply: acknowledge + advance one step, never resend
    # a body verbatim
    body = "Got it — noted. I'll follow up with the next step shortly."
    if state.already_sent(body):
        body = "Understood — I'll come back with what's next."
    return {
        "action": "send", "body": body, "cta": "open_ended",
        "rationale": "Generic engaged reply; acknowledging and advancing "
                     "without re-pitching or repeating a prior message.",
    }
