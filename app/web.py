"""Web UI routes (server-rendered Jinja2 + HTMX)."""
import asyncio
import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import select

from app import browser, config, posting, settings_store, worker
from app.db import get_session
from app.i18n import DEFAULT_LANG, LANGS, translate
from app.models import Account, AccountGroup, Category, Job, Post, utcnow
from app.posting import local_tz
from app.platforms import load_platforms, platform_name
from app.texts import PlatformText, adapt_texts, fit_to_rules, parse_hashtags
from app.video import check_video, probe

router = APIRouter()
templates = Jinja2Templates(directory=config.TEMPLATES_DIR)

DEBUG_FILES = {"screenshot.png", "page.html", "steps.log"}
# Result of the last "Перевірити вхід" per account id: ok | fail | error text
check_results: dict[int, str] = {}


# ---- helpers ----

def get_lang(request: Request) -> str:
    lang = request.cookies.get("lang")
    return lang if lang in LANGS else DEFAULT_LANG


def fmt_dt(dt: datetime | None) -> str:
    return dt.astimezone(local_tz()).strftime("%d.%m.%Y %H:%M") if dt else ""


def render(request: Request, template: str, status_code: int = 200, **ctx) -> HTMLResponse:
    lang = get_lang(request)
    ctx.update(
        lang=lang,
        t=lambda key, **kw: translate(lang, key, **kw),
        platforms=load_platforms(),
        platform_name=platform_name,
        fmt_dt=fmt_dt,
        debug_files=lambda d: [n for n in sorted(DEBUG_FILES) if (Path(d) / n).exists()],
    )
    return templates.TemplateResponse(request, template, ctx, status_code=status_code)


def implemented_platforms() -> list[str]:
    from app.publishers import PUBLISHERS
    return [k for k in load_platforms() if k in PUBLISHERS]


def all_accounts() -> list[Account]:
    with get_session() as s:
        return list(s.exec(select(Account).order_by(Account.platform, Account.name)).all())


def all_groups() -> list[AccountGroup]:
    with get_session() as s:
        return list(s.exec(select(AccountGroup).order_by(AccountGroup.name)).all())


def accounts_needing_login() -> list[Account]:
    return [a for a in all_accounts() if a.logged_in_at is None]


def media_path(name: str) -> Path:
    """Only files directly inside data/media are allowed."""
    return config.MEDIA_DIR / Path(name).name


def selected_accounts(form) -> list[Account]:
    ids = {int(i) for i in form.getlist("account_ids") if str(i).isdigit()}
    return [a for a in all_accounts() if a.id in ids]


def template_from_form(form) -> PlatformText:
    return PlatformText(
        title=str(form.get("title", "")),
        description=str(form.get("description", "")),
        hashtags=parse_hashtags(str(form.get("hashtags", ""))),
    )


def text_from_form(form, platform: str) -> PlatformText | None:
    if f"text_{platform}_description" not in form:
        return None
    return PlatformText(
        title=str(form.get(f"text_{platform}_title", "")),
        description=str(form.get(f"text_{platform}_description", "")),
        hashtags=parse_hashtags(str(form.get(f"text_{platform}_hashtags", ""))),
    )


def unique_slug(platform: str, name: str) -> str:
    base = config.slugify(name)
    taken = {a.slug for a in all_accounts() if a.platform == platform}
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug


# ---- language ----

@router.get("/lang/{code}")
def set_lang(code: str, request: Request):
    resp = RedirectResponse(request.headers.get("referer") or "/", status_code=303)
    if code in LANGS:
        resp.set_cookie("lang", code, max_age=3600 * 24 * 365)
    return resp


# ---- new post ----

@router.get("/", response_class=HTMLResponse)
def new_post_page(request: Request):
    with get_session() as s:
        categories = s.exec(select(Category).order_by(Category.id)).all()
    return render(request, "new_post.html", accounts=all_accounts(), categories=categories, groups=all_groups(),
                  need_login=accounts_needing_login(), dry_run=settings_store.get_bool("dry_run"),
                  timezone=settings_store.get("timezone"), error=request.query_params.get("error"))


@router.post("/upload", response_class=HTMLResponse)
def upload_video(request: Request, video: UploadFile):
    config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in Path(video.filename or "video.mp4").name)
    name = f"{uuid.uuid4().hex[:8]}_{safe}"
    with open(media_path(name), "wb") as f:
        shutil.copyfileobj(video.file, f)
    info, warning = None, None
    try:
        info = probe(media_path(name))
    except FileNotFoundError:
        warning = {"code": "ffprobe_missing"}
    except Exception as e:
        warning = {"code": "probe_failed", "error": str(e)[:200]}
    resp = render(request, "partials/video_uploaded.html", name=name, info=info, warning=warning)
    resp.headers["HX-Trigger"] = "videoUploaded"
    return resp


@router.post("/check-video", response_class=HTMLResponse)
async def check_video_view(request: Request):
    form = await request.form()
    name, accounts = str(form.get("video_name", "")), selected_accounts(form)
    warnings, ready = [], bool(name and accounts)
    if ready:
        try:
            info = probe(media_path(name))
            platforms = load_platforms()
            for key in dict.fromkeys(a.platform for a in accounts):
                warnings += check_video(info, key, platforms[key])
        except FileNotFoundError:
            warnings.append({"code": "ffprobe_missing"})
        except Exception as e:
            warnings.append({"code": "probe_failed", "error": str(e)[:200]})
    return render(request, "partials/video_check.html", warnings=warnings, ready=ready)


@router.post("/texts", response_class=HTMLResponse)
async def texts_view(request: Request):
    """mode=keep: keep texts already edited, fill new platforms by fallback; mode=ai: adapt all with AI."""
    form = await request.form()
    platforms = load_platforms()
    keys = list(dict.fromkeys(a.platform for a in selected_accounts(form)))
    template = template_from_form(form)
    error = None
    if form.get("mode") == "ai":
        texts, error = await adapt_texts(template, str(form.get("category", "")), keys, platforms,
                                         settings_store.all_settings())
    else:
        texts = {k: text_from_form(form, k) or fit_to_rules(template, platforms[k]) for k in keys}
    return render(request, "partials/texts.html", texts=texts, error=error, mode=form.get("mode"))


@router.post("/posts")
async def create_post(request: Request):
    form = await request.form()
    name, accounts = str(form.get("video_name", "")), selected_accounts(form)
    if not name or not media_path(name).exists():
        return RedirectResponse("/?error=post.err_video", status_code=303)
    if not accounts:
        return RedirectResponse("/?error=post.err_accounts", status_code=303)
    run_at = utcnow()
    if form.get("when") == "at":
        try:
            local = datetime.fromisoformat(str(form.get("scheduled_at", "")))
            run_at = local.replace(tzinfo=local_tz()).astimezone(timezone.utc)
        except ValueError:
            return RedirectResponse("/?error=post.err_date", status_code=303)
    texts = {a.platform: text_from_form(form, a.platform) for a in accounts if text_from_form(form, a.platform)}
    posting.create_post(media_path(name), template_from_form(form), accounts,
                        run_at if form.get("when") == "at" else None, form.get("dry_run") == "1",
                        category=str(form.get("category", "")), texts=texts)
    request.app.state.scheduler.schedule(run_at)
    return RedirectResponse("/posts?created=1", status_code=303)


# ---- posts / jobs ----

def posts_with_jobs() -> list[dict]:
    with get_session() as s:
        posts = s.exec(select(Post).order_by(Post.id.desc()).limit(50)).all()
        accounts = {a.id: a for a in s.exec(select(Account)).all()}
        result = []
        for p in posts:
            jobs = s.exec(select(Job).where(Job.post_id == p.id).order_by(Job.id)).all()
            result.append({"post": p, "jobs": [(j, accounts.get(j.account_id)) for j in jobs]})
    return result


@router.get("/posts", response_class=HTMLResponse)
def posts_page(request: Request):
    return render(request, "posts.html", items=posts_with_jobs(), created=request.query_params.get("created"))


@router.get("/posts/table", response_class=HTMLResponse)
def posts_table(request: Request):
    return render(request, "partials/posts_table.html", items=posts_with_jobs())


@router.post("/jobs/{job_id}/retry", response_class=HTMLResponse)
def retry(job_id: int, request: Request):
    if worker.retry_job(job_id):
        request.app.state.worker.wake()
    return posts_table(request)


@router.post("/jobs/{job_id}/cancel", response_class=HTMLResponse)
def cancel(job_id: int, request: Request):
    if worker.cancel_job(job_id):
        request.app.state.worker.abort(job_id)
    return posts_table(request)


@router.post("/jobs/{job_id}/publish", response_class=HTMLResponse)
def publish_real(job_id: int, request: Request):
    if worker.publish_for_real(job_id):
        request.app.state.worker.wake()
    return posts_table(request)


@router.post("/posts/{post_id}/publish", response_class=HTMLResponse)
def publish_post_real(post_id: int, request: Request):
    with get_session() as s:
        ids = s.exec(select(Job.id).where(Job.post_id == post_id, Job.status == "dry_run")).all()
    if [i for i in ids if worker.publish_for_real(i)]:
        request.app.state.worker.wake()
    return posts_table(request)


@router.post("/posts/{post_id}/delete", response_class=HTMLResponse)
def delete_post(post_id: int, request: Request):
    worker.delete_post(post_id)
    return posts_table(request)


def load_job(job_id: int):
    with get_session() as s:
        job = s.get(Job, job_id)
        return (job, s.get(Account, job.account_id)) if job else (None, None)


@router.get("/jobs/{job_id}/edit", response_class=HTMLResponse)
def edit_job_page(job_id: int, request: Request):
    job, account = load_job(job_id)
    if not job:
        return RedirectResponse("/posts", status_code=303)
    return render(request, "job_edit.html", job=job, account=account, text=PlatformText.from_json(job.text_json),
                  run_at_local=job.run_at.astimezone(local_tz()).strftime("%Y-%m-%dT%H:%M"),
                  timezone=settings_store.get("timezone"), error=request.query_params.get("error"))


@router.post("/jobs/{job_id}/edit")
async def edit_job(job_id: int, request: Request):
    form = await request.form()
    try:
        local = datetime.fromisoformat(str(form.get("run_at", "")))
    except ValueError:
        return RedirectResponse(f"/jobs/{job_id}/edit?error=post.err_date", status_code=303)
    run_at = local.replace(tzinfo=local_tz()).astimezone(timezone.utc)
    text = PlatformText(title=str(form.get("title", "")), description=str(form.get("description", "")),
                        hashtags=parse_hashtags(str(form.get("hashtags", ""))))
    if worker.update_job(job_id, text, run_at):
        request.app.state.scheduler.schedule(run_at)
    return RedirectResponse("/posts", status_code=303)


@router.get("/debug/{job_id}/{name}")
def debug_file(job_id: int, name: str):
    path = config.DEBUG_DIR / str(job_id) / name
    if name not in DEBUG_FILES or not path.exists():
        return HTMLResponse("not found", status_code=404)
    media = {"png": "image/png", "html": "text/plain; charset=utf-8", "log": "text/plain; charset=utf-8"}
    return FileResponse(path, media_type=media[name.rsplit(".", 1)[1]])


# ---- accounts ----

def accounts_ctx() -> dict:
    accounts = all_accounts()
    return {
        "accounts": accounts,
        "login_state": worker.login_state,
        "check_results": check_results,
        "session_exists": {a.id: browser.session_file(a).exists() for a in accounts},
        "polling": any(worker.login_state.get(a.id) in ("queued", "waiting") for a in accounts),
        "mask": config.mask_secret,
    }


@router.get("/accounts", response_class=HTMLResponse)
def accounts_page(request: Request):
    later = [k for k in load_platforms() if k not in implemented_platforms()]
    return render(request, "accounts.html", implemented=implemented_platforms(), later=later, groups=all_groups(),
                  need_login=accounts_needing_login(), error=request.query_params.get("error"), **accounts_ctx())


@router.get("/accounts/table", response_class=HTMLResponse)
def accounts_table(request: Request):
    return render(request, "partials/accounts_table.html", **accounts_ctx())


@router.post("/accounts")
async def add_account(request: Request):
    form = await request.form()
    platform, name = str(form.get("platform", "")), str(form.get("name", "")).strip()
    if platform not in implemented_platforms() or not name:
        return RedirectResponse("/accounts?error=acc.err_name", status_code=303)
    cfg = {}
    if platform == "telegram":
        cfg = {"bot_token": str(form.get("bot_token", "")).strip(), "chat_id": str(form.get("chat_id", "")).strip()}
        if not all(cfg.values()):
            return RedirectResponse("/accounts?error=acc.err_telegram", status_code=303)
    with get_session() as s:
        acc = Account(platform=platform, name=name, slug=unique_slug(platform, name), config=json.dumps(cfg))
        s.add(acc)
        s.commit()
    if platform == "telegram":
        await run_check(acc.id)
    return RedirectResponse("/accounts", status_code=303)


@router.get("/accounts/{account_id}/edit", response_class=HTMLResponse)
def edit_account_page(account_id: int, request: Request):
    with get_session() as s:
        acc = s.get(Account, account_id)
    if not acc:
        return RedirectResponse("/accounts", status_code=303)
    return render(request, "account_edit.html", account=acc, mask=config.mask_secret)


@router.post("/accounts/{account_id}/edit")
async def edit_account(account_id: int, request: Request):
    form = await request.form()
    with get_session() as s:
        acc = s.get(Account, account_id)
        if acc:
            acc.name = str(form.get("name", "")).strip() or acc.name  # slug (profile folder) stays the same
            acc.signature = str(form.get("signature", "")).strip()
            if acc.platform == "telegram":
                cfg = acc.cfg
                if str(form.get("bot_token", "")).strip():
                    cfg["bot_token"] = str(form.get("bot_token")).strip()
                cfg["chat_id"] = str(form.get("chat_id", "")).strip() or cfg.get("chat_id", "")
                acc.config = json.dumps(cfg)
            s.add(acc)
            s.commit()
    return RedirectResponse("/accounts", status_code=303)


@router.post("/accounts/{account_id}/test", response_class=HTMLResponse)
def test_account(account_id: int, request: Request):
    """Dry-run with the bundled 3-second test video: quick check that the platform steps still work."""
    with get_session() as s:
        acc = s.get(Account, account_id)
    if acc:
        posting.create_post(config.ASSETS_DIR / "test_video.mp4",
                            PlatformText("Web-API-Poster test", "Test video (dry-run)", ["test"]),
                            [acc], None, True, category="test")
        request.app.state.worker.wake()
    return HTMLResponse("", headers={"HX-Redirect": "/posts"})


# ---- account groups ----

@router.post("/groups")
async def add_group(request: Request):
    form = await request.form()
    name = str(form.get("name", "")).strip()
    ids = sorted({int(i) for i in form.getlist("account_ids") if str(i).isdigit()})
    if name and ids:
        with get_session() as s:
            taken = {g.slug for g in s.exec(select(AccountGroup)).all()}
            slug, n = config.slugify(name), 2
            while slug in taken:
                slug, n = f"{config.slugify(name)}-{n}", n + 1
            s.add(AccountGroup(name=name, slug=slug, account_ids=json.dumps(ids),
                               slots=str(form.get("slots", "")).strip()))
            s.commit()
    return RedirectResponse("/accounts", status_code=303)


@router.post("/groups/{group_id}/slots")
def set_group_slots(group_id: int, request: Request):
    with get_session() as s:
        group = s.get(AccountGroup, group_id)
        if group:
            group.slots = (request.headers.get("HX-Prompt") or "").strip()
            s.add(group)
            s.commit()
    return HTMLResponse("", headers={"HX-Redirect": "/accounts"})


@router.post("/groups/{group_id}/delete")
def delete_group(group_id: int):
    with get_session() as s:
        group = s.get(AccountGroup, group_id)
        if group:
            s.delete(group)
            s.commit()
    return HTMLResponse("", headers={"HX-Redirect": "/accounts"})


@router.post("/accounts/{account_id}/delete", response_class=HTMLResponse)
def delete_account(account_id: int, request: Request):
    with get_session() as s:
        acc = s.get(Account, account_id)
        if acc:
            for job in s.exec(select(Job).where(Job.account_id == account_id)).all():
                s.delete(job)
            s.delete(acc)
            s.commit()
            shutil.rmtree(browser.profile_dir(acc), ignore_errors=True)
            browser.session_file(acc).unlink(missing_ok=True)
    worker.login_state.pop(account_id, None)
    check_results.pop(account_id, None)
    return accounts_table(request)


@router.post("/accounts/{account_id}/login", response_class=HTMLResponse)
def login(account_id: int, request: Request):
    if worker.login_state.get(account_id) not in ("queued", "waiting"):
        worker.login_state[account_id] = "queued"
        browser.runner.submit(worker.login_account(account_id))
    return accounts_table(request)


async def run_check(account_id: int) -> None:
    try:
        future = browser.runner.submit(worker.check_login(account_id))
        ok = await asyncio.wait_for(asyncio.wrap_future(future), timeout=180)
        check_results[account_id] = "ok" if ok else "fail"
    except Exception as e:
        check_results[account_id] = f"{type(e).__name__}: {e}"[:200]


@router.post("/accounts/{account_id}/check", response_class=HTMLResponse)
async def check(account_id: int, request: Request):
    await run_check(account_id)
    return accounts_table(request)


# ---- settings ----

@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    with get_session() as s:
        categories = s.exec(select(Category).order_by(Category.id)).all()
    return render(request, "settings.html", s=settings_store.all_settings(), categories=categories,
                  mask=config.mask_secret, saved=request.query_params.get("saved"),
                  error=request.query_params.get("error"))


@router.post("/settings")
async def save_settings(request: Request):
    form = await request.form()
    try:
        ZoneInfo(str(form.get("timezone", "")))
    except Exception:
        return RedirectResponse("/settings?error=set.err_timezone", status_code=303)
    for key in settings_store.DEFAULTS:
        if key in ("headless", "dry_run"):
            settings_store.set_value(key, "1" if form.get(key) == "1" else "0")
        elif key in settings_store.SECRET_KEYS:
            if form.get(f"clear_{key}") == "1":
                settings_store.set_value(key, "")
            elif str(form.get(key, "")).strip():
                settings_store.set_value(key, str(form.get(key)).strip())
        elif key in form:
            settings_store.set_value(key, str(form.get(key)).strip())
    return RedirectResponse("/settings?saved=1", status_code=303)


@router.post("/categories")
async def add_category(request: Request):
    name = str((await request.form()).get("name", "")).strip()
    if name:
        with get_session() as s:
            s.add(Category(name=name))
            s.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/categories/{category_id}/delete")
def delete_category(category_id: int):
    with get_session() as s:
        cat = s.get(Category, category_id)
        if cat:
            s.delete(cat)
            s.commit()
    return RedirectResponse("/settings", status_code=303)
