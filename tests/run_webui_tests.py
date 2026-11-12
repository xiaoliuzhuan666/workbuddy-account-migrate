#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本机网页版与用量核心库的自动化测试。全部走临时 fixture，不碰真实数据。"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from http.client import HTTPConnection
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from workbuddy_core.accounts import list_accounts, list_sessions
from workbuddy_core.codebuddy import collect_codebuddy
from workbuddy_core.paths import resolve_workbuddy
from workbuddy_core.rates import NOT_FOUND, load_rates
from workbuddy_core.security import (
    default_token_path,
    generate_token,
    host_is_allowed,
    load_or_create_token_file,
    redact,
    token_matches,
    forwarded_host_ok,
    origin_is_allowed,
)
from workbuddy_core.usage import collect_usage, parse_raw_usage_line, _uncached_input

PASSED = 0
FAILED = 0
FAILURES = []


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print("  [ok] %s" % name)
    else:
        FAILED += 1
        FAILURES.append((name, detail))
        print("  [FAIL] %s%s" % (name, ("  " + detail) if detail else ""))


def _uid(n="1"):
    return "11111111-1111-1111-1111-11111111111" + n


def make_home():
    tmp = Path(tempfile.mkdtemp(prefix="wb-webui-"))
    home = tmp
    wb = home / ".workbuddy"
    proj = wb / "projects" / "demo"
    snap = wb / "storage" / "skeleton"
    proj.mkdir(parents=True)
    snap.mkdir(parents=True)
    # 快照里故意塞一个假凭证字段，测试必须证明它不会被带出去
    (snap / "account-snapshot.json").write_text(json.dumps({
        "primary": {
            "uid": _uid("1"),
            "nickname": "测试账号",
            "access_token": "SUPER_SECRET_TOKEN_DO_NOT_LEAK",
        },
        "tokens": {"refresh": "ANOTHER_SECRET"},
    }, ensure_ascii=False), encoding="utf-8")
    sid_a = "sess-aaa-1111"
    sid_b = "sess-bbb-2222"
    lines_a = [
        {
            "timestamp": 1731400000000,
            "cwd": "/tmp/demo",
            "content": "用户提问原文，测试必须丢掉",
            "providerData": {
                "model": "deepseek-v4.1-flash",
                "rawUsage": {
                    "prompt_tokens": 1000,
                    "prompt_cache_hit_tokens": 200,
                    "completion_tokens": 50,
                    "completion_thinking_tokens": 10,
                    "credit": 1.5,
                    "total_tokens": 1050,
                },
            },
        },
        {
            "timestamp": 1731400001000,
            "providerData": {
                "model": "glm-5.1",
                "rawUsage": {
                    "prompt_tokens": 500,
                    "prompt_cache_hit_tokens": 600,  # hit > prompt → uncached 0
                    "completion_tokens": 20,
                    "credit": 0.5,
                },
            },
        },
    ]
    lines_b = [
        {
            "timestamp": 1731486400000,
            "providerData": {
                "model": "deepseek-v4.1-flash",
                "rawUsage": {
                    "prompt_tokens": 200,
                    "prompt_cache_hit_tokens": 0,
                    "completion_tokens": 30,
                    "credit": 2.0,
                },
            },
        }
    ]
    with (proj / (sid_a + ".jsonl")).open("w", encoding="utf-8") as f:
        for o in lines_a:
            f.write(json.dumps(o, ensure_ascii=False) + "\n")
    with (proj / (sid_b + ".jsonl")).open("w", encoding="utf-8") as f:
        for o in lines_b:
            f.write(json.dumps(o, ensure_ascii=False) + "\n")

    db = wb / "workbuddy.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            cwd TEXT,
            user_id TEXT,
            title TEXT,
            custom_title TEXT,
            status TEXT,
            created_at INTEGER,
            updated_at INTEGER,
            deleted_at INTEGER,
            model TEXT
        );
        CREATE TABLE session_usage (
            session_id TEXT PRIMARY KEY,
            used INTEGER,
            size INTEGER,
            updated_at INTEGER,
            credit_json TEXT
        );
    """)
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?)",
        (sid_a, "/tmp/demo", _uid("1"), "会话甲", None, "active", 1, 2, None, "deepseek-v4.1-flash"),
    )
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?)",
        (sid_b, "/tmp/demo", _uid("1"), "会话乙", None, "active", 3, 4, None, "deepseek-v4.1-flash"),
    )
    # 账本：会话甲与 jsonl 一致 2.0；会话乙故意写成 1.0，制造差异
    conn.execute(
        "INSERT INTO session_usage VALUES (?,?,?,?,?)",
        (sid_a, 0, 0, 1, json.dumps({"r1": 1.5, "r2": 0.5})),
    )
    conn.execute(
        "INSERT INTO session_usage VALUES (?,?,?,?,?)",
        (sid_b, 0, 0, 1, json.dumps({"r3": 1.0})),
    )
    conn.commit()
    conn.close()
    return home, sid_a, sid_b


def test_parse():
    print("\n[解析]")
    line = json.dumps({
        "timestamp": 1000,
        "content": "SECRET_PROMPT",
        "providerData": {
            "model": "m1",
            "rawUsage": {
                "prompt_tokens": 100,
                "prompt_cache_hit_tokens": 40,
                "completion_tokens": 10,
                "credit": 1.25,
            },
        },
    })
    call = parse_raw_usage_line(line)
    check("解析 rawUsage", call is not None)
    check("非缓存输入 = prompt - hit", call["uncached_input"] == 60, str(call))
    check("不带回提问原文", "SECRET_PROMPT" not in json.dumps(call))
    check("credit 保留", abs(call["credit"] - 1.25) < 1e-9)
    check("hit 大于 prompt 时非缓存为 0", _uncached_input(10, 20) == 0)
    check("相等时非缓存为 0", _uncached_input(10, 10) == 0)
    check("无 rawUsage 返回 None", parse_raw_usage_line('{"a":1}') is None)
    check("坏 JSON 返回 None", parse_raw_usage_line("{not json rawUsage") is None)


def test_usage_and_crosscheck():
    print("\n[用量与交叉校验]")
    home, sid_a, sid_b = make_home()
    os.environ["WORKBUDDY_MIGRATE_HOME"] = str(home)
    resolved = resolve_workbuddy(home=home)
    rates = load_rates(ROOT / "config" / "model_rates.json")
    report = collect_usage(resolved, rates=rates, conversion_key="topup_pack")
    check("扫描 2 个 jsonl", report.files_scanned == 2, str(report.files_scanned))
    check("3 次调用", report.calls == 3, str(report.calls))
    totals = report.totals
    check("合计积分 4.0", abs(totals["credit"] - 4.0) < 1e-9, str(totals["credit"]))
    # prompt 1000+500+200=1700, hit 200+600+0=800, uncached (800+0+200)=1000
    check("合计非缓存输入 1000", totals["uncached_input"] == 1000, str(totals["uncached_input"]))
    check("缓存命中率 800/1700", abs(totals["cache_hit_rate"] - round(800 / 1700, 6)) < 1e-9, str(totals["cache_hit_rate"]))
    # 去缓存效率 (1000+100)/4 = 275
    check("去缓存效率", abs(totals["tokens_per_credit_uncached"] - 275) < 0.1, str(totals["tokens_per_credit_uncached"]))
    check("估算费用按加量包 4*0.05=0.2", abs(totals["estimated_cny"] - 0.2) < 1e-9, str(totals["estimated_cny"]))
    models = {r["model"]: r for r in report.by_model}
    check("glm-5.1 有官方倍率 0.79", models["glm-5.1"]["official_multiplier"] == 0.79)
    check("cache_hit 大于 prompt 时命中率不超过 100%", models["glm-5.1"]["cache_hit_rate"] <= 1.0 + 1e-12, str(models["glm-5.1"]["cache_hit_rate"]))
    check("deepseek-v4.1-flash 官方倍率没读到",
          models["deepseek-v4.1-flash"]["official_multiplier_status"] == NOT_FOUND)
    check("逐 token 费率表没读到", models["deepseek-v4.1-flash"]["rate_table_status"] == NOT_FOUND)
    cc = report.cross_check
    check("交叉校验跑过", cc["status"] == "ok")
    check("jsonl 合计 4，账本 3，有差额", abs(cc["jsonl_credit_total"] - 4) < 1e-9 and abs(cc["db_credit_total"] - 3) < 1e-9)
    check("会话乙出现在差异样本里",
          any(s["session_id"] == sid_b for s in cc["mismatched_samples"]),
          str(cc["mismatched_samples"]))
    # 会话甲应接近
    check("有接近的会话", cc["close"] >= 1, str(cc))


def test_accounts_redact():
    print("\n[账号视图脱敏]")
    home, _, _ = make_home()
    resolved = resolve_workbuddy(home=home)
    data = list_accounts(resolved)
    blob = json.dumps(data, ensure_ascii=False)
    check("不泄露 access_token", "SUPER_SECRET" not in blob)
    check("不泄露 refresh", "ANOTHER_SECRET" not in blob)
    check("有当前账号", data["current_user_id"] == _uid("1"))
    check("有昵称", data["current_nickname"] == "测试账号")
    sessions = list_sessions(resolved)
    check("列出 2 个会话", sessions["total"] == 2)
    blob2 = json.dumps(sessions, ensure_ascii=False)
    check("会话列表不含提问原文", "用户提问原文" not in blob2)


def test_paths_dual():
    print("\n[路径]")
    home, _, _ = make_home()
    (home / ".workbuddy-ai").mkdir()
    (home / ".workbuddy-ai" / "marker.txt").write_text("x", encoding="utf-8")
    r = resolve_workbuddy(home=home)
    check("双版本默认国内版", r.edition == "domestic" and r.dual)
    check("有提示", "国际版" in r.notice)
    r2 = resolve_workbuddy(home=home, edition="intl")
    check("--intl 走到国际版", r2.edition == "intl" and r2.path.name == ".workbuddy-ai")
    r3 = resolve_workbuddy(home=home, explicit_dir=home / ".workbuddy-ai")
    check("--dir 优先", r3.edition == "intl")


def test_rates_and_security():
    print("\n[价格表与安全函数]")
    rates = load_rates(ROOT / "config" / "model_rates.json")
    check("加量包每积分 0.05", abs(rates.cny_per_credit("topup_pack") - 0.05) < 1e-12)
    check("来源是官方定价页", "workbuddy.cn/docs/workbuddy/Pricing" in rates.conversion("topup_pack").source)
    check("未知模型倍率没读到", rates.multiplier_for("kimi-k3.1-pro") is None)
    check("Host 127.0.0.1 允许", host_is_allowed("127.0.0.1", 18765))
    check("Host 127.0.0.1:端口 允许", host_is_allowed("127.0.0.1:18765", 18765))
    check("Host localhost 允许", host_is_allowed("localhost:18765", 18765))
    check("Host 空拒绝", not host_is_allowed("", 18765))
    check("Host example.com 拒绝", not host_is_allowed("example.com", 18765))
    check("Host 0.0.0.0 拒绝", not host_is_allowed("0.0.0.0:18765", 18765))
    check("Host 局域网拒绝", not host_is_allowed("192.168.1.2:18765", 18765))
    class H(dict):
        def get(self, k, d=""):
            return dict.get(self, k, d) or dict.get(self, k.title(), d)
    check("转发头非本机拒绝", not forwarded_host_ok(H({"X-Forwarded-Host": "evil.com"}), 18765))
    check("Origin 本机允许", origin_is_allowed("http://127.0.0.1:18765", 18765))
    check("Origin https 拒绝", not origin_is_allowed("https://127.0.0.1:18765", 18765))
    check("Origin 外站拒绝", not origin_is_allowed("http://evil.com", 18765))
    t = generate_token()
    check("令牌匹配", token_matches(t, t))
    check("令牌不匹配", not token_matches(t, t[:-1] + ("x" if t[-1] != "x" else "y")))
    check("空令牌不匹配", not token_matches(t, "") and not token_matches(t, None))
    red = redact("Authorization: Bearer %s password=hunter2" % t)
    check("脱敏去掉 Bearer", t not in red and "Bearer [redacted]" in red)
    check("脱敏去掉 password", "hunter2" not in red)


def test_codebuddy():
    print("\n[CodeBuddy]")
    empty = Path(tempfile.mkdtemp(prefix="wb-cb-empty-"))
    r = collect_codebuddy(home=empty)
    check("没有目录时没读到", r.status == NOT_FOUND)

    home, _, _ = make_home()
    cb = home / ".codebuddy" / "projects" / "p1"
    cb.mkdir(parents=True)
    (cb / "c1.jsonl").write_text(json.dumps({
        "timestamp": 1,
        "providerData": {
            "model": "hy4-free",
            "rawUsage": {
                "prompt_tokens": 100,
                "prompt_cache_hit_tokens": 10,
                "completion_tokens": 5,
                "credit": 0,
                "original_credit": 1.67,
            },
        },
    }) + "\n", encoding="utf-8")
    r2 = collect_codebuddy(home=home)
    check("读到 CodeBuddy jsonl", r2.status == "ok" and r2.totals["calls"] == 1, r2.reason)
    check("记录 original_credit", abs(r2.totals["original_credit"] - 1.67) < 1e-9, str(r2.totals))


def _start_server(home: Path, token_file: Path, rates=None):
    # 在子线程启动，端口由系统分配
    os.environ["WORKBUDDY_MIGRATE_HOME"] = str(home)
    import webui as webui_mod
    # 重置模块级 STATE
    args = webui_mod.parse_args([
        "--home", str(home),
        "--port", "0",
        "--bind", "127.0.0.1",
        "--token-file", str(token_file),
        "--rates", str(rates or (ROOT / "config" / "model_rates.json")),
    ])
    # 不要 --open
    webui_mod.setup_state(args)
    from http.server import ThreadingHTTPServer
    httpd = ThreadingHTTPServer((webui_mod.STATE.bind, 0), webui_mod.Handler)
    webui_mod.STATE.port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    # 等端口起来
    for _ in range(50):
        try:
            c = HTTPConnection("127.0.0.1", webui_mod.STATE.port, timeout=1)
            c.request("GET", "/api/health", headers={"Host": "127.0.0.1:%d" % webui_mod.STATE.port})
            r = c.getresponse()
            r.read()
            c.close()
            if r.status == 200:
                break
        except Exception:
            time.sleep(0.05)
    return httpd, webui_mod.STATE.port, webui_mod.STATE.token


def _req(port, method, path, host=None, headers=None, body=None, timeout=5):
    h = {"Host": host if host is not None else ("127.0.0.1:%d" % port)}
    if headers:
        h.update(headers)
    conn = HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            h.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=payload, headers=h)
        resp = conn.getresponse()
        data = resp.read()
        try:
            obj = json.loads(data.decode("utf-8"))
        except Exception:
            obj = {"_raw": data.decode("utf-8", "replace")}
        return resp.status, obj, data
    finally:
        conn.close()


def test_http():
    print("\n[HTTP 门卫与写保护]")
    home, sid_a, sid_b = make_home()
    token_file = home / ".config" / "workbuddy-account-migrate" / "webui-token"
    httpd, port, token = _start_server(home, token_file)
    try:
        st, obj, _ = _req(port, "GET", "/api/health")
        check("本机 Host 健康检查 200", st == 200 and obj.get("ok") is True, str((st, obj)))

        st, obj, raw = _req(port, "GET", "/api/health", host="evil.example")
        check("坏 Host 返回 403", st == 403 and obj.get("error") == "forbidden_host", str((st, obj)))
        check("坏 Host 响应不含令牌", token not in raw.decode("utf-8", "replace"))

        st, obj, raw = _req(port, "GET", "/api/accounts", host="example.com:%d" % port)
        check("accounts 坏 Host 也 403", st == 403)

        st, obj, raw = _req(port, "GET", "/api/accounts")
        blob = raw.decode("utf-8")
        check("accounts 200 且不泄露快照里的假凭证", st == 200 and "SUPER_SECRET" not in blob and "ANOTHER_SECRET" not in blob)

        st, obj, _ = _req(port, "GET", "/api/usage")
        check("usage 200", st == 200 and obj.get("ok") is True)
        check("usage 有交叉校验", "cross_check" in obj)

        st, obj, _ = _req(port, "GET", "/")
        check("首页 200", st == 200)

        # 写操作无令牌
        before = sqlite3.connect(str(home / ".workbuddy" / "workbuddy.db"))
        before_rows = list(before.execute("SELECT id, user_id FROM sessions ORDER BY id"))
        before.close()

        st, obj, raw = _req(port, "POST", "/api/sync", body={
            "source_uid": _uid("1"),
            "target_uid": _uid("2"),
            "confirm": True,
            "confirm_text": "确认同步",
        })
        check("无令牌写操作 401", st == 401, str((st, obj)))
        after = sqlite3.connect(str(home / ".workbuddy" / "workbuddy.db"))
        after_rows = list(after.execute("SELECT id, user_id FROM sessions ORDER BY id"))
        after.close()
        check("无令牌时数据库未被改写", before_rows == after_rows)

        # 有令牌但未确认
        st, obj, _ = _req(port, "POST", "/api/sync", headers={
            "Authorization": "Bearer " + token,
            "Origin": "http://127.0.0.1:%d" % port,
        }, body={
            "source_uid": _uid("1"),
            "target_uid": _uid("2"),
            "confirm": False,
            "confirm_text": "确认同步",
        })
        check("有令牌但 confirm=false 拒绝", st == 400, str((st, obj)))
        after = sqlite3.connect(str(home / ".workbuddy" / "workbuddy.db"))
        after_rows2 = list(after.execute("SELECT id, user_id FROM sessions ORDER BY id"))
        after.close()
        check("未确认时数据库未被改写", before_rows == after_rows2)

        st, obj, _ = _req(port, "POST", "/api/sync", headers={
            "Authorization": "Bearer " + token,
            "Origin": "http://127.0.0.1:%d" % port,
        }, body={
            "source_uid": _uid("1"),
            "target_uid": _uid("2"),
            "confirm": True,
            "confirm_text": "随便写",
        })
        check("确认语句不对拒绝", st == 400 and "确认" in (obj.get("message") or ""), str(obj))
        after = sqlite3.connect(str(home / ".workbuddy" / "workbuddy.db"))
        after_rows3 = list(after.execute("SELECT id, user_id FROM sessions ORDER BY id"))
        after.close()
        check("确认语句不对时数据库未被改写", before_rows == after_rows3)

        # 外站 Origin + 令牌
        st, obj, _ = _req(port, "POST", "/api/sync", headers={
            "Authorization": "Bearer " + token,
            "Origin": "http://evil.example",
        }, body={"confirm": True, "confirm_text": "确认同步", "source_uid": "a", "target_uid": "b"})
        check("外站 Origin 即使有令牌也 403", st == 403, str((st, obj)))

        # 绑定拒绝
        import webui as webui_mod
        threw = False
        try:
            webui_mod._enforce_bind("0.0.0.0")
        except SystemExit:
            threw = True
        check("拒绝绑定 0.0.0.0", threw)
        threw2 = False
        try:
            webui_mod._enforce_bind("192.168.0.10")
        except SystemExit:
            threw2 = True
        check("拒绝绑定局域网", threw2)
        check("localhost 被规范成 127.0.0.1", webui_mod._enforce_bind("localhost") == "127.0.0.1")
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_token_file():
    print("\n[令牌文件]")
    d = Path(tempfile.mkdtemp(prefix="wb-tok-"))
    path = d / "webui-token"
    t1, p1, created = load_or_create_token_file(path, create=True)
    check("首次创建令牌", created and t1 and p1 == path)
    mode = path.stat().st_mode & 0o777
    check("令牌文件权限 0600（或接近，视平台）", mode == 0o600 or sys.platform.startswith("win"), oct(mode))
    t2, _, created2 = load_or_create_token_file(path, create=True)
    check("再次调用不覆盖", (not created2) and t2 == t1)


def test_bind_cli():
    print("\n[CLI 绑定参数]")
    import subprocess
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "webui.py"), "--bind", "0.0.0.0", "--init-token"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    check("命令行 --bind 0.0.0.0 退出非 0", r.returncode != 0)
    check("提示拒绝绑定", "拒绝" in (r.stderr + r.stdout))


def main():
    print("workbuddy 本机网页版测试")
    print("仓库：%s" % ROOT)
    for fn in (
        test_parse,
        test_usage_and_crosscheck,
        test_accounts_redact,
        test_paths_dual,
        test_rates_and_security,
        test_codebuddy,
        test_token_file,
        test_http,
        test_bind_cli,
    ):
        try:
            fn()
        except Exception:
            global FAILED
            FAILED += 1
            FAILURES.append((fn.__name__, traceback.format_exc()))
            print("  [EXC] %s\n%s" % (fn.__name__, traceback.format_exc()))
    print()
    print("结果：通过 %d，失败 %d" % (PASSED, FAILED))
    for name, detail in FAILURES:
        print("--- %s ---\n%s" % (name, detail))
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
