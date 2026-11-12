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
from workbuddy_core.inference import (
    fit_credit_rates, infer_for_model, FORBIDDEN_DEFAULT_TOKENS_PER_CREDIT,
)
from workbuddy_core.volc_afp import afp_for_tokens, coef_for_model
from workbuddy_core.equivalent_cost import lookup_unit_price, equivalent_cny, official_cost_by_models, official_cost_by_calls, official_cost_for_call
from workbuddy_core.rates import NOT_FOUND

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



def test_inference_afp_equivalent():
    print("\n[反推 / AFP / 等效人民币]")
    # 已知公式 credit = 1e-5 * u + 1e-6 * h + 4e-5 * o，40 个点，应可靠拟合
    rows = []
    # 三项要各自独立变化，避免共线导致拟合出负单价
    for i in range(40):
        u = 800.0 + (i % 7) * 900
        h = 200.0 + (i % 5) * 2500
        o = 40.0 + (i % 11) * 70
        cr = 1e-5 * u + 1e-6 * h + 4e-5 * o
        rows.append((u, h, o, cr))
    fitted = fit_credit_rates(rows, min_calls=30, min_r2=0.95)
    check("样本足够时三项拟合成功", fitted["status"] == "ok", str(fitted))
    tpc = fitted.get("tokens_per_credit") or {}
    # 1/1e-5 = 100000, 1/1e-6=1e6, 1/4e-5=25000
    check("未命中输入每积分约 10 万", tpc.get("uncached_input") and abs(tpc["uncached_input"] - 100000) / 100000 < 0.01, str(tpc))
    check("输出每积分约 2.5 万", tpc.get("output") and abs(tpc["output"] - 25000) / 25000 < 0.01, str(tpc))
    check("反推结果不是禁止的 5 万默认值", all(
        v is None or abs(v - FORBIDDEN_DEFAULT_TOKENS_PER_CREDIT) > 1 for v in tpc.values()
    ), str(tpc))

    few = infer_for_model(
        [{"uncached_input": 100, "cache_hit_tokens": 10, "completion_tokens": 5, "credit": 1, "total_tokens": 115}],
        min_calls=30,
    )
    check("样本不足标没读到", few["status"] == NOT_FOUND)
    check("样本不足不回退 5 万", few["tokens_per_credit"]["uncached_input"] != FORBIDDEN_DEFAULT_TOKENS_PER_CREDIT)
    check("样本不足三项都是空", all(v is None for v in few["tokens_per_credit"].values()))

    # 官方示例：(50000*2.5 + 500*2.5)/10000 = 12.625
    ex = afp_for_tokens(50000, 500, 2.5, 2.5)
    check("AFP 官方示例 12.625", ex["status"] == "ok" and abs(ex["afp"] - 12.625) < 1e-9, str(ex))
    miss = afp_for_tokens(100, 10, None, 0.5)
    check("缺系数 AFP 没读到", miss["status"] == NOT_FOUND and miss["afp"] is None)

    rates = load_rates(ROOT / "config" / "model_rates.json")
    coef = coef_for_model(rates.volc_afp, "deepseek-v4.1-flash")
    check("官文核对过的 v4.1-flash 系数 0.875", coef["status"] == "ok" and abs(coef["input_coef"] - 0.875) < 1e-12)
    coef2 = coef_for_model(rates.volc_afp, "some-unknown-model")
    check("未知模型 AFP 没读到", coef2["status"] == NOT_FOUND)

    pr = lookup_unit_price(rates.api_unit_prices, "deepseek-v4-flash")
    check("v4-flash 有闲时人民币单价", pr["status"] == "ok" and pr["input"] == 1 and pr["cached_read"] == 0.02)
    # 100 万未命中 + 0 缓存 + 0 输出 = 1 元
    eq = equivalent_cny(1_000_000, 0, 0, pr, use_peak=False)
    check("等效人民币闲时 100 万输入 = 1 元", eq["status"] == "ok" and abs(eq["cny"] - 1) < 1e-9, str(eq))
    eqp = equivalent_cny(1_000_000, 0, 0, pr, use_peak=True)
    check("高峰 100 万输入 = 2 元", eqp["status"] == "ok" and abs(eqp["cny"] - 2) < 1e-9, str(eqp))
    pr_glm = lookup_unit_price(rates.api_unit_prices, "glm-5.3-flash")
    check("glm-5.3-flash 已对照智谱官文填写", pr_glm["status"] == "ok" and pr_glm["input"] == 0.8 and pr_glm["output"] == 2.8 and pr_glm["cached_read"] == 0.23, str(pr_glm))
    check("glm-5.3-flash 来源是智谱官方定价页", "docs.bigmodel.cn" in (pr_glm.get("source") or ""), str(pr_glm))
    pr_hy = lookup_unit_price(rates.api_unit_prices, "hy4-free")
    check("hy4-free 官方单价没读到（不写成 0）", pr_hy["status"] == NOT_FOUND)
    eqm = equivalent_cny(1000, 100, 10, pr_hy)
    check("没读到单价时等效费用不是 0 而是没读到", eqm["status"] == NOT_FOUND and eqm["cny"] is None)
    pr41 = lookup_unit_price(rates.api_unit_prices, "deepseek-v4.1-flash")
    check("v4.1-flash 已对照 DeepSeek 官文填写", pr41["status"] == "ok" and pr41["input"] == 1 and pr41["output"] == 4 and pr41["cached_read"] == 0.02, str(pr41))
    check("v4.1-flash 来源是 DeepSeek 官方定价页", "api-docs.deepseek.com" in (pr41.get("source") or ""), str(pr41))

    # 在完整 fixture 上：glm-5.1 无 API 价；用量接口应带 inference 字段
    home, _, _ = make_home()
    resolved = resolve_workbuddy(home=home)
    report = collect_usage(resolved, rates=rates, conversion_key="topup_pack")
    models = {r["model"]: r for r in report.by_model}
    check("用量行带 inference", "inference" in models["glm-5.1"])
    check("fixture 样本不足时 inference 没读到", models["glm-5.1"]["inference"]["status"] == NOT_FOUND)
    check("fixture 不出现 5 万默认", models["glm-5.1"]["inference"]["tokens_per_credit"]["uncached_input"] is None)
    check("glm-5.1 已对照智谱官文有分档单价", models["glm-5.1"]["equivalent_cny_status"] == "ok" and models["glm-5.1"]["equivalent_cny"] is not None, str(models["glm-5.1"]))
    expected_glm51 = (0 * 6 + 600 * 1.3 + 20 * 24) / 1_000_000.0
    check("glm-5.1 按低于 32K 档", abs(models["glm-5.1"]["equivalent_cny"] - expected_glm51) < 1e-9, str(models["glm-5.1"]))
    ds = models["deepseek-v4.1-flash"]
    check("fixture 里 v4.1-flash 等效人民币已算出", ds["equivalent_cny_status"] == "ok" and ds["equivalent_cny"] is not None, str(ds))
    # fixture 两次调用都在北京时间 16:26 工作日，属高峰。高峰价为闲时两倍。
    # sess-a: 800 uncached + 200 hit + 50 out；sess-b: 200 + 0 + 30
    expected = (1000 * 2 + 200 * 0.04 + 80 * 8) / 1_000_000.0
    check("v4.1-flash 等效人民币按高峰价（调用时间在高峰窗口）", abs(ds["equivalent_cny"] - expected) < 1e-9, str((ds["equivalent_cny"], expected)))
    check("v4.1-flash 高峰合计单独给出", ds.get("equivalent_cny_peak") is not None and abs(ds["equivalent_cny_peak"] - expected) < 1e-9, str(ds))
    total_eq = expected + expected_glm51
    check("全部有价时总等效为 ok", report.totals.get("equivalent_cny_status") == "ok", str(report.totals))
    check("总等效是高峰 DeepSeek 加标准 glm-5.1", abs(report.totals.get("equivalent_cny") - round(total_eq, 4)) < 1e-9, str((report.totals.get("equivalent_cny"), total_eq)))


def test_prices_api_and_refresh_skeleton():
    print("\n[价格 API 与刷新骨架]")
    home, _, _ = make_home()
    token_file = home / ".config" / "workbuddy-account-migrate" / "webui-token"
    httpd, port, token = _start_server(home, token_file)
    try:
        st, obj, raw = _req(port, "GET", "/api/prices")
        check("GET /api/prices 200", st == 200 and obj.get("ok"))
        blob = raw.decode("utf-8")
        check("价格接口含 ModelLink 政策", "ModelLink" in blob)
        check("价格接口不含令牌内容", token not in blob)
        models = ((obj.get("api_unit_prices") or {}).get("models") or [])
        flash = next((m for m in models if m.get("id") == "glm-5.3-flash"), None)
        check("glm-5.3-flash 在表里且已核对", flash and flash.get("status") == "ok" and flash.get("input") == 0.8)
        hy = next((m for m in models if m.get("id") == "hy4-free"), None)
        check("hy4-free 在表里且没读到", hy and hy.get("status") == NOT_FOUND)
        v41 = next((m for m in models if m.get("id") == "deepseek-v4.1-flash"), None)
        check("v4.1-flash 在表里且已核对", v41 and v41.get("status") == "ok" and v41.get("input") == 1)
        st, obj, _ = _req(port, "GET", "/api/compare?model=glm-5.1")
        check("GET /api/compare 200", st == 200 and obj.get("ok"))
        # 无令牌刷新
        st, obj, _ = _req(port, "POST", "/api/prices/refresh", body={})
        check("无令牌刷新 401", st == 401)
        st, obj, _ = _req(port, "POST", "/api/prices/refresh", headers={
            "Authorization": "Bearer " + token,
            "Origin": "http://127.0.0.1:%d" % port,
        }, body={"confirm": True})
        check("有令牌时刷新骨架 501（留给练手 3）", st == 501, str((st, obj)))
    finally:
        httpd.shutdown()
        httpd.server_close()



def test_session_official_cost():
    print("\n[会话官方价]")
    # 纯函数：三项拆分、缺价不计 0
    rates = load_rates(ROOT / "config" / "model_rates.json")
    prices = rates.api_unit_prices
    one = official_cost_by_models([
        {"model": "deepseek-v4.1-flash", "uncached_input": 1_000_000, "cache_hit_tokens": 0, "completion_tokens": 0},
    ], prices)
    check("单一有价模型 status=ok", one["status"] == "ok" and abs(one["cny"] - 1) < 1e-9, str(one))

    glm = official_cost_by_models([
        {"model": "glm-5.3-flash", "uncached_input": 1_000_000, "cache_hit_tokens": 1_000_000, "completion_tokens": 1_000_000},
    ], prices)
    expected_glm = 0.8 + 0.23 + 2.8
    check("glm-5.3-flash 三项拆分", glm["status"] == "ok" and abs(glm["cny"] - expected_glm) < 1e-9, str(glm))

    miss = official_cost_by_models([
        {"model": "hy4-free", "uncached_input": 1000, "cache_hit_tokens": 100, "completion_tokens": 50},
    ], prices)
    check("hy4-free 官方价没读到且费用不是 0", miss["status"] == NOT_FOUND and miss["cny"] is None, str(miss))
    check("缺价模型出现在 missing_models", "hy4-free" in miss["missing_models"], str(miss))

    mixed = official_cost_by_models([
        {"model": "deepseek-v4.1-flash", "uncached_input": 1000, "cache_hit_tokens": 200, "completion_tokens": 50},
        {"model": "kimi-k3-2", "uncached_input": 0, "cache_hit_tokens": 600, "completion_tokens": 20},
    ], prices)
    expected_mix = (1000 * 1 + 200 * 0.02 + 50 * 4) / 1_000_000.0
    check("混合模型 status=partial", mixed["status"] == "partial", str(mixed))
    check("混合模型只计入有价部分", abs(mixed["cny"] - expected_mix) < 1e-9, str(mixed))
    check("混合模型注明未计入", "没读到价格，未计入" in (mixed["note"] or "") and mixed["missing_count"] == 1, str(mixed))
    check("缺价模型是 kimi-k3-2", "kimi-k3-2" in mixed["missing_models"], str(mixed))

    flashx = official_cost_by_models([
        {"model": "glm-5.3-flashx", "uncached_input": 1_000_000, "cache_hit_tokens": 1_000_000, "completion_tokens": 1_000_000},
    ], prices)
    check("glm-5.3-flashx 三项拆分 2+0.57+7", flashx["status"] == "ok" and abs(flashx["cny"] - 9.57) < 1e-9, str(flashx))

    # 完整 fixture：两次 DeepSeek 调用在高峰，glm-5.1 用标准低档
    home, sid_a, sid_b = make_home()
    resolved = resolve_workbuddy(home=home)
    report = collect_usage(resolved, rates=rates)
    su = report.as_dict()["session_usage"]
    a = su[sid_a]
    b = su[sid_b]
    expected_a_ds = (800 * 2 + 200 * 0.04 + 50 * 8) / 1_000_000.0
    expected_a_glm = (0 * 6 + 600 * 1.3 + 20 * 24) / 1_000_000.0
    expected_a = expected_a_ds + expected_a_glm
    check("会话甲官方价为 ok（两个模型都有价）", a["official_cny_status"] == "ok", str(a))
    check("会话甲按高峰 DeepSeek 加标准 glm-5.1", abs(a["official_cny"] - expected_a) < 1e-9, str(a))
    check("会话甲高峰与标准拆开", abs((a.get("official_cny_peak") or 0) - expected_a_ds) < 1e-9 and abs((a.get("official_cny_standard") or 0) - expected_a_glm) < 1e-9, str(a))
    expected_saved_a = 200 * (2 - 0.04) / 1_000_000.0 + 600 * (6 - 1.3) / 1_000_000.0
    check("会话甲算出缓存命中省下的费用", a.get("cache_hit_saved") is not None and abs(a["cache_hit_saved"] - expected_saved_a) < 1e-9, str(a))
    expected_b = (200 * 2 + 0 + 30 * 8) / 1_000_000.0
    check("会话乙官方价为高峰", b["official_cny_status"] == "ok" and abs(b["official_cny"] - expected_b) < 1e-9, str(b))
    check("会话有首次和最近调用时间", bool(a.get("first_call_at")) and bool(a.get("last_call_at")), str(a))

    # HTTP：/api/sessions 合计覆盖全部会话，带高峰拆分和缓存命中
    token_file = home / ".config" / "workbuddy-account-migrate" / "webui-token"
    httpd, port, token = _start_server(home, token_file)
    try:
        st, obj, raw = _req(port, "GET", "/api/sessions?limit=1")
        check("GET /api/sessions 200", st == 200, str((st, obj)))
        blob = raw.decode("utf-8")
        check("会话接口不含假凭证", "SUPER_SECRET" not in blob and "ANOTHER_SECRET" not in blob)
        check("limit=1 时列表只有 1 条", len(obj.get("sessions") or []) == 1, str(len(obj.get("sessions") or [])))
        totals = obj.get("official_totals") or {}
        ut = obj.get("usage_totals") or {}
        check("合计范围是全部会话", totals.get("scope") == "all_sessions" and ut.get("scope") == "all_sessions", str((ut, totals)))
        check("limit=1 时合计仍覆盖 2 个会话", ut.get("session_count") == 2 and totals.get("session_count") == 2, str((ut, totals)))
        check("接口合计含总 token", totals.get("total_tokens") == 1800, str(totals))
        check("接口合计缓存命中和命中率", totals.get("cache_hit_tokens") == 800 and totals.get("cache_hit_rate") is not None, str(totals))
        expected_all = expected_a + expected_b
        check("接口合计官方价覆盖全部会话", totals.get("official_cny_status") == "ok" and abs(totals["official_cny"] - expected_all) < 1e-9, str(totals))
        check("接口合计带高峰拆分", abs((totals.get("official_cny_peak") or 0) - (expected_a_ds + expected_b)) < 1e-9, str(totals))
        expected_saved = expected_saved_a  # 会话乙无缓存命中
        check("接口合计含缓存命中省下的费用", totals.get("cache_hit_saved") is not None and abs(totals["cache_hit_saved"] - expected_saved) < 1e-9, str(totals))
        check("合计注明按调用时间区分高峰", "高峰" in (totals.get("official_cny_period_note") or ""), str(totals))
        check("时间线按日返回", isinstance((obj.get("timeline") or {}).get("by_day"), list) and (obj["timeline"]["by_day"]), str(obj.get("timeline")))

        st, calls, raw2 = _req(port, "GET", "/api/sessions/%s/calls?limit=50" % sid_a)
        check("GET 会话调用明细 200", st == 200 and calls.get("ok") and calls.get("total") == 2, str(calls))
        blob2 = raw2.decode("utf-8")
        check("调用明细不含提问原文", "用户提问原文" not in blob2 and "SECRET" not in blob2)
        rows = calls.get("calls") or []
        check("调用有北京时间", all(r.get("time_local") and r.get("timezone") == "Asia/Shanghai" for r in rows), str(rows))
        check("DeepSeek 调用标为高峰", any(r.get("model") == "deepseek-v4.1-flash" and r.get("period") == "peak" for r in rows), str(rows))
        ds_row = next(r for r in rows if r.get("model") == "deepseek-v4.1-flash")
        check("调用明细有缓存命中和省下的费用", ds_row.get("cache_hit_tokens") == 200 and ds_row.get("cache_hit_saved") is not None, str(ds_row))

        st, tl, _ = _req(port, "GET", "/api/timeline?granularity=day")
        check("GET /api/timeline 200", st == 200 and tl.get("ok") and (tl.get("rows") or []), str(tl))
        day = (tl["rows"][0])
        check("时间线含缓存命中和官方价", day.get("cache_hit_tokens") is not None and day.get("official_cny") is not None, str(day))

        st, prices, _ = _req(port, "GET", "/api/prices")
        check("价格接口含 WorkBuddy 积分规则", st == 200 and (prices.get("workbuddy_credit_rules") or {}).get("exact_formula_status") == NOT_FOUND, str(prices.get("workbuddy_credit_rules")))
        check("价格接口含高峰窗口说明", "09:00" in str((prices.get("deepseek_peak_windows") or {}).get("windows")), str(prices.get("deepseek_peak_windows")))
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_peak_hours_and_cache_savings():
    print("\n[高峰窗口与缓存命中省下的费用]")
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from workbuddy_core.peak_hours import classify_timestamp, is_deepseek_peak, PEAK_RULE

    tz = ZoneInfo("Asia/Shanghai")

    def ms(s):
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
        return int(dt.timestamp() * 1000)

    cases = [
        ("2026-11-10 09:00:00", "peak"),
        ("2026-11-10 11:59:59", "peak"),
        ("2026-11-10 12:00:00", "offpeak"),
        ("2026-11-10 13:59:59", "offpeak"),
        ("2026-11-10 14:00:00", "peak"),
        ("2026-11-10 17:59:59", "peak"),
        ("2026-11-10 18:00:00", "offpeak"),
        ("2026-11-14 10:00:00", "offpeak"),  # 周六
        ("2026-05-01 10:00:00", "offpeak"),  # 劳动节周五
        ("2026-05-04 10:00:00", "peak"),     # 调休放假周一，按字面不是法定节假日
        ("2026-10-10 10:00:00", "offpeak"),  # 调休上班周六，仍按周末
        ("2026-02-16 10:00:00", "offpeak"),  # 除夕
    ]
    for s, exp in cases:
        got = classify_timestamp(ms(s))["period"]
        check("高峰判断 %s 为 %s" % (s, exp), got == exp, "%s -> %s" % (s, got))
    check("高峰规则带来源", "api-docs.deepseek.com" in PEAK_RULE["source"] and PEAK_RULE["timezone"] == "Asia/Shanghai")

    rates = load_rates(ROOT / "config" / "model_rates.json")
    prices = rates.api_unit_prices
    # 100 万缓存命中、闲时：实际 0.02 元；若按未命中 1 元计会多花 0.98
    off = official_cost_for_call({
        "model": "deepseek-v4.1-flash",
        "uncached_input": 0,
        "cache_hit_tokens": 1_000_000,
        "completion_tokens": 0,
        "prompt_tokens": 1_000_000,
        "timestamp": ms("2026-11-14 10:00:00"),
    }, prices)
    check("闲时缓存命中官方价 0.02", off["status"] == "ok" and abs(off["cny"] - 0.02) < 1e-9, str(off))
    check("闲时缓存命中省 0.98", off.get("cache_hit_saved") is not None and abs(off["cache_hit_saved"] - 0.98) < 1e-9, str(off))
    check("周末调用标闲时", off.get("period") == "offpeak", str(off))

    pk = official_cost_for_call({
        "model": "deepseek-v4.1-flash",
        "uncached_input": 0,
        "cache_hit_tokens": 1_000_000,
        "completion_tokens": 0,
        "prompt_tokens": 1_000_000,
        "timestamp": ms("2026-11-10 10:00:00"),
    }, prices)
    check("高峰缓存命中官方价 0.04", pk["status"] == "ok" and abs(pk["cny"] - 0.04) < 1e-9, str(pk))
    check("高峰缓存命中省 1.96", abs(pk["cache_hit_saved"] - 1.96) < 1e-9, str(pk))
    check("工作日上午标高峰", pk.get("period") == "peak", str(pk))

    # 没有时间戳：按闲时并标明
    unk = official_cost_for_call({
        "model": "deepseek-v4.1-flash",
        "uncached_input": 1_000_000,
        "cache_hit_tokens": 0,
        "completion_tokens": 0,
        "timestamp": None,
    }, prices)
    check("无时间戳按闲时 1 元", unk["status"] == "ok" and abs(unk["cny"] - 1) < 1e-9 and unk["period"] == "offpeak_unknown_time", str(unk))

    # glm-5.1 分档：32K 边界
    low = official_cost_for_call({
        "model": "glm-5.1",
        "uncached_input": 1_000_000,
        "cache_hit_tokens": 0,
        "completion_tokens": 0,
        "prompt_tokens": 31999,
        "timestamp": ms("2026-11-10 10:00:00"),
    }, prices)
    high = official_cost_for_call({
        "model": "glm-5.1",
        "uncached_input": 1_000_000,
        "cache_hit_tokens": 0,
        "completion_tokens": 0,
        "prompt_tokens": 32000,
        "timestamp": ms("2026-11-10 10:00:00"),
    }, prices)
    check("glm-5.1 低于 32K 用 6 元档", low["status"] == "ok" and abs(low["cny"] - 6) < 1e-9, str(low))
    check("glm-5.1 达到 32K 用 8 元档", high["status"] == "ok" and abs(high["cny"] - 8) < 1e-9, str(high))
    check("glm-5.1 没有高峰价，标标准", low.get("period") == "standard", str(low))

    # 应扣积分：官方费率没读到
    home, sid_a, _ = make_home()
    resolved = resolve_workbuddy(home=home)
    report = collect_usage(resolved, rates=rates)
    a = report.as_dict()["session_usage"][sid_a]
    check("官方未公布逐 token 积分费率时规则应扣为没读到", a.get("wb_rule_expected_status") == NOT_FOUND and a.get("wb_rule_expected_credit") is None, str(a))
    cr = report.credit_rules or {}
    check("规则摘要带来源", "workbuddy.cn" in (cr.get("source") or "") and cr.get("exact_formula_status") == NOT_FOUND, str(cr))


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
        test_inference_afp_equivalent,
        test_prices_api_and_refresh_skeleton,
        test_session_official_cost,
        test_peak_hours_and_cache_savings,
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
