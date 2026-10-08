"""基建层：日志、错误码、路径、网络、界面文案。

本层不包含任何业务规则，只提供被各层复用的底层能力。
"""

from __future__ import annotations

__all__ = ["errors", "i18n", "logger", "netutil", "paths"]
