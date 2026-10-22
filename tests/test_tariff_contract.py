"""The funnel's strict view of the wrapper's public tariff list: roles by rank, loud contract errors, no stale prices."""
from __future__ import annotations

import json
import pathlib

import httpx
import pytest

from tests.fake_wrapper import SERVICE_KEY, FakeWrapper, funnel_tariffs
from ytgen_bot.funnel import render
from ytgen_bot.funnel.model import Role
from ytgen_bot.funnel.tariffs import (
    TariffContractError,
    TariffProvider,
    check_rows,
    parse_public_tariffs,
)
from ytgen_bot.wrapper import WrapperClient, WrapperError, WrapperUnavailable

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "public_tariffs_2026_10_22.json"


def rows() -> list[dict]:
    return funnel_tariffs()


def test_roles_are_assigned_by_rank_not_by_position_or_name():
    shuffled = rows()
    for t, misleading in zip(shuffled, ["Ultra", "Lite Start", "Pro", "Base"], strict=True):
        t["name"] = misleading  # names say nothing: price decides
    roles = parse_public_tariffs(shuffled).by_role
    assert {r: t.price_rub for r, t in roles.items()} == {
        Role.ENTRY: 4321, Role.BASE: 11111, Role.PRO: 22222, Role.ULTRA: 33333}
    assert roles[Role.ENTRY].is_monthly is False and all(roles[r].is_monthly for r in (Role.BASE, Role.PRO, Role.ULTRA))
    assert {t.name for t in roles.values()} == {"Ultra", "Lite Start", "Pro", "Base"}  # names are shown as the API gave them


def test_the_recorded_production_response_parses_into_the_four_roles():
    """Fixture-replay: the real /v1/public/tariffs answer of 2026-10-22 (wrapper 0fe0f06)."""
    data = json.loads(FIXTURE.read_text())
    tariffs = parse_public_tariffs(data)
    assert [(r.value, t.id) for r, t in tariffs.ordered] == [("entry", 11), ("base", 7), ("pro", 8), ("ultra", 9)]
    entry = tariffs.get(Role.ENTRY)
    assert entry.period_days == 7 and entry.capacity.window_hours == 168 and entry.capacity.image_stock_videos_per_window == 5
    assert render.window_phrase(entry.capacity.window_hours) == "за 7 дн."  # labelled from window_hours, not a constant
    base = tariffs.get(Role.BASE)
    assert base.capacity.image_stock_videos_per_day == 2 and render.window_phrase(base.capacity.window_hours) == "в сутки"


def test_the_short_plans_job_limit_is_per_window_and_the_funnel_never_reads_daily_job_limit():
    entry = parse_public_tariffs(rows()).get(Role.ENTRY)
    assert "daily_job_limit" not in type(entry).model_fields
    assert render.other_videos(entry) == "до 5 на картинках и стоках за 7 дн."
    assert render.ai_videos(entry) == "до 2 на ИИ-клипах за 7 дн."


@pytest.mark.parametrize(
    "mutate,message",
    [
        (lambda r: [{k: v for k, v in t.items() if k != "is_monthly"} if t["id"] == 22 else t for t in r], "no is_monthly"),
        (lambda r: [{k: v for k, v in t.items() if k != "capacity"} if t["id"] == 22 else t for t in r], "no capacity"),
        (lambda r: [dict(t, capacity=None) if t["id"] == 22 else t for t in r], "no capacity"),
        (lambda r: [dict(t, capacity={k: v for k, v in t["capacity"].items() if k != "window_hours"}) if t["id"] == 22 else t for t in r],
         "window_hours"),
        (lambda r: [dict(t, capacity={k: v for k, v in t["capacity"].items() if not k.startswith("image_stock")}) if t["id"] == 22 else t for t in r],
         "image_stock"),
        (lambda r: [], "empty"),
        (lambda r: {"tariffs": r}, "not a list"),
        (lambda r: ["x"], "not an object"),
    ],
)
def test_a_row_without_is_monthly_or_a_complete_capacity_is_a_contract_error(mutate, message):
    with pytest.raises(TariffContractError, match=message):
        check_rows(mutate(rows()))
    with pytest.raises(TariffContractError):
        parse_public_tariffs(mutate(rows()))  # never defaulted


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: [dict(t, is_monthly=False, period_days=7) if t["id"] == 22 else t for t in r],  # two non-monthly
        lambda r: [t for t in r if t["id"] != 21],  # no non-monthly tariff
        lambda r: [t for t in r if t["id"] != 24],  # two monthly
        lambda r: [*r, dict(r[0], id=99, price_rub=44444)],  # four monthly
        lambda r: [dict(t, price_rub=22222) if t["id"] == 22 else t for t in r],  # ambiguous monthly ranks
    ],
)
def test_the_wrong_number_of_tariffs_or_ambiguous_ranks_is_an_error_not_a_guess(mutate):
    check_rows(mutate(rows()))  # every row itself is fine...
    with pytest.raises(TariffContractError):
        parse_public_tariffs(mutate(rows()))  # ...but the roles cannot be assigned


async def test_the_provider_caches_for_the_ttl_and_never_serves_stale_prices_after_it():
    clock = [0.0]
    calls = {"n": 0}
    state: dict[str, object] = {"rows": rows(), "error": None}

    async def fetch():
        calls["n"] += 1
        if state["error"]:
            raise state["error"]  # type: ignore[misc]
        return state["rows"]

    provider = TariffProvider(fetch, 300.0, clock=lambda: clock[0])
    first = await provider.get()
    assert await provider.get() is first and calls["n"] == 1
    clock[0] = 299.0
    assert await provider.get() is first
    clock[0] = 301.0
    state["error"] = WrapperUnavailable("down")
    with pytest.raises(WrapperUnavailable):  # the wrapper is unreadable: an error, not yesterday's price list
        await provider.get()
    state["error"] = None
    state["rows"] = [dict(t, price_rub=t["price_rub"] + 1) for t in rows()]
    assert (await provider.get()).get(Role.ENTRY).price_rub == 4322


async def test_a_bad_list_is_never_cached():
    provider = TariffProvider(lambda: _bad(), 300.0)
    for _ in range(2):
        with pytest.raises(TariffContractError):
            await provider.get()


async def _bad():
    return [{"id": 1}]


# --- the wrapper client's public route -----------------------------------------------------------------------------------------------------


async def test_public_tariffs_is_unauthenticated_so_the_service_key_never_travels_to_it():
    fake = FakeWrapper()
    client = WrapperClient("https://wrapper.example.test", SERVICE_KEY, transport=fake.transport())
    data = await client.public_tariffs()
    await client.aclose()
    assert isinstance(data, list) and len(data) == len(fake.tariffs)
    (call,) = fake.calls("GET", "/v1/public/tariffs")
    assert "x-bot-service-key" not in call["headers"] and "x-telegram-user-id" not in call["headers"]


async def test_public_tariffs_errors_are_the_usual_wrapper_failures():
    fake = FakeWrapper()
    client = WrapperClient("https://wrapper.example.test", SERVICE_KEY, transport=fake.transport())
    fake.queue_error("GET", "/v1/public/tariffs", 500, "boom")
    with pytest.raises(WrapperError):
        await client.public_tariffs()
    fake.queue("GET", "/v1/public/tariffs", httpx.ConnectError("down"))
    with pytest.raises(WrapperUnavailable):
        await client.public_tariffs()
    await client.aclose()


def test_the_checkout_screens_tolerate_an_older_wrapper_without_is_monthly():
    """`is_monthly` is optional in the checkout schema (only the funnel requires it), so a missing field cannot break checkout."""
    from ytgen_bot import schemas

    old = {k: v for k, v in rows()[0].items() if k != "is_monthly"}
    assert schemas.Tariff.model_validate(old).is_monthly is None
