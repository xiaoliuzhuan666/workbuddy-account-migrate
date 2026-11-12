# -*- coding: utf-8 -*-
"""从本机 jsonl 和 workbuddy.db 汇总用量。

Copyright (c) 2026 Clancy
Portions adapted from workbuddy-usage-status (MIT),
https://github.com/clancy-feng/workbuddy-usage-status (v1.6.5 / 2ad519f).
See NOTICE and THIRD_PARTY/workbuddy-usage-status.LICENSE.

改编点（必须保留上述版权声明）：
  - 读 projects/*/*.jsonl 里每行 providerData.rawUsage 与 providerData.model
  - 非缓存输入 = prompt_tokens - prompt_cache_hit_tokens（前者不大于后者时记 0）
  - session_usage.credit_json 作为本地没有逐次明细时的账本
  - 按调用自己的 timestamp 归日期，不按会话首次出现日挂积分

本仓库新增：按 sessions.user_id 归属、与 credit_json 交叉校验、
缓存命中率 / 去缓存效率、按可编辑费率估算费用（费率缺失则标「没读到」）。
本模块只读，不写 usage-archive，也不改登录态。
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .paths import ResolvedDir, open_db_readonly
from .rates import NOT_FOUND, RatesConfig
from .inference import infer_for_model, DEFAULT_MIN_CALLS, DEFAULT_MIN_R2, FORBIDDEN_DEFAULT_TOKENS_PER_CREDIT
from .equivalent_cost import lookup_unit_price, equivalent_cny
from .volc_afp import afp_for_tokens, coef_for_model

# 与上游 usage-status 一致的字段名
_HIT_KEYS = (
    "prompt_cache_hit_tokens",
    "cache_read_input_tokens",
    "cached_tokens",
)


def _as_int(v, default=0) -> int:
    try:
        if v is None:
            return default
        return int(v)
    except (TypeError, ValueError):
        return default


def _as_float(v, default=0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _uncached_input(prompt: int, hit: int) -> int:
    # 上游口径：prompt 不大于 hit 时记 0
    return prompt - hit if prompt > hit else 0


def _cache_hit(ru: dict) -> int:
    for k in _HIT_KEYS:
        if k in ru and ru[k] is not None:
            return _as_int(ru[k])
    details = ru.get("prompt_tokens_details")
    if isinstance(details, dict) and details.get("cached_tokens") is not None:
        return _as_int(details.get("cached_tokens"))
    return 0


def parse_raw_usage_line(line: str) -> Optional[dict]:
    """解析一行 jsonl。没有顶层 providerData.rawUsage 则返回 None。

    不返回提问原文、content、arguments 等正文字段。
    """
    if '"rawUsage"' not in line and "'rawUsage'" not in line:
        return None
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    pd = obj.get("providerData")
    if not isinstance(pd, dict):
        return None
    ru = pd.get("rawUsage")
    if not isinstance(ru, dict):
        return None
    prompt = _as_int(ru.get("prompt_tokens"))
    completion = _as_int(ru.get("completion_tokens"))
    hit = _cache_hit(ru)
    thinking = _as_int(ru.get("completion_thinking_tokens"))
    credit = _as_float(ru.get("credit"))
    original = ru.get("original_credit")
    original_credit = _as_float(original) if original is not None else None
    model = pd.get("model") or pd.get("requestModelId") or obj.get("requestModelId") or "unknown"
    ts = _as_int(obj.get("timestamp"))
    day = ""
    if ts:
        try:
            # 时间戳按毫秒；若小到像秒则按秒
            sec = ts / 1000.0 if ts > 10_000_000_000 else float(ts)
            day = datetime.fromtimestamp(sec).strftime("%Y-%m-%d")
        except (OSError, ValueError, OverflowError):
            day = ""
    return {
        "model": str(model),
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": _as_int(ru.get("total_tokens"), prompt + completion),
        "cache_hit_tokens": hit,
        "uncached_input": _uncached_input(prompt, hit),
        "thinking_tokens": thinking,
        "credit": credit,
        "original_credit": original_credit,
        "timestamp": ts,
        "day": day,
        "request_id": str(pd.get("conversationRequestId") or ""),
    }


def iter_project_jsonl(projects_dir: Path) -> Iterable[Path]:
    if not projects_dir.is_dir():
        return
    for p in projects_dir.rglob("*.jsonl"):
        if p.is_file():
            yield p


def _empty_bucket() -> dict:
    return {
        "calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cache_hit_tokens": 0,
        "uncached_input": 0,
        "thinking_tokens": 0,
        "credit": 0.0,
        "original_credit": 0.0,
        "sessions": set(),
    }


def _add(bucket: dict, call: dict, session_id: str) -> None:
    bucket["calls"] += 1
    for k in (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cache_hit_tokens",
        "uncached_input",
        "thinking_tokens",
    ):
        bucket[k] += int(call.get(k) or 0)
    bucket["credit"] += float(call.get("credit") or 0)
    oc = call.get("original_credit")
    if oc is not None:
        bucket["original_credit"] += float(oc)
    bucket["sessions"].add(session_id)


def _metrics(bucket: dict, cny_per_credit: Optional[float], multiplier=None) -> dict:
    prompt = bucket["prompt_tokens"]
    hit = bucket["cache_hit_tokens"]
    uncached = bucket["uncached_input"]
    completion = bucket["completion_tokens"]
    credit = bucket["credit"]
    total = bucket["total_tokens"] or (prompt + completion)
    # 偶发 rawUsage 里 cache_hit 大于 prompt，命中率封顶 100%，避免出现 120%
    hit_rate = (min(hit, prompt) / prompt) if prompt else None
    # 去缓存效率：非缓存输入 + 输出，每积分多少 token
    useful = uncached + completion
    eff_uncached = (useful / credit) if credit > 0 else None
    eff_total = (total / credit) if credit > 0 else None
    cny = (credit * cny_per_credit) if (cny_per_credit is not None and credit) else None
    out = {
        "calls": bucket["calls"],
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "cache_hit_tokens": hit,
        "uncached_input": uncached,
        "thinking_tokens": bucket["thinking_tokens"],
        "credit": round(credit, 4),
        "original_credit": round(bucket["original_credit"], 4),
        "session_count": len(bucket["sessions"]),
        "cache_hit_rate": round(hit_rate, 6) if hit_rate is not None else None,
        "tokens_per_credit_uncached": round(eff_uncached, 2) if eff_uncached is not None else None,
        "tokens_per_credit_total": round(eff_total, 2) if eff_total is not None else None,
        "estimated_cny": round(cny, 4) if cny is not None else None,
        "estimated_cny_status": "ok" if cny is not None else NOT_FOUND,
        "official_multiplier": multiplier,
        "official_multiplier_status": "ok" if multiplier is not None else NOT_FOUND,
    }
    return out


def read_session_usage_credits(db_path: Path) -> Dict[str, Any]:
    """读 session_usage.credit_json，返回 {session_id: sum} 以及合计、坏行数。"""
    result = {"by_session": {}, "total": 0.0, "rows": 0, "with_credit": 0, "bad": 0, "ok": False, "error": ""}
    if not db_path.exists():
        result["error"] = "找不到数据库"
        return result
    conn = None
    try:
        conn = open_db_readonly(db_path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(session_usage)")}
        if "session_id" not in cols or "credit_json" not in cols:
            result["error"] = "session_usage 表缺少 session_id 或 credit_json"
            return result
        rows = conn.execute(
            "SELECT session_id, credit_json FROM session_usage"
        ).fetchall()
        result["rows"] = len(rows)
        result["ok"] = True
        by = {}
        total = 0.0
        for r in rows:
            sid = r[0]
            cj = r[1]
            if not cj:
                continue
            try:
                obj = json.loads(cj)
                if isinstance(obj, dict):
                    s = sum(float(v) for v in obj.values() if v is not None)
                elif isinstance(obj, (int, float)):
                    s = float(obj)
                else:
                    result["bad"] += 1
                    continue
            except (json.JSONDecodeError, TypeError, ValueError):
                result["bad"] += 1
                continue
            by[sid] = by.get(sid, 0.0) + s
            total += s
            result["with_credit"] += 1
        result["by_session"] = by
        result["total"] = total
        return result
    except sqlite3.Error as e:
        result["error"] = type(e).__name__
        return result
    finally:
        if conn is not None:
            conn.close()


def read_session_owners(db_path: Path) -> Dict[str, str]:
    if not db_path.exists():
        return {}
    conn = None
    try:
        conn = open_db_readonly(db_path)
        return {
            r[0]: (r[1] or "")
            for r in conn.execute("SELECT id, user_id FROM sessions")
        }
    except sqlite3.Error:
        return {}
    finally:
        if conn is not None:
            conn.close()


@dataclass
class UsageReport:
    source: str
    files_scanned: int
    parse_errors: int
    calls: int
    by_model: List[dict] = field(default_factory=list)
    by_session: Dict[str, dict] = field(default_factory=dict)
    totals: dict = field(default_factory=dict)
    cross_check: dict = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "files_scanned": self.files_scanned,
            "parse_errors": self.parse_errors,
            "calls": self.calls,
            "by_model": self.by_model,
            "totals": self.totals,
            "cross_check": self.cross_check,
            "notes": self.notes,
            # 会话用量单独走 sessions 接口拼，这里只给 id→数字，避免把标题重复塞两遍
            "session_usage": {
                sid: {
                    "calls": b["calls"],
                    "credit": round(b["credit"], 4),
                    "prompt_tokens": b["prompt_tokens"],
                    "completion_tokens": b["completion_tokens"],
                    "cache_hit_tokens": b["cache_hit_tokens"],
                    "uncached_input": b["uncached_input"],
                    "thinking_tokens": b["thinking_tokens"],
                    "total_tokens": b["total_tokens"],
                    "model_counts": b.get("model_counts") or {},
                    "user_id": b.get("user_id") or "",
                }
                for sid, b in self.by_session.items()
            },
        }


def collect_usage(
    resolved: ResolvedDir,
    rates: Optional[RatesConfig] = None,
    conversion_key: Optional[str] = None,
    owners: Optional[Dict[str, str]] = None,
) -> UsageReport:
    notes = []
    cny = rates.cny_per_credit(conversion_key) if rates else None
    if rates and cny is None:
        notes.append("人民币估算：所选折算方式不可用，标为「没读到」。")
    elif rates and rates.conversion(conversion_key):
        conv = rates.conversion(conversion_key)
        notes.append(
            "人民币估算按「%s」：每积分 %s 元。来源：%s（取自 %s）。"
            % (conv.label, conv.cny_per_credit, conv.source_title or conv.source, conv.retrieved_at)
        )

    by_model = defaultdict(_empty_bucket)
    by_model_calls = defaultdict(list)
    by_session: Dict[str, dict] = {}
    totals = _empty_bucket()
    files = 0
    errors = 0
    calls = 0
    for fp in iter_project_jsonl(resolved.projects_dir):
        files += 1
        sid = fp.stem
        try:
            with open(fp, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    call = parse_raw_usage_line(line)
                    if not call:
                        if '"rawUsage"' in line:
                            errors += 1
                        continue
                    calls += 1
                    _add(by_model[call["model"]], call, sid)
                    by_model_calls[call["model"]].append(call)
                    if sid not in by_session:
                        by_session[sid] = _empty_bucket()
                        by_session[sid]["model_counts"] = defaultdict(int)
                    _add(by_session[sid], call, sid)
                    by_session[sid]["model_counts"][call["model"]] += 1
                    _add(totals, call, sid)
        except OSError:
            errors += 1

    if owners is None:
        owners = read_session_owners(resolved.db_path) if resolved.db_path.exists() else {}
    for sid, b in by_session.items():
        b["user_id"] = owners.get(sid, "")
        b["model_counts"] = dict(b.get("model_counts") or {})

    ledger = read_session_usage_credits(resolved.db_path)
    cross = _cross_check(by_session, ledger)
    notes.append(cross.get("summary") or "")

    model_rows = []
    for name, b in by_model.items():
        mul = rates.multiplier_for(name) if rates else None
        row = {"model": name}
        row.update(_metrics(b, cny, mul))
        pm = rates.per_million_for(name) if rates else None
        if pm:
            est = _estimate_from_per_million(b, pm)
            row["rate_table_estimated_credit"] = est
            row["rate_table_status"] = "ok"
        else:
            row["rate_table_estimated_credit"] = None
            row["rate_table_status"] = NOT_FOUND
        min_c = DEFAULT_MIN_CALLS
        min_r = DEFAULT_MIN_R2
        if rates and rates.inference:
            min_c = int(rates.inference.get("min_calls") or min_c)
            min_r = float(rates.inference.get("min_r2") or min_r)
        inf = infer_for_model(by_model_calls.get(name) or [], min_calls=min_c, min_r2=min_r)
        row["inference"] = inf
        price = lookup_unit_price(rates.api_unit_prices if rates else {}, name) if rates else {"status": NOT_FOUND, "reason": "没有价格表"}
        eq = equivalent_cny(b["uncached_input"], b["cache_hit_tokens"], b["completion_tokens"], price, use_peak=False)
        if eq.get("status") == "ok":
            eq["period_note"] = "默认按闲时/标准价；本地记录未区分高峰。"
        row["equivalent_cny"] = eq.get("cny")
        row["equivalent_cny_status"] = eq.get("status")
        row["equivalent_cny_reason"] = eq.get("reason") or ""
        row["equivalent_cny_source"] = eq.get("source") or ""
        row["equivalent_cny_retrieved_at"] = eq.get("retrieved_at") or ""
        afp_meta = coef_for_model(rates.volc_afp if rates else {}, name) if rates else {"status": NOT_FOUND}
        # AFP 用全部输入（含缓存）按官文「输入 token」；官文未单独写缓存，注明这一点
        afp = afp_for_tokens(
            b["prompt_tokens"], b["completion_tokens"],
            afp_meta.get("input_coef"), afp_meta.get("output_coef"),
        )
        if afp.get("status") == "ok":
            afp["note"] = (afp_meta.get("note") or "") + " 官文输入系数按全部输入 token；未单独说明缓存命中。"
            afp["source"] = afp_meta.get("source")
            afp["retrieved_at"] = afp_meta.get("retrieved_at")
            afp["limited_offer"] = afp_meta.get("limited_offer")
        else:
            afp["source"] = afp_meta.get("source") or ""
            afp["retrieved_at"] = afp_meta.get("retrieved_at") or ""
        row["afp"] = afp
        model_rows.append(row)
    model_rows.sort(key=lambda r: (-r["credit"], -r["calls"], r["model"]))

    eq_sum = 0.0
    eq_ok = 0
    eq_miss = 0
    for r in model_rows:
        if r.get("equivalent_cny_status") == "ok" and r.get("equivalent_cny") is not None:
            eq_sum += r["equivalent_cny"]
            eq_ok += 1
        else:
            eq_miss += 1
    if eq_ok and not eq_miss:
        eq_total, eq_status, eq_note = round(eq_sum, 4), "ok", "全部模型都有已核对的官方单价。"
    elif eq_ok:
        eq_total, eq_status, eq_note = round(eq_sum, 4), "partial", "只加总了有官方单价的模型，其余为没读到，不是全部用量。"
    else:
        eq_total, eq_status, eq_note = None, NOT_FOUND, "没有模型能按官方 API 单价折算。"

    report = UsageReport(
        source="workbuddy-jsonl",
        files_scanned=files,
        parse_errors=errors,
        calls=calls,
        by_model=model_rows,
        by_session=by_session,
        totals=_metrics(totals, cny, None),
        cross_check=cross,
        notes=[n for n in notes if n],
    )
    report.totals["files_scanned"] = files
    report.totals["parse_errors"] = errors
    report.totals["model_count"] = len(model_rows)
    report.totals["equivalent_cny"] = eq_total
    report.totals["equivalent_cny_status"] = eq_status
    report.totals["equivalent_cny_note"] = eq_note
    return report


def _estimate_from_per_million(bucket: dict, pm: dict) -> Optional[float]:
    inp = _as_float(pm.get("input"), None) if "input" in pm else None
    hit = _as_float(pm.get("cached"), None) if "cached" in pm else None
    out = _as_float(pm.get("output"), None) if "output" in pm else None
    if inp is None or hit is None or out is None:
        return None
    # 单位：积分 / 百万 token
    credit = (
        bucket["uncached_input"] * inp
        + bucket["cache_hit_tokens"] * hit
        + bucket["completion_tokens"] * out
    ) / 1_000_000.0
    return round(credit, 4)


def _cross_check(by_session: Dict[str, dict], ledger: dict) -> dict:
    db_map = ledger.get("by_session") or {}
    jsonl_total = sum(b["credit"] for b in by_session.values())
    db_total = float(ledger.get("total") or 0)
    compared = 0
    close = 0
    mismatched = []
    only_jsonl = 0
    only_db = 0
    # 相对差 1% 或绝对差 0.05 积分以内算接近
    for sid, b in by_session.items():
        jc = float(b["credit"])
        if sid not in db_map:
            if jc > 0:
                only_jsonl += 1
            continue
        dc = float(db_map[sid])
        compared += 1
        diff = jc - dc
        scale = max(abs(jc), abs(dc), 1e-9)
        if abs(diff) <= 0.05 or abs(diff) / scale <= 0.01:
            close += 1
        else:
            if len(mismatched) < 20:
                mismatched.append({
                    "session_id": sid,
                    "jsonl_credit": round(jc, 4),
                    "db_credit": round(dc, 4),
                    "delta": round(diff, 4),
                })
    for sid in db_map:
        if sid not in by_session and db_map[sid] > 0:
            only_db += 1
    delta = jsonl_total - db_total
    if not ledger.get("ok"):
        summary = "交叉校验：数据库账本%s，已跳过。" % (ledger.get("error") or "不可用")
        status = "skipped"
    else:
        summary = (
            "交叉校验：jsonl 合计积分 %.2f，session_usage.credit_json 合计 %.2f，"
            "差额 %.2f；逐会话比对 %d 个，接近 %d，差异较大 %d；"
            "只在 jsonl 出现 %d，只在账本出现 %d。"
            % (jsonl_total, db_total, delta, compared, close, len(mismatched), only_jsonl, only_db)
        )
        status = "ok"
    return {
        "status": status,
        "summary": summary,
        "jsonl_credit_total": round(jsonl_total, 4),
        "db_credit_total": round(db_total, 4),
        "delta": round(delta, 4),
        "compared": compared,
        "close": close,
        "mismatched_count": len(mismatched) + max(0, compared - close - len(mismatched)),
        "mismatched_samples": mismatched,
        "only_jsonl": only_jsonl,
        "only_db": only_db,
        "db_rows": ledger.get("rows") or 0,
        "db_bad_rows": ledger.get("bad") or 0,
        "db_error": ledger.get("error") or "",
    }
