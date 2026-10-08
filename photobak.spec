# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（onedir 模式）。

为什么选 onedir 而不是 onefile？
    用户的目标电脑是 Windows 11，要求"零依赖、不要管理员权限、双击就能跑"。
    两种模式都能做到，但 onedir 明显更适合本项目的场景：

    ┌────────────┬──────────────────────────┬──────────────────────────┐
    │            │ onefile                  │ onedir（本配置）          │
    ├────────────┼──────────────────────────┼──────────────────────────┤
    │ 启动速度   │ 每次解压到 %TEMP%，慢    │ 直接加载，快              │
    │ 杀软误报   │ 高（自解压行为像病毒）   │ 低                        │
    │ 磁盘残留   │ 每次运行留下临时目录     │ 无                        │
    │ adb.exe    │ 路径在临时目录，会变     │ 路径固定，好排查          │
    │ 资源定位   │ _MEIPASS 每次不同        │ 稳定                      │
    └────────────┴──────────────────────────┴──────────────────────────┘

    代价是"一个 exe"变成"一个文件夹"，但对本项目反而是优点：内置的 adb.exe
    用户能直接看到，出问题时也容易自查。

构建：双击 build.bat（或 pyinstaller photobak.spec --noconfirm）
"""

from PyInstaller.utils.hooks import collect_submodules, collect_data_files

# ---------------------------------------------------------------------------
# 1. 需要显式声明的隐藏导入
# ---------------------------------------------------------------------------
# uvicorn 用 importlib 在运行时动态选择事件循环与协议实现，静态分析看不到，
# 漏掉的话打包出来的程序会在启动管理页时崩掉（报 "No module named ...auto"）。
hidden = []
hidden += collect_submodules("uvicorn")
hidden += collect_submodules("websockets")
hidden += collect_submodules("fastapi")
hidden += collect_submodules("starlette")

hidden += [
    # 托盘图标的 Windows 后端（pystray 用 importlib 挑后端）
    "pystray._win32",
    # Pillow 在 pystray / qrcode 里用到的子模块
    "PIL._tkinter_finder",
    "PIL.Image",
    "PIL.ImageDraw",
    "PIL.ImageFont",
    # 二维码渲染（终端字符块）
    "qrcode",
    "qrcode.image.pil",
    # 数据库 / 哈希等标准库（显式列出，防止被过度裁剪）
    "sqlite3",
    "hashlib",
    "hmac",
    # uvicorn 的可选加速件（装了就用，没装会自己回退）
    "httptools",
    "watchfiles",
    "websockets.legacy",
    "websockets.legacy.server",
]

# ---------------------------------------------------------------------------
# 2. 需要一起打包的数据文件
# ---------------------------------------------------------------------------
datas = [
    # 本地管理页前端（全本地、无 CDN）
    ("webui", "webui"),
    # 内置 adb 三件套（用户电脑上不需要装任何东西）
    ("adb", "adb"),
    # 图标
    ("assets", "assets"),
]

# 第三方包自带的模板等资源（Pillow 的字体/元数据之类）
datas += collect_data_files("qrcode")
datas += collect_data_files("fastapi")

# ---------------------------------------------------------------------------
# 3. 明确排除的模块（把体积压下来）
# ---------------------------------------------------------------------------
# 本项目是纯命令行 + 本地网页，完全用不到这些重型库。
excludes = [
    "tkinter", "_tkinter", "turtle",
    "matplotlib", "numpy", "scipy", "pandas",
    "PyQt5", "PyQt6", "PySide2", "PySide6", "wx",
    "IPython", "jupyter", "notebook", "nbformat",
    "pytest", "unittest.mock",
    "torch", "tensorflow", "cv2",
    "setuptools", "pip", "wheel",
    # 打包工具自身不需要进包
    "PyInstaller",
]


a = Analysis(
    ["run.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PhotoBak",              # 内部用 ASCII 名，避免非 ASCII 路径带来的坑；
                                  # 交付前由 tools/make_zip.py 改名为「安卓相册备份.exe」
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                    # 不用 UPX：压缩后更容易被杀软误报
    console=True,                 # 本工具是终端界面，必须有控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/icon.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="PhotoBak",
)
