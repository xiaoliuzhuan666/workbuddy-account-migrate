#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 本机网页服务。

只绑定 127.0.0.1，校验 Host，写操作（账号同步）必须带本机令牌并二次确认。
默认只读浏览，不改登录态。不要把服务绑到局域网或公网。
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from workbuddy_core.accounts import list_accounts, list_sessions  # noqa: E402
from workbuddy_core.codebuddy import collect_codebuddy  # noqa: E402
from workbuddy_core.migrate_bridge import (  # noqa: E402
    ClientsStillRunning,
    SyncRefused,
    preflight,
    run_sync,
)
from workbuddy_core.paths import resolve_workbuddy  # noqa: E402
from workbuddy_core.rates import default_rates_path, load_rates  # noqa: E402
from workbuddy_core.security import (  # noqa: E402
    default_token_path,
    extract_bearer,
    forwarded_host_ok,
    host_is_allowed,
    load_or_create_token_file,
    origin_is_allowed,
    redact,
    token_matches,
)
from workbuddy_core.usage import collect_usage  # noqa: E402

CONFIRM_TEXT = "确认同步"
DEFAULT_PORT = 18765
STATIC_DIR = ROOT / "webui"


class AppState:
    def __init__(self):
        self.home = None  # Path
        self.explicit_dir = None
        self.default_edition = "auto"
        self.token = ""
        self.token_path = None
        self.rates_path = default_rates_path(ROOT)
        self.bind = "127.0.0.1"
        self.port = DEFAULT_PORT
        self._usage_cache = {}

    def resolved(self, edition=None):
        return resolve_workbuddy(
            home=self.home,
            explicit_dir=self.explicit_dir,
            edition=edition or self.default_edition,
        )


STATE = AppState()


def _json_bytes(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "WorkBuddyWebui/0.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        try:
            msg = fmt % args
        except Exception:
            msg = fmt
        sys.stderr.write("%s - %s\n" % (self.address_string(), redact(str(msg))))

    def _security_gate(self, writing=False) -> bool:
        host = self.headers.get("Host", "")
        if not host_is_allowed(host, STATE.port):
            self._send_json(403, {
                "ok": False,
                "error": "forbidden_host",
                "message": "Host 不被允许。本服务只接受 127.0.0.1 或 localhost。",
            })
            return False
        if not forwarded_host_ok(self.headers, STATE.port):
            self._send_json(403, {
                "ok": False,
                "error": "forbidden_forwarded",
                "message": "拒绝带有非本机转发头的请求。",
            })
            return False
        if writing:
            origin = self.headers.get("Origin") or ""
            referer = self.headers.get("Referer") or ""
            if origin and not origin_is_allowed(origin, STATE.port):
                self._send_json(403, {
                    "ok": False,
                    "error": "forbidden_origin",
                    "message": "Origin 不被允许。",
                })
                return False
            if referer and not origin_is_allowed(referer, STATE.port):
                self._send_json(403, {
                    "ok": False,
                    "error": "forbidden_referer",
                    "message": "Referer 不被允许。",
                })
                return False
            given = extract_bearer(self.headers)
            if not token_matches(STATE.token, given):
                self._send_json(401, {
                    "ok": False,
                    "error": "unauthorized",
                    "message": "写操作需要本机令牌。令牌在启动时写到本机文件，请看终端里打印的路径，不要把令牌发到网上。",
                    "token_path": str(STATE.token_path) if STATE.token_path else "",
                })
                return False
        return True

    def _send_json(self, code, obj):
        body = _json_bytes(obj)
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'")
        self.send_header("X-Frame-Options", "DENY")
        self._end()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _end(self):
        self.send_header("Connection", "close")
        self.end_headers()

    def _query(self):
        parsed = urlparse(self.path)
        return parsed.path, {k: v[-1] if v else "" for k, v in parse_qs(parsed.query, keep_blank_values=True).items()}

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1_000_000:
            return None, "请求体太大"
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, "JSON 无法解析"
        if not isinstance(data, dict):
            return None, "JSON 必须是对象"
        return data, ""

    def do_GET(self):
        if not self._security_gate(writing=False):
            return
        path, q = self._query()
        try:
            if path == "/api/health":
                return self._send_json(200, {"ok": True, "bind": STATE.bind, "port": STATE.port})
            if path == "/api/meta":
                return self._send_json(200, self._meta(q))
            if path == "/api/accounts":
                resolved = STATE.resolved(q.get("edition"))
                data = list_accounts(resolved)
                data["ok"] = True
                data["edition"] = resolved.as_dict()
                return self._send_json(200, data)
            if path == "/api/sessions":
                return self._send_json(200, self._sessions(q))
            if path == "/api/usage":
                return self._send_json(200, self._usage(q))
            if path == "/api/rates":
                rates = load_rates(STATE.rates_path)
                return self._send_json(200, {"ok": True, "rates": rates.as_public_dict()})
            if path == "/api/sync/preflight":
                resolved = STATE.resolved(q.get("edition"))
                pf = preflight(STATE.home, resolved.path)
                pf["edition"] = resolved.as_dict()
                return self._send_json(200, pf)
            if path in ("/", "/index.html"):
                return self._send_static("index.html")
            if path.startswith("/static/"):
                return self._send_static(path[len("/static/"):])
            # 也允许直接 /styles.css /app.js
            name = path.lstrip("/")
            if name in ("styles.css", "app.js"):
                return self._send_static(name)
            self._send_json(404, {"ok": False, "error": "not_found"})
        except Exception as e:
            self._handle_err(e)

    def do_POST(self):
        if not self._security_gate(writing=True):
            return
        path, q = self._query()
        if path != "/api/sync":
            self._send_json(404, {"ok": False, "error": "not_found"})
            return
        body, err = self._read_json_body()
        if err:
            self._send_json(400, {"ok": False, "error": "bad_request", "message": err})
            return
        resolved = STATE.resolved(body.get("edition") or q.get("edition"))
        try:
            result = run_sync(
                home=STATE.home,
                wb_dir=resolved.path,
                source_uid=str(body.get("source_uid") or ""),
                target_uid=str(body.get("target_uid") or ""),
                confirm=bool(body.get("confirm")),
                confirm_text=str(body.get("confirm_text") or ""),
                expected_confirm=CONFIRM_TEXT,
            )
            code = 200 if result.get("ok") else 400
            return self._send_json(code, result)
        except ClientsStillRunning as e:
            self._send_json(409, {
                "ok": False,
                "error": "clients_running",
                "message": "WorkBuddy 客户端还在运行，必须先退出再同步（和命令行工具同一条规则）。网页不会在客户端开着时继续。",
                "clients": e.clients,
            })
        except SyncRefused as e:
            self._send_json(400, {"ok": False, "error": "refused", "message": str(e)})
        except Exception as e:
            self._handle_err(e)

    def do_PUT(self):
        # 所有未开放的写方法一律走写门（要令牌），再 405，避免误以为能写
        if not self._security_gate(writing=True):
            return
        self._send_json(405, {"ok": False, "error": "method_not_allowed"})

    def do_DELETE(self):
        if not self._security_gate(writing=True):
            return
        self._send_json(405, {"ok": False, "error": "method_not_allowed"})

    def _handle_err(self, e: Exception):
        # 不把堆栈和路径里可能出现的敏感信息原样丢给浏览器；终端打脱敏后的类型
        sys.stderr.write("webui error: %s\n" % redact(type(e).__name__ + ": " + str(e)))
        self._send_json(500, {
            "ok": False,
            "error": "internal",
            "message": "服务内部出错（%s）。详情只写在启动这个服务的终端里，且已脱敏。"
            % type(e).__name__,
        })

    def _meta(self, q):
        resolved = STATE.resolved(q.get("edition"))
        return {
            "ok": True,
            "bind": STATE.bind,
            "port": STATE.port,
            "token_required_for_writes": True,
            "token_path": str(STATE.token_path) if STATE.token_path else "",
            "confirm_text": CONFIRM_TEXT,
            "rates_path": str(STATE.rates_path),
            "edition": resolved.as_dict(),
            "read_only_default": True,
        }

    def _sessions(self, q):
        resolved = STATE.resolved(q.get("edition"))
        limit = int(q.get("limit") or 200)
        offset = int(q.get("offset") or 0)
        data = list_sessions(
            resolved,
            user_id=q.get("user_id") or None,
            limit=limit,
            offset=offset,
        )
        # 附上该页会话的用量
        usage = collect_usage(resolved, rates=load_rates(STATE.rates_path))
        su = usage.as_dict().get("session_usage") or {}
        for s in data.get("sessions") or []:
            u = su.get(s["id"]) or {}
            s["usage"] = u
        data["usage_totals"] = {
            "files_scanned": usage.files_scanned,
            "calls": usage.calls,
        }
        return data

    def _usage(self, q):
        resolved = STATE.resolved(q.get("edition"))
        conv = q.get("conversion") or None
        rates = load_rates(STATE.rates_path)
        usage = collect_usage(resolved, rates=rates, conversion_key=conv)
        cb = collect_codebuddy(home=STATE.home, rates=rates, conversion_key=conv)
        payload = usage.as_dict()
        payload["ok"] = True
        payload["edition"] = resolved.as_dict()
        payload["rates"] = rates.as_public_dict()
        payload["conversion"] = (rates.conversion(conv).as_dict() if rates.conversion(conv) else None)
        payload["codebuddy"] = cb.as_dict()
        # 会话明细很大，用量接口只保留汇总；完整会话走 /api/sessions
        payload.pop("session_usage", None)
        return payload

    def _send_static(self, name: str):
        # 禁止路径穿越
        name = name.lstrip("/").replace("\\", "/")
        if ".." in name.split("/"):
            self._send_json(403, {"ok": False, "error": "forbidden"})
            return
        fp = (STATIC_DIR / name).resolve()
        if not str(fp).startswith(str(STATIC_DIR.resolve())) or not fp.is_file():
            self._send_json(404, {"ok": False, "error": "not_found"})
            return
        ctype = mimetypes.guess_type(str(fp))[0] or "application/octet-stream"
        if fp.suffix == ".js":
            ctype = "application/javascript; charset=utf-8"
        elif fp.suffix == ".css":
            ctype = "text/css; charset=utf-8"
        elif fp.suffix == ".html":
            ctype = "text/html; charset=utf-8"
        data = fp.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
            "form-action 'self'; base-uri 'self'",
        )
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self._end()
        try:
            self.wfile.write(data)
        except BrokenPipeError:
            pass


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="WorkBuddy 本机网页版（只绑定 127.0.0.1）")
    p.add_argument("--dir", type=str, help="WorkBuddy 数据目录（优先级最高）")
    p.add_argument("--intl", action="store_true", help="默认看国际版 ~/.workbuddy-ai")
    p.add_argument("--home", type=str, help="覆盖家目录（测试用，等同 WORKBUDDY_MIGRATE_HOME）")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help="端口，默认 %d" % DEFAULT_PORT)
    p.add_argument("--bind", type=str, default="127.0.0.1", help="只能是 127.0.0.1 或 localhost")
    p.add_argument("--token-file", type=str, help="本机令牌文件路径（默认 ~/.config/workbuddy-account-migrate/webui-token）")
    p.add_argument("--rates", type=str, help="价格表 JSON 路径")
    p.add_argument("--init-token", action="store_true", help="只生成令牌文件然后退出，不启动服务")
    p.add_argument("--open", action="store_true", help="启动后尝试打开本机浏览器")
    return p.parse_args(argv)


def _enforce_bind(bind: str) -> str:
    b = (bind or "").strip().lower()
    if b in ("127.0.0.1", "localhost"):
        return "127.0.0.1"
    raise SystemExit(
        "拒绝绑定到 %s。本服务只允许 127.0.0.1，不能绑到 0.0.0.0 或局域网地址。" % bind
    )


def setup_state(args) -> None:
    if args.home:
        os.environ["WORKBUDDY_MIGRATE_HOME"] = str(Path(args.home).expanduser().resolve())
        STATE.home = Path(os.environ["WORKBUDDY_MIGRATE_HOME"])
    else:
        env = (os.environ.get("WORKBUDDY_MIGRATE_HOME") or "").strip()
        STATE.home = Path(env).resolve() if env else None
    STATE.explicit_dir = Path(args.dir).expanduser().resolve() if args.dir else None
    STATE.default_edition = "intl" if args.intl else "auto"
    STATE.bind = _enforce_bind(args.bind)
    STATE.port = int(args.port)
    if STATE.port < 0 or STATE.port > 65535:
        raise SystemExit("端口不合法")
    # 0 表示由系统分配临时端口，启动后再回写实际端口
    if args.rates:
        STATE.rates_path = Path(args.rates).expanduser().resolve()
    token_path = Path(args.token_file).expanduser() if args.token_file else default_token_path(STATE.home)
    token, token_path, created = load_or_create_token_file(token_path, create=True)
    STATE.token = token
    STATE.token_path = token_path
    # 只报路径，不打印令牌本身
    if created:
        print("已生成本机写操作令牌，保存在：")
    else:
        print("使用已有的本机写操作令牌，文件在：")
    print("  %s" % token_path)
    print("查看令牌：在本机终端执行  cat %s" % token_path)
    print("只读浏览不需要令牌；同步账号时要在网页里粘贴，并输入「%s」。" % CONFIRM_TEXT)


def serve(args=None):
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass
    args = args or parse_args()
    setup_state(args)
    if args.init_token:
        return 0
    # 再确认一遍价格表能读
    try:
        load_rates(STATE.rates_path)
    except FileNotFoundError as e:
        print("警告：%s（用量还能看，费用估算会标「没读到」）" % e)
    httpd = ThreadingHTTPServer((STATE.bind, STATE.port), Handler)
    # 若 port=0，记下实际端口再做 Host 校验
    STATE.port = httpd.server_address[1]
    url = "http://127.0.0.1:%d/" % STATE.port
    print()
    print("本机网页版已启动（只监听 127.0.0.1）")
    print("  浏览器打开：%s" % url)
    print("  不要把这个地址发到网上，也不要改绑到 0.0.0.0。")
    print("  按 Ctrl+C 停止。")
    print()
    if getattr(args, "open", False):
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
    return 0


def main(argv=None):
    args = parse_args(argv)
    try:
        return serve(args)
    except KeyboardInterrupt:
        print("\n已停止")
        return 130


if __name__ == "__main__":
    sys.exit(main() or 0)
