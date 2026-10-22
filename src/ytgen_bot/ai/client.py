"""kie.ai chat-completions client (re-implemented here; core's `src/utils/kie_chat.py` was read for the protocol only).

Wire contract (verified in core on production, 2026-10-08):
  POST {KIE_API_URL}/{model}/v1/chat/completions, `Authorization: Bearer <key>`,
  body {"model", "messages", "stream": false, "max_tokens", "temperature"}.
  Success: HTTP 200 with `choices[0].message.content` and `finish_reason`.
  kie reports most errors as **HTTP 200** with {"code": <int>, "msg": ..., "data": null} and NO `choices`
  ("Network error, please try again later" is code 500). The upstream gateway can also answer 200 with a canned
  error sentence as the "completion", or cut a real answer off and glue that sentence on.

EVERY one of these is a FAILURE (`ProviderError`), never an answer:
  http status != 2xx, 200 with code >= 300 / no choices / malformed shape, an empty completion, `finish_reason`
  other than "stop" (e.g. "length": truncated), a canned gateway error sentence (whole reply or glued to the end),
  a network error, a timeout. The key travels only in the Authorization header and is scrubbed from every message.
"""
from __future__ import annotations

import dataclasses
import re
from typing import Any

import httpx

MAX_ERROR_CHARS = 200

# Lower-cased phrases of the gateway's canned error sentences (observed by core on production 2026-10-04..09).
_GATEWAY_PHRASES = (
    "sorry, something went wrong",
    "i encountered an error doing what you asked",
    "please try your request again",
    "i seem to be encountering an error",
    "hard time fulfilling your request",
)
_WHOLE_REPLY_MAX = 600  # a canned sentence is short; a long legitimate reply that merely contains a phrase is not one
_TAIL_RE = re.compile(
    r"(?:sorry, something went wrong[.!]?\s*)?(?:please try your request again[.!]?|"
    r"i encountered an error doing what you asked[.!]?(?:\s+could you try again\??)?|"
    r"i seem to be encountering an error[.!]?(?:\s+can i try something else for you\??)?|"
    r"i'?m having a hard time fulfilling your request[.!]?(?:\s+can i help you with something else(?: instead)?\??)?)\s*$",
    re.IGNORECASE,
)


class ProviderError(Exception):
    """The LLM call failed. `kind` is a short machine label; `retryable` says whether one more try is reasonable."""

    def __init__(self, kind: str, detail: str = "", *, retryable: bool = False) -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.retryable = retryable


@dataclasses.dataclass(frozen=True)
class Completion:
    text: str
    finish_reason: str | None


def looks_like_gateway_error(text: str) -> bool:
    folded = text.replace("’", "'").replace("‘", "'").strip().lower()
    if not folded:
        return False
    if len(folded) < _WHOLE_REPLY_MAX and any(p in folded for p in _GATEWAY_PHRASES):
        return True
    # A real answer cut off mid-generation with the canned sentence appended (even fused onto the last word).
    stripped = folded.rstrip(" \t\r\n\"'`")
    return _TAIL_RE.search(stripped[-400:]) is not None


def _scrub(text: str, key: str) -> str:
    if key:
        text = text.replace(key, "***")
    text = text.replace("\n", " ")
    return text if len(text) <= MAX_ERROR_CHARS else text[:MAX_ERROR_CHARS] + "..."


class KieChatClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout_s: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._key = api_key
        self._model = model
        self._url = f"{base_url.rstrip('/')}/{model}/v1/chat/completions"
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s, connect=min(10.0, timeout_s)), transport=transport,
            follow_redirects=False,
        )

    @property
    def model(self) -> str:
        return self._model

    async def aclose(self) -> None:
        await self._client.aclose()

    async def chat(self, *, system: str, user: str, max_tokens: int, temperature: float) -> Completion:
        body: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": False,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        try:
            response = await self._client.post(self._url, json=body, headers={"Authorization": f"Bearer {self._key}"})
        except httpx.TimeoutException as exc:
            raise ProviderError("timeout", type(exc).__name__, retryable=True) from None
        except httpx.HTTPError as exc:
            raise ProviderError("network", type(exc).__name__, retryable=True) from None
        try:
            payload: Any = response.json()
        except ValueError:
            payload = None

        if not 200 <= response.status_code < 300:
            raise self._http_error(response.status_code, payload)

        choices = payload.get("choices") if isinstance(payload, dict) else None
        if not isinstance(choices, list) or not choices:
            # HTTP 200 whose body is an error envelope ({"code": 500, "msg": "Network error ..."}) or malformed.
            code = payload.get("code") if isinstance(payload, dict) else None
            msg = _scrub(str(payload.get("msg", "")), self._key) if isinstance(payload, dict) else ""
            if isinstance(code, int) and not isinstance(code, bool) and code >= 300:
                raise ProviderError("soft_error", f"code={code} {msg}".strip(), retryable=code >= 429 or code == 408)
            raise ProviderError("malformed", "no choices in a 200 reply", retryable=True)
        choice = choices[0]
        message = choice.get("message") if isinstance(choice, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        finish = choice.get("finish_reason") if isinstance(choice, dict) else None
        if not isinstance(content, str):
            raise ProviderError("malformed", "no message content", retryable=True)
        if finish is not None and str(finish).lower() != "stop":
            raise ProviderError("truncated", f"finish_reason={_scrub(str(finish), self._key)}", retryable=True)
        if not content.strip():
            raise ProviderError("empty", "empty completion", retryable=True)
        if looks_like_gateway_error(content):
            raise ProviderError("gateway_error_text", "a gateway error sentence came back as the completion",
                                retryable=True)
        return Completion(text=content, finish_reason=str(finish).lower() if finish is not None else None)

    def _http_error(self, status: int, payload: Any) -> ProviderError:
        msg = ""
        if isinstance(payload, dict):
            msg = _scrub(str(payload.get("msg") or payload.get("message") or ""), self._key)
        # 429 and 5xx are worth one retry; 401/402/403/422 and the rest are configuration/account problems.
        return ProviderError(f"http_{status}", msg, retryable=status == 429 or status >= 500)
