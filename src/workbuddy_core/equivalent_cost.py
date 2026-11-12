# -*- coding: utf-8 -*-
"""把本地 token 按官方 API 单价折成等效人民币。

主源 ModelLink（人民币、缓存价、峰谷）；辅源 models.dev；LiteLLM 只作备用。
缺任何一个必需单价就整项标「没读到」，不把缺失当成 0。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .rates import NOT_FOUND, _norm, _num_or_none


def lookup_unit_price(prices_section: Dict[str, Any], model: str) -> Dict[str, Any]:
    entries = (prices_section or {}).get("models") or []
    if isinstance(entries, dict):
        # 也允许 {name: {...}} 写法
        entries = [dict(v, id=k) if isinstance(v, dict) else v for k, v in entries.items()]
    key = _norm(model)
    for ent in entries:
        if not isinstance(ent, dict):
            continue
        names = [ent.get("id") or ""] + list(ent.get("match") or [])
        if key not in {_norm(n) for n in names if n}:
            continue
        if ent.get("status") == NOT_FOUND:
            return {"status": NOT_FOUND, "reason": ent.get("note") or "该模型单价标为没读到。", "entry": _public(ent)}
        inp = _num_or_none(ent.get("input"))
        out = _num_or_none(ent.get("output"))
        cached = _num_or_none(ent.get("cached_read"))
        if inp is None or out is None or cached is None:
            return {
                "status": NOT_FOUND,
                "reason": "输入、输出或缓存读单价有一项没读到，整项不估算。",
                "entry": _public(ent),
            }
        return {
            "status": "ok",
            "reason": "",
            "input": inp,
            "output": out,
            "cached_read": cached,
            "currency": ent.get("currency") or "CNY",
            "unit": ent.get("unit") or "CNY / million tokens",
            "peak": ent.get("peak") if isinstance(ent.get("peak"), dict) else None,
            "source": ent.get("source") or NOT_FOUND,
            "source_via": ent.get("source_via") or "",
            "retrieved_at": ent.get("retrieved_at") or "",
            "entry": _public(ent),
        }
    return {"status": NOT_FOUND, "reason": "价格表里没有与该模型精确匹配的条目。", "entry": None}


def equivalent_cny(
    uncached_input: int,
    cache_hit: int,
    output: int,
    price: Dict[str, Any],
    use_peak: bool = False,
) -> Dict[str, Any]:
    if not price or price.get("status") != "ok":
        return {
            "status": NOT_FOUND,
            "cny": None,
            "reason": (price or {}).get("reason") or "没有可用的官方单价。",
        }
    if (price.get("currency") or "CNY") != "CNY":
        # 美元价必须先有已注明的汇率；没有就不折
        fx = _num_or_none(price.get("cny_per_usd"))
        if fx is None:
            return {
                "status": NOT_FOUND,
                "cny": None,
                "reason": "单价不是人民币，且没有已注明来源的汇率，不折算。",
            }
    else:
        fx = 1.0
    inp, out, cached = price["input"], price["output"], price["cached_read"]
    peak = price.get("peak") if use_peak else None
    period = "peak" if use_peak else "offpeak_or_standard"
    if use_peak:
        if not isinstance(peak, dict):
            return {"status": NOT_FOUND, "cny": None, "reason": "没有已核对的高峰价，不假装用闲时代替高峰。"}
        inp = _num_or_none(peak.get("input"))
        out = _num_or_none(peak.get("output"))
        cached = _num_or_none(peak.get("cached_read"))
        if inp is None or out is None or cached is None:
            return {"status": NOT_FOUND, "cny": None, "reason": "高峰价不完整。"}
        period = "peak"
    cny = (uncached_input * inp + cache_hit * cached + output * out) / 1_000_000.0 * fx
    return {
        "status": "ok",
        "cny": round(cny, 6),
        "reason": "",
        "period": period,
        "currency": "CNY",
        "source": price.get("source"),
        "retrieved_at": price.get("retrieved_at"),
    }


def _public(ent: dict) -> dict:
    keys = (
        "id", "match", "currency", "unit", "input", "output", "cached_read",
        "cached_write", "peak", "source", "source_via", "retrieved_at",
        "status", "note",
    )
    return {k: ent.get(k) for k in keys if k in ent}


def official_cost_by_models(model_usages, prices_section) -> Dict[str, Any]:
    """按官方单价把一组「模型 + 三项 token」折成人民币。

    每个模型单独查找单价；缺价的模型标「没读到」，不把缺失当成 0，
    也不计入合计。返回 status：全部有价为 ok，部分有价为 partial，全部没有为 没读到。
    """
    from .rates import NOT_FOUND

    priced = []
    missing = []
    by_model = []
    total = 0.0
    for item in model_usages or []:
        if not isinstance(item, dict):
            continue
        model = str(item.get("model") or "").strip() or "unknown"
        uncached = int(item.get("uncached_input") or 0)
        hit = int(item.get("cache_hit_tokens") or 0)
        output = int(item.get("completion_tokens") or 0)
        price = lookup_unit_price(prices_section, model)
        eq = equivalent_cny(uncached, hit, output, price, use_peak=False)
        row = {
            "model": model,
            "uncached_input": uncached,
            "cache_hit_tokens": hit,
            "completion_tokens": output,
            "status": eq.get("status"),
            "cny": eq.get("cny"),
            "reason": eq.get("reason") or "",
            "source": eq.get("source") or "",
            "retrieved_at": eq.get("retrieved_at") or "",
            "period": eq.get("period") or "",
        }
        by_model.append(row)
        if eq.get("status") == "ok" and eq.get("cny") is not None:
            priced.append(model)
            total += float(eq["cny"])
        else:
            missing.append(model)
    if priced and not missing:
        status, cny, note = "ok", round(total, 6), ""
    elif priced:
        status, cny = "partial", round(total, 6)
        note = "%d 个会话模型没读到价格，未计入" % len(missing)
    else:
        status, cny = NOT_FOUND, None
        if missing:
            note = "%d 个会话模型没读到价格，未计入" % len(missing)
        else:
            note = "没有可用的官方单价。"
    return {
        "status": status,
        "cny": cny,
        "currency": "CNY",
        "period": "offpeak_or_standard",
        "period_note": "默认按闲时/标准价；本地记录未区分高峰。",
        "note": note,
        "missing_models": missing,
        "missing_count": len(missing),
        "priced_count": len(priced),
        "by_model": by_model,
    }
