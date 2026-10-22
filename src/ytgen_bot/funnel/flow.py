"""The funnel state machine: contact -> SPIN -> recommendation -> presentation -> objections -> closing -> hand-off,
plus reminders and the payment bridge. The script speaks; the AI only answers free questions and labels an objection.

State lives in Postgres (`funnel_leads.step` / `status`), never in memory, so a restart resumes people where they were.

ISOLATION (a hard rule of this repo): funnel failures must never break checkout, payment status, link, referral or
withdrawal flows.
  * INSIDE funnel flows a `StoreError` (already logged at ERROR by the store) is shown to the person ("something went
    wrong, write to the manager") by `_shown`; nothing is faked.
  * Hooks called FROM the money flows (`on_payment_success`, `on_blocked`) are `_isolated`: any failure is logged at ERROR
    and swallowed, the money flow carries on.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import User

from .. import callbacks, schemas
from ..ai.layer import AiResult
from ..context import AppContext
from ..db.store import Followup, PgStore, StoreError
from ..handlers.tariffs import choose_tariff
from ..knowledge import KnowledgeBase
from ..ui import fail, say
from ..wrapper import WRAPPER_FAILURES, WrapperClient
from . import handoff as handoff_card
from . import keyboards as kb
from . import texts, triggers
from .model import Lead, Role, Status
from .reminders import next_followup, sync_schedule
from .rules import Answers, choose_role
from .tariffs import FunnelTariff, TariffContractError, TariffProvider, TariffSet

log = logging.getLogger(__name__)


class AiAsker(Protocol):
    async def ask(self, uid: int, question: str, step: str) -> AiResult: ...


# Step names (stored in funnel_leads.step).
REMINDERS = "reminders_ask"
GREET, HOW, EXAMPLES, ASK_FREE = "greet", "how", "examples", "ask_free"
S1, S2, S3, P1, P1_OTHER, I1, N1 = "s1", "s2", "s3", "p1", "p1_other", "i1", "n1"
REC, DET_A, DET_B, DET_C, CLOSE, OBJ, END = "rec", "det_a", "det_b", "det_c", "close", "obj", "end"
OBJ_REPEAT, DISCOUNT, HANDOFF = "obj_repeat", "discount", "handoff"
PAY_PREFIX = "pay:"

_BACK = {S2: S1, S3: S2, P1: S3, P1_OTHER: P1, I1: P1, N1: I1}
_ANSWER_STEP = {"s1": S1, "s2": S2, "p1": P1, "i1": I1, "n1": N1}
ANSWER_CODES = frozenset(c for c in callbacks.FUNNEL_CODES if c[:3] in ("s1_", "s2_", "p1_", "i1_", "n1_") or c == "s3_skip")
OBJECTION_CODES = frozenset(c for c in callbacks.FUNNEL_CODES if c.startswith("obj_"))
# Steps at which a free question is answered and the person is brought back with "Продолжить подбор".
_RESUME_STEPS = frozenset({S1, S2, S3, P1, P1_OTHER, I1, N1, REC, DET_A, DET_B, DET_C, CLOSE, OBJ, REMINDERS})
_AFTER_HANDOFF = frozenset({"handoff_manual", "unsub"})


def _answer_step(code: str) -> str | None:
    if code == "s3_skip":
        return S3
    return _ANSWER_STEP.get(code.split("_", 1)[0]) if code in ANSWER_CODES else None


def _isolated[**P](fn: Callable[P, Awaitable[Any]]) -> Callable[P, Awaitable[Any]]:
    """For hooks called from money flows: never raise into them."""

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
        try:
            return await fn(*args, **kwargs)
        except Exception:
            log.exception("funnel_hook_failed", extra={"op": fn.__name__})
            return None

    return wrapper


def _shown(default: Any = None) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """For funnel entry points: a database failure is shown to the person (the store already logged it at ERROR)."""

    def decorate(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @functools.wraps(fn)
        async def wrapper(self: Funnel, ctx: AppContext, bot: Bot, uid: int, *args: Any, **kwargs: Any) -> Any:
            try:
                return await fn(self, ctx, bot, uid, *args, **kwargs)
            except StoreError:
                await say(ctx, bot, uid, texts.funnel_error(self.support), kb.manager_markup(self.support))
                return default

        return wrapper

    return decorate


class Funnel:
    def __init__(
        self,
        *,
        store: PgStore,
        wrapper: WrapperClient,
        tariffs: TariffProvider,
        kb: KnowledgeBase,
        ai: AiAsker,
        support: str,
        manager_chat_id: int,
        clock: Callable[[], dt.datetime],
    ) -> None:
        self.store = store
        self.wrapper = wrapper
        self.tariffs = tariffs
        self.kb = kb
        self.ai = ai
        self.support = support
        self.manager_chat_id = manager_chat_id
        self.clock = clock

    # --- small helpers ------------------------------------------------------------------------------------------

    async def _tariff_set(self, uid: int, why: str) -> TariffSet | None:
        """The live tariff set, or None after an ERROR log (the caller falls back to the manager)."""
        try:
            return await self.tariffs.get()
        except TariffContractError as exc:
            log.error("funnel_tariff_contract", extra={"tg_user": uid, "why": why, "problem": str(exc)})
        except WRAPPER_FAILURES as exc:
            log.error("funnel_tariffs_unavailable", extra={"tg_user": uid, "why": why, "error": type(exc).__name__})
        return None

    @staticmethod
    def _paid(me: schemas.MeResponse) -> bool:
        return me.linked and me.tenant is not None and me.tenant.paid_access_active

    @staticmethod
    def _keep_handoff(lead: Lead, status: Status) -> str:
        """A person handed to the manager stays `handoff` however many old buttons they press: sales stay stopped."""
        return Status.HANDOFF.value if lead.status == Status.HANDOFF else status.value

    @staticmethod
    def _live_status(lead: Lead) -> str:
        if lead.rec_role:
            return Status.RECOMMENDED.value
        return Status.IN_SPIN.value if lead.stage else Status.NEW.value

    async def _touch(self, lead: Lead, user: User | None = None) -> Lead:
        """Any user activity: refresh `last_activity_at` (and the Telegram name/handle, which people change), note that
        they answered a reminder, wake a cold/blocked lead."""
        now = self.clock()
        fields: dict[str, Any] = {"last_activity_at": now}
        if user is not None and (user.username, user.first_name) != (lead.username, lead.first_name):
            fields.update(username=user.username, first_name=user.first_name)
        if (
            lead.followups_sent > 0 and not lead.answered_followup
            and lead.last_followup_at is not None and now >= lead.last_followup_at
        ):
            fields["answered_followup"] = True
        if lead.status in (Status.COLD, Status.BLOCKED):
            fields["status"] = self._live_status(lead)
        await self.store.update_lead(lead.tg_id, **fields)
        return dataclasses.replace(lead, **fields)

    async def _update(self, lead: Lead, **fields: Any) -> Lead:
        await self.store.update_lead(lead.tg_id, **fields)
        return dataclasses.replace(lead, **fields)

    async def _event(self, uid: int, event: str, value: str | None = None) -> None:
        await self.store.add_event(uid, event, value)

    async def accepted(self, uid: int) -> bool:
        """True when this person already accepted the funnel's data processing (a lead row exists)."""
        lead = await self.store.get_lead(uid)
        return lead is not None and not lead.is_tombstone

    # --- entry: /start and the consent button -----------------------------------------------------------------------

    async def entry(
        self, ctx: AppContext, bot: Bot, uid: int, user: User, *, source: str | None, me: schemas.MeResponse
    ) -> bool:
        """Start or resume the funnel for a consented person. False = the funnel does not apply (they already have paid
        access): the caller shows the existing menu. Raises StoreError (already logged): the CALLER shows it together with
        the main menu, so the rest of the bot stays reachable."""
        if self._paid(me):
            return False  # callers use `on_paid_start` for these people (isolated), never the funnel
        lead = await self.store.upsert_lead(uid, username=user.username, first_name=user.first_name, source=source)
        if lead.status == Status.PAID:
            return False  # a customer (e.g. marked paid by the manager): no sales funnel
        await self._event(uid, "start", lead.source or "none")
        lead = await self._touch(lead)
        if lead.status == Status.HANDOFF:
            await say(ctx, bot, uid, texts.handoff_status_note(self.support), kb.manager_markup(self.support))
        elif lead.reminders_opt_in is None:
            lead = await self._update(lead, step=REMINDERS)
            await say(ctx, bot, uid, texts.reminders_ask(), kb.reminders_choice())
        elif lead.step in (None, GREET):
            lead = await self._update(lead, step=GREET)
            await say(ctx, bot, uid, texts.greeting(lead.source), kb.greeting())
        else:
            await say(ctx, bot, uid, texts.welcome_back())
            await self._render(ctx, bot, lead)
        await sync_schedule(self.store, uid)
        return True

    async def _paid_seen(self, ctx: AppContext, bot: Bot, uid: int, me: schemas.MeResponse) -> None:
        """Wrapper says paid access is active: a lead (if there is one) becomes `paid`, M5.6 goes out once."""
        lead = await self.store.get_lead(uid)
        if lead is None or lead.is_tombstone:
            return
        await self.store.mark_paid(uid, tariff=me.tenant.tariff_name if me.tenant else None, by_manager=False)
        await self._announce_paid(ctx, bot, uid)

    async def _announce_paid(self, ctx: AppContext, bot: Bot, uid: int) -> None:
        if await self.store.claim_paid_announcement(uid):
            await say(ctx, bot, uid, texts.paid_welcome(self.support))

    # --- rendering a stored step ---------------------------------------------------------------------------------

    async def _show(self, ctx: AppContext, bot: Bot, lead: Lead, step: str, **fields: Any) -> Lead:
        lead = await self._update(lead, step=step, **fields)
        await self._render(ctx, bot, lead)
        return lead

    async def _render(self, ctx: AppContext, bot: Bot, lead: Lead) -> None:
        uid, step = lead.tg_id, lead.step or GREET
        if step == REMINDERS:
            await say(ctx, bot, uid, texts.reminders_ask(), kb.reminders_choice())
        elif step == HOW:
            await say(ctx, bot, uid, texts.how_it_works(self.kb), kb.how_it_works())
        elif step == EXAMPLES:
            await say(ctx, bot, uid, texts.examples(self.kb), kb.examples())
        elif step == ASK_FREE:
            await say(ctx, bot, uid, texts.ask_free_intro())
        elif step == S1:
            await say(ctx, bot, uid, texts.S1, kb.s1())
        elif step == S2:
            await say(ctx, bot, uid, texts.S2, kb.s2())
        elif step == S3:
            await say(ctx, bot, uid, texts.S3, kb.s3())
        elif step == P1:
            await say(ctx, bot, uid, texts.P1, kb.p1())
        elif step == P1_OTHER:
            await say(ctx, bot, uid, texts.P1_OTHER, kb.p1_other())
        elif step == I1:
            await say(ctx, bot, uid, texts.i1(lead.pain), kb.i1())
        elif step == N1:
            await say(ctx, bot, uid, texts.N1, kb.n1())
        elif step in (REC, DET_A, DET_B, DET_C):
            await self._send_recommendation(ctx, bot, lead)
        elif step == CLOSE:
            await self._send_closing(ctx, bot, lead)
        elif step == OBJ:
            await say(ctx, bot, uid, texts.M40, kb.objections())
        elif step == OBJ_REPEAT:
            await say(ctx, bot, uid, texts.OBJ_REPEAT, kb.repeat_objection())
        elif step == DISCOUNT:
            await say(ctx, bot, uid, texts.discount_note(self.support), kb.discount())
        elif step.startswith(PAY_PREFIX):
            await self._send_pay_howto(ctx, bot, lead, self._pay_role(lead))
        elif step == HANDOFF:
            await say(ctx, bot, uid, texts.handoff_status_note(self.support), kb.manager_markup(self.support))
        elif step == END:
            await say(ctx, bot, uid, texts.POLITE_END, kb.end_polite())
        else:
            await say(ctx, bot, uid, texts.greeting(lead.source), kb.greeting())

    # --- buttons -----------------------------------------------------------------------------------------------------

    @_shown()
    async def on_button(self, ctx: AppContext, bot: Bot, uid: int, code: str, user: User | None = None) -> None:
        lead = await self.store.get_lead(uid)
        if lead is None or lead.is_tombstone:
            await say(ctx, bot, uid, texts.SOMETHING_STALE + " Начать заново: /start")
            return
        if lead.status == Status.PAID and code not in _AFTER_HANDOFF:
            await say(ctx, bot, uid, "Оплата уже подтверждена, спасибо! Команды: /help")
            return
        lead = await self._touch(lead, user)
        await self._event(uid, "btn", code)
        expected = _answer_step(code)
        if expected is not None and not (lead.step == expected or (expected == P1 and lead.step == P1_OTHER)):
            # A button from an earlier message: never apply an answer out of order.
            await say(ctx, bot, uid, texts.SOMETHING_STALE)
            await self._render(ctx, bot, lead)
        else:
            await self._dispatch(ctx, bot, lead, code)
        await sync_schedule(self.store, uid)

    async def _dispatch(self, ctx: AppContext, bot: Bot, lead: Lead, code: str) -> None:
        uid = lead.tg_id
        if code == "intro_how":
            await self._show(ctx, bot, lead, HOW)
        elif code == "examples":
            await self._show(ctx, bot, lead, EXAMPLES)
        elif code == "ask_free":
            await self._show(ctx, bot, lead, ASK_FREE)
        elif code == "back_start":
            await self._show(ctx, bot, lead, GREET)
        elif code == "spin_start":
            await self._show(ctx, bot, lead, S1, status=self._keep_handoff(lead, Status.IN_SPIN))
        elif code == "resume":
            await self._render(ctx, bot, lead)
        elif code == "back":
            await self._show(ctx, bot, lead, _BACK.get(lead.step or "", GREET) if lead.step != S1 else GREET)
        elif code == "handoff_manual":
            reason = {OBJ_REPEAT: "repeat_objection", DISCOUNT: "discount"}.get(lead.step or "", "manual_request")
            if (lead.step or "").startswith(PAY_PREFIX):
                reason = "payment_help"
            await self._handoff(ctx, bot, lead, reason=reason)
        elif code == "doubt_open":
            await self._show(ctx, bot, lead, OBJ)
        elif code in ANSWER_CODES:
            await self._answer(ctx, bot, lead, code)
        elif code == "show_details":
            await self._show(ctx, bot, lead, DET_A)
        elif code == "details_next":
            await self._details_next(ctx, bot, lead)
        elif code == "compare":
            await self._compare(ctx, bot, lead)
        elif code == "back_close":
            await self._show(ctx, bot, lead, CLOSE)
        elif code in ("pay_main", "try_lite"):
            await self._pay_intent(ctx, bot, lead, Role.ENTRY if code == "try_lite" else None)
        elif code == "go_pay":
            await self._go_pay(ctx, bot, lead)
        elif code in OBJECTION_CODES:
            await self._objection(ctx, bot, lead, code)
        elif code in ("q_visual", "q_voice", "q_template", "h_setup", "h_yt", "g_script", "g_video", "g_all", "g_text"):
            await self._branch(ctx, bot, lead, code)
        elif code == "remind_ok":
            await self._remind_ok(ctx, bot, lead)
        elif code == "end_polite":
            await self._update(lead, status=Status.COLD.value, step=END)
            await say(ctx, bot, uid, texts.POLITE_END, kb.end_polite())
        elif code == "unsub":
            await self._update(lead, unsub=True)
            await say(ctx, bot, uid, "Хорошо, больше не напоминаю. Если захочешь вернуться, отправь /start.")
        elif code == "pay_done":
            await self._pay_done(ctx, bot, lead)
        elif code == "pay_not_yet":
            back = Status.RECOMMENDED if lead.rec_role else Status.IN_SPIN
            await self._update(lead, status=self._keep_handoff(lead, back))
            await say(ctx, bot, uid, texts.PAY_NOT_YET)
        else:  # pragma: no cover - FUNNEL_CODES is closed and every code is handled above
            log.error("funnel_code_unhandled", extra={"tg_user": uid, "code": code})
            await say(ctx, bot, uid, texts.SOMETHING_STALE)

    # --- SPIN ------------------------------------------------------------------------------------------------------------

    async def _answer(self, ctx: AppContext, bot: Bot, lead: Lead, code: str) -> None:
        uid = lead.tg_id
        status = self._keep_handoff(lead, Status.IN_SPIN)
        if code.startswith("s1_"):
            lead = await self._update(lead, stage=code, status=status)
            await self._show(ctx, bot, lead, S2)
        elif code.startswith("s2_"):
            lead = await self._update(lead, volume=code, status=status)
            await self._show(ctx, bot, lead, S3)
        elif code == "s3_skip":
            await self._show(ctx, bot, lead, P1)
        elif code == "p1_other":
            await self._show(ctx, bot, lead, P1_OTHER, pain=code, status=status)
        elif code.startswith("p1_"):
            lead = await self._update(lead, pain=code, pain_text=None, status=status)
            await self._show(ctx, bot, lead, I1)
        elif code.startswith("i1_"):
            lead = await self._update(lead, impact=code, step=N1)
            await say(ctx, bot, uid, f"{texts.after_i1(code)}\n\n{texts.N1}", kb.n1())
        elif code.startswith("n1_"):
            lead = await self._update(lead, goal=code)
            await self._event(uid, "spin_done")
            await say(ctx, bot, uid, texts.PICKING)
            await self._recommend(ctx, bot, lead)

    # --- recommendation and presentation ---------------------------------------------------------------------------------

    async def _recommend(self, ctx: AppContext, bot: Bot, lead: Lead) -> None:
        role, rule = choose_role(Answers(lead.stage, lead.volume, lead.goal))
        tariffs = await self._tariff_set(lead.tg_id, "recommend")
        tariff = tariffs.get(role) if tariffs is not None else None
        lead = await self._update(
            lead, rec_role=role.value, rec_tariff_id=tariff.id if tariff else None,
            status=self._keep_handoff(lead, Status.RECOMMENDED), step=REC,
        )
        await self._event(lead.tg_id, "recommended", role.value)
        log.info("funnel_recommended", extra={"tg_user": lead.tg_id, "role": role.value, "rule": rule})
        await self._send_recommendation(ctx, bot, lead, tariffs)

    async def _rec_tariff(
        self, lead: Lead, tariffs: TariffSet | None = None
    ) -> tuple[TariffSet, Role, FunnelTariff] | None:
        tariffs = tariffs or await self._tariff_set(lead.tg_id, "present")
        if tariffs is None:
            return None
        if lead.rec_role:
            role = Role(lead.rec_role)
        else:
            role, _ = choose_role(Answers(lead.stage, lead.volume, lead.goal))
        return tariffs, role, tariffs.get(role)

    async def _send_recommendation(
        self, ctx: AppContext, bot: Bot, lead: Lead, tariffs: TariffSet | None = None
    ) -> None:
        found = await self._rec_tariff(lead, tariffs)
        uid = lead.tg_id
        if found is None:
            await say(ctx, bot, uid, texts.recommendation_unavailable(self.support), kb.manager_fallback(self.support))
            return
        ts, role, tariff = found
        step = lead.step or REC
        if step == DET_A:
            await say(ctx, bot, uid, texts.details_a(lead.pain, self.kb), kb.details_a())
        elif step == DET_B:
            await say(ctx, bot, uid, texts.details_b(tariff, role == Role.ENTRY, self.kb), kb.details_b())
        elif step == DET_C:
            await say(ctx, bot, uid, texts.details_c(), kb.details_c())
        else:
            await say(
                ctx, bot, uid, texts.recommendation(tariff, lead.goal, self.kb),
                kb.recommendation(ts.get(Role.ENTRY).name, tariff.name, role == Role.ENTRY),
            )

    async def _details_next(self, ctx: AppContext, bot: Bot, lead: Lead) -> None:
        nxt = {DET_A: DET_B, DET_B: DET_C}.get(lead.step or "")
        if nxt is None:
            await self._show(ctx, bot, lead, DET_A)
        else:
            await self._show(ctx, bot, lead, nxt)

    async def _compare(self, ctx: AppContext, bot: Bot, lead: Lead) -> None:
        uid = lead.tg_id
        tariffs = await self._tariff_set(uid, "compare")
        if tariffs is None:
            await say(ctx, bot, uid, texts.recommendation_unavailable(self.support), kb.manager_fallback(self.support))
            return
        await say(ctx, bot, uid, texts.compare(tariffs), kb.compare())
        if lead.rec_role:
            await self._show(ctx, bot, lead, CLOSE)

    async def _send_closing(self, ctx: AppContext, bot: Bot, lead: Lead) -> None:
        found = await self._rec_tariff(lead)
        if found is None:
            await say(ctx, bot, lead.tg_id, texts.recommendation_unavailable(self.support),
                      kb.manager_fallback(self.support))
            return
        ts, role, tariff = found
        await say(ctx, bot, lead.tg_id, texts.closing(tariff), kb.closing(ts.get(Role.ENTRY).name, tariff.name, role == Role.ENTRY))

    # --- objections -------------------------------------------------------------------------------------------------------

    async def _objection(self, ctx: AppContext, bot: Bot, lead: Lead, code: str) -> None:
        uid = lead.tg_id
        if lead.status == Status.HANDOFF:  # sales stopped after a hand-off
            await say(ctx, bot, uid, texts.handoff_status_note(self.support), kb.manager_markup(self.support))
            return
        if code in await self.store.objection_codes(uid):
            # Second time: the bot does not repeat itself, it offers a human (and keeps the objection on record).
            await self.store.add_objection(uid, code)  # kept on record (with its time) even though the bot does not repeat itself
            await self._event(uid, "objection_repeat", code)
            await self._show(ctx, bot, lead, OBJ_REPEAT)
            return
        await self.store.add_objection(uid, code)
        await self._event(uid, "objection", code)
        tariffs = await self._tariff_set(uid, "objection") if code in ("obj_price", "obj_roi") else None
        entry = tariffs.get(Role.ENTRY) if tariffs else None
        name = entry.name if entry else None
        text, markup = {
            "obj_price": lambda: (texts.obj_price(entry), kb.obj_price(name)),
            "obj_quality": lambda: (texts.OBJ_QUALITY, kb.obj_quality()),
            "obj_hard": lambda: (texts.OBJ_HARD, kb.obj_hard()),
            "obj_ban": lambda: (texts.OBJ_BAN, kb.obj_ban(None)),
            "obj_gpt": lambda: (texts.OBJ_GPT, kb.obj_gpt()),
            "obj_time": lambda: (texts.OBJ_TIME, kb.obj_time(None)),
            "obj_roi": lambda: (texts.obj_roi(entry), kb.obj_roi(name)),
            "obj_refund": lambda: (texts.OBJ_REFUND, kb.obj_refund(None)),
            "obj_think": lambda: (texts.OBJ_THINK, kb.obj_think()),
        }[code]()
        await self._update(lead, step=OBJ)
        await say(ctx, bot, uid, text, markup)

    async def _branch(self, ctx: AppContext, bot: Bot, lead: Lead, code: str) -> None:
        uid = lead.tg_id
        if code == "g_all":
            await self._show(ctx, bot, lead, DET_A)
        elif code == "g_text":
            await say(ctx, bot, uid, texts.G_TEXT, kb.end_polite())
        elif code in ("g_script", "g_video"):
            await say(ctx, bot, uid, texts.OBJ_GPT_2, kb.obj_gpt_2())
        elif code in ("h_setup", "h_yt"):
            await say(ctx, bot, uid, texts.H_SETUP if code == "h_setup" else texts.H_YT, kb.after_hard(None))
        else:
            body = {"q_visual": texts.q_visual, "q_voice": texts.q_voice, "q_template": texts.q_template}[code](self.kb)
            await say(ctx, bot, uid, f"{body}\n\n{texts.CLOSE_SEE}", kb.after_quality(None))

    # --- reminders opt-in / remind_ok --------------------------------------------------------------------------------------

    @_shown()
    async def on_reminders_answer(self, ctx: AppContext, bot: Bot, uid: int, answer: str) -> None:
        lead = await self.store.get_lead(uid)
        if lead is None or lead.is_tombstone:
            await say(ctx, bot, uid, texts.SOMETHING_STALE + " Начать заново: /start")
            return
        lead = await self._touch(lead)
        yes = answer == "yes"
        await self._event(uid, "reminders", answer)
        fields: dict[str, Any] = {"reminders_opt_in": yes}
        if yes and lead.step != REMINDERS:
            fields["remind_ok"] = True  # it came from "Сохранить подбор и напомнить позже"
        lead = await self._update(lead, **fields)
        await say(ctx, bot, uid, texts.reminders_saved(yes))
        if lead.step == REMINDERS:
            await self._show(ctx, bot, lead, GREET)
        await sync_schedule(self.store, uid)

    async def _remind_ok(self, ctx: AppContext, bot: Bot, lead: Lead) -> None:
        lead = await self._update(lead, remind_ok=True)
        if lead.reminders_opt_in is True:
            await say(ctx, bot, lead.tg_id, "Сохранил твой подбор. Если ты не вернёшься сам, я напомню о нём.")
        else:
            # The reminders opt-in is its own explicit step: pressing this button does not replace it.
            await say(ctx, bot, lead.tg_id, texts.reminders_ask(), kb.reminders_choice())

    # --- payment bridge ------------------------------------------------------------------------------------------------------

    @staticmethod
    def _pay_role(lead: Lead) -> Role:
        step = lead.step or ""
        if step.startswith(PAY_PREFIX):
            try:
                return Role(step[len(PAY_PREFIX):])
            except ValueError:
                pass
        return Role(lead.rec_role) if lead.rec_role else Role.BASE

    async def _pay_intent(self, ctx: AppContext, bot: Bot, lead: Lead, role: Role | None) -> None:
        """`pay_main` (the recommended tariff) / `try_lite` (the entry tariff): explain the in-bot payment."""
        target = role or (Role(lead.rec_role) if lead.rec_role else None)
        if target is None:  # pressed before any recommendation: compute it from the answers
            target, _ = choose_role(Answers(lead.stage, lead.volume, lead.goal))
        lead = await self._update(lead, step=f"{PAY_PREFIX}{target.value}")
        await self._send_pay_howto(ctx, bot, lead, target)

    async def _send_pay_howto(self, ctx: AppContext, bot: Bot, lead: Lead, role: Role) -> None:
        tariffs = await self._tariff_set(lead.tg_id, "pay")
        if tariffs is None:
            await say(ctx, bot, lead.tg_id, texts.pay_howto_unavailable(self.support), kb.manager_markup(self.support))
            return
        await say(ctx, bot, lead.tg_id, texts.pay_howto(tariffs.get(role)), kb.pay_howto())

    async def _go_pay(self, ctx: AppContext, bot: Bot, lead: Lead) -> None:
        """Enter the EXISTING in-bot checkout with the tariff preselected (link flow if unlinked, optional promo, quote,
        payment button, status polling). Status `link_sent` + the M5.3 check are scheduled here."""
        uid = lead.tg_id
        role = self._pay_role(lead)
        tariffs = await self._tariff_set(uid, "go_pay")
        if tariffs is None:
            await say(ctx, bot, uid, texts.pay_howto_unavailable(self.support), kb.manager_markup(self.support))
            return
        tariff = tariffs.get(role)
        # (a person handed to the manager may still pay: the checkout is the same, their status stays `handoff`)
        await self._update(lead, status=self._keep_handoff(lead, Status.LINK_SENT), step=f"{PAY_PREFIX}{role.value}")
        await self._event(uid, "link_sent", role.value)
        await choose_tariff(ctx, bot, uid, tariff.id)

    async def _pay_done(self, ctx: AppContext, bot: Bot, lead: Lead) -> None:
        """M5.3 answer: believe the wrapper, not the person's word."""
        uid = lead.tg_id
        try:
            me = await self.wrapper.me(uid)
        except WRAPPER_FAILURES as exc:
            await fail(ctx, bot, uid, exc, "me")
            return
        if self._paid(me):
            await self.store.mark_paid(uid, tariff=me.tenant.tariff_name if me.tenant else None, by_manager=False)
            await self._announce_paid(ctx, bot, uid)
        else:
            await say(ctx, bot, uid, texts.PAY_NOT_CONFIRMED, kb.pay_recheck())

    # --- free text ------------------------------------------------------------------------------------------------------------

    @_shown(default=True)
    async def on_text(self, ctx: AppContext, bot: Bot, uid: int, text: str, user: User | None = None) -> bool:
        """Free text from a funnel participant. False = not a funnel participant (the caller falls through)."""
        lead = await self.store.get_lead(uid)
        if lead is None or lead.is_tombstone or lead.status == Status.PAID:
            return False
        lead = await self._touch(lead, user)
        await self.store.push_message(uid, text)
        step = lead.step or GREET
        if step == S3:
            lead = await self._update(lead, niche_text=text[:1000])
            await self._show(ctx, bot, lead, P1)
        elif step == P1_OTHER:
            lead = await self._update(lead, pain="p1_other", pain_text=text[:1000])
            await self._show(ctx, bot, lead, I1)
        else:
            await self._event(uid, "free_text", step)
            await self._free_question(ctx, bot, lead, text)
        await sync_schedule(self.store, uid)
        return True

    async def _free_question(self, ctx: AppContext, bot: Bot, lead: Lead, text: str) -> None:
        uid, step = lead.tg_id, lead.step or GREET
        trigger = triggers.classify(text)
        if trigger == "discount":
            await self._event(uid, "trigger", trigger)
            await self._show(ctx, bot, lead, DISCOUNT)
            return
        if trigger is not None:
            await self._event(uid, "trigger", trigger)
            priority = "high" if trigger in triggers.HIGH_PRIORITY else "normal"
            await self._handoff(ctx, bot, lead, reason=trigger, priority=priority)
            return

        result = await self.ai.ask(uid, text, step)
        keyboard = kb.after_ai_resume() if step in _RESUME_STEPS else kb.after_ai()
        if result.status == "answer" and result.reply is not None:
            await self._event(uid, "ai", "answer")
            if result.handoff:
                await say(ctx, bot, uid, result.reply)
                await self._handoff(ctx, bot, lead, reason="ai_handoff")
            elif result.objection_code and lead.status != Status.HANDOFF:
                await self._objection(ctx, bot, lead, result.objection_code)  # the script speaks, not the model
            else:
                await say(ctx, bot, uid, result.reply, keyboard)
        elif result.status == "blocked":
            await self._event(uid, "ai", "blocked")
            await say(ctx, bot, uid, texts.AI_BLOCKED, keyboard)
        elif result.status == "rate_limited":
            await self._event(uid, "ai", "rate_limited")
            await say(ctx, bot, uid, texts.AI_RATE_LIMITED, keyboard)
        else:  # failed / ceiling: the provider fallback (the layer already logged the ERROR)
            await self._event(uid, "ai", result.status)
            await say(ctx, bot, uid, texts.ai_unavailable(self.support), keyboard)

    # --- hand-off ---------------------------------------------------------------------------------------------------------------

    async def _handoff(self, ctx: AppContext, bot: Bot, lead: Lead, *, reason: str, priority: str = "normal") -> None:
        uid = lead.tg_id
        lead = await self.store.get_lead(uid) or lead
        if lead.status == Status.HANDOFF:
            await say(ctx, bot, uid, texts.handoff_already(self.support), kb.manager_markup(self.support))
            return
        text = handoff_card.card(
            lead, reason=reason, priority=priority,
            objections=await self.store.objection_codes(uid), messages=await self.store.recent_messages(uid),
        )
        try:
            await bot.send_message(self.manager_chat_id, text)
        except TelegramAPIError:
            log.exception("handoff_card_failed", extra={"tg_user": uid, "reason": reason})
            await say(ctx, bot, uid, texts.handoff_failed(self.support), kb.manager_markup(self.support))
            return
        await self._update(
            lead, status=Status.HANDOFF.value, step=HANDOFF, handoff_reason=reason, handoff_at=self.clock(),
            handoff_priority=priority,
        )
        await self._event(uid, "handoff", reason)
        await say(ctx, bot, uid, texts.handoff_user(self.support), kb.handoff_done(self.support))

    # --- hooks from OTHER flows (isolated: they never raise into a money flow) --------------------------------------------------

    @_isolated
    async def on_payment_success(self, ctx: AppContext, bot: Bot, uid: int, me: schemas.MeResponse | None) -> None:
        """A payment was credited: a lead becomes `paid` (from the wrapper's `paid_access_active`, not from anyone's word)
        and M5.6 is sent once."""
        if me is None or not self._paid(me):
            return
        await self._paid_seen(ctx, bot, uid, me)

    @_isolated
    async def on_paid_start(self, ctx: AppContext, bot: Bot, uid: int, me: schemas.MeResponse) -> None:
        """`/start` of a person who already has paid access: they skip the funnel; a lead row (if any) becomes `paid`."""
        await self._paid_seen(ctx, bot, uid, me)

    @_isolated
    async def on_blocked(self, uid: int) -> None:
        """Telegram refused delivery (the person blocked the bot): status `blocked`, reminders cancelled."""
        lead = await self.store.get_lead(uid)
        if lead is None or lead.is_tombstone or lead.status in (Status.PAID, Status.BLOCKED):
            return
        await self.store.update_lead(uid, status=Status.BLOCKED.value)
        await self.store.replace_followup(uid, None, None)

    async def on_stop(self, uid: int) -> bool:
        """/stop: delete ALL funnel data of this person, keep a minimal tombstone. Raises StoreError (already logged)."""
        result = await self.store.purge_user(uid)
        return result.had_data

    async def manager_paid(self, ctx: AppContext, bot: Bot, tg_id: int, note: str | None) -> str:
        """`/paid <tg_id> [note]` (manager only): mark a lead paid for a non-bot payment. Returns the reply for the manager."""
        try:
            lead = await self.store.get_lead(tg_id)
            if lead is None or lead.is_tombstone:
                return f"Лид {tg_id} не найден."
            if lead.status == Status.PAID:
                return f"Лид {tg_id} уже отмечен как оплативший."
            await self.store.mark_paid(tg_id, tariff=None, by_manager=True, note=note)
            await self._event(tg_id, "paid_by_manager")
            await self._announce_paid(ctx, bot, tg_id)
        except StoreError:
            return "Не удалось записать: ошибка базы данных (см. лог)."
        return f"Готово: {tg_id} отмечен как оплативший."

    # --- reminders (called by the loop) ------------------------------------------------------------------------------------------

    async def deliver_followup(self, ctx: AppContext, bot: Bot, item: Followup) -> bool:
        """Send one CLAIMED follow-up (at-most-once). True when a message went out."""
        uid = item.tg_id
        lead = await self.store.get_lead(uid)
        plan = next_followup(lead) if lead is not None else None
        if lead is None or plan is None or plan[0] != item.kind:
            await self.store.finish_followup(item.id, "cancelled")  # the lead changed since it was scheduled
            return False
        try:
            me = await self.wrapper.me(uid)
        except WRAPPER_FAILURES as exc:
            log.error("followup_wrapper_check_failed", extra={"tg_user": uid, "error": type(exc).__name__})
            await self.store.finish_followup(item.id, "cancelled")
            await self.store.replace_followup(uid, item.kind, self.clock() + dt.timedelta(minutes=10))
            return False
        if not me.consented:
            await self.store.finish_followup(item.id, "cancelled")
            return False
        if self._paid(me):
            await self.store.finish_followup(item.id, "cancelled")
            await self.store.mark_paid(uid, tariff=me.tenant.tariff_name if me.tenant else None, by_manager=False)
            if item.kind == "m53":
                await self._announce_paid(ctx, bot, uid)
            return False

        entry_tariff: FunnelTariff | None = None
        if item.kind == "f3":
            tariffs = await self._tariff_set(uid, "f3")
            entry_tariff = tariffs.get(Role.ENTRY) if tariffs else None
        text, markup = {
            "f1": lambda: (texts.F1, kb.f1()),
            "f2": lambda: (texts.f2(self.kb), kb.f2()),
            "f3": lambda: (texts.f3(entry_tariff), kb.f3(entry_tariff.name if entry_tariff else None)),
            "m53": lambda: (texts.PAY_CHECK, kb.pay_check()),
        }[item.kind]()
        message = await say(ctx, bot, uid, text, markup)
        if message is None:  # blocked / undeliverable: `say` already reported it, no further tries
            await self.store.finish_followup(item.id, "cancelled")
            return False
        await self.store.finish_followup(item.id, "sent")
        await self._event(uid, "followup", item.kind)
        now = self.clock()
        if item.kind == "m53":
            await self.store.update_lead(uid, pay_check_sent=True)
        else:
            # `answered_followup` is deliberately not reset: "answered F1" is what makes F2 and F3 eligible.
            fields: dict[str, Any] = {"followups_sent": lead.followups_sent + 1, "last_followup_at": now}
            if item.kind == "f3":
                fields["status"] = Status.COLD.value  # after F3 the dialogue closes; /start continues from the saved place
            await self.store.update_lead(uid, **fields)
        await sync_schedule(self.store, uid)
        return True
