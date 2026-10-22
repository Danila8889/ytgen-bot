# ytgen-bot (YouTubeGen Bot)

A small, **stateless** Telegram bot that lets a YouTubeGen customer **link their account, pay for a tariff and use
the referral programme**. It is a thin client of the `youtubegen-saas-wrapper` `/v1/bot/*` API.

> **The product itself is not used inside Telegram.** Video generation and everything else happens on the website
> (ytgenerator.ru); the bot only handles account linking, payment and the referral programme. The bot's `/help` says the same.

- Stack: Python 3.12+, aiogram 3, httpx, uv. Long polling. **No database, no public port.**
- The wrapper is the system of record (links, consent, payments, referrals). The bot never talks to YooKassa or to
  YouTubeGen core, and holds no state that outlives the process (see "What the bot remembers" in `documentation.md`).
- All bot-facing text is Russian, short and plain. No price, limit or percentage is written in the bot: every figure
  shown comes from a wrapper response.

## What users can do

| Command / button | What happens |
|---|---|
| `/start` | Consent step first (links to the offer, referral terms = offer section 10, and bot data processing = privacy policy section 11). Nothing else works until the user presses "Согласен". |
| `/start ref_<code>` | Same, and the referral code is sent to the wrapper (after consent; on every such `/start`, also for users who started organically earlier). The wrapper decides the outcome and the bot reports it. |
| `/start paid` | Where the payment page returns the user: shows the account and tariff state. |
| `/tariffs` | The tariff list as the wrapper returns it. Choosing one requires a linked account (the link flow starts if there is none), then an optional promo code, a quote with the payable amount, and a payment button. |
| payment | "Payment is being processed", background polling with backoff for up to 10 minutes, and a persistent "Проверить оплату" button (works after a restart). Result message: tariff + expiry, or cancelled/failed. |
| `/referral` | Linked users only: personal link, counts, balances, rules from API fields, link to offer section 10, withdrawal request. Payout details are never asked for or stored; the operator contacts the user in Telegram. |
| `/status` (`/me`) | Linked account (masked email) and tariff. |
| `/stop` | Opt-out (`POST /v1/bot/opt-out`). Sending `/start` and consenting again undoes it. |
| `/help` | Commands, the product note, the support contact. |

## Wrapper contract it depends on

Source of truth: the wrapper's `README.md` ("Telegram bot API", "Referral programme"), `src/api/routers/bot.py` and
`src/bot/` (introduced in wrapper commit `54ee58b`). Base `/v1/bot`; every call carries `X-Bot-Service-Key` and
`X-Telegram-User-Id`.

`POST /start`, `POST /opt-out`, `POST /blocked`, `GET /me`, `POST /link/request-code`, `POST /link/verify`,
`GET /tariffs`, `POST /billing/quote`, `POST /billing/checkout`, `GET /billing/payments/{payment_id}`, `GET /referral`,
`POST /referral/withdrawals`. Errors are `{"detail": {"error": <code>, ...}}`, with two exceptions the bot handles
(see "Contract notes" in `documentation.md`): a plain-string `detail` (the tariff 404) and FastAPI's list-shaped 422.

The wrapper must have **both** `BOT_SERVICE_API_KEY` (equal to the bot's) **and** `BOT_DEEP_LINK_BASE`
(`https://t.me/<bot username>`) set, or the bot cannot work: without the key every call is `503 bot_api_disabled`
(the bot's startup self-check fails on it); without the base, checkout is `503 bot_return_url_not_configured` and the
referral `deep_link` is null.

## Configuration

Names only; values are never committed. `.env.example` lists them.

| Variable | Secret | Required | Set by | Meaning |
|---|---|---|---|---|
| `BOT_TOKEN` | yes | yes | owner: GitHub secret `BOT_TOKEN` | Telegram bot token from BotFather. |
| `BOT_SERVICE_API_KEY` | yes | yes | owner: GitHub secret `BOT_SERVICE_API_KEY` | Must equal the wrapper's `BOT_SERVICE_API_KEY` (>= 32 chars, `openssl rand -hex 32`). |
| `WRAPPER_BASE_URL` | no | yes | `.env.production` (committed) | Wrapper base URL **as seen from the container**. `https` only (plain `http` is accepted for loopback, for local runs). |
| `SUPPORT_CONTACT` | no | yes | `.env.production` (owner fills in; committed empty) | Shown in `/help` and the withdrawal confirmation (e.g. `@name`). |
| `LOG_LEVEL` | no | no | `.env.production` | `DEBUG`/`INFO`/`WARNING`/`ERROR`, default `INFO`. |
| `HEARTBEAT_FILE` | no | no | default | Heartbeat path read by the container healthcheck, default `/tmp/ytgen-bot.heartbeat`. |
| `DOCKER_LOG_MAX_SIZE`, `DOCKER_LOG_MAX_FILE` | no | no | default | Docker log rotation, default `20m` / `5`. |

The bot **fails at startup, naming the variable (never the value)**, if a required variable is missing or malformed.

### Reaching the wrapper from Docker

The wrapper listens on the VPS host's `127.0.0.1:8090` and is also public at `https://api.ytgenerator.ru`.

- `http://127.0.0.1:8090` does **not** work from the bot's container: inside a bridge-network container `127.0.0.1` is the
  container itself.
- `http://host.docker.internal:8090` (with `host-gateway`) does **not** work either: it resolves to the Docker bridge
  address, and a listener bound to the host's loopback only does not answer there. Making it work would mean changing
  the wrapper's port binding (another repo, and a wider exposure).
- `network_mode: host` + `http://127.0.0.1:8090` works, but gives the bot container the host's whole network
  namespace (loopback Postgres/Redis of the other stacks included) for no benefit.
- **Chosen: `WRAPPER_BASE_URL=https://api.ytgenerator.ru`.** It works from a container with only outbound internet access
  (which the bot needs for Telegram anyway), keeps the container isolated, and protects the service key in transit with
  TLS (the config refuses plain `http` to a non-loopback host). Caveat: it depends on public DNS and Caddy, and on the
  VPS routing a container's request to its own public address; the deploy's self-check proves it on every deploy.

## Run, test

```bash
uv sync                                  # runtime + dev deps
uv run pytest -q                         # the whole suite: no network, no secrets
cp .env.example .env                     # fill in values, then:
set -a; . ./.env; set +a
uv run python -m ytgen_bot               # run the bot (long polling)
uv run python -m ytgen_bot selfcheck     # getMe + one authenticated wrapper call, then exit
docker compose up -d --build             # the production-shaped way (needs .env)
```

Locally against a wrapper on your machine use `WRAPPER_BASE_URL=http://127.0.0.1:8090` (loopback `http` is allowed).

## Deploy

`.github/workflows/ci.yml`: the `test` job runs on every push/PR to `main`. The `deploy` job runs **only on
`workflow_dispatch`** (`gh workflow run ci.yml --ref main`), on the self-hosted runner labelled `ytgen-bot-vps`, after
`test`:

1. `scripts/render_env.sh` renders `.env` from `.env.production` + the GitHub secrets and **fails the deploy naming the
   missing secret/empty value** before anything is touched.
2. `docker compose up -d --build`.
3. `scripts/post_deploy_check.sh`: the container must be `running` and `healthy`, and `python -m ytgen_bot selfcheck`
   inside it must pass (`getMe` against Telegram, then `GET /v1/bot/me` for the bot's own id, which proves the URL and
   that the key matches the wrapper's).
4. On failure of 2 or 3 the job rolls back to the previously deployed commit and re-runs the same check
   (`always() && !cancelled() && (...failure)`), and fails the run either way.

One-time prerequisites (owner): the GitHub secrets `BOT_TOKEN` and `BOT_SERVICE_API_KEY` on this repo, the same
`BOT_SERVICE_API_KEY` on the wrapper repo, `SUPPORT_CONTACT` filled in `.env.production`, the wrapper's
`BOT_DEEP_LINK_BASE` set to `https://t.me/<this bot's username>`, and the self-hosted runner registered for this repo.
The exact commands are in `documentation.md` ("Owner setup").

## Docs

- `documentation.md`: architecture, flows, error map, invariants, contract notes, owner setup.
- `CLAUDE.md`, `.claude/rules/engineering.md`, `.claude/rules/principles.md`: working rules for contributors and agents.
