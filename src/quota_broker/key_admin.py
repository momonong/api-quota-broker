"""Private loopback key-entry page with fixed Doppler destinations.

This module is a separate administrator process. It never loads the inference
Service Token or calls a model provider. Only a human form POST sends a secret.
"""

import html
import json
import os
import re
import secrets
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

DESTINATIONS = {
    "nvidia": ("NVIDIA", "api-quota-broker", "dev", "NVIDIA_API_KEY"),
    "groq": ("Groq", "api-quota-broker", "dev", "GROQ_API_KEY"),
}
SESSION_SECONDS = 1800
MAX_BODY = 8192
PAGE_STYLE = """<style>
:root{color-scheme:light;--bg:#ffffff;--surface:#f3f6fa;--text:#182230;--muted:#475467;--border:#667085;--field:#ffffff;--button:#1d4ed8;--button-text:#ffffff;--link:#1648b5;--success:#12633c;--error:#a01e2f}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;--bg:#101720;--surface:#1b2734;--text:#eef3f8;--muted:#bdc9d6;--border:#8393a8;--field:#1b2734;--button:#a9ccff;--button-text:#101720;--link:#b8d4ff;--success:#8beaaf;--error:#ffaaaa}}
body{font:16px system-ui,sans-serif;max-width:800px;margin:2rem auto;padding:0 1rem;line-height:1.5;background:var(--bg);color:var(--text)}
p{color:var(--muted)}a{color:var(--link)}table{border-collapse:collapse;width:100%}th,td{border:1px solid var(--border);padding:.5rem;text-align:left}th{background:var(--surface)}
label{display:block;margin:.7rem 0}input,select{font:inherit;width:100%;max-width:32rem;padding:.4rem;background:var(--field);color:var(--text);border:1px solid var(--border);border-radius:.25rem}input::placeholder{color:var(--muted)}
button{font:inherit;padding:.45rem .8rem;background:var(--button);color:var(--button-text);border:1px solid var(--border);border-radius:.25rem;cursor:pointer}button:focus-visible,input:focus-visible,select:focus-visible,a:focus-visible{outline:3px solid var(--link);outline-offset:2px}
.status-good{color:var(--success)}.status-error{color:var(--error)}.status-warn{color:var(--muted)}.message:empty{display:none}
</style>"""


class AdminError(Exception):
    """Safe, non-secret error code for the management page."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class KeyState:
    configured: bool | None
    scope_ready: bool | None


Command = Callable[[list[str], bytes | None], subprocess.CompletedProcess[bytes]]


def _run(command: list[str], value: bytes | None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(command, input=value, capture_output=True, timeout=15, check=False)


class DopplerCLIWriter:
    """Only two fixed project/config/secret names; value passes through stdin."""

    def __init__(self, binary: str, scope: Path, run: Command = _run):
        if not Path(binary).is_file():
            raise AdminError("doppler_cli_missing")
        self.binary = binary
        self.scope = str(scope.resolve())
        self.run = run

    def _call(
        self, args: list[str], *, value: bytes | None = None, json_output: bool = False
    ) -> subprocess.CompletedProcess[bytes]:
        command = [
            self.binary,
            "--no-read-env",
            "--no-check-version",
            "--attempts",
            "1",
            "--scope",
            self.scope,
            "--silent",
        ]
        if json_output:
            command.append("--json")
        try:
            return self.run(command + args, value)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AdminError("doppler_unavailable") from exc

    def _list(self, args: list[str]) -> list[dict[str, Any]]:
        result = self._call(args, json_output=True)
        if result.returncode:
            raise AdminError("doppler_read_failed")
        try:
            data = json.loads(result.stdout)
        except ValueError as exc:
            raise AdminError("doppler_response_invalid") from exc
        if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
            raise AdminError("doppler_response_invalid")
        return data

    def _projects(self) -> set[str]:
        projects: set[str] = set()
        for page in range(1, 21):
            rows = self._list(["projects", "--number", "100", "--page", str(page)])
            projects.update(row["id"] for row in rows if isinstance(row.get("id"), str))
            if len(rows) < 100:
                return projects
        raise AdminError("doppler_list_incomplete")

    def _configs(self, project: str) -> set[str]:
        names: set[str] = set()
        for page in range(1, 21):
            rows = self._list(
                ["configs", "--project", project, "--number", "100", "--page", str(page)]
            )
            names.update(row["name"] for row in rows if isinstance(row.get("name"), str))
            if len(rows) < 100:
                return names
        raise AdminError("doppler_list_incomplete")

    def state(self, provider: str) -> KeyState:
        if provider not in DESTINATIONS:
            raise AdminError("invalid_provider")
        _, project, config, name = DESTINATIONS[provider]
        if project not in self._projects():
            return KeyState(False, False)
        if config not in self._configs(project):
            return KeyState(False, False)
        result = self._call(
            ["secrets", "--only-names", "--project", project, "--config", config],
            json_output=True,
        )
        if result.returncode:
            raise AdminError("doppler_read_failed")
        try:
            names = json.loads(result.stdout)
        except ValueError as exc:
            raise AdminError("doppler_response_invalid") from exc
        if not isinstance(names, (list, dict)):
            raise AdminError("doppler_response_invalid")
        return KeyState(name in names, True)

    def save(self, provider: str, value: str) -> None:
        if provider not in DESTINATIONS:
            raise AdminError("invalid_provider")
        if not value or len(value) > 4096 or "\n" in value or "\r" in value or "\x00" in value:
            raise AdminError("invalid_key")
        _, project, config, name = DESTINATIONS[provider]
        initial = self.state(provider)
        if not initial.scope_ready:
            raise AdminError("shared_scope_missing")
        result = self._call(
            ["secrets", "set", name, "--no-interactive", "--project", project, "--config", config],
            value=value.encode("utf-8"),
        )
        if result.returncode:
            raise AdminError("doppler_write_failed")
        if not self.state(provider).configured:
            raise AdminError("doppler_write_unconfirmed")


class MetadataStore:
    """Optional public key metadata only; never store the key value."""

    def __init__(self, path: Path):
        self.path = path

    def read(self) -> dict[str, dict[str, str]]:
        if not self.path.exists():
            return {}
        if self.path.is_symlink() or self.path.stat().st_mode & 0o077:
            raise AdminError("metadata_permissions")
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AdminError("metadata_unavailable") from exc
        if not isinstance(data, dict):
            raise AdminError("metadata_unavailable")
        return cast(dict[str, dict[str, str]], data)

    def write(self, provider: str, metadata: dict[str, str]) -> None:
        if provider not in DESTINATIONS:
            raise AdminError("invalid_provider")
        data = self.read()
        data[provider] = metadata
        temporary = self.path.with_name("." + self.path.name + "." + secrets.token_hex(8))
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            raise AdminError("metadata_unavailable") from exc
        finally:
            temporary.unlink(missing_ok=True)


def validate_metadata(form: dict[str, str]) -> dict[str, str]:
    metadata = {name: form.get(name, "").strip() for name in ("key_name", "key_id", "expiry")}
    if any(len(value) > 128 or any(ord(ch) < 32 for ch in value) for value in metadata.values()):
        raise AdminError("invalid_metadata")
    if metadata["expiry"]:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", metadata["expiry"]):
            raise AdminError("invalid_metadata")
        try:
            date.fromisoformat(metadata["expiry"])
        except ValueError as exc:
            raise AdminError("invalid_metadata") from exc
    return metadata


def make_key_admin_server(
    writer: DopplerCLIWriter,
    metadata: MetadataStore,
    admin_token: str,
    *,
    host: str = "127.0.0.1",
    port: int = 18085,
) -> ThreadingHTTPServer:
    if host != "127.0.0.1":
        raise ValueError("key admin binds IPv4 loopback only")
    if len(admin_token) < 32:
        raise ValueError("strong administrator token required")
    sessions: dict[str, tuple[str, float, str]] = {}
    lock = threading.Lock()
    save_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def setup(self) -> None:
            self.request.settimeout(15)
            super().setup()

        def log_message(self, *_args: object) -> None:
            pass

        def _headers(
            self,
            status: int,
            body: bytes,
            *,
            location: str | None = None,
            cookie: str | None = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'",
            )
            if location:
                self.send_header("Location", location)
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(body)

        def _page(self, status: int, body: str, *, cookie: str | None = None) -> None:
            if not body.startswith("<!doctype"):
                body = f'<!doctype html><html lang="zh-Hant"><meta charset="utf-8">{PAGE_STYLE}{body}</html>'
            self._headers(status, body.encode("utf-8"), cookie=cookie)

        def _redirect(self, target: str, *, cookie: str | None = None) -> None:
            self._headers(303, b"", location=target, cookie=cookie)

        def _origin(self) -> str:
            return f"http://{host}:{cast(ThreadingHTTPServer, self.server).server_port}"

        def _host_ok(self) -> bool:
            return self.headers.get("Host") == self._origin().removeprefix("http://")

        def _same_origin_post(self) -> bool:
            origin = self.headers.get("Origin")
            if origin == self._origin():
                return True
            # Codex IAB sends Origin: null for a form submitted from this very
            # document. Fetch Metadata remains browser-generated and reports
            # same-origin navigation; reject all other opaque origins.
            return (
                origin == "null"
                and self.headers.get("Sec-Fetch-Site") == "same-origin"
                and self.headers.get("Sec-Fetch-Mode") == "navigate"
                and self.headers.get("Sec-Fetch-Dest") == "document"
            )

        def _form(self) -> dict[str, str]:
            if (
                self.headers.get("Content-Type", "").split(";", 1)[0]
                != "application/x-www-form-urlencoded"
            ):
                raise AdminError("invalid_form")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise AdminError("invalid_form") from exc
            if not 0 < length <= MAX_BODY:
                raise AdminError("invalid_form")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise AdminError("invalid_form")
            try:
                parsed = parse_qs(raw.decode("utf-8"), keep_blank_values=True, strict_parsing=True)
            except (UnicodeError, ValueError) as exc:
                raise AdminError("invalid_form") from exc
            if any(len(values) != 1 for values in parsed.values()):
                raise AdminError("invalid_form")
            return {name: values[0] for name, values in parsed.items()}

        def _session(self) -> tuple[str, str] | None:
            try:
                jar = cookies.SimpleCookie()
                jar.load(self.headers.get("Cookie", ""))
                sid = jar.get("key_admin_session")
            except cookies.CookieError:
                return None
            if sid is None:
                return None
            with lock:
                entry = sessions.get(sid.value)
                if entry is None or entry[1] < time.monotonic():
                    sessions.pop(sid.value, None)
                    return None
                return sid.value, entry[0]

        def _admin_page(self, sid: str, csrf: str) -> str:
            with lock:
                old_csrf, expiry, flash = sessions[sid]
                sessions[sid] = (old_csrf, expiry, "")
            cards = []
            saved = metadata.read()
            for provider, (label, project, config, name) in DESTINATIONS.items():
                try:
                    state = writer.state(provider)
                    status = "configured" if state.configured else "missing"
                    status_class = "status-good" if state.configured else "status-warn"
                    if not state.scope_ready:
                        status += "（Doppler scope 尚未建立）"
                except AdminError:
                    status = "查詢失敗／未知"
                    status_class = "status-error"
                extra = saved.get(provider, {})
                details = "、".join(
                    f"{html.escape(field)}: {html.escape(extra.get(field, ''))}"
                    for field in ("key_name", "key_id", "expiry")
                    if extra.get(field)
                )
                cards.append(
                    f"<tr><th>{html.escape(label)}</th><td>{html.escape(project)}/{html.escape(config)}/{html.escape(name)}</td>"
                    f'<td><span class="{status_class}">{html.escape(status)}</span></td><td>{details or "—"}</td></tr>'
                )
            note = html.escape(flash)
            note_class = "status-error" if "失敗" in flash or "未確認" in flash else "status-good"
            return f'''<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><title>API Key 管理</title>
{PAGE_STYLE}
<h1>私用 API Key 管理</h1><p>僅在這台電腦的 loopback 提供。儲存只更新 Doppler；不會呼叫模型，也不代表 key 已通過供應商驗證。</p>
<p role="status" class="message {note_class}">{note}</p>
<table><tr><th>供應商</th><th>固定目的地</th><th>名稱狀態</th><th>選填資訊</th></tr>{"".join(cards)}</table>
<h2>儲存或替換金鑰</h2><p>按下儲存會覆寫所選供應商固定名稱的 key。共用 Doppler scope 若不存在，會安全拒絕。其他秘密不會變更。</p>
<form method="post" action="/admin/save" autocomplete="off"><input type="hidden" name="csrf" value="{html.escape(csrf)}">
<label>供應商<select name="provider"><option value="groq">Groq</option><option value="nvidia">NVIDIA</option></select></label>
<label>API Key<input type="password" name="key" required maxlength="4096" autocomplete="off"></label>
<label>名稱（選填）<input name="key_name" maxlength="128"></label>
<label>ID（選填）<input name="key_id" maxlength="128"></label>
<label>到期日期（選填，YYYY-MM-DD）<input name="expiry" maxlength="10" placeholder="YYYY-MM-DD"></label>
<button type="submit">明確儲存／替換至 Doppler</button></form>
<form method="post" action="/admin/logout"><input type="hidden" name="csrf" value="{html.escape(csrf)}"><button>登出</button></form></html>'''

        def _handle(self) -> None:
            if not self._host_ok() or urlsplit(self.path).query or urlsplit(self.path).fragment:
                self._page(403, "<h1>Forbidden</h1>")
                return
            path = urlsplit(self.path).path
            if self.command == "GET" and path == "/":
                self._redirect("/admin")
                return
            if self.command == "GET" and path == "/admin/login":
                self._page(
                    200,
                    f'<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><title>管理員登入</title>{PAGE_STYLE}<h1>管理員登入</h1><p>私用 API Key 管理。請輸入此機的管理憑證。</p><form method="post" action="/admin/login" autocomplete="off"><label>管理憑證<input name="token" type="password" autocomplete="off" required></label><button>登入</button></form></html>',
                )
                return
            if self.command == "POST" and path == "/admin/login":
                if not self._same_origin_post():
                    raise AdminError("origin_rejected")
                form = self._form()
                if set(form) != {"token"} or not secrets.compare_digest(form["token"], admin_token):
                    raise AdminError("unauthorized")
                sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                with lock:
                    sessions[sid] = (csrf, time.monotonic() + SESSION_SECONDS, "")
                self._redirect(
                    "/admin",
                    cookie=f"key_admin_session={sid}; HttpOnly; SameSite=Strict; Path=/admin",
                )
                return
            session = self._session()
            if session is None:
                self._redirect("/admin/login")
                return
            sid, csrf = session
            if self.command == "GET" and path == "/admin":
                self._page(200, self._admin_page(sid, csrf))
                return
            if self.command != "POST" or path not in {"/admin/save", "/admin/logout"}:
                self._page(404, "<h1>Not found</h1>")
                return
            if not self._same_origin_post():
                raise AdminError("origin_rejected")
            form = self._form()
            if not secrets.compare_digest(form.get("csrf", ""), csrf):
                raise AdminError("csrf_rejected")
            if path == "/admin/logout":
                if set(form) != {"csrf"}:
                    raise AdminError("invalid_form")
                with lock:
                    sessions.pop(sid, None)
                self._redirect(
                    "/admin/login",
                    cookie="key_admin_session=; Max-Age=0; HttpOnly; SameSite=Strict; Path=/admin",
                )
                return
            if set(form) != {"csrf", "provider", "key", "key_name", "key_id", "expiry"}:
                raise AdminError("invalid_form")
            provider = form["provider"]
            if provider not in DESTINATIONS:
                raise AdminError("invalid_provider")
            public_metadata = validate_metadata(form)
            if any(form["key"] in field for field in public_metadata.values() if field):
                raise AdminError("invalid_metadata")
            with save_lock:
                try:
                    writer.save(provider, form["key"])
                except AdminError as exc:
                    with lock:
                        sessions[sid] = (
                            csrf,
                            time.monotonic() + SESSION_SECONDS,
                            f"儲存未確認／失敗：{exc.code}。請核對 Doppler 後再決定是否重試。",
                        )
                    self._redirect("/admin")
                    return
                try:
                    metadata.write(provider, public_metadata)
                    note = "Doppler 已回報儲存成功且名稱存在；供應商有效性尚未驗證。"
                except AdminError:
                    note = "Doppler 已回報儲存成功，但選填資訊保存失敗；供應商有效性尚未驗證。"
            with lock:
                sessions[sid] = (csrf, time.monotonic() + SESSION_SECONDS, note)
            self._redirect("/admin")

        def do_GET(self) -> None:
            try:
                self._handle()
            except AdminError as exc:
                self._page(
                    403
                    if exc.code in {"csrf_rejected", "origin_rejected"}
                    else 401
                    if exc.code == "unauthorized"
                    else 400,
                    "<h1>Request failed</h1>",
                )
            except Exception:  # noqa: BLE001 - secret and CLI errors must never be echoed
                self._page(503, "<h1>Unavailable</h1>")

        def do_POST(self) -> None:
            self.do_GET()

    return ThreadingHTTPServer((host, port), Handler)
