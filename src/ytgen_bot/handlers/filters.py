from __future__ import annotations

from aiogram.filters import Filter
from aiogram.types import CallbackQuery

from .. import callbacks


class IsAction(Filter):
    """Matches a callback whose data parses as one of the given action kinds; passes it as `action`."""

    def __init__(self, *kinds: str) -> None:
        self._kinds = kinds

    async def __call__(self, callback: CallbackQuery) -> bool | dict[str, callbacks.Action]:
        action = callbacks.parse(callback.data)
        if action is not None and action.kind in self._kinds:
            return {"action": action}
        return False
