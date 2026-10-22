"""Consent state of a Telegram user, as far as this process knows it.

The wrapper is the system of record (`bot_users.consent_at` / `opted_out_at`). The bot keeps only a bounded
in-memory hint so it does not ask the wrapper on every message:

  True   consent given and not withdrawn;
  False  known NOT consented (opted out via /stop, or the wrapper said so);
  absent unknown (fresh process): resolved with one `POST /start` WITHOUT `consent` (the wrapper documents
         that as "changes nothing" and returns the stored state), which is how a button pressed before a
         restart keeps working without asking for consent again.
"""
from __future__ import annotations

import logging
from collections import OrderedDict

from .wrapper import WrapperClient

log = logging.getLogger(__name__)

_MAX_ENTRIES = 50_000


class ConsentCache:
    def __init__(self, max_entries: int = _MAX_ENTRIES) -> None:
        self._data: OrderedDict[int, bool] = OrderedDict()
        self._max = max_entries

    def get(self, uid: int) -> bool | None:
        value = self._data.get(uid)
        if value is not None:
            self._data.move_to_end(uid)
        return value

    def set(self, uid: int, consented: bool) -> None:
        self._data[uid] = consented
        self._data.move_to_end(uid)
        while len(self._data) > self._max:
            self._data.popitem(last=False)


async def has_consent(
    wrapper: WrapperClient, cache: ConsentCache, uid: int, language: str | None
) -> bool:
    """Raises WrapperError / WrapperUnavailable / WrapperContractError when the state cannot be read."""
    cached = cache.get(uid)
    if cached is not None:
        return cached
    state = await wrapper.start(uid, language=language)
    cache.set(uid, state.consented)
    log.info("consent_resolved", extra={"tg_user": uid, "consented": state.consented})
    return state.consented
