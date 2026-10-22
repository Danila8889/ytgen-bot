# ytgen-bot - Priorities

This file is about **what matters and why**. For git, tests, env vars and deployment mechanics see
`.claude/rules/engineering.md`; for decision authority and code quality see `.claude/rules/principles.md` (canonical copy
in `ytgen-infra`, do not edit it here).

## What this service actually is

A **stateless** Telegram bot (long polling, aiogram 3) that is a thin client of `youtubegen-saas-wrapper`'s `/v1/bot/*`
API: account linking, checkout, payment status, the referral programme. The wrapper is the system of record. The bot has
no database and no public port, never talks to YooKassa or YouTubeGen core, and the product itself is **not** used inside
Telegram (the README and `/help` say so). Not to be confused with `tryon-bot` (`~/PycharmProjects/TelegramBot`, frozen,
a different product).

## Priority order when things conflict

1. **Correctness and honest failure.** A failure is shown and logged, never turned into a fake success.
2. **Nothing the wrapper does not say.** No price, limit, percentage or promise that is not a wrapper field.
3. **Convenience, polish, extra features.** Real, but never at the expense of 1 or 2.

## Hard invariants - never "fix" these away

- **Stateless.** No database, no persisted state, no published port. If a feature seems to need storage, it belongs in
  the wrapper: stop and flag it.
- **Isolation.** The only upstream is the wrapper (`WrapperClient`). No YooKassa, no core, no direct DB access. If a task
  seems to need a shortcut, stop and flag it.
- **No money or limits in the bot.** Tariffs, prices, periods, limits, referral rules are rendered from API responses only.
  `tests/test_no_constants.py` enforces it; do not add an allowlist entry to get around it.
- **Consent first.** Only `/start`, the consent button and `/stop` work before consent; `consent: true` goes to the wrapper only
  after the button press; the referral code is sent only after consent.
- **No payout details** (card, phone) asked for, accepted, stored or logged.
- **Fail loud.** No silent `except`, no fake success, unmapped wrapper codes and contract mismatches are ERROR logs.
- **Secrets and PII.** `BOT_TOKEN` / `BOT_SERVICE_API_KEY` are env-only, never printed, logged or put in an error message
  (name the variable, never the value). Emails are masked and codes never logged in full. Only the Telegram id identifies a person in logs.
- **Deploy is `workflow_dispatch` only** and a missing secret fails the deploy by name.

## Always flag before proceeding, even if not explicitly asked

- Anything touching payments, referral/withdrawal flows, or the shared `BOT_SERVICE_API_KEY`.
- Any new wrapper-side endpoint or field the bot would rely on (validate it against the wrapper's real code and README first;
  a mismatch is reported, never patched around, and the wrapper repo is not edited from here).
- New external dependencies, or any state that would outlive the process.
- Changing user-facing consent / referral / withdrawal wording that makes a promise.

## Real incidents - why these rules exist

None yet in this repo. Add an entry whenever an agent repeats a mistake (what happened, root cause, failure layer per
`principles.md` §6.1, the resulting rule). Sibling repos' incident lists (`YouTubeGenSaaS/CLAUDE.md`) are worth reading for the billing side.
