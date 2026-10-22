"""The wrapper client: auth headers on every call, error shapes, the contract it relies on."""
from __future__ import annotations

import httpx
import pytest

from tests.fake_wrapper import SERVICE_KEY, FakeWrapper
from ytgen_bot.wrapper import (
    WrapperClient,
    WrapperContractError,
    WrapperError,
    WrapperUnavailable,
    parse_error,
)

UID = 777


def client(fake: FakeWrapper) -> WrapperClient:
    return WrapperClient("https://wrapper.example.test", SERVICE_KEY, transport=fake.transport())


async def test_every_route_carries_both_headers_and_uses_the_documented_path():
    fake = FakeWrapper()
    fake.link(UID)
    fake.consent(UID)
    pid = fake.new_payment()
    fake.latest[UID] = pid
    c = client(fake)
    await c.start(UID, language="ru", consent=True, referral_code="ABC")
    await c.opt_out(UID)
    await c.set_blocked(UID, True)
    await c.me(UID)
    fake.linked.pop(UID)
    await c.request_code(UID, "a@example.com")
    await c.verify_code(UID, "123456")
    await c.tariffs(UID)
    await c.quote(UID, 11, "SAVE10XX")
    await c.checkout(UID, 11, "https://t.me/ytgen_test_bot?start=paid", "SAVE10XX")
    await c.payment_status(UID, pid)
    await c.latest_payment(UID)
    await c.referral(UID)
    await c.request_withdrawal(UID, 300)
    seen = [(r["method"], r["path"]) for r in fake.requests]
    assert seen == [
        ("POST", "/start"), ("POST", "/opt-out"), ("POST", "/blocked"), ("GET", "/me"),
        ("POST", "/link/request-code"), ("POST", "/link/verify"), ("GET", "/tariffs"),
        ("POST", "/billing/quote"), ("POST", "/billing/checkout"), ("GET", f"/billing/payments/{pid}"),
        ("GET", "/billing/payments/latest"), ("GET", "/referral"), ("POST", "/referral/withdrawals"),
    ]
    for r in fake.requests:
        assert r["headers"]["x-bot-service-key"] == SERVICE_KEY
        assert r["headers"]["x-telegram-user-id"] == str(UID)
    await c.aclose()


async def test_request_bodies_match_the_contract():
    fake = FakeWrapper()
    fake.link(UID)
    c = client(fake)
    await c.start(UID, language="x" * 40, consent=False)
    await c.quote(UID, 11)
    await c.checkout(UID, 11, "https://t.me/b?start=paid")
    bodies = [r["json"] for r in fake.requests]
    assert bodies[0] == {"consent": False, "language": "x" * 16}  # the wrapper caps language at 16
    assert bodies[1] == {"tariff_id": 11}  # no promo key unless a promo was given
    assert bodies[2] == {"tariff_id": 11, "return_url": "https://t.me/b?start=paid"}
    await c.aclose()


async def test_wrong_key_is_a_401_error_with_its_code():
    fake = FakeWrapper()
    c = WrapperClient("https://wrapper.example.test", "wrong" * 10, transport=fake.transport())
    with pytest.raises(WrapperError) as exc:
        await c.me(UID)
    assert (exc.value.status, exc.value.code) == (401, "invalid_bot_service_key")
    await c.aclose()


def response(status: int, body, headers=None) -> httpx.Response:
    return httpx.Response(status, json=body, headers=headers)


def test_error_shape_with_code_and_extras():
    err = parse_error(response(400, {"detail": {"error": "code_invalid", "attempts_remaining": 2}}))
    assert (err.status, err.code, err.detail["attempts_remaining"]) == (400, "code_invalid", 2)


def test_429_retry_after_from_the_body_or_the_header():
    body = parse_error(response(429, {"detail": {"error": "rate_limited", "bucket": "bot_call", "retry_after_s": 12}}))
    assert body.retry_after_s == 12
    header = parse_error(response(429, {"detail": {"error": "rate_limited"}}, {"Retry-After": "30"}))
    assert header.retry_after_s == 30
    assert parse_error(response(429, {"detail": {"error": "rate_limited"}})).retry_after_s is None


def test_plain_string_detail_such_as_the_tariff_404():
    err = parse_error(response(404, {"detail": "Tariff not found."}))
    assert (err.status, err.code) == (404, "http_404") and err.detail["message"] == "Tariff not found."


def test_list_shaped_422_validation_error():
    err = parse_error(response(422, {"detail": [{"loc": ["body"], "msg": "bad", "type": "x"}]}))
    assert err.code == "validation_error"


def test_non_json_error_body():
    err = parse_error(httpx.Response(502, text="<html>Bad gateway</html>"))
    assert (err.status, err.code) == (502, "http_502")


@pytest.mark.parametrize("exc", [httpx.ConnectError("x"), httpx.ReadTimeout("x"), httpx.ConnectTimeout("x")])
async def test_network_failures_become_wrapper_unavailable(exc, caplog):
    fake = FakeWrapper()
    fake.queue("GET", "/me", exc)
    c = client(fake)
    with pytest.raises(WrapperUnavailable):
        await c.me(UID)
    assert any(r.message == "wrapper_unreachable" for r in caplog.records)
    await c.aclose()


async def test_a_2xx_with_the_wrong_shape_fails_loudly(caplog):
    fake = FakeWrapper()
    fake.queue("GET", "/me", response(200, {"telegram_user_id": UID}))
    c = client(fake)
    with pytest.raises(WrapperContractError):
        await c.me(UID)
    assert any(r.message == "wrapper_contract_mismatch" for r in caplog.records)
    await c.aclose()


async def test_non_json_2xx_and_wrong_tariff_payload():
    fake = FakeWrapper()
    fake.queue("GET", "/me", httpx.Response(200, text="ok"))
    fake.queue("GET", "/tariffs", response(200, {"not": "a list"}))
    fake.queue("GET", "/tariffs", response(200, [{"id": 1}]))
    c = client(fake)
    for call in (c.me(UID), c.tariffs(UID), c.tariffs(UID)):
        with pytest.raises(WrapperContractError):
            await call
    await c.aclose()


async def test_additive_fields_are_ignored():
    fake = FakeWrapper()
    fake.link(UID)
    fake.queue("GET", "/me", response(200, {
        "telegram_user_id": UID, "linked": True, "brand_new_field": 1,
        "tenant": {"email_masked": "j***@x.y", "tariff_id": None, "tariff_name": None,
                   "tariff_expires_at": None, "paid_access_active": False, "extra": True},
    }))
    c = client(fake)
    assert (await c.me(UID)).tenant.email_masked == "j***@x.y"
    await c.aclose()


async def test_the_service_key_is_never_in_logs(caplog):
    fake = FakeWrapper()
    fake.queue("GET", "/me", httpx.ConnectError("x"))
    c = client(fake)
    with pytest.raises(WrapperUnavailable):
        await c.me(UID)
    assert SERVICE_KEY not in caplog.text
    await c.aclose()


async def test_redirects_are_not_followed():
    fake = FakeWrapper()
    fake.queue("GET", "/me", httpx.Response(302, headers={"Location": "https://evil.example/"}))
    c = client(fake)
    with pytest.raises(WrapperError) as exc:
        await c.me(UID)
    assert exc.value.status == 302
    await c.aclose()


async def test_me_carries_the_bot_user_state_and_creates_nothing():
    fake = FakeWrapper()
    c = client(fake)
    never = await c.me(UID)
    assert (never.linked, never.consented, never.blocked) == (False, False, False)
    assert fake.users == {}  # read-only: the fake (like the wrapper) creates no row
    fake.consent(UID)
    assert (await c.me(UID)).consented is True
    fake.user(UID)["opted_out_at"] = "2026-10-22T11:00:00Z"
    assert (await c.me(UID)).consented is False  # opted out keeps consent_at but is not consent
    fake.user(UID).update(opted_out_at=None, blocked_at="2026-10-22T12:00:00Z")
    me = await c.me(UID)
    assert me.blocked is True and me.consented is True
    await c.aclose()


async def test_latest_payment_shape_and_404():
    fake = FakeWrapper()
    fake.link(UID)
    c = client(fake)
    with pytest.raises(WrapperError) as exc:
        await c.latest_payment(UID)
    assert (exc.value.status, exc.value.code) == (404, "payment_not_found")
    pid = fake.new_payment("succeeded", True)
    fake.latest[UID] = pid
    status = await c.latest_payment(UID)
    assert (status.payment_id, status.status, status.credited) == (pid, "succeeded", True)
    fake.linked.pop(UID)
    with pytest.raises(WrapperError) as exc:
        await c.latest_payment(UID)
    assert exc.value.code == "telegram_not_linked"
    await c.aclose()
