"""One template -> per-platform texts. AI (OpenAI / Anthropic over plain HTTP) with a no-AI fallback."""
import json
import re
from dataclasses import asdict, dataclass, field

import httpx

AI_TIMEOUT = 60


@dataclass
class PlatformText:
    title: str = ""
    description: str = ""
    hashtags: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "PlatformText":
        return cls(**json.loads(raw or "{}"))

    def hashtags_line(self) -> str:
        return " ".join(f"#{t}" for t in self.hashtags)

    def caption(self) -> str:
        """Description with hashtags appended - what goes into the main text field."""
        return "\n\n".join(p for p in (self.description, self.hashtags_line()) if p)


def parse_hashtags(raw: str | list) -> list[str]:
    items = raw if isinstance(raw, list) else re.split(r"[\s,;]+", raw or "")
    tags = []
    for item in items:
        tag = str(item).strip().lstrip("#").strip()
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def truncate(text: str, limit: int) -> str:
    text = text.strip()
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def fit_to_rules(text: PlatformText, rules: dict, merge_title: bool = True) -> PlatformText:
    """Cut text to the platform limits. Without a title field the title is moved into the text."""
    title, description = text.title.strip(), text.description.strip()
    if not rules.get("has_title"):
        if merge_title and title and not description.startswith(title):
            description = f"{title}\n\n{description}" if description else title
        title = ""
    else:
        title = truncate(title, rules.get("title_max", 100))

    tags = list(text.hashtags)[: rules.get("hashtags_max", 5)]
    limit = rules.get("description_max", 5000)
    # Hashtags are appended to the description, so drop tags until both fit.
    while tags and len(PlatformText(hashtags=tags).hashtags_line()) + 2 > limit // 2:
        tags.pop()
    tags_len = len(PlatformText(hashtags=tags).hashtags_line())
    budget = limit - (tags_len + 2 if tags else 0)
    return PlatformText(title=title, description=truncate(description, budget), hashtags=tags)


def fallback_texts(template: PlatformText, platforms: dict[str, dict], keys: list[str]) -> dict[str, PlatformText]:
    return {k: fit_to_rules(template, platforms[k]) for k in keys}


SYSTEM_PROMPT = (
    "You adapt one social media video post for several platforms. "
    "Keep the language of the template. Respect every limit exactly (characters include spaces). "
    "If a platform has no title field, put the essence of the title into the description and return an empty title. "
    "Hashtags: words without '#'. Hashtags are appended to the description, so description + hashtags must fit "
    "description_max. Reply with JSON only: "
    '{"<platform_key>": {"title": "...", "description": "...", "hashtags": ["..."]}, ...}'
)


def build_prompt(template: PlatformText, category: str, platforms: dict[str, dict], keys: list[str]) -> str:
    rules = {
        k: {
            "name": platforms[k].get("name", k),
            "has_title": bool(platforms[k].get("has_title")),
            "title_max": platforms[k].get("title_max", 0),
            "description_max": platforms[k].get("description_max"),
            "hashtags_max": platforms[k].get("hashtags_max"),
            "style": platforms[k].get("style", ""),
        }
        for k in keys
    }
    return json.dumps(
        {"template": asdict(template), "category": category, "platforms": rules},
        ensure_ascii=False, indent=1,
    )


def extract_json(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("AI reply has no JSON")
    return json.loads(text[start : end + 1])


async def call_ai(provider: str, api_key: str, model: str, prompt: str,
                  client: httpx.AsyncClient | None = None) -> dict:
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=AI_TIMEOUT)
    try:
        if provider == "openai":
            r = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                },
            )
            r.raise_for_status()
            return extract_json(r.json()["choices"][0]["message"]["content"])
        if provider == "anthropic":
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
                json={
                    "model": model,
                    "max_tokens": 4096,
                    "system": SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            r.raise_for_status()
            text = "".join(b.get("text", "") for b in r.json()["content"] if b.get("type") == "text")
            return extract_json(text)
        raise ValueError(f"unknown AI provider: {provider}")
    finally:
        if own_client:
            await client.aclose()


async def adapt_texts(template: PlatformText, category: str, keys: list[str], platforms: dict[str, dict],
                      settings: dict[str, str], client: httpx.AsyncClient | None = None
                      ) -> tuple[dict[str, PlatformText], str | None]:
    """Return (texts per platform, error). error is None when AI succeeded,
    "not_configured" when AI is off, or a short error text; texts are always filled."""
    provider = settings.get("ai_provider", "none")
    api_key = settings.get(f"{provider}_api_key", "")
    if provider not in ("openai", "anthropic") or not api_key or not keys:
        return fallback_texts(template, platforms, keys), "not_configured"
    try:
        data = await call_ai(provider, api_key, settings.get(f"{provider}_model", ""),
                             build_prompt(template, category, platforms, keys), client)
    except Exception as e:  # network, HTTP status, bad JSON
        msg = f"{type(e).__name__}: {e}".replace(api_key, "***")
        return fallback_texts(template, platforms, keys), msg[:300]

    result = {}
    for k in keys:
        item = data.get(k)
        if not isinstance(item, dict):
            result[k] = fit_to_rules(template, platforms[k])
            continue
        ai_text = PlatformText(
            title=str(item.get("title") or ""),
            description=str(item.get("description") or ""),
            hashtags=parse_hashtags(item.get("hashtags") or []),
        )
        # AI can still overshoot limits: enforce them, but don't duplicate the title.
        result[k] = fit_to_rules(ai_text, platforms[k], merge_title=False)
    return result, None
