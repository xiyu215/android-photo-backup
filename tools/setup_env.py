"""源码版的一键环境准备：创建虚拟环境 + 安装依赖 + 自检。

为什么不在 .bat 里直接做这些？
    cmd.exe 解析 UTF-8 批处理文件是不可靠的（它按字节偏移读文件，
    多字节汉字一旦跨过内部缓冲区边界就会被截断，进而把半截汉字当成
    命令去执行，报出 "'xx' is not recognized" 这种莫名其妙的错误）。
    所以 .bat 只做一件最稳的事：找到 Python、把活交给这个脚本。
    所有中文提示都由 Python 打印，编码完全可控。

用法（由「安装依赖.bat」调用）：
    python tools/setup_env.py
"""

from __future__ import annotations

import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV_DIR = ROOT / ".venv"
VENV_PY = VENV_DIR / "Scripts" / "python.exe"

MIRRORS = [
    ("清华镜像", "https://pypi.tuna.tsinghua.edu.cn/simple"),
    ("官方源", "https://pypi.org/simple/"),
]

REQUIRED = [
    ("fastapi", "本地管理页"),
    ("uvicorn", "本地管理页"),
    ("websockets", "实时进度推送"),
    ("qrcode", "终端二维码"),
]
OPTIONAL = [
    ("pystray", "托盘图标"),
    ("PIL", "托盘图标 / 图标生成"),
    ("PyInstaller", "重新打包 exe"),
]


def rule(title: str = "") -> None:
    print()
    if title:
        print(f"── {title} " + "─" * max(0, 60 - len(title) * 2))
    else:
        print("─" * 66)


def run(cmd: list[str], *, quiet: bool = False) -> int:
    """执行子进程并实时回显输出（保持进度可见）。"""
    try:
        proc = subprocess.run(cmd, check=False)
        return proc.returncode
    except FileNotFoundError:
        print(f"  [错误] 找不到可执行文件：{cmd[0]}")
        return 127


def main() -> int:
    print("=" * 66)
    print("  安卓相册局域网备份工具 —— 环境准备")
    print("=" * 66)
    print()
    print(f"  项目目录：{ROOT}")
    print(f"  当前解释器：{sys.executable}")
    print(f"  Python 版本：{sys.version.split()[0]}")
    print()

    if sys.version_info < (3, 10):
        print("  [错误] Python 版本过低，需要 3.10 及以上。")
        print()
        print("  解决办法：")
        print("    1. 到 https://www.python.org/downloads/windows/ 下载 3.11 / 3.12 / 3.13")
        print("    2. 安装时务必勾选 “Add python.exe to PATH”")
        print("    3. 装完重新运行本脚本")
        return 1

    # ---- 1. 准备虚拟环境 ----
    rule("第 1 步 / 共 4 步：准备项目虚拟环境")
    in_venv = sys.prefix != sys.base_prefix

    if in_venv:
        target = Path(sys.executable)
        print(f"  已经在虚拟环境中运行，直接使用：{target}")
    elif VENV_PY.is_file():
        target = VENV_PY
        print(f"  已存在项目虚拟环境，直接使用：{target}")
    else:
        print(f"  正在创建项目虚拟环境：{VENV_DIR}")
        print("  （把依赖装进项目自己的 .venv 里，不污染系统 Python；")
        print("    将来删掉整个文件夹就等于卸载干净。）")
        print()
        try:
            venv.EnvBuilder(with_pip=True, clear=False).create(str(VENV_DIR))
        except Exception as exc:  # noqa: BLE001
            print(f"  [错误] 创建虚拟环境失败：{exc}")
            print()
            print("  解决办法：")
            print("    1. 确认安装 Python 时勾选了 pip 与 venv 组件")
            print("    2. 换个不含空格和中文的路径再试（例如 D:\\photobak）")
            print("    3. 或者直接用「免安装版（exe）」，那个版本无需任何依赖")
            return 1

        if not VENV_PY.is_file():
            print(f"  [错误] 虚拟环境创建后找不到 {VENV_PY}")
            return 1
        target = VENV_PY
        print(f"  已创建：{target}")

    py = str(target)

    # ---- 2. 升级 pip ----
    rule("第 2 步 / 共 4 步：升级 pip")
    run([py, "-m", "pip", "install", "--upgrade", "pip", "-i", MIRRORS[0][1]])

    # ---- 3. 安装依赖 ----
    rule("第 3 步 / 共 4 步：安装项目依赖")
    req = ROOT / "requirements.txt"
    if not req.is_file():
        print(f"  [错误] 找不到依赖清单：{req}")
        return 1

    print("  约 60~120 MB，视网速可能需要 1~3 分钟。")
    print("  如果长时间卡住，可以按 Ctrl+C 中止，然后重新运行本脚本。")
    print()

    ok = False
    for label, url in MIRRORS:
        print(f"  正在使用【{label}】安装 ...")
        rc = run([py, "-m", "pip", "install", "-r", str(req), "-i", url])
        if rc == 0:
            print(f"  【{label}】安装成功。")
            ok = True
            break
        print(f"  【{label}】安装失败（返回码 {rc}），换下一个源重试 ...")
        print()

    if not ok:
        rule()
        print("  [错误] 所有镜像都安装失败。")
        print()
        print("  常见原因与解决办法：")
        print("    1. 网络不通 —— 先确认能打开 https://pypi.tuna.tsinghua.edu.cn")
        print("    2. 公司网络有代理 —— 设置好代理后重跑本脚本")
        print("    3. 权限不足 —— 右键「安装依赖.bat」，选「以管理员身份运行」")
        print("    4. 实在装不上 —— 直接用「免安装版（exe）」，无需任何依赖")
        return 1

    # ---- 4. 自检 ----
    rule("第 4 步 / 共 4 步：依赖自检")
    missing_required: list[str] = []
    for mod, why in REQUIRED:
        rc = run([py, "-c", f"import {mod}"])
        if rc == 0:
            print(f"  [OK]   {mod:<14} （{why}）")
        else:
            print(f"  [缺失] {mod:<14} （{why}）")
            missing_required.append(mod)

    for mod, why in OPTIONAL:
        rc = run([py, "-c", f"import {mod}"])
        if rc == 0:
            print(f"  [OK]   {mod:<14} （{why}）")
        else:
            print(f"  [可选] {mod:<14} 未安装（只影响：{why}）")

    rule()
    if missing_required:
        print("  [失败] 缺少必需的依赖：" + "、".join(missing_required))
        print("         请重跑本脚本；若反复失败，请改用「免安装版（exe）」。")
        return 1

    print("  安装完成！接下来双击「启动.bat」就能用了。")
    print()
    print("  提示：第一次运行前建议先看一眼「使用说明.txt」的第 3 章，")
    print("        那里写了手机上怎么开 USB 调试（红米/澎湃 OS 的路径也在里面）。")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
