# ytgen-bot - Priorities

This file is about **what matters and why**. For git, tests, env vars and deployment mechanics see
`.claude/rules/engineering.md`; for decision authority and code quality see `.claude/rules/principles.md` (canonical copy
in `ytgen-infra`, do not edit it here).

## What this service actually is

A Telegram bot (long polling, aiogram 3) with two jobs:

1. **Checkout and referral client of `youtubegen-saas-wrapper`'s `/v1/bot/*` API**: account linking, checkout, payment status,
   the referral programme. The wrapper is the system of record for accounts, consent-to-wrapper, billing and referrals.
2. **A scripted sales funnel** (SPIN questions -> tariff recommendation -> presentation -> objections -> closing -> hand-off to a
   human, plus reminders), with an LLM used only for free questions and for labelling an objection. Its data lives in the
   bot's **own Postgres** and is **funnel data only**.

The bot never talks to YooKassa or YouTubeGen core, has no public port, and the product itself is **not** used inside Telegram
(the README and `/help` say so). Not to be confused with `tryon-bot` (`~/PycharmProjects/TelegramBot`, frozen, a different
product).

## Priority order when things conflict

1. **Correctness and honest failure.** A failure is shown and logged, never turned into a fake success.
2. **Nothing the wrapper does not say.** No price, limit, percentage or promise that is not a wrapper field.
3. **Convenience, polish, extra features.** Real, but never at the expense of 1 or 2.

## Hard invariants - never "fix" these away

- **Stateful for funnel data only (amended 2026-10-22; was "stateless").** The bot owns one Postgres (compose service `db`,
  compose-internal network, **no published port**, named volume) holding lead profiles, events, objections, the AI log, a
  rolling buffer of five free-text messages per person and the reminder schedule. It is **never** authoritative for accounts,
  consent-to-wrapper, billing, referrals, money or limits: those stay in the wrapper. If a feature seems to need any of that
  in the bot's database, stop and flag it.
- **Upstreams (amended).** The wrapper (`WrapperClient`), the bot's own Postgres, the kie.ai chat API (LLM, **single provider,
  no failover**) and the manager's Telegram chat (`MANAGER_CHAT_ID`). No YooKassa, no core, no other database. If a task seems
  to need a shortcut, stop and flag it.
- **ISOLATION (new).** A funnel / DB / LLM failure must **never** break checkout, payment status, link, referral or withdrawal.
  Persistence or hook failures *inside those flows* are logged at ERROR and do not block them (`funnel.flow._isolated`; every
  broad `except` in this repo logs at ERROR with the traceback: that one, the manager-alert send, the reminder loop's crash guard); failures *inside funnel flows* are shown to the person ("something went wrong, write to the
  manager", with the `SUPPORT_CONTACT` button) and logged at ERROR. The bot starts even if the funnel database is down (ERROR at
  startup). Never a silent `except`, never a fake success. `tests/test_funnel_stop_isolation.py` pins this: do not weaken it.
- **No money or limits in the bot.** Tariffs, prices, periods, limits, referral rules are rendered from API responses only
  (`tests/test_no_constants.py`, which also covers the funnel's text modules; do not add an allowlist entry to get around it).
  The only numbers that are not tariff data are non-tariff product facts no API exposes ("до 4 языков из 12", "1-5 ссылок",
  music): they live **only** in `src/ytgen_bot/knowledge/knowledge_base.toml`, each with a source, listed in `documentation.md` as
  facts that can go stale, and a test fails if a tariff price/limit appears there.
- **Tariff roles by rank, never by name.** Entry = the cheapest `is_monthly=false` tariff; monthly tariffs by ascending price =
  base / pro / ultra. A list without exactly one non-monthly and three monthly tariffs, a row without `is_monthly` or without a
  complete `capacity`, is an ERROR and the funnel falls back to the manager. Never guess, never default.
- **The AI never decides.** It cannot pick a tariff, state a price or move a person through the funnel; the script does. It
  returns strict JSON (anything else is a failure), every reply passes the output validator, the live tariff block must be
  buildable or the LLM is not called, and every provider soft error (200 + error body, truncated JSON, `finish_reason=length`,
  gateway error sentences) is a failure, never an answer. **The provider/model is an interim MVP choice** (see README "AI
  layer"); do not add a second provider or a failover without the owner.
- **Consent first.** Only `/start`, the consent button and `/stop` work before consent; `consent: true` goes to the wrapper only
  after the button press; the referral code is sent only after consent; **funnel data is processed only after consent** (a lead
  row exists only after the consent button of the current consent text; people who consented before the funnel existed see it
  once more). Reminders need their **own explicit opt-in** (Yes/No after consent); "No" blocks nothing.
- **Retention.** `/stop` deletes ALL of that person's funnel data in one transaction and keeps only a tombstone (tg id, status
  `stopped`, timestamp); AI-log rows older than 90 days are purged in-process. Free text is never logged at INFO/DEBUG; logs
  identify people by Telegram id only; `funnel_events.value` holds button codes only.
- **Additive migrations only.** Alembic revisions only add (tables, nullable/defaulted columns, indexes): a rollback to the
  previous image must run on the migrated schema (`tests/test_db_store.py` scans for drops/renames/retyping).
- **No payout details** (card, phone) asked for, accepted, stored or logged.
- **Fail loud.** No silent `except`, no fake success, unmapped wrapper codes and contract mismatches are ERROR logs.
- **Secrets and PII.** `BOT_TOKEN` / `BOT_SERVICE_API_KEY` / `BOT_DB_PASSWORD` / `KIE_API_KEY` / `MANAGER_CHAT_ID` are env-only, never
  printed, logged or put in an error message (name the variable, never the value). Emails are masked and codes never logged in
  full. Only the Telegram id identifies a person in logs.
- **The manager command `/paid` fails closed**: authorised strictly by chat id == `MANAGER_CHAT_ID`; anyone else is ignored
  silently with an INFO log.
- **Deploy is `workflow_dispatch` only** and a missing secret fails the deploy by name (before anything is touched).

## Always flag before proceeding, even if not explicitly asked

- Anything touching payments, referral/withdrawal flows, or the shared `BOT_SERVICE_API_KEY`.
- Any new wrapper-side endpoint or field the bot would rely on (validate it against the wrapper's real code and README first;
  a mismatch is reported, never patched around, and the wrapper repo is not edited from here).
- Anything that adds a field to the funnel database, a new upstream, a second LLM provider, or any state about accounts/billing.
- Changing user-facing consent / reminders / referral / withdrawal wording that makes a promise (the consent text and the
  reminders opt-in text are flagged changes).
- Any funnel text that states a number, an upgrade rule, a guarantee, a discount, a manager response time, or a capability
  (computer vision, publishing, auxiliary scenes) that the knowledge base does not support.

## Real incidents - why these rules exist

None yet in this repo. Add an entry whenever an agent repeats a mistake (what happened, root cause, failure layer per
`principles.md` §6.1, the resulting rule). Sibling repos' incident lists (`YouTubeGenSaaS/CLAUDE.md`) are worth reading for the billing side.
