"""Video generation with a local model through ComfyUI (e.g. MiniMax H3 installed with Pinokio).

The user exports a working ComfyUI workflow in API format ("Export (API)") after typing
{{PROMPT}} into its prompt field; optional {{NEGATIVE}} (negative prompt) and {{IMAGE}}
(file name of a Load Image node, for image-to-video). Any model the workflow uses will work.
"""
import copy
import json
import logging
import random
from pathlib import Path

import httpx
from sqlmodel import select

from app import config, settings_store
from app.db import get_session
from app.models import Generation, utcnow
from app.notify import message, notify

logger = logging.getLogger("poster.videogen")

VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov", ".gif")
SEED_KEYS = {"seed", "noise_seed"}
CLIENT_ID = "web-api-poster"


class ComfyError(Exception):
    pass


# ---- workflow ----

def load_workflow() -> dict | None:
    if not config.COMFY_WORKFLOW.exists():
        return None
    return json.loads(config.COMFY_WORKFLOW.read_text(encoding="utf-8"))


def parse_workflow(raw: str) -> dict:
    """Check an uploaded workflow: it must be ComfyUI's API format (node id -> {class_type, inputs})."""
    data = json.loads(raw)
    if not isinstance(data, dict) or not data or not all(
            isinstance(n, dict) and "class_type" in n for n in data.values()):
        raise ValueError("not an API-format workflow")
    return data


def workflow_needs_image(workflow: dict) -> bool:
    return "{{IMAGE}}" in json.dumps(workflow)


def fill_workflow(workflow: dict, prompt: str, negative: str = "", image_name: str | None = None,
                  seed: int | None = None) -> dict:
    """Put the prompt (and negative / image / a random seed) into a copy of the workflow."""
    wf = copy.deepcopy(workflow)
    seed = random.randint(0, 2**31 - 1) if seed is None else seed
    has_prompt = False
    for node in wf.values():
        inputs = node.get("inputs", {})
        for key, value in inputs.items():
            if isinstance(value, str) and "{{PROMPT}}" in value:
                inputs[key] = value.replace("{{PROMPT}}", prompt)
                has_prompt = True
            elif isinstance(value, str) and "{{NEGATIVE}}" in value:
                inputs[key] = value.replace("{{NEGATIVE}}", negative)
            elif isinstance(value, str) and "{{IMAGE}}" in value:
                if not image_name:
                    raise ComfyError("needs_image")
                inputs[key] = image_name
            elif key in SEED_KEYS and isinstance(value, int):
                inputs[key] = seed  # a new video every time
    if not has_prompt:  # no placeholder: use the first text-encode node
        for node_id in sorted(wf, key=lambda k: (not str(k).isdigit(), int(k) if str(k).isdigit() else 0, str(k))):
            inputs = wf[node_id].get("inputs", {})
            if "TextEncode" in wf[node_id].get("class_type", "") and isinstance(inputs.get("text"), str):
                inputs["text"] = prompt
                has_prompt = True
                break
    if not has_prompt:
        raise ComfyError("no_prompt_field")
    return wf


def find_video(outputs: dict) -> dict | None:
    """Find the produced video among ComfyUI outputs (different nodes use different keys)."""
    for node_output in outputs.values():
        for key in ("videos", "gifs", "images"):
            for item in node_output.get(key, []) or []:
                if str(item.get("filename", "")).lower().endswith(VIDEO_EXTENSIONS):
                    return item
    return None


def execution_error(status: dict) -> str:
    for msg in status.get("messages", []) or []:
        if isinstance(msg, list) and len(msg) == 2 and msg[0] == "execution_error":
            info = msg[1] or {}
            return f"{info.get('node_type', '')}: {info.get('exception_message', '')}".strip(": ")[:500]
    return "ComfyUI reported an error"


# ---- ComfyUI HTTP API ----

class ComfyClient:
    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.transport = transport

    def _client(self, timeout: float = 30) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_url, timeout=timeout, transport=self.transport)

    async def system_stats(self) -> dict:
        async with self._client(10) as c:
            r = await c.get("/system_stats")
            r.raise_for_status()
            return r.json()

    async def upload_image(self, path: Path) -> str:
        async with self._client(120) as c:
            with open(path, "rb") as f:
                r = await c.post("/upload/image", files={"image": (path.name, f)}, data={"overwrite": "true"})
            r.raise_for_status()
            data = r.json()
        return f"{data['subfolder']}/{data['name']}" if data.get("subfolder") else data["name"]

    async def submit(self, workflow: dict) -> str:
        async with self._client() as c:
            r = await c.post("/prompt", json={"prompt": workflow, "client_id": CLIENT_ID})
            data = r.json() if r.content else {}
            if r.status_code >= 400 or "prompt_id" not in data:
                error = data.get("error", {})
                detail = error.get("message") if isinstance(error, dict) else str(error)
                nodes = "; ".join(f"{k}: {v.get('class_type', '')}" for k, v in (data.get("node_errors") or {}).items())
                raise ComfyError(f"ComfyUI rejected the workflow: {detail or r.status_code} {nodes}".strip())
            return data["prompt_id"]

    async def state(self, prompt_id: str) -> tuple[str, dict]:
        """("queued", {"position": n}) | ("running", {}) | ("done", {"file": ...}) | ("failed", {"error": ...})
        | ("lost", {}) when ComfyUI does not know the task (it was restarted)."""
        async with self._client() as c:
            r = await c.get(f"/history/{prompt_id}")
            r.raise_for_status()
            entry = r.json().get(prompt_id)
            if entry:
                status = entry.get("status") or {}
                if status.get("status_str") == "error":
                    return "failed", {"error": execution_error(status)}
                video = find_video(entry.get("outputs") or {})
                if video:
                    return "done", {"file": video}
                if status.get("completed"):
                    return "failed", {"error": "the workflow finished without a video output"}
                return "running", {}
            r = await c.get("/queue")
            r.raise_for_status()
            queue = r.json()
        if any(item[1] == prompt_id for item in queue.get("queue_running", [])):
            return "running", {}
        pending = [item[1] for item in sorted(queue.get("queue_pending", []), key=lambda i: i[0])]
        if prompt_id in pending:
            return "queued", {"position": pending.index(prompt_id) + 1}
        return "lost", {}

    async def download(self, file: dict, dest: Path) -> None:
        params = {"filename": file["filename"], "subfolder": file.get("subfolder", ""), "type": file.get("type", "output")}
        async with self._client(600) as c:
            async with c.stream("GET", "/view", params=params) as r:
                r.raise_for_status()
                with open(dest, "wb") as f:
                    async for chunk in r.aiter_bytes():
                        f.write(chunk)

    async def cancel(self, prompt_id: str) -> None:
        state, _ = await self.state(prompt_id)
        async with self._client(10) as c:
            await c.post("/queue", json={"delete": [prompt_id]})
            if state == "running":  # interrupt only our own task, not someone else's
                await c.post("/interrupt")


def client(transport: httpx.AsyncBaseTransport | None = None) -> ComfyClient:
    return ComfyClient(settings_store.get("comfy_url"), transport)


# ---- generations ----

def create_generation(prompt: str, negative: str = "", image_path: str | None = None) -> Generation:
    with get_session() as s:
        gen = Generation(prompt=prompt.strip(), negative=negative.strip(), image_path=image_path)
        s.add(gen)
        s.commit()
        return gen


def get_generation(gen_id: int) -> Generation | None:
    with get_session() as s:
        return s.get(Generation, gen_id)


def update(gen_id: int, **fields) -> Generation | None:
    with get_session() as s:
        gen = s.get(Generation, gen_id)
        if gen is None:
            return None
        for key, value in fields.items():
            setattr(gen, key, value)
        s.add(gen)
        s.commit()
        return gen


def retry_generation(gen_id: int) -> bool:
    gen = get_generation(gen_id)
    if not gen or gen.status not in ("failed", "cancelled", "done"):
        return False
    update(gen_id, status="queued", comfy_prompt_id=None, queue_position=None, error=None, finished_at=None)
    return True


async def cancel_generation(gen_id: int, transport=None) -> bool:
    gen = get_generation(gen_id)
    if not gen or gen.status not in ("queued", "running"):
        return False
    if gen.comfy_prompt_id:
        try:
            await client(transport).cancel(gen.comfy_prompt_id)
        except httpx.HTTPError:
            pass  # ComfyUI is down: nothing to cancel there
    update(gen_id, status="cancelled", finished_at=utcnow())
    return True


def delete_generation(gen_id: int) -> bool:
    with get_session() as s:
        gen = s.get(Generation, gen_id)
        if not gen or gen.status in ("queued", "running"):
            return False
        for path in (gen.video_path, gen.image_path):
            if path and Path(path).parent == config.GENERATIONS_DIR:
                Path(path).unlink(missing_ok=True)
        s.delete(gen)
        s.commit()
    return True


async def _submit(c: ComfyClient, gen: Generation) -> None:
    workflow = load_workflow()
    if workflow is None:
        raise ComfyError("no_workflow")
    image_name = await c.upload_image(Path(gen.image_path)) if gen.image_path else None
    prompt_id = await c.submit(fill_workflow(workflow, gen.prompt, gen.negative, image_name))
    update(gen.id, status="running", comfy_prompt_id=prompt_id, error=None)


async def _check(c: ComfyClient, gen: Generation) -> str | None:
    """Returns the final status when the generation ended."""
    state, info = await c.state(gen.comfy_prompt_id)
    if state == "queued":
        update(gen.id, queue_position=info["position"])
    elif state == "running":
        update(gen.id, queue_position=None)
    elif state == "done":
        config.GENERATIONS_DIR.mkdir(parents=True, exist_ok=True)
        dest = config.GENERATIONS_DIR / f"{gen.id}{Path(info['file']['filename']).suffix.lower()}"
        await c.download(info["file"], dest)
        update(gen.id, status="done", video_path=str(dest), queue_position=None, finished_at=utcnow())
        return "done"
    else:
        error = info.get("error") or "ComfyUI lost the task (was it restarted?)"
        update(gen.id, status="failed", error=error, queue_position=None, finished_at=utcnow())
        return "failed"
    return None


async def _announce(gen_id: int) -> None:
    """Tell the owner (Telegram) and the assistant chat how a generation ended."""
    from app import assistant  # assistant imports this module

    gen = get_generation(gen_id)
    short = (gen.prompt[:80] + "…") if len(gen.prompt) > 80 else gen.prompt
    if gen.status == "done":
        await notify(message("notify.gen_done", id=gen.id, prompt=short))
        assistant.add_event(message("event.gen_done", id=gen.id, prompt=short))
    else:
        await notify(message("notify.gen_failed", id=gen.id, error=gen.error or ""))
        assistant.add_event(message("event.gen_failed", id=gen.id, prompt=short, error=gen.error or ""))
        if settings_store.get_bool("assistant_auto_analyze") and assistant.configured():
            await assistant.reply()  # the assistant explains the error and suggests a fixed prompt


async def poll(transport: httpx.AsyncBaseTransport | None = None) -> None:
    """Called every 10 s: send queued generations to ComfyUI, follow running ones, download results."""
    with get_session() as s:
        gens = s.exec(select(Generation).where(Generation.status.in_(["queued", "running"]))
                      .order_by(Generation.id)).all()
    if not gens:
        return
    c = client(transport)
    for gen in gens:
        try:
            if gen.status == "queued":
                await _submit(c, gen)
                continue
            ended = await _check(c, gen)
        except httpx.TransportError:
            update(gen.id, error="comfy_offline")  # ComfyUI is not running: try again later
            continue
        except Exception as e:
            reason = str(e) if isinstance(e, ComfyError) else f"{type(e).__name__}: {e}"
            update(gen.id, status="failed", error=reason[:500], finished_at=utcnow())
            ended = "failed"
        if ended:
            try:
                await _announce(gen.id)
            except Exception:
                logger.exception("announcing generation %s failed", gen.id)
