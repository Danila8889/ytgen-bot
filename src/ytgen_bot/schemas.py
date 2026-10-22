"""Response shapes of the wrapper's /v1/bot API (wrapper README "Telegram bot API", src/api/routers/bot.py).

Unknown fields are ignored (the wrapper adds fields additively); a missing or mistyped field fails loudly
at parse time, so a contract drift never turns into a half-filled screen.
"""
from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, ConfigDict


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ReferralOutcome(_Model):
    outcome: str


class BotUserState(_Model):
    telegram_user_id: int
    linked: bool
    first_start_at: dt.datetime
    consent_at: dt.datetime | None = None
    opted_out_at: dt.datetime | None = None
    blocked_at: dt.datetime | None = None
    language: str | None = None

    @property
    def consented(self) -> bool:
        """Consent given and not withdrawn (an opt-out keeps `consent_at`; only a new consent clears it)."""
        return self.consent_at is not None and self.opted_out_at is None


class StartResponse(BotUserState):
    referral: ReferralOutcome | None = None


class MeTenant(_Model):
    email_masked: str
    tariff_id: int | None = None
    tariff_name: str | None = None
    tariff_expires_at: dt.datetime | None = None
    paid_access_active: bool


class MeResponse(_Model):
    telegram_user_id: int
    linked: bool
    tenant: MeTenant | None = None


class LinkRequestCodeResponse(_Model):
    status: str
    expires_in_s: int


class LinkVerifyResponse(_Model):
    linked: bool
    already_linked: bool
    tenant_created: bool
    referral: ReferralOutcome | None = None


class TariffCapacity(_Model):
    window_hours: int | None = None
    ai_video_videos_per_window: int | None = None
    image_stock_videos_per_day: int | None = None
    image_stock_videos_per_window: int | None = None


class Tariff(_Model):
    id: int
    name: str
    price_rub: int
    period_days: int
    max_ai_scenes_per_video: int | None = None
    capacity: TariffCapacity | None = None


class PromoQuote(_Model):
    code: str
    percent: int
    list_price_rub: int
    discount_rub: int
    price_rub: int


class QuoteResponse(_Model):
    tariff_id: int
    plan_name: str
    price_rub: int  # the LIST price; the amount to charge is `payable_rub`
    period_days: int
    promo: PromoQuote | None = None

    @property
    def payable_rub(self) -> int:
        return self.promo.price_rub if self.promo is not None else self.price_rub


class CheckoutResponse(_Model):
    payment_id: str
    payment_url: str
    amount_rub: int | None = None
    tariff_id: int
    reused: bool


class PaymentStatus(_Model):
    payment_id: str
    status: str  # pending | succeeded | canceled
    credited: bool
    cancellation_reason: str | None = None


class ReferralCounts(_Model):
    opens: int
    attributed: int
    paid: int


class ReferralBalance(_Model):
    pending_rub: int
    available_rub: int
    requested_rub: int
    paid_out_rub: int
    owed_rub: int


class OpenWithdrawal(_Model):
    id: int
    amount_rub: int
    created_at: dt.datetime


class ReferralInfo(_Model):
    code: str
    start_parameter: str
    deep_link: str | None = None
    reward_percent: int
    hold_days: int
    min_withdrawal_rub: int
    is_referred: bool
    counts: ReferralCounts
    balance: ReferralBalance
    open_withdrawal: OpenWithdrawal | None = None


class WithdrawalResponse(_Model):
    id: int
    amount_rub: int
    status: str
    created_at: dt.datetime
    balance: ReferralBalance
