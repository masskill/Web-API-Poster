"""Assistant: tool loop with a mocked OpenAI, actions only after confirmation."""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import assistant, settings_store, videogen
from app.main import app
from app.models import AssistantAction


def openai_mock(replies, seen):
    """Return the given assistant messages one by one."""
    queue = list(replies)

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": queue.pop(0)}]})
    return httpx.MockTransport(handler)


def tool_call(name, args, call_id="c1"):
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": call_id, "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)}}]}


@pytest.fixture(autouse=True)
def key():
    settings_store.set_value("openai_api_key", "sk-test-assistant")


def test_without_key_adds_hint():
    settings_store.set_value("openai_api_key", "")
    asyncio.run(assistant.reply())
    assert assistant.all_messages()[-1].content == "no_key"


def test_read_tool_runs_and_answer_is_saved():
    videogen.create_generation("beach sunset")
    seen = []
    transport = openai_mock([tool_call("list_generations", {}),
                             {"role": "assistant", "content": "Генерація #1 чекає в черзі."}], seen)
    assistant.add_message("user", "Що з генераціями?")
    asyncio.run(assistant.reply(transport))
    roles = [m.role for m in assistant.all_messages()]
    assert roles == ["user", "assistant", "tool", "assistant"]
    tool_result = json.loads(assistant.all_messages()[2].content)
    assert tool_result["generations"][0]["prompt"] == "beach sunset"
    assert seen[0]["model"] == "gpt-5.4-mini" and "max_completion_tokens" in seen[0]
    assert seen[1]["messages"][-1]["role"] == "tool"  # the result went back to the model


def test_action_waits_for_confirmation_then_runs():
    seen = []
    transport = openai_mock([tool_call("start_generation", {"prompt": "neon city, seamless loop"}),
                             {"role": "assistant", "content": "Натисніть «Підтвердити»."}], seen)
    assistant.add_message("user", "Згенеруй нічне місто")
    asyncio.run(assistant.reply(transport))
    action_msg = [m for m in assistant.all_messages() if m.action_id][0]
    assert videogen.get_generation(1) is None  # nothing started yet
    assistant.resolve_action(action_msg.action_id, confirm=True)
    gen = videogen.get_generation(1)
    assert gen.prompt == "neon city, seamless loop" and gen.status == "queued"
    assert assistant.actions_by_id()[action_msg.action_id].status == "done"
    assistant.resolve_action(action_msg.action_id, confirm=True)  # second click does nothing
    assert videogen.get_generation(2) is None


def test_cancelled_action_does_nothing():
    action = assistant.propose("start_generation", {"prompt": "x"})
    assistant.resolve_action(action.id, confirm=False)
    assert videogen.get_generation(1) is None
    assert assistant.actions_by_id()[action.id].status == "cancelled"


def test_create_post_action_needs_finished_video():
    action = assistant.propose("create_post", {"generation_id": 99, "title": "T", "account_ids": [1]})
    assistant.resolve_action(action.id, confirm=True)
    a = assistant.actions_by_id()[action.id]
    assert a.status == "failed" and "finished" in a.result


def test_openai_error_is_shown_without_key():
    def handler(request):
        return httpx.Response(401, json={"error": {"message": "bad key sk-test-assistant"}})
    assistant.add_message("user", "hi")
    asyncio.run(assistant.reply(httpx.MockTransport(handler)))
    last = assistant.all_messages()[-1]
    assert last.role == "event" and "401" in last.content and "sk-test-assistant" not in last.content


def test_history_never_starts_with_tool_result():
    assistant.add_message("assistant", "", tool_calls=json.dumps([{"id": "a", "type": "function",
                                                                   "function": {"name": "x", "arguments": "{}"}}]))
    assistant.add_message("tool", "{}", tool_call_id="a")
    assistant.add_message("user", "next")
    msgs = assistant.api_messages(limit=2)
    assert msgs[0]["role"] == "user"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("app.db.init_db", lambda url=None: None)
    with TestClient(app) as c:
        yield c


def test_chat_page_and_action_card(client):
    assistant.add_message("user", "Привіт")
    assistant.add_message("assistant", "Ось промт:\n```\nsunset beach, seamless loop\n```")
    action = assistant.propose("start_generation", {"prompt": "sunset beach"})
    assistant.add_message("tool", "{}", tool_call_id="c1", action_id=action.id)
    page = client.get("/assistant").text
    assert '<pre class="prompt">sunset beach, seamless loop</pre>' in page
    assert "Запустити генерацію відео?" in page and f"/assistant/actions/{action.id}/confirm" in page
    client.post(f"/assistant/actions/{action.id}/cancel")
    assert assistant.actions_by_id()[action.id].status == "cancelled"
    client.cookies.set("lang", "ru")
    assert "AI-ассистент" in client.get("/assistant").text
