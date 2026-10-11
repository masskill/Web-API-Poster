"""AI assistant (OpenAI chat with tools): ideas, prompts, following generations and publications.

Read-only tools run at once. Actions (start a generation, create a publication) are only proposed:
the chat shows a card and nothing happens until the user presses "Confirm".
"""
import json
import os
import shutil
from pathlib import Path

import httpx
from sqlmodel import delete, select

from app import config, posting, settings_store, videogen
from app.config import scrub
from app.db import get_session
from app.models import Account, AccountGroup, AssistantAction, AssistantMessage, Generation, Job, Post, utcnow
from app.notify import message
from app.platforms import platform_name
from app.texts import PlatformText, parse_hashtags

# Any OpenAI-compatible endpoint works (e.g. OpenRouter); default is OpenAI itself.
OPENAI_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
HISTORY = 20  # messages sent to the model: keeps every request cheap
MAX_STEPS = 5

SYSTEM_PROMPT = """You are the assistant inside Web-API-Poster, a local app of one video creator.
The app generates short videos with a local model (MiniMax H3 running in ComfyUI on the user's PC)
and publishes them to YouTube, TikTok, Instagram, Telegram and other platforms.

Help with:
- ideas for videos and series, content plans, trends of the user's niche;
- prompts for the video model: always in English, one paragraph: subject, setting, lighting, colors,
  camera movement, mood, style; add "seamless loop" for background videos; give a short negative prompt too;
  clips are 5-10 seconds, so describe one continuous shot, not a story with cuts;
- titles, descriptions and hashtags for publications;
- following generations and publications: use the tools to check statuses, explain errors in plain words
  and suggest a fixed prompt or setting.

Rules:
- Reply in the user's language (Ukrainian by default), short and practical. Put prompts in ``` blocks.
- start_generation and create_post only PROPOSE an action: the user must press "Confirm" in the chat.
  Call them when the user asked for it or clearly agreed; never call the same action twice.
- For create_post use a finished generation (status done). Ask which group or accounts if unclear.
- Messages starting with [Event] come from the app, not from the user.
"""

TOOLS = [
    {"type": "function", "function": {
        "name": "list_generations", "description": "Recent video generations with status and errors.",
        "parameters": {"type": "object", "properties": {"limit": {"type": "integer", "default": 10}}}}},
    {"type": "function", "function": {
        "name": "list_jobs", "description": "Recent publication jobs (platform, status, errors).",
        "parameters": {"type": "object", "properties": {"limit": {"type": "integer", "default": 15}}}}},
    {"type": "function", "function": {
        "name": "list_accounts_and_groups", "description": "Platform accounts and account groups of the user.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "start_generation",
        "description": "Propose to generate a video with the local model. Runs only after the user confirms.",
        "parameters": {"type": "object", "required": ["prompt"], "properties": {
            "prompt": {"type": "string", "description": "English prompt for the video model"},
            "negative_prompt": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "create_post",
        "description": "Propose to publish a finished generation. Runs only after the user confirms.",
        "parameters": {"type": "object", "required": ["generation_id", "title"], "properties": {
            "generation_id": {"type": "integer"},
            "title": {"type": "string"},
            "description": {"type": "string"},
            "hashtags": {"type": "array", "items": {"type": "string"}},
            "group_id": {"type": "integer", "description": "publish to all accounts of this group"},
            "account_ids": {"type": "array", "items": {"type": "integer"}},
            "when": {"type": "string", "enum": ["now", "slot"], "description": "slot = next free slot of the group"},
            "dry_run": {"type": "boolean", "description": "true = test run without pressing Publish", "default": True},
        }}}},
]
READ_TOOLS = {"list_generations", "list_jobs", "list_accounts_and_groups"}
ACTION_TOOLS = {"start_generation", "create_post"}


def configured() -> bool:
    return bool(settings_store.get("openai_api_key").strip())


# ---- chat storage ----

def add_message(role: str, content: str = "", **fields) -> AssistantMessage:
    with get_session() as s:
        msg = AssistantMessage(role=role, content=content, **fields)
        s.add(msg)
        s.commit()
        return msg


def add_event(text: str) -> AssistantMessage:
    return add_message("event", text)


def all_messages() -> list[AssistantMessage]:
    with get_session() as s:
        return list(s.exec(select(AssistantMessage).order_by(AssistantMessage.id)).all())


def actions_by_id() -> dict[int, AssistantAction]:
    with get_session() as s:
        return {a.id: a for a in s.exec(select(AssistantAction)).all()}


def clear() -> None:
    with get_session() as s:
        s.exec(delete(AssistantMessage))
        s.exec(delete(AssistantAction).where(AssistantAction.status == "pending"))
        s.commit()


def api_messages(limit: int = HISTORY) -> list[dict]:
    """The last messages in OpenAI format (a tool result is never sent without its tool call)."""
    with get_session() as s:
        rows = list(s.exec(select(AssistantMessage).order_by(AssistantMessage.id.desc()).limit(limit)).all())[::-1]
    while rows and rows[0].role == "tool":
        rows.pop(0)
    result = []
    for m in rows:
        if m.role == "user":
            result.append({"role": "user", "content": m.content})
        elif m.role == "event":
            result.append({"role": "user", "content": f"[Event] {m.content}"})
        elif m.role == "assistant":
            item = {"role": "assistant", "content": m.content or None}
            if m.tool_calls:
                item["tool_calls"] = json.loads(m.tool_calls)
            result.append(item)
        elif m.role == "tool":
            result.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content})
    return result


# ---- read-only tools ----

def _local(dt) -> str:
    return dt.astimezone(posting.local_tz()).strftime("%d.%m %H:%M") if dt else ""


def run_read_tool(name: str, args: dict) -> dict:
    with get_session() as s:
        if name == "list_generations":
            gens = s.exec(select(Generation).order_by(Generation.id.desc()).limit(int(args.get("limit") or 10))).all()
            return {"generations": [{"id": g.id, "status": g.status, "prompt": g.prompt[:300], "error": g.error,
                                     "queue_position": g.queue_position, "created": _local(g.created_at)}
                                    for g in gens]}
        if name == "list_jobs":
            jobs = s.exec(select(Job).order_by(Job.id.desc()).limit(int(args.get("limit") or 15))).all()
            out = []
            for j in jobs:
                acc, post = s.get(Account, j.account_id), s.get(Post, j.post_id)
                out.append({"job_id": j.id, "post_id": j.post_id, "title": post.title if post else "",
                            "platform": platform_name(acc.platform) if acc else "", "account": acc.name if acc else "",
                            "status": j.status, "dry_run": j.dry_run, "error_code": j.error_code,
                            "error": (j.error or "")[:300], "time": _local(j.run_at)})
            return {"jobs": out}
        if name == "list_accounts_and_groups":
            accounts = s.exec(select(Account)).all()
            groups = s.exec(select(AccountGroup)).all()
            return {"accounts": [{"id": a.id, "platform": platform_name(a.platform), "name": a.name,
                                  "logged_in": a.logged_in_at is not None} for a in accounts],
                    "groups": [{"id": g.id, "name": g.name, "account_ids": g.ids, "slots": g.slot_times}
                               for g in groups]}
    return {"error": f"unknown tool {name}"}


# ---- actions (after confirmation) ----

def propose(kind: str, args: dict) -> AssistantAction:
    with get_session() as s:
        action = AssistantAction(kind=kind, args=json.dumps(args, ensure_ascii=False))
        s.add(action)
        s.commit()
        return action


def _execute(kind: str, args: dict):
    """Returns (result text, run_at of a created publication or None)."""
    if kind == "start_generation":
        gen = videogen.create_generation(str(args.get("prompt", "")), str(args.get("negative_prompt", "")))
        return message("event.gen_queued", id=gen.id), None
    if kind == "create_post":
        gen = videogen.get_generation(int(args.get("generation_id", 0)))
        if not gen or gen.status != "done" or not gen.video_path:
            raise ValueError("the generation has no finished video")
        with get_session() as s:
            group = s.get(AccountGroup, int(args["group_id"])) if args.get("group_id") else None
            ids = set(group.ids if group else args.get("account_ids") or [])
            accounts = [a for a in s.exec(select(Account)).all() if a.id in ids]
        if not accounts:
            raise ValueError("no accounts chosen")
        video = config.MEDIA_DIR / posting.media_name(Path(gen.video_path).name)
        config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy(gen.video_path, video)
        run_at = posting.next_free_slot(group) if group and args.get("when") == "slot" else None
        template = PlatformText(str(args.get("title", "")), str(args.get("description", "")),
                                parse_hashtags(args.get("hashtags") or []))
        post = posting.create_post(video, template, accounts, run_at, bool(args.get("dry_run", True)),
                                   category=group.name if group else "", group_id=group.id if group else None)
        when = _local(run_at) if run_at else message("event.now")
        text = message("event.post_created", id=post.id, n=len(accounts), when=when,
                       dry_run="dry-run" if post.dry_run else message("event.for_real"))
        return text, run_at or utcnow()
    raise ValueError(f"unknown action {kind}")


def resolve_action(action_id: int, confirm: bool):
    """Confirm or cancel a proposed action. Returns run_at of a created publication (to wake the queue)."""
    with get_session() as s:
        action = s.get(AssistantAction, action_id)
        if not action or action.status != "pending":
            return None
        args = json.loads(action.args)
    run_at = None
    if not confirm:
        status, result = "cancelled", message("event.action_cancelled")
    else:
        try:
            result, run_at = _execute(action.kind, args)
            status = "done"
        except Exception as e:
            status, result = "failed", str(e)
    with get_session() as s:
        action = s.get(AssistantAction, action_id)
        action.status, action.result = status, result
        s.add(action)
        s.commit()
    add_event(result if status != "failed" else message("event.action_failed", error=result))
    return run_at


# ---- talking to the model ----

async def call_openai(messages: list[dict], transport: httpx.AsyncBaseTransport | None = None) -> dict:
    key = settings_store.get("openai_api_key").strip()
    async with httpx.AsyncClient(timeout=90, transport=transport) as c:
        r = await c.post(OPENAI_URL, headers={"Authorization": f"Bearer {key}"}, json={
            "model": settings_store.get("assistant_model") or "gpt-5.4-mini",
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + messages,
            "tools": TOOLS,
            "max_completion_tokens": 2000,
        })
        if r.status_code >= 400:
            raise RuntimeError(f"OpenAI {r.status_code}: {r.text[:300]}")
        return r.json()


async def reply(transport: httpx.AsyncBaseTransport | None = None) -> None:
    """Get the next assistant answer: read-only tools run at once, actions become confirmation cards."""
    key = settings_store.get("openai_api_key")
    if not configured():
        add_event("no_key")
        return
    for _ in range(MAX_STEPS):
        try:
            data = await call_openai(api_messages(), transport)
        except Exception as e:
            add_event(scrub(f"error: {type(e).__name__}: {e}", [key])[:400])
            return
        msg = data["choices"][0]["message"]
        calls = msg.get("tool_calls") or []
        add_message("assistant", msg.get("content") or "", tool_calls=json.dumps(calls) if calls else "")
        if not calls:
            return
        for call in calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            action_id = None
            if name in READ_TOOLS:
                result = run_read_tool(name, args)
            elif name in ACTION_TOOLS:
                action_id = propose(name, args).id
                result = {"status": "awaiting_user_confirmation", "action_id": action_id,
                          "note": "Shown to the user as a card with Confirm / Cancel. Do not call it again."}
            else:
                result = {"error": f"unknown tool {name}"}
            add_message("tool", json.dumps(result, ensure_ascii=False, default=str),
                        tool_call_id=call["id"], action_id=action_id)
    add_event("too_many_steps")
