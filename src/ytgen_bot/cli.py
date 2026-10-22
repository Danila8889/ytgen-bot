"""Operator commands: `migrate`, `funnel-report`, `ai-sample`, `ai-replay`. They read the same environment as the bot."""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
from typing import Any

from .ai.client import KieChatClient
from .ai.layer import Responder
from .ai.replay import format_results, run_cases
from .config import Settings
from .db import migrate
from .db.engine import build_url, make_engine
from .db.store import PgStore
from .funnel.tariffs import TariffProvider
from .knowledge import load as load_knowledge
from .wrapper import WrapperClient


def _pct(part: int, whole: int) -> str:
    return f"{part} из {whole} ({100 * part / whole:.0f}%)" if whole else f"{part} из 0 (-)"


def format_report(data: dict[str, Any]) -> str:
    lines = [
        "Воронка YTGen: отчёт (метрики PDF, раздел 12)",
        "",
        f"Лидов (без /stop): {data['started']}",
        f"Старт -> конец SPIN: {_pct(data['spin_done'], data['started'])}",
        "",
        "Где отваливаются (последний шаг тех, кто не дошёл до конца SPIN):",
        *(f"  {row['step']}: {row['n']}" for row in data["dropoff"]),
        *(["  -"] if not data["dropoff"] else []),
        "",
        f"Рекомендация -> ссылка на оплату: {_pct(data['link_sent'], data['recommended'])}",
        "",
        "Возражения (лидов; из них дошли до оплаты):",
        *(f"  {row['code']}: {row['leads']} ({row['reached_link']} дошли до link_sent)" for row in data["objections"]),
        *(["  -"] if not data["objections"] else []),
        "",
        "Причины передачи менеджеру:",
        *(f"  {row['reason']}: {row['n']}" for row in data["handoff_reasons"]),
        *(["  -"] if not data["handoff_reasons"] else []),
        "",
        "Источник -> оплата:",
        *(f"  {row['source']}: {_pct(row['paid'], row['leads'])}" for row in data["sources"]),
        *(["  -"] if not data["sources"] else []),
    ]
    return "\n".join(lines)


async def _with_store(settings: Settings, fn: Any) -> Any:
    engine = make_engine(build_url(settings))
    try:
        return await fn(PgStore(engine), engine)
    finally:
        await engine.dispose()


def run_migrate(settings: Settings) -> int:
    async def go(_: PgStore, engine: Any) -> None:
        await migrate.upgrade_to_head(engine)

    asyncio.run(_with_store(settings, go))
    print(f"migrations at head ({migrate.head_revision()})")
    return 0


def run_report(settings: Settings) -> int:
    async def go(store: PgStore, _: Any) -> dict[str, Any]:
        return await store.report_data()

    print(format_report(asyncio.run(_with_store(settings, go))))
    return 0


def run_ai_sample(settings: Settings, n: int) -> int:
    """A random sample of the AI log for the weekly review (it prints the people's own questions: owner's terminal only)."""
    async def go(store: PgStore, _: Any) -> list[dict[str, Any]]:
        return await store.ai_sample(n)

    rows = asyncio.run(_with_store(settings, go))
    for row in rows:
        print(f"--- {row['ts']:%Y-%m-%d %H:%M} tg={row['tg_id']} step={row['step']} outcome={row['outcome']} "
              f"code={row['objection_code']} handoff={row['handoff']} {row['latency_ms']} ms")
        print(f"Q: {row['question']}")
        print(f"A: {row['reply']}")
        if row["reason"]:
            print(f"reason: {row['reason']}")
    print(f"\n{len(rows)} of the log (random sample of up to {n})")
    return 0


def run_ai_replay(settings: Settings) -> int:
    async def go() -> int:
        wrapper = WrapperClient(settings.wrapper_base_url, settings.service_key)
        client = KieChatClient(
            api_key=settings.kie_api_key, base_url=settings.kie_api_url, model=settings.llm_model,
            timeout_s=settings.llm_timeout_s,
        )
        try:
            responder = Responder(
                client=client, tariffs=TariffProvider(wrapper.public_tariffs, settings.tariff_cache_ttl_s),
                kb=load_knowledge(settings.support_contact), max_tokens=settings.llm_max_tokens,
                temperature=settings.llm_temperature,
            )
            results = await run_cases(responder)
        finally:
            await client.aclose()
            await wrapper.aclose()
        print(f"model: {settings.llm_model} @ {dt.datetime.now(dt.UTC):%Y-%m-%d %H:%M} UTC\n")
        print(format_results(results))
        return 0 if all(r.passed for r in results) else 1

    return asyncio.run(go())


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("command", nargs="?", default="run",
                        choices=["run", "selfcheck", "migrate", "funnel-report", "ai-sample", "ai-replay"])
    parser.add_argument("--n", type=int, default=20, help="ai-sample: how many rows")
