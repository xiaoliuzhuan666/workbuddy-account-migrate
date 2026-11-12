# -*- coding: utf-8 -*-
"""WorkBuddy 本机数据只读核心库：路径、账号视图、用量解析。

网页版和测试都走这里。不在导入时扫描真实家目录，也不改登录态文件。
"""

__version__ = "0.1.0"

from .paths import resolve_workbuddy, ResolvedDir, EditionSummary  # noqa: F401
from .accounts import list_accounts, AccountView  # noqa: F401
from .usage import collect_usage, UsageReport  # noqa: F401
from .rates import load_rates, RatesConfig  # noqa: F401
from .codebuddy import collect_codebuddy, CodeBuddyReport  # noqa: F401
from .security import (  # noqa: F401
    host_is_allowed,
    generate_token,
    load_or_create_token_file,
    token_matches,
    redact,
)
