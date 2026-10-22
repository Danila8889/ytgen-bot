"""Process-wide collaborators handed to every handler (aiogram workflow data key `ctx`)."""
from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from .config import Settings
from .consent import ConsentCache
from .ratelimit import ActionGuard, TokenBucket
from .wrapper import WrapperClient

if TYPE_CHECKING:  # pragma: no cover
    from .payments import PaymentWatchers


@dataclasses.dataclass
class AppContext:
    settings: Settings
    wrapper: WrapperClient
    bot_username: str
    consent: ConsentCache = dataclasses.field(default_factory=ConsentCache)
    limiter: TokenBucket = dataclasses.field(default_factory=TokenBucket)
    guard: ActionGuard = dataclasses.field(default_factory=ActionGuard)
    watchers: "PaymentWatchers | None" = None

    @property
    def support(self) -> str:
        return self.settings.support_contact

    @property
    def return_url(self) -> str:
        """Where YooKassa sends the payer back: the bot's deep link with `start=paid`.

        The wrapper allowlists it against its own BOT_DEEP_LINK_BASE (`https://t.me/<bot username>`), so
        the username used here (from getMe) must be the one configured there.
        """
        return f"https://t.me/{self.bot_username}?start=paid"
