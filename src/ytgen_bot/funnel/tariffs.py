"""The funnel's strict view of the wrapper's public tariff list.

Everything the funnel says about a tariff (name, price, period, limits) comes from here, i.e. from the wrapper;
nothing is a constant. Roles are assigned by rank, never by name:

  * ENTRY = the cheapest tariff with `is_monthly == false`;
  * BASE / PRO / ULTRA = the monthly tariffs sorted by ascending price.

A list that does not have exactly one non-monthly and three monthly tariffs (with distinct prices), a row without
`is_monthly`, or a row without a complete `capacity` is a CONTRACT ERROR: it is logged at ERROR by the caller and the
funnel falls back to the manager. Nothing is guessed or defaulted.
"""
from __future__ import annotations

import dataclasses
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from .model import Role

log = logging.getLogger(__name__)


class TariffContractError(Exception):
    """The wrapper's tariff list is not usable by the funnel. The message names the problem, never a value."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Capacity(_Strict):
    window_hours: int
    ai_video_videos_per_window: int
    ai_video_scenes_per_day: int
    images_per_day: int
    stock_per_day: int
    # 24 h plans carry the per-day image/stock video count, short plans (Lite Start) the per-window one.
    image_stock_videos_per_day: int | None = None
    image_stock_videos_per_window: int | None = None


class FunnelTariff(_Strict):
    id: int
    name: str
    price_rub: int
    period_days: int
    is_monthly: bool
    capacity: Capacity
    max_ai_scenes_per_video: int | None = None  # null = uncapped; only shown when it is a number

    @property
    def image_stock_videos(self) -> int:
        cap = self.capacity
        value = cap.image_stock_videos_per_day if cap.image_stock_videos_per_day is not None else (
            cap.image_stock_videos_per_window
        )
        assert value is not None  # enforced by parse
        return value


@dataclasses.dataclass(frozen=True)
class TariffSet:
    by_role: dict[Role, FunnelTariff]

    def get(self, role: Role) -> FunnelTariff:
        return self.by_role[role]

    def by_id(self, tariff_id: int) -> FunnelTariff | None:
        return next((t for t in self.by_role.values() if t.id == tariff_id), None)

    def role_of(self, tariff_id: int) -> Role | None:
        return next((r for r, t in self.by_role.items() if t.id == tariff_id), None)

    @property
    def ordered(self) -> list[tuple[Role, FunnelTariff]]:
        return [(r, self.by_role[r]) for r in (Role.ENTRY, Role.BASE, Role.PRO, Role.ULTRA)]


def check_rows(raw: object) -> list[FunnelTariff]:
    """Row-level contract: every row has `is_monthly` and a complete `capacity`. Used by the deploy self-check."""
    if not isinstance(raw, list) or not raw:
        raise TariffContractError("the public tariff list is empty or not a list")
    rows: list[FunnelTariff] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise TariffContractError(f"tariff row {index} is not an object")
        if "is_monthly" not in item:
            raise TariffContractError(f"tariff row {index} has no is_monthly")
        if not isinstance(item.get("capacity"), dict):
            raise TariffContractError(f"tariff row {index} has no capacity")
        try:
            row = FunnelTariff.model_validate(item)
        except ValidationError as exc:
            fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
            raise TariffContractError(f"tariff row {index} is incomplete: {', '.join(fields)}") from None
        cap = row.capacity
        if cap.image_stock_videos_per_day is None and cap.image_stock_videos_per_window is None:
            raise TariffContractError(f"tariff row {index} capacity has no image_stock_videos_per_day/_per_window")
        rows.append(row)
    return rows


def parse_public_tariffs(raw: object) -> TariffSet:
    rows = check_rows(raw)
    short = sorted((t for t in rows if not t.is_monthly), key=lambda t: t.price_rub)
    monthly = sorted((t for t in rows if t.is_monthly), key=lambda t: t.price_rub)
    if len(short) != 1 or len(monthly) != 3:
        raise TariffContractError(
            f"expected exactly one non-monthly and three monthly public tariffs, got {len(short)} and {len(monthly)}"
        )
    if len({t.price_rub for t in monthly}) != 3:
        raise TariffContractError("monthly tariffs do not have distinct prices, roles would be ambiguous")
    return TariffSet(
        {Role.ENTRY: short[0], Role.BASE: monthly[0], Role.PRO: monthly[1], Role.ULTRA: monthly[2]}
    )


class TariffProvider:
    """A short-TTL cache of the public tariff list. A stale list is never served after the TTL: if the wrapper
    cannot be read, the caller gets the error (and falls back to the manager), not yesterday's prices."""

    def __init__(
        self,
        fetch: Callable[[], Awaitable[Any]],
        ttl_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch
        self._ttl = ttl_s
        self._clock = clock
        self._cached: tuple[float, TariffSet] | None = None

    async def get(self) -> TariffSet:
        now = self._clock()
        if self._cached is not None and now - self._cached[0] < self._ttl:
            return self._cached[1]
        parsed = parse_public_tariffs(await self._fetch())
        self._cached = (now, parsed)
        return parsed

    def invalidate(self) -> None:
        self._cached = None
