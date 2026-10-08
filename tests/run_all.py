"""测试运行器（不依赖 pytest，只用标准库 unittest）。

用法：
    python tests/run_all.py            # 跑全部测试
    python tests/run_all.py -v         # 详细输出
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# tests/ 与 tools/ 都需要能被 import
for extra in ("tests", "tools", "."):
    p = str(_ROOT / extra)
    if p not in sys.path:
        sys.path.insert(0, p)


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    for mod in ("test_core", "test_e2e"):
        try:
            suite.addTests(loader.loadTestsFromName(mod))
        except Exception as exc:  # noqa: BLE001
            print(f"！无法加载测试模块 {mod}：{exc}")
            return 2

    verbosity = 2 if "-v" in sys.argv or "--verbose" in sys.argv else 1
    runner = unittest.TextTestRunner(verbosity=verbosity, buffer=False)
    result = runner.run(suite)

    print()
    print("=" * 60)
    print(f"共 {result.testsRun} 项，失败 {len(result.failures)}，错误 {len(result.errors)}，"
          f"跳过 {len(result.skipped)}")
    print("=" * 60)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
