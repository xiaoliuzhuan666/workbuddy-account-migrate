# -*- coding: utf-8 -*-
"""可编辑的价格表。没核实到的价格写「没读到」，计算时不算进估算。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

NOT_FOUND = "没读到"


@dataclass
class Conversion:
    key: str
    label: str
    cny_per_credit: Optional[float]
    source: str
    source_title: str
    retrieved_at: str
    note: str
    available: bool

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "cny_per_credit": self.cny_per_credit if self.available else None,
            "source": self.source,
            "source_title": self.source_title,
            "retrieved_at": self.retrieved_at,
            "note": self.note,
            "available": self.available,
            "status": "ok" if self.available else NOT_FOUND,
        }


@dataclass
class RatesConfig:
    path: Path
    raw: Dict[str, Any]
    updated_at: str
    default_conversion: str
    conversions: Dict[str, Conversion] = field(default_factory=dict)
    official_multipliers: Dict[str, Any] = field(default_factory=dict)
    per_million: Dict[str, Any] = field(default_factory=dict)
    api_unit_prices: Dict[str, Any] = field(default_factory=dict)
    volc_afp: Dict[str, Any] = field(default_factory=dict)
    inference: Dict[str, Any] = field(default_factory=dict)
    pricing_policy: Dict[str, Any] = field(default_factory=dict)
    notes: str = ""

    def conversion(self, key: Optional[str] = None) -> Optional[Conversion]:
        k = key or self.default_conversion
        return self.conversions.get(k)

    def cny_per_credit(self, key: Optional[str] = None) -> Optional[float]:
        c = self.conversion(key)
        if c and c.available and c.cny_per_credit is not None:
            return float(c.cny_per_credit)
        return None

    def multiplier_for(self, model: str) -> Optional[float]:
        models = (self.official_multipliers or {}).get("models") or {}
        if not model or not models:
            return None
        key = _norm(model)
        for name, val in models.items():
            if val in (None, NOT_FOUND, ""):
                continue
            if _norm(name) == key:
                try:
                    return float(val)
                except (TypeError, ValueError):
                    return None
        return None

    def per_million_for(self, model: str) -> Optional[dict]:
        models = (self.per_million or {}).get("models") or {}
        if not model:
            return None
        key = _norm(model)
        for name, val in models.items():
            if _norm(name) == key:
                if not isinstance(val, dict):
                    return None
                if val.get("status") == NOT_FOUND or val.get("input") == NOT_FOUND:
                    return None
                return val
        return None

    def as_public_dict(self) -> dict:
        """给网页看的配置摘要，不含任何密钥。"""
        return {
            "path": str(self.path),
            "updated_at": self.updated_at,
            "notes": self.notes,
            "default_conversion": self.default_conversion,
            "conversions": {k: v.as_dict() for k, v in self.conversions.items()},
            "official_multipliers": self.official_multipliers,
            "per_million_token_credits": self.per_million,
            "api_unit_prices": self.api_unit_prices,
            "volc_afp": self.volc_afp,
            "inference": self.inference,
            "pricing_policy": self.pricing_policy,
            "workbuddy_credit_rules": (self.raw or {}).get("workbuddy_credit_rules") or {},
            "deepseek_peak_windows": (self.raw or {}).get("deepseek_peak_windows") or {},
        }


def _norm(name: str) -> str:
    return (
        (name or "")
        .strip()
        .lower()
        .replace("_", "-")
        .replace(" ", "-")
        .replace("/", "-")
    )


def _num_or_none(v) -> Optional[float]:
    if v in (None, "", NOT_FOUND):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load_rates(path: Path) -> RatesConfig:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError("找不到价格表：%s" % path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    conversions = {}
    for item in raw.get("credit_to_cny") or []:
        cny = _num_or_none(item.get("cny_per_credit"))
        credits = _num_or_none(item.get("credits"))
        cny_total = _num_or_none(item.get("cny"))
        if cny is None and credits and cny_total and credits > 0:
            cny = cny_total / credits
        available = cny is not None and item.get("status") != NOT_FOUND
        key = item.get("key") or ""
        conversions[key] = Conversion(
            key=key,
            label=item.get("label") or key,
            cny_per_credit=cny if available else None,
            source=item.get("source") or NOT_FOUND,
            source_title=item.get("source_title") or "",
            retrieved_at=item.get("retrieved_at") or "",
            note=item.get("note") or "",
            available=available,
        )
    default = raw.get("default_conversion") or ""
    if default not in conversions and conversions:
        default = next(iter(conversions))
    return RatesConfig(
        path=path,
        raw=raw,
        updated_at=raw.get("updated_at") or "",
        default_conversion=default,
        conversions=conversions,
        official_multipliers=raw.get("official_multipliers") or {},
        per_million=raw.get("per_million_token_credits") or {},
        api_unit_prices=raw.get("api_unit_prices") or {},
        volc_afp=raw.get("volc_afp") or {},
        inference=raw.get("inference") or {},
        pricing_policy=raw.get("pricing_policy") or {},
        notes=raw.get("notes") or "",
    )


def default_rates_path(repo_root: Path) -> Path:
    return Path(repo_root) / "config" / "model_rates.json"
