from __future__ import annotations

import ipaddress
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path
from secrets import token_urlsafe
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

from argon2 import PasswordHasher
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.datastructures import FormData, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import __version__
from .auth import (
    LoginResult,
    attempt_login,
    hash_password_off_loop,
    verify_password_off_loop,
)
from .config import AppConfig
from .evaluator import past_cutoff
from .overrides import ConfirmationRequired, OverrideError, override_expiry
from .runtime import test_connections
from .secrets import SECRET_NAMES
from .security import constant_equal
from .store import SETUP_LOCK_SUBJECT, ConfigWriteError, LastAdminError, Store, cookie_max_age

LOGGER = logging.getLogger(__name__)

HOST_SESSION_COOKIE = "__Host-swa_session"
PLAIN_SESSION_COOKIE = "swa_session"
_USERNAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_CUTOFF = re.compile(r"\d{2}:\d{2}")
_TEST_TOKENS = {"ok", "skylight_error", "unifi_error", "not_configured"}
_PACKAGE = Path(__file__).resolve().parent


@dataclass
class Visitor:
    username: str
    csrf: str
    token: str
    role: str = "admin"


@dataclass
class AppContext:
    store: Store
    service: object | None
    source: str
    clock: Callable[[], datetime]
    setup_token: str | None
    cookie_secure: bool
    hasher: PasswordHasher
    dummy_hash: str
    tester: Callable[..., Awaitable[str]] | None
    read_only: bool
    trusted_proxies: str = ""
    auth_disabled: bool = False
    anonymous_csrf: str = field(default_factory=lambda: token_urlsafe(32))


ANONYMOUS_USER = "anonymous"
# Account pages have nothing to act on when sign-in is turned off.
_ACCOUNT_PATHS = ("/login", "/setup", "/logout", "/password", "/sessions", "/users")


def _account_path(path: str) -> bool:
    return any(path == item or path.startswith(item + "/") for item in _ACCOUNT_PATHS)


@dataclass
class DashChild:
    child_id: int
    label: str
    object_name: str
    tasks: str
    state: str
    reason: str
    has_override: bool
    override_text: str
    past_cutoff: bool
    expiry_text: str


def is_public(path: str, method: str) -> bool:
    if ".." in path or "\\" in path or "\x00" in path:
        return False
    if method in {"GET", "HEAD"} and path.startswith("/static/"):
        return True
    if path == "/healthz":
        return method in {"GET", "HEAD"}
    if path in {"/login", "/setup"}:
        return method in {"GET", "HEAD", "POST"}
    return False


def current_user(request: Request) -> Visitor | None:
    user = request.scope.get("skylight_user")
    return user if isinstance(user, Visitor) else None


def _stated_origin(request: Request) -> str | None:
    raw = request.headers.get("origin")
    if raw is None:
        return None
    value = raw.strip()
    if value == "" or value.lower() == "null":
        return None
    return value


_FETCH_SITE_VALUES = {"same-origin", "same-site", "cross-site", "none"}
STALE_MESSAGE = "Your session changed, please try again."


def origin_failure(request: Request) -> str | None:
    """Return a rejection check name, or None when the request is same-origin.

    A matching Origin is accepted. When Origin is missing or the literal null
    a browser sends, Sec-Fetch-Site must be same-origin. If that header is
    absent, a same-origin Referer is accepted. Any other Sec-Fetch-Site value
    is rejected, including when the Referer matches. No signal at all is
    rejected. Plain HTTP to a non-loopback host is not a trustworthy URL, so
    current Chrome and Safari omit Sec-Fetch-Site there and send Origin with
    the non-default port. The CSRF token is checked separately and stays mandatory.
    """
    host = request.headers.get("host", "")
    if not host:
        return "no_signal"
    origin = _stated_origin(request)
    if origin is not None:
        parsed = urlsplit(origin)
        if parsed.scheme in {"http", "https"} and parsed.netloc == host:
            return None
        return "origin_mismatch"
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None:
        if fetch_site.strip().lower() == "same-origin":
            return None
        return "fetch_site"
    referer = request.headers.get("referer")
    if not referer:
        return "no_signal"
    parsed = urlsplit(referer)
    if parsed.scheme in {"http", "https"} and parsed.netloc == host:
        return None
    return "origin_mismatch"


def same_origin(request: Request) -> bool:
    return origin_failure(request) is None


def coarse_user_agent(value: str) -> str:
    text = " ".join((value or "").split())
    if "Edg/" in text:
        browser = "Edge"
    elif "Chrome/" in text:
        browser = "Chrome"
    elif "Firefox/" in text:
        browser = "Firefox"
    elif "Safari/" in text:
        browser = "Safari"
    else:
        browser = "Browser"
    if "iPhone" in text or "iPad" in text:
        system = "iOS"
    elif "Android" in text:
        system = "Android"
    elif "Mac OS X" in text or "Macintosh" in text:
        system = "macOS"
    elif "Windows" in text:
        system = "Windows"
    elif "Linux" in text:
        system = "Linux"
    else:
        system = "Unknown"
    return f"{browser} on {system}"


def admin_only(request: Request) -> HTMLResponse | None:
    user = current_user(request)
    if user is not None and user.role == "admin":
        return None
    return render(
        request,
        "blocked.html",
        title="Not allowed",
        message="You do not have access to that page.",
        status_code=403,
    )


def _log_text(value: str) -> str:
    """One log token. Control characters are percent-encoded so they cannot break the line."""
    parts: list[str] = []
    for char in value:
        code = ord(char)
        if code < 32 or code == 127:
            parts.append(f"%{code:02X}")
        else:
            parts.append(char)
    return "".join(parts)


def _log_rejection(request: Request, check: str) -> None:
    raw_fetch = request.headers.get("sec-fetch-site")
    if raw_fetch is None:
        fetch_site = "-"
    else:
        value = raw_fetch.strip().lower()
        fetch_site = value if value in _FETCH_SITE_VALUES else "other"
    LOGGER.warning(
        "rejected state-changing request check=%s method=%s path=%s origin_present=%s sec_fetch_site=%s",
        _log_text(check),
        _log_text(request.method),
        _log_text(request.url.path),
        "yes" if request.headers.get("origin") is not None else "no",
        fetch_site,
    )


def session_cookie_name(secure: bool) -> str:
    if secure:
        return HOST_SESSION_COOKIE
    return PLAIN_SESSION_COOKIE


def _proxy_trusts(host: str, spec: str) -> bool:
    if not spec.strip() or not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    for item in spec.split(","):
        token = item.strip()
        if not token:
            continue
        try:
            if "/" in token:
                if address in ipaddress.ip_network(token, strict=False):
                    return True
            elif address == ipaddress.ip_address(token):
                return True
        except ValueError:
            continue
    return False


def _forwarded_client(header: str, spec: str) -> str | None:
    hops = [part.strip() for part in header.split(",") if part.strip()]
    if not hops:
        return None
    for hop in reversed(hops):
        host = hop.rsplit("%", 1)[0]
        if host.startswith("[") and "]" in host:
            host = host[1 : host.index("]")]
        elif host.count(":") == 1 and "." in host:
            host = host.split(":", 1)[0]
        if not _proxy_trusts(host, spec):
            return host[:64]
    return hops[0][:64]


def client_ip(request: Request) -> str:
    peer = ""
    if request.client is not None and request.client.host:
        peer = request.client.host
    context: AppContext = request.app.state.context
    if peer and _proxy_trusts(peer, context.trusted_proxies):
        forwarded = _forwarded_client(request.headers.get("x-forwarded-for", ""), context.trusted_proxies)
        if forwarded:
            return forwarded
    if not peer:
        return "unknown"
    return peer[:64]


def field_errors(exc: ValidationError) -> list[str]:
    lines: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err["loc"] if part != "__root__")
        message = str(err["msg"])
        lines.append(f"{loc}: {message}" if loc else message)
    return lines or ["The configuration is invalid."]


def password_problem(password: str) -> str | None:
    if len(password) < 12:
        return "Password must be at least 12 characters."
    if len(password) > 128:
        return "Password must be at most 128 characters."
    return None


def username_problem(username: str) -> str | None:
    if _USERNAME.fullmatch(username) is None:
        return "Username must be 1-64 letters, numbers, dots, underscores, or hyphens."
    return None


def clean_test_result(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    parts = [part for part in value.split(",") if part in _TEST_TOKENS]
    if not parts or ("ok" in parts and len(parts) > 1):
        return None
    return ", ".join(parts)


def format_local(moment: datetime, timezone: str) -> str:
    return moment.astimezone(ZoneInfo(timezone)).strftime("%Y-%m-%d %H:%M %Z")


def format_remaining(expires: datetime, now: datetime) -> str:
    seconds = int((expires - now).total_seconds())
    if seconds <= 0:
        return "due on the next poll"
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def render(request: Request, name: str, status_code: int = 200, **extra: object) -> HTMLResponse:
    user = current_user(request)
    csrf = extra.pop("csrf", None)
    if csrf is None and user is not None:
        csrf = user.csrf
    context = {
        "title": extra.pop("title", "Skylight Wi-Fi"),
        "user": user,
        "csrf": csrf or "",
        "read_only": request.app.state.context.read_only,
        "auth_disabled": request.app.state.context.auth_disabled,
        "saved": request.query_params.get("saved") == "1",
        "errors": extra.pop("errors", []),
        "notice": extra.pop("notice", ""),
        "message": extra.pop("message", ""),
        "version": __version__,
    }
    context.update(extra)
    html = request.app.state.templates.get_template(name).render(**context)
    return HTMLResponse(html, status_code=status_code)


def session_cookie(
    response: Response, token: str, secure: bool, max_age: int | None = None
) -> None:
    """Set the session cookie. ``max_age`` None is a browser session cookie, with no Max-Age."""
    name = session_cookie_name(secure)
    if name.startswith("__Host-") and not secure:
        raise RuntimeError("refusing to set a prefixed cookie without Secure")
    response.set_cookie(
        name,
        token,
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite="strict",
        path="/",
    )


def clear_session_cookie(response: Response, secure: bool) -> None:
    response.delete_cookie(
        session_cookie_name(secure),
        path="/",
        secure=secure,
        httponly=True,
        samesite="strict",
    )


def blocked_page(request: Request) -> HTMLResponse:
    return render(
        request,
        "blocked.html",
        title="Request blocked",
        message="Cross-site request blocked.",
        status_code=403,
    )


def _admin_path(path: str) -> bool:
    if path in {"/settings", "/unifi", "/audit", "/users", "/children"}:
        return True
    return path.startswith(("/children/", "/secrets", "/users/"))


async def stale_page(request: Request) -> Response:
    """Re-render the form that was posted, with a fresh token and a notice."""
    context: AppContext = request.app.state.context
    path = request.url.path
    notice = STALE_MESSAGE
    user = current_user(request)
    if _admin_path(path) and (user is None or user.role != "admin"):
        return render(
            request,
            "blocked.html",
            title="Not allowed",
            message="You do not have access to that page.",
            status_code=403,
        )
    if path == "/login":
        return await login_page(request, notice=notice, status_code=403)
    if path == "/setup":
        if await context.store.get_admin() is not None:
            return PlainTextResponse("Not found.", status_code=404)
        if context.setup_token is None or len(context.setup_token) < 32:
            return render(request, "setup_disabled.html", title="Setup", notice=notice, status_code=403)
        token = await context.store.create_form_token(context.clock())
        return render(
            request,
            "setup.html",
            title="Setup",
            csrf=token,
            notice=notice,
            status_code=403,
        )
    if path == "/settings":
        config = await current_config(context)
        return render(
            request,
            "settings.html",
            title="Settings",
            config=config,
            notice=notice,
            status_code=403,
        )
    if path == "/children" or path.startswith("/children/"):
        config = await current_config(context)
        rows = await context.store.list_children() if context.source == "db" else []
        return render(
            request,
            "children.html",
            title="Schedule",
            config=config,
            rows=rows,
            notice=notice,
            status_code=403,
        )
    if path == "/unifi":
        config = await current_config(context)
        return render(
            request, "unifi.html", title="UniFi", config=config, notice=notice, status_code=403
        )
    if path == "/secrets" or path.startswith("/secrets/"):
        statuses = await context.store.secret_statuses()
        return render(
            request,
            "secrets.html",
            title="Secrets",
            statuses=statuses,
            encryption_enabled=context.store.encryption_enabled,
            test_result=None,
            notice=notice,
            status_code=403,
        )
    if path == "/password":
        return render(request, "password.html", title="Password", notice=notice, status_code=403)
    if path == "/users" or path.startswith("/users/"):
        return await users_page(request, notice=notice, status_code=403)
    if path == "/sessions" or path.startswith("/sessions/"):
        return await sessions_page(request, notice=notice, status_code=403)
    return await dashboard(request, notice=notice, status_code=403)


async def posted(request: Request) -> tuple[FormData, Response | None]:
    failure = origin_failure(request)
    if failure is not None:
        _log_rejection(request, failure)
        return FormData(), blocked_page(request)
    try:
        form = await request.form()
    except Exception:  # noqa: BLE001 - a bad body is a failed CSRF check
        _log_rejection(request, "bad_form")
        return FormData(), blocked_page(request)
    token = str(form.get("csrf_token") or "")
    user = current_user(request)
    context: AppContext = request.app.state.context
    if user is not None:
        if not constant_equal(token, user.csrf):
            _log_rejection(request, "csrf_mismatch")
            if token:
                return form, await stale_page(request)
            return form, blocked_page(request)
        return form, None
    if not await context.store.consume_form_token(token, context.clock()):
        _log_rejection(request, "form_token")
        if token:
            return form, await stale_page(request)
        return form, blocked_page(request)
    return form, None


def post_guard(handler: Callable[[Request], Awaitable[Response]]) -> Callable[[Request], Awaitable[Response]]:
    async def wrapper(request: Request) -> Response:
        if request.method in {"POST", "PUT", "DELETE"}:
            form, denied = await posted(request)
            if denied is not None:
                return denied
            request.state.form = form
        return await handler(request)

    return wrapper


async def load_user(request: Request) -> Visitor | None:
    context: AppContext = request.app.state.context
    token = request.cookies.get(session_cookie_name(context.cookie_secure), "")
    session = await context.store.use_session(token, context.clock())
    if session is None:
        return None
    if session.refresh_cookie:
        request.scope["skylight_cookie_refresh"] = (token, session.cookie_max_age)
    return Visitor(session.username, session.csrf, token, session.role)


def unauthorized(method: str) -> Response:
    if method in {"GET", "HEAD"}:
        return RedirectResponse("/login", status_code=303)
    return PlainTextResponse("Authentication required.", status_code=401)


class SecurityGate:
    def __init__(self, app: object) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: object, send: object) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)  # type: ignore[operator]
            return

        async def send_with_headers(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(raw=message["headers"])
                headers["Content-Security-Policy"] = "default-src 'self'; frame-ancestors 'none'"
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Referrer-Policy"] = "same-origin"
                if not str(scope.get("path", "")).startswith("/static/"):
                    headers["Cache-Control"] = "no-store"
                refresh = scope.get("skylight_cookie_refresh")
                if isinstance(refresh, tuple) and isinstance(refresh[1], int):
                    context: AppContext = request.app.state.context
                    name = session_cookie_name(context.cookie_secure)
                    prefix = f"{name}=".lower().encode()
                    already = any(
                        key.lower() == b"set-cookie" and value.lower().startswith(prefix)
                        for key, value in message["headers"]
                    )
                    if not already:
                        temp = Response()
                        session_cookie(temp, str(refresh[0]), context.cookie_secure, refresh[1])
                        headers.append("set-cookie", temp.headers["set-cookie"])
            await send(message)  # type: ignore[operator]

        request = Request(scope, receive)  # type: ignore[arg-type]
        gate: AppContext = request.app.state.context
        if gate.auth_disabled:
            path = request.url.path
            if _account_path(path):
                if request.method in {"GET", "HEAD"} and path in {"/login", "/setup"}:
                    response = RedirectResponse("/", status_code=303)
                else:
                    response = PlainTextResponse("Not found.", status_code=404)
                await response(scope, receive, send_with_headers)
                return
            if not is_public(path, request.method):
                scope["skylight_user"] = Visitor(ANONYMOUS_USER, gate.anonymous_csrf, "", "admin")
            await self.app(scope, receive, send_with_headers)  # type: ignore[operator]
            return
        if not is_public(request.url.path, request.method):
            try:
                user = await load_user(request)
            except Exception:  # noqa: BLE001 - session errors stay generic
                LOGGER.error("session lookup failed")
                response: Response = PlainTextResponse("Something went wrong.", status_code=500)
                await response(scope, receive, send_with_headers)
                return
            if user is None:
                context: AppContext = request.app.state.context
                needs_setup = (
                    request.method in {"GET", "HEAD"} and await context.store.get_admin() is None
                )
                if needs_setup:
                    response = RedirectResponse("/setup", status_code=303)
                else:
                    response = unauthorized(request.method)
                await response(scope, receive, send_with_headers)
                return
            scope["skylight_user"] = user
        await self.app(scope, receive, send_with_headers)  # type: ignore[operator]


async def current_config(context: AppContext) -> AppConfig:
    if context.source == "db":
        return await context.store.read_config()
    if context.service is None:
        raise RuntimeError("config is not loaded")
    return context.service.config  # type: ignore[attr-defined]


def read_only_blocked(context: AppContext) -> PlainTextResponse | None:
    if not context.read_only:
        return None
    return PlainTextResponse(
        "Configuration is read-only when CONFIG_SOURCE=file.", status_code=403
    )


async def healthz(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "version": __version__})


async def login_page(
    request: Request,
    error: str | None = None,
    notice: str = "",
    status_code: int = 200,
) -> Response:
    context: AppContext = request.app.state.context
    if await context.store.get_admin() is None:
        return RedirectResponse("/setup", status_code=303)
    token = await context.store.create_form_token(context.clock())
    return render(
        request,
        "login.html",
        title="Sign in",
        csrf=token,
        error=error,
        notice=notice,
        created=request.query_params.get("created") == "1",
        status_code=status_code,
    )


async def login(request: Request) -> Response:
    if request.method in {"GET", "HEAD"}:
        return await login_page(request)
    context: AppContext = request.app.state.context
    form: FormData = request.state.form
    username = str(form.get("username") or "")
    password = str(form.get("password") or "")
    now = context.clock()
    result: LoginResult = await attempt_login(
        context.store,
        context.hasher,
        context.dummy_hash,
        username,
        password,
        client_ip(request),
        now,
    )
    if not result.ok:
        action = "lockout" if result.locked else "login_failure"
        await context.store.add_audit(result.username, action, result.username, {"result": action}, now)
        return await login_page(request, error="Sign-in failed.")
    remember = str(form.get("remember") or "") == "1"
    previous = request.cookies.get(session_cookie_name(context.cookie_secure), "")
    if previous:
        await context.store.delete_session(previous)
    token, _csrf = await context.store.create_session(
        result.username,
        now,
        remember,
        coarse_user_agent(request.headers.get("user-agent", "")),
    )
    await context.store.add_audit(result.username, "login", result.username, {"result": "ok"}, now)
    response = RedirectResponse("/", status_code=303)
    session_cookie(response, token, context.cookie_secure, cookie_max_age(remember, now))
    return response


async def setup(request: Request) -> Response:
    context: AppContext = request.app.state.context
    if await context.store.get_admin() is not None:
        return PlainTextResponse("Not found.", status_code=404)
    if context.setup_token is None or len(context.setup_token) < 32:
        return render(request, "setup_disabled.html", title="Setup")
    now = context.clock()
    ip = client_ip(request)
    if request.method in {"GET", "HEAD"}:
        token = await context.store.create_form_token(now)
        return render(request, "setup.html", title="Setup", csrf=token)
    form, denied = await posted(request)
    if denied is not None:
        return denied
    supplied = str(form.get("setup_token") or "")
    username = str(form.get("username") or "").strip()
    password = str(form.get("password") or "")
    confirm = str(form.get("password2") or "")
    if await context.store.login_is_locked(SETUP_LOCK_SUBJECT, ip, now):
        await verify_password_off_loop(context.hasher, context.dummy_hash, password or "x")
        await context.store.add_audit(username or "unknown", "setup_failure", username or "unknown", {}, now)
        token = await context.store.create_form_token(now)
        return render(request, "setup.html", title="Setup", csrf=token, errors=["Setup failed."])
    if not constant_equal(supplied, context.setup_token):
        await context.store.record_failure(SETUP_LOCK_SUBJECT, ip, now)
        await context.store.add_audit(username or "unknown", "setup_failure", username or "unknown", {}, now)
        token = await context.store.create_form_token(now)
        return render(request, "setup.html", title="Setup", csrf=token, errors=["Setup failed."])
    errors: list[str] = []
    name_error = username_problem(username)
    if name_error:
        errors.append(name_error)
    password_error = password_problem(password)
    if password_error:
        errors.append(password_error)
    if password != confirm:
        errors.append("Passwords do not match.")
    if errors:
        token = await context.store.create_form_token(now)
        return render(request, "setup.html", title="Setup", csrf=token, errors=errors, status_code=400)
    try:
        password_hash = await hash_password_off_loop(context.hasher, password)
        await context.store.create_admin(username, password_hash, now)
    except ConfigWriteError as exc:
        if any("already in use" in item for item in exc.errors):
            token = await context.store.create_form_token(now)
            return render(
                request,
                "setup.html",
                title="Setup",
                csrf=token,
                errors=exc.errors,
                status_code=400,
            )
        return PlainTextResponse("Not found.", status_code=404)
    response = RedirectResponse("/login?created=1", status_code=303)
    return response


async def logout(request: Request) -> Response:
    context: AppContext = request.app.state.context
    user = current_user(request)
    now = context.clock()
    if user is not None:
        await context.store.delete_session(user.token)
        await context.store.add_audit(user.username, "logout", user.username, {}, now)
    response = RedirectResponse("/login", status_code=303)
    clear_session_cookie(response, context.cookie_secure)
    return response


async def dashboard(
    request: Request,
    errors: list[str] | None = None,
    status_code: int = 200,
    notice: str = "",
) -> HTMLResponse:
    context: AppContext = request.app.state.context
    config = await current_config(context)
    now = context.clock()
    zone = config.timezone
    local = now.astimezone(ZoneInfo(zone))
    polls = getattr(context.service, "last_poll", {}) if context.service is not None else {}
    poll_at = getattr(context.service, "last_poll_at", None) if context.service is not None else None
    overrides = await context.store.list_overrides(now) if context.source == "db" else []
    by_label = {item.child: item for item in overrides}
    if context.source == "db":
        children = await context.store.list_children()
        rows = [(child.id, child.label, child.object_name, child.cutoff) for child in children]
    else:
        rows = [(0, child.name, child.object_name, child.cutoff.strftime("%H:%M")) for child in config.children]
    views: list[DashChild] = []
    for child_id, label, object_name, cutoff_text in rows:
        if isinstance(cutoff_text, str):
            cutoff = time.fromisoformat(cutoff_text)
        else:
            cutoff = cutoff_text
        poll = polls.get(label)
        if poll is None:
            tasks, state, reason = "—", "Unknown", "Waiting for the first poll."
        elif poll.blocked is None:
            tasks, state, reason = f"{poll.complete}/{poll.required}", "Unchanged", poll.reason
        else:
            tasks = f"{poll.complete}/{poll.required}"
            state = "Blocked" if poll.blocked else "Unblocked"
            reason = poll.reason
        active = by_label.get(label)
        if active is None:
            override_text = "None"
        else:
            override_text = (
                f"{active.action} by {active.set_by} until {format_local(active.expires_at, zone)} "
                f"({format_remaining(active.expires_at, now)} remaining)"
            )
        expiry = override_expiry(now, cutoff, zone)
        views.append(
            DashChild(
                child_id,
                label,
                object_name,
                tasks,
                state,
                reason,
                active is not None,
                override_text,
                past_cutoff(cutoff, local),
                format_local(expiry, zone),
            )
        )
    return render(
        request,
        "dashboard.html",
        status_code=status_code,
        title="Dashboard",
        children=views,
        dry_run=config.dry_run,
        poll_at=format_local(poll_at, zone) if poll_at is not None else None,
        errors=errors or [],
        notice=notice,
    )


async def set_override(request: Request) -> Response:
    context: AppContext = request.app.state.context
    blocked = read_only_blocked(context)
    if blocked is not None:
        return blocked
    form: FormData = request.state.form
    action = str(form.get("action") or "")
    confirmation = str(form.get("confirm") or "")
    child_id = int(request.path_params["child_id"])
    now = context.clock()
    user = current_user(request)
    actor = user.username if user is not None else "unknown"
    try:
        await context.store.set_override(child_id, action, actor, now, confirmation)
    except ConfirmationRequired as exc:
        when = format_local(exc.expires_at, (await current_config(context)).timezone)
        return await dashboard(
            request,
            errors=[f"Type UNBLOCK to confirm this unblock. It lasts until {when}."],
            status_code=400,
        )
    except OverrideError as exc:
        return await dashboard(request, errors=[str(exc)], status_code=400)
    except KeyError:
        return PlainTextResponse("Not found.", status_code=404)
    return RedirectResponse("/", status_code=303)


async def clear_override(request: Request) -> Response:
    context: AppContext = request.app.state.context
    blocked = read_only_blocked(context)
    if blocked is not None:
        return blocked
    user = current_user(request)
    actor = user.username if user is not None else "unknown"
    child_id = int(request.path_params["child_id"])
    await context.store.clear_override(child_id, actor, context.clock())
    return RedirectResponse("/", status_code=303)


async def settings(request: Request) -> Response:
    denied = admin_only(request)
    if denied is not None:
        return denied
    context: AppContext = request.app.state.context
    config = await current_config(context)
    if request.method == "GET":
        return render(request, "settings.html", title="Settings", config=config)
    blocked = read_only_blocked(context)
    if blocked is not None:
        return blocked
    form: FormData = request.state.form
    errors: list[str] = []
    try:
        poll_seconds = int(str(form.get("poll_seconds") or ""))
    except ValueError:
        errors.append("poll_seconds: enter a whole number")
        poll_seconds = config.poll_seconds
    flags: dict[str, bool] = {}
    for name in ("dry_run", "include_late", "include_routines", "empty_day_unlocks"):
        raw = str(form.get(name) or "")
        if raw not in {"true", "false"}:
            errors.append(f"{name}: choose true or false")
            flags[name] = bool(getattr(config, name))
        else:
            flags[name] = raw == "true"
    if errors:
        return render(request, "settings.html", title="Settings", config=config, errors=errors, status_code=400)
    changes = {
        "timezone": str(form.get("timezone") or "").strip(),
        "poll_seconds": poll_seconds,
        **flags,
    }
    user = current_user(request)
    try:
        await context.store.update_settings(user.username if user else "unknown", context.clock(), changes)
    except ValidationError as exc:
        return render(
            request,
            "settings.html",
            title="Settings",
            config=config,
            errors=field_errors(exc),
            status_code=400,
        )
    return RedirectResponse("/settings?saved=1", status_code=303)


async def children(request: Request) -> Response:
    denied = admin_only(request)
    if denied is not None:
        return denied
    context: AppContext = request.app.state.context
    config = await current_config(context)
    rows = await context.store.list_children() if context.source == "db" else []
    if context.source != "db":
        rows = []
    if request.method == "GET":
        return render(request, "children.html", title="Children", config=config, rows=rows)
    blocked = read_only_blocked(context)
    if blocked is not None:
        return blocked
    form: FormData = request.state.form
    label = str(form.get("label") or "").strip()
    profile = str(form.get("skylight_profile") or "").strip()
    object_name = str(form.get("object_name") or "").strip()
    cutoff = str(form.get("cutoff") or "").strip()
    errors = _child_errors(label, profile, object_name, cutoff)
    user = current_user(request)
    actor = user.username if user else "unknown"
    if errors:
        return render(
            request,
            "children.html",
            title="Children",
            config=config,
            rows=rows,
            errors=errors,
            status_code=400,
        )
    try:
        await context.store.add_child(actor, context.clock(), label, profile, object_name, cutoff)
    except ValidationError as exc:
        return render(
            request,
            "children.html",
            title="Children",
            config=config,
            rows=rows,
            errors=field_errors(exc),
            status_code=400,
        )
    return RedirectResponse("/children?saved=1", status_code=303)


async def update_child(request: Request) -> Response:
    denied = admin_only(request)
    if denied is not None:
        return denied
    context: AppContext = request.app.state.context
    blocked = read_only_blocked(context)
    if blocked is not None:
        return blocked
    form: FormData = request.state.form
    label = str(form.get("label") or "").strip()
    profile = str(form.get("skylight_profile") or "").strip()
    object_name = str(form.get("object_name") or "").strip()
    cutoff = str(form.get("cutoff") or "").strip()
    errors = _child_errors(label, profile, object_name, cutoff)
    if errors:
        config = await current_config(context)
        rows = await context.store.list_children()
        return render(
            request,
            "children.html",
            title="Children",
            config=config,
            rows=rows,
            errors=errors,
            status_code=400,
        )
    user = current_user(request)
    try:
        await context.store.update_child(
            int(request.path_params["child_id"]),
            user.username if user else "unknown",
            context.clock(),
            label,
            profile,
            object_name,
            cutoff,
        )
    except KeyError:
        return PlainTextResponse("Not found.", status_code=404)
    except ValidationError as exc:
        config = await current_config(context)
        rows = await context.store.list_children()
        return render(
            request,
            "children.html",
            title="Children",
            config=config,
            rows=rows,
            errors=field_errors(exc),
            status_code=400,
        )
    return RedirectResponse("/children?saved=1", status_code=303)


async def delete_child(request: Request) -> Response:
    denied = admin_only(request)
    if denied is not None:
        return denied
    context: AppContext = request.app.state.context
    blocked = read_only_blocked(context)
    if blocked is not None:
        return blocked
    user = current_user(request)
    try:
        await context.store.delete_child(
            int(request.path_params["child_id"]),
            user.username if user else "unknown",
            context.clock(),
        )
    except KeyError:
        return PlainTextResponse("Not found.", status_code=404)
    except ConfigWriteError as exc:
        config = await current_config(context)
        rows = await context.store.list_children()
        return render(
            request,
            "children.html",
            title="Children",
            config=config,
            rows=rows,
            errors=exc.errors,
            status_code=400,
        )
    return RedirectResponse("/children?saved=1", status_code=303)


def _child_errors(label: str, profile: str, object_name: str, cutoff: str) -> list[str]:
    errors: list[str] = []
    if not label or not profile or not object_name:
        errors.append("Label, Skylight profile, and object name are required.")
    if _CUTOFF.fullmatch(cutoff) is None:
        errors.append("cutoff: use HH:MM")
    else:
        try:
            time.fromisoformat(cutoff)
        except ValueError:
            errors.append("cutoff: use HH:MM")
    return errors


async def unifi(request: Request) -> Response:
    denied = admin_only(request)
    if denied is not None:
        return denied
    context: AppContext = request.app.state.context
    config = await current_config(context)
    if request.method == "GET":
        return render(request, "unifi.html", title="UniFi", config=config)
    blocked = read_only_blocked(context)
    if blocked is not None:
        return blocked
    form: FormData = request.state.form
    site = str(form.get("unifi_site") or "").strip()
    changes = {
        "unifi_url": str(form.get("unifi_url") or "").strip(),
        "unifi_site": site or None,
        "unifi_tls_ca": str(form.get("unifi_tls_ca") or ""),
    }
    user = current_user(request)
    try:
        await context.store.update_settings(user.username if user else "unknown", context.clock(), changes)
    except ValidationError as exc:
        return render(
            request, "unifi.html", title="UniFi", config=config, errors=field_errors(exc), status_code=400
        )
    return RedirectResponse("/unifi?saved=1", status_code=303)


async def secrets(request: Request) -> Response:
    denied = admin_only(request)
    if denied is not None:
        return denied
    context: AppContext = request.app.state.context
    statuses = await context.store.secret_statuses()
    test_result = clean_test_result(request.query_params.get("test"))
    if request.method == "GET":
        return render(
            request,
            "secrets.html",
            title="Secrets",
            statuses=statuses,
            encryption_enabled=context.store.encryption_enabled,
            test_result=test_result,
        )
    if not context.store.encryption_enabled:
        return render(
            request,
            "secrets.html",
            title="Secrets",
            statuses=statuses,
            encryption_enabled=False,
            test_result=test_result,
            errors=["Secret storage is disabled because APP_ENCRYPTION_KEY is not set."],
            status_code=403,
        )
    form: FormData = request.state.form
    user = current_user(request)
    actor = user.username if user else "unknown"
    now = context.clock()
    for name in SECRET_NAMES:
        value = str(form.get(name) or "")
        if value == "":
            continue
        await context.store.set_secret(name, value, actor, now)
    return RedirectResponse("/secrets?saved=1", status_code=303)


async def clear_secret(request: Request) -> Response:
    denied = admin_only(request)
    if denied is not None:
        return denied
    context: AppContext = request.app.state.context
    if not context.store.encryption_enabled:
        return PlainTextResponse("Secret storage is disabled.", status_code=403)
    name = str(request.path_params["name"])
    if name not in SECRET_NAMES:
        return PlainTextResponse("Not found.", status_code=404)
    user = current_user(request)
    await context.store.clear_secret(name, user.username if user else "unknown", context.clock())
    return RedirectResponse("/secrets?saved=1", status_code=303)


async def test_secret_connections(request: Request) -> Response:
    denied = admin_only(request)
    if denied is not None:
        return denied
    context: AppContext = request.app.state.context
    secrets = await context.store.resolve_secrets()
    try:
        config = await current_config(context)
    except ValidationError:
        return RedirectResponse("/secrets?test=not_configured", status_code=303)
    tester = context.tester or test_connections
    try:
        result = await tester(secrets, config)
    except Exception as exc:  # noqa: BLE001 - the tester error may contain a secret
        LOGGER.warning("connection test failed error_type=%s", type(exc).__name__)
        result = "skylight_error"
    safe = clean_test_result(result) or "skylight_error"
    return RedirectResponse(f"/secrets?test={quote(safe)}", status_code=303)


async def audit(request: Request) -> Response:
    denied = admin_only(request)
    if denied is not None:
        return denied
    context: AppContext = request.app.state.context
    raw = request.query_params.get("page", "1")
    try:
        page = int(raw)
    except ValueError:
        page = 1
    listing = await context.store.list_audit(page, 25)
    return render(request, "audit.html", title="Audit", listing=listing)


async def password(request: Request) -> Response:
    context: AppContext = request.app.state.context
    if request.method == "GET":
        return render(request, "password.html", title="Password")
    form: FormData = request.state.form
    user = current_user(request)
    if user is None:
        return unauthorized(request.method)
    current = str(form.get("current_password") or "")
    new_password = str(form.get("new_password") or "")
    confirm = str(form.get("password2") or "")
    now = context.clock()
    account = await context.store.get_user(user.username)
    ip = client_ip(request)
    locked = await context.store.login_is_locked(user.username, ip, now)
    current_ok = False
    if account is not None and not account.disabled and not locked:
        current_ok = await verify_password_off_loop(context.hasher, account.password_hash, current)
    else:
        await verify_password_off_loop(context.hasher, context.dummy_hash, current or "x")
    if not current_ok:
        if not locked:
            await context.store.record_failure(user.username, ip, now)
        await context.store.add_audit(user.username, "password_change_failed", user.username, {}, now)
        return render(
            request,
            "password.html",
            title="Password",
            errors=["Password change failed."],
            status_code=400,
        )
    errors: list[str] = []
    problem = password_problem(new_password)
    if problem:
        errors.append(problem)
    if new_password != confirm:
        errors.append("Passwords do not match.")
    if errors:
        return render(request, "password.html", title="Password", errors=errors, status_code=400)
    new_hash = await hash_password_off_loop(context.hasher, new_password)
    remember, agent, anchor = await context.store.session_hints(user.token)
    await context.store.set_password(user.username, new_hash, now)
    kept = anchor if remember else None
    token, _csrf = await context.store.create_session(user.username, now, remember, agent, kept)
    response = RedirectResponse("/password?saved=1", status_code=303)
    session_cookie(response, token, context.cookie_secure, cookie_max_age(remember, now, kept))
    return response


@dataclass
class ShownSession:
    id: int
    username: str
    created_text: str
    seen_text: str
    agent: str
    current: bool
    remember: bool


async def users_page(
    request: Request,
    errors: list[str] | None = None,
    notice: str = "",
    status_code: int = 200,
) -> Response:
    denied = admin_only(request)
    if denied is not None:
        return denied
    context: AppContext = request.app.state.context
    rows = await context.store.list_users()
    return render(
        request,
        "users.html",
        title="Users",
        rows=rows,
        errors=errors or [],
        notice=notice,
        status_code=status_code,
    )


async def users(request: Request) -> Response:
    if request.method == "GET":
        return await users_page(request)
    denied = admin_only(request)
    if denied is not None:
        return denied
    context: AppContext = request.app.state.context
    form: FormData = request.state.form
    username = str(form.get("username") or "").strip()
    password = str(form.get("password") or "")
    confirm = str(form.get("password2") or "")
    role = str(form.get("role") or "")
    errors: list[str] = []
    name_error = username_problem(username)
    if name_error:
        errors.append(name_error)
    password_error = password_problem(password)
    if password_error:
        errors.append(password_error)
    if password != confirm:
        errors.append("Passwords do not match.")
    if role not in {"admin", "member"}:
        errors.append("role: choose admin or member")
    if errors:
        return await users_page(request, errors=errors, status_code=400)
    user = current_user(request)
    actor = user.username if user else "unknown"
    try:
        password_hash = await hash_password_off_loop(context.hasher, password)
        await context.store.create_user(actor, context.clock(), username, password_hash, role)
    except ConfigWriteError as exc:
        return await users_page(request, errors=exc.errors, status_code=400)
    return RedirectResponse("/users?saved=1", status_code=303)


async def set_user_role(request: Request) -> Response:
    denied = admin_only(request)
    if denied is not None:
        return denied
    return await _change_user(request, "role")


async def disable_user(request: Request) -> Response:
    denied = admin_only(request)
    if denied is not None:
        return denied
    return await _change_user(request, "disable")


async def enable_user(request: Request) -> Response:
    denied = admin_only(request)
    if denied is not None:
        return denied
    return await _change_user(request, "enable")


async def delete_user_account(request: Request) -> Response:
    denied = admin_only(request)
    if denied is not None:
        return denied
    return await _change_user(request, "delete")


async def reset_user_password(request: Request) -> Response:
    denied = admin_only(request)
    if denied is not None:
        return denied
    return await _change_user(request, "password")


async def _change_user(request: Request, action: str) -> Response:
    context: AppContext = request.app.state.context
    username = str(request.path_params["username"])
    if username_problem(username):
        return PlainTextResponse("Not found.", status_code=404)
    form: FormData = request.state.form
    user = current_user(request)
    actor = user.username if user else "unknown"
    now = context.clock()
    try:
        if action == "role":
            await context.store.set_role(username, str(form.get("role") or ""), actor, now)
        elif action == "disable":
            await context.store.set_disabled(username, True, actor, now)
        elif action == "enable":
            await context.store.set_disabled(username, False, actor, now)
        elif action == "delete":
            await context.store.delete_user(username, actor, now)
        elif action == "password":
            new_password = str(form.get("new_password") or "")
            confirm = str(form.get("password2") or "")
            errors: list[str] = []
            problem = password_problem(new_password)
            if problem:
                errors.append(problem)
            if new_password != confirm:
                errors.append("Passwords do not match.")
            if errors:
                return await users_page(request, errors=errors, status_code=400)
            password_hash = await hash_password_off_loop(context.hasher, new_password)
            self_reset = user is not None and user.username.casefold() == username.casefold()
            remember, agent, anchor = False, "", None
            if self_reset and user is not None:
                remember, agent, anchor = await context.store.session_hints(user.token)
            await context.store.reset_password(actor, username, password_hash, now)
            if self_reset and user is not None:
                kept = anchor if remember else None
                token, _csrf = await context.store.create_session(
                    user.username, now, remember, agent, kept
                )
                response = RedirectResponse("/users?saved=1", status_code=303)
                session_cookie(
                    response, token, context.cookie_secure, cookie_max_age(remember, now, kept)
                )
                return response
    except LastAdminError as exc:
        return await users_page(request, errors=[str(exc)], status_code=400)
    except ConfigWriteError as exc:
        return await users_page(request, errors=exc.errors, status_code=400)
    except KeyError:
        return PlainTextResponse("Not found.", status_code=404)
    return RedirectResponse("/users?saved=1", status_code=303)


async def sessions_page(
    request: Request,
    errors: list[str] | None = None,
    notice: str = "",
    status_code: int = 200,
) -> Response:
    context: AppContext = request.app.state.context
    user = current_user(request)
    if user is None:
        return unauthorized(request.method)
    config = await current_config(context)
    scope = None if user.role == "admin" else user.username
    rows = await context.store.list_sessions(user.token, scope)
    shown = [
        ShownSession(
            item.id,
            item.username,
            format_local(item.created_at, config.timezone),
            format_local(item.last_seen, config.timezone),
            item.user_agent,
            item.current,
            item.remember,
        )
        for item in rows
    ]
    return render(
        request,
        "sessions.html",
        title="Sessions",
        rows=shown,
        errors=errors or [],
        notice=notice,
        status_code=status_code,
    )


async def sessions(request: Request) -> Response:
    return await sessions_page(request)


async def revoke_session(request: Request) -> Response:
    context: AppContext = request.app.state.context
    user = current_user(request)
    if user is None:
        return unauthorized(request.method)
    session_id = str(request.path_params["session_id"])
    try:
        await context.store.revoke_session(
            session_id,
            user.username,
            context.clock(),
            username=user.username,
            admin=user.role == "admin",
        )
    except KeyError:
        return PlainTextResponse("Not found.", status_code=404)
    if user.role != "admin":
        still = await context.store.list_sessions(user.token, user.username)
        if not any(item.current for item in still):
            response = RedirectResponse("/login", status_code=303)
            clear_session_cookie(response, context.cookie_secure)
            return response
    return RedirectResponse("/sessions?saved=1", status_code=303)


async def unhandled(request: Request, exc: Exception) -> Response:
    if isinstance(exc, HTTPException):
        detail = exc.detail if isinstance(exc.detail, str) else "Error"
        return render(request, "blocked.html", title="Error", message=detail, status_code=exc.status_code)
    LOGGER.error("request failed error_type=%s", type(exc).__name__)
    return render(
        request,
        "blocked.html",
        title="Error",
        message="Something went wrong.",
        status_code=500,
    )


def create_app(context: AppContext) -> Starlette:
    templates = Environment(
        loader=FileSystemLoader(_PACKAGE / "templates"),
        autoescape=select_autoescape(["html"]),
    )
    routes = [
        Route("/healthz", healthz, methods=["GET"]),
        Route("/login", post_guard(login), methods=["GET", "POST"]),
        Route("/setup", setup, methods=["GET", "POST"]),
        Route("/logout", post_guard(logout), methods=["POST"]),
        Route("/", dashboard, methods=["GET"]),
        Route("/settings", post_guard(settings), methods=["GET", "POST"]),
        Route("/children", post_guard(children), methods=["GET", "POST"]),
        Route("/children/{child_id:int}", post_guard(update_child), methods=["POST"]),
        Route("/children/{child_id:int}/delete", post_guard(delete_child), methods=["POST"]),
        Route("/unifi", post_guard(unifi), methods=["GET", "POST"]),
        Route("/secrets", post_guard(secrets), methods=["GET", "POST"]),
        Route("/secrets/test", post_guard(test_secret_connections), methods=["POST"]),
        Route("/secrets/{name}/clear", post_guard(clear_secret), methods=["POST"]),
        Route("/audit", audit, methods=["GET"]),
        Route("/password", post_guard(password), methods=["GET", "POST"]),
        Route("/users", post_guard(users), methods=["GET", "POST"]),
        Route("/users/{username}/role", post_guard(set_user_role), methods=["POST"]),
        Route("/users/{username}/disable", post_guard(disable_user), methods=["POST"]),
        Route("/users/{username}/enable", post_guard(enable_user), methods=["POST"]),
        Route("/users/{username}/delete", post_guard(delete_user_account), methods=["POST"]),
        Route("/users/{username}/password", post_guard(reset_user_password), methods=["POST"]),
        Route("/sessions", post_guard(sessions), methods=["GET"]),
        Route("/sessions/{session_id}/revoke", post_guard(revoke_session), methods=["POST"]),
        Route("/overrides/{child_id:int}", post_guard(set_override), methods=["POST"]),
        Route("/overrides/{child_id:int}/clear", post_guard(clear_override), methods=["POST"]),
        Mount("/static", app=StaticFiles(directory=_PACKAGE / "static"), name="static"),
    ]
    app = Starlette(
        routes=routes,
        middleware=[Middleware(SecurityGate)],
        exception_handlers={Exception: unhandled},
    )
    app.state.context = context
    app.state.templates = templates
    return app
