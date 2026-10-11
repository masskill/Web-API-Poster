import asyncio
import json

import httpx

from app.platforms import load_platforms
from app.texts import PlatformText, adapt_texts, fit_to_rules, parse_hashtags, truncate

TEMPLATE = PlatformText(title="Борщ за 30 хвилин", description="Покроковий рецепт. " * 100,
                        hashtags=parse_hashtags("#борщ #рецепт, кухня #борщ їжа шеф обід вечеря"))


def test_parse_hashtags_dedupes_and_strips():
    assert parse_hashtags("#a b,#c  a") == ["a", "b", "c"]


def test_truncate():
    assert truncate("hello", 10) == "hello"
    assert truncate("hello world", 6) == "hello…"
    assert len(truncate("x" * 50, 10)) == 10


def test_fallback_fits_every_platform():
    platforms = load_platforms()
    for key, rules in platforms.items():
        t = fit_to_rules(TEMPLATE, rules)
        assert len(t.caption()) <= rules["description_max"], key
        assert len(t.hashtags) <= rules["hashtags_max"], key
        if rules["has_title"]:
            assert len(t.title) <= rules["title_max"]
        else:
            assert t.title == "" and t.description.startswith("Борщ"), key


def test_fallback_x_is_short():
    t = fit_to_rules(TEMPLATE, load_platforms()["x"])
    assert len(t.caption()) <= 280 and len(t.hashtags) <= 2


def run(coro):
    return asyncio.run(coro)


def test_adapt_without_ai_uses_fallback():
    texts, err = run(adapt_texts(TEMPLATE, "Рецепти", ["youtube", "telegram"], load_platforms(),
                                 {"ai_provider": "none"}))
    assert err == "not_configured"
    assert set(texts) == {"youtube", "telegram"}


def test_adapt_with_mocked_anthropic():
    reply = {
        "youtube": {"title": "Борщ " * 40, "description": "Опис", "hashtags": ["#борщ", "рецепт"]},
        "telegram": {"title": "", "description": "Пост для каналу", "hashtags": ["борщ"]},
    }
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["key"] = request.headers["x-api-key"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"content": [{"type": "text", "text": json.dumps(reply)}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = {"ai_provider": "anthropic", "anthropic_api_key": "sk-test", "anthropic_model": "m"}
    texts, err = run(adapt_texts(TEMPLATE, "Рецепти", ["youtube", "telegram"], load_platforms(), settings, client))
    assert err is None
    assert seen["url"].endswith("/v1/messages") and seen["key"] == "sk-test"
    assert "youtube" in seen["body"]["messages"][0]["content"]
    assert len(texts["youtube"].title) <= 100  # AI overshoot is cut
    assert texts["youtube"].hashtags == ["борщ", "рецепт"]
    assert texts["telegram"].description == "Пост для каналу"


def test_adapt_with_mocked_openai_and_missing_platform():
    reply = {"youtube": {"title": "T", "description": "D", "hashtags": []}}

    def handler(request):
        assert request.headers["authorization"] == "Bearer sk-o"
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(reply)}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = {"ai_provider": "openai", "openai_api_key": "sk-o", "openai_model": "m"}
    texts, err = run(adapt_texts(TEMPLATE, "", ["youtube", "telegram"], load_platforms(), settings, client))
    assert err is None
    assert texts["youtube"].title == "T"
    assert texts["telegram"].description.startswith("Борщ")  # filled by fallback


def test_ai_error_falls_back_and_hides_key():
    def handler(request):
        return httpx.Response(401, json={"error": "bad key sk-secret-123"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = {"ai_provider": "openai", "openai_api_key": "sk-secret-123", "openai_model": "m"}
    texts, err = run(adapt_texts(TEMPLATE, "", ["youtube"], load_platforms(), settings, client))
    assert err and "sk-secret-123" not in err
    assert texts["youtube"].title == "Борщ за 30 хвилин"
