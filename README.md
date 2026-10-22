# ytgen-bot (YouTubeGen Bot)

A Telegram bot for YouTubeGen with two jobs: it lets a customer **link their account, pay for a tariff and use the referral
programme** (a thin client of the `youtubegen-saas-wrapper` `/v1/bot/*` API), and it runs a **scripted sales funnel** for people
who have no paid access yet (a few short questions, a tariff recommendation, objection handling, a hand-off to a human, optional
reminders). An LLM is used only for free questions and for labelling an objection; the script decides everything else.

> **The product itself is not used inside Telegram.** Video generation and everything else happens on the website
> (ytgenerator.ru); the bot handles account linking, payment, the referral programme and the sales conversation. The bot's `/help` says the same.

- Stack: Python 3.12+, aiogram 3, httpx, SQLAlchemy (asyncpg) + Alembic, uv. Long polling. **No public port** (and no service of the
  compose stack publishes a host port).
- **The wrapper is the system of record** for accounts, consent-to-wrapper, billing and referrals. The bot never talks to YooKassa or
  to YouTubeGen core. **The bot is stateful for funnel data only**: its own Postgres holds lead profiles, events, objections, the AI log,
  a buffer of five free-text messages per person and the reminder schedule (see "What the bot remembers" in `documentation.md`).
  It stores no money, price, limit, email, code or payout detail.
- All bot-facing text is Russian ("ты" in the funnel, "вы" in the legal texts). No price, limit or percentage is written in the code:
  every figure shown comes from a wrapper response (non-tariff product facts live only in the static knowledge base, each with a source).

## What users can do

| Command / button | What happens |
|---|---|
| `/start` | Consent step first (links to the offer, referral terms = offer section 10, and the bot's data processing = privacy policy section 11 with clauses 11.9-11.16 for the funnel). Nothing else works until the user presses "Согласен". Then the **reminders opt-in** (Yes / No, separate from consent), then the funnel. People who already have paid access skip the funnel and get the menu. |
| `/start yt`, `/start tg`, `/start site` | Source tokens: the greeting gets a source line and the source is stored (first touch, never overwritten). |
| `/start ref_<code>` | As `/start`, and the referral code is sent to the wrapper (after consent). A referral payload carries no source. |
| `/start paid` | Where the payment page returns the user: shows the real status/result of their latest payment. |
| the funnel | M1 contact -> SPIN questions (situation, volume, niche, pain, impact, goal; Back; free text mid-way = a free question, then "Продолжить подбор") -> a tariff **recommended by a rule table over the wrapper's tariffs** -> presentation (what you get, what the tariff contains, an honest "what to know" block, a text comparison) -> objections -> closing -> in-bot payment or a hand-off to a human. State is saved: returning leads resume where they stopped, also after a restart. |
| free text | Answered by the AI from the knowledge base and the live tariff list; objections are labelled by the AI but **answered by the script**; discounts, other payment methods, refund disputes and complaints go to the manager. |
| pay buttons | **Enter the in-bot checkout** with the recommended tariff preselected (link flow if unlinked, optional promo, quote, payment button, status polling). The funnel never sends people to the website to pay. `paid` is set from the wrapper (`paid_access_active`), not from anyone's word. |
| reminders | Up to three (F1 after 24 h, F2 after 3 days, F3 after 7 days), only with the opt-in, with "Больше не напоминать" in each; any activity, hand-off, payment or `/stop` cancels them. Plus one "did the payment work?" check 30 minutes after the pay button. |
| `/tariffs` | The tariff list as the wrapper returns it, then link -> optional promo -> quote -> payment button (as before; the `active_tariff_mismatch` message when the account has an active *different* tariff). |
| `/referral` | Linked users only: personal link, counts, balances, rules from API fields, withdrawal request. Payout details are never asked for or stored. |
| `/status` (`/me`) | Linked account (masked email) and tariff. |
| `/stop` | Opt-out (`POST /v1/bot/opt-out`) **and deletion of all your funnel data** (answers, objections, events, AI log, message buffer, pending reminders); only a tombstone (Telegram id, "stopped", a timestamp) stays so reminders never resume. Sending `/start` and consenting again starts from scratch. |
| `/help` | Commands, the product note, the support contact. |
| `/paid <tg_id> [note]` | **Manager only** (chat id == `MANAGER_CHAT_ID`; anyone else is silently ignored): marks a lead paid for a payment made outside the bot. |

## AI layer (interim MVP choice)

Provider and model: **kie.ai chat completions, `LLM_MODEL` (currently `gemini-3-8-flash-openai`), a single provider with no failover.**
**This is strictly an interim MVP choice: the YouTubeGen core has not yet been profiled or debugged for availability and cost; reducing LLM
cost and improving availability is the planned next task, after which the provider / model / failover decision must be revisited.**

Guard rails: strict JSON replies; every provider "soft error" (HTTP 200 with an error body, truncated JSON, `finish_reason=length`, gateway error
sentences) is a failure, never an answer; an output validator checks every number against the knowledge base rendered for that call and blocks forbidden
claims (guarantees, publishing to YouTube, scarcity, frame checking / computer vision, dollars, ...); the call carries the current question and the step name
only; no live tariff block, no LLM call; per-person hourly limit, a global daily ceiling and a circuit breaker (the manager gets ONE Telegram message when it opens
and when the ceiling is reached). Details: `documentation.md` section 3.9.

## Commands

```bash
uv run python -m ytgen_bot                    # run the bot (long polling)
uv run python -m ytgen_bot selfcheck          # deploy check: getMe, the wrapper, the DB at head, the wrapper's tariff contract (no live LLM call)
uv run python -m ytgen_bot migrate            # apply Alembic migrations (idempotent); the compose `migrate` service runs this before the bot
uv run python -m ytgen_bot funnel-report      # the PDF section 12 metrics: SPIN completion, drop-off, link_sent rate, objections, hand-off reasons, source -> paid
uv run python -m ytgen_bot ai-sample --n 20   # a random sample of the AI log for the weekly review
uv run python -m ytgen_bot ai-replay          # ~25 typical + adversarial questions through the REAL model and validator (needs KIE_API_KEY; run by the owner)
```

## Wrapper contract it depends on

Source of truth: the wrapper's `README.md` ("Telegram bot API", "Referral programme"), `src/api/routers/bot.py` and `src/bot/`. Base `/v1/bot`; every call
carries `X-Bot-Service-Key` and `X-Telegram-User-Id`. `POST /start`, `POST /opt-out`, `POST /blocked`, `GET /me`, `POST /link/request-code`, `POST /link/verify`,
`GET /tariffs`, `POST /billing/quote`, `POST /billing/checkout`, `GET /billing/payments/latest`, `GET /billing/payments/{payment_id}`, `GET /referral`,
`POST /referral/withdrawals`; plus the **unauthenticated** `GET /v1/public/tariffs`, which the funnel reads. Needs wrapper `1abbd66` or later (and `a36c9ac` for the early
`active_tariff_mismatch` message); **the funnel needs `0fe0f06`** (`is_monthly` on every tariff object). A tariff row without `is_monthly` or a complete `capacity` is a contract
error (deploy self-check fails, the funnel falls back to the manager). Tariff roles are assigned by **price rank among the public tariffs** (entry = cheapest non-monthly,
base / pro / ultra = monthly by price), never by name. Errors are `{"detail": {"error": <code>, ...}}`, with FastAPI's list-shaped 422 as the one exception
(see "Contract notes" in `documentation.md`). The bot has no upgrade route, so no funnel text states any upgrade rule.

The wrapper must have **both** `BOT_SERVICE_API_KEY` (equal to the bot's) **and** `BOT_DEEP_LINK_BASE` (`https://t.me/<bot username>`) set, or the bot cannot work.

## Configuration

Names only; values are never committed. `.env.example` lists them.

| Variable | Secret | Required | Set by | Meaning |
|---|---|---|---|---|
| `BOT_TOKEN` | yes | yes | owner: GitHub secret | Telegram bot token from BotFather. |
| `BOT_SERVICE_API_KEY` | yes | yes | owner: GitHub secret | Must equal the wrapper's `BOT_SERVICE_API_KEY` (>= 32 chars, `openssl rand -hex 32`). |
| `BOT_DB_PASSWORD` | yes | yes | owner: GitHub secret | Password of the bot's own Postgres (`openssl rand -hex 24`). Used by the `db` image on its first start. |
| `KIE_API_KEY` | yes | yes | owner: GitHub secret | kie.ai API key (AI layer). |
| `MANAGER_CHAT_ID` | yes | yes | owner: GitHub secret | Numeric Telegram chat id for hand-off cards, AI alerts and `/paid`. |
| `WRAPPER_BASE_URL` | no | yes | `.env.production` | Wrapper base URL **as seen from the container**. `https` only (plain `http` for loopback). |
| `SUPPORT_CONTACT` | no | yes | `.env.production` | The manager's `@handle`: shown to users and used for the "write to the manager" button. |
| `POSTGRES_DB`, `POSTGRES_USER` | no | yes | `.env.production` | Funnel database name / user (`POSTGRES_HOST` / `POSTGRES_PORT` default to the compose service `db` / `5432`). |
| `KIE_API_URL`, `LLM_MODEL` | no | yes | `.env.production` | kie.ai base URL and the model slug (the model name is config, not code; interim choice, see "AI layer"). |
| `AI_USER_HOURLY_LIMIT`, `AI_DAILY_CEILING` | no | no | `.env.production` | Free questions per person per hour (default 10) and LLM calls per UTC day (default 500). |
| `LLM_TIMEOUT_S`, `LLM_MAX_TOKENS`, `LLM_TEMPERATURE`, `AI_BREAKER_FAILURES`, `AI_BREAKER_COOLDOWN_MIN`, `TARIFF_CACHE_TTL_S` | no | no | default | 25, 600, 0.2, 5, 10, 300. |
| `RCLONE_REMOTE` | no | yes | `.env.production` | Offsite path for `scripts/backup_db.sh` (the host cron is installed by the owner, see `documentation.md` 11). |
| `LOG_LEVEL`, `HEARTBEAT_FILE`, `DOCKER_LOG_MAX_SIZE`, `DOCKER_LOG_MAX_FILE` | no | no | default | As before. |

The bot **fails at startup, naming the variable (never the value)**, if a required variable is missing or malformed.

### Reaching the wrapper from Docker

The wrapper listens on the VPS host's `127.0.0.1:8090` and is also public at `https://api.ytgenerator.ru`. `127.0.0.1` inside a bridge-network container is the container itself, and
`host.docker.internal` cannot reach a loopback-only listener; `network_mode: host` would expose the host namespace. **Chosen: `WRAPPER_BASE_URL=https://api.ytgenerator.ru`** (outbound
internet only, TLS protects the service key in transit; the deploy's self-check proves it on every deploy).

## Run, test

```bash
uv sync                                  # runtime + dev deps
uv run pytest -q                         # the whole suite: no outbound network, no secrets; needs docker (or local postgres binaries) for a throw-away Postgres
cp .env.example .env                     # fill in values, then:
set -a; . ./.env; set +a
uv run python -m ytgen_bot migrate       # (once) create the funnel tables in your database
uv run python -m ytgen_bot               # run the bot (long polling)
docker compose up -d --build             # the production-shaped way (needs .env): db -> migrate -> bot
```

The test suite starts one `postgres:16-alpine` container on `127.0.0.1` with a docker-chosen random port (never a well-known host port, never a production database) and removes it afterwards.
Locally against a wrapper on your machine use `WRAPPER_BASE_URL=http://127.0.0.1:8090` (loopback `http` is allowed).

## Deploy

`.github/workflows/ci.yml`: the `test` job runs on every push/PR to `main`. Both jobs run on the self-hosted runner labelled `ytgen-bot-vps` (GitHub-hosted jobs on this private repo do not start
while the account has a billing problem); a runner executes one job at a time and `deploy` `needs: test`, so the two cannot deadlock each other, and the test checkout goes into `.ci-test-src/` so it can never
move HEAD or clean the `.env` of the production checkout. The `deploy` job runs **only on `workflow_dispatch`** (`gh workflow run ci.yml --ref main`), after `test`:

1. `scripts/render_env.sh` renders `.env` from `.env.production` + the GitHub secrets and **fails the deploy naming the missing secret/empty value** before anything is touched.
2. `docker compose up -d --build`: `db` starts and must become healthy, the one-shot `migrate` service applies the Alembic migrations (**additive only**, idempotent; a failure stops the deploy), then the bot starts.
3. `scripts/post_deploy_check.sh`: `db` healthy, `migrate` exited 0, the bot `running` + `healthy`, and `python -m ytgen_bot selfcheck` inside it must pass (`getMe`, the wrapper, the database reachable and at head,
   the wrapper's public tariffs valid for the funnel; **no live kie.ai call**).
4. On failure of 2 or 3 the job rolls back to the previously deployed commit and re-runs the same check (`always() && !cancelled() && (...failure)`), and fails the run either way. Migrations are additive, so the previous image runs on the migrated schema.

One-time prerequisites (owner): the GitHub secrets `BOT_TOKEN`, `BOT_SERVICE_API_KEY`, **`BOT_DB_PASSWORD`, `KIE_API_KEY`, `MANAGER_CHAT_ID`** on this repo, the same `BOT_SERVICE_API_KEY` on the wrapper repo, the wrapper's
`BOT_DEEP_LINK_BASE`, the self-hosted runner, and **the backup cron** for the funnel database. Exact commands: `documentation.md` ("Owner setup").

## Docs

- `documentation.md`: architecture, data model, flows, the AI layer, retention, backup, error map, invariants, contract notes, owner setup, ops, facts that can go stale.
- `CLAUDE.md`, `.claude/rules/engineering.md`, `.claude/rules/principles.md`: working rules for contributors and agents.
