"""A stateful in-memory stand-in for the wrapper's /v1/bot API, served through httpx.MockTransport.

Response shapes and error bodies are copied from the real code (wrapper README "Telegram bot API",
src/api/routers/bot.py, src/bot/*.py), including the two awkward ones: the tariff 404 whose `detail` is a
plain string, and FastAPI's list-shaped 422 for a body that fails validation.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict, deque
from typing import Any

import httpx

SERVICE_KEY = "k" * 40
NOW = "2026-10-22T10:00:00Z"
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")


def detail_error(status: int, code: str, headers: dict[str, str] | None = None, **extra: Any) -> httpx.Response:
    return httpx.Response(status, json={"detail": {"error": code, **extra}}, headers=headers)


def tariff(tid: int, name: str, price: int, **over: Any) -> dict[str, Any]:
    base = {
        "id": tid,
        "name": name,
        "price_rub": price,
        "daily_job_limit": 3,
        "daily_scene_limit": 40,
        "daily_image_limit": 30,
        "daily_stock_limit": 20,
        "period_days": 30,
        "max_ai_scenes_per_video": None,
        "capacity": {
            "ai_video_scenes_per_day": 40,
            "window_hours": 24,
            "ai_video_videos_per_window": 3,
            "ai_video_videos_per_48h": 3,
            "image_stock_videos_per_day": 5,
            "images_per_day": 30,
            "stock_per_day": 20,
            "video_max_minutes": None,
        },
    }
    base.update(over)
    return base


class FakeWrapper:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.users: dict[int, dict[str, Any]] = {}
        self.linked: dict[int, dict[str, Any]] = {}  # uid -> tenant
        self.pending_codes: dict[int, str] = {}
        self.queued: dict[tuple[str, str], deque[httpx.Response | Exception]] = defaultdict(deque)
        self.tariffs = [tariff(11, "Старт", 1234), tariff(12, "Профи", 5678, period_days=7)]
        self.payments: dict[str, dict[str, Any]] = {}
        self.payment_seq = 0
        # Successor mapping of the wrapper (src/billing/terms.py): {current tariff id: (successor id, E reached)}.
        self.successors: dict[int, tuple[int, bool]] = {}
        self.latest: dict[int, str] = {}  # uid -> payment id of the most recent bot checkout
        self.code = "123456"
        self.referral_info: dict[str, Any] = {
            "code": "ABCDEF123456",
            "start_parameter": "ref_ABCDEF123456",
            "deep_link": "https://t.me/ytgen_test_bot?start=ref_ABCDEF123456",
            "reward_percent": 13,
            "hold_days": 3,
            "min_withdrawal_rub": 250,
            "is_referred": False,
            "counts": {"opens": 4, "attributed": 2, "paid": 1},
            "balance": {"pending_rub": 111, "available_rub": 777, "requested_rub": 0, "paid_out_rub": 55, "owed_rub": 0},
            "open_withdrawal": None,
        }

    # --- test controls -----------------------------------------------------------------------

    def queue(self, method: str, path: str, response: httpx.Response | Exception) -> None:
        """The next call to (method, path) gets this answer instead of the normal one."""
        self.queued[(method, path)].append(response)

    def queue_error(self, method: str, path: str, status: int, code: str, **extra: Any) -> None:
        headers = {"Retry-After": str(extra["retry_after_s"])} if "retry_after_s" in extra else None
        self.queue(method, path, detail_error(status, code, headers, **extra))

    def calls(self, method: str | None = None, path: str | None = None) -> list[dict[str, Any]]:
        return [
            r for r in self.requests
            if (method is None or r["method"] == method) and (path is None or r["path"] == path)
        ]

    def link(self, uid: int, email: str = "john@example.com", **tenant: Any) -> None:
        self.linked[uid] = {
            "email": email, "tariff_id": None, "tariff_name": None, "tariff_expires_at": None, **tenant,
        }

    def consent(self, uid: int) -> None:
        self.user(uid)["consent_at"] = NOW

    def user(self, uid: int) -> dict[str, Any]:
        return self.users.setdefault(
            uid,
            {"first_start_at": NOW, "consent_at": None, "opted_out_at": None, "blocked_at": None, "language": None,
             "pending_referral": None},
        )

    def new_payment(self, status: str = "pending", credited: bool = False, reason: str | None = None) -> str:
        self.payment_seq += 1
        pid = f"2c0a7fa5-000f-5000-8000-{self.payment_seq:012d}"
        self.payments[pid] = {"status": status, "credited": credited, "reason": reason}
        return pid

    # --- the transport ------------------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v1/bot")
        body = json.loads(request.content) if request.content else None
        self.requests.append(
            {"method": request.method, "path": path, "headers": dict(request.headers), "json": body}
        )
        queued = self.queued.get((request.method, path))
        if queued:
            item = queued.popleft()
            if isinstance(item, Exception):
                raise item
            return item
        if request.headers.get("X-Bot-Service-Key") != SERVICE_KEY:
            return detail_error(401, "invalid_bot_service_key")
        raw_uid = request.headers.get("X-Telegram-User-Id", "")
        if not raw_uid.isdigit() or int(raw_uid) < 1:
            return detail_error(400, "invalid_telegram_user_id")
        uid = int(raw_uid)
        for pattern, name in _ROUTES:
            match = pattern.fullmatch(f"{request.method} {path}")
            if match:
                return getattr(self, name)(uid, body, *match.groups())
        return httpx.Response(404, json={"detail": "Not Found"})

    # --- routes -------------------------------------------------------------------------------

    def _state(self, uid: int) -> dict[str, Any]:
        u = self.user(uid)
        return {
            "telegram_user_id": uid, "linked": uid in self.linked, "first_start_at": u["first_start_at"],
            "consent_at": u["consent_at"], "opted_out_at": u["opted_out_at"], "blocked_at": u["blocked_at"],
            "language": u["language"],
        }

    def r_start(self, uid: int, body: dict[str, Any]) -> httpx.Response:
        u = self.user(uid)
        if body.get("language"):
            u["language"] = body["language"]
        u["blocked_at"] = None
        if body.get("consent"):
            if u["consent_at"] is None or u["opted_out_at"] is not None:
                u["consent_at"] = NOW
            u["opted_out_at"] = None
        out = self._state(uid)
        code = body.get("referral_code")
        out["referral"] = {"outcome": self._referral_outcome(uid, code)} if code else None
        return httpx.Response(200, json=out)

    def _referral_outcome(self, uid: int, code: str) -> str:
        if code == "SELFSELFSELF":
            return "rejected_self_referral"
        if code == "NOSUCHCODE":
            return "rejected_unknown_code"
        if uid in self.linked:
            return "attributed"
        self.user(uid)["pending_referral"] = code
        return "pending_stored"

    def r_opt_out(self, uid: int, body: Any) -> httpx.Response:
        u = self.user(uid)
        u["opted_out_at"] = u["opted_out_at"] or NOW
        return httpx.Response(200, json=self._state(uid))

    def r_blocked(self, uid: int, body: dict[str, Any]) -> httpx.Response:
        self.user(uid)["blocked_at"] = NOW if body["blocked"] else None
        return httpx.Response(200, json=self._state(uid))

    def r_me(self, uid: int, body: Any) -> httpx.Response:
        """Read-only: never creates a bot-user row (unlike POST /start)."""
        u = self.users.get(uid)
        state = {
            "consent_at": u["consent_at"] if u else None,
            "opted_out_at": u["opted_out_at"] if u else None,
            "blocked": bool(u and u["blocked_at"]),
            "blocked_at": u["blocked_at"] if u else None,
        }
        tenant = self.linked.get(uid)
        if tenant is None:
            return httpx.Response(200, json={"telegram_user_id": uid, "linked": False, "tenant": None, **state})
        local, _, domain = tenant["email"].partition("@")
        expires = tenant["tariff_expires_at"]
        active = bool(tenant["tariff_id"] and expires and expires > "2026-10-22T10:00:00Z")
        return httpx.Response(200, json={
            "telegram_user_id": uid, "linked": True,
            "tenant": {"email_masked": f"{local[:1]}***@{domain}", "tariff_id": tenant["tariff_id"],
                       "tariff_name": tenant["tariff_name"], "tariff_expires_at": expires,
                       "paid_access_active": active},
            **state,
        })

    def r_request_code(self, uid: int, body: dict[str, Any]) -> httpx.Response:
        if uid in self.linked:
            return detail_error(409, "telegram_already_linked")
        if not EMAIL_RE.fullmatch(body["email"].strip()):
            return detail_error(422, "invalid_email")
        self.pending_codes[uid] = self.code
        self.last_email = body["email"].strip()
        return httpx.Response(200, json={"status": "code_sent", "expires_in_s": 600})

    def r_verify(self, uid: int, body: dict[str, Any]) -> httpx.Response:
        if uid not in self.pending_codes:
            return detail_error(400, "no_pending_code")
        if body["code"] != self.pending_codes[uid]:
            return detail_error(400, "code_invalid", attempts_remaining=4)
        del self.pending_codes[uid]
        self.link(uid, self.last_email.lower())
        pending = self.user(uid)["pending_referral"]
        return httpx.Response(200, json={
            "linked": True, "already_linked": False, "tenant_created": True,
            "referral": {"outcome": "attributed"} if pending else None,
        })

    def r_tariffs(self, uid: int, body: Any) -> httpx.Response:
        return httpx.Response(200, json=self.tariffs)

    def _tariff(self, tid: int | None) -> dict[str, Any] | None:
        return next((t for t in self.tariffs if t["id"] == tid), None)

    def _chosen(self, uid: int, requested: dict[str, Any]) -> dict[str, Any]:
        """terms.choose_terms_tariff: with a successor mapping, a request for the current plan or its
        successor is sold on the successor once E is reached, on the current plan before."""
        current = self.linked[uid]["tariff_id"]
        mapping = self.successors.get(current)
        if mapping and requested["id"] in (current, mapping[0]):
            return self._tariff(mapping[0] if mapping[1] else current)  # type: ignore[return-value]
        return requested

    def r_quote(self, uid: int, body: dict[str, Any]) -> httpx.Response:
        if uid not in self.linked:
            return detail_error(403, "telegram_not_linked")
        wanted = body.get("tariff_id") or self.linked[uid]["tariff_id"]
        requested = self._tariff(wanted)
        if requested is None:
            return detail_error(404, "tariff_not_found")
        t = self._chosen(uid, requested)  # NOTE: the real quote does not apply the 409 rule (only checkout does)
        out: dict[str, Any] = {
            "tariff_id": t["id"], "plan_name": t["name"], "price_rub": t["price_rub"], "currency": "RUB",
            "period_days": t["period_days"], "period_start": NOW, "period_end": NOW, "limits": {},
            "terms_change_at": None, "promo": None,
        }
        code = body.get("promo_code")
        if code:
            if code != "SAVE10XX":
                return detail_error(400, "promo_invalid")
            discount = t["price_rub"] * 10 // 100
            out["promo"] = {"code": code, "percent": 10, "list_price_rub": t["price_rub"],
                            "discount_rub": discount, "price_rub": t["price_rub"] - discount}
        return httpx.Response(200, json=out)

    def r_checkout(self, uid: int, body: dict[str, Any]) -> httpx.Response:
        if uid not in self.linked:
            return detail_error(403, "telegram_not_linked")
        requested = self._tariff(body["tariff_id"])
        if requested is None:
            return detail_error(404, "tariff_not_found")
        tenant = self.linked[uid]
        current, expires = tenant["tariff_id"], tenant["tariff_expires_at"]
        mapping = self.successors.get(current)
        if (
            current is not None and current != requested["id"] and expires and expires > NOW
            and not (mapping and requested["id"] == mapping[0])
        ):  # billing.decide_subscribe: 409 before anything is created
            return detail_error(409, "active_tariff_mismatch", active_tariff_id=current, tariff_expires_at=expires)
        t = self._chosen(uid, requested)
        amount = t["price_rub"]
        if body.get("promo_code") == "SAVE10XX":
            amount -= amount * 10 // 100
        pid = self.new_payment()
        self.latest[uid] = pid
        return httpx.Response(200, json={
            "payment_id": pid, "payment_url": f"https://pay.example.test/checkout/{pid}",
            "amount_rub": amount, "tariff_id": t["id"], "reused": False,
        })

    def r_latest_payment(self, uid: int, body: Any) -> httpx.Response:
        if uid not in self.linked:
            return detail_error(403, "telegram_not_linked")
        pid = self.latest.get(uid)
        if pid is None:
            return detail_error(404, "payment_not_found")
        return self.r_payment(uid, None, pid)

    def r_payment(self, uid: int, body: Any, payment_id: str) -> httpx.Response:
        if uid not in self.linked:
            return detail_error(403, "telegram_not_linked")
        p = self.payments.get(payment_id)
        if p is None:
            return detail_error(404, "payment_not_found")
        return httpx.Response(200, json={
            "payment_id": payment_id, "status": p["status"], "credited": p["credited"],
            "cancellation_reason": p["reason"],
        })

    def r_referral(self, uid: int, body: Any) -> httpx.Response:
        if uid not in self.linked:
            return detail_error(403, "telegram_not_linked")
        return httpx.Response(200, json=self.referral_info)

    def r_withdraw(self, uid: int, body: dict[str, Any]) -> httpx.Response:
        if uid not in self.linked:
            return detail_error(403, "telegram_not_linked")
        info = self.referral_info
        amount = body["amount_rub"]
        if amount < info["min_withdrawal_rub"]:
            return detail_error(422, "withdrawal_below_minimum", minimum_rub=info["min_withdrawal_rub"])
        if amount > info["balance"]["available_rub"]:
            return detail_error(409, "insufficient_balance", available_rub=info["balance"]["available_rub"])
        if info["open_withdrawal"]:
            return detail_error(409, "withdrawal_already_open")
        info["open_withdrawal"] = {"id": 9, "amount_rub": amount, "created_at": NOW}
        bal = dict(info["balance"], available_rub=info["balance"]["available_rub"] - amount, requested_rub=amount)
        return httpx.Response(201, json={"id": 9, "amount_rub": amount, "status": "requested",
                                         "created_at": NOW, "balance": bal})


_ROUTES = [
    (re.compile(p), n)
    for p, n in [
        (r"POST /start", "r_start"),
        (r"POST /opt-out", "r_opt_out"),
        (r"POST /blocked", "r_blocked"),
        (r"GET /me", "r_me"),
        (r"POST /link/request-code", "r_request_code"),
        (r"POST /link/verify", "r_verify"),
        (r"GET /tariffs", "r_tariffs"),
        (r"POST /billing/quote", "r_quote"),
        (r"POST /billing/checkout", "r_checkout"),
        (r"GET /billing/payments/latest", "r_latest_payment"),  # before the {id} route, as in the wrapper
        (r"GET /billing/payments/([^/]+)", "r_payment"),
        (r"GET /referral", "r_referral"),
        (r"POST /referral/withdrawals", "r_withdraw"),
    ]
]
