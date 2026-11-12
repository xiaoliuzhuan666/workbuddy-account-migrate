# -*- coding: utf-8 -*-
"""从本地 jsonl 反推「1 积分对应多少 token」。

只使用本机 rawUsage，把缓存命中 / 未命中输入 / 输出分开。
不准回退到社区插件里的「5 万 token/积分」或任何写死的官方未公布汇率。
样本不足或拟合不可靠时三项都标「没读到」。

Copyright (c) 2026 Clancy
无截距三元最小二乘的思路改编自 workbuddy-usage-status（MIT）。
见 NOTICE 与 THIRD_PARTY/workbuddy-usage-status.LICENSE。
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .rates import NOT_FOUND

# 任何实现都不许把这个数当成缺省汇率返回
FORBIDDEN_DEFAULT_TOKENS_PER_CREDIT = 50000

# 与 usage-status 相近的可靠性门槛（可被配置覆盖）
DEFAULT_MIN_CALLS = 30
DEFAULT_MIN_R2 = 0.95


def _solve3(a: List[List[float]], b: List[float]) -> Optional[List[float]]:
    """3×3 线性方程，高斯消元。奇异则返回 None。"""
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(3):
        pivot = col
        for r in range(col + 1, 3):
            if abs(m[r][col]) > abs(m[pivot][col]):
                pivot = r
        if abs(m[pivot][col]) < 1e-18:
            return None
        m[col], m[pivot] = m[pivot], m[col]
        div = m[col][col]
        for c in range(col, 4):
            m[col][c] /= div
        for r in range(3):
            if r == col:
                continue
            f = m[r][col]
            for c in range(col, 4):
                m[r][c] -= f * m[col][c]
    return [m[0][3], m[1][3], m[2][3]]


def fit_credit_rates(
    rows: Sequence[Tuple[float, float, float, float]],
    min_calls: int = DEFAULT_MIN_CALLS,
    min_r2: float = DEFAULT_MIN_R2,
) -> Dict:
    """rows: (uncached_input, cache_hit, output, credit)。

    拟合 credit = a*uncached + b*hit + c*output。
    返回积分/token（a,b,c）以及每积分 token（倒数）。不可靠则 status=没读到。
    """
    n = len(rows)
    empty = {
        "status": NOT_FOUND,
        "reason": "",
        "calls": n,
        "r2": None,
        "credits_per_token": {"uncached_input": None, "cache_hit": None, "output": None},
        "tokens_per_credit": {"uncached_input": None, "cache_hit": None, "output": None},
        "credits_per_million": {"uncached_input": None, "cache_hit": None, "output": None},
    }
    if n < max(3, int(min_calls)):
        empty["reason"] = "样本不足（需要至少 %d 次带积分的调用，实际 %d）。" % (min_calls, n)
        return empty
    # 丢掉积分和 token 全为 0 的行已由调用方处理
    xtx = [[0.0, 0.0, 0.0] for _ in range(3)]
    xty = [0.0, 0.0, 0.0]
    for u, h, o, cr in rows:
        x = (u, h, o)
        for i in range(3):
            xty[i] += x[i] * cr
            for j in range(3):
                xtx[i][j] += x[i] * x[j]
    beta = _solve3(xtx, xty)
    if beta is None:
        empty["reason"] = "方程组无唯一解（可能几乎只有一种 token 组合）。"
        return empty
    if any(v is None or v != v for v in beta):  # NaN
        empty["reason"] = "拟合结果不是有效数字。"
        return empty
    # 系数必须非负才像单价；出现明显负数视为不可靠
    if any(v < -1e-12 for v in beta):
        empty["reason"] = "拟合出现负单价，不采用。"
        empty["r2"] = None
        return empty
    yhat = []
    y = []
    ss_res = 0.0
    ss_tot = 0.0
    for u, h, o, cr in rows:
        pred = beta[0] * u + beta[1] * h + beta[2] * o
        yhat.append(pred)
        y.append(cr)
        ss_res += (cr - pred) ** 2
        ss_tot += cr * cr  # 无截距用非中心 R²
    r2 = (1.0 - ss_res / ss_tot) if ss_tot > 0 else None
    if r2 is None or r2 < min_r2:
        empty["reason"] = "拟合 R² 不足（%.4f，门槛 %.2f），不把结果当成可靠费率。" % (
            r2 if r2 is not None else -1, min_r2
        )
        empty["r2"] = None if r2 is None else round(r2, 6)
        return empty

    def inv(v):
        if v is None or v <= 1e-18:
            return None
        return 1.0 / v

    tpc = {
        "uncached_input": inv(beta[0]),
        "cache_hit": inv(beta[1]),
        "output": inv(beta[2]),
    }
    # 安全网：禁止出现写死的 5 万默认值被当成结果塞进来
    for v in tpc.values():
        if v is not None and abs(v - FORBIDDEN_DEFAULT_TOKENS_PER_CREDIT) < 1e-9:
            empty["reason"] = "拒绝使用被禁止的默认汇率。"
            return empty
    return {
        "status": "ok",
        "reason": "",
        "calls": n,
        "r2": round(r2, 6),
        "credits_per_token": {
            "uncached_input": beta[0],
            "cache_hit": beta[1],
            "output": beta[2],
        },
        "tokens_per_credit": tpc,
        "credits_per_million": {
            "uncached_input": beta[0] * 1e6,
            "cache_hit": beta[1] * 1e6,
            "output": beta[2] * 1e6,
        },
    }


def infer_for_model(
    calls: Iterable[dict],
    min_calls: int = DEFAULT_MIN_CALLS,
    min_r2: float = DEFAULT_MIN_R2,
) -> Dict:
    """calls 是 parse_raw_usage_line 的结果列表。"""
    rows = []
    total_tokens = 0
    total_credit = 0.0
    for c in calls:
        cr = float(c.get("credit") or 0)
        u = int(c.get("uncached_input") or 0)
        h = int(c.get("cache_hit_tokens") or 0)
        o = int(c.get("completion_tokens") or 0)
        total_tokens += int(c.get("total_tokens") or (u + h + o))
        total_credit += cr
        if cr > 0 and (u + h + o) > 0:
            rows.append((float(u), float(h), float(o), cr))
    fitted = fit_credit_rates(rows, min_calls=min_calls, min_r2=min_r2)
    # 含缓存的合计只作参考，明确标注不能当成官方汇率
    crude = None
    if total_credit > 0 and total_tokens > 0:
        crude = total_tokens / total_credit
        if abs(crude - FORBIDDEN_DEFAULT_TOKENS_PER_CREDIT) < 1e-9:
            # 真值碰巧等于 5 万时仍展示，但标成实测合计，不是默认值
            pass
    fitted["crude_total_tokens_per_credit"] = crude
    fitted["crude_note"] = (
        "总 token / 积分（含缓存）只作参考，高缓存命中会把这个数拉得很高，不能当成官方汇率。"
        if crude is not None
        else NOT_FOUND
    )
    return fitted
