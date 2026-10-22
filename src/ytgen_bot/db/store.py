"""The only code that talks SQL. Every public method either succeeds or raises `StoreError` (after an ERROR log
that names the operation and the error class, never a value): callers decide what the user sees, and the money
flows (checkout, payment status, link, referral, withdrawal) wrap their funnel hooks so a StoreError never reaches
them (see `funnel.isolation`).
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import functools
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import asyncpg
import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ..funnel.model import Lead, Status
from . import schema as t

log = logging.getLogger(__name__)

MESSAGE_BUFFER = 5
MAX_STORED_TEXT = 1000
# An AI call was actually made to the provider for these outcomes (they count against the limits).
CALL_OUTCOMES = ("ok", "validator_blocked", "parse_failed", "provider_error")
AI_OUTCOMES = (*CALL_OUTCOMES, "rate_limited", "ceiling")
# `provider_error` rows with these reasons are calls that were NOT made (breaker open / no tariff block): not counted.
NO_CALL_REASONS = ("breaker_open", "tariff_block_unavailable")

_LEAD_COLUMNS = {c.name for c in t.leads.c} - {"tg_id", "created_at"}

_DB_ERRORS = (SQLAlchemyError, asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError)


class StoreError(Exception):
    """A funnel database operation failed (already logged at ERROR)."""


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _guard[**P, R](fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await fn(*args, **kwargs)
        except StoreError:
            raise
        except _DB_ERRORS as exc:
            log.error("funnel_store_error", extra={"op": fn.__name__, "error": type(exc).__name__})
            raise StoreError(fn.__name__) from exc

    return wrapper


def _row_to_lead(row: Mapping[str, Any]) -> Lead:
    return Lead(**{f.name: row[f.name] for f in dataclasses.fields(Lead)})


@dataclasses.dataclass(frozen=True)
class Followup:
    id: int
    tg_id: int
    kind: str
    due_at: dt.datetime


@dataclasses.dataclass(frozen=True)
class PurgeResult:
    had_data: bool


class PgStore:
    def __init__(self, engine: AsyncEngine, clock: Callable[[], dt.datetime] = utcnow) -> None:
        self._engine = engine
        self._clock = clock

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    # --- health ---------------------------------------------------------------------------------

    @_guard
    async def ping(self) -> None:
        async with self._engine.connect() as conn:
            await conn.execute(sa.text("SELECT 1"))

    # --- leads ----------------------------------------------------------------------------------

    @_guard
    async def get_lead(self, uid: int) -> Lead | None:
        async with self._engine.connect() as conn:
            row = (await conn.execute(sa.select(t.leads).where(t.leads.c.tg_id == uid))).mappings().first()
        return _row_to_lead(row) if row is not None else None

    @_guard
    async def upsert_lead(
        self, uid: int, *, username: str | None, first_name: str | None, source: str | None
    ) -> Lead:
        """Create the lead (first touch: `source` is stored now and never changed later), refresh the names of an
        existing one, or revive a /stop tombstone as a brand-new lead (nothing of the old data survives)."""
        now = self._clock()
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(sa.select(t.leads).where(t.leads.c.tg_id == uid).with_for_update())
            ).mappings().first()
            if row is not None and row["status"] != Status.STOPPED:
                await conn.execute(
                    sa.update(t.leads)
                    .where(t.leads.c.tg_id == uid)
                    .values(username=username, first_name=first_name, updated_at=now)
                )
            else:
                if row is not None:
                    await conn.execute(sa.delete(t.leads).where(t.leads.c.tg_id == uid))
                await conn.execute(
                    sa.insert(t.leads).values(
                        tg_id=uid, username=username, first_name=first_name, source=source,
                        status=Status.NEW.value, last_activity_at=now, created_at=now, updated_at=now,
                    )
                )
            fresh = (await conn.execute(sa.select(t.leads).where(t.leads.c.tg_id == uid))).mappings().one()
        return _row_to_lead(fresh)

    @_guard
    async def update_lead(self, uid: int, **fields: Any) -> None:
        unknown = set(fields) - _LEAD_COLUMNS
        if unknown:
            raise ValueError(f"unknown lead columns: {sorted(unknown)}")
        fields["updated_at"] = self._clock()
        async with self._engine.begin() as conn:
            await conn.execute(sa.update(t.leads).where(t.leads.c.tg_id == uid).values(**fields))

    @_guard
    async def touch_lead(self, uid: int) -> None:
        now = self._clock()
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.update(t.leads).where(t.leads.c.tg_id == uid).values(last_activity_at=now, updated_at=now)
            )

    @_guard
    async def mark_paid(self, uid: int, *, tariff: str | None, by_manager: bool, note: str | None = None) -> bool:
        """Set status `paid`, drop pending follow-ups. True only on the transition (False: already paid / no lead)."""
        now = self._clock()
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    sa.select(t.leads.c.status, t.leads.c.notes).where(t.leads.c.tg_id == uid).with_for_update()
                )
            ).first()
            if row is None or row.status in (Status.PAID, Status.STOPPED):
                return False
            notes = (f"{row.notes}\n{note}" if row.notes and note else note or row.notes)
            await conn.execute(
                sa.update(t.leads)
                .where(t.leads.c.tg_id == uid)
                .values(
                    status=Status.PAID.value, paid_tariff=tariff, paid_at=now, paid_by_manager=by_manager,
                    notes=notes, updated_at=now,
                )
            )
            await conn.execute(
                sa.update(t.followups)
                .where(t.followups.c.tg_id == uid, t.followups.c.state == "pending")
                .values(state="cancelled", done_at=now)
            )
        return True

    @_guard
    async def claim_paid_announcement(self, uid: int) -> bool:
        """Exactly-once M5.6: True for the single caller that flips `paid_announced`."""
        async with self._engine.begin() as conn:
            res = await conn.execute(
                sa.update(t.leads)
                .where(t.leads.c.tg_id == uid, t.leads.c.paid_announced.is_(False))
                .values(paid_announced=True)
            )
        return res.rowcount == 1

    # --- events / objections / messages --------------------------------------------------------

    @_guard
    async def add_event(self, uid: int, event: str, value: str | None = None) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(sa.insert(t.events).values(ts=self._clock(), tg_id=uid, event=event, value=value))

    @_guard
    async def event_count(self, uid: int, event: str) -> int:
        async with self._engine.connect() as conn:
            return int(
                (
                    await conn.execute(
                        sa.select(sa.func.count()).select_from(t.events).where(
                            t.events.c.tg_id == uid, t.events.c.event == event
                        )
                    )
                ).scalar_one()
            )

    @_guard
    async def add_objection(self, uid: int, code: str) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(sa.insert(t.objections).values(tg_id=uid, code=code, ts=self._clock()))

    @_guard
    async def objection_codes(self, uid: int) -> list[str]:
        async with self._engine.connect() as conn:
            rows = await conn.execute(
                sa.select(t.objections.c.code).where(t.objections.c.tg_id == uid).order_by(t.objections.c.id)
            )
        return [r[0] for r in rows]

    @_guard
    async def push_message(self, uid: int, text: str) -> None:
        """Rolling buffer: the last MESSAGE_BUFFER free-text messages of this user, for the manager card."""
        text = text[:MAX_STORED_TEXT]
        async with self._engine.begin() as conn:
            await conn.execute(sa.insert(t.messages).values(tg_id=uid, ts=self._clock(), text=text))
            keep = (
                sa.select(t.messages.c.id)
                .where(t.messages.c.tg_id == uid)
                .order_by(t.messages.c.id.desc())
                .limit(MESSAGE_BUFFER)
            )
            await conn.execute(sa.delete(t.messages).where(t.messages.c.tg_id == uid, t.messages.c.id.not_in(keep)))

    @_guard
    async def recent_messages(self, uid: int) -> list[str]:
        async with self._engine.connect() as conn:
            rows = await conn.execute(
                sa.select(t.messages.c.text)
                .where(t.messages.c.tg_id == uid)
                .order_by(t.messages.c.id.desc())
                .limit(MESSAGE_BUFFER)
            )
        return [r[0] for r in reversed(rows.all())]

    # --- AI log and limits ----------------------------------------------------------------------

    @_guard
    async def log_ai(
        self,
        uid: int,
        *,
        step: str | None,
        question: str,
        reply: str | None,
        objection_code: str | None,
        handoff: bool,
        reason: str | None,
        model: str | None,
        latency_ms: int | None,
        outcome: str,
    ) -> None:
        if outcome not in AI_OUTCOMES:
            raise ValueError(f"unknown ai outcome {outcome!r}")
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.insert(t.ai_log).values(
                    ts=self._clock(), tg_id=uid, step=step, question=question[:MAX_STORED_TEXT], reply=reply,
                    objection_code=objection_code, handoff=handoff, reason=reason, model=model,
                    latency_ms=latency_ms, outcome=outcome,
                )
            )

    @_guard
    async def ai_calls_since(self, since: dt.datetime, *, uid: int | None = None) -> int:
        query = (
            sa.select(sa.func.count()).select_from(t.ai_log)
            .where(
                t.ai_log.c.ts >= since,
                t.ai_log.c.outcome.in_(CALL_OUTCOMES),
                sa.func.coalesce(t.ai_log.c.reason, "").not_in(NO_CALL_REASONS),  # skipped, not called
            )
        )
        if uid is not None:
            query = query.where(t.ai_log.c.tg_id == uid)
        async with self._engine.connect() as conn:
            return int((await conn.execute(query)).scalar_one())

    @_guard
    async def ceiling_logged_since(self, since: dt.datetime) -> bool:
        async with self._engine.connect() as conn:
            count = (
                await conn.execute(
                    sa.select(sa.func.count()).select_from(t.ai_log)
                    .where(t.ai_log.c.ts >= since, t.ai_log.c.outcome == "ceiling")
                )
            ).scalar_one()
        return int(count) > 0

    @_guard
    async def purge_ai_log(self, before: dt.datetime) -> int:
        async with self._engine.begin() as conn:
            res = await conn.execute(sa.delete(t.ai_log).where(t.ai_log.c.ts < before))
        return int(res.rowcount or 0)

    @_guard
    async def ai_sample(self, n: int) -> list[dict[str, Any]]:
        async with self._engine.connect() as conn:
            rows = await conn.execute(sa.select(t.ai_log).order_by(sa.func.random()).limit(n))
            return [dict(r) for r in rows.mappings()]

    # --- follow-ups ------------------------------------------------------------------------------

    @_guard
    async def replace_followup(self, uid: int, kind: str | None, due_at: dt.datetime | None) -> None:
        """Cancel the pending follow-up of this user and (when `kind` is given) schedule the next one."""
        now = self._clock()
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.update(t.followups)
                .where(t.followups.c.tg_id == uid, t.followups.c.state == "pending")
                .values(state="cancelled", done_at=now)
            )
            if kind is not None and due_at is not None:
                await conn.execute(
                    sa.insert(t.followups).values(tg_id=uid, kind=kind, due_at=due_at, state="pending", created_at=now)
                )

    @_guard
    async def pending_followup(self, uid: int) -> Followup | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    sa.select(t.followups).where(t.followups.c.tg_id == uid, t.followups.c.state == "pending")
                )
            ).mappings().first()
        return Followup(row["id"], row["tg_id"], row["kind"], row["due_at"]) if row else None

    @_guard
    async def claim_due_followups(self, now: dt.datetime, limit: int) -> list[Followup]:
        """At-most-once: a row is flipped to `claimed` BEFORE anything is sent. A crash between the claim and the
        send loses that one message (it is never sent twice)."""
        due = (
            sa.select(t.followups.c.id)
            .where(t.followups.c.state == "pending", t.followups.c.due_at <= now)
            .order_by(t.followups.c.due_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        async with self._engine.begin() as conn:
            rows = await conn.execute(
                sa.update(t.followups)
                .where(t.followups.c.id.in_(due), t.followups.c.state == "pending")
                .values(state="claimed", claimed_at=now)
                .returning(t.followups.c.id, t.followups.c.tg_id, t.followups.c.kind, t.followups.c.due_at)
            )
            return [Followup(*r) for r in rows.all()]

    @_guard
    async def finish_followup(self, followup_id: int, state: str) -> None:
        if state not in ("sent", "cancelled"):
            raise ValueError(state)
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.update(t.followups).where(t.followups.c.id == followup_id).values(state=state, done_at=self._clock())
            )

    # --- retention -------------------------------------------------------------------------------

    @_guard
    async def purge_user(self, uid: int) -> PurgeResult:
        """/stop: delete ALL funnel data of this user in one transaction and keep a minimal tombstone (tg id,
        status `stopped`, timestamp) so reminders can never resume."""
        now = self._clock()
        async with self._engine.begin() as conn:
            had = False
            for table in (t.events, t.objections, t.ai_log, t.messages, t.followups):
                res = await conn.execute(sa.delete(table).where(table.c.tg_id == uid))
                had = had or bool(res.rowcount)
            res = await conn.execute(sa.delete(t.leads).where(t.leads.c.tg_id == uid))
            had = had or bool(res.rowcount)
            if had:
                await conn.execute(
                    sa.insert(t.leads).values(
                        tg_id=uid, status=Status.STOPPED.value, stopped_at=now,
                        last_activity_at=now, created_at=now, updated_at=now,
                    )
                )
        return PurgeResult(had_data=had)

    @_guard
    async def user_row_counts(self, uid: int) -> dict[str, int]:
        """For tests and the retention audit: how many rows each table holds for this user."""
        out: dict[str, int] = {}
        async with self._engine.connect() as conn:
            for table in (t.leads, t.events, t.objections, t.ai_log, t.messages, t.followups):
                out[table.name] = int(
                    (await conn.execute(sa.select(sa.func.count()).select_from(table).where(table.c.tg_id == uid)))
                    .scalar_one()
                )
        return out

    # --- report ----------------------------------------------------------------------------------

    @_guard
    async def report_data(self) -> dict[str, Any]:
        async with self._engine.connect() as conn:
            return await _report(conn)


async def _rows(conn: AsyncConnection, sql: str) -> list[dict[str, Any]]:
    return [dict(r) for r in (await conn.execute(sa.text(sql))).mappings()]


async def _report(conn: AsyncConnection) -> dict[str, Any]:
    live = "status <> 'stopped'"
    started = (await conn.execute(sa.text(f"SELECT count(*) FROM funnel_leads WHERE {live}"))).scalar_one()
    spin_done = (
        await conn.execute(sa.text("SELECT count(DISTINCT tg_id) FROM funnel_events WHERE event = 'spin_done'"))
    ).scalar_one()
    return {
        "started": int(started),
        "spin_done": int(spin_done),
        "dropoff": await _rows(
            conn,
            f"SELECT coalesce(step, '-') AS step, count(*) AS n FROM funnel_leads "
            f"WHERE {live} AND status IN ('new', 'in_spin') GROUP BY 1 ORDER BY 2 DESC",
        ),
        "recommended": int(
            (await conn.execute(sa.text("SELECT count(DISTINCT tg_id) FROM funnel_events WHERE event = 'recommended'")))
            .scalar_one()
        ),
        "link_sent": int(
            (await conn.execute(sa.text("SELECT count(DISTINCT tg_id) FROM funnel_events WHERE event = 'link_sent'")))
            .scalar_one()
        ),
        "objections": await _rows(
            conn,
            "SELECT o.code AS code, count(DISTINCT o.tg_id) AS leads, "
            "count(DISTINCT o.tg_id) FILTER (WHERE EXISTS (SELECT 1 FROM funnel_events e "
            "WHERE e.tg_id = o.tg_id AND e.event = 'link_sent')) AS reached_link "
            "FROM funnel_objections o GROUP BY o.code ORDER BY leads DESC",
        ),
        "handoff_reasons": await _rows(
            conn,
            "SELECT handoff_reason AS reason, count(*) AS n FROM funnel_leads "
            "WHERE handoff_reason IS NOT NULL GROUP BY 1 ORDER BY 2 DESC",
        ),
        "sources": await _rows(
            conn,
            f"SELECT coalesce(source, 'none') AS source, count(*) AS leads, "
            f"count(*) FILTER (WHERE paid_at IS NOT NULL) AS paid FROM funnel_leads WHERE {live} "
            f"GROUP BY 1 ORDER BY 2 DESC",
        ),
    }


__all__ = ["AI_OUTCOMES", "CALL_OUTCOMES", "Followup", "PgStore", "PurgeResult", "StoreError"]
