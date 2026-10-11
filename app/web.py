"""Web UI routes (server-rendered Jinja2 + HTMX)."""
import asyncio
import calendar
import html
import io
import json
import shutil
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from sqlmodel import select

from app import assistant, automation, browser, config, db, docs, mixer, posting, settings_store, videogen, worker
from app.db import get_session
from app.i18n import DEFAULT_LANG, LANGS, translate
from app.models import Account, AccountGroup, Category, Generation, Job, Post, utcnow
from app.notify import message, notify
from app.posting import local_tz
from app.platforms import load_platforms, platform_name
from app.texts import PlatformText, adapt_texts, fit_to_rules, parse_hashtags
from app.video import check_video, extract_frame, probe

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


THEMES = ("auto", "dark", "light")


def render(request: Request, template: str, status_code: int = 200, **ctx) -> HTMLResponse:
    lang = get_lang(request)
    theme = request.cookies.get("theme")
    ctx.setdefault("has_update", request.cookies.get("seen_version") != config.APP_VERSION)
    latest = settings_store.get("latest_version")  # found on GitHub by automation.check_update()
    ctx.update(
        new_version=latest if automation.is_newer(latest) else "",
        download_url=automation.download_url(),
        lang=lang,
        theme=theme if theme in THEMES else "auto",
        app_version=config.APP_VERSION,
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


@router.get("/theme/{mode}")
def set_theme(mode: str, request: Request):
    resp = RedirectResponse(request.headers.get("referer") or "/", status_code=303)
    if mode in THEMES:
        resp.set_cookie("theme", mode, max_age=3600 * 24 * 365)
    return resp


# ---- instruction and changelog (docs/*.md) ----

@router.get("/guide", response_class=HTMLResponse)
def guide_page(request: Request):
    body, contents = docs.render_markdown(docs.load_doc("guide", get_lang(request)))
    return render(request, "doc.html", body=body, contents=contents, page="guide")


@router.get("/updates", response_class=HTMLResponse)
def updates_page(request: Request):
    body, _ = docs.render_markdown(docs.load_doc("updates", get_lang(request)))
    resp = render(request, "doc.html", body=body, contents=[], page="updates", has_update=False)
    resp.set_cookie("seen_version", config.APP_VERSION, max_age=3600 * 24 * 365 * 5)
    return resp


# ---- new post ----

def probe_card(name: str) -> dict:
    """Context for partials/video_uploaded.html."""
    info, warning = None, None
    try:
        info = probe(media_path(name))
    except FileNotFoundError:
        warning = {"code": "ffprobe_missing"}
    except Exception as e:
        warning = {"code": "probe_failed", "error": str(e)[:200]}
    return {"name": name, "info": info, "warning": warning}


@router.get("/", response_class=HTMLResponse)
def new_post_page(request: Request):
    with get_session() as s:
        categories = s.exec(select(Category).order_by(Category.id)).all()
    video = request.query_params.get("video", "")  # a video sent from the "Відео" or "Мікс" page
    prefill = probe_card(video) if video and media_path(video).exists() else None
    mix_id = request.query_params.get("mix", "")
    mix = mixer.get_mix(int(mix_id)) if mix_id.isdigit() else None
    return render(request, "new_post.html", accounts=all_accounts(), categories=categories, groups=all_groups(),
                  need_login=accounts_needing_login(), dry_run=settings_store.get_bool("dry_run"),
                  timezone=settings_store.get("timezone"), error=request.query_params.get("error"),
                  prefill=prefill, prefill_title=mix.title if mix else "",
                  prefill_description=mix.tracklist if mix else "")


@router.post("/upload", response_class=HTMLResponse)
def upload_video(request: Request, video: UploadFile):
    config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    name = posting.media_name(video.filename or "video.mp4")
    with open(media_path(name), "wb") as f:
        shutil.copyfileobj(video.file, f)
    resp = render(request, "partials/video_uploaded.html", **probe_card(name))
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


def thumbnail_from_form(form, video: Path) -> str:
    """Cover for YouTube: an uploaded JPG / PNG, or a frame of the video at the given second ("" = none)."""
    upload = form.get("thumbnail")
    if getattr(upload, "filename", ""):
        if Path(upload.filename).suffix.lower() not in (".jpg", ".jpeg", ".png"):
            raise ValueError("JPG or PNG only")
        return str(save_upload(upload, config.MEDIA_DIR / f"thumb_{posting.media_name(upload.filename)}"))
    second = str(form.get("thumb_second", "")).strip().replace(",", ".")
    if not second:
        return ""
    dest = config.MEDIA_DIR / f"thumb_{posting.media_name(video.stem + '.jpg')}"
    return str(extract_frame(video, float(second), dest))


@router.post("/posts")
async def create_post(request: Request):
    form = await request.form()
    name, accounts = str(form.get("video_name", "")), selected_accounts(form)
    if not name or not media_path(name).exists():
        return RedirectResponse("/?error=post.err_video", status_code=303)
    if not accounts:
        return RedirectResponse("/?error=post.err_accounts", status_code=303)
    group = None
    group_id = str(form.get("slot_group") if form.get("when") == "slot" else form.get("group_id", ""))
    if group_id.isdigit():
        with get_session() as s:
            group = s.get(AccountGroup, int(group_id))
    run_at, scheduled = utcnow(), None
    if form.get("when") == "at":
        try:
            local = datetime.fromisoformat(str(form.get("scheduled_at", "")))
            run_at = scheduled = local.replace(tzinfo=local_tz()).astimezone(timezone.utc)
        except ValueError:
            return RedirectResponse("/?error=post.err_date", status_code=303)
    elif form.get("when") == "slot":
        slot = posting.next_free_slot(group) if group else None
        if slot is None:
            return RedirectResponse("/?error=post.err_no_slots", status_code=303)
        run_at = scheduled = slot
    try:
        thumbnail = thumbnail_from_form(form, media_path(name))
    except (ValueError, FileNotFoundError):
        return RedirectResponse("/?error=post.err_thumbnail", status_code=303)
    texts = {a.platform: text_from_form(form, a.platform) for a in accounts if text_from_form(form, a.platform)}
    posting.create_post(media_path(name), template_from_form(form), accounts, scheduled, form.get("dry_run") == "1",
                        category=str(form.get("category", "")), texts=texts, group_id=group.id if group else None,
                        thumbnail=thumbnail)
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


# ---- schedule ----

STATUS_ORDER = ("failed", "needs_action", "running", "pending", "published", "dry_run")


def month_calendar(year: int, month: int, lang: str) -> list[list[tuple[date, list[dict]]]]:
    """Weeks (Monday first) of the month; each day holds its publications (one entry per post and time)."""
    tz = local_tz()
    weeks = calendar.Calendar().monthdatescalendar(year, month)
    start = datetime.combine(weeks[0][0], datetime.min.time(), tz).astimezone(timezone.utc)
    end = datetime.combine(weeks[-1][-1] + timedelta(days=1), datetime.min.time(), tz).astimezone(timezone.utc)
    with get_session() as s:
        jobs = s.exec(select(Job).where(Job.run_at >= start, Job.run_at < end, Job.status != "cancelled")
                      .order_by(Job.run_at, Job.id)).all()
        posts = {p.id: p for p in s.exec(select(Post).where(Post.id.in_({j.post_id for j in jobs}))).all()}
        accounts = {a.id: a for a in s.exec(select(Account)).all()}
    entries: dict[tuple, dict] = {}
    for job in jobs:
        local = job.run_at.astimezone(tz)
        entry = entries.setdefault((local.date(), local.strftime("%H:%M"), job.post_id), {
            "time": local.strftime("%H:%M"), "post": posts.get(job.post_id), "jobs": []})
        entry["jobs"].append((job, accounts.get(job.account_id)))
    days: dict[date, list[dict]] = {}
    for (day, _, _), entry in entries.items():
        statuses = {j.status for j, _ in entry["jobs"]}
        entry["status"] = next(s for s in STATUS_ORDER if s in statuses)
        entry["tooltip"] = "\n".join(f"{platform_name(a.platform) if a else '?'} / {a.name if a else '?'}: "
                                     f"{translate(lang, 'status.' + j.status)}" for j, a in entry["jobs"])
        entry["edit"] = next((j.id for j, _ in entry["jobs"] if j.status == "pending"), None)
        days.setdefault(day, []).append(entry)
    return [[(day, days.get(day, [])) for day in week] for week in weeks]


@router.get("/schedule", response_class=HTMLResponse)
def schedule_page(request: Request):
    """Month calendar of all publications and the list of upcoming jobs grouped by local date."""
    today = datetime.now(local_tz()).date()
    try:
        year, month = (int(x) for x in request.query_params.get("month", "").split("-"))
        date(year, month, 1)
    except ValueError:
        year, month = today.year, today.month
    with get_session() as s:
        jobs = s.exec(select(Job).where(Job.status.in_(["pending", "running", "needs_action"]))
                      .order_by(Job.run_at, Job.id)).all()
        posts = {p.id: p for p in s.exec(select(Post).where(Post.id.in_({j.post_id for j in jobs}))).all()}
        accounts = {a.id: a for a in s.exec(select(Account)).all()}
    days: dict[str, list] = {}
    for job in jobs:
        day = job.run_at.astimezone(local_tz()).strftime("%d.%m.%Y")
        days.setdefault(day, []).append((job, posts.get(job.post_id), accounts.get(job.account_id)))
    prev_month = date(year, month, 1) - timedelta(days=1)
    next_month = date(year, month, 28) + timedelta(days=4)
    return render(request, "schedule.html", days=days, groups=all_groups(), today=today, year=year, month=month,
                  weeks=month_calendar(year, month, get_lang(request)),
                  prev_month=f"{prev_month:%Y-%m}", next_month=f"{next_month:%Y-%m}")


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
        "proxy_label": browser.proxy_label,
    }


@router.get("/accounts", response_class=HTMLResponse)
def accounts_page(request: Request):
    later = [k for k in load_platforms() if k not in implemented_platforms()]
    groups = all_groups()
    return render(request, "accounts.html", implemented=implemented_platforms(), later=later, groups=groups,
                  inbox_paths={g.id: str(automation.inbox_dir() / g.slug) for g in groups},
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
    return render(request, "account_edit.html", account=acc, mask=config.mask_secret, proxy_label=browser.proxy_label,
                  error=request.query_params.get("error"))


@router.post("/accounts/{account_id}/edit")
async def edit_account(account_id: int, request: Request):
    form = await request.form()
    try:
        browser.proxy_settings(str(form.get("proxy", "")))
    except ValueError:  # e.g. a port that is not a number
        return RedirectResponse(f"/accounts/{account_id}/edit?error=acc.err_proxy", status_code=303)
    with get_session() as s:
        acc = s.get(Account, account_id)
        if acc:
            acc.name = str(form.get("name", "")).strip() or acc.name  # slug (profile folder) stays the same
            acc.signature = str(form.get("signature", "")).strip()
            cfg = acc.cfg
            if acc.platform == "telegram":
                if str(form.get("bot_token", "")).strip():
                    cfg["bot_token"] = str(form.get("bot_token")).strip()
                cfg["chat_id"] = str(form.get("chat_id", "")).strip() or cfg.get("chat_id", "")
            elif form.get("clear_proxy") == "1":
                cfg.pop("proxy", None)
            elif str(form.get("proxy", "")).strip():  # empty field keeps the saved proxy (it may hold a password)
                cfg["proxy"] = str(form.get("proxy")).strip()
            acc.config = json.dumps(cfg)
            s.add(acc)
            s.commit()
    return RedirectResponse("/accounts", status_code=303)


def create_test_post(accounts: list[Account], request: Request) -> HTMLResponse:
    """Dry-run with the bundled 3-second test video: quick check that the platform steps still work."""
    if accounts:
        posting.create_post(config.ASSETS_DIR / "test_video.mp4",
                            PlatformText("Web-API-Poster test", "Test video (dry-run)", ["test"]),
                            accounts, None, True, category="test")
        request.app.state.worker.wake()
    return HTMLResponse("", headers={"HX-Redirect": "/posts"})


@router.post("/accounts/test-all", response_class=HTMLResponse)
def test_all_accounts(request: Request):
    return create_test_post(all_accounts(), request)


@router.post("/accounts/{account_id}/test", response_class=HTMLResponse)
def test_account(account_id: int, request: Request):
    with get_session() as s:
        acc = s.get(Account, account_id)
    return create_test_post([acc] if acc else [], request)


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
    workflow = videogen.load_workflow()
    workflow_info = {"nodes": len(workflow), "has_prompt": "{{PROMPT}}" in json.dumps(workflow),
                     "needs_image": videogen.workflow_needs_image(workflow)} if workflow else None
    return render(request, "settings.html", s=settings_store.all_settings(), categories=categories,
                  mask=config.mask_secret, saved=request.query_params.get("saved"),
                  error=request.query_params.get("error"), workflow_info=workflow_info)


@router.post("/settings")
async def save_settings(request: Request):
    form = await request.form()
    try:
        ZoneInfo(str(form.get("timezone", "")))
    except Exception:
        return RedirectResponse("/settings?error=set.err_timezone", status_code=303)
    for key in settings_store.DEFAULTS:
        if key in settings_store.BOOL_KEYS:
            settings_store.set_value(key, "1" if form.get(key) == "1" else "0")
        elif key in settings_store.SECRET_KEYS:
            if form.get(f"clear_{key}") == "1":
                settings_store.set_value(key, "")
            elif str(form.get(key, "")).strip():
                settings_store.set_value(key, str(form.get(key)).strip())
        elif key in form:
            settings_store.set_value(key, str(form.get(key)).strip())
    request.app.state.start_automation()  # login check time may have changed
    return RedirectResponse("/settings?saved=1", status_code=303)


@router.post("/settings/notify-test", response_class=HTMLResponse)
async def notify_test(request: Request):
    error = await notify(message("notify.test"))
    lang = get_lang(request)
    if error is None:
        return HTMLResponse(f'<span class="ok">{translate(lang, "set.notify_sent")}</span>')
    text = translate(lang, "set.notify_not_configured") if error == "not_configured" else error
    return HTMLResponse(f'<span class="error">{html.escape(text)}</span>')


@router.post("/settings/update-check", response_class=HTMLResponse)
async def update_check(request: Request):
    lang = get_lang(request)
    try:
        version = await automation.check_update()
    except Exception as e:
        return HTMLResponse(f'<span class="error">{html.escape(translate(lang, "upd.fail", error=str(e)[:200]))}</span>')
    if automation.is_newer(version):
        text = html.escape(translate(lang, "upd.available", version=version))
        return HTMLResponse(f'<span class="ok">{text} <a href="/updates">{translate(lang, "upd.how")}</a></span>')
    return HTMLResponse(f'<span class="ok">{html.escape(translate(lang, "upd.latest", version=config.APP_VERSION))}</span>')


@router.get("/settings/backup")
def backup():
    """Database, session files and the ComfyUI workflow in one zip; restore = unzip into the data folder."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("app.db", db.snapshot())
        for folder in (config.SESSIONS_DIR, config.COMFY_WORKFLOW.parent):
            for f in sorted(folder.rglob("*")) if folder.exists() else []:
                if f.is_file():
                    z.write(f, f.relative_to(config.DATA_DIR).as_posix())
    name = f"web-api-poster-backup-{datetime.now(local_tz()):%Y-%m-%d}.zip"
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


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


# ---- video generator (local model in ComfyUI) ----

GEN_ERRORS = {"no_workflow", "needs_image", "no_prompt_field", "comfy_offline"}


def generations() -> list[Generation]:
    with get_session() as s:
        return list(s.exec(select(Generation).order_by(Generation.id.desc()).limit(30)).all())


def videos_ctx() -> dict:
    gens = generations()
    return {"generations": gens, "gen_errors": GEN_ERRORS,
            "gen_active": any(g.status in ("queued", "running") for g in gens)}


@router.get("/videos", response_class=HTMLResponse)
def videos_page(request: Request):
    return render(request, "videos.html", workflow=videogen.load_workflow(), comfy_url=settings_store.get("comfy_url"),
                  error=request.query_params.get("error"), **videos_ctx())


@router.get("/videos/list", response_class=HTMLResponse)
def videos_list(request: Request):
    return render(request, "partials/generations.html", **videos_ctx())


@router.post("/videos")
async def start_video(request: Request):
    form = await request.form()
    prompt = str(form.get("prompt", "")).strip()
    if not prompt:
        return RedirectResponse("/videos?error=gen.err_prompt", status_code=303)
    image_path = None
    image = form.get("image")
    if getattr(image, "filename", ""):
        config.GENERATIONS_DIR.mkdir(parents=True, exist_ok=True)
        image_path = config.GENERATIONS_DIR / f"frame_{posting.media_name(image.filename)}"
        with open(image_path, "wb") as f:
            shutil.copyfileobj(image.file, f)
    videogen.create_generation(prompt, str(form.get("negative", "")), str(image_path) if image_path else None)
    return RedirectResponse("/videos", status_code=303)


@router.post("/generations/{gen_id}/retry", response_class=HTMLResponse)
def retry_generation(gen_id: int, request: Request):
    videogen.retry_generation(gen_id)
    return videos_list(request)


@router.post("/generations/{gen_id}/cancel", response_class=HTMLResponse)
async def cancel_generation(gen_id: int, request: Request):
    await videogen.cancel_generation(gen_id)
    return videos_list(request)


@router.post("/generations/{gen_id}/delete", response_class=HTMLResponse)
def delete_generation(gen_id: int, request: Request):
    videogen.delete_generation(gen_id)
    return videos_list(request)


@router.get("/generations/{gen_id}/video")
def generation_video(gen_id: int):
    gen = videogen.get_generation(gen_id)
    if not gen or not gen.video_path or Path(gen.video_path).parent != config.GENERATIONS_DIR \
            or not Path(gen.video_path).exists():
        return HTMLResponse("not found", status_code=404)
    return FileResponse(gen.video_path)


@router.post("/generations/{gen_id}/post")
def generation_to_post(gen_id: int):
    """Copy the generated video to data/media and open "Нова публікація" with it."""
    gen = videogen.get_generation(gen_id)
    if not gen or not gen.video_path or not Path(gen.video_path).exists():
        return HTMLResponse("", headers={"HX-Redirect": "/videos"})
    name = posting.media_name(Path(gen.video_path).name)
    config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy(gen.video_path, media_path(name))
    return HTMLResponse("", headers={"HX-Redirect": f"/?video={name}"})


# ---- mix studio: a clip looped under a music mix ----

MIX_ERRORS = {"no_ffmpeg"}


def mixes_ctx() -> dict:
    mixes = mixer.all_mixes()
    return {"mixes": mixes, "mix_errors": MIX_ERRORS,
            "mix_active": any(m.status in ("queued", "running") for m in mixes),
            "mix_total": lambda m: mixer.fmt_time(mixer.build_tracklist(m.track_list, m.crossfade)[1], True)}


@router.get("/mix", response_class=HTMLResponse)
def mix_page(request: Request):
    clips = [g for g in generations() if g.status == "done" and g.video_path and Path(g.video_path).exists()]
    return render(request, "mix.html", clips=clips, selected=request.query_params.get("generation", ""),
                  error=request.query_params.get("error"), **mixes_ctx())


@router.get("/mix/list", response_class=HTMLResponse)
def mix_list(request: Request):
    return render(request, "partials/mixes.html", **mixes_ctx())


def save_upload(upload, path: Path) -> Path:
    with open(path, "wb") as f:
        shutil.copyfileobj(upload.file, f)
    return path


@router.post("/mix")
async def start_mix(request: Request):
    form = await request.form()
    tracks = sorted((f for f in form.getlist("tracks") if getattr(f, "filename", "")
                     and Path(f.filename).suffix.lower() in mixer.AUDIO_EXTENSIONS), key=lambda f: f.filename.lower())
    clip_file, gen_id = form.get("clip"), str(form.get("generation_id", ""))
    gen = videogen.get_generation(int(gen_id)) if gen_id.isdigit() else None
    gen_video = Path(gen.video_path) if gen and gen.video_path and Path(gen.video_path).exists() else None
    if not tracks:
        return RedirectResponse("/mix?error=mix.err_tracks", status_code=303)
    if not getattr(clip_file, "filename", "") and not gen_video:
        return RedirectResponse("/mix?error=mix.err_clip", status_code=303)
    folder = mixer.new_folder()
    if getattr(clip_file, "filename", ""):
        clip = save_upload(clip_file, folder / f"clip{Path(clip_file.filename).suffix.lower() or '.mp4'}")
    else:
        clip = folder / f"clip{gen_video.suffix.lower()}"
        shutil.copy(gen_video, clip)
    saved = [(f.filename, save_upload(f, folder / f"{i:02d}{Path(f.filename).suffix.lower()}"))
             for i, f in enumerate(tracks, 1)]
    try:
        crossfade = min(30.0, max(0.0, float(form.get("crossfade") or 0)))
    except ValueError:
        crossfade = 0.0
    try:
        mixer.create_mix(str(form.get("title", "")), clip, saved, crossfade)
    except Exception as e:
        shutil.rmtree(folder, ignore_errors=True)
        code = "mix.err_ffmpeg" if isinstance(e, FileNotFoundError) else "mix.err_audio"
        return RedirectResponse(f"/mix?error={code}", status_code=303)
    mixer.worker.wake()
    return RedirectResponse("/mix", status_code=303)


@router.post("/mixes/{mix_id}/cancel", response_class=HTMLResponse)
def cancel_mix(mix_id: int, request: Request):
    mixer.worker.cancel(mix_id)
    return mix_list(request)


@router.post("/mixes/{mix_id}/retry", response_class=HTMLResponse)
def retry_mix(mix_id: int, request: Request):
    if mixer.retry_mix(mix_id):
        mixer.worker.wake()
    return mix_list(request)


@router.post("/mixes/{mix_id}/delete", response_class=HTMLResponse)
def delete_mix(mix_id: int, request: Request):
    mixer.delete_mix(mix_id)
    return mix_list(request)


def finished_mix(mix_id: int):
    mix = mixer.get_mix(mix_id)
    if mix and mix.video_path and mixer.folder_of(mix) and Path(mix.video_path).exists():
        return mix
    return None


@router.get("/mixes/{mix_id}/video")
def mix_video(mix_id: int):
    mix = finished_mix(mix_id)
    return FileResponse(mix.video_path) if mix else HTMLResponse("not found", status_code=404)


@router.post("/mixes/{mix_id}/post")
def mix_to_post(mix_id: int):
    """Open "Нова публікація" with the mix video, its title and the tracklist as the description."""
    mix = finished_mix(mix_id)
    if not mix:
        return HTMLResponse("", headers={"HX-Redirect": "/mix"})
    return HTMLResponse("", headers={"HX-Redirect": f"/?video={mixer.to_media(mix)}&mix={mix.id}"})


@router.post("/settings/workflow")
async def upload_workflow(request: Request):
    form = await request.form()
    file = form.get("workflow")
    try:
        workflow = videogen.parse_workflow((await file.read()).decode("utf-8-sig"))
    except Exception:
        return RedirectResponse("/settings?error=set.err_workflow", status_code=303)
    config.COMFY_WORKFLOW.parent.mkdir(parents=True, exist_ok=True)
    config.COMFY_WORKFLOW.write_text(json.dumps(workflow, ensure_ascii=False, indent=1), encoding="utf-8")
    return RedirectResponse("/settings?saved=1", status_code=303)


@router.post("/settings/comfy-test", response_class=HTMLResponse)
async def comfy_test(request: Request):
    lang = get_lang(request)
    try:
        stats = await videogen.client().system_stats()
    except Exception as e:
        return HTMLResponse(f'<span class="error">{html.escape(translate(lang, "set.comfy_fail", error=str(e)[:200]))}</span>')
    devices = ", ".join(d.get("name", "") for d in stats.get("devices", [])) or "?"
    return HTMLResponse(f'<span class="ok">{html.escape(translate(lang, "set.comfy_ok", devices=devices))}</span>')


# ---- assistant ----

def chat_format(text: str) -> Markup:
    """Escape the text; ``` blocks become copyable <pre>, **bold** becomes <b>."""
    parts = str(text or "").split("```")
    out = []
    for i, part in enumerate(parts):
        if i % 2:  # inside a code block: drop the language line
            body = part.split("\n", 1)[1] if "\n" in part and part.split("\n", 1)[0].strip().isalpha() else part
            out.append(f'<pre class="prompt">{escape(body.strip())}</pre>')
        else:
            chunks = str(escape(part)).split("**")
            out.append("".join(f"<b>{c}</b>" if j % 2 else c for j, c in enumerate(chunks)))
    return Markup("".join(out))


templates.env.filters["chat_format"] = chat_format


def chat_ctx() -> dict:
    return {"messages": assistant.all_messages(), "actions": assistant.actions_by_id(),
            "assistant_ready": assistant.configured()}


@router.get("/assistant", response_class=HTMLResponse)
def assistant_page(request: Request):
    return render(request, "assistant.html", **chat_ctx())


@router.get("/assistant/chat", response_class=HTMLResponse)
def assistant_chat(request: Request):
    return render(request, "partials/chat.html", **chat_ctx())


@router.post("/assistant/send", response_class=HTMLResponse)
async def assistant_send(request: Request):
    text = str((await request.form()).get("text", "")).strip()
    if text:
        assistant.add_message("user", text)
        await assistant.reply()
    return assistant_chat(request)


@router.post("/assistant/actions/{action_id}/{decision}", response_class=HTMLResponse)
async def assistant_action(action_id: int, decision: str, request: Request):
    run_at = assistant.resolve_action(action_id, confirm=decision == "confirm")
    if run_at is not None:
        request.app.state.scheduler.schedule(run_at)
    if decision == "confirm" and assistant.configured():
        await assistant.reply()  # short comment on what happened
    return assistant_chat(request)


@router.post("/assistant/clear", response_class=HTMLResponse)
def assistant_clear(request: Request):
    assistant.clear()
    return assistant_chat(request)
