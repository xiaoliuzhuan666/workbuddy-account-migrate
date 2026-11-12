# -*- coding: utf-8 -*-
"""探测本机 CodeBuddy CLI 用量。读得到就统计，读不到就标明「没读到」。

不读账号凭据文件（如 antigravity_cockpit 下的 accounts.json），
只扫描常见的项目 jsonl 目录。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .paths import home_dir
from .rates import NOT_FOUND, RatesConfig
from .usage import (
    _add,
    _empty_bucket,
    _metrics,
    iter_project_jsonl,
    parse_raw_usage_line,
)
from .paths import ResolvedDir


# 只探测这些相对路径，不遍地乱翻用户家目录里的备份和凭据
_CANDIDATES = (
    ".codebuddy",
    ".codebuddycn",
    ".local/share/codebuddy-native",
)


@dataclass
class CodeBuddyReport:
    status: str  # ok / 没读到
    reason: str
    roots: List[dict] = field(default_factory=list)
    reports: List[dict] = field(default_factory=list)
    totals: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "reason": self.reason,
            "roots": self.roots,
            "reports": self.reports,
            "totals": self.totals,
        }


def _candidate_roots(home: Path) -> List[Path]:
    found = []
    for rel in _CANDIDATES:
        p = home / rel
        if p.is_dir():
            found.append(p)
    return found


def collect_codebuddy(
    home: Optional[Path] = None,
    rates: Optional[RatesConfig] = None,
    conversion_key: Optional[str] = None,
) -> CodeBuddyReport:
    h = home_dir(home)
    roots = _candidate_roots(h)
    listed = [{"path": str(p), "has_projects": (p / "projects").is_dir()} for p in roots]
    if not roots:
        return CodeBuddyReport(
            status=NOT_FOUND,
            reason="常见目录（~/.codebuddy、~/.codebuddycn、~/.local/share/codebuddy-native）都不存在。",
            roots=listed,
        )

    reports = []
    any_calls = 0
    merged = _empty_bucket()
    cny = rates.cny_per_credit(conversion_key) if rates else None
    scanned_any = False
    for root in roots:
        projects = root / "projects"
        if not projects.is_dir():
            reports.append({
                "path": str(root),
                "status": NOT_FOUND,
                "reason": "有这个目录，但没有 projects/，没法按会话文件统计。",
                "calls": 0,
            })
            continue
        # 复用 usage 解析，构造一个临时 ResolvedDir
        resolved = ResolvedDir(
            home=h,
            path=root,
            edition="codebuddy",
            label="CodeBuddy CLI",
            db_path=root / "workbuddy.db",  # 可能不存在
            projects_dir=projects,
            snapshot_path=root / "storage" / "skeleton" / "account-snapshot.json",
            memory_dir=root / "memory",
            dual=False,
            editions=[],
            notice="",
        )
        # 不走 session_usage 交叉校验（CodeBuddy 常见没有这张表）
        files = 0
        errors = 0
        calls = 0
        by_model = {}
        totals = _empty_bucket()
        from collections import defaultdict
        by_model = defaultdict(_empty_bucket)
        for fp in iter_project_jsonl(projects):
            files += 1
            scanned_any = True
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
                        any_calls += 1
                        _add(by_model[call["model"]], call, sid)
                        _add(totals, call, sid)
                        _add(merged, call, sid)
            except OSError:
                errors += 1
        if files == 0:
            reports.append({
                "path": str(root),
                "status": NOT_FOUND,
                "reason": "projects/ 下没有 jsonl 会话文件。",
                "calls": 0,
                "files_scanned": 0,
            })
            continue
        if calls == 0:
            reports.append({
                "path": str(root),
                "status": NOT_FOUND,
                "reason": "找到 %d 个 jsonl，但没有解析到 providerData.rawUsage。" % files,
                "calls": 0,
                "files_scanned": files,
                "parse_errors": errors,
            })
            continue
        model_rows = []
        for name, b in by_model.items():
            row = {"model": name}
            row.update(_metrics(b, cny, None))
            model_rows.append(row)
        model_rows.sort(key=lambda r: (-r["credit"], -r["calls"], r["model"]))
        reports.append({
            "path": str(root),
            "status": "ok",
            "reason": "",
            "files_scanned": files,
            "parse_errors": errors,
            "calls": calls,
            "by_model": model_rows,
            "totals": _metrics(totals, cny, None),
        })

    if any_calls == 0:
        reason = "探测到 CodeBuddy 目录，但没有读到可用的 rawUsage。"
        if not scanned_any:
            reason = "探测到目录，但其中没有 projects/*.jsonl。"
        return CodeBuddyReport(
            status=NOT_FOUND,
            reason=reason,
            roots=listed,
            reports=reports,
        )
    return CodeBuddyReport(
        status="ok",
        reason="",
        roots=listed,
        reports=reports,
        totals=_metrics(merged, cny, None),
    )
