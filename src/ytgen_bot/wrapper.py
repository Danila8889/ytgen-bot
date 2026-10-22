"""HTTP client for the wrapper's /v1/bot API: the only thing the bot talks to besides Telegram.

Every call carries X-Bot-Service-Key and X-Telegram-User-Id. Non-2xx answers become a `WrapperError` carrying
the machine code from `{"detail": {"error": <code>, ...}}`. Two other shapes get generic codes: FastAPI's
list-shaped 422 body-validation `detail` (`validation_error`; the contract says bot routes keep it) and any
plain-string or non-JSON body (`http_<status>`; the bot routes no longer emit plain strings, the unknown-tariff
404 is `tariff_not_found`, so this is only a fallback). Nothing is retried here: a retry of a state-changing
call is the caller's decision, and the wrapper's own duplicate guard covers the checkout.
"""
from __future__ import annotations

import logging
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from . import schemas

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

API_PREFIX = "/v1/bot"
DEFAULT_TIMEOUT_S = 20.0


class WrapperError(Exception):
    """The wrapper answered with a non-2xx status."""

    def __init__(
        self,
        status: int,
        code: str,
        detail: dict[str, Any] | None = None,
        retry_after_s: int | None = None,
    ) -> None:
        super().__init__(f"{status} {code}")
        self.status = status
        self.code = code
        self.detail = detail or {}
        self.retry_after_s = retry_after_s


class WrapperUnavailable(Exception):
    """The wrapper could not be reached (network error, timeout)."""


class WrapperContractError(Exception):
    """A 2xx answer that does not have the documented shape."""


# Every way a wrapper call can fail; handlers catch exactly these and show a mapped message. Anything else is
# a bug and goes to the dispatcher's error handler (ERROR log + generic message).
WRAPPER_FAILURES = (WrapperError, WrapperUnavailable, WrapperContractError)


def _retry_after(response: httpx.Response, detail: dict[str, Any]) -> int | None:
    value: Any = detail.get("retry_after_s")
    if value is None:
        value = response.headers.get("Retry-After")
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def parse_error(response: httpx.Response) -> WrapperError:
    try:
        body = response.json()
    except ValueError:
        body = None
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict) and isinstance(detail.get("error"), str):
        return WrapperError(response.status_code, detail["error"], detail, _retry_after(response, detail))
    if isinstance(detail, list):
        return WrapperError(response.status_code, "validation_error", {})
    extra: dict[str, Any] = {"message": detail} if isinstance(detail, str) else {}
    return WrapperError(
        response.status_code, f"http_{response.status_code}", extra, _retry_after(response, {})
    )


class WrapperClient:
    def __init__(
        self,
        base_url: str,
        service_key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + API_PREFIX,
            headers={"X-Bot-Service-Key": service_key},
            timeout=timeout_s,
            transport=transport,
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _send(
        self, method: str, path: str, uid: int, json: dict[str, Any] | None = None
    ) -> Any:
        """One HTTP exchange; returns the decoded 2xx body, raises WrapperError / WrapperUnavailable."""
        try:
            response = await self._client.request(
                method, path, json=json, headers={"X-Telegram-User-Id": str(uid)}
            )
        except httpx.HTTPError as exc:
            log.error(
                "wrapper_unreachable",
                extra={"method": method, "path": path, "tg_user": uid, "error": type(exc).__name__},
            )
            raise WrapperUnavailable(type(exc).__name__) from exc
        if not response.is_success:
            error = parse_error(response)
            log.warning(
                "wrapper_error",
                extra={"method": method, "path": path, "status": error.status, "code": error.code, "tg_user": uid},
            )
            raise error
        try:
            return response.json()
        except ValueError as exc:
            log.error("wrapper_contract_mismatch", extra={"method": method, "path": path, "tg_user": uid})
            raise WrapperContractError(f"{method} {path}: response is not JSON") from exc

    async def _call(
        self,
        method: str,
        path: str,
        uid: int,
        model: type[T],
        *,
        json: dict[str, Any] | None = None,
    ) -> T:
        payload = await self._send(method, path, uid, json)
        try:
            return model.model_validate(payload)
        except ValidationError as exc:
            log.error("wrapper_contract_mismatch", extra={"method": method, "path": path, "tg_user": uid})
            raise WrapperContractError(f"{method} {path}: unexpected response shape") from exc

    # --- start / consent / opt-out / blocked -------------------------------------------------

    async def start(
        self,
        uid: int,
        *,
        language: str | None = None,
        consent: bool = False,
        referral_code: str | None = None,
    ) -> schemas.StartResponse:
        body: dict[str, Any] = {"consent": consent}
        if language:
            body["language"] = language[:16]
        if referral_code:
            body["referral_code"] = referral_code
        return await self._call("POST", "/start", uid, schemas.StartResponse, json=body)

    async def opt_out(self, uid: int) -> schemas.BotUserState:
        return await self._call("POST", "/opt-out", uid, schemas.BotUserState)

    async def set_blocked(self, uid: int, blocked: bool) -> schemas.BotUserState:
        return await self._call("POST", "/blocked", uid, schemas.BotUserState, json={"blocked": blocked})

    # --- account ------------------------------------------------------------------------------

    async def me(self, uid: int) -> schemas.MeResponse:
        return await self._call("GET", "/me", uid, schemas.MeResponse)

    async def request_code(self, uid: int, email: str) -> schemas.LinkRequestCodeResponse:
        return await self._call(
            "POST", "/link/request-code", uid, schemas.LinkRequestCodeResponse, json={"email": email}
        )

    async def verify_code(self, uid: int, code: str) -> schemas.LinkVerifyResponse:
        return await self._call("POST", "/link/verify", uid, schemas.LinkVerifyResponse, json={"code": code})

    # --- tariffs / billing --------------------------------------------------------------------

    async def tariffs(self, uid: int) -> list[schemas.Tariff]:
        payload = await self._send("GET", "/tariffs", uid)
        if not isinstance(payload, list):
            log.error("wrapper_contract_mismatch", extra={"method": "GET", "path": "/tariffs", "tg_user": uid})
            raise WrapperContractError("GET /tariffs: a list was expected")
        try:
            return [schemas.Tariff.model_validate(item) for item in payload]
        except ValidationError as exc:
            log.error("wrapper_contract_mismatch", extra={"method": "GET", "path": "/tariffs", "tg_user": uid})
            raise WrapperContractError("GET /tariffs: unexpected response shape") from exc

    async def quote(
        self, uid: int, tariff_id: int, promo_code: str | None = None
    ) -> schemas.QuoteResponse:
        body: dict[str, Any] = {"tariff_id": tariff_id}
        if promo_code:
            body["promo_code"] = promo_code
        return await self._call("POST", "/billing/quote", uid, schemas.QuoteResponse, json=body)

    async def checkout(
        self, uid: int, tariff_id: int, return_url: str, promo_code: str | None = None
    ) -> schemas.CheckoutResponse:
        body: dict[str, Any] = {"tariff_id": tariff_id, "return_url": return_url}
        if promo_code:
            body["promo_code"] = promo_code
        return await self._call("POST", "/billing/checkout", uid, schemas.CheckoutResponse, json=body)

    async def payment_status(self, uid: int, payment_id: str) -> schemas.PaymentStatus:
        return await self._call(
            "GET", f"/billing/payments/{payment_id}", uid, schemas.PaymentStatus
        )

    async def latest_payment(self, uid: int) -> schemas.PaymentStatus:
        """The caller's most recent bot checkout (what the `?start=paid` return refers to);
        404 `payment_not_found` when there is none."""
        return await self._call("GET", "/billing/payments/latest", uid, schemas.PaymentStatus)

    # --- referral -----------------------------------------------------------------------------

    async def referral(self, uid: int) -> schemas.ReferralInfo:
        return await self._call("GET", "/referral", uid, schemas.ReferralInfo)

    async def request_withdrawal(self, uid: int, amount_rub: int) -> schemas.WithdrawalResponse:
        return await self._call(
            "POST", "/referral/withdrawals", uid, schemas.WithdrawalResponse, json={"amount_rub": amount_rub}
        )
