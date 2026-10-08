"""把打包产物整理成两个可以直接发出去的 zip 包。

产出
----
    dist/安卓相册备份_v{版本}_免安装版.zip
        解压即用。里面是 exe + _internal/ + 配置.txt + 使用说明.txt。
        目标电脑上不需要装 Python、不需要装 ADB、不需要管理员权限。

    dist/安卓相册备份_v{版本}_源码版.zip
        完整的源代码工程，含测试与打包脚本。

一个实现细节
------------
早期版本会先把 PyInstaller 的输出复制到一个临时"发布目录"、改名、再压缩。
那会凭空多出一份 55 MB 的副本，还要在结束时整体删除。现在改成**直接写进
zip**，用 arcname 控制包内路径，需要改名的文件（exe）只改 arcname、不动磁盘，
额外附带的文本文件用 ``writestr`` 写进去。全程零临时文件。

用法：
    python tools/make_zip.py
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from photobak import __version__  # noqa: E402

DIST = ROOT / "dist"
BUILT = DIST / "PhotoBak"                     # PyInstaller onedir 的输出目录
APP_NAME = "安卓相册备份"
EXE_INTERNAL = "PhotoBak.exe"
EXE_FINAL = f"{APP_NAME}.exe"

# 源码版要带上哪些东西（白名单，避免把 build/dist/缓存打进去）
SOURCE_INCLUDE = [
    "run.py",
    "photobak",
    "webui",
    "adb",
    "assets",
    "tools",
    "tests",
    "docs",
    "配置.txt",
    "使用说明.txt",
    "requirements.txt",
    "安装依赖.bat",
    "启动.bat",
    "build.bat",
    "photobak.spec",
    "README.md",
]

SOURCE_EXCLUDE_DIRS = {"__pycache__", "build", "dist", ".photobak", ".git", ".venv", "venv"}
SOURCE_EXCLUDE_SUFFIX = {".pyc", ".pyo", ".zip"}


def human(n: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    v = float(n)
    for u in units:
        if v < 1024 or u == units[-1]:
            return f"{v:.1f} {u}" if u != "B" else f"{int(v)} B"
        v /= 1024
    return f"{v:.1f} GB"


def dir_size(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def source_entries():
    """把白名单展开成 (磁盘路径, 包内相对路径)。"""
    for name in SOURCE_INCLUDE:
        p = ROOT / name
        if not p.exists():
            print(f"      [跳过] 不存在：{name}")
            continue
        if p.is_file():
            yield p, Path(name)
            continue
        for f in sorted(p.rglob("*")):
            if not f.is_file():
                continue
            if any(part in SOURCE_EXCLUDE_DIRS for part in f.parts):
                continue
            if f.suffix.lower() in SOURCE_EXCLUDE_SUFFIX:
                continue
            yield f, f.relative_to(ROOT)


def first_run_text() -> str:
    return f"""============================================================
  {APP_NAME} v{__version__} —— 先看我（30 秒读完）
============================================================

这个文件夹里有三样东西：

    安卓相册备份.exe   ← 双击这个就能用
    _internal\\         ← 程序自己的文件，别删、别动
    配置.txt           ← 想改备份位置/范围就编辑它（记事本即可）

------------------------------------------------------------
第一次用，只要三步
------------------------------------------------------------

第一步  手机上打开「USB 调试」
        设置 → 我的设备 → 全部参数与信息 → 连点「OS 版本」7 次
        然后回到 设置 → 更多设置 → 开发者选项 → 打开「USB 调试」
        最后用数据线把手机插到电脑上，手机上弹窗点「允许」

第二步  双击「安卓相册备份.exe」
        按提示选择连接方式：
          · 想省事 → 选 1（USB 直连，推荐）
          · 想无线 → 选 2（无线配对码，手机和电脑连同一个 WiFi）
        然后照着屏幕上的提示一步步做就行

第三步  等它跑完，去备份目录看结果
        备份目录里会多出两个文件夹：
          · 照片归档\\  按拍摄时间分好类的照片（平时看这个）
          · 手机镜像\\  和手机上一模一样的目录结构（用来对账）

------------------------------------------------------------
最重要的一句话
------------------------------------------------------------
本程序**永远不会**自动删除你手机上的任何文件。

它会生成一份「清理清单」，告诉你哪些照片在电脑上已经有
完整副本、可以放心删。但删不删、什么时候删，全部由你自己
在手机上手动完成。程序只读不写，请放心使用。

------------------------------------------------------------
出问题了？
------------------------------------------------------------
    1. 先看屏幕上的提示 —— 每个错误都会告诉你具体怎么解决
    2. 再看「使用说明.txt」的「常见问题」一节
    3. 还不行就看日志：备份目录\\.photobak\\logs\\ 里最新的那个文件

============================================================
"""


def build_exe_zip() -> Path | None:
    print("[1/2] 生成「免安装版」zip ...")
    if not BUILT.is_dir():
        print(f"      [失败] 找不到打包产物：{BUILT}")
        print("             请先运行 build.bat（或 pyinstaller photobak.spec）。")
        return None

    out = DIST / f"{APP_NAME}_v{__version__}_免安装版.zip"
    if out.exists():
        out.unlink()

    prefix = Path(APP_NAME)          # 解压后是一个以中文名命名的文件夹
    count = 0

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for f in sorted(BUILT.rglob("*")):
            if not f.is_file():
                continue
            rel = f.relative_to(BUILT)
            # 顶层 exe 改名：只改包内路径，不动磁盘上的文件
            if rel == Path(EXE_INTERNAL):
                rel = Path(EXE_FINAL)
            zf.write(f, prefix / rel)
            count += 1

        # 随包附带、用户可见的三个文本文件
        extra = {
            "配置.txt": (ROOT / "配置.txt").read_text(encoding="utf-8"),
            "使用说明.txt": (ROOT / "使用说明.txt").read_text(encoding="utf-8"),
            "先看我-首次使用.txt": first_run_text(),
        }
        for name, text in extra.items():
            zf.writestr(str(prefix / name), text)
            count += 1

    print(f"      完成：{count} 个文件 → {out.name}（{human(out.stat().st_size)}）")
    print(f"      exe 已改名为：{EXE_FINAL}")
    return out


def build_source_zip() -> Path:
    print("[2/2] 生成「源码版」zip ...")
    out = DIST / f"{APP_NAME}_v{__version__}_源码版.zip"
    if out.exists():
        out.unlink()

    prefix = Path(f"{APP_NAME}_源码")
    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for f, rel in source_entries():
            zf.write(f, prefix / rel)
            count += 1

    print(f"      完成：{count} 个文件 → {out.name}（{human(out.stat().st_size)}）")
    return out


def main() -> int:
    DIST.mkdir(parents=True, exist_ok=True)

    print("=" * 68)
    print(f"  整理交付包 —— {APP_NAME} v{__version__}")
    print("=" * 68)
    print()

    exe_zip = build_exe_zip()
    src_zip = build_source_zip()

    print()
    print("=" * 68)
    print("  交付清单")
    print("=" * 68)

    if exe_zip:
        print(f"  [免安装版]  {exe_zip}")
        print(f"              体积：{human(exe_zip.stat().st_size)}")
        if BUILT.is_dir():
            print(f"              解压后约：{human(dir_size(BUILT))}")
        print("              适用：普通用户，双击 exe 即用，零依赖")
    else:
        print("  [免安装版]  未生成（请先打包）")

    print()
    print(f"  [源码版]    {src_zip}")
    print(f"              体积：{human(src_zip.stat().st_size)}")
    print("              适用：想改代码 / 想自己重新打包的人")
    print("              用法：解压后先双击「安装依赖.bat」，再双击「启动.bat」")
    print()
    print("=" * 68)
    return 0 if exe_zip else 1


if __name__ == "__main__":
    sys.exit(main())
