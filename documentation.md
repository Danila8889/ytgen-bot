# ytgen-bot - architecture and behaviour

README.md says what the bot is and how to run it; this file is the detailed reference (architecture, flows, error
map, invariants, contract notes, owner setup). Keep both current: a change to behaviour, env vars, the wrapper
contract it uses, or a data flow updates the matching section in the same commit.

## 1. Shape

```
Telegram  <--long polling-->  ytgen-bot (aiogram 3)  <--HTTPS, X-Bot-Service-Key + X-Telegram-User-Id-->  wrapper /v1/bot/*
```

- One process, one container (`bot`), outbound connections only. No database, no volume, no published port.
- The wrapper owns accounts, consent, billing, referrals and the link between a Telegram id and a tenant. The bot
  never sees a tenant key or session, never calls YooKassa or YouTubeGen core.
- Python 3.12+, `src/ytgen_bot/`:

| Module | Role |
|---|---|
| `config.py` | Env parsing, fail-fast, names variables never values. |
| `wrapper.py`, `schemas.py` | The only HTTP client: auth headers on every call, error shapes, response models (extra fields ignored, missing/mistyped fields fail loudly). |
| `app.py`, `__main__.py` | Dispatcher wiring, startup self-check, heartbeat, polling, `selfcheck` command. |
| `middlewares.py` | `GuardMiddleware` (private chats only, input length, per-user token bucket, Forbidden -> `/blocked`) and `ConsentMiddleware`. |
| `handlers/` | `start.py` (/start, consent, /stop; not behind the consent gate), `link.py`, `tariffs.py` (tariff, promo, quote, pay), `payment.py` (check button, return from payment), `referral.py`, `menu.py` (help, status, menu buttons, fallbacks). |
| `payments.py` | Bounded background payment poller and the terminal-state messages. |
| `errors_ru.py` | The one place that maps wrapper error codes to Russian text, per context. |
| `texts.py`, `keyboards.py`, `callbacks.py`, `formatting.py` | Text, buttons, validated callback data, rendering helpers. |
| `ratelimit.py` | Token bucket + action guard (debounce). |
| `masking.py`, `logging_setup.py` | PII masking and the JSON/redacting log setup. |

## 2. What the bot remembers (all in process memory, none of it authoritative)

| What | Where | Lost on restart means |
|---|---|---|
| Dialogue state (email, code, promo, withdrawal amount prompts) | aiogram `MemoryStorage` | The user's next message gets "не понял, откройте меню"; they start the step again. |
| Token buckets, double-tap guards | `ratelimit.py` | Reset (harmless). |
| Running payment pollers | `PaymentWatchers` | Polling stops; the "Проверить оплату" button (payment id in its callback data) starts a new one. |

**Consent is not remembered at all**: every gated update reads it from the wrapper (`GET /me`), so it survives restarts
and an opt-out applies at once.

Everything a button needs to work after a restart is **in its callback data** (`callbacks.py`): the payment id, tariff
id, expected amount, promo code, the referral code held by the consent button, the withdrawal amount.

## 3. Flows

### 3.1 Consent and `/start`
1. `/start [payload]`. Payload: `ref_<code>` (`[A-Za-z0-9_-]{1,60}`) -> referral code; exactly `paid` -> payment screen;
   anything else is ignored and logged.
2. Consent is read with `GET /me` (`consent_at`, `opted_out_at`; read-only, creates no row, no `start` event). For a
   consented user the bot then sends `POST /start {language, referral_code?}` (a real start: records the event, clears
   `blocked_at`). For anyone else **nothing is written to the wrapper**.
3. Not consented (never, or opted out): the consent text with the three links and the "Согласен" button. A referral
   code from the link is put **into the button's callback data** (`cn:<code>`), so it is not held anywhere else and not sent
   before consent. Pressing the button sends `POST /start {consent: true, referral_code?}`, then the menu.
4. The wrapper's referral outcome (`pending_stored`, `attributed`, `already_attributed`, `already_pending`, `rejected_*`)
   is shown in one plain sentence; nothing promises a benefit. A code is sent on every `/start` that carries one, also
   for users who started organically earlier; the wrapper decides.
5. `ConsentMiddleware` (one `GET /me` per update) gates every other handler: unconsented users get the consent screen (callbacks: a toast first).
   Exempt: `/start`, the consent button, `/stop`.

### 3.2 `/stop`
`POST /opt-out`, then one confirmation line. A failed call is shown as a failure, never as success. The next update
reads `opted_out_at` from `/me`: every other command shows the consent screen, and a payment result is logged, not sent.
Telegram `Forbidden` on any send (user blocked the bot / account gone) is caught in `ui.say` and in the guard
middleware and reported with `POST /blocked {blocked: true}` (the wrapper clears it on the next `/start`).

### 3.3 Account link
Email -> `POST /link/request-code` -> code -> `POST /link/verify`. Local checks (email shape and length, 6 digits)
save round trips only; the wrapper stays the authority. The email is shown back masked (`j***@example.com`) and is
only held in the user's message and the request; it is not stored by the bot. The tariff that started the flow
travels in the dialogue state and the "request again" button (`lk:<tariff_id>`); after a successful link the bot
continues with that tariff. A `/command` at any step leaves the flow.

### 3.4 Tariffs, promo, quote, checkout
`GET /tariffs` -> list (name, price, `period_days` and the `capacity` / `max_ai_scenes_per_video` figures, only those the
API returned) -> pick -> `GET /me` (not linked: link flow first) -> optional promo (normalised to upper case,
`[A-Z0-9_-]{1,32}`; the wrapper decides validity) -> `POST /billing/quote` -> the payable amount is
`promo.price_rub` when a promo applied, else `price_rub` (the quote's top-level price is the list price) ->
"Перейти к оплате" (`pay:<tariff>:<amount>[:<promo>]`) -> `POST /billing/checkout {tariff_id, promo_code?, return_url}`.

- `return_url = https://t.me/<bot_username>?start=paid`, `bot_username` from `getMe` at startup. The wrapper rebuilds it from
  its own `BOT_DEEP_LINK_BASE`; a different username there gives `422 return_url_not_allowed`, which the bot treats as an
  operator misconfiguration (generic user message, `wrapper_misconfigured` at ERROR).
- The payment button is a URL button (`https` only) to `payment_url`, with a "Проверить оплату" button next to it.
- `reused: true`: "the earlier payment link is still valid, no new payment is created". If the amount in the response differs
  from the quoted one, the message says so.
- Double tap: one in-flight checkout per (user, tariff, promo) plus a 3 s cooldown in the bot; behind it the wrapper's own
  duplicate guard (`reused`, or `409 checkout_in_progress`).

### 3.5 Payment result
After checkout: "payment is being processed" and a background poller on `GET /billing/payments/{id}` with growing delays
(3 s, x1.5, capped at 30 s) for **at most 10 minutes**, one poller per payment, at most 200 at once. Outcomes:
`succeeded` + `credited` -> success message with tariff and expiry from `GET /me`; `canceled` -> cancelled message with a
reason when it is one of the known codes (otherwise no raw code is shown); `succeeded` but not credited at the deadline ->
"paid, tariff not applied yet, check again"; still `pending` at the deadline -> "not confirmed yet"; five consecutive
failures -> "could not check". Every timeout/give-up message carries the check button. The poller stops quietly for
opted-out users.
`/start paid` (the return from the payment page) carries no payment id, so it calls `GET /billing/payments/latest` and shows
the real status with the same result messages (pending/delayed also start a poller and show the check button). Only on
`404 payment_not_found` (no bot checkout yet) or `403 telegram_not_linked` does it fall back to the `GET /me` account state.
A user who opted out is not messaged with a payment result (logged only).

### 3.6 Referral
`GET /referral` for linked users (a 403 `telegram_not_linked` becomes the link prompt). Shown: the `deep_link` (if null:
"temporarily unavailable" and a `referral_deep_link_missing` ERROR log, **no fallback link is invented**), counts, the four
balances (+ `owed_rub` when > 0), `reward_percent` / `hold_days` / `min_withdrawal_rub` and a link to offer section 10, an
open withdrawal if any. Withdrawal: button only when `available >= min` and none is open -> amount prompt (digits only) ->
confirmation (`wdok:<amount>`) -> `POST /referral/withdrawals {amount_rub}` -> "the operator will contact you in Telegram"
+ `SUPPORT_CONTACT`. No payout details are asked, accepted or stored; the prompt says not to send them.

### 3.7 Throttling and input
Per Telegram user, in memory: token bucket (burst 6, one more every 2 s), at most one "too fast" notice per 5 s, buttons
answered with a toast. Wrapper `429` -> "retry in N s/min" from `retry_after_s` (or the `Retry-After` header). Text over
1000 characters is refused before any handling; every state input is validated; callback data is parsed by strict regexes
(`callbacks.parse`) and anything else is "button outdated".

## 4. Error map (wrapper -> user)

Implemented in `errors_ru.py`; `DOCUMENTED_CODES` lists every code per context and `tests/test_errors.py` pins each to a
specific message.

| Context | Codes |
|---|---|
| link request | `invalid_email`, `telegram_already_linked`, `rate_limited` (cooldown / hourly buckets), `email_delivery_failed` |
| link verify | `no_pending_code`, `code_invalid` (+`attempts_remaining`), `code_attempts_exceeded`, `rate_limited`, `telegram_already_linked`, `tenant_already_linked`, `email_ambiguous`, `tenant_inactive` |
| quote / checkout | `promo_invalid`, `promo_expired`, `promo_exhausted`, `promo_rate_limited`, `tariff_id_required`, `tariff_not_found`, `active_tariff_mismatch`, `checkout_in_progress`, `tariff_misconfigured`, `telegram_not_linked`, `tenant_inactive`, `rate_limited` |
| payment | `payment_not_found`, `payment_provider_unknown_status`, `telegram_not_linked` |
| withdrawal | `withdrawal_below_minimum` (+`minimum_rub`), `insufficient_balance` (+`available_rub`), `withdrawal_already_open`, `rate_limited` |
| operator-side (generic text for the user, ERROR in the log) | `invalid_bot_service_key`, `bot_api_disabled`, `bot_return_url_not_configured`, `return_url_not_allowed`, `invalid_telegram_user_id` |
| anything else | 5xx -> "service temporarily unavailable"; unknown 4xx -> generic retry message **and** `wrapper_error_unmapped` at ERROR. Network failure -> "service unavailable". A 2xx with an unexpected shape -> generic message and `wrapper_contract_mismatch` at ERROR (never a half-filled screen). |

## 5. Invariants (do not "fix" these away)

- **Stateless.** No database, no file that matters, no public port. The wrapper is the system of record.
- **No money or limits in the bot.** No price, limit, period, percentage or reward rule is a constant in the code or the text;
  everything displayed is a wrapper field (`tests/test_no_constants.py`).
- **Consent first.** Nothing but `/start`, the consent button and `/stop` works before consent; `consent: true` is sent only
  after the button press; a referral code is sent only once the user is known to have consented.
- **No payout details.** Never requested, never stored, never logged.
- **Fail loudly.** No silent `except`, no fake success: a failed opt-out or checkout is shown as a failure and logged; unmapped
  codes and contract mismatches are ERROR logs.
- **Secrets and PII.** `BOT_TOKEN` and `BOT_SERVICE_API_KEY` are only ever env values (never in `repr`, errors or logs; the
  logging filter replaces them if they leak into text). Emails are masked, codes are never written, only the Telegram id
  identifies a person in a log line.
- **No automatic deploy.** `deploy` is `workflow_dispatch` only.

## 6. Logging

JSON lines on stdout (`logging_setup.py`): `ts`, `level`, `logger`, `msg` (an event name such as `checkout_created`) plus
fields (`tg_user`, `payment_id`, `code`, ...). A redaction filter replaces the configured secrets, bot-token shapes and
email addresses even in tracebacks. httpx/httpcore are held at WARNING (they log full URLs).

## 7. Contract notes (what remains of the differences found while building; the wrapper was not changed)

Resolved by wrapper `1abbd66` (the bot now uses them): consent is read from `GET /me` (`consent_at`, `opted_out_at`,
`blocked`, `blocked_at`), `/start paid` uses `GET /billing/payments/latest`, and the unknown-tariff 404 is
`{"detail": {"error": "tariff_not_found"}}`.

1. **FastAPI body-validation 422** keeps a list-shaped `detail` on bot routes (the contract says so); mapped to `validation_error`.
2. **`BOT_DEEP_LINK_BASE` must name this bot** (the username the bot reads from `getMe`, `ytgener_bot`) or checkout is `422
   return_url_not_allowed`; while it is unset checkout is `503` and the referral `deep_link` is null.
3. `GET /billing/payments/{id}` and `/latest` ask YooKassa synchronously on each call, so the poller's delays are deliberately modest.
4. The startup/deploy self-check calls `GET /me` with the **bot's own Telegram id** (never a real user). Since `1abbd66` it
   creates no bot-user row; it only counts one `bot_call` rate event.
5. Consent is read on every gated update (one extra `GET /me`, counted by the wrapper's 60/min per-user `bot_call` limit);
   the bot's own token bucket (burst 6, then one per 2 s) keeps normal use well inside it.

## 8. Owner setup (one time; secret values are only ever typed in the owner's own terminal)

```bash
# 1. Create the bot with BotFather; note its username and token. Then, in your own terminal:
gh secret set BOT_TOKEN -R Danila8889/ytgen-bot            # paste the token at the prompt

# 2. One shared key for bot and wrapper (never echo it):
KEY=$(openssl rand -hex 32)
printf %s "$KEY" | gh secret set BOT_SERVICE_API_KEY -R Danila8889/ytgen-bot
printf %s "$KEY" | gh secret set BOT_SERVICE_API_KEY -R Danila8889/youtubegen-saas-wrapper
unset KEY

# 3. Wrapper: BOT_DEEP_LINK_BASE=https://t.me/<bot username> in its .env.production, then redeploy the wrapper
#    (the key is rendered at wrapper deploy time).
# 4. This repo: SUPPORT_CONTACT is set in .env.production (@togn15).
# 5. Register the self-hosted runner on the VPS (below), then:
gh workflow run ci.yml --ref main -R Danila8889/ytgen-bot
```

Runner (same convention as the sibling repos: one runner per repo, service named `actions.runner.<owner>-<repo>.<name>`,
directory `/opt/actions-runner-<label>`; see `ytgen-infra/hosts.md`). On the VPS, as the user the sibling runners use
(it must be able to run `docker`):

```bash
# in your own terminal, get a short-lived registration token:
gh api -X POST repos/Danila8889/ytgen-bot/actions/runners/registration-token --jq .token
# on the VPS:
sudo mkdir -p /opt/actions-runner-ytgen-bot && sudo chown "$USER" /opt/actions-runner-ytgen-bot && cd /opt/actions-runner-ytgen-bot
# download the runner package matching the sibling runners' version from https://github.com/actions/runner/releases, extract it, then:
./config.sh --url https://github.com/Danila8889/ytgen-bot --token <REGISTRATION_TOKEN> \
            --name ytgen-bot-vps-runner --labels ytgen-bot-vps --unattended
sudo ./svc.sh install "$USER" && sudo ./svc.sh start
```

The workflow targets the label `ytgen-bot-vps`; the service ends up as `actions.runner.Danila8889-ytgen-bot.ytgen-bot-vps-runner`.
`ytgen-infra` should then get a row in `hosts.md` and an `env-vars/ytgen-bot.md` (this repo does not edit that repo).

## 9. Operations

- Logs: `docker compose logs -f bot`. Health: `docker compose ps` (the healthcheck reads the heartbeat file, refreshed every
  minute while Telegram answers `getMe`).
- A restart loop with `self_check_failed` means: wrong/absent `BOT_SERVICE_API_KEY` on either side, the wrapper's bot API
  switched off, or the wrapper unreachable at `WRAPPER_BASE_URL` (the reason is in the log line, never a secret).
- Rollback is automatic on a failed deploy (see README "Deploy"); a bad commit that passes the self-check is fixed forward through git.
