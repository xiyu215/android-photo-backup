"""一键打包：测试 → 离线自检 → PyInstaller → 生成交付 zip。

为什么把流程放进 Python 而不是 .bat？
    cmd.exe 解析含中文的 UTF-8 批处理文件不可靠（会按字节偏移读文件，
    多字节汉字跨过内部缓冲区边界就被截断，进而报出 "'xx' is not recognized"）。
    .bat 只保留"找 Python + 转发"这两件最稳的事，其余全部交给这里，
    中文输出与进度展示都由 Python 控制，编码不会出问题。

用法（由 build.bat 调用）：
    python tools/build_all.py            # 完整流程
    python tools/build_all.py --skip-tests    # 跳过测试
    python tools/build_all.py --no-clean      # 不清理 build/ 与 dist/
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from photobak import __version__  # noqa: E402

MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"
BUILD_DIR = ROOT / "build"
DIST_DIR = ROOT / "dist"
SPEC = ROOT / "photobak.spec"
PY = sys.executable


def rule(title: str = "") -> None:
    print()
    if title:
        print(f"── {title} " + "─" * max(0, 60 - len(title) * 2))
    else:
        print("─" * 66)


def run(cmd: list[str]) -> int:
    try:
        return subprocess.run(cmd, check=False).returncode
    except FileNotFoundError:
        print(f"  [错误] 找不到可执行文件：{cmd[0]}")
        return 127


def safe_rmtree(p: Path) -> bool:
    """尽力删除目录；失败不致命（PyInstaller 的 --noconfirm 会覆盖输出）。"""
    if not p.exists():
        return True
    try:
        shutil.rmtree(p)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"  [提示] 删除 {p.name} 失败（{exc}）。")
        print("         如果打包结果异常，请手动删掉该目录后重试；")
        print("         也可能是它正被资源管理器或杀毒软件占用。")
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="一键打包")
    ap.add_argument("--skip-tests", action="store_true", help="跳过自动化测试")
    ap.add_argument("--no-clean", action="store_true", help="不清理 build/ 与 dist/")
    ap.add_argument("--yes", "-y", action="store_true", help="测试失败时也继续")
    args = ap.parse_args()

    t0 = time.time()

    print("=" * 66)
    print(f"  安卓相册局域网备份工具 —— 一键打包  v{__version__}")
    print("=" * 66)
    print()
    print(f"  项目目录：{ROOT}")
    print(f"  使用的 Python：{PY}")
    print(f"  Python 版本：{sys.version.split()[0]}")

    # ---- 1. 打包依赖 ----
    rule("第 1 步 / 共 6 步：检查打包依赖")
    if run([PY, "-c", "import PyInstaller"]) != 0:
        print("  缺少 PyInstaller，正在安装 ...")
        if run([PY, "-m", "pip", "install", "pyinstaller", "-i", MIRROR]) != 0:
            print()
            print("  [错误] PyInstaller 安装失败。")
            print("         请检查网络后重试，或先运行「安装依赖.bat」。")
            return 1
    print("  PyInstaller：OK")

    # ---- 2. 运行依赖 ----
    rule("第 2 步 / 共 6 步：检查运行依赖")
    missing = [m for m in ("fastapi", "uvicorn", "websockets", "qrcode", "pystray", "PIL")
               if run([PY, "-c", f"import {m}"]) != 0]
    if missing:
        print(f"  [错误] 缺少运行依赖：{'、'.join(missing)}")
        print("         请先双击「安装依赖.bat」，装完再打包。")
        return 1
    print("  运行依赖：全部就绪")

    # ---- 3. 测试 ----
    if args.skip_tests:
        rule("第 3 步 / 共 6 步：自动化测试（已跳过）")
    else:
        rule("第 3 步 / 共 6 步：运行自动化测试")
        print("  48 项，约 30 秒。测试不通过就打包，等于把风险发给用户。")
        print()
        rc = run([PY, str(ROOT / "tests" / "run_all.py")])
        if rc != 0:
            print()
            print("  [警告] 测试没有全部通过。")
            if not args.yes:
                print("         如果你只是想打个包试玩，可以回答 y 继续；")
                print("         正式交付前请先把测试修到全绿。")
                try:
                    ans = input("         仍要继续打包吗？[y/N] ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    ans = "n"
                if ans not in ("y", "yes", "是"):
                    print("  已中止。")
                    return 1
        else:
            print()
            print("  测试全部通过。")

    # ---- 4. 离线自检 ----
    rule("第 4 步 / 共 6 步：离线自检")
    print("  确认程序里没有任何会导致联网的代码（前端资源、HTTP 调用）。")
    print()
    rc = run([PY, str(ROOT / "tools" / "check_offline.py")])
    if rc != 0:
        print()
        print("  [警告] 离线自检发现问题，请查看上面的输出。")
        print("         这些问题会导致程序在断网环境下卡住或崩溃。")
        if not args.yes:
            try:
                ans = input("         仍要继续打包吗？[y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = "n"
            if ans not in ("y", "yes", "是"):
                print("  已中止。")
                return 1

    # ---- 5. 清理旧产物 ----
    rule("第 5 步 / 共 6 步：清理上一次的打包产物")
    if args.no_clean:
        print("  已按参数要求跳过清理。")
    else:
        for d in (BUILD_DIR, DIST_DIR):
            if safe_rmtree(d):
                print(f"  已清理：{d.name}\\")
        # PyInstaller 在项目根留下的缓存
        for cache in ROOT.rglob("__pycache__"):
            safe_rmtree(cache)

    # ---- 6. 打包 ----
    rule("第 6 步 / 共 6 步：PyInstaller 打包")
    print("  首次约 1~3 分钟，请耐心等待。下面会滚动大量日志，属正常现象。")
    print()
    rc = run([PY, "-m", "PyInstaller", str(SPEC), "--noconfirm", "--clean",
              "--distpath", str(DIST_DIR), "--workpath", str(BUILD_DIR)])
    if rc != 0:
        rule()
        print("  [错误] 打包失败。")
        print()
        print("  常见原因与解决办法：")
        print("    1. 杀毒软件拦截 —— 把本目录加入白名单后重试")
        print("    2. dist / build 被占用 —— 关掉正在运行的程序和资源管理器窗口")
        print("    3. 磁盘空间不足 —— 打包过程需要约 1 GB 临时空间")
        print("    4. 上面滚动的日志里有具体原因，请往上翻")
        return 1

    built = DIST_DIR / "PhotoBak"
    exe = built / "PhotoBak.exe"
    if not exe.is_file():
        print(f"  [错误] 打包结束了，但没找到 {exe}")
        return 1
    print()
    print(f"  打包成功：{exe}")

    # ---- 收尾：生成交付 zip ----
    rule("生成交付 zip 包")
    rc = run([PY, str(ROOT / "tools" / "make_zip.py")])
    if rc != 0:
        print()
        print("  [错误] 生成 zip 失败，但 dist\\ 目录里的程序是可用的。")
        return 1

    rule()
    print(f"  全部完成，用时 {time.time() - t0:.0f} 秒。")
    print("  产物都在 dist\\ 目录下，详见上面的「交付清单」。")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
