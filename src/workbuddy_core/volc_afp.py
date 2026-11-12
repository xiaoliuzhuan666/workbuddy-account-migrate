# -*- coding: utf-8 -*-
"""火山方舟 Agent Plan 的 AFP 估算。

官方公式（文本生成）：
    AFP = (输入 token × 输入系数 + 输出 token × 输出系数) / 10000
来源必须写在 rates.json 里，没核实到的模型标「没读到」，不编造系数。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .rates import NOT_FOUND, _norm


def afp_for_tokens(
    input_tokens: int,
    output_tokens: int,
    input_coef: Optional[float],
    output_coef: Optional[float],
) -> Dict[str, Any]:
    if input_coef is None or output_coef is None:
        return {
            "status": NOT_FOUND,
            "afp": None,
            "reason": "该模型没有已核对的 AFP 系数。",
        }
    try:
        cin = float(input_coef)
        cout = float(output_coef)
    except (TypeError, ValueError):
        return {"status": NOT_FOUND, "afp": None, "reason": "系数不是数字。"}
    afp = (int(input_tokens) * cin + int(output_tokens) * cout) / 10000.0
    return {"status": "ok", "afp": afp, "reason": "", "input_coef": cin, "output_coef": cout}


def coef_for_model(afp_section: Dict[str, Any], model: str) -> Dict[str, Any]:
    models = (afp_section or {}).get("models") or {}
    key = _norm(model)
    meta = {
        "source": (afp_section or {}).get("source") or NOT_FOUND,
        "source_title": (afp_section or {}).get("source_title") or "",
        "retrieved_at": (afp_section or {}).get("retrieved_at") or "",
        "formula": (afp_section or {}).get("formula") or "(input * cin + output * cout) / 10000",
    }
    for name, val in models.items():
        if _norm(name) != key:
            # 也看 match 列表
            if isinstance(val, dict):
                matches = [_norm(x) for x in (val.get("match") or [])]
                if key not in matches:
                    continue
            else:
                continue
        if not isinstance(val, dict) or val.get("status") == NOT_FOUND:
            return {"status": NOT_FOUND, "input_coef": None, "output_coef": None, "model": name, **meta}
        cin = val.get("input_coef")
        cout = val.get("output_coef")
        if cin in (None, NOT_FOUND) or cout in (None, NOT_FOUND):
            return {"status": NOT_FOUND, "input_coef": None, "output_coef": None, "model": name, **meta,
                    "note": val.get("note") or ""}
        return {
            "status": "ok",
            "input_coef": float(cin),
            "output_coef": float(cout),
            "model": name,
            "note": val.get("note") or "",
            "limited_offer": bool(val.get("limited_offer")),
            **meta,
        }
    return {"status": NOT_FOUND, "input_coef": None, "output_coef": None, "model": model, **meta}
