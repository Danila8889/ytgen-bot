"""The deep-link payload of `/start <payload>`."""
from __future__ import annotations

import dataclasses

from .callbacks import REF_CODE_RE, SOURCES


@dataclasses.dataclass(frozen=True)
class StartParam:
    kind: str  # "none" | "paid" | "ref" | "source" | "invalid"
    ref_code: str | None = None
    source: str | None = None  # yt | tg | site (kind == "source"); a ref_ payload carries no source


def parse_start_param(raw: str | None) -> StartParam:
    value = (raw or "").strip()
    if not value:
        return StartParam("none")
    if value == "paid":
        return StartParam("paid")
    if value in SOURCES:
        return StartParam("source", source=value)
    if value.startswith("ref_"):
        code = value[len("ref_"):]
        if REF_CODE_RE.fullmatch(code):
            return StartParam("ref", code)
    return StartParam("invalid")
