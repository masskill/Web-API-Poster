"""Video generator: workflow filling, ComfyUI client, generation lifecycle."""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config, settings_store, videogen
from app.main import app

WORKFLOW = {
    "3": {"class_type": "KSampler", "inputs": {"seed": 1, "steps": 20}},
    "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "{{PROMPT}}, cinematic"}},
    "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "{{NEGATIVE}}"}},
    "9": {"class_type": "SaveVideo", "inputs": {"filename_prefix": "video"}},
}


def test_fill_workflow_placeholders_and_seed():
    wf = videogen.fill_workflow(WORKFLOW, "a beach", "blurry", seed=42)
    assert wf["6"]["inputs"]["text"] == "a beach, cinematic"
    assert wf["7"]["inputs"]["text"] == "blurry"
    assert wf["3"]["inputs"]["seed"] == 42
    assert WORKFLOW["6"]["inputs"]["text"] == "{{PROMPT}}, cinematic"  # original untouched


def test_fill_workflow_without_placeholder_uses_first_text_node():
    wf = {"10": {"class_type": "CLIPTextEncode", "inputs": {"text": "old"}},
          "4": {"class_type": "MiniMaxTextEncode", "inputs": {"text": "x"}}}
    assert videogen.fill_workflow(wf, "new")["4"]["inputs"]["text"] == "new"


def test_fill_workflow_errors():
    with pytest.raises(videogen.ComfyError, match="no_prompt_field"):
        videogen.fill_workflow({"1": {"class_type": "Foo", "inputs": {}}}, "p")
    with pytest.raises(videogen.ComfyError, match="needs_image"):
        videogen.fill_workflow({"1": {"class_type": "LoadImage", "inputs": {"image": "{{IMAGE}}"}}, **WORKFLOW}, "p")


def test_parse_workflow_rejects_ui_format():
    assert videogen.parse_workflow(json.dumps(WORKFLOW)) == WORKFLOW
    with pytest.raises(ValueError):
        videogen.parse_workflow(json.dumps({"nodes": [], "links": []}))


class FakeComfy:
    """A tiny in-memory ComfyUI: queues a prompt, then finishes it with a video (or an error)."""

    def __init__(self, fail=False):
        self.fail = fail
        self.prompts = {}
        self.checks = 0

    def __call__(self, request: httpx.Request):
        path = request.url.path
        if path == "/upload/image":
            return httpx.Response(200, json={"name": "frame.png", "subfolder": "", "type": "input"})
        if path == "/prompt":
            body = json.loads(request.content)
            self.prompts["p1"] = body["prompt"]
            return httpx.Response(200, json={"prompt_id": "p1", "number": 1, "node_errors": {}})
        if path.startswith("/history/"):
            self.checks += 1
            if self.checks < 2:
                return httpx.Response(200, json={})
            if self.fail:
                status = {"status_str": "error", "completed": False,
                          "messages": [["execution_error", {"node_type": "MiniMaxSampler",
                                                            "exception_message": "CUDA out of memory"}]]}
                return httpx.Response(200, json={"p1": {"status": status, "outputs": {}}})
            outputs = {"9": {"images": [{"filename": "video_00001.mp4", "subfolder": "", "type": "output"}],
                             "animated": [True]}}
            return httpx.Response(200, json={"p1": {"status": {"status_str": "success", "completed": True},
                                                    "outputs": outputs}})
        if path == "/queue":
            return httpx.Response(200, json={"queue_running": [], "queue_pending": [[1, "p1", {}, {}, []]]})
        if path == "/view":
            return httpx.Response(200, content=b"MP4DATA")
        return httpx.Response(404)


def save_workflow():
    config.COMFY_WORKFLOW.parent.mkdir(parents=True, exist_ok=True)
    config.COMFY_WORKFLOW.write_text(json.dumps(WORKFLOW), encoding="utf-8")


def test_generation_lifecycle(monkeypatch):
    save_workflow()
    sent = []

    async def fake_notify(text, transport=None):
        sent.append(text)

    monkeypatch.setattr("app.videogen.notify", fake_notify)
    comfy = FakeComfy()
    transport = httpx.MockTransport(comfy)
    gen = videogen.create_generation("summer beach, seamless loop", "blurry")
    asyncio.run(videogen.poll(transport))  # submit
    gen = videogen.get_generation(gen.id)
    assert gen.status == "running" and gen.comfy_prompt_id == "p1"
    assert comfy.prompts["p1"]["6"]["inputs"]["text"].startswith("summer beach")
    asyncio.run(videogen.poll(transport))  # still in the ComfyUI queue
    assert videogen.get_generation(gen.id).queue_position == 1
    asyncio.run(videogen.poll(transport))  # finished
    gen = videogen.get_generation(gen.id)
    assert gen.status == "done" and open(gen.video_path, "rb").read() == b"MP4DATA"
    assert sent and "#%d" % gen.id in sent[0]
    from app import assistant
    assert any("готове" in m.content for m in assistant.all_messages())


def test_generation_failure_is_explained(monkeypatch):
    save_workflow()
    settings_store.set_value("assistant_auto_analyze", "0")
    monkeypatch.setattr("app.videogen.notify", lambda text, transport=None: _none())
    comfy = FakeComfy(fail=True)
    transport = httpx.MockTransport(comfy)
    gen = videogen.create_generation("x")
    for _ in range(3):
        asyncio.run(videogen.poll(transport))
    gen = videogen.get_generation(gen.id)
    assert gen.status == "failed" and "CUDA out of memory" in gen.error


async def _none():
    return None


def test_offline_comfy_keeps_generation_queued():
    save_workflow()

    def down(request):
        raise httpx.ConnectError("refused")

    gen = videogen.create_generation("x")
    asyncio.run(videogen.poll(httpx.MockTransport(down)))
    gen = videogen.get_generation(gen.id)
    assert gen.status == "queued" and gen.error == "comfy_offline"


def test_missing_workflow_fails_with_code():
    config.COMFY_WORKFLOW.unlink(missing_ok=True)
    gen = videogen.create_generation("x")
    asyncio.run(videogen.poll(httpx.MockTransport(FakeComfy())))
    gen = videogen.get_generation(gen.id)
    assert gen.status == "failed" and gen.error == "no_workflow"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("app.db.init_db", lambda url=None: None)
    with TestClient(app) as c:
        yield c


def test_video_pages_and_generation_to_post(client):
    assert "Генерація відео" in client.get("/videos").text
    client.post("/videos", data={"prompt": "ocean at night", "negative": ""})
    from app.web import generations
    gen = generations()[0]
    assert gen.prompt == "ocean at night" and gen.status == "queued"
    video = config.GENERATIONS_DIR / f"{gen.id}.mp4"
    config.GENERATIONS_DIR.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"0")
    videogen.update(gen.id, status="done", video_path=str(video))
    assert client.get(f"/generations/{gen.id}/video").status_code == 200
    r = client.post(f"/generations/{gen.id}/post")
    target = r.headers["HX-Redirect"]
    assert target.startswith("/?video=")
    page = client.get(target).text
    assert 'name="video_name"' in page
    workflow = json.dumps(WORKFLOW).encode()
    client.post("/settings/workflow", files={"workflow": ("wf.json", workflow, "application/json")})
    assert videogen.load_workflow() == WORKFLOW
    assert "Завантажено (4" in client.get("/settings").text
