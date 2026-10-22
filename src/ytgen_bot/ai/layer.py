"""The AI layer: one entry point, `AiLayer.ask`, used for three things only (PDF section 10): a free question, the
objection-code classification of free text, and text outside the scenario. It can never pick a tariff, state a price
or move a person through the funnel: it returns a reply, an objection code and a hand-off flag; the script decides.

Order of checks (each outcome is written to `funnel_ai_log`):
  per-person hourly limit -> `rate_limited` (INFO)            global daily ceiling -> `ceiling` (+ ONE manager message)
  circuit breaker open     -> failure, no call                  live tariff block unavailable -> failure, NO call (ERROR)
  call (<= 1 retry)        -> `provider_error` on any provider failure (ERROR)
  strict JSON parse        -> `parse_failed` (ERROR)            output validator -> `validator_blocked` (WARNING)
  otherwise                -> `ok`

SINGLE provider, NO failover to any other model or provider: an interim MVP choice (see `ytgen_bot/ai/__init__.py`).
"""
from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from ..db.store import PgStore, StoreError
from ..funnel.render import kb_tariff_block
from ..funnel.tariffs import TariffContractError, TariffProvider
from ..knowledge import KnowledgeBase
from ..wrapper import WRAPPER_FAILURES
from .client import KieChatClient, ProviderError
from .prompt import CODE_CHOICES, render_system, render_user
from .validator import validate

log = logging.getLogger(__name__)

RETRY_BACKOFF_S = 1.5
MAX_REPLY_CHARS = 900  # the prompt asks for <= 600; this bounds a misbehaving model before it reaches Telegram
VALID_CODES = frozenset(CODE_CHOICES.split("|"))


@dataclasses.dataclass(frozen=True)
class AiResult:
    """status: answer | blocked | failed | rate_limited | ceiling. Only `answer` carries a usable reply."""

    status: str
    reply: str | None = None
    objection_code: str | None = None
    handoff: bool = False
    reason: str | None = None


class Breaker:
    """After `threshold` consecutive provider failures the layer is skipped for `cooldown_s` seconds."""

    def __init__(self, threshold: int, cooldown_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._threshold = threshold
        self._cooldown = cooldown_s
        self._clock = clock
        self._consecutive = 0
        self._open_until = 0.0

    def is_open(self) -> bool:
        return self._clock() < self._open_until

    def record_success(self) -> None:
        self._consecutive = 0

    def record_failure(self) -> bool:
        """True exactly when this failure opened the breaker (the caller announces it once)."""
        self._consecutive += 1
        if self._consecutive >= self._threshold and not self.is_open():
            self._open_until = self._clock() + self._cooldown
            self._consecutive = 0
            return True
        return False


def parse_reply(text: str) -> tuple[str, str, bool, str] | None:
    """Strict: the whole completion is one JSON object with exactly the contract's field types. Else None."""
    try:
        data = json.loads(text.strip())
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    reply, code, handoff, reason = data.get("reply"), data.get("objection_code"), data.get("handoff"), data.get("reason")
    if not isinstance(reply, str) or not reply.strip() or len(reply) > MAX_REPLY_CHARS:
        return None
    if not isinstance(code, str) or code not in VALID_CODES:
        return None
    if not isinstance(handoff, bool) or not isinstance(reason, str):
        return None
    return reply.strip(), code, handoff, reason.strip()[:300]


@dataclasses.dataclass(frozen=True)
class Outcome:
    """What one model round-trip produced, before any limits/logging: `kind` is answer | blocked | provider_error |
    parse_failed."""

    kind: str
    reply: str | None = None
    objection_code: str | None = None
    handoff: bool = False
    reason: str | None = None
    latency_ms: int = 0
    kb_text: str = ""


class Responder:
    """One round-trip: build the per-call KB, call the model (<= 1 retry), parse strictly, validate. No database, no
    limits: the CLI `ai-replay` uses it directly with the REAL model and the REAL validator."""

    def __init__(
        self,
        *,
        client: KieChatClient,
        tariffs: TariffProvider,
        kb: KnowledgeBase,
        max_tokens: int,
        temperature: float,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.client = client
        self._tariffs = tariffs
        self._kb = kb
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._sleep = sleep

    async def build_kb(self) -> str:
        """Static facts + the live tariff block. Raises (TariffContractError / wrapper failure) when the tariff block
        cannot be built: the caller then does NOT call the LLM."""
        tariffs = await self._tariffs.get()
        return f"{self._kb.facts_block()}\n\n{kb_tariff_block(tariffs)}"

    async def respond(self, question: str, step: str, kb_text: str) -> Outcome:
        system, user = render_system(kb_text), render_user(question, step)
        started = time.monotonic()
        completion = None
        error: ProviderError | None = None
        for attempt in (1, 2):
            try:
                completion = await self.client.chat(
                    system=system, user=user, max_tokens=self._max_tokens, temperature=self._temperature
                )
                break
            except ProviderError as exc:
                error = exc
                if attempt == 1 and exc.retryable:
                    await self._sleep(RETRY_BACKOFF_S)
                    continue
                break
        latency_ms = int((time.monotonic() - started) * 1000)
        if completion is None:
            assert error is not None
            return Outcome("provider_error", reason=error.kind, latency_ms=latency_ms, kb_text=kb_text)
        parsed = parse_reply(completion.text)
        if parsed is None:
            return Outcome("parse_failed", latency_ms=latency_ms, kb_text=kb_text)
        reply, code, handoff, reason = parsed
        verdict = validate(reply, kb_text)
        if not verdict.ok:
            return Outcome(
                "blocked", reply=reply, objection_code=code, handoff=handoff, reason=",".join(verdict.reasons),
                latency_ms=latency_ms, kb_text=kb_text,
            )
        return Outcome(
            "answer", reply=reply, objection_code=code, handoff=handoff, reason=reason, latency_ms=latency_ms,
            kb_text=kb_text,
        )


class AiLayer:
    def __init__(
        self,
        *,
        store: PgStore,
        client: KieChatClient,
        tariffs: TariffProvider,
        kb: KnowledgeBase,
        user_hourly_limit: int,
        daily_ceiling: int,
        max_tokens: int,
        temperature: float,
        breaker: Breaker,
        notify_manager: Callable[[str], Awaitable[None]],
        clock: Callable[[], dt.datetime],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._store = store
        self._client = client
        self._responder = Responder(
            client=client, tariffs=tariffs, kb=kb, max_tokens=max_tokens, temperature=temperature, sleep=sleep
        )
        self._user_limit = user_hourly_limit
        self._ceiling = daily_ceiling
        self._breaker = breaker
        self._notify = notify_manager
        self._clock = clock

    async def _log(self, uid: int, step: str | None, question: str, outcome: str, **kw: Any) -> None:
        try:
            await self._store.log_ai(
                uid, step=step, question=question, reply=kw.get("reply"), objection_code=kw.get("objection_code"),
                handoff=bool(kw.get("handoff", False)), reason=kw.get("reason"), model=self._client.model,
                latency_ms=kw.get("latency_ms"), outcome=outcome,
            )
        except StoreError:
            pass  # already logged at ERROR by the store; the answer is still delivered

    async def _announce(self, text: str) -> None:
        try:
            await self._notify(text)
        except Exception:
            # The manager message is an alert, never a gate: a failure to send it must not break the answer.
            log.exception("ai_manager_alert_failed")

    async def ask(self, uid: int, question: str, step: str) -> AiResult:
        now = self._clock()
        if await self._store.ai_calls_since(now - dt.timedelta(hours=1), uid=uid) >= self._user_limit:
            log.info("ai_rate_limited", extra={"tg_user": uid})
            await self._log(uid, step, question, "rate_limited")
            return AiResult("rate_limited")

        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if await self._store.ai_calls_since(day_start) >= self._ceiling:
            first = not await self._store.ceiling_logged_since(day_start)
            await self._log(uid, step, question, "ceiling")
            if first:
                log.error("ai_daily_ceiling_reached", extra={"ceiling": self._ceiling})
                await self._announce(
                    "AI-слой: достигнут суточный лимит вызовов LLM. До конца суток (UTC) бот отвечает на свободные "
                    "вопросы запасными текстами и зовёт менеджера."
                )
            return AiResult("ceiling")

        if self._breaker.is_open():
            await self._log(uid, step, question, "provider_error", reason="breaker_open")
            return AiResult("failed", reason="breaker_open")

        try:
            kb_text = await self._responder.build_kb()
        except (TariffContractError, *WRAPPER_FAILURES) as exc:
            log.error("ai_tariff_block_unavailable", extra={"tg_user": uid, "error": type(exc).__name__})
            await self._log(uid, step, question, "provider_error", reason="tariff_block_unavailable")
            return AiResult("failed", reason="tariff_block_unavailable")

        out = await self._responder.respond(question, step, kb_text)

        if out.kind == "provider_error":
            log.error("ai_provider_error", extra={"tg_user": uid, "kind": out.reason, "latency_ms": out.latency_ms})
            opened = self._breaker.record_failure()
            await self._log(uid, step, question, "provider_error", reason=out.reason, latency_ms=out.latency_ms)
            if opened:
                log.error("ai_breaker_opened")
                await self._announce(
                    "AI-слой: подряд несколько ошибок провайдера LLM. Ответы AI отключены на время охлаждения, бот "
                    "отвечает запасными текстами и зовёт менеджера."
                )
            return AiResult("failed", reason=out.reason)

        self._breaker.record_success()  # the provider answered; a bad answer is not a provider outage
        if out.kind == "parse_failed":
            log.error("ai_parse_failed", extra={"tg_user": uid, "latency_ms": out.latency_ms})
            await self._log(uid, step, question, "parse_failed", latency_ms=out.latency_ms)
            return AiResult("failed", reason="parse_failed")
        if out.kind == "blocked":
            log.warning("ai_validator_blocked", extra={"tg_user": uid, "reasons": out.reason})
            await self._log(
                uid, step, question, "validator_blocked", reply=out.reply, objection_code=out.objection_code,
                handoff=out.handoff, reason=out.reason, latency_ms=out.latency_ms,
            )
            return AiResult("blocked", reason=out.reason)

        await self._log(
            uid, step, question, "ok", reply=out.reply, objection_code=out.objection_code, handoff=out.handoff,
            reason=out.reason, latency_ms=out.latency_ms,
        )
        return AiResult(
            "answer", reply=out.reply, objection_code=None if out.objection_code == "none" else out.objection_code,
            handoff=out.handoff, reason=out.reason,
        )
