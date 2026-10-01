"""Role-separated loopback executor and same-origin administrator UI."""

import html
import json
import secrets
import threading
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

from .catalog import MODELS
from .core import BrokerError
from .nvidia import ExecutionError, NvidiaExecutor


def make_nvidia_server(
    executor: NvidiaExecutor, host: str, port: int, client_token: str, admin_token: str
) -> ThreadingHTTPServer:
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("NVIDIA executor binds loopback only")
    if min(len(client_token), len(admin_token)) < 32 or secrets.compare_digest(
        client_token, admin_token
    ):
        raise ValueError("distinct strong client and admin tokens required")
    sessions: dict[str, str] = {}
    session_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def setup(self) -> None:
            self.request.settimeout(10)
            super().setup()

        def log_message(self, *_args: object) -> None:
            pass

        def _host_ok(self) -> bool:
            return (
                self.headers.get("Host")
                == f"{host}:{cast(ThreadingHTTPServer, self.server).server_port}"
            )

        def _origin_ok(self) -> bool:
            return (
                self.headers.get("Origin")
                == f"http://{host}:{cast(ThreadingHTTPServer, self.server).server_port}"
            )

        def _headers(self, status: int, mime: str, body: bytes, cookie: str | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'",
            )
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, value: dict[str, Any]) -> None:
            self._headers(
                status,
                "application/json; charset=utf-8",
                json.dumps(value, separators=(",", ":")).encode(),
            )

        def _html(self, status: int, value: str, cookie: str | None = None) -> None:
            self._headers(status, "text/html; charset=utf-8", value.encode(), cookie)

        def _body(self) -> bytes:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ExecutionError("invalid_request", "invalid body length") from exc
            if not 0 < length <= 65_536:
                raise ExecutionError("invalid_request", "invalid body length")
            data = self.rfile.read(length)
            if len(data) != length:
                raise ExecutionError("invalid_request", "incomplete body")
            return data

        def _session(self) -> str | None:
            cookie = cookies.SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
            except cookies.CookieError:
                return None
            item = cookie.get("broker_session")
            if not item:
                return None
            with session_lock:
                return sessions.get(item.value)

        def _admin_page(self, note: str = "") -> str:
            csrf_session = self._session()
            if csrf_session is None:
                return "<h1>Unauthorized</h1>"
            profile = executor.profile()
            fields = (
                ""
                if profile is None
                else html.escape(json.dumps(profile, indent=2, ensure_ascii=False))
            )
            if profile is None:
                quota_summary = "<p>官方限額／剩餘：未知；本地安全上限：未設定。</p>"
            elif "local_safety_caps" not in profile:
                quota_summary = (
                    "<p>舊版 quotas 欄位：來源類型未標示。請顯式遷移至 "
                    "local_safety_caps 與 provider_quota_facts。</p>"
                )
            else:
                caps = profile["local_safety_caps"]
                cap_text = html.escape(
                    f"RPM {caps['rpm']}、RPD {caps['rpd']}、input TPM {caps['input_tpm']}"
                )
                evidence_rows = ""
                for fact in profile["provider_quota_facts"]:
                    dimension = html.escape(f"{fact['metric']}/{fact['window']}")
                    cells = [dimension]
                    for name in ("limit", "remaining"):
                        evidence = fact[name]
                        value = "未知" if evidence["value"] is None else str(evidence["value"])
                        detail = (
                            f"{value}（{evidence['provenance']}；"
                            f"{evidence['as_of'] or '時間未知'}；"
                            f"{evidence['source'] or '來源未知'}；"
                            f"{evidence['scope'] or '範圍未知'}；"
                            f"有效至 {evidence['valid_until'] or '未知'}）"
                        )
                        cells.append(html.escape(detail))
                    evidence_rows += (
                        "<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>"
                    )
                if not evidence_rows:
                    evidence_rows = "<tr><td colspan=3>供應商限額與剩餘：未知</td></tr>"
                quota_summary = (
                    f"<p>本地安全上限（非官方 quota）：{cap_text}。</p>"
                    "<table><tr><th>維度</th><th>供應商 limit 證據</th>"
                    f"<th>供應商 remaining 證據</th></tr>{evidence_rows}</table>"
                )
            rows = ""
            for row in executor.list_status():
                usage = row["usage"]
                usage_text = (
                    f"{usage['prompt_tokens']} / {usage['completion_tokens'] if usage['completion_tokens'] is not None else '未知'}"
                    if usage is not None
                    else "未知／待對帳"
                    if row["state"] in {"dispatched", "unknown"}
                    else "尚無"
                )
                cells = [row[k] for k in ("request_key", "state", "error_code", "updated_at")]
                cells += [usage_text, row["accounted_input_tokens"]]
                rows += (
                    "<tr>"
                    + "".join(f"<td>{html.escape(str(cell))}</td>" for cell in cells)
                    + "</tr>"
                )
            notice = html.escape(note)
            providers = "".join(
                "<tr><td>"
                + html.escape(
                    {
                        "google": "Google",
                        "cloudflare": "Cloudflare",
                        "nvidia": "NVIDIA",
                        "groq": "Groq",
                        "mistral": "Mistral",
                        "openrouter": "OpenRouter",
                        "ocrspace": "OCR.space",
                    }[model.provider]
                )
                + "</td><td>"
                + html.escape(model.model)
                + "</td><td>"
                + html.escape(
                    executor.profile_state()
                    if model.provider == "nvidia"
                    else "未知（本頁無帳號資料）"
                )
                + "</td></tr>"
                for model in MODELS.values()
            )
            project = html.escape(executor.doppler_project)
            config = html.escape(executor.doppler_config)
            secret_ref = html.escape(profile["secret_ref"] if profile else "未設定")
            return f"""<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><title>NVIDIA Broker</title>
<style>body{{font:16px system-ui;max-width:900px;margin:2rem auto;line-height:1.5}}textarea{{width:100%;height:30rem}}td,th{{padding:.4rem;border:1px solid #aaa}}table{{border-collapse:collapse}}</style>
<h1>NVIDIA Broker 管理</h1><p>{notice}</p>
<h2>供應商與狀態</h2><table><tr><th>供應商</th><th>模型</th><th>狀態</th></tr>{providers}</table>
<p>Doppler project/config：<strong>{project}/{config}</strong>；secret ref：<strong>{secret_ref}</strong>（僅名稱）。</p>
<p>僅保存金鑰識別、名稱、到期類型、範圍、驗證與配額中繼資料。實際金鑰只能在 Doppler 修改。</p>
{quota_summary}
<form method="post" action="/admin/profile"><input type="hidden" name="csrf" value="{csrf_session}">
<label>設定中繼資料（JSON）<textarea name="profile">{fields}</textarea></label><button>儲存</button></form>
<h2>手動連線測試</h2><p>按下會傳送一筆真實 NVIDIA 請求、消耗配額；可能計費。只在確認目前為免費資格時執行。</p>
<form method="post" action="/admin/test"><input type="hidden" name="csrf" value="{csrf_session}"><button>送出測試請求</button></form>
<h2>最近請求狀態</h2><p>用量為供應商已回報的 prompt/completion tokens；配額帳本 input 在 unknown 時為保留估算，完成後為實際入帳值。</p><table><tr><th>請求鍵</th><th>狀態</th><th>錯誤碼</th><th>更新時間</th><th>用量 input/output</th><th>配額帳本 input</th></tr>{rows}</table>
<form method="post" action="/admin/logout"><input type="hidden" name="csrf" value="{csrf_session}"><button>登出</button></form></html>"""

        def _form(self) -> dict[str, str]:
            if (
                self.headers.get("Content-Type", "").split(";", 1)[0]
                != "application/x-www-form-urlencoded"
            ):
                raise ExecutionError("invalid_request", "invalid form content type")
            parsed = parse_qs(self._body().decode("utf-8"), keep_blank_values=True)
            if any(len(v) != 1 for v in parsed.values()):
                raise ExecutionError("invalid_request", "duplicate form field")
            return {k: v[0] for k, v in parsed.items()}

        def _handle(self) -> None:
            path = urlsplit(self.path)
            if path.query or path.fragment or not self._host_ok():
                self._json(400, {"error": "invalid_request"})
                return
            if path.path.startswith("/v1/nvidia/"):
                header = self.headers.get("Authorization", "")
                if not secrets.compare_digest(header, "Bearer " + client_token):
                    self._json(401, {"error": "unauthorized"})
                    return
                if self.command == "GET" and path.path.startswith("/v1/nvidia/requests/"):
                    self._json(200, executor.status(path.path.removeprefix("/v1/nvidia/requests/")))
                elif self.command == "POST" and path.path == "/v1/nvidia/text":
                    if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                        raise ExecutionError("invalid_request", "JSON required")
                    data = json.loads(self._body())
                    if not isinstance(data, dict):
                        raise ExecutionError("invalid_request", "JSON object required")
                    self._json(200, executor.execute(data))
                else:
                    self._json(404, {"error": "not_found"})
                return
            if path.path == "/admin/login" and self.command == "GET":
                self._html(
                    200,
                    '<!doctype html><meta charset="utf-8"><h1>管理員登入</h1><form method="post"><input type="password" name="token" autocomplete="off"><button>登入</button></form>',
                )
                return
            if path.path == "/admin/login" and self.command == "POST":
                if not self._origin_ok():
                    self._json(403, {"error": "origin_rejected"})
                    return
                form = self._form()
                if not secrets.compare_digest(form.get("token", ""), admin_token):
                    self._json(401, {"error": "unauthorized"})
                    return
                sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                with session_lock:
                    sessions[sid] = csrf
                self._html(
                    200,
                    self._admin_page_for(),
                    f"broker_session={sid}; HttpOnly; SameSite=Strict; Path=/admin",
                )
                return
            csrf_session = self._session()
            if csrf_session is None:
                self._json(401, {"error": "unauthorized"})
                return
            if path.path == "/admin" and self.command == "GET":
                self._html(200, self._admin_page())
                return
            if self.command != "POST" or path.path not in {
                "/admin/profile",
                "/admin/test",
                "/admin/logout",
            }:
                self._json(404, {"error": "not_found"})
                return
            if not self._origin_ok():
                self._json(403, {"error": "origin_rejected"})
                return
            form = self._form()
            if not secrets.compare_digest(form.get("csrf", ""), csrf_session):
                self._json(403, {"error": "csrf_rejected"})
                return
            if path.path == "/admin/profile":
                if set(form) != {"csrf", "profile"}:
                    raise ExecutionError("invalid_request", "invalid form")
                value = json.loads(form["profile"])
                if not isinstance(value, dict):
                    raise ExecutionError("invalid_request", "profile object required")
                executor.put_profile(value)
                self._html(200, self._admin_page("已儲存中繼資料"))
            elif path.path == "/admin/test":
                if set(form) != {"csrf"}:
                    raise ExecutionError("invalid_request", "invalid form")
                result = executor.execute(
                    {
                        "request_key": "admin-test-" + secrets.token_urlsafe(16),
                        "prompt": "Reply with OK.",
                        "max_output_tokens": 8,
                    }
                )
                self._html(200, self._admin_page("連線測試：" + result["state"]))
            else:
                cookie = cookies.SimpleCookie()
                cookie.load(self.headers.get("Cookie", ""))
                session_morsel = cookie.get("broker_session")
                if session_morsel:
                    with session_lock:
                        sessions.pop(session_morsel.value, None)
                self._html(
                    200,
                    "<p>已登出</p>",
                    "broker_session=; Max-Age=0; HttpOnly; SameSite=Strict; Path=/admin",
                )

        def _admin_page_for(self) -> str:
            # Newly issued cookie has not reached the browser; render against its CSRF value.
            return '<!doctype html><meta charset="utf-8"><h1>登入成功</h1><a href="/admin">前往管理頁</a>'

        def do_GET(self) -> None:
            try:
                self._handle()
            except (ExecutionError, BrokerError) as exc:
                self._json(404 if exc.code == "not_found" else 400, {"error": exc.code})

        def do_POST(self) -> None:
            try:
                self._handle()
            except (ExecutionError, BrokerError) as exc:
                status = (
                    409
                    if exc.code == "conflict"
                    else 503
                    if exc.code in {"unavailable", "secret_unavailable", "provider_unknown"}
                    else 400
                )
                self._json(status, {"error": exc.code})
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                self._json(400, {"error": "invalid_request"})

    class BoundedServer(ThreadingHTTPServer):
        def __init__(self) -> None:
            self._slots = threading.BoundedSemaphore(16)
            super().__init__((host, port), Handler)
            self.daemon_threads = True

        def process_request(self, request: Any, client_address: Any) -> None:
            if not self._slots.acquire(blocking=False):
                try:
                    request.settimeout(1)
                    request.sendall(
                        b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                    )
                finally:
                    self.shutdown_request(request)
                return
            try:
                super().process_request(request, client_address)
            except BaseException:
                self._slots.release()
                raise

        def process_request_thread(self, request: Any, client_address: Any) -> None:
            try:
                super().process_request_thread(request, client_address)
            finally:
                self._slots.release()

    return BoundedServer()
