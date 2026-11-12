# -*- coding: utf-8 -*-
"""按 DeepSeek 官方定价页判断一次调用是否落在高峰时段。

官方原文（https://api-docs.deepseek.com/zh-cn/quick_start/pricing.md，
2026-11-12 读取）：「北京时间周一至周五（不含中国法定节假日）9:00 - 12:00、
14:00 - 18:00 为高峰时段；其余时段，包括周末及中国法定节假日全天均为空闲时段。」
英文页把同一窗口写成 UTC 01:00-04:00、06:00-10:00。

本模块约定：
  - 时区固定 Asia/Shanghai（北京时间），不跟运行机器的系统时区走。
  - 时间窗口右开：[09:00, 12:00)、[14:00, 18:00)。12:00 整和 18:00 整算闲时。
  - 「中国法定节假日」按《全国年节及纪念日放假办法》的全体公民放假节日，
    不是国务院「放假调休」通知里的全部调休日。周六、周日即使调休上班，
    仍按官方「周末……全天均为空闲」处理。
  - 目前只收录了 2026 年的法定节假日日期（对照 国办发明电〔2025〕7 号
    核对过日历）。其他年份只排除周末，不把没核对过的节日算进去。
"""
from __future__ import annotations

from datetime import date, datetime, time
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")

PEAK_WINDOWS = (
    (time(9, 0, 0), time(12, 0, 0)),
    (time(14, 0, 0), time(18, 0, 0)),
)

# 2026 年全体公民放假的法定节假日。春节按除夕至初三（2026-02-16 至 02-19），
# 清明为当日 2026-04-05，端午 2026-06-19，中秋 2026-09-25。
# 来源：全国年节及纪念日放假办法 + 国务院 2026 年放假安排通知核对日历。
STATUTORY_HOLIDAYS = {
    2026: frozenset({
        date(2026, 1, 1),
        date(2026, 2, 16), date(2026, 2, 17), date(2026, 2, 18), date(2026, 2, 19),
        date(2026, 4, 5),
        date(2026, 5, 1), date(2026, 5, 2),
        date(2026, 6, 19),
        date(2026, 9, 25),
        date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3),
    }),
}

PEAK_RULE = {
    "timezone": "Asia/Shanghai",
    "windows": ["09:00-12:00", "14:00-18:00"],
    "end_exclusive": True,
    "weekdays": "周一至周五",
    "source": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing.md",
    "source_retrieved_at": "2026-11-12",
    "holiday_source": "https://www.gov.cn/zhengce/zhengceku/202511/content_7047091.htm",
    "holiday_source_title": "国务院办公厅关于2026年部分节假日安排的通知（国办发明电〔2025〕7号）",
    "holiday_note": (
        "法定节假日按全体公民放假节日计入：元旦 1 月 1 日，春节除夕至初三，"
        "清明当日，劳动节 5 月 1–2 日，端午当日，中秋当日，国庆 10 月 1–3 日。"
        "放假调休里多出来的工作日（例如 2026-02-20、2026-02-23、2026-05-04、"
        "2026-05-05、2026-10-05 至 10-07）按官方字面不是法定节假日，"
        "若落在周一至周五的高峰窗口，按高峰计。调休上班的周六周日仍按周末闲时。"
    ),
    "statutory_holidays_2026": sorted(d.isoformat() for d in STATUTORY_HOLIDAYS[2026]),
}


def unix_to_beijing(ts) -> Optional[datetime]:
    """把 jsonl 里的时间戳（毫秒或秒）转到北京时间。无效则返回 None。"""
    if ts is None or ts == "":
        return None
    try:
        raw = int(ts)
    except (TypeError, ValueError):
        return None
    if raw <= 0:
        return None
    sec = raw / 1000.0 if raw > 10_000_000_000 else float(raw)
    try:
        return datetime.fromtimestamp(sec, tz=BEIJING)
    except (OSError, ValueError, OverflowError):
        return None


def is_statutory_holiday(d: date) -> bool:
    days = STATUTORY_HOLIDAYS.get(d.year)
    if not days:
        return False
    return d in days


def is_deepseek_peak(dt: datetime) -> bool:
    """dt 可以带任意时区，内部转到北京时间再判断。"""
    if dt.tzinfo is None:
        local = dt.replace(tzinfo=BEIJING)
    else:
        local = dt.astimezone(BEIJING)
    if local.weekday() >= 5:
        return False
    if is_statutory_holiday(local.date()):
        return False
    t = local.time()
    for start, end in PEAK_WINDOWS:
        if start <= t < end:
            return True
    return False


def classify_timestamp(ts) -> Dict[str, Any]:
    """给一次调用的时间戳分类。

    period: peak / offpeak / unknown
    unknown 表示没有可用时间戳，调用方应按闲时计价并注明。
    """
    dt = unix_to_beijing(ts)
    if dt is None:
        return {
            "period": "unknown",
            "is_peak": False,
            "local": "",
            "day": "",
            "hour": "",
            "weekday": None,
            "reason": "没有可用的调用时间戳",
            "timezone": "Asia/Shanghai",
        }
    peak = is_deepseek_peak(dt)
    if dt.weekday() >= 5:
        reason = "周末，闲时"
    elif is_statutory_holiday(dt.date()):
        reason = "中国法定节假日，闲时"
    elif peak:
        reason = "北京时间工作日高峰窗口"
    else:
        reason = "工作日非高峰窗口，闲时"
    return {
        "period": "peak" if peak else "offpeak",
        "is_peak": peak,
        "local": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "day": dt.strftime("%Y-%m-%d"),
        "hour": dt.strftime("%H"),
        "weekday": dt.weekday(),
        "reason": reason,
        "timezone": "Asia/Shanghai",
    }
