"""Payment result: bounded polling with backoff, success / cancelled / delayed messages, the check button."""
from __future__ import annotations

import asyncio
import itertools

from tests.conftest import UID, Harness
from ytgen_bot import callbacks, texts
from ytgen_bot.payments import MAX_CONSECUTIVE_FAILURES, WATCH_TOTAL_S, backoff_delays


def test_backoff_is_finite_capped_and_bounded():
    delays = list(backoff_delays())
    assert delays[0] == 3.0
    assert all(b >= a for a, b in itertools.pairwise(delays[:-1]))  # growing; only the last step is trimmed
    assert max(delays) <= 30.0
    assert abs(sum(delays) - WATCH_TOTAL_S) < 1e-6
    assert len(delays) < 40


async def drain(h: Harness) -> None:
    await asyncio.gather(*list(h.ctx.watchers._tasks.values()), return_exceptions=True)


async def start_payment(h: Harness) -> str:
    await h.press(callbacks.pay(11, 1234))
    return next(iter(h.fake.payments))


async def test_success_is_reported_with_tariff_and_expiry(linked: Harness):
    h = linked
    pid = await start_payment(h)
    h.fake.payments[pid].update(status="succeeded", credited=True)
    h.fake.linked[UID].update(tariff_id=11, tariff_name="Старт", tariff_expires_at="2026-11-21T10:00:00Z")
    await drain(h)
    assert "Оплата прошла" in h.last and "Старт" in h.last and "21.11.2026" in h.last


async def test_cancelled_is_reported_with_a_reason(linked: Harness):
    h = linked
    pid = await start_payment(h)
    h.fake.payments[pid].update(status="canceled", reason="insufficient_funds")
    await drain(h)
    assert "Платёж не прошёл" in h.last and "Недостаточно средств" in h.last


async def test_cancelled_with_unknown_reason_is_still_clear(linked: Harness):
    h = linked
    pid = await start_payment(h)
    h.fake.payments[pid].update(status="canceled", reason="some_new_reason")
    await drain(h)
    assert "Платёж не прошёл" in h.last and "some_new_reason" not in h.last


async def test_polling_is_bounded_and_ends_with_a_check_button(linked: Harness):
    h = linked
    await start_payment(h)
    await drain(h)  # stays pending for all three (test-sized) polls
    assert len(h.fake.calls("GET", next(c["path"] for c in h.fake.requests if c["path"].startswith("/billing/payments/")))) == 3
    assert h.last == texts.payment_pending_timeout()
    assert h.session.last_markup().inline_keyboard[0][0].callback_data.startswith("chk:")


async def test_paid_but_not_credited_yet_ends_with_a_specific_message(linked: Harness):
    h = linked
    pid = await start_payment(h)
    h.fake.payments[pid].update(status="succeeded", credited=False)
    await drain(h)
    assert h.last == texts.payment_credit_delayed()


async def test_poller_gives_up_after_consecutive_failures(linked: Harness, caplog):
    import httpx

    h = linked
    h.ctx.watchers._delays = lambda: iter([1.0] * 20)
    await start_payment(h)
    pid = next(iter(h.fake.payments))
    for _ in range(MAX_CONSECUTIVE_FAILURES):
        h.fake.queue("GET", f"/billing/payments/{pid}", httpx.ConnectError("down"))
    await drain(h)
    assert "Не удалось проверить платёж" in h.last
    assert any(r.message == "payment_watch_gave_up" for r in caplog.records)


async def test_poller_stops_on_payment_not_found(linked: Harness):
    h = linked
    await start_payment(h)
    pid = next(iter(h.fake.payments))
    h.fake.queue_error("GET", f"/billing/payments/{pid}", 404, "payment_not_found")
    await drain(h)
    assert "Платёж не найден" in h.last


async def test_poller_survives_a_rate_limit(linked: Harness):
    h = linked
    h.ctx.watchers._delays = lambda: iter([1.0] * 5)
    await start_payment(h)
    pid = next(iter(h.fake.payments))
    h.fake.queue_error("GET", f"/billing/payments/{pid}", 429, "rate_limited", retry_after_s=2)
    h.fake.payments[pid].update(status="succeeded", credited=True)
    h.fake.linked[UID].update(tariff_id=11, tariff_name="Старт", tariff_expires_at="2026-11-21T10:00:00Z")
    await drain(h)
    assert "Оплата прошла" in h.last


async def test_poller_does_not_message_an_opted_out_user(linked: Harness):
    h = linked
    pid = await start_payment(h)
    await h.say("/stop")
    count = len(h.texts)
    h.fake.payments[pid].update(status="succeeded", credited=True)
    await drain(h)
    assert len(h.texts) == count


async def test_one_watcher_per_payment(linked: Harness):
    h = linked
    await start_payment(h)
    pid = next(iter(h.fake.payments))
    assert h.ctx.watchers.watch(UID, pid) is False


# --- the persistent "Проверить оплату" button -------------------------------------------------------


async def test_check_button_pending(linked: Harness):
    h = linked
    pid = h.fake.new_payment()
    await h.press(callbacks.check(pid))
    assert texts.payment_still_pending() in [a.text for a in h.session.answers()]
    assert h.ctx.watchers.active(pid)  # a fresh watch after a restart


async def test_check_button_success_after_restart(linked: Harness):
    """The payment id is in the callback data: no memory of the checkout is needed."""
    h = linked
    pid = h.fake.new_payment("succeeded", True)
    h.fake.linked[UID].update(tariff_id=12, tariff_name="Профи", tariff_expires_at="2026-11-01T00:00:00Z")
    await h.press(callbacks.check(pid))
    assert "Оплата прошла" in h.last and "Профи" in h.last


async def test_check_button_cancelled(linked: Harness):
    h = linked
    pid = h.fake.new_payment("canceled", False, "expired_on_confirmation")
    await h.press(callbacks.check(pid))
    assert "Время на оплату истекло" in h.last


async def test_check_button_paid_not_yet_credited(linked: Harness):
    h = linked
    pid = h.fake.new_payment("succeeded", False)
    await h.press(callbacks.check(pid))
    assert h.last == texts.payment_credit_delayed()


async def test_check_button_unknown_payment(linked: Harness):
    await linked.press(callbacks.check("does-not-exist"))
    assert "Платёж не найден" in linked.last


async def test_check_button_double_tap_is_debounced(linked: Harness):
    h = linked
    h.ctx.watchers = None  # only the button's own calls count here
    pid = h.fake.new_payment()
    await asyncio.gather(h.press(callbacks.check(pid)), h.press(callbacks.check(pid)))
    assert len(h.fake.calls("GET", f"/billing/payments/{pid}")) == 1
    assert texts.ALREADY_PROCESSING in [a.text for a in h.session.answers()]


async def test_check_button_for_an_unlinked_user_offers_linking(consented: Harness):
    await consented.press(callbacks.check("abc"))
    assert "привяжите аккаунт" in consented.last
