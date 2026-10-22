"""The deep-link payload of `/start <payload>`."""
from __future__ import annotations

import dataclasses

from .callbacks import REF_CODE_RE


@dataclasses.dataclass(frozen=True)
class StartParam:
    kind: str  # "none" | "paid" | "ref" | "invalid"
    ref_code: str | None = None


def parse_start_param(raw: str | None) -> StartParam:
    value = (raw or "").strip()
    if not value:
        return StartParam("none")
    if value == "paid":
        return StartParam("paid")
    if value.startswith("ref_"):
        code = value[len("ref_"):]
        if REF_CODE_RE.fullmatch(code):
            return StartParam("ref", code)
    return StartParam("invalid")
