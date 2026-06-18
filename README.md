# Vera Message Engine — magicpin AI Challenge

**Team**: Bishwjit Kumar  
**Model**: `claude-sonnet-4-6`  
**Framework**: 4-context composition with trigger-kind routing

---

## Quick start

```bash
pip install fastapi uvicorn httpx
export ANTHROPIC_API_KEY=sk-ant-...
uvicorn bot:app --host 0.0.0.0 --port 8080
```

Local test with the provided judge:
```bash
export BOT_URL=http://localhost:8080
python judge_simulator.py
```

---

## Architecture

```
Judge → POST /v1/context  → Context Store (in-memory dict)
                                    ↓
Judge → POST /v1/tick     → Trigger Router
                                    ↓
                             4-Context Assembler
                            (category + merchant + trigger + customer?)
                                    ↓
                             Claude Composer (claude-sonnet-4-6)
                            ┌── trigger-kind routing (12 kinds)
                            ├── language-adaptive (en / hi / hinglish)
                            └── anti-repetition guard
                                    ↓
                             Action → Judge

Judge → POST /v1/reply    → Intent Detector
                            ┌── accept → ACTION mode (no qualifying)
                            ├── reject → graceful exit hook
                            ├── hostile → end immediately
                            ├── question → answer + restate CTA
                            └── auto-reply → wait / back-off
                                    ↓
                             Reply Composer (Claude)
                                    ↓
                             send | wait | end → Judge
```

---

## How it beats production Vera

| Pain point | This bot's fix |
|---|---|
| Auto-reply pollution | `detect_auto_reply()` — keyword match + verbatim-repeat counter. Backs off after 2 detections. |
| Intent-handoff failure | `detect_intent()` — 11-case Hinglish-aware classifier. On `accept`, reply composer is instructed to switch to ACTION mode immediately — zero qualifying questions. |
| Generic copy | Composer is forced to prefer service+price catalog ("Dental Cleaning @ ₹299") and penalized for discount-only phrasing. |
| Low engagement frequency | 12 trigger-kind routing variants, each with its own framing hint (research_digest, recall_due, perf_spike, competitor_opened, etc.) |

---

## 5 endpoints

| Endpoint | Description |
|---|---|
| `POST /v1/context` | Receive category / merchant / customer / trigger context (idempotent by `context_id + version`) |
| `POST /v1/tick` | Periodic wake-up; composes and returns proactive messages |
| `POST /v1/reply` | Handle merchant / customer reply; returns `send | wait | end` |
| `GET /v1/healthz` | Liveness probe with context counts |
| `GET /v1/metadata` | Bot identity |
| `POST /v1/teardown` | (Optional) wipe all state |

---

## Composer design

### Trigger-kind routing

12 trigger kinds are handled with specific framing hints:

- `research_digest` → open with the specific finding + source citation, anchor to merchant's patient cohort
- `recall_due` → patient name, exact gap in months, 2 concrete time slots
- `perf_spike` → lead with the specific % spike, suggest capitalizing with a post/offer
- `perf_dip` → propose one concrete fix, no doom
- `competitor_opened` → voyeur curiosity without naming the competitor
- `festival_upcoming` → name + date + specific offer
- `dormant_with_vera` → one useful intel, no pitch
- `review_theme_emerged` → name the theme, suggest one action
- `customer_lapsed_soft` → warm re-engagement, easy re-booking path
- `milestone_reached` → celebrate briefly, pivot to next action
- `weather_heatwave` → tie weather to relevant service
- `scheduled_recurring` → curious-ask style, pick the most interesting signal

### Compulsion levers used

- **Specificity** (numbers, source citations, dates)
- **Social proof** (peer benchmarks vs. merchant's actual CTR)
- **Curiosity** (competitor opens, trend signals)
- **Loss aversion** (lapsed patients, CTR gap)
- **Reciprocity** (digest + patient-ed offer)

### Language

Auto-detects from `identity.languages`:
- `["en"]` → English
- `["hi"]` → Hindi  
- `["hi", "en"]` → Hinglish code-mix

---

## Conversation state machine

```
NEW CONVERSATION
    │
    ▼
/v1/tick → compose proactive nudge → send
    │
    ▼ (merchant replies)
/v1/reply → intent detection
    │
    ├─ hostile   → end
    ├─ auto-reply × 2 → wait 1h
    ├─ accept    → ACTION mode reply → send
    ├─ reject    → graceful exit hook → send/end
    ├─ question  → direct answer + CTA → send
    └─ neutral   → advance conversation → send

3 unanswered Vera nudges → end (graceful)
```

---

## Anti-patterns avoided

- ❌ Generic "Flat 30% OFF" → always service+price from catalog
- ❌ Multiple CTAs → single binary ask, always last line
- ❌ Long preambles → start sharp, no "I hope you're doing well"
- ❌ Re-introducing Vera → only turn 1 may name Vera
- ❌ Hallucinated data → only cites numbers from injected context
- ❌ Repetition → anti-repetition guard on sent_bodies
- ❌ Wrong language → language derived per-merchant

---

## Files

```
bot.py        ← Main server (all logic in one file)
README.md     ← This file
```
