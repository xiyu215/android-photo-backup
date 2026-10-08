"""启动脚本（源码版与打包版的统一入口）。

为什么要有这一层？
    真正的逻辑在 ``photobak/main.py``（包内），这样相对导入才能正常工作；
    而 PyInstaller 需要一个"顶层脚本"作为分析起点，Windows 用户也习惯
    双击一个显眼的 .py / .exe。这个小文件就把两件事同时满足了。
"""

from __future__ import annotations

import os
import sys

# 确保能 import 到同目录下的 photobak 包（无论从哪个工作目录启动）
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from photobak.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
