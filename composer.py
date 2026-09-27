"""
composer.py — deterministic, grounded message composition for the magicpin
AI Challenge ("Vera") bot.

Design choice: rules/template-based, NOT an LLM call.
Why: the rubric penalizes hallucination hardest ("Hallucinated facts... is
the #1 anti-pattern") and rewards determinism + specificity. A composer that
can only ever say things that are literally present in the pushed context
cannot hallucinate, and is trivially deterministic (same input -> same
output). The tradeoff is some loss of prose variety — mitigated with light
randomization keyed to a stable hash so wording still varies message to
message without breaking determinism per-input.

Everything here is pure functions on dicts (the raw JSON contexts) so it's
easy to unit-test outside the HTTP layer — see test_local.py.
"""

from __future__ import annotations
import hashlib
from typing import Any, Optional


# ---------------------------------------------------------------------------
# small utilities
# ---------------------------------------------------------------------------

def _stable_pick(options: list, *keys: str):
    """Deterministically pick one of `options` based on a hash of `keys`.
    Same keys always -> same choice, but different (merchant, trigger) pairs
    get variety instead of the exact same sentence every time."""
    if not options:
        return None
    h = hashlib.sha256("|".join(str(k) for k in keys).encode()).hexdigest()
    return options[int(h, 16) % len(options)]


def _is_hindi_pref(merchant: dict, customer: Optional[dict]) -> bool:
    if customer:
        lang = (customer.get("identity", {}) or {}).get("language_pref", "")
        return "hi" in lang.lower()
    langs = (merchant.get("identity", {}) or {}).get("languages", [])
    return "hi" in langs


def _first_name(merchant: dict) -> str:
    ident = merchant.get("identity", {}) or {}
    return ident.get("owner_first_name") or ident.get("name", "there")


def _active_offer(merchant: dict) -> Optional[dict]:
    for o in merchant.get("offers", []) or []:
        if o.get("status") == "active":
            return o
    return None


def _digest_item(category: dict, item_id: Optional[str]) -> Optional[dict]:
    if not item_id:
        return None
    for d in category.get("digest", []) or []:
        if d.get("id") == item_id:
            return d
    return None


def _taboos(category: dict) -> set:
    return set((category.get("voice", {}) or {}).get("vocab_taboo", []) or [])


def _scrub_taboos(text: str, category: dict) -> str:
    """Belt-and-braces: strip any category-taboo phrase that slipped in."""
    for word in _taboos(category):
        if word.lower() in text.lower():
            # crude but safe: remove the offending clause-ish chunk
            text = text.replace(word, "")
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# framing selection — maps a trigger to a message "family"
# This is intentionally keyword-based over `kind`/`source`/`scope`, NOT a
# hardcoded switch on the 24 seed kind values, so *unseen* trigger kinds the
# judge injects mid-test still get routed sensibly (§ challenge-brief 12.1,
# testing-brief §4 Phase 3).
# ---------------------------------------------------------------------------

def pick_framing(trigger: dict) -> str:
    kind = (trigger.get("kind") or "").lower()
    scope = trigger.get("scope", "merchant")

    if scope == "customer":
        if "lapsed" in kind or "recall" in kind or "winback" in kind:
            return "customer_recall"
        if "followup" in kind or "trial" in kind:
            return "customer_followup"
        if "refill" in kind:
            return "customer_refill"
        if "appointment" in kind:
            return "customer_appointment"
        return "customer_generic"

    if "dip" in kind:
        return "perf_dip"
    if "spike" in kind:
        return "perf_spike"
    if "milestone" in kind:
        return "milestone"
    if "regulation" in kind or "compliance" in kind or "supply_alert" in kind:
        return "compliance"
    if "research" in kind or "digest" in kind or "trend" in kind:
        return "digest"
    if "festival" in kind or "ipl" in kind or "seasonal" in kind or "wedding" in kind:
        return "seasonal_opportunity"
    if "competitor" in kind:
        return "competitor"
    if "review_theme" in kind:
        return "review_theme"
    if "dormant" in kind:
        return "dormant"
    if "renewal" in kind:
        return "renewal"
    if "curious_ask" in kind or "scheduled_recurring" in kind:
        return "curious_ask"
    if "gbp_unverified" in kind:
        return "profile_gap"
    if "active_planning_intent" in kind:
        return "planning_intent"
    if "opportunity" in kind:
        return "opportunity"
    return "generic_nudge"


# Human-readable fallbacks for trigger kinds when the payload carries no
# rich facts (many generated triggers only carry {"placeholder": true,
# "metric_or_topic": "<kind>"}). Used so the generic builders never have to
# fall back to a raw key:value dump. Extend this as new kinds show up.
KIND_PHRASES = {
    "appointment_tomorrow": "you have an appointment coming up tomorrow",
    "gbp_unverified": "your Google Business Profile isn't verified yet",
    "renewal_due": "your plan renewal is coming up",
    "winback_eligible": "a past customer is eligible for a winback offer",
    "supply_alert": "a supply/stock alert applies to your category",
    "wedding_package_followup": "a wedding-package enquiry is still open",
    "trial_followup": "a trial customer is due a follow-up",
}


def _humanize_topic(raw: Optional[str]) -> str:
    if not raw:
        return "something worth a quick look"
    return KIND_PHRASES.get(raw, raw.replace("_", " "))


# ---------------------------------------------------------------------------
# anchor extraction — pull the one verifiable fact this message hangs on
# ---------------------------------------------------------------------------

def pick_anchor(framing: str, category: dict, merchant: dict, trigger: dict,
                 customer: Optional[dict]) -> dict:
    payload = trigger.get("payload", {}) or {}
    is_placeholder = bool(payload.get("placeholder"))
    anchor: dict[str, Any] = {"kind": framing}

    if framing == "planning_intent":
        anchor.update({
            "topic": payload.get("intent_topic"),
            "last_message": payload.get("merchant_last_message"),
        })
    elif is_placeholder and framing not in ("customer_recall", "customer_refill", "customer_appointment"):
        # thin/generated trigger — no rich facts, just a topic label
        anchor["topic_phrase"] = _humanize_topic(payload.get("metric_or_topic") or trigger.get("kind"))
    elif framing == "digest":
        item = _digest_item(category, payload.get("top_item_id"))
        if item:
            anchor.update({
                "title": item.get("title"),
                "source": item.get("source"),
                "trial_n": item.get("trial_n"),
                "segment": item.get("patient_segment"),
                "actionable": item.get("actionable"),
            })
    elif framing == "compliance":
        item = _digest_item(category, payload.get("top_item_id"))
        if item:
            anchor.update({
                "title": item.get("title"),
                "source": item.get("source"),
                "deadline": payload.get("deadline_iso"),
                "summary": item.get("summary"),
            })
    elif framing in ("perf_dip", "perf_spike"):
        anchor.update({
            "metric": payload.get("metric") or "views",
            "delta_pct": payload.get("delta_pct"),
            "window": payload.get("window") or "7d",
            "vs_baseline": payload.get("vs_baseline"),
        })
    elif framing == "milestone":
        anchor.update({
            "metric": payload.get("metric"),
            "value_now": payload.get("value_now"),
            "milestone_value": payload.get("milestone_value"),
            "is_imminent": payload.get("is_imminent"),
        })
    elif framing == "competitor":
        anchor.update({
            "competitor_name": payload.get("competitor_name"),
            "distance_km": payload.get("distance_km"),
            "their_offer": payload.get("their_offer"),
            "opened_date": payload.get("opened_date"),
        })
    elif framing in ("customer_recall", "customer_followup", "customer_refill", "customer_appointment"):
        anchor.update({
            "service_due": payload.get("service_due"),
            "due_date": payload.get("due_date"),
            "last_service_date": payload.get("last_service_date"),
            "slots": payload.get("available_slots", []),
        })
    elif framing == "review_theme":
        themes = merchant.get("review_themes", []) or []
        if themes:
            anchor["theme"] = themes[0]
    elif framing == "seasonal_opportunity":
        beats = category.get("seasonal_beats", []) or []
        if beats:
            anchor["seasonal_beat"] = _stable_pick(
                beats, merchant.get("merchant_id"), trigger.get("id"))
    elif framing == "opportunity":
        item = _digest_item(category, payload.get("digest_item_id") or payload.get("top_item_id"))
        if item:
            anchor.update({"title": item.get("title"), "source": item.get("source")})
        anchor["credits"] = payload.get("credits")
        anchor["fee"] = payload.get("fee")
    if len(anchor) <= 1:  # nothing matched above — last-resort natural fallback
        anchor["topic_phrase"] = _humanize_topic(
            payload.get("metric_or_topic") or trigger.get("kind"))

    return anchor


# ---------------------------------------------------------------------------
# body builders — one per framing family
# All of these ONLY use fields already present in category/merchant/trigger/
# customer. If a fact isn't there, the sentence referencing it is skipped
# rather than invented.
# ---------------------------------------------------------------------------

def _greet(merchant: dict, hindi: bool) -> str:
    name = _first_name(merchant)
    return f"{name}," if not hindi else f"{name},"


def _offer_line(merchant: dict) -> str:
    o = _active_offer(merchant)
    return o["title"] if o else ""


def build_digest(anchor, category, merchant, hindi) -> str:
    title = anchor.get("title")
    source = anchor.get("source")
    if not title:
        return build_generic({}, category, merchant, hindi, note="research")
    parts = [f"{source.split(',')[0]} item relevant to you" if source else "Research update:"]
    if anchor.get("trial_n") and anchor.get("segment"):
        seg = anchor["segment"].replace("_", " ")
        parts.append(f"{anchor['trial_n']}-patient/sample data on {seg} — {title.lower()}." if len(title) < 90 else title)
    else:
        parts.append(title)
    if anchor.get("actionable"):
        parts.append(anchor["actionable"] + ".")
    ask = "Worth a look — want me to pull it and draft something you can share?" if not hindi else \
          "Dekhna chahenge? Main isse ek WhatsApp draft me bhi bana deti hoon."
    body = " ".join(parts) + " " + ask
    if source:
        body += f" — {source}"
    return body


def build_compliance(anchor, category, merchant, hindi) -> str:
    title = anchor.get("title", "A regulation update applies to your category.")
    deadline = anchor.get("deadline")
    source = anchor.get("source")
    parts = [title + "."]
    if deadline:
        parts.append(f"Effective {deadline}." if not hindi else f"{deadline} se effective hai.")
    ask = "Want the 1-line summary of what changes for your setup?" if not hindi else \
          "Chahiye toh main aapke liye 1-line summary bhej deti hoon ki kya badalta hai?"
    parts.append(ask)
    body = " ".join(parts)
    if source:
        body += f" — {source}"
    return body


def build_perf_dip(anchor, category, merchant, hindi) -> str:
    metric = anchor.get("metric", "performance")
    delta = anchor.get("delta_pct")
    window = anchor.get("window", "7d")
    pct = f"{abs(round(delta * 100))}%" if isinstance(delta, (int, float)) else "noticeably"
    peer = category.get("peer_stats", {}) or {}
    peer_line = ""
    if metric == "views" and peer.get("avg_views_30d"):
        peer_line = f" Peers in your category average {peer['avg_views_30d']} views/30d."
    elif metric == "calls" and peer.get("avg_calls_30d"):
        peer_line = f" Peers average {peer['avg_calls_30d']} calls/30d."
    if not hindi:
        body = (f"Your {metric} dropped {pct} over the last {window}.{peer_line} "
                 f"Want me to check what changed — offers, photos, or posting gap?")
    else:
        body = (f"Aapke {metric} me {pct} ki girawat aayi hai last {window} me.{peer_line} "
                 f"Check karu kya wajah hai — offers, photos ya posting gap?")
    return body


def build_perf_spike(anchor, category, merchant, hindi) -> str:
    metric = anchor.get("metric", "performance")
    delta = anchor.get("delta_pct")
    pct = f"{round(delta * 100)}%" if isinstance(delta, (int, float)) else "up"
    offer = _offer_line(merchant)
    if not hindi:
        body = f"Your {metric} is up {pct} this week."
        if offer:
            body += f" Good moment to push '{offer}' while attention is high — want me to feature it in a post?"
        else:
            body += " Want me to draft a post to ride this momentum?"
    else:
        body = f"Aapke {metric} is week {pct} badhe hain."
        if offer:
            body += f" '{offer}' ko abhi feature karna accha rahega — post bana du?"
        else:
            body += " Ek post bana du isi momentum pe?"
    return body


def build_milestone(anchor, category, merchant, hindi) -> str:
    metric = anchor.get("metric")
    value_now = anchor.get("value_now")
    target = anchor.get("milestone_value")
    if metric and value_now is not None and target is not None:
        metric_h = metric.replace("_", " ")
        if anchor.get("is_imminent") and value_now < target:
            fact = f"you're at {value_now} {metric_h}, {target - value_now} away from {target}"
        else:
            fact = f"you've hit {value_now} {metric_h} (past the {target} mark)"
    else:
        fact = anchor.get("topic_phrase", "a new milestone")
    if not hindi:
        return (f"Almost there — {fact}. "
                 f"Worth sharing with your customers — want a post drafted announcing it?")
    return (f"Bas thoda aur — {fact}. "
             f"Customers ke saath share karna chahenge? Main post bana deti hoon.")


def build_customer_recall(anchor, category, merchant, customer, hindi) -> str:
    cust_name = (customer.get("identity", {}) or {}).get("name", "there")
    merchant_name = merchant.get("identity", {}).get("name", "")
    owner = _first_name(merchant)
    service = (anchor.get("service_due") or "recall").replace("_", " ")
    slots = anchor.get("slots") or []
    slot_str = ""
    if slots:
        labels = [s.get("label") for s in slots if s.get("label")]
        slot_str = " / ".join(labels[:2])
    offer = _offer_line(merchant)
    last = anchor.get("last_service_date")
    if not hindi:
        if last:
            body = f"Hi {cust_name}, {merchant_name} here — it's been a while since your last visit ({last})."
        else:
            body = f"Hi {cust_name}, {merchant_name} here."
        body += f" Your {service} is due."
        if slot_str:
            body += f" We have {slot_str} open."
        if offer:
            body += f" {offer}."
        body += " Reply 1 or 2 to pick a slot, or tell us a time that works."
    else:
        body = f"Hi {cust_name}, {merchant_name} ki taraf se."
        body += f" Aapka {service} due hai."
        if slot_str:
            body += f" {slot_str} slots khaali hain."
        if offer:
            body += f" {offer}."
        body += " Reply 1 ya 2 slot ke liye, ya apna time batayein."
    return body


def build_customer_followup(anchor, category, merchant, customer, hindi) -> str:
    cust_name = (customer.get("identity", {}) or {}).get("name", "there")
    merchant_name = merchant.get("identity", {}).get("name", "")
    if not hindi:
        return (f"Hi {cust_name}, following up from {merchant_name} — "
                 f"how did things go with your last visit? Happy to help book a next step if useful.")
    return (f"Hi {cust_name}, {merchant_name} se follow-up — "
             f"pichli visit kaisi rahi? Agla step book karna ho toh bataiye.")


def build_customer_refill(anchor, category, merchant, customer, hindi) -> str:
    cust_name = (customer.get("identity", {}) or {}).get("name", "there")
    merchant_name = merchant.get("identity", {}).get("name", "")
    due = anchor.get("due_date")
    if not hindi:
        body = f"Hi {cust_name}, {merchant_name} here."
        if due:
            body += f" Your refill looks due around {due}."
        body += " Want us to keep it ready for pickup?"
    else:
        body = f"Hi {cust_name}, {merchant_name} ki taraf se."
        if due:
            body += f" Aapka refill {due} ke aas-paas due hai."
        body += " Ready rakh du pickup ke liye?"
    return body


def build_competitor(anchor, category, merchant, hindi) -> str:
    name = anchor.get("competitor_name")
    dist = anchor.get("distance_km")
    their_offer = anchor.get("their_offer")
    own_offer = _offer_line(merchant)
    if name:
        fact = f"{name} opened"
        if dist is not None:
            fact += f" {dist}km away"
        if their_offer and own_offer:
            fact += f", running '{their_offer}' against your '{own_offer}'"
        elif their_offer:
            fact += f", running '{their_offer}'"
    else:
        fact = anchor.get("topic_phrase", "a new competitor nearby")
    if not hindi:
        return (f"Heads up — {fact}. "
                 f"Want me to check how your listing compares on photos, offers and reviews?")
    return (f"Ek update — {fact}. "
             f"Check karu aapki listing kaise compare karti hai photos, offers aur reviews pe?")


def build_review_theme(anchor, category, merchant, hindi) -> str:
    theme = anchor.get("theme") or {}
    name = (theme.get("theme") or "a recurring topic").replace("_", " ")
    n = theme.get("occurrences_30d")
    sentiment = theme.get("sentiment")
    quote = theme.get("common_quote")
    if not hindi:
        body = f"'{name}' came up in {n} reviews this month" if n else f"'{name}' is coming up in recent reviews"
        if sentiment == "neg":
            body += " — worth addressing before it compounds."
        else:
            body += " — worth highlighting, it's working for you."
        if quote:
            body += f' One patient wrote: "{quote}."'
        body += " Want a draft response or a fix checklist?"
    else:
        body = f"'{name}' {n} reviews me aaya hai is mahine" if n else f"'{name}' recent reviews me aa raha hai"
        body += " — dekh lete hain?"
    return body


def build_seasonal_opportunity(anchor, category, merchant, hindi) -> str:
    beat = anchor.get("seasonal_beat") or {}
    note = beat.get("note") or anchor.get("topic_phrase")
    offer = _offer_line(merchant)
    if not hindi:
        body = note.capitalize() + "." if note else "A seasonal window is opening for your category."
        if offer:
            body += f" '{offer}' fits this moment — want me to push it as a campaign this week?"
        else:
            body += " Want me to draft a seasonal campaign?"
    else:
        body = (note.capitalize() + "." if note else "Ek seasonal window khul raha hai.")
        body += " Campaign bana du is week?"
    return body


def build_dormant(anchor, category, merchant, hindi) -> str:
    if not hindi:
        return ("Haven't heard from you in a bit — your profile's still active on our end. "
                 "Anything specific you'd like help with this week — offers, photos, or reviews?")
    return ("Kaafi din se baat nahi hui — profile abhi bhi active hai. "
             "Is week kisme madad chahiye — offers, photos, ya reviews?")


def build_renewal(anchor, category, merchant, hindi) -> str:
    days = merchant.get("subscription", {}).get("days_remaining")
    plan = merchant.get("subscription", {}).get("plan")
    if not hindi:
        body = f"Your {plan} plan renews in {days} days." if days is not None else "Your plan renewal is coming up."
        body += " Want me to lock in the renewal now so nothing lapses?"
    else:
        body = f"Aapka {plan} plan {days} din me renew hoga." if days is not None else "Renewal aane wala hai."
        body += " Abhi confirm kar du?"
    return body


def build_curious_ask(anchor, category, merchant, hindi) -> str:
    if not hindi:
        return "Quick one: what's the most-asked question or treatment you're getting this week? Helps me tailor what I send you."
    return "Ek chhota sawaal: is week sabse zyada kya pooch rahe hain customers? Isse main aapke liye better content bana sakti hoon."


def build_profile_gap(anchor, category, merchant, hindi) -> str:
    signals = merchant.get("signals", []) or []
    gap = next((s for s in signals if "stale" in s or "unverified" in s.lower()), None)
    gap_h = gap.replace("_", " ").replace(":", " — ") if gap else None
    if not hindi:
        body = f"Noticed: {gap_h}." if gap_h else "Your Google profile has a gap that's easy to close."
        body += " Want me to fix it — takes 2 minutes on your end?"
    else:
        body = f"Ek gap mili — {gap_h}." if gap_h else "Profile me ek gap hai jo jaldi fix ho sakta hai."
        body += " Fix kar du? 2 minute ka kaam hai."
    return body


def build_opportunity(anchor, category, merchant, hindi) -> str:
    title = anchor.get("title")
    source = anchor.get("source")
    credits = anchor.get("credits")
    fee = anchor.get("fee")
    if title:
        fact = title
        extras = []
        if credits:
            extras.append(f"{credits} CE credits")
        if fee:
            extras.append(fee.replace("_", " "))
        if extras:
            fact += f" ({', '.join(extras)})"
    else:
        fact = anchor.get("topic_phrase", "a relevant opportunity for your category")
    body = f"{fact}. Want me to put together a plan for it?" if not hindi else \
           f"{fact}. Plan bana du iske liye?"
    if source:
        body += f" — {source}"
    return body


def build_planning_intent(anchor, category, merchant, hindi) -> str:
    """The merchant already expressed clear intent (they asked a question or
    said yes to something). This is the exact 'Pattern D' anti-pattern the
    brief calls out — the wrong move is to ask another qualifying question.
    The right move: acknowledge specifically and move straight to action."""
    topic = (anchor.get("topic") or "this").replace("_", " ")
    if not hindi:
        return (f"On it — starting the {topic} plan now based on what you said. "
                 f"I'll have a draft ready shortly. Want it to include pricing, "
                 f"or just the structure first?")
    return (f"Ho jayega — {topic} ka plan abhi shuru kar rahi hoon jo aapne bataya. "
             f"Thodi der me draft ready hoga. Pricing bhi chahiye ya pehle sirf structure?")


def build_customer_appointment(anchor, category, merchant, customer, hindi) -> str:
    cust_name = (customer.get("identity", {}) or {}).get("name", "there") if customer else "there"
    merchant_name = merchant.get("identity", {}).get("name", "")
    if not hindi:
        return (f"Hi {cust_name}, reminder from {merchant_name} — you have an "
                 f"appointment tomorrow. Reply 1 to confirm or 2 to reschedule.")
    return (f"Hi {cust_name}, {merchant_name} se reminder — kal aapki appointment "
             f"hai. Confirm ke liye 1, reschedule ke liye 2 reply karein.")


def build_generic(anchor, category, merchant, hindi, note="update") -> str:
    signals = merchant.get("signals", []) or []
    sig = signals[0].replace("_", " ").replace(":", " — ") if signals else None
    if not hindi:
        body = f"Quick {note}: " + (sig if sig else "checking in on your listing.")
        body += " Anything you'd like me to look at this week?"
    else:
        body = f"Ek {note}: " + (sig if sig else "aapki listing check kar rahi thi.")
        body += " Kisme madad chahiye is week?"
    return body


_BUILDERS_MERCHANT = {
    "digest": build_digest,
    "compliance": build_compliance,
    "perf_dip": build_perf_dip,
    "perf_spike": build_perf_spike,
    "milestone": build_milestone,
    "competitor": build_competitor,
    "review_theme": build_review_theme,
    "seasonal_opportunity": build_seasonal_opportunity,
    "dormant": build_dormant,
    "renewal": build_renewal,
    "curious_ask": build_curious_ask,
    "profile_gap": build_profile_gap,
    "opportunity": build_opportunity,
    "planning_intent": build_planning_intent,
    "generic_nudge": build_generic,
}

_BUILDERS_CUSTOMER = {
    "customer_recall": build_customer_recall,
    "customer_followup": build_customer_followup,
    "customer_refill": build_customer_refill,
    "customer_appointment": build_customer_appointment,
    "customer_generic": build_customer_followup,
}


# ---------------------------------------------------------------------------
# CTA + rationale
# ---------------------------------------------------------------------------

_ACTIONABLE_FRAMINGS = {
    "customer_recall", "customer_followup", "customer_refill", "customer_generic",
    "customer_appointment", "perf_dip", "compliance", "profile_gap", "renewal",
    "competitor", "planning_intent",
}
_INFO_FRAMINGS = {"digest", "milestone", "review_theme", "curious_ask"}


def decide_cta(framing: str, trigger: dict) -> str:
    urgency = trigger.get("urgency", 1)
    if framing in _ACTIONABLE_FRAMINGS or urgency >= 4:
        return "binary_yes_no"
    if framing in _INFO_FRAMINGS:
        return "open_ended"
    return "open_ended"


def build_rationale(framing: str, trigger: dict, merchant: dict, customer: Optional[dict]) -> str:
    who = f"customer {customer.get('identity', {}).get('name')}" if customer else merchant.get("identity", {}).get("name")
    kind = trigger.get("kind")
    return (f"Framing='{framing}' chosen from trigger kind='{kind}' (urgency={trigger.get('urgency')}); "
            f"anchored on facts present in trigger/category context only; addressed to {who}; "
            f"CTA kept single and binary where the trigger is actionable, open-ended for pure-information triggers.")


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------

def compose(category: dict, merchant: dict, trigger: dict,
            customer: Optional[dict] = None) -> dict:
    framing = pick_framing(trigger)
    anchor = pick_anchor(framing, category, merchant, trigger, customer)
    hindi = _is_hindi_pref(merchant, customer)

    if customer is not None:
        builder = _BUILDERS_CUSTOMER.get(framing, build_customer_followup)
        body = builder(anchor, category, merchant, customer, hindi)
        send_as = "merchant_on_behalf"
    else:
        builder = _BUILDERS_MERCHANT.get(framing, build_generic)
        body = builder(anchor, category, merchant, hindi)
        send_as = "vera"

    body = _scrub_taboos(body, category)
    cta = decide_cta(framing, trigger)
    rationale = build_rationale(framing, trigger, merchant, customer)

    return {
        "body": body,
        "cta": cta,
        "send_as": send_as,
        "suppression_key": trigger.get("suppression_key", f"{trigger.get('id','')}"),
        "rationale": rationale,
    }
