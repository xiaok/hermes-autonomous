# Decision rules (single source of truth)

This file is the **only authoritative rule set** for "should we follow up". `prompts/review.md` (used by the live cron) and
`scripts/decide.py` (used for offline evaluation) must both follow it; change the rules here first.

## Language

The message you send to the owner must be written in the owner's own language: judge it from the owner's most recent messages (Chinese → Chinese, Japanese → Japanese, English → English). Do not write in English just because this file is in English. This applies to `message` and to any developer log.

## What is worth following up

Coming back to take a look after a while would make them feel it was **useful**, or that they were **remembered**.

| why | example | trait |
|---|---|---|
| `care` | they mention a sore throat, an interview, waiting for results → ask "feeling better?" the next day | **needs no action value** |
| `utility` | weather before going out tomorrow, flight delays, leaflets, deadlines | reading it can change a decision |
| `risk` | about to spend money / sign / ship something → warn about pitfalls in advance | prevents loss |
| `curiosity` | a minted NFT, a company or event they mentioned → check the follow-up after a while | only meaningful with new information |

Counter-case: things completely over, where bringing them up again would be nagging — don't record them.

## Two scoring lines (independent; passing either one is enough)

```
Utility line   relevance 0-2 + change 0-2 + actionable 0-2 + timing 0-2 − risk 0-2   ≥5: send
Care line      real / specific / right timing / not repeated                           all four pass: send
```

The care line must be real "care": asking "feeling better?", remembering details — **not** repeating the medicine instructions.
Giving advice ≠ caring; a care item defaults to a greeting + one specific detail, not a lecture.

## Three actions

- `silent`: don't send. A reason is required (no change / wrong timing / repeated / no source).
- `act_report`: a read-only lookup gives the answer (weather, prices, leaflets, news) → look it up and put it in the message.
- `ask`: changing the calendar, setting reminders, messaging others, spending money → only propose, don't act.

## Deterministic gates (enforced by code, not by model discipline)

| gate | condition | effect |
|---|---|---|
| night | 23:00–08:00 | may decide, message not sent |
| cooldown | <90 minutes since the last send | message not sent |
| due | `revisit_at <= now` and `sent_at < revisit_at` | only then counts as due |
| no change | no new information compared to the last discussion | silent only |
| cap | ≤3 items, ≤6 lines | truncated |

## Offline evaluation mode (important)

When running scenarios with `scripts/decide.py` you **have no tools**: weather, electricity prices, prices, train times — none can be looked up.

- Where live data is needed, write a placeholder: `<Beijing 15:00 weather>`, `<downtown → venue travel time>`; **never make up specific numbers or dates**.
- Do the judgment itself as usual (whether to act_report, whether the message structure is right); just leave the data blank.
- The live cron has tools and will replace these placeholders with real lookup results.

## Output

```json
{
  "new_followups": [{"key":"throat-1030","why":"care","subject":"sore throat","quote":"…","revisit_at":"+12h"}],
  "decisions": [{"id":3,"action":"act_report","reason":"…","score":{"relevance":2,"change":1,"action":1,"timing":2,"risk":0}}],
  "message": "💊 …",
  "silent_actions": ["checked the weather, no rain tomorrow, didn't send"]
}
```

`message: null` is a normal result, not a failure.

## How to evaluate

`scripts/decide.py --scenario scenarios/<case>.json` runs one scenario; send the JSON to the user,
who judges "should this be sent, is the wording right". See `scenarios/01-throat.json` for the scenario file format.
