# hermes-autonomous

**Your agent should remember to check on you.**

Most assistants wait. You ask, it answers, the thread closes — and the thing you mentioned on Tuesday
(you were sick, you had a meeting, you bought something) never comes back.

`hermes-autonomous` is a proactivity layer for [Hermes Agent](https://hermes-agent.nousresearch.com):
it quietly reads your past conversations, notices what is worth returning to, and comes back at the
right moment — sometimes with a useful check (weather, price, a deadline), sometimes with nothing more
than *"did your throat get better?"*

It is not a digest. It is not a task list. It does not require you to configure what it should watch.

[中文说明](README.zh-CN.md)

---

## Install (about 2 minutes, no coding)

It is an ordinary Hermes plugin, so the install is one command:

```bash
hermes plugins install xiaok/hermes-autonomous --enable
```

Then four more lines wire it to your chat and schedule the loop:

```bash
PLUGIN=~/.hermes/plugins/hermes-autonomous
python3 $PLUGIN/scripts/followups.py setup --owner-id <id> --dm-target telegram:<id>
cp $PLUGIN/cron/*.py ~/.hermes/scripts/
hermes cron create "*/15 * * * *" --name "proactive follow-ups" \
  --monitor-script followup-wake.py --script followup-digest.py \
  --prompt "Read $PLUGIN/prompts/review.md and follow it" --deliver origin
```

Don't know your Telegram id? Message [@userinfobot](https://t.me/userinfobot), or ask Hermes — it can
read the id of the conversation it is in.

Or simply tell your agent:

> Install the plugin `xiaok/hermes-autonomous`, enable it, configure it for the chat we are in, and
> create the review cron job.

### From source (development)

```bash
git clone https://github.com/xiaok/hermes-autonomous.git ~/opt/hermes-autonomous
ln -s ~/opt/hermes-autonomous ~/.hermes/plugins/hermes-autonomous
hermes plugins enable hermes-autonomous      # tools load on the next session
python3 ~/opt/hermes-autonomous/scripts/followups.py setup --owner-id … --dm-target …
```

---

## What it actually does

```
every 15 min   a cheap script asks "is anything worth waking up for?"      ~0 tokens
when yes       read the last hours of conversation, update the board
               check the weather / price / page / calendar
               decide: stay silent · act and report · ask first
               at most one message, at most three items
```

Four things have to be true before it speaks:

| Gate | Rule |
|---|---|
| Due | that specific thing is worth revisiting *now* — not a fixed 9am digest |
| New | something changed since you last talked about it; a repeat is not a follow-up |
| Sourced | it can quote what you actually said; no source, no message |
| Rationed | ≤1 message per wake, ≤3 items, 90-minute cooldown, silence 23:00–08:00 |

And the reason it does not become noise: **silence is a scored outcome.** Ignored items demote
themselves; a class of follow-up you shrugged at three times stops speaking.

## Two kinds of value (the part most assistants miss)

- **Utility** — the meeting is tomorrow: here is the weather and when to leave.
- **Care** — you said your throat hurt. Two days later: *"better?"* No action value at all, and still
  the most important message it can send.

That is why the decision runs on two separate scoring lines. Judging only by "is this actionable"
silently deletes every caring message.

## Why this shape

We read the post-mortems so you don't have to. ChatGPT Pulse was retired nine months after launch:
its own limitation note admitted it kept suggesting work you had already finished, and v1 shipped with
no off switch. The pattern behind every dead proactive assistant is the same — fixed schedules, no
completion write-back, configuration pushed onto the user.

This one binds to events, writes every outcome back, and asks for nothing but a yes/no answer in chat.
The full teardown and the research numbers (the best models score ~66% F1 on "should I speak", with
35–48% precision) are in [`docs/RESEARCH.md`](docs/RESEARCH.md) and
[`docs/teardown.md`](docs/teardown.md).

## How it decides

- [`prompts/decide.md`](prompts/decide.md) — the rubric: what counts as worth returning to, the two
  scoring lines, the three action tiers.
- [`scripts/decide.py`](scripts/decide.py) — run any scenario offline and see the exact judgment:
  `python3 scripts/decide.py --scenario scenarios/01-throat.json`
- [`docs/DECIDER.md`](docs/DECIDER.md) — inputs, gates, output schema.

## Your data

- Only messages **you** wrote, in your own DM and your own group topics, are read. Nobody else's text
  reaches the model.
- State lives in `~/.hermes/plugin-data/hermes-autonomous/` (SQLite + settings, mode 0600), never in
  the repo.
- The repo ships with **no** ids, names, chats or personal scenarios — you configure them locally.

## What it touches (disclosure)

- **Reads** the Hermes session database `~/.hermes/state.db` — read-only, and only messages *you*
  wrote in your own DM and group topics. No other files are read.
- **Writes** only its own data directory `~/.hermes/plugin-data/hermes-autonomous/`
  (SQLite board + `settings.json`, mode 0600).
- **Network**: the plugin code makes no requests. During a review your agent may look things up
  (weather, prices, news) using Hermes' own tools — that traffic belongs to Hermes, not to this plugin.
- **Shell**: `/followups` runs one local Python helper (`scripts/followups.py`) with
  `sys.executable`; the cron helpers are plain Python. No `hermes` subprocess is spawned, no hook,
  no background thread, no approval bypass.
- **Credentials**: none. Your Telegram ids live in the plugin's own `settings.json` (0600) — not in
  `.env`, `auth.json` or any keychain.
- No telemetry, no analytics.

## Docs

[中文说明](README.zh-CN.md) · [Design](docs/DESIGN.md) · [Research](docs/RESEARCH.md) ·
[Decider](docs/DECIDER.md)

MIT.
