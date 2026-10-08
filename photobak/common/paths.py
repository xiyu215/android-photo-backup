"""路径与文件系统工具。

本模块集中处理三件事：
1. **打包后的资源定位**（PyInstaller onedir 与源码运行两种形态）。
2. **相对路径与安全拼接**（索引库只存相对路径；防目录穿越）。
3. **Windows 路径限制**（260 字符、`\\\\?\\` 长路径前缀、可写性探测）。
"""

from __future__ import annotations

import os
import sys
import errno
from pathlib import Path

# Windows 完整路径长度上限（不含结尾 NUL）
WIN_PATH_LIMIT = 260


# ---------------------------------------------------------------------------
# 资源定位
# ---------------------------------------------------------------------------
def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包后的环境中。"""
    return getattr(sys, "frozen", False)


def app_dir() -> Path:
    """程序所在目录（用户可见的那个目录）。

    - 打包后：exe 所在目录（onedir 形态下即包含 exe 的文件夹）。
    - 源码运行：项目根目录（即 ``photobak/`` 包的上一级）。

    注意：配置、日志、报告都应放在这里，而不是临时目录，
    以保证"绿色免安装、删文件夹即卸载"。
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    # 源码模式：本文件位于 <项目根>/photobak/common/paths.py
    return Path(__file__).resolve().parents[2]


def resource_dir() -> Path:
    """打包进去的只读资源（webui/adb/assets）所在目录。

    PyInstaller 6+ 的 onedir 模式把资源放在 exe 同级的 ``_internal/``；
    旧版本与 onefile 模式使用 ``sys._MEIPASS``。这里做兼容探测，
    避免升级 PyInstaller 后资源找不到。
    """
    if is_frozen():
        base = getattr(sys, "_MEIPASS", None)
        if base:
            return Path(base)
        internal = app_dir() / "_internal"
        if internal.is_dir():
            return internal
        return app_dir()
    return Path(__file__).resolve().parents[2]


def resource_path(*parts: str) -> Path:
    """拼接资源路径。"""
    return resource_dir().joinpath(*parts)


def find_adb_dir() -> Path | None:
    """定位内置 adb 目录，兼容 onedir 的多种布局。"""
    candidates = [
        resource_dir() / "adb",
        app_dir() / "adb",
        app_dir() / "_internal" / "adb",
    ]
    for c in candidates:
        if (c / "adb.exe").is_file():
            return c
    return None


# ---------------------------------------------------------------------------
# 相对路径工具（索引库只存相对路径，保证整个备份目录可搬动）
# ---------------------------------------------------------------------------
def to_posix(rel: str | Path) -> str:
    """把任意平台的分隔符统一成 ``/``，便于跨平台存储与比较。"""
    return str(rel).replace("\\", "/")


def from_posix(rel: str, root: str | Path) -> Path:
    """把存储用的 ``/`` 相对路径还原为当前平台的绝对路径。

    这里**不使用** :func:`safe_join` 的校验，因为入参来自我们自己的数据库；
    但依旧会做一次 ``..`` 过滤，防御数据库被手工改坏的情况。
    """
    clean = to_posix(rel).lstrip("/")
    if ".." in clean.split("/"):
        raise ValueError(f"非法相对路径：{rel!r}")
    return Path(root) / Path(*clean.split("/"))


def safe_join(root: str | Path, rel: str | Path) -> Path:
    """安全拼接：确保结果落在 ``root`` 之内。

    手机端文件名是不可信输入（可能含 ``../``），若直接拼接会造成
    "目录穿越"，把文件写到备份根之外。这里做统一拦截。

    :raises ValueError: 当拼接结果逃出 root 时。
    """
    root_p = Path(root).resolve()
    candidate = (root_p / Path(*to_posix(rel).lstrip("/").split("/"))).resolve()
    try:
        candidate.relative_to(root_p)
    except ValueError as exc:  # 不在 root 之下
        raise ValueError(f"检测到目录穿越，已拒绝：{rel!r}") from exc
    return candidate


def is_subpath(child: str | Path, parent: str | Path) -> bool:
    """判断 ``child`` 是否位于 ``parent`` 之下（含相等）。"""
    try:
        Path(child).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


def relpath_of(path: str | Path, root: str | Path) -> str:
    """取相对 root 的 POSIX 风格相对路径。"""
    return to_posix(os.path.relpath(str(path), str(root)))


# ---------------------------------------------------------------------------
# Windows 长路径
# ---------------------------------------------------------------------------
def long_path(p: str | Path) -> str:
    """给绝对路径加上 ``\\\\?\\`` 前缀以突破 260 字符限制。

    注意：``\\\\?\\`` 前缀要求路径为**绝对路径**且**已规范化**（不能含 ``..``、
    不能有 ``/``）。UNC 路径需用 ``\\\\?\\UNC\\`` 形式。
    """
    s = os.path.abspath(str(p))
    if s.startswith("\\\\?\\"):
        return s
    if s.startswith("\\\\"):  # UNC
        return "\\\\?\\UNC\\" + s[2:]
    return "\\\\?\\" + s


def path_len(p: str | Path) -> int:
    """返回路径的字符长度（用于判断是否超过 Windows 限制）。"""
    return len(os.path.abspath(str(p)))


def too_long(p: str | Path, limit: int = WIN_PATH_LIMIT) -> bool:
    """路径是否过长。

    这里保留 12 字符余量：因为写入时会先落到 ``.part`` 临时文件，
    文件名会额外加上 ``.part`` 后缀（见 :mod:`core.verify`）。
    """
    return path_len(p) + 12 > limit


# ---------------------------------------------------------------------------
# 目录与权限
# ---------------------------------------------------------------------------
def ensure_dir(p: str | Path) -> Path:
    """确保目录存在（递归创建），返回 Path。"""
    path = Path(p)
    path.mkdir(parents=True, exist_ok=True)
    return path


def is_writable(directory: str | Path) -> bool:
    """探测目录是否可写。

    直接尝试创建并删除一个临时文件，比只看 os.access 更可靠
    （Windows 上权限位常常不可信）。
    """
    d = Path(directory)
    if not d.is_dir():
        return False
    probe = d / f".photobak_write_test_{os.getpid()}"
    try:
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        probe.unlink()
        return True
    except OSError:
        try:
            probe.unlink()
        except OSError:
            pass
        return False


def free_space(path: str | Path) -> int:
    """返回该路径所在分区的剩余字节数；失败返回 0。"""
    try:
        return int(__import__("shutil").disk_usage(str(path)).free)
    except OSError:
        return 0


def is_same_volume(a: str | Path, b: str | Path) -> bool:
    """判断两个路径是否位于同一个卷（硬链接的前提条件之一）。"""
    try:
        return os.path.splitdrive(os.path.abspath(str(a)))[0].lower() == \
               os.path.splitdrive(os.path.abspath(str(b)))[0].lower()
    except (OSError, ValueError):
        return False


def fs_type(path: str | Path) -> str:
    """尽力探测文件系统类型（NTFS / exFAT / FAT32 / 未知）。

    Windows 上通过 ``GetVolumeInformationW`` 获取，失败时返回 "UNKNOWN"。
    只用于"是否提示硬链接不可用"，因此探测失败不影响主流程。
    """
    if os.name != "nt":
        return "UNKNOWN"
    try:
        import ctypes

        root = os.path.splitdrive(os.path.abspath(str(path)))[0] + "\\"
        fs_name = ctypes.create_unicode_buffer(261)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(root), None, 0,
            None, None, None, fs_name, ctypes.sizeof(fs_name),
        )
        return fs_name.value if ok else "UNKNOWN"
    except Exception:  # noqa: BLE001 —— 探测类操作，任何异常都不应影响主流程
        return "UNKNOWN"


def supports_hardlink(path: str | Path) -> bool:
    """该卷是否支持硬链接（NTFS/ReFS 支持；exFAT/FAT32 不支持）。"""
    return fs_type(path).upper() in ("NTFS", "REFS")


# ---------------------------------------------------------------------------
# 展示辅助
# ---------------------------------------------------------------------------
def human_size(num: int | float) -> str:
    """把字节数格式化为人类可读字符串。"""
    n = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(n)} {unit}"
            return f"{n:.2f} {unit}"
        n /= 1024.0
    return f"{n:.2f} TB"


def human_speed(bytes_per_sec: float) -> str:
    """把速度格式化为 ``x MB/s``。"""
    return f"{human_size(bytes_per_sec)}/s"


def errno_name(exc: OSError) -> str:
    """把 OSError 的 errno 翻译成便于阅读的名字。"""
    mapping = {
        errno.ENOSPC: "磁盘空间不足",
        errno.EACCES: "权限不足",
        errno.EPERM: "操作不被允许",
        errno.ENOENT: "文件或目录不存在",
        errno.EXDEV: "跨设备链接不被支持",
        errno.ENOTSUP: "该文件系统不支持此操作",
        errno.EEXIST: "目标已存在",
        errno.EROFS: "只读文件系统",
        errno.EBUSY: "文件被占用",
        errno.EIO: "磁盘 IO 错误",
    }
    return mapping.get(exc.errno, f"系统错误 {exc.errno}")
