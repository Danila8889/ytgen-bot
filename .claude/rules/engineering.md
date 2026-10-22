# ytgen-bot - Engineering Rules

The "how". See root `CLAUDE.md` for the "what and why". This file assumes you agree with it.

## Git & deployment

- **Production changes only through `git commit` -> `git push` -> CI/CD.** Never copy files onto the VPS or hand-edit
  anything there; SSH is for read-only diagnosis.
- Commits go to `main` once the full suite is green (the sibling repos' no-PR convention); non-trivial work may use a
  branch first. Commits are atomic: one logical change each.
- **CI runs on the self-hosted runner, unlike the sibling repos** (`test` and `deploy` both `[self-hosted, ytgen-bot-vps]`).
  Reason: GitHub-hosted jobs on this private repo do not start while the account has a billing problem. Keep it safe:
  `deploy` `needs: test` (one runner, one job at a time, no circular wait), `test` has a timeout and no concurrency group, and
  the test checkout uses `path: .ci-test-src` so it never moves HEAD or `git clean`s the production checkout (rollback reads
  `git rev-parse HEAD`; `.env` is gitignored). If hosted runners come back, switching `test` to `ubuntu-latest` is a one-line change.
- **Deploys are `workflow_dispatch`-only** (`gh workflow run ci.yml --ref main`), on the self-hosted runner labelled
  `ytgen-bot-vps`. Pushing to `main` runs tests only. Never wire deploy to push.
- The deploy job renders `.env` (`scripts/render_env.sh`), runs `docker compose up -d --build`, then
  `scripts/post_deploy_check.sh` (container healthy + `python -m ytgen_bot selfcheck`); on failure it rolls back to the
  previous commit with the repo-wide pattern `always() && !cancelled() && (steps.deploy.outcome == 'failure' ||
  steps.healthcheck.outcome == 'failure')`. Do not weaken that condition.

## Documentation stays current

`README.md` (what it is, env vars, run/test/deploy, the wrapper contract) and `documentation.md` (architecture, flows,
error map, invariants, contract notes, owner setup) are the single sources of truth. A change to behaviour, an env var, the
wrapper contract it uses, or a data flow updates them in the same commit. Say in the report which docs were updated, or that
none applied and why.

## Adding or changing an env var

1. Decide: **secret** (credential/token/key, or a value that works as a bearer credential) -> GitHub secret, named in
   `SECRET_KEYS` of `scripts/render_env.sh` **and** mapped in the `deploy` job's `env:` block of `ci.yml` (a secret with no
   mapping never reaches the VPS). **Non-secret** -> `.env.production`; add it to `REQUIRED_PLAIN_KEYS` if the bot cannot run without it.
2. Validate it in `config.py` (fail fast, name the variable, never print the value) and add a test in `tests/test_config.py`.
3. List it in `.env.example` (names only), the README table, and classify it in `ytgen-infra/env-vars/ytgen-bot.md`
   (owner/orchestrator task: that repo is not edited from here).
4. Secrets entered by the owner in his own terminal (`gh secret set ...`), never pasted into a chat or a transcript.

## Calling the wrapper

- All calls go through `WrapperClient`; every call carries `X-Bot-Service-Key` and `X-Telegram-User-Id`. No call carries
  a tenant id (the wrapper derives the tenant from the Telegram link).
- Nothing is retried inside the client. State-changing calls (`checkout`, `verify`, `withdrawals`) are never retried
  automatically; polling `GET /billing/payments/{id}` is the only repeated call and it is bounded.
- A new field/endpoint is read from the wrapper's real code (`src/api/routers/bot.py`, `src/bot/`) and README first. Add the
  response shape to `schemas.py`, a route to `tests/fake_wrapper.py` copied from the real shape, and a test. A mismatch with the
  stated contract goes into `documentation.md` section 7 and the task report; do not paper over it.
- A new wrapper error code gets an entry in `errors_ru.py` (and `DOCUMENTED_CODES`) and a test. Unmapped codes log at ERROR on purpose.

## Handlers and callbacks

- Callback data is built and parsed only through `callbacks.py` (strict regex, <= 64 bytes). A button that must work after
  a restart carries its whole input in the data (payment id, tariff id, expected amount, promo, referral code).
- State-changing handlers use `ctx.guard.hold(...)` (double-tap debounce). Sends go through `ui.say` (it reports Telegram
  `Forbidden` with `POST /blocked`); wrapper failures go through `ui.fail` -> `errors_ru.describe`.
- User text is HTML-escaped (`formatting.esc`); no price/limit/percent literal in `texts.py` or anywhere else.
- Everything the user types is length-limited and validated before it is used.

## Testing

`uv run pytest -q` before calling a task done. No network and no secrets are needed: the wrapper is faked by
`tests/fake_wrapper.py` (httpx `MockTransport`, response shapes copied from the real code, including the quirky ones), Telegram by
`tests/mocked_telegram.py` (a recording aiogram session; handlers are driven through `Dispatcher.feed_update`).
`tests/conftest.py` fails any real outbound connection (even if the caller swallows the exception).
New behaviour needs tests for: its error mappings, the consent gate where relevant, double-tap where it changes state,
and log masking where it logs user data.

## Lint

`uv run ruff check` (ruff pinned in `uv.lock`, default rule set) must be clean; the CI `test` job runs it before the tests.
Handlers catch `WRAPPER_FAILURES` (the three wrapper failure types), never a blind `Exception`; anything else is a bug and goes to the
dispatcher's error handler (ERROR log + generic message).

## Known technical landmines

- **Docker -> wrapper URL.** `127.0.0.1:8090` is the container's own loopback; `host.docker.internal` cannot reach a
  loopback-bound listener. Use the public https URL (README "Reaching the wrapper from Docker"). Do not "simplify" it to loopback.
- **`BOT_DEEP_LINK_BASE` (wrapper) must name this bot.** The bot builds `return_url` from `getMe`'s username; a mismatch is
  `422 return_url_not_allowed` on every checkout.
- **Consent is read with `GET /me`** (read-only). Never use `POST /start` as a probe: it is a counted `start` event and clears
  `blocked_at`. The bot keeps no consent state, so it survives restarts and an opt-out applies at once.
- **aiogram routers attach to one parent only**: handler modules expose `build_router()` (called once per dispatcher), not a
  module-level `router`.
- **An env value with `$`, quotes or a backtick** breaks `docker compose` env-file parsing; `render_env.sh` refuses such values.
- `docker-compose.yml`'s `read_only: true` + `tmpfs /tmp`: anything the bot writes must go to `/tmp` (the heartbeat does).
