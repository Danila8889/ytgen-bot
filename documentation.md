# ytgen-bot - architecture and behaviour

README.md says what the bot is and how to run it; this file is the detailed reference (architecture, data model, flows, AI
layer, retention, backup, error map, invariants, contract notes, owner setup, ops). Keep both current: a change to behaviour,
env vars, the wrapper contract it uses, the data model or a data flow updates the matching section in the same commit.

## 1. Shape

```
Telegram  <--long polling-->  ytgen-bot (aiogram 3)  <--HTTPS, X-Bot-Service-Key + X-Telegram-User-Id-->  wrapper /v1/bot/*
                                   |    (+ GET /v1/public/tariffs, unauthenticated)
                                   |--- asyncpg ---> own Postgres 16 (compose service `db`, internal network only)
                                   |--- HTTPS ----> kie.ai chat completions (the LLM: ONE provider, no failover)
                                   '--- Telegram -> the manager's chat (MANAGER_CHAT_ID): hand-off cards, AI alerts, /paid
```

- One bot process, plus the `db` service and the one-shot `migrate` service. Outbound connections only; **no service publishes a
  host port** (the VPS host already uses 5432, 5433, 5434, 6379, 6380, 8088, 8090, 3000, 8080, 8100, 5555 and 8081).
- The wrapper owns accounts, consent-to-wrapper, billing, referrals and the link between a Telegram id and a tenant. The bot never
  sees a tenant key or session, never calls YooKassa or YouTubeGen core.
- The bot's own Postgres holds **funnel data only** (section 5). It is never authoritative for anything the wrapper owns, and no
  money, price, limit or period is stored in it.
- Python 3.12+, `src/ytgen_bot/`:

| Module | Role |
|---|---|
| `config.py` | Env parsing, fail-fast, names variables never values. |
| `wrapper.py`, `schemas.py` | The wrapper HTTP client: auth headers on every call, error shapes, response models; `public_tariffs()` is a separate, unauthenticated client (the service key never travels to it). |
| `app.py`, `__main__.py`, `cli.py` | Dispatcher wiring, startup checks, heartbeat, background loops, the deploy self-check, `migrate` / `funnel-report` / `ai-sample` / `ai-replay`. |
| `middlewares.py` | `GuardMiddleware` (private chats only, plus the manager group for `/paid`; input length, throttle, Forbidden -> `/blocked`) and `ConsentMiddleware`. |
| `handlers/` | `start.py` (/start, consent, /stop, the manager's /paid; not behind the consent gate), `link.py`, `tariffs.py`, `payment.py`, `referral.py`, `funnel.py` (funnel buttons and free text), `menu.py`. |
| `db/` | `schema.py` (tables), `migrations/` (Alembic, additive only), `migrate.py`, `engine.py`, `store.py` (all SQL; `StoreError`). |
| `funnel/` | `flow.py` (the state machine), `tariffs.py` (strict wrapper tariff view, roles), `rules.py` (the tariff rule table), `texts.py`, `keyboards.py`, `render.py`, `triggers.py`, `handoff.py`, `reminders.py`. |
| `ai/` | `client.py` (kie.ai client), `prompt.py`, `validator.py`, `layer.py` (limits, breaker, `Responder`), `replay.py`. |
| `knowledge/` | `knowledge_base.toml`: static non-tariff product facts, each with a source. |
| `payments.py`, `errors_ru.py`, `texts.py`, `keyboards.py`, `callbacks.py`, `formatting.py`, `ratelimit.py`, `masking.py`, `logging_setup.py` | As before: payment poller, error map, text, buttons, validated callback data, helpers, throttling, PII masking, JSON logs. |

## 2. What the bot remembers

| What | Where | Lost on restart means |
|---|---|---|
| **Funnel data** (leads, answers, events, objections, AI log, a buffer of five free-text messages, the reminder schedule) | **Postgres** (section 5) | Nothing is lost: it is the point. A returning lead resumes from the saved step. |
| Dialogue state of the old text prompts (email, code, promo, withdrawal amount) | aiogram `MemoryStorage` | The next message gets "не понял, откройте меню"; the person starts the step again. A funnel button closes any open prompt. |
| Token buckets, double-tap guards, the AI circuit-breaker state | process memory | Reset (harmless). |
| Running payment pollers | `PaymentWatchers` | Polling stops; "Проверить оплату" (payment id in its callback data) starts a new one. |
| The wrapper's tariff list | a short-TTL cache (`TARIFF_CACHE_TTL_S`, default 300 s) | Re-read. After the TTL a wrapper outage is an **error**, never yesterday's prices. |

**Consent is not remembered by the bot**: every gated update reads it from the wrapper (`GET /me`), so it survives restarts and an
opt-out applies at once. What the bot *does* record is that a person **accepted the funnel's data processing**: that is the
existence of their lead row (created by the consent button, deleted by `/stop`).

Everything a button needs to work after a restart is **in its callback data** (`callbacks.py`) or in the lead row (`step`).

## 3. Flows

### 3.1 Consent and `/start`
1. `/start [payload]`. Payload: `ref_<code>` (`[A-Za-z0-9_-]{1,60}`) -> referral code (**carries no source**); `yt` / `tg` / `site` -> a
   **source** (first-touch attribution; the reserved tokens); exactly `paid` -> payment screen; anything else is ignored and logged.
2. Consent is read with `GET /me` (read-only). For a consented person the bot sends `POST /start {language, referral_code?}`. For anyone
   else **nothing is written to the wrapper and nothing to the funnel database**.
3. Not consented (never, or opted out): the consent text with the links and the "Согласен" button. A referral code or a source token from
   the link is put **into the button's callback data** (`cn:<code>` / `cn.<source>`): nothing is held anywhere else, nothing is sent or
   stored before consent. The text links the offer, the referral terms (offer section 10) and the privacy policy section 11 **plus
   the clauses for the funnel's processing** (11.9 identity and source, 11.10 answers and free text, 11.11 objections / tariff /
   timestamps, 11.12 purposes, 11.13 manager, 11.14 reminders, 11.15 AI assistant (kie.ai) and its log, 11.16 `/stop` deletion and the
   retention period).
4. Pressing the button sends `POST /start {consent: true, referral_code?}`, shows the wrapper's referral outcome in one plain sentence, then
   branches (`handlers/start.py::_funnel_or_menu`):
   - linked **with active paid access** (`GET /me` `tenant.paid_access_active`) -> **skips the sales funnel**: the existing menu. An existing
     lead row (if any) becomes `paid` and M5.6 is sent once (isolated hook);
   - everyone else (unlinked, or linked without active paid access) -> the funnel: the reminders opt-in first (if not answered yet), then
     M1.1. A lead row is created here, with `source` first-touch.
5. A person who consented to the **wrapper** before the funnel existed has no lead row: their next `/start` shows the consent text once more
   (the processing is new), and the button creates the row. Their old features (`/tariffs`, `/referral`, ...) work without it.
6. A returning lead (`/start` again, or a stale button) resumes from `funnel_leads.step`. A `handoff` lead gets a short note; a `paid`
   lead gets the menu; `cold` / `blocked` leads are woken by any activity.
7. `ConsentMiddleware` (one `GET /me` per update) gates every other handler: unconsented people get the consent screen (callbacks: a toast first).
   Exempt: `/start`, the consent button, `/stop` (and the manager's `/paid`, which is authorised by chat id inside its handler).

The **reminders opt-in** is its own explicit step after consent: "Напоминания (по желанию)... не больше трёх коротких напоминаний... отписаться
в любой момент... /stop остановит и их. Напомнить?" with `Да, напоминать` / `Нет, не нужно`. "No" blocks nothing. It is stored as
`reminders_opt_in` (null = not asked yet). `remind_ok` ("Сохранить подбор и напомнить позже") never replaces it.

### 3.2 `/stop`
1. `POST /opt-out` to the wrapper (unchanged). 2. **Delete all of that person's funnel data** (`PgStore.purge_user`, one transaction): events,
objections, AI-log rows, the message buffer, pending follow-ups and the lead row; then insert the tombstone. Both steps run regardless of the
other's outcome. A failed wrapper call is shown as a failure (never as success); a failed deletion is shown too ("не удалось удалить ваши
ответы... напишите в поддержку") and logged at ERROR.
The **tombstone** is a `funnel_leads` row with only `tg_id`, `status = stopped`, timestamps: reminders can never resume. If the person
consents again later, the tombstone is replaced by a brand-new lead (fresh opt-in question, fresh first-touch source).
Telegram `Forbidden` on any send is caught in `ui.say` and in the guard middleware: `POST /blocked {blocked: true}` to the wrapper and the
funnel status `blocked` (reminders cancelled, no further tries); the wrapper clears it on the next `/start`.

### 3.3 Account link
Unchanged: email -> `POST /link/request-code` -> code -> `POST /link/verify`; the tariff that started the flow travels in the dialogue state
and the "request again" button (`lk:<tariff_id>`); after a successful link the bot continues with that tariff.

### 3.4 Tariffs, promo, quote, checkout
Unchanged: `GET /tariffs` -> list -> pick -> `GET /me` (unlinked: link flow first) -> optional promo -> `POST /billing/quote` -> payable amount =
`promo.price_rub` or `price_rub` -> `pay:<tariff>:<amount>[:<promo>]` -> `POST /billing/checkout {tariff_id, promo_code?, return_url}`.
`return_url = https://t.me/<bot_username>?start=paid`; a quote with `active_tariff_mismatch: true` shows "сейчас действует другой тариф" and no pay
button; the checkout `409` stays the fallback; double tap = one in-flight checkout per (user, tariff, promo) + 3 s cooldown + the wrapper's own guard.
**The funnel's pay buttons enter exactly this flow** (3.8).

### 3.5 Payment result
Unchanged: a bounded background poller (3 s, x1.5, capped at 30 s, at most 10 minutes, at most 200 at once) on `GET /billing/payments/{id}`;
`succeeded` + `credited` -> success message with tariff and expiry from `GET /me`; `canceled`, `succeeded` but not credited, still `pending`, five
failures -> the matching messages with the check button; `/start paid` uses `GET /billing/payments/latest`. A person who opted out is not messaged.
**New:** after the success message, the funnel hook `on_payment_success` runs (isolated): from `GET /me` `paid_access_active` (never from anyone's
word) the lead becomes `paid` and **M5.6 is sent exactly once** (`paid_announced` is claimed atomically).

### 3.6 Referral
Unchanged (`GET /referral`, withdrawal requests, no payout details ever).

### 3.7 Throttling and input
Per Telegram user, in memory: token bucket (burst 6, one more every 2 s); wrapper `429` -> "retry in N s/min"; text over 1000 characters refused; callback
data parsed by strict regexes (`callbacks.parse`: the funnel's `f:<code>` is a closed set, `rm:yes|no`, `cn`, `cn:<ref>`, `cn.<source>`).

### 3.8 The funnel (`funnel/flow.py`)
State is `funnel_leads.step` + `status`, never memory. Steps: `reminders_ask`, `greet`, `how`, `examples`, `ask_free`, `s1 s2 s3 p1 p1_other i1 n1`, `rec`,
`det_a det_b det_c`, `close`, `obj`, `obj_repeat`, `discount`, `pay:<role>`, `handoff`, `end`. Statuses: `new in_spin recommended link_sent handoff paid cold blocked stopped`.

- **Stage 1 contact** (M1.1-M1.4): greeting with a source line (`yt`/`tg`/`site`), how it works, four example links, free-question mode.
- **Stage 2 SPIN**: S1, S2, S3 (optional, free text or skip), P1 (+ "Другое" -> one phrase), I1 (lead-in varies by P1), N1, with the PDF callback names and a
  Back button. **Any free text outside S3 / P1-other is a free question**: answered by the AI, then "Продолжить подбор" (`resume`) re-sends the same step.
  An answer button from an earlier message is never applied out of order ("кнопка устарела", the current step is shown again).
- **Tariff choice** (`funnel/rules.py`): the PDF section 5 rule table, **top to bottom, first match**, giving a *role*; the tariff, price and every limit come from the
  wrapper. **Known defect of the source table (reported to the owner, left as written):** rule 6 (Ultra) can never match, because every answer set that satisfies it
  also satisfies rule 5 above it, so **Ultra is never recommended**; `tests/test_funnel_rules.py` pins this on purpose.
- **Stage 3**: M3.1 (recommendation: goal phrase + limits sentence + price/period + "Автопродления нет"), M3.2a/b/c (what the service does, what the tariff contains, the
  honesty block), M3.3 (text comparison, no tables). The figures of the limits sentence are `capacity.*` rendered by `funnel/render.py`; the period label comes from
  `capacity.window_hours` / `period_days`, **never from a constant** (the entry tariff's counts are per 168 h window, the monthly ones per day; `daily_job_limit` is never read).
- **Stage 4 objections**: M4.0 and a templated answer per code (`obj_price obj_quality obj_hard obj_ban obj_gpt obj_time obj_roi obj_refund obj_think`, with the branch buttons
  `q_* h_* g_*`). Free text: the AI only returns the objection code, **the script speaks**. The same objection a second time -> the bot does not repeat itself, it offers a human (the
  objection is kept on record; the hand-off reason is `repeat_objection`).
- **Deterministic triggers** (`funnel/triggers.py`, run before the AI so they work when it is down): other payment methods (Visa / crypto / invoices / legal entities) -> hand-off
  (`payment_method`); discounts, promotions, promo campaigns -> M5.5 (the promo field is the one in the in-bot checkout; **the bot never promises a discount**) with an optional
  hand-off; refund details / payment disputes / a double charge -> hand-off, HIGH priority; complaints / a sharp tone -> hand-off, HIGH priority, the AI does not argue.
- **Stage 5 closing and payment**: M5.1; `pay_main` / `try_lite` (the entry tariff) show M5.2 (how in-bot payment works); `go_pay` **enters the existing in-bot checkout** with the tariff
  preselected (3.4: link flow if unlinked, optional promo, quote, payment button, status polling), sets `link_sent` and schedules M5.3. Nothing sends anyone to the website.
- **Hand-off** (M5.4): a card to `MANAGER_CHAT_ID` (name, @username, source, the SPIN answers, the recommended role and tariff id, the objections shown, reason, priority, the last five free-text
  messages; each answer clipped, HTML-escaped). Status `handoff`; reminders and sales stop; simple questions are still answered; old buttons never restart sales. Every mention of the manager
  uses `SUPPORT_CONTACT`; the "write to the manager" button is `https://t.me/<handle>` built from it (no button when it is not a plain `@handle`). If sending the card fails: ERROR log and the
  person is told to write to `SUPPORT_CONTACT` directly (the status stays, they can retry).
- **`/paid <tg_id> [note]`** (manager only): marks a lead paid for a non-bot payment (`paid_by_manager`, the note appended to `notes`), cancels reminders, sends M5.6 once. **Fail closed**:
  authorised strictly by chat id == `MANAGER_CHAT_ID` (a private chat with that id, or that group); anyone else is ignored without a reply and logged at INFO (`paid_command_denied`).
- **The M5.6 text** says the closed training group is included in the tariff and access is clarified with the manager; it does not say who adds anyone.

### 3.9 The AI layer (`ai/`)
**Interim MVP choice (stated in README, in `ai/__init__.py` and in the env table):** the provider and model (**kie.ai**, `LLM_MODEL`, currently `gemini-3-8-flash-openai`), the fact that there is a
**single provider with no failover**, are strictly interim. The YouTubeGen core has not yet been profiled or debugged for availability and cost; reducing LLM cost and improving availability is the planned
next task, after which the provider / model / failover decision must be revisited.

It is used in **three places only**: a free question, the objection-code classification of free text, and text outside the scenario. It cannot pick a tariff, state a price or move anyone through the funnel.

- **The call**: `POST {KIE_API_URL}/{LLM_MODEL}/v1/chat/completions` (`Authorization: Bearer <KIE_API_KEY>`, `stream: false`, low temperature, bounded `max_tokens`, timeout `LLM_TIMEOUT_S`), **at most one retry** with a short
  backoff (only for retryable failures: 429/5xx/timeouts/soft errors). The message carries the **current question and the funnel step name only** (no history, no other user data); the question is delimited and
  declared to be data (the delimiters cannot be forged). The system prompt is the PDF section 10 prompt plus the owner's additions (never claim frame checking / computer vision, never mention auxiliary scenes, channel
  count only as the knowledge-base sentence, no dollar figures); `{{KB}}` is rendered per call from (a) `knowledge/knowledge_base.toml` and (b) a **live tariff block** built from the wrapper's tariffs.
  **If the tariff block cannot be built, the LLM is not called** (fallback + ERROR log).
- **Every one of these is a FAILURE, never an answer**: HTTP != 2xx; HTTP 200 whose body has `code >= 300` or no `choices` ("Network error"); an empty or malformed completion; `finish_reason` other than `stop`
  (`length` = truncated); a canned gateway error sentence as the whole reply or glued to the end of a real answer; a network error; a timeout. (`finish_reason` is compared case-insensitively; a missing one is accepted.)
- **Strict JSON** `{reply, objection_code, handoff, reason}` (`objection_code` in the closed set or `none`, `handoff` a real boolean, `reply` non-empty and bounded): anything else (prose around the JSON, a code fence, a missing key,
  truncated JSON) is a **parse failure** -> fallback + ERROR log.
- **Output validator** (`ai/validator.py`) on every reply before it is sent: each number next to a unit (money, videos, clips, days, languages, tracks, channels, minutes, `%`, `$`...) must appear in the KB text rendered for **that same
  call**; the forbidden-phrase list (guarantees, "никогда не", a hundred percent, no risk, no demonetisation, protected, safe for YouTube, publishing to YouTube (negations like "не публикует" pass), scarcity, computer vision / frame checking,
  auxiliary scenes, dollars, percentages) blocks it. A blocked reply is replaced by "Тут лучше уточнить у менеджера." + the hand-off button; WARNING log and outcome `validator_blocked`. A number spelled in words is not checked.
- **Fallbacks**: provider failure / parse failure / breaker open / ceiling -> "Сейчас не могу ответить, напиши менеджеру {SUPPORT_CONTACT}." + the hand-off button + an ERROR log (`ai_provider_error`, `ai_parse_failed`, ...);
  per-person limit -> a short "кнопками" message (INFO). All outcomes are written to `funnel_ai_log` (`ok`, `validator_blocked`, `parse_failed`, `provider_error`, `rate_limited`, `ceiling`).
- **Limits** (defaults in `config.py`, tunable): `AI_USER_HOURLY_LIMIT` (10 questions per person per rolling hour) and `AI_DAILY_CEILING` (500 LLM calls per UTC day). Both are counted from `funnel_ai_log` (so they survive restarts); calls that were
  never made (breaker open, no tariff block) are not counted. A person's hourly count includes their failed calls.
- **Circuit breaker**: `AI_BREAKER_FAILURES` (5) consecutive *provider* failures skip the layer for `AI_BREAKER_COOLDOWN_MIN` (10) minutes (fallback immediately, no HTTP call). A bad *answer* (parse / validator) is not an outage and does
  not count. State is per process. **ONE Telegram message to `MANAGER_CHAT_ID`** when the breaker opens, and ONE when the daily ceiling is first reached that UTC day (decided from the database, so a restart does not repeat it); a failing alert is
  an ERROR log, never a gate.
- **CLIs**: `python -m ytgen_bot ai-sample --n 20` prints a random sample of the AI log for the weekly review (it prints the people's own questions: owner's terminal only). `python -m ytgen_bot ai-replay` runs ~25 typical and adversarial
  questions (demonetisation / ban guarantees, income promises, discounts / trial / free access, "does it publish to YouTube", competitor comparison / prices, prompt injection, legal / tax advice, another language, frame checking / computer vision,
  channel count, auxiliary scenes, dollars, other payment methods, a complaint) through the **real model and the real validator** and prints pass/fail per case; it needs `KIE_API_KEY` and network access, writes nothing to the database, and is run
  once by the owner in his own terminal. The unit tests use recorded provider replies (fixture-replay convention); passing the replay is evidence about those questions, not a guarantee.

### 3.10 Reminders (`funnel/reminders.py`)
All of F1-F3 **require `reminders_opt_in`**. The schedule is a pure function of the lead row (`next_followup`), stored as at most one pending row per person in `funnel_followups` and recomputed after every change:
- **F1**: 24 h after the last activity, once, for a lead who abandoned SPIN (`in_spin`) or did not respond after the recommendation (`recommended`).
- **F2** (3 days after F1) and **F3** (7 days after F2): only for a lead who pressed `remind_ok` or answered F1 (any activity after it); never sooner than 24 h after their last activity. After F3 the status is `cold`.
- **M5.3** ("Получилось оплатить и войти?"): 30 minutes after `go_pay`, once, for a `link_sent` lead who opted in. Before sending, the bot asks the wrapper: if `paid_access_active` the lead becomes `paid` and M5.6 is sent instead.
- Any user activity, hand-off, paid, `unsub`, `/stop` or a blocked bot cancels what is pending. No invented scarcity: nothing like "осталось мест" or "акция до вечера" (a test scans the sent texts). F3 shows the entry tariff's price from the wrapper.
- **The loop** (`ReminderLoop`, in the bot process, no external scheduler): every 30 s it **claims** due rows (`UPDATE ... state='claimed' ... RETURNING`, `FOR UPDATE SKIP LOCKED`) and then sends. **Delivery is at-most-once:** a crash between the claim and
  the send loses that one message instead of ever sending it twice. Before each send it re-validates the lead and asks the wrapper (`GET /me`): withdrawn consent -> cancelled; wrapper unreachable -> ERROR log and the row is re-scheduled 10 minutes later.
  Sends are paced (0.25 s apart, far below Telegram's limits). Telegram "bot blocked by the user" -> status `blocked` + the existing `POST /v1/bot/blocked`, no further tries.

### 3.11 Retention
- `/stop`: everything of that person is deleted, a tombstone stays (3.2).
- `funnel_ai_log` rows older than **90 days** are deleted by an in-process loop (`app.purge_loop`: at startup and every 6 h, idempotent; a failure is an ERROR from the store and the loop carries on).
- The message buffer is trimmed to the last five messages per person on every insert. Everything else is kept until `/stop`.

## 4. Error map (wrapper -> user)
Unchanged: implemented in `errors_ru.py`; `DOCUMENTED_CODES` lists every code per context and `tests/test_errors.py` pins each to a specific message (link request, link verify, quote / checkout, payment,
withdrawal, operator-side codes with a generic user text and an ERROR log, anything else: 5xx -> "service temporarily unavailable", unknown 4xx -> generic **and** `wrapper_error_unmapped` at ERROR, a 2xx with an unexpected shape ->
`wrapper_contract_mismatch` at ERROR). Funnel errors: a `StoreError` inside a funnel flow -> "Что-то пошло не так. Напиши менеджеру: {SUPPORT_CONTACT}." (+ button); a tariff contract problem -> an ERROR log and the manager fallback text.

## 5. Data model (Postgres; `db/schema.py`, migrations in `db/migrations/versions/`)

| Table | Holds |
|---|---|
| `funnel_leads` (PK `tg_id`) | `username`, `first_name`, `source` (first touch), `status`, `step`, the SPIN answers `stage volume niche_text pain pain_text impact goal`, `rec_role` + `rec_tariff_id`, `handoff_reason/at/priority`, `remind_ok`, `reminders_opt_in`, `followups_sent`, `answered_followup`, `last_followup_at`, `unsub`, `paid_tariff`, `paid_at`, `paid_by_manager`, `paid_announced`, `pay_check_sent`, `notes` (manager), `last_activity_at`, timestamps, `stopped_at` |
| `funnel_events` | `ts`, `tg_id`, `event`, `value` - **button codes and short tokens only, never free text** |
| `funnel_objections` | `tg_id`, `code`, `ts` |
| `funnel_ai_log` | `ts`, `tg_id`, `step`, `question`, `reply`, `objection_code`, `handoff`, `reason`, `model`, `latency_ms`, `outcome` (the owner's weekly-review table; 90-day retention) |
| `funnel_messages` | the rolling buffer: the last 5 free-text messages per person (for the manager card) |
| `funnel_followups` | the reminder schedule: `kind` (`f1 f2 f3 m53`), `due_at`, `state` (`pending claimed sent cancelled`); a partial unique index allows one `pending` row per person |

No table holds a price, a limit, a period, an email, a code or payout details. **Migration rule: additive only** - new tables, new nullable/defaulted columns, new indexes; never drop, rename or retype - so a rollback to the previous image runs
unchanged on the migrated schema (the previous image simply ignores the tables; the compose `db` / `migrate` containers stay as orphans, which is harmless). `tests/test_db_store.py` enforces it.
Migrations are applied before the bot starts by the one-shot `migrate` service (`python -m ytgen_bot migrate`): idempotent (a database at head is a no-op), serialised by a Postgres advisory lock, and any failure exits non-zero, which fails the deploy.

`python -m ytgen_bot funnel-report` prints the PDF section 12 metrics from these tables: start -> end of SPIN, drop-off by last step, recommendation -> `link_sent`, objection frequency (and how many reached `link_sent`), hand-off reasons,
source -> paid.

## 6. Backup
`scripts/backup_db.sh` (modelled on the wrapper's): `docker compose exec -T db pg_dump -U <user> -Fc <db>` -> `rclone copy` to `RCLONE_REMOTE` -> prune that database's dumps older than `BACKUP_RETENTION_DAYS` (default 14). Dump names are
`<db>_<YYYYmmdd>_<HHMMSS>_<token>.dump` (UTC; 8 hex chars) - exactly what ytgen-infra's backup watchdog matches. It **reads** `POSTGRES_USER`, `POSTGRES_DB`, `RCLONE_REMOTE` (and an optional `HEALTHCHECKS_PING_URL`) from the checkout's
`.env` without sourcing it, so the watchdog can check the same file. An empty database dumps to ~17 KB (above the watchdog's 10 KB "suspiciously small" floor). It does not install itself: the owner (or the orchestrator) installs the cron, see 11.

## 7. Invariants (do not "fix" these away; the amended list is in `CLAUDE.md`)
- **Stateful for funnel data only**; the wrapper stays the system of record. No published port anywhere.
- **Isolation**: a funnel / DB / LLM failure never breaks checkout, payment status, link, referral or withdrawal (3.8, `tests/test_funnel_stop_isolation.py`).
- **No money or limits in the bot** (`tests/test_no_constants.py`, `tests/test_knowledge_base.py`).
- **Consent first**; funnel data only after consent; reminders need their own opt-in.
- **No payout details.** **Fail loudly.** **Secrets and PII** (`BOT_TOKEN`, `BOT_SERVICE_API_KEY`, `BOT_DB_PASSWORD`, `KIE_API_KEY`, `MANAGER_CHAT_ID` are env-only and redacted from logs; free text never at INFO/DEBUG; logs identify people by Telegram id only).
- **No automatic deploy.**

## 8. Logging
JSON lines on stdout (`logging_setup.py`): `ts`, `level`, `logger`, `msg` (an event name such as `checkout_created`, `funnel_store_error`, `ai_provider_error`) plus fields (`tg_user`, `payment_id`, `code`, ...). A redaction filter replaces the configured
secrets, bot-token shapes and email addresses even in tracebacks. httpx/httpcore are held at WARNING (they log full URLs). **ERROR** is used for: a store failure, an AI provider / parse failure, a tariff contract problem, an unmapped wrapper code, a failed
hand-off card, a crashed background task; **WARNING** for a validator block; **INFO** for rate limits and a denied `/paid`. No free text, name, handle or email appears in any funnel log line (`tests/test_funnel_privacy.py`).

## 9. Contract notes (what the bot relies on; the wrapper repo is not edited from here)
Everything from the previous version stands (wrapper `1abbd66` `GET /me` state and `GET /billing/payments/latest`, `a36c9ac` `active_tariff_mismatch`, `BOT_DEEP_LINK_BASE` must name this bot, the self-check uses the bot's own id, consent is read on every gated update).

**Tariffs (wrapper `0fe0f06`, verified against the wrapper's code and README "Telegram bot API" on 2026-10-22, and against the live `GET /v1/public/tariffs`):** every tariff object carries top-level `is_monthly: bool`
(`period_days == 30`, the predicate the wrapper's upgrade rules use) in `/v1/bot/tariffs`, `/v1/public/tariffs` and `/v1/tariffs`. Per public tariff the funnel reads `id name price_rub period_days is_monthly max_ai_scenes_per_video` (`null` = uncapped) and `capacity`
`{window_hours, ai_video_videos_per_window, ai_video_scenes_per_day, images_per_day, stock_per_day, image_stock_videos_per_day | image_stock_videos_per_window}`.
- **Traps handled**: `daily_job_limit` is per *window* (not per day) on the short plan, so the funnel never reads it; the period label comes from `capacity.window_hours` / `period_days`; the AI-video count is per *rolling* window.
- **Fail loudly**: a row without `is_monthly`, without `capacity`, or with an incomplete `capacity` is an ERROR (the wrapper omits `capacity` from a half-configured row and logs it); the deploy self-check fails on it. A list that does not have exactly one
  non-monthly and three monthly tariffs with distinct prices cannot be assigned roles: ERROR + manager fallback for the recommendation (the self-check logs it but does not block the deploy). Nothing is defaulted.
- **Roles by rank**: entry = the cheapest non-monthly tariff; base / pro / ultra = monthly tariffs by ascending price. Names come from the API.
- **The funnel reads the public list** (`GET /v1/public/tariffs`, unauthenticated, no per-user event) through a separate HTTP client; the per-user `/v1/bot/tariffs` (which records a `tariff_view` event) is used only by the `/tariffs` screen.
- **No upgrade route**: the wrapper has extra upgrade conditions the PDF omits and the bot has no upgrade endpoint, so **no funnel text states any upgrade rule** (nothing like "можно повысить тариф" or "Lite нельзя повысить").
- `GET /v1/bot/me`: `linked`, `tenant.paid_access_active`, `tenant.tariff_name`, `tenant.tariff_expires_at`; unlinked is `200` with `linked: false`.

## 10. Environment (names only; values are never committed)

| Variable | Secret | Required | Set by | Meaning |
|---|---|---|---|---|
| `BOT_TOKEN` | yes | yes | owner: GitHub secret | Telegram bot token. |
| `BOT_SERVICE_API_KEY` | yes | yes | owner: GitHub secret (same value on the wrapper repo) | The shared bot key (>= 32 chars). |
| `BOT_DB_PASSWORD` | yes | yes | owner: GitHub secret **new** | The funnel Postgres password (`openssl rand -hex 24`, >= 16 chars of `[A-Za-z0-9_-]`). Read by the `db` image **only on the first start of an empty volume**; changing it later needs `ALTER USER`. |
| `KIE_API_KEY` | yes | yes | owner: GitHub secret **new** | kie.ai API key (AI layer). Never printed, never validated through an endpoint that echoes it, **not used by the self-check**. |
| `MANAGER_CHAT_ID` | yes | yes | owner: GitHub secret **new** | Numeric Telegram chat id of the manager (a private chat: the manager's user id, who must have pressed Start; or a group: negative id). Receives hand-off cards and AI alerts; the only chat that may use `/paid`. |
| `WRAPPER_BASE_URL` | no | yes | `.env.production` | Wrapper base URL as seen from the container (`https` only; plain `http` for loopback). |
| `SUPPORT_CONTACT` | no | yes | `.env.production` | The manager's `@handle`: every user-facing mention and the t.me button are built from it. |
| `POSTGRES_DB`, `POSTGRES_USER` | no | yes | `.env.production` | Funnel database name and user (also read by `backup_db.sh` and the backup watchdog). |
| `POSTGRES_HOST`, `POSTGRES_PORT` | no | no | default | `db` / `5432` (the compose service). |
| `KIE_API_URL` | no | yes | `.env.production` | kie.ai base URL (`https`). |
| `LLM_MODEL` | no | yes | `.env.production` | Model slug (`gemini-3-8-flash-openai`). **Interim MVP choice, single provider, no failover** (3.9). |
| `AI_USER_HOURLY_LIMIT` | no | no | `.env.production` | Free questions per person per hour, default 10. |
| `AI_DAILY_CEILING` | no | no | `.env.production` | LLM calls per UTC day across everyone, default 500. |
| `LLM_TIMEOUT_S`, `LLM_MAX_TOKENS`, `LLM_TEMPERATURE` | no | no | default | 25 s, 600 tokens, 0.2. |
| `AI_BREAKER_FAILURES`, `AI_BREAKER_COOLDOWN_MIN` | no | no | default | 5 consecutive provider failures, 10 minutes. |
| `TARIFF_CACHE_TTL_S` | no | no | default | 300 s. |
| `RCLONE_REMOTE` | no | yes (render check) | `.env.production` | Offsite backup path for `backup_db.sh` (own path for this project). |
| `LOG_LEVEL`, `HEARTBEAT_FILE`, `DOCKER_LOG_MAX_SIZE`, `DOCKER_LOG_MAX_FILE` | no | no | default | As before. |

The bot **fails at startup, naming the variable (never the value)**, if a required variable is missing or malformed; `scripts/render_env.sh` fails the **deploy** by name, before anything is touched, if a secret is empty or a required plain value is
missing. Secrets must not contain whitespace or `$ " ' \` ` (compose would interpret them). **The provider/model note again:** kie.ai + `gemini-3-8-flash-openai`, single provider, no failover is strictly an interim MVP choice (the YouTubeGen core has not been
profiled or debugged for availability and cost; reducing LLM cost and improving availability is the planned next task, after which this decision must be revisited).

## 11. Owner setup (one time; secret values are only ever typed in the owner's own terminal)

```bash
# 1. Telegram bot + shared key (done 2026-10-22): BOT_TOKEN, BOT_SERVICE_API_KEY on this repo and the wrapper repo.
# 2. THREE NEW secrets on this repo (never paste values into a chat or a transcript):
openssl rand -hex 24 | gh secret set BOT_DB_PASSWORD -R Danila8889/ytgen-bot      # nothing is printed
gh secret set KIE_API_KEY -R Danila8889/ytgen-bot                                 # paste the kie.ai key at the prompt
gh secret set MANAGER_CHAT_ID -R Danila8889/ytgen-bot                             # the numeric chat id; the manager must press Start in the bot first
gh secret list -R Danila8889/ytgen-bot                                            # must now show the three names
# 3. Verify the wrapper side the funnel relies on (every public row must carry is_monthly):
curl -s https://api.ytgenerator.ru/v1/public/tariffs | python3 -c 'import json,sys; print(all("is_monthly" in t for t in json.load(sys.stdin)))'
# 4. Deploy:
gh workflow run ci.yml --ref main -R Danila8889/ytgen-bot
# 5. Once, in your own terminal with the full environment (needs KIE_API_KEY): python -m ytgen_bot ai-replay   (or run it in the container)
```

**Backup cron (the exact steps; do not skip: this is the only copy of the funnel data).** On the VPS, as the user that runs docker and has the rclone config for the `beget` remote (the same user as the wrapper's backup cron):
```bash
cd /opt/actions-runner-ytgen-bot/_work/ytgen-bot/ytgen-bot        # the deployed checkout (it holds the rendered .env)
./scripts/backup_db.sh && echo ok                                  # one manual run first; expect "backup ok: <db>_<date>_<time>_<token>.dump"
sudo touch /var/log/ytgen-bot-backup.log && sudo chown "$USER" /var/log/ytgen-bot-backup.log
crontab -e                                                         # add this single line (04:00 host time, after the core 03:00 and wrapper 03:30 backups):
0 4 * * *  cd /opt/actions-runner-ytgen-bot/_work/ytgen-bot/ytgen-bot && ./scripts/backup_db.sh >> /var/log/ytgen-bot-backup.log 2>&1
```
Then extend ytgen-infra's `scripts/backup_watchdog.sh` `TARGETS` with `"ytgen-bot|/opt/actions-runner-ytgen-bot/_work/ytgen-bot/ytgen-bot/.env|POSTGRES_DB|26"`, re-install it to `/usr/local/sbin/ytgen-backup-watchdog.sh`, and run it with `--dry-run`
(`ytgen-infra` is not edited from this repo). A restore drill: `docker run --rm -i postgres:16-alpine pg_restore --list < <dump>` lists the archive; a real restore is `docker compose exec -T db pg_restore -U <user> -d <db> --clean --if-exists < <dump>`.
`.env` is rewritten by every deploy, so `RCLONE_REMOTE` lives in `.env.production` (committed): change it there.

Runner (same convention as the sibling repos, registered 2026-10-22): service `actions.runner.Danila8889-ytgen-bot.ytgen-bot-vps-runner`, directory `/opt/actions-runner-ytgen-bot`, label `ytgen-bot-vps`. The runner also runs the `test` job, which now starts a throw-away
`postgres:16-alpine` container on `127.0.0.1` with a random high port (removed at the end of the run); the first run pulls the image.

## 12. Operations
- Logs: `docker compose logs -f bot` (also `db`, `migrate`). Health: `docker compose ps` (the bot's healthcheck reads the heartbeat file; `db` has `pg_isready`).
- A restart loop with `self_check_failed` means: wrong/absent `BOT_SERVICE_API_KEY` on either side, the wrapper's bot API switched off, or the wrapper unreachable. The **deploy** also fails (and rolls back) on: the database unreachable or not at head, a public tariff row
  without `is_monthly` / `capacity`, the `migrate` service exiting non-zero. The reason is in the log line, never a secret.
- **The bot starts even when the funnel database is down** (ERROR `funnel_db_unavailable_at_startup`): checkout, payment status, link, referral and withdrawal keep working; funnel flows tell people to write to the manager. Fix the database, no restart needed (the pool reconnects).
- `python -m ytgen_bot funnel-report`, `ai-sample --n 20` (weekly: read the answers where the bot said it does not know and extend the knowledge base), `ai-replay` (after any prompt / knowledge-base / model change).
- Rollback is automatic on a failed deploy (README "Deploy"); a bad commit that passes the self-check is fixed forward through git.

## 13. Facts that can go stale (static knowledge base, `knowledge/knowledge_base.toml`)
Copied from the third-party funnel spec of 2026-10-22 (its own sources: the site, the offer, the READMEs, channel posts); **not independently verified in this repo**. Re-check when the product changes: the dubbing fact (up to 4 additional languages of 12, MP3 / SRT / TXT, on-screen text not
translated, daily limit, included in every tariff); the competitor-analysis fact (1-5 links, the "Библия канала" draft); the music fact (instrumental tracks, up to 5 per channel, a generation limit); the model names (Veo 3.1, Gemini Omni Flash, Nano Banana Pro, Claude 5, ElevenLabs); the process
description; voice cloning / preview; "Несколько каналов доступны на любом тарифе."; "все функции на любом тарифе"; "поддержка и закрытая группа входят в каждый тариф" and "доступ уточняется у менеджера"; "Автопродления нет" (policy); the entry tariff's "остаток не переносится"; the no-trial statement;
"моушн-графика в разработке"; the payment fact (YooKassa page, receipt by email); the refund fact (offer section 4); the contacts; the four example links (Google Drive, not checked by the spec's author). Tariff money and limits are **not** in this file.

## 14. Deviations from the PDF spec and decisions
Mandatory (owner): no claim of computer vision / frame checking or of automatic regeneration (only "any scene can be regenerated manually, by prompt"); no auxiliary scenes; no dollar approximations and no "≈ N минут" figures; channels only as the site FAQ sentence; M5.6 does not say who adds a person to the closed
group; no promised manager response time; no upgrade rules; `@togn15` replaced by `SUPPORT_CONTACT`; the payment path is the in-bot checkout (M5.2 / M5.3 / M5.5 adapted); payment methods only what the checkout offers, other methods -> hand-off; "Автопродления нет" kept.
Decisions taken while building (flagged in the task report): the rule-6 (Ultra) shadowing is implemented as written; `remind_ok` does not replace the reminders opt-in; M5.3 requires the reminders opt-in; "Сохранить подбор" / `end_polite` sets `cold`; the clarifying sub-branch of the price objection (manual work vs another service) is not implemented
(the same objection a second time goes to a human); trigger 8 of the hand-off table ("ready to pay and asks clarifying questions") is only reachable through the AI's `handoff` flag and the "Нужна помощь с оплатой" button; people who consented to the wrapper before the funnel existed accept the new processing once.
