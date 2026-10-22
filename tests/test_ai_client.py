"""The kie.ai chat client against recorded provider replies (fixture-replay): every soft failure is a FAILURE."""
from __future__ import annotations

import json

import httpx
import pytest

from ytgen_bot.ai.client import KieChatClient, ProviderError, looks_like_gateway_error

KEY = "kie-key-SECRET-0123456789"
MODEL = "gemini-3-8-flash-openai"


def ok_body(content: str = '{"reply": "ok"}', finish: str | None = "stop") -> dict:
    return {
        "id": "chatcmpl-1", "model": MODEL, "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        "credits_consumed": 0.9,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": finish}],
    }


def client_for(handler) -> KieChatClient:
    return KieChatClient(api_key=KEY, base_url="https://api.kie.example/", model=MODEL, timeout_s=5.0,
                         transport=httpx.MockTransport(handler))


async def call(client: KieChatClient):
    return await client.chat(system="SYS", user="USER", max_tokens=300, temperature=0.2)


async def test_request_shape_matches_the_verified_wire_contract():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=ok_body())

    out = await call(client_for(handler))
    assert out.text == '{"reply": "ok"}' and out.finish_reason == "stop"
    assert seen["url"] == f"https://api.kie.example/{MODEL}/v1/chat/completions"
    assert seen["auth"] == f"Bearer {KEY}"
    assert seen["body"] == {
        "model": MODEL, "stream": False, "temperature": 0.2, "max_tokens": 300,
        "messages": [{"role": "system", "content": "SYS"}, {"role": "user", "content": "USER"}],
    }


async def test_a_missing_finish_reason_is_accepted_but_any_other_one_is_a_failure():
    ok = client_for(lambda r: httpx.Response(200, json=ok_body(finish=None)))
    assert (await call(ok)).finish_reason is None
    assert (await call(client_for(lambda r: httpx.Response(200, json=ok_body(finish="STOP"))))).finish_reason == "stop"


@pytest.mark.parametrize("finish", ["length", "content_filter", "max_tokens"])
async def test_truncated_or_filtered_completions_are_failures(finish):
    with pytest.raises(ProviderError) as exc:
        await call(client_for(lambda r: httpx.Response(200, json=ok_body(finish=finish))))
    assert exc.value.kind == "truncated" and exc.value.retryable


async def test_http_200_with_an_error_envelope_is_a_failure_not_an_answer():
    body = {"code": 500, "msg": "Network error, please try again later", "data": None}
    with pytest.raises(ProviderError) as exc:
        await call(client_for(lambda r: httpx.Response(200, json=body)))
    assert exc.value.kind == "soft_error" and "500" in str(exc.value) and exc.value.retryable


async def test_http_200_with_422_envelope_is_a_non_retryable_failure():
    body = {"code": 422, "msg": "The model is not supported", "data": None}
    with pytest.raises(ProviderError) as exc:
        await call(client_for(lambda r: httpx.Response(200, json=body)))
    assert exc.value.kind == "soft_error" and not exc.value.retryable


async def test_http_200_without_choices_is_malformed():
    with pytest.raises(ProviderError) as exc:
        await call(client_for(lambda r: httpx.Response(200, json={"code": 200, "msg": "success", "data": {}})))
    assert exc.value.kind == "malformed"
    with pytest.raises(ProviderError):
        await call(client_for(lambda r: httpx.Response(200, json={"choices": []})))
    with pytest.raises(ProviderError):
        await call(client_for(lambda r: httpx.Response(200, text="<html>gateway</html>")))


@pytest.mark.parametrize("content", ["", "   ", None])
async def test_an_empty_completion_is_a_failure(content):
    body = ok_body()
    body["choices"][0]["message"]["content"] = content
    with pytest.raises(ProviderError):
        await call(client_for(lambda r: httpx.Response(200, json=body)))


@pytest.mark.parametrize(
    "content",
    [
        "Sorry, something went wrong. Please try your request again.",
        "I encountered an error doing what you asked. Could you try again?",
        "I'm having a hard time fulfilling your request. Can I help you with something else instead?",
        # a real answer cut off mid-generation with the canned sentence glued to the end (even fused onto the word)
        '{"reply": "Дубляж входит в любой тариф и на каждый язык выдаётся MP3, SRT и TXT, "' + "x" * 700
        + "I'm having a hard time fulfilling your request. Can I help you with something else instead?",
        '{"reply": "Начало ответа, который оборвался"}\nSorry, something went wrong. Please try your request again.',
    ],
)
async def test_gateway_error_sentences_are_failures(content):
    with pytest.raises(ProviderError) as exc:
        await call(client_for(lambda r: httpx.Response(200, json=ok_body(content=content))))
    assert exc.value.kind == "gateway_error_text"


def test_a_long_legitimate_reply_that_merely_mentions_a_phrase_is_not_a_gateway_error():
    legit = "Если что-то пойдёт не так, мы не скажем 'please try your request again'. " + "Это нормальный ответ. " * 60
    assert not looks_like_gateway_error(legit)
    assert not looks_like_gateway_error('{"reply": "Всё работает"}')


@pytest.mark.parametrize("status,retryable", [(429, True), (500, True), (502, True), (503, True), (524, True),
                                              (401, False), (402, False), (403, False), (422, False), (400, False)])
async def test_http_statuses_and_which_are_worth_a_retry(status, retryable):
    with pytest.raises(ProviderError) as exc:
        await call(client_for(lambda r: httpx.Response(status, json={"msg": "nope"})))
    assert exc.value.kind == f"http_{status}" and exc.value.retryable is retryable


async def test_network_and_timeout_errors_are_retryable_failures():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(ProviderError) as exc:
        await call(client_for(boom))
    assert exc.value.kind == "network" and exc.value.retryable

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    with pytest.raises(ProviderError) as exc:
        await call(client_for(slow))
    assert exc.value.kind == "timeout" and exc.value.retryable


async def test_the_api_key_is_scrubbed_from_every_error_message():
    body = {"code": 500, "msg": f"upstream said key {KEY} is bad", "data": None}
    with pytest.raises(ProviderError) as exc:
        await call(client_for(lambda r: httpx.Response(200, json=body)))
    assert KEY not in str(exc.value)
    with pytest.raises(ProviderError) as exc:
        await call(client_for(lambda r: httpx.Response(401, json={"msg": f"bad key {KEY}"})))
    assert KEY not in str(exc.value)


async def test_redirects_are_not_followed_so_the_key_cannot_travel():
    def redirect(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example/steal"})

    with pytest.raises(ProviderError):
        await call(client_for(redirect))
