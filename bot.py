"""
Vera Message Engine — magicpin AI Challenge Submission
======================================================
Team: Bishwjit Kumar
Model: gemini-2.5-pro
Approach: 4-context composition with trigger-kind routing, auto-reply detection,
          intent-aware multi-turn state, and suppression dedup.

Run: uvicorn bot:app --host 0.0.0.0 --port 8080
"""

import os, time, json, re, uuid, httpx, asyncio
from datetime import datetime, timezone
from typing import Any, Literal
from fastapi import FastAPI
from pydantic import BaseModel

# ─── CONFIG ────────────────────────────────────────────────────────────────────
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL    = os.getenv("GEMINI_MODEL", "gemini-2.5-pro")
VERSION           = "1.0.0"
TEAM_NAME         = "Bishwjit Kumar"

app = FastAPI(title="Vera Message Engine")
START = time.time()

# ─── IN-MEMORY STATE ───────────────────────────────────────────────────────────
# contexts[(scope, context_id)] = {version, payload}
contexts: dict[tuple[str, str], dict] = {}

# conversations[conv_id] = {merchant_id, customer_id, turns, sent_bodies}
conversations: dict[str, dict] = {}

# global suppression: suppression_key → True (already sent)
sent_suppressions: set[str] = set()


# ─── UTILITIES ─────────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def get_ctx(scope: str, ctx_id: str) -> dict | None:
    entry = contexts.get((scope, ctx_id))
    return entry["payload"] if entry else None

def detect_auto_reply(message: str, conv_history: list[dict]) -> bool:
    """Return True if this looks like a WhatsApp Business auto-reply."""
    AUTO_PHRASES = [
        "thank you for contacting",
        "thanks for reaching out",
        "we will get back to you",
        "our team will respond",
        "this is an automated",
        "auto reply",
        "outside business hours",
    ]
    msg_lower = message.lower()
    if any(phrase in msg_lower for phrase in AUTO_PHRASES):
        return True
    # Same message verbatim 3+ times from this party
    merchant_msgs = [t["msg"] for t in conv_history if t.get("from") == "merchant"]
    if merchant_msgs.count(message) >= 2:   # including current → 3 total
        return True
    return False

def detect_intent(message: str) -> Literal["accept", "reject", "hostile", "question", "neutral"]:
    """Classify merchant intent from reply."""
    msg_lower = message.lower().strip()

    HOSTILE = ["stop messaging", "stop contacting", "spam", "block", "useless",
               "don't message", "mat karo", "band karo", "chhodo"]
    # Explicit negation phrases checked FIRST — prevent "nahi chahiye" matching accept
    REJECT_PHRASES = ["not interested", "don't need", "nahi chahiye", "nahi hai",
                      "mat bhejo", "nahi lena", "abhi nahi", "dekh lenge", "baad mein"]
    REJECT_WORDS   = ["nahi", "nope", "later", "no thanks"]
    ACCEPT_PHRASES = ["go ahead", "haan please", "yes please", "zaroor karo",
                      "judrna hai", "subscribe karo", "send karo"]
    ACCEPT_WORDS   = ["yes", "haan", "sure", "proceed", "judrna", "join",
                      "subscribe", "confirm", "karein", "zaroor", "bilkul",
                      "share", "chahiye", "le lo", "send", "done", "please"]

    if any(w in msg_lower for w in HOSTILE):
        return "hostile"
    # Negation phrases before single words
    if any(p in msg_lower for p in REJECT_PHRASES):
        return "reject"
    if any(p in msg_lower for p in ACCEPT_PHRASES):
        return "accept"
    # Guard: if "nahi" present, don't flip to accept even if accept word follows
    has_negation = any(w in msg_lower for w in ["nahi", "no", "not", "nope", "mat"])
    if has_negation and any(w in msg_lower for w in REJECT_WORDS):
        return "reject"
    if not has_negation and any(w in msg_lower for w in ACCEPT_WORDS):
        return "accept"
    if any(w in msg_lower for w in REJECT_WORDS):
        return "reject"
    if "?" in message:
        return "question"
    return "neutral"


# ─── GEMINI COMPOSER ───────────────────────────────────────────────────────────

async def call_gemini(system: str, user: str, max_tokens: int = 600) -> str:
    """Make a single Gemini API call. Returns the text response."""
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    payload = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"parts": [{"text": user}]}],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": max_tokens,
        },
    }
    async with httpx.AsyncClient(timeout=25) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()


def _language_instruction(merchant: dict) -> str:
    langs = merchant.get("identity", {}).get("languages", ["en"])
    if "hi" in langs and "en" in langs:
        return "Use natural Hindi-English code-mix (Hinglish). Example: 'Dr. Meera, ek kaam ki baat — aapke CTR peers se kaafi neeche hai.'"
    elif "hi" in langs:
        return "Reply in Hindi."
    return "Reply in English."

def _voice_instruction(category: dict) -> str:
    voice = category.get("voice", {})
    tone = voice.get("tone", "professional")
    taboos = voice.get("taboos", [])
    taboo_str = f" Avoid these words: {', '.join(taboos)}." if taboos else ""
    return f"Tone: {tone}.{taboo_str}"

def _build_composer_system(category: dict, merchant: dict,
                            trigger: dict, customer: dict | None) -> str:
    """Build the full system prompt for the engagement composer."""
    lang = _language_instruction(merchant)
    voice = _voice_instruction(category)

    offers_str = json.dumps(merchant.get("offers", []), ensure_ascii=False)
    perf = merchant.get("performance", {})
    sigs = ", ".join(merchant.get("signals", []))
    peer_ctr = category.get("peer_stats", {}).get("avg_ctr", "N/A")
    merchant_ctr = perf.get("ctr", "N/A")
    name = merchant.get("identity", {}).get("name", "the merchant")
    locality = merchant.get("identity", {}).get("locality", "")
    subs = merchant.get("subscription", {})
    conv_hist = merchant.get("conversation_history", {})

    cat_section = f"""
CATEGORY CONTEXT ({category.get('slug', 'unknown')}):
- Voice: {voice}
- Offer catalog: {json.dumps(category.get('offer_catalog', []), ensure_ascii=False)}
- Peer stats: {json.dumps(category.get('peer_stats', {}), ensure_ascii=False)}
- Seasonal beats: {json.dumps(category.get('seasonal_beats', []), ensure_ascii=False)}
- Trend signals: {json.dumps(category.get('trend_signals', []), ensure_ascii=False)}
- Recent digest: {json.dumps(category.get('digest', [])[:3], ensure_ascii=False)}
""".strip()

    merchant_section = f"""
MERCHANT CONTEXT:
- Name: {name}, Locality: {locality}
- Subscription: {json.dumps(subs, ensure_ascii=False)}
- Performance (30d): views={perf.get('views_30d','?')}, calls={perf.get('calls_30d','?')}, CTR={merchant_ctr} (peer median={peer_ctr})
- Active offers: {offers_str}
- Derived signals: {sigs if sigs else 'none'}
- Customer aggregate: {json.dumps(merchant.get('customer_aggregate', {}), ensure_ascii=False)}
- Recent conversation: {json.dumps(conv_hist, ensure_ascii=False)}
""".strip()

    trigger_section = f"""
TRIGGER:
- Kind: {trigger.get('kind')}
- Scope: {trigger.get('scope')}
- Source: {trigger.get('source')}
- Urgency: {trigger.get('urgency')}/5
- Payload: {json.dumps(trigger.get('payload', {}), ensure_ascii=False)}
""".strip()

    customer_section = ""
    if customer:
        customer_section = f"""
CUSTOMER CONTEXT:
- Name: {customer.get('identity', {}).get('name', '?')}
- State: {customer.get('state', '?')}
- Relationship: {json.dumps(customer.get('relationship', {}), ensure_ascii=False)}
- Preferences: {json.dumps(customer.get('preferences', {}), ensure_ascii=False)}
""".strip()

    return f"""You are Vera, magicpin's AI assistant composing a WhatsApp message to a merchant (or their customer).

=== INPUTS ===
{cat_section}

{merchant_section}

{trigger_section}
{customer_section}

=== OUTPUT RULES ===
1. {lang}
2. Max 3 sentences + 1 CTA. Total < 160 words.
3. No long preamble ("I hope you're doing well..."). Start sharp.
4. CTA must be single, binary, and last: "Reply YES", "Reply 1/2", or one open question.
5. Use ONE compulsion lever: specificity (cite real numbers/source), social proof, loss aversion, curiosity, or reciprocity.
6. Prefer service+price offers ("Dental Cleaning @ ₹299") over generic discounts ("Flat 20% OFF").
7. Never hallucinate data not in the inputs above.
8. Never re-introduce yourself after turn 1.
9. Do NOT use multiple CTAs in one message.
10. For regulated categories (dentists, doctors, lawyers): use peer/clinical tone — no hype or exclamation spam.

You must reply with ONLY the WhatsApp message body. No preamble, no "Here's the message:", no quotes."""


def _get_trigger_kind_hint(kind: str) -> str:
    """Additional prompt instruction based on trigger kind."""
    hints = {
        "research_digest": "Open with the specific finding, cite source. Frame it as relevant to THIS merchant's patient/customer cohort. End with offer to pull abstract + draft patient-ed content.",
        "recall_due": "Address the customer by name. State exactly how long it's been since their last visit. Offer 2 concrete time slots. Keep it warm, not clinical.",
        "perf_spike": "Lead with the specific number spike. Attribute it to recent activity if possible. Suggest capitalizing with a post or offer. Urgency: ride the momentum now.",
        "perf_dip": "Acknowledge the dip with a specific number. Propose one concrete fix (e.g., reactivate a paused offer, post today). No doom — solutions only.",
        "milestone_reached": "Celebrate the milestone briefly. Then pivot to the next action: request a Google post, thank recent reviewers, or suggest the next offer.",
        "competitor_opened": "Use voyeur curiosity. 'A new [category] opened [distance] away — want to see how you compare?' Don't name the competitor unless in payload.",
        "festival_upcoming": "Name the festival and date. Tie it to a specific offer or action. Urgency: few days left.",
        "dormant_with_vera": "Re-engage lightly. One useful piece of intel from the context, not a sales pitch.",
        "review_theme_emerged": "Name the theme from reviews. Frame as an insight. Suggest one quick response or action.",
        "weather_heatwave": "Tie the weather event to a relevant service or seasonal offer. Specific temperature if in payload.",
        "customer_lapsed_soft": "Warm re-engagement. Reference how long it's been. Offer an easy re-booking path.",
        "scheduled_recurring": "Pick the most interesting signal from context. Curious-ask style: 'Want to know...?' or 'Quick question for you—'",
    }
    return hints.get(kind, "Focus on the most actionable insight from the trigger payload.")


async def compose_message(
    category: dict, merchant: dict, trigger: dict, customer: dict | None,
    prior_body: str = ""
) -> tuple[str, str]:
    """
    Compose a message via Claude.
    Returns (body, rationale).
    """
    system = _build_composer_system(category, merchant, trigger, customer)
    kind = trigger.get("kind", "")
    kind_hint = _get_trigger_kind_hint(kind)
    prior_note = f"\n\nIMPORTANT: Do NOT send this verbatim again: '{prior_body[:100]}'" if prior_body else ""

    user_prompt = f"""Compose a WhatsApp message for trigger kind: {kind}

Specific framing guidance for this kind: {kind_hint}

Remember: One compulsion lever. One CTA at the end. No preamble.{prior_note}

Write the message now:"""

    body = await call_gemini(system, user_prompt, max_tokens=350)

    # Generate rationale
    rationale = (
        f"Trigger: {kind} (urgency {trigger.get('urgency', '?')}). "
        f"Merchant: {merchant.get('identity', {}).get('name', '?')} | "
        f"Signals: {', '.join(merchant.get('signals', [])) or 'none'}. "
        f"Category: {category.get('slug', '?')} | "
        f"Composer: {GEMINI_MODEL}."
    )
    return body, rationale


async def compose_reply(
    merchant: dict, category: dict, trigger: dict | None,
    merchant_message: str, conv_history: list[dict], intent: str
) -> tuple[str, str, str]:
    """
    Compose a reply to a merchant turn.
    Returns (action, body, rationale).
    """
    if intent == "hostile":
        return "end", "", "Merchant expressed hostility — gracefully exiting."

    name = merchant.get("identity", {}).get("name", "the merchant")
    lang = _language_instruction(merchant)
    voice = _voice_instruction(category)
    hist_str = json.dumps(conv_history[-6:], ensure_ascii=False)
    trigger_str = json.dumps(trigger or {}, ensure_ascii=False)

    system = f"""You are Vera, magicpin's AI merchant assistant. You are in an ongoing WhatsApp conversation.

Merchant: {name}
Category: {category.get('slug', 'unknown')} — Voice: {voice}
Merchant intent: {intent}
Conversation so far: {hist_str}
Active trigger context: {trigger_str}

{lang}
Rules:
- If intent=accept: switch immediately to ACTION. Say 'Sending now…' or 'Done, here's what's next…'. Never ask qualifying questions after acceptance.
- If intent=reject: acknowledge briefly, leave door open with one soft future hook. Keep < 2 sentences.
- If intent=question: answer directly, then re-state CTA.
- If intent=neutral: advance the conversation one step. Stay concise.
- Max 120 words. Single CTA if applicable."""

    user_prompt = f"""Merchant just said: "{merchant_message}"

Decide: send | wait | end
If send: write the reply body only.
If wait or end: just output the word.

Output format:
ACTION: send|wait|end
BODY: <reply text if send, else empty>"""

    raw = await call_gemini(system, user_prompt, max_tokens=300)

    # Parse output
    action = "send"
    body = ""
    for line in raw.splitlines():
        if line.upper().startswith("ACTION:"):
            action = line.split(":", 1)[-1].strip().lower()
        elif line.upper().startswith("BODY:"):
            body = line.split(":", 1)[-1].strip()

    # Fallback if Claude didn't follow format
    if not action or action not in ("send", "wait", "end"):
        action = "send"
    if action == "send" and not body:
        body = raw.replace(f"ACTION: {action}", "").replace("BODY:", "").strip()

    rationale = f"Merchant intent detected: {intent}. Action: {action}."
    return action, body, rationale


# ─── REQUEST MODELS ────────────────────────────────────────────────────────────

class CtxBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: str

class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = []

class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: str | None = None
    customer_id: str | None = None
    from_role: str
    message: str
    received_at: str
    turn_number: int


# ─── ENDPOINTS ─────────────────────────────────────────────────────────────────

@app.get("/v1/healthz")
async def healthz():
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for (scope, _) in contexts:
        if scope in counts:
            counts[scope] += 1
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START),
        "contexts_loaded": counts,
    }


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": TEAM_NAME,
        "team_members": ["Bishwjit Kumar"],
        "model": GEMINI_MODEL,
        "approach": (
            "4-context composer (CategoryContext + MerchantContext + TriggerContext + CustomerContext) "
            "with trigger-kind routing, auto-reply detection, intent-aware multi-turn FSM, "
            "suppression dedup, and language-adaptive output via Claude."
        ),
        "contact_email": "bishwjit.kumar@example.com",
        "version": VERSION,
        "submitted_at": now_iso(),
    }


@app.post("/v1/context")
async def push_context(body: CtxBody):
    if body.scope not in ("category", "merchant", "customer", "trigger"):
        return {"accepted": False, "reason": "invalid_scope",
                "details": f"Unknown scope: {body.scope}"}

    key = (body.scope, body.context_id)
    cur = contexts.get(key)
    if cur and cur["version"] >= body.version:
        return {"accepted": False, "reason": "stale_version",
                "current_version": cur["version"]}

    contexts[key] = {"version": body.version, "payload": body.payload}
    return {
        "accepted": True,
        "ack_id": f"ack_{body.context_id}_v{body.version}",
        "stored_at": now_iso(),
    }


@app.post("/v1/tick")
async def tick(body: TickBody):
    actions = []
    # Deduplicate: one action per merchant per tick
    merchants_this_tick: set[str] = set()

    for trg_id in body.available_triggers:
        trg = get_ctx("trigger", trg_id)
        if not trg:
            continue

        # Skip expired triggers
        expires_at = trg.get("expires_at")
        if expires_at:
            try:
                exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                if datetime.now(timezone.utc) > exp:
                    continue
            except Exception:
                pass

        # Suppression check
        sup_key = trg.get("suppression_key", trg_id)
        if sup_key in sent_suppressions:
            continue

        merchant_id = trg.get("merchant_id") or trg.get("payload", {}).get("merchant_id")
        if not merchant_id or merchant_id in merchants_this_tick:
            continue

        merchant = get_ctx("merchant", merchant_id)
        if not merchant:
            continue

        cat_slug = merchant.get("category_slug") or merchant.get("identity", {}).get("category_slug", "")
        category = get_ctx("category", cat_slug)
        if not category:
            # Try to find any category as fallback
            for (scope, cid), entry in contexts.items():
                if scope == "category":
                    category = entry["payload"]
                    break
        if not category:
            continue

        # Customer context (for customer-scoped triggers)
        customer_id = trg.get("customer_id") or trg.get("payload", {}).get("customer_id")
        customer = get_ctx("customer", customer_id) if customer_id else None

        # Check prior sends for this merchant to avoid repetition
        conv_id = f"conv_{merchant_id}_{trg_id}"
        prior_body = ""
        if conv_id in conversations:
            sent = conversations[conv_id].get("sent_bodies", [])
            prior_body = sent[-1] if sent else ""

        try:
            body_text, rationale = await asyncio.wait_for(
                compose_message(category, merchant, trg, customer, prior_body),
                timeout=22,
            )
        except asyncio.TimeoutError:
            continue
        except Exception as e:
            body_text = ""
            rationale = f"Composition error: {e}"

        if not body_text:
            continue

        # Record to conversation state
        if conv_id not in conversations:
            conversations[conv_id] = {
                "merchant_id": merchant_id,
                "customer_id": customer_id,
                "trigger_id": trg_id,
                "turns": [],
                "sent_bodies": [],
            }
        conversations[conv_id]["sent_bodies"].append(body_text)
        conversations[conv_id]["turns"].append({"from": "vera", "msg": body_text})

        sent_suppressions.add(sup_key)
        merchants_this_tick.add(merchant_id)

        scope = trg.get("scope", "merchant")
        send_as = "merchant_on_behalf" if scope == "customer" else "vera"

        actions.append({
            "conversation_id": conv_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "send_as": send_as,
            "trigger_id": trg_id,
            "template_name": f"vera_{trg.get('kind', 'generic')}_v1",
            "template_params": [
                merchant.get("identity", {}).get("name", ""),
                trg.get("kind", ""),
                body_text[:60],
            ],
            "body": body_text,
            "cta": "open_ended",
            "suppression_key": sup_key,
            "rationale": rationale,
        })

    return {"actions": actions}


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    conv_id = body.conversation_id
    merchant_id = body.merchant_id
    customer_id = body.customer_id
    message = body.message

    # Initialize conversation if new
    if conv_id not in conversations:
        conversations[conv_id] = {
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "trigger_id": None,
            "turns": [],
            "sent_bodies": [],
            "auto_reply_count": 0,
        }

    conv = conversations[conv_id]

    # Auto-reply detection
    if detect_auto_reply(message, conv["turns"]):
        conv["auto_reply_count"] = conv.get("auto_reply_count", 0) + 1
        if conv["auto_reply_count"] >= 2:
            # Silently back off — not worth engaging an auto-responder
            return {
                "action": "wait",
                "wait_seconds": 3600,
                "rationale": "Auto-reply detected (same message 3+ times). Backing off 1h.",
            }
        # First auto-reply detection: wait a bit, then try again
        return {
            "action": "wait",
            "wait_seconds": 900,
            "rationale": "Possible auto-reply detected. Waiting 15 min before next attempt.",
        }

    # Log this turn
    conv["turns"].append({"from": body.from_role, "msg": message})

    # Intent detection
    intent = detect_intent(message)

    # Hostile → end immediately
    if intent == "hostile":
        return {
            "action": "end",
            "rationale": "Merchant requested to stop contact. Respecting preference.",
        }

    # Pull context for this conversation
    merchant = (get_ctx("merchant", merchant_id) or {}) if merchant_id else {}
    cat_slug = merchant.get("category_slug") or merchant.get("identity", {}).get("category_slug", "")
    category = get_ctx("category", cat_slug) or {}
    if not category:
        for (scope, cid), entry in contexts.items():
            if scope == "category":
                category = entry["payload"]
                break

    trigger_id = conv.get("trigger_id")
    trigger = get_ctx("trigger", trigger_id) if trigger_id else None

    # Check for 3 consecutive unanswered nudges → stop
    vera_turns = [t for t in conv["turns"] if t.get("from") == "vera"]
    merchant_turns = [t for t in conv["turns"] if t.get("from") == "merchant"]
    if len(vera_turns) >= 3 and len(merchant_turns) == 0:
        return {
            "action": "end",
            "rationale": "3 Vera nudges with no merchant engagement. Gracefully exiting.",
        }

    try:
        action, reply_body, rationale = await asyncio.wait_for(
            compose_reply(merchant, category, trigger, message, conv["turns"], intent),
            timeout=22,
        )
    except asyncio.TimeoutError:
        return {"action": "wait", "wait_seconds": 60,
                "rationale": "Composition timeout — backing off."}
    except Exception as e:
        return {"action": "wait", "wait_seconds": 60,
                "rationale": f"Error: {e}"}

    if action == "end":
        return {"action": "end", "rationale": rationale}

    if action == "wait":
        return {"action": "wait", "wait_seconds": 1800, "rationale": rationale}

    # Anti-repetition check
    sent = conv.get("sent_bodies", [])
    if reply_body in sent:
        # Force a different angle
        reply_body = reply_body + " — Want me to show you how?"  # mild differentiation

    conv["sent_bodies"].append(reply_body)
    conv["turns"].append({"from": "vera", "msg": reply_body})

    return {
        "action": "send",
        "body": reply_body,
        "cta": "open_ended",
        "rationale": rationale,
    }


@app.post("/v1/teardown")
async def teardown():
    """Optional: wipe all state at end of test."""
    contexts.clear()
    conversations.clear()
    sent_suppressions.clear()
    return {"wiped": True}


# ─── ENTRY POINT ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("bot:app", host="0.0.0.0", port=8080, reload=False)
