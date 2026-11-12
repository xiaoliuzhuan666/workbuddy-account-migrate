# -*- coding: utf-8 -*-
"""本机网页服务的安全底线：Host 白名单、令牌、日志脱敏。

令牌只写在本机文件里，接口响应和日志里只报路径，不报令牌内容。
"""
from __future__ import annotations

import hmac
import os
import re
import secrets
from pathlib import Path
from typing import Optional, Set, Tuple

# 日志里这些键的值一律打成 [redacted]
_SECRET_KEYS = re.compile(
    r"(token|password|secret|authorization|api[_-]?key|lic_?code|"
    r"access[_-]?token|refresh[_-]?token|credential|cookie|set-cookie)",
    re.I,
)
_BEARER = re.compile(r"(Bearer\s+)(\S+)", re.I)
_LONG_HEX = re.compile(r"\b[0-9a-fA-F]{32,}\b")


def allowed_hosts(port: int) -> Set[str]:
    port = int(port)
    names = ("127.0.0.1", "localhost")
    out = set()
    for n in names:
        out.add(n)
        out.add("%s:%d" % (n, port))
    return out


def host_is_allowed(host: Optional[str], port: int) -> bool:
    if not host:
        return False
    h = host.strip().lower()
    # 有的客户端会给 Host: 127.0.0.1:08765，去掉前导零再比一次太苛刻，按原值比即可
    return h in allowed_hosts(port)


def forwarded_host_ok(headers, port: int) -> bool:
    """若带了 X-Forwarded-Host / X-Forwarded-For，也必须是本机，否则拒绝。

    本服务不该坐在反向代理后面；有转发头通常说明请求不是浏览器直接打到 127.0.0.1。
    """
    if headers is None:
        return True
    def _get(name):
        if hasattr(headers, "get"):
            return headers.get(name) or headers.get(name.title()) or ""
        return ""
    for key in ("X-Forwarded-Host", "X-Forwarded-For", "X-Forwarded-Server", "Forwarded"):
        raw = _get(key).strip()
        if not raw:
            continue
        first = raw.split(",")[0].strip()
        # Forwarded: for=..., host=localhost:port
        if key == "Forwarded":
            m = re.search(r"host\s*=\s*\"?([^;,\"]+)", first, re.I)
            if m:
                first = m.group(1).strip()
            else:
                # 只有 for= 也拒绝，因为我们不接受被代理
                return False
        # X-Forwarded-For 是 IP，允许 127.0.0.1 / ::1 / localhost
        if key == "X-Forwarded-For":
            ip = first.split(":")[0].strip("[]")
            if ip not in ("127.0.0.1", "::1", "localhost"):
                return False
            continue
        if not host_is_allowed(first, port):
            # host 可能不带端口
            if not host_is_allowed(first.split(":")[0], port):
                return False
    return True


def origin_is_allowed(origin: Optional[str], port: int) -> bool:
    """写操作时，若带了 Origin / Referer，必须是本机页面。"""
    if not origin:
        return True  # 非浏览器客户端（curl）可以不带 Origin，靠令牌把关
    origin = origin.strip()
    # 只要 scheme://host[:port]
    m = re.match(r"^(https?://)([^/]+)", origin, re.I)
    if not m:
        return False
    if m.group(1).lower() != "http://":
        return False
    return host_is_allowed(m.group(2), port)


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def default_token_path(home: Optional[Path] = None) -> Path:
    if home is None:
        env = (os.environ.get("WORKBUDDY_MIGRATE_HOME") or "").strip()
        base = Path(env).expanduser().resolve() if env else Path.home().resolve()
    else:
        base = Path(home).expanduser().resolve()
    return base / ".config" / "workbuddy-account-migrate" / "webui-token"


def load_or_create_token_file(path: Path, create: bool = True) -> Tuple[str, Path, bool]:
    """返回 (token, path, created)。token 仅供进程内校验，不要打印到网页。

    文件权限尽量设为 0600，目录 0700。已有文件则只读，不覆盖。
    """
    path = Path(path)
    if path.exists():
        data = path.read_text(encoding="utf-8").strip()
        if not data:
            raise RuntimeError("令牌文件是空的：%s" % path)
        # 只取第一行，避免误把注释写进去
        token = data.splitlines()[0].strip()
        if not token:
            raise RuntimeError("令牌文件第一行是空的：%s" % path)
        return token, path, False
    if not create:
        raise FileNotFoundError("找不到令牌文件：%s" % path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    token = generate_token()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(str(path), flags, 0o600)
    try:
        os.write(fd, (token + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return token, path, True


def token_matches(expected: str, given: Optional[str]) -> bool:
    if not expected or not given:
        return False
    a = expected.encode("utf-8")
    b = given.encode("utf-8")
    if len(a) != len(b):
        # compare_digest 在长度不同时会抛或直接 False，先统一处理为 False
        # 仍做一次比较以保持耗时接近，但绝不根据长度短路泄露
        dummy = b"x" * len(a)
        hmac.compare_digest(a, dummy)
        return False
    return hmac.compare_digest(a, b)


def extract_bearer(headers) -> Optional[str]:
    if headers is None:
        return None
    raw = headers.get("Authorization") or headers.get("X-Webui-Token") or ""
    if not raw:
        return None
    raw = raw.strip()
    m = re.match(r"^Bearer\s+(\S+)$", raw, re.I)
    if m:
        return m.group(1)
    if raw.lower().startswith("bearer"):
        return None
    # X-Webui-Token: 直接是令牌
    if headers.get("X-Webui-Token"):
        return raw
    return None


def redact(text: str) -> str:
    """日志和错误信息脱敏。不改变业务数据本身。"""
    if not text:
        return text
    s = _BEARER.sub(r"\1[redacted]", text)

    def _kv(m):
        return m.group(1) + "[redacted]"

    s = re.sub(
        r"([A-Za-z0-9_-]*(?:token|password|secret|api[_-]?key|lic_?code)"
        r"\s*[:=]\s*)([^\s,&;\"']+)",
        _kv,
        s,
        flags=re.I,
    )
    s = _LONG_HEX.sub("[redacted-hex]", s)
    return s


def mask_uid(uid: Optional[str]) -> str:
    u = (uid or "").strip()
    if not u:
        return ""
    if len(u) <= 12:
        return u[:2] + "…"
    return u[:8] + "…" + u[-4:]
