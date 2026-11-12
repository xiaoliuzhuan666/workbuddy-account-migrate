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
            "tiers": ent.get("tiers") if isinstance(ent.get("tiers"), list) else None,
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
    uncached_cny = uncached_input * inp / 1_000_000.0 * fx
    cache_hit_cny = cache_hit * cached / 1_000_000.0 * fx
    output_cny = output * out / 1_000_000.0 * fx
    cny = uncached_cny + cache_hit_cny + output_cny
    # 若这批缓存命中按「未命中输入」单价计，会多花多少：只在有价时计算，不把缺价当 0
    cache_hit_saved = cache_hit * (inp - cached) / 1_000_000.0 * fx
    return {
        "status": "ok",
        "cny": round(cny, 6),
        "cny_uncached": round(uncached_cny, 6),
        "cny_cache_hit": round(cache_hit_cny, 6),
        "cny_output": round(output_cny, 6),
        "cache_hit_saved": round(cache_hit_saved, 6),
        "unit_input": inp,
        "unit_cached_read": cached,
        "unit_output": out,
        "reason": "",
        "period": period,
        "currency": "CNY",
        "source": price.get("source"),
        "retrieved_at": price.get("retrieved_at"),
    }


def _public(ent: dict) -> dict:
    keys = (
        "id", "match", "currency", "unit", "input", "output", "cached_read",
        "cached_write", "peak", "tiers", "source", "source_via", "retrieved_at",
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


def _tier_matches(tier: dict, prompt_tokens: Optional[int]) -> bool:
    if prompt_tokens is None:
        return False
    mn = tier.get("min_prompt")
    mx = tier.get("max_prompt_exclusive")
    if mn is not None and prompt_tokens < int(mn):
        return False
    if mx is not None and prompt_tokens >= int(mx):
        return False
    return True


def price_for_tokens(price: Dict[str, Any], prompt_tokens: Optional[int] = None, use_peak: bool = False) -> Dict[str, Any]:
    """在已查到的单价上，按输入长度选档、再按是否高峰取数。缺项则没读到。"""
    if not price or price.get("status") != "ok":
        return {
            "status": NOT_FOUND,
            "reason": (price or {}).get("reason") or "没有可用的官方单价。",
        }
    base = dict(price)
    tiers = price.get("tiers") if isinstance(price.get("tiers"), list) else None
    if tiers:
        chosen = None
        if prompt_tokens is not None:
            for t in tiers:
                if isinstance(t, dict) and _tier_matches(t, prompt_tokens):
                    chosen = t
                    break
        if chosen is None:
            return {
                "status": NOT_FOUND,
                "reason": "该模型按输入长度分档计价，但这次调用没有可用的输入长度，不估。",
                "source": price.get("source"),
                "retrieved_at": price.get("retrieved_at"),
            }
        for k in ("input", "output", "cached_read"):
            n = _num_or_none(chosen.get(k))
            if n is None:
                return {
                    "status": NOT_FOUND,
                    "reason": "分档单价不完整。",
                    "source": price.get("source"),
                    "retrieved_at": price.get("retrieved_at"),
                }
            base[k] = n
        # 分档条目一般没有单独高峰价；若有则沿用
        if isinstance(chosen.get("peak"), dict):
            base["peak"] = chosen["peak"]
        base["tier_note"] = chosen.get("note") or ""
        base["tier_prompt"] = prompt_tokens
    return base


def official_cost_for_call(call: Dict[str, Any], prices_section: Dict[str, Any]) -> Dict[str, Any]:
    """按单次调用的时间戳和三项 token 计算官方价。

    有高峰价且时间落在 DeepSeek 官方高峰窗口时用高峰价；
    有高峰价但没有时间戳时用闲时价，并标明 unknown。
    没有高峰价的模型用标准价，period 为 standard。
    """
    from .peak_hours import classify_timestamp

    if not isinstance(call, dict):
        return {"status": NOT_FOUND, "cny": None, "reason": "调用记录无效。", "period": "unknown"}
    model = str(call.get("model") or "").strip() or "unknown"
    uncached = int(call.get("uncached_input") or 0)
    hit = int(call.get("cache_hit_tokens") or 0)
    output = int(call.get("completion_tokens") or 0)
    prompt = call.get("prompt_tokens")
    try:
        prompt_n = int(prompt) if prompt is not None else uncached + hit
    except (TypeError, ValueError):
        prompt_n = uncached + hit
    price = lookup_unit_price(prices_section, model)
    classified = classify_timestamp(call.get("timestamp"))
    has_peak = isinstance((price or {}).get("peak"), dict) and price.get("status") == "ok"
    if has_peak:
        if classified["period"] == "peak":
            use_peak, billed = True, "peak"
        else:
            # unknown 和 offpeak 都用闲时，unknown 另外注明
            use_peak, billed = False, ("offpeak" if classified["period"] == "offpeak" else "offpeak_unknown_time")
    else:
        use_peak, billed = False, "standard"
    priced = price_for_tokens(price, prompt_n, use_peak=use_peak)
    eq = equivalent_cny(uncached, hit, output, priced, use_peak=use_peak)
    return {
        "model": model,
        "status": eq.get("status"),
        "cny": eq.get("cny"),
        "cny_uncached": eq.get("cny_uncached"),
        "cny_cache_hit": eq.get("cny_cache_hit"),
        "cny_output": eq.get("cny_output"),
        "cache_hit_saved": eq.get("cache_hit_saved") if eq.get("status") == "ok" else None,
        "reason": eq.get("reason") or "",
        "source": eq.get("source") or priced.get("source") or "",
        "retrieved_at": eq.get("retrieved_at") or priced.get("retrieved_at") or "",
        "period": billed,
        "window": classified["period"],
        "time_local": classified.get("local") or "",
        "day": classified.get("day") or "",
        "hour": classified.get("hour") or "",
        "period_reason": classified.get("reason") or "",
        "timezone": "Asia/Shanghai",
        "uncached_input": uncached,
        "cache_hit_tokens": hit,
        "completion_tokens": output,
        "prompt_tokens": prompt_n,
    }


def official_cost_by_calls(calls, prices_section) -> Dict[str, Any]:
    """按每次调用自己的时间戳累计官方价。缺价模型不把缺失当成 0。"""
    from collections import defaultdict
    from .peak_hours import PEAK_RULE

    priced_models = set()
    missing_models = []
    missing_set = set()
    by_model_acc = defaultdict(lambda: {
        "uncached_input": 0, "cache_hit_tokens": 0, "completion_tokens": 0,
        "cny": 0.0, "cny_peak": 0.0, "cny_offpeak": 0.0, "cny_standard": 0.0,
        "cny_uncached": 0.0, "cny_cache_hit": 0.0, "cny_output": 0.0, "cache_hit_saved": 0.0,
        "saved_calls": 0,
        "calls": 0, "calls_peak": 0, "calls_offpeak": 0, "calls_standard": 0,
        "calls_unknown_time": 0, "status": NOT_FOUND, "reason": "", "source": "", "retrieved_at": "",
    })
    total = 0.0
    cny_peak = 0.0
    cny_offpeak = 0.0
    cny_standard = 0.0
    cny_uncached = 0.0
    cny_cache_hit = 0.0
    cny_output = 0.0
    cache_hit_saved = 0.0
    saved_calls = 0
    calls_peak = 0
    calls_offpeak = 0
    calls_standard = 0
    calls_unknown = 0
    priced_calls = 0
    unknown_time_priced = 0

    for call in calls or []:
        row = official_cost_for_call(call, prices_section)
        model = row["model"]
        acc = by_model_acc[model]
        acc["uncached_input"] += row["uncached_input"]
        acc["cache_hit_tokens"] += row["cache_hit_tokens"]
        acc["completion_tokens"] += row["completion_tokens"]
        acc["calls"] += 1
        if row.get("source"):
            acc["source"] = row["source"]
        if row.get("retrieved_at"):
            acc["retrieved_at"] = row["retrieved_at"]
        if row.get("window") == "unknown":
            acc["calls_unknown_time"] += 1
            calls_unknown += 1
        if row.get("status") == "ok" and row.get("cny") is not None:
            cny = float(row["cny"])
            acc["cny"] += cny
            acc["status"] = "ok"
            priced_models.add(model)
            priced_calls += 1
            total += cny
            if row.get("cny_uncached") is not None:
                acc["cny_uncached"] += float(row["cny_uncached"])
                cny_uncached += float(row["cny_uncached"])
            if row.get("cny_cache_hit") is not None:
                acc["cny_cache_hit"] += float(row["cny_cache_hit"])
                cny_cache_hit += float(row["cny_cache_hit"])
            if row.get("cny_output") is not None:
                acc["cny_output"] += float(row["cny_output"])
                cny_output += float(row["cny_output"])
            if row.get("cache_hit_saved") is not None:
                acc["cache_hit_saved"] += float(row["cache_hit_saved"])
                cache_hit_saved += float(row["cache_hit_saved"])
                acc["saved_calls"] += 1
                saved_calls += 1
            period = row.get("period") or ""
            if period == "peak":
                acc["cny_peak"] += cny
                acc["calls_peak"] += 1
                cny_peak += cny
                calls_peak += 1
            elif period.startswith("offpeak"):
                acc["cny_offpeak"] += cny
                acc["calls_offpeak"] += 1
                cny_offpeak += cny
                calls_offpeak += 1
                if period == "offpeak_unknown_time":
                    unknown_time_priced += 1
            else:
                acc["cny_standard"] += cny
                acc["calls_standard"] += 1
                cny_standard += cny
                calls_standard += 1
        else:
            acc["reason"] = row.get("reason") or acc["reason"]
            if model not in missing_set:
                missing_set.add(model)
                missing_models.append(model)
    by_model = []
    for name, acc in by_model_acc.items():
        by_model.append({
            "model": name,
            "uncached_input": acc["uncached_input"],
            "cache_hit_tokens": acc["cache_hit_tokens"],
            "completion_tokens": acc["completion_tokens"],
            "calls": acc["calls"],
            "status": acc["status"] if name in priced_models else NOT_FOUND,
            "cny": round(acc["cny"], 6) if name in priced_models else None,
            "cny_peak": round(acc["cny_peak"], 6),
            "cny_offpeak": round(acc["cny_offpeak"], 6),
            "cny_standard": round(acc["cny_standard"], 6),
            "cny_uncached": round(acc["cny_uncached"], 6) if name in priced_models else None,
            "cny_cache_hit": round(acc["cny_cache_hit"], 6) if name in priced_models else None,
            "cny_output": round(acc["cny_output"], 6) if name in priced_models else None,
            "cache_hit_saved": round(acc["cache_hit_saved"], 6) if name in priced_models else None,
            "calls_peak": acc["calls_peak"],
            "calls_offpeak": acc["calls_offpeak"],
            "calls_standard": acc["calls_standard"],
            "calls_unknown_time": acc["calls_unknown_time"],
            "reason": "" if name in priced_models else (acc["reason"] or "没读到价格"),
            "source": acc["source"],
            "retrieved_at": acc["retrieved_at"],
        })
    if priced_models and not missing_models:
        status, cny, note = "ok", round(total, 6), ""
    elif priced_models:
        status, cny = "partial", round(total, 6)
        note = "%d 个会话模型没读到价格，未计入" % len(missing_models)
    else:
        status, cny = NOT_FOUND, None
        if missing_models:
            note = "%d 个会话模型没读到价格，未计入" % len(missing_models)
        else:
            note = "没有可用的官方单价。"
    period_note = (
        "官方价按每次调用自己的时间戳，用北京时间对照 DeepSeek 官方高峰窗口计价："
        "周一至周五 09:00–12:00、14:00–18:00（右开区间），不含中国法定节假日；"
        "周末及法定节假日全天闲时。没有高峰价的模型用标准价。"
        "没有时间戳的调用按闲时计，并单独标明。"
    )
    return {
        "status": status,
        "cny": cny,
        "cny_peak": round(cny_peak, 6) if priced_models else None,
        "cny_offpeak": round(cny_offpeak, 6) if priced_models else None,
        "cny_standard": round(cny_standard, 6) if priced_models else None,
        "cny_uncached": round(cny_uncached, 6) if priced_models else None,
        "cny_cache_hit": round(cny_cache_hit, 6) if priced_models else None,
        "cny_output": round(cny_output, 6) if priced_models else None,
        "cache_hit_saved": round(cache_hit_saved, 6) if priced_models else None,
        "cache_hit_saved_calls": saved_calls,
        "calls_peak": calls_peak,
        "calls_offpeak": calls_offpeak,
        "calls_standard": calls_standard,
        "calls_unknown_time": calls_unknown,
        "priced_calls": priced_calls,
        "unknown_time_priced_calls": unknown_time_priced,
        "currency": "CNY",
        "period": "by_call_timestamp",
        "period_note": period_note,
        "peak_rule": PEAK_RULE,
        "note": note,
        "missing_models": missing_models,
        "missing_count": len(missing_models),
        "priced_count": len(priced_models),
        "by_model": by_model,
    }
