"""配置读取、校验与"备份根目录"三级降级。

配置文件形态
------------
纯文本 ``键=值``，``#`` 开头为注释，记事本可直接编辑。
刻意**不使用** configparser 的分节语法：用户要的是"打开就能看懂、改完就能用"，
分节会让文件变长且容易改错。解析器对未知键只警告不报错，保证向后兼容。
"""

from __future__ import annotations

import ctypes
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ..common import paths
from ..common.errors import EnvError
from ..common.logger import get_logger

log = get_logger("config")

CONFIG_FILENAME = "配置.txt"
DATA_DIRNAME = ".photobak"

DEFAULT_ARCHIVE_DIR = "照片归档"
DEFAULT_MIRROR_DIR = "手机镜像"
DEFAULT_UNCERTAIN_DIR = "_时间存疑"
DEFAULT_UNKNOWN_DIR = "_未知时间"

DEFAULT_IMAGE_EXT = ["jpg", "jpeg", "png", "gif", "webp", "bmp", "heic", "heif",
                     "dng", "tif", "tiff", "avif"]
DEFAULT_VIDEO_EXT = ["mp4", "mov", "3gp", "3gpp", "mkv", "avi", "wmv", "flv",
                     "m4v", "mpg", "mpeg", "ts", "webm"]
DEFAULT_AUDIO_EXT = ["m4a", "aac", "mp3", "wav", "flac", "ogg", "amr"]

DEFAULT_APP_MAP = {
    "dcim/camera": "Camera",
    "dcim/100andro": "Camera",
    "dcim/screenshots": "Screenshot",
    "pictures/screenshots": "Screenshot",
    "pictures/weixin": "WeiXin",
    "pictures/微信": "WeiXin",
    "tencent/micromsg": "WeiXin",
    "tencent/qq_images": "QQ",
    "tencent/qq_thumbnails": "QQ",
    "tencent/qqfile_recv": "QQ",
    "pictures/微博": "Weibo",
    "sina/weibo": "Weibo",
    "download": "Download",
    "documents": "Download",
}


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def _as_bool(value: str, default: bool) -> bool:
    v = str(value).strip().lower()
    if v in ("1", "true", "yes", "y", "on", "是", "开", "开启"):
        return True
    if v in ("0", "false", "no", "n", "off", "否", "关", "关闭"):
        return False
    return default


def _as_int(value: str, default: int, lo: int | None = None, hi: int | None = None) -> int:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    if lo is not None and n < lo:
        n = lo
    if hi is not None and n > hi:
        n = hi
    return n


def _as_list(value: str, default: list[str]) -> list[str]:
    if value is None:
        return list(default)
    parts = [p.strip().lower().lstrip(".") for p in str(value).replace("；", ";").split(";")]
    parts = [p for p in parts if p]
    return parts or list(default)


def _as_choice(value: str, choices: tuple[str, ...], default: str) -> str:
    v = str(value).strip().lower()
    return v if v in choices else default


# ---------------------------------------------------------------------------
# 磁盘枚举
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DriveInfo:
    letter: str
    kind: str          # fixed / removable / network / cdrom / ramdisk / unknown
    free: int
    total: int

    @property
    def root(self) -> str:
        return f"{self.letter}:\\"

    @property
    def is_system(self) -> bool:
        return self.letter.upper() == os.environ.get("SystemDrive", "C:").rstrip(":\\").upper()


_DRIVE_KIND = {2: "removable", 3: "fixed", 4: "network", 5: "cdrom", 6: "ramdisk"}


def list_drives() -> list[DriveInfo]:
    """枚举所有可用盘符及其剩余空间。

    过滤掉光驱、网络盘和 RAM 盘——它们要么不可写、要么不适合放大体量备份。
    """
    out: list[DriveInfo] = []
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        root = f"{letter}:\\"
        if not os.path.exists(root):
            continue
        kind = "unknown"
        if os.name == "nt":
            try:
                code = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root))
                kind = _DRIVE_KIND.get(code, "unknown")
            except Exception:  # noqa: BLE001
                pass
        if kind in ("cdrom", "ramdisk", "network"):
            continue
        try:
            usage = __import__("shutil").disk_usage(root)
            free, total = int(usage.free), int(usage.total)
        except OSError:
            continue
        if total <= 0:
            continue
        out.append(DriveInfo(letter, kind, free, total))
    return out


# ---------------------------------------------------------------------------
# 配置对象
# ---------------------------------------------------------------------------
@dataclass
class Config:
    """全部配置项。字段名与「配置.txt」的键一一对应（见 :data:`KEY_TO_FIELD`）。"""

    # 存储
    backup_root: str = ""
    mirror_link_mode: str = "auto"          # auto / hardlink / copy / off
    archive_dir: str = DEFAULT_ARCHIVE_DIR
    mirror_dir: str = DEFAULT_MIRROR_DIR

    # 扫描范围
    include_dirs: list[str] = field(default_factory=lambda: ["DCIM", "Pictures", "Movies", "Download"])
    include_android_data: bool = False
    ignore_nomedia: bool = False
    image_ext: list[str] = field(default_factory=lambda: list(DEFAULT_IMAGE_EXT))
    video_ext: list[str] = field(default_factory=lambda: list(DEFAULT_VIDEO_EXT))
    include_audio: bool = False
    audio_ext: list[str] = field(default_factory=lambda: list(DEFAULT_AUDIO_EXT))

    # 校验与安全
    second_read: str = "auto"               # auto / always / never
    second_read_threshold_mb: int = 100
    max_retries: int = 3
    max_file_mb: int = 4096                 # 单文件上限，超过则拒绝并跳过（Q: 用户指定）
    uncertain_can_delete: bool = False
    keep_camera_original: bool = False

    # 网络
    port: int = 8765
    access_token: str = ""
    heartbeat_sec: int = 15

    # App 目录映射
    app_map: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_APP_MAP))

    # 反向下载
    restore_dir: str = "/sdcard/DCIM/Restored"
    enable_restore: bool = True

    # 界面
    color: str = "auto"
    auto_open_browser: bool = True
    log_level: str = "INFO"
    tray_icon: bool = True                  # Q15：托盘图标
    minimize_on_close: bool = True          # Q15：关闭时最小化

    # 运行时（不来自文件）
    config_path: Path | None = None
    base_dir: Path | None = None            # 程序目录
    data_dir: Path | None = None            # .photobak 目录

    # ------------------------------------------------------------------
    # 派生属性
    # ------------------------------------------------------------------
    @property
    def backup_root_path(self) -> Path:
        return Path(self.backup_root)

    @property
    def archive_root(self) -> Path:
        return self.backup_root_path / self.archive_dir

    @property
    def mirror_root(self) -> Path:
        return self.backup_root_path / self.mirror_dir

    @property
    def index_db(self) -> Path:
        return self.backup_root_path / DATA_DIRNAME / "index.db"

    @property
    def tmp_dir(self) -> Path:
        return self.backup_root_path / DATA_DIRNAME / "tmp"

    @property
    def report_dir(self) -> Path:
        return self.backup_root_path / DATA_DIRNAME / "reports"

    @property
    def log_dir(self) -> Path:
        return self.backup_root_path / DATA_DIRNAME / "logs"

    @property
    def backup_dir(self) -> Path:
        return self.backup_root_path / DATA_DIRNAME / "backup"

    @property
    def restore_staging(self) -> Path:
        return self.backup_root_path / "恢复包"

    def all_media_ext(self) -> set[str]:
        exts = set(self.image_ext) | set(self.video_ext)
        if self.include_audio:
            exts |= set(self.audio_ext)
        return exts

    def kind_of(self, ext: str) -> str:
        e = ext.lower().lstrip(".")
        if e in self.image_ext:
            return "image"
        if e in self.video_ext:
            return "video"
        if e in self.audio_ext:
            return "audio"
        return "other"

    def max_file_bytes(self) -> int:
        return max(1, self.max_file_mb) * 1024 * 1024

    def second_read_enabled(self, size: int) -> bool:
        """该大小是否需要写后复读校验。"""
        if self.second_read == "never":
            return False
        if self.second_read == "always":
            return True
        return size >= self.second_read_threshold_mb * 1024 * 1024


KEY_TO_FIELD = {
    "备份根目录": "backup_root",
    "镜像树链接方式": "mirror_link_mode",
    "归档目录名": "archive_dir",
    "镜像目录名": "mirror_dir",
    "包含目录": "include_dirs",
    "包含AndroidData": "include_android_data",
    "忽略nomedia": "ignore_nomedia",
    "图片扩展名": "image_ext",
    "视频扩展名": "video_ext",
    "包含音频": "include_audio",
    "音频扩展名": "audio_ext",
    "二次读校验": "second_read",
    "二次读阈值MB": "second_read_threshold_mb",
    "最大重试次数": "max_retries",
    "单文件上限MB": "max_file_mb",
    "时间存疑可删": "uncertain_can_delete",
    "保留相机原片": "keep_camera_original",
    "端口": "port",
    "访问令牌": "access_token",
    "心跳间隔": "heartbeat_sec",
    "App目录映射": "app_map",
    "恢复目录": "restore_dir",
    "启用反向下载": "enable_restore",
    "彩色输出": "color",
    "自动打开浏览器": "auto_open_browser",
    "日志级别": "log_level",
    "托盘图标": "tray_icon",
    "关闭时最小化": "minimize_on_close",
}


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
def parse_text(text: str) -> tuple[dict[str, str], list[str]]:
    """解析配置文本，返回 (键值对, 未知键列表)。

    容错策略：``#``/``;`` 开头为注释；不含 ``=`` 的行忽略；
    键名两端空白与全角空格都会被清理（中文输入法下极易混入全角空格）。
    """
    data: dict[str, str] = {}
    unknown: list[str] = []
    for raw in text.splitlines():
        line = raw.replace("\u3000", " ").strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key in KEY_TO_FIELD:
            data[key] = value
        else:
            unknown.append(key)
    return data, unknown


def _parse_app_map(value: str) -> dict[str, str]:
    """解析 ``关键词:标签,关键词:标签`` 形式的映射。"""
    mapping = dict(DEFAULT_APP_MAP)
    for item in str(value).replace("；", ";").replace("，", ",").split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        k, _, v = item.partition(":")
        k = k.strip().lower().replace("\\", "/").strip("/")
        v = v.strip()
        if k and v:
            mapping[k] = v
    return mapping


def load(config_path: str | Path | None = None) -> Config:
    """加载配置。文件不存在时**自动生成**一份带注释的默认配置。"""
    base = paths.app_dir()
    cfg_path = Path(config_path) if config_path else (base / CONFIG_FILENAME)

    cfg = Config()
    cfg.config_path = cfg_path
    cfg.base_dir = base

    if not cfg_path.is_file():
        try:
            write_default(cfg_path)
            log.info("未找到配置文件，已自动生成默认配置：%s", cfg_path)
        except OSError as exc:
            log.warning("无法写入默认配置文件（%s）：%s", cfg_path, exc)

    unknown: list[str] = []
    try:
        text = cfg_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        # 用户可能用 GBK 保存过；做一次兼容读取
        text = cfg_path.read_text(encoding="gbk", errors="replace")
        log.warning("配置文件不是 UTF-8 编码，已按 GBK 读取；建议另存为 UTF-8")
    except OSError as exc:
        log.warning("读取配置文件失败（%s），将全部使用默认值", exc)
        text = ""

    data, unknown = parse_text(text)
    if unknown:
        log.warning("配置文件中有无法识别的项（已忽略）：%s", "、".join(unknown))

    if "备份根目录" in data:
        cfg.backup_root = data["备份根目录"].strip().strip('"')
    cfg.mirror_link_mode = _as_choice(data.get("镜像树链接方式", "auto"),
                                      ("auto", "hardlink", "copy", "off"), "auto")
    cfg.archive_dir = data.get("归档目录名", DEFAULT_ARCHIVE_DIR).strip() or DEFAULT_ARCHIVE_DIR
    cfg.mirror_dir = data.get("镜像目录名", DEFAULT_MIRROR_DIR).strip() or DEFAULT_MIRROR_DIR

    cfg.include_dirs = [d.strip().strip("/") for d in
                        _split_dirs(data.get("包含目录", "DCIM;Pictures;Movies;Download"))]
    cfg.include_android_data = _as_bool(data.get("包含AndroidData", "0"), False)
    cfg.ignore_nomedia = _as_bool(data.get("忽略nomedia", "0"), False)
    cfg.image_ext = _as_list(data.get("图片扩展名"), DEFAULT_IMAGE_EXT)
    cfg.video_ext = _as_list(data.get("视频扩展名"), DEFAULT_VIDEO_EXT)
    cfg.include_audio = _as_bool(data.get("包含音频", "0"), False)
    cfg.audio_ext = _as_list(data.get("音频扩展名"), DEFAULT_AUDIO_EXT)

    cfg.second_read = _as_choice(data.get("二次读校验", "auto"),
                                 ("auto", "always", "never"), "auto")
    cfg.second_read_threshold_mb = _as_int(data.get("二次读阈值MB", "100"), 100, 1, 100_000)
    cfg.max_retries = _as_int(data.get("最大重试次数", "3"), 3, 0, 10)
    cfg.max_file_mb = _as_int(data.get("单文件上限MB", "4096"), 4096, 1, 10_000_000)
    cfg.uncertain_can_delete = _as_bool(data.get("时间存疑可删", "0"), False)
    cfg.keep_camera_original = _as_bool(data.get("保留相机原片", "0"), False)

    cfg.port = _as_int(data.get("端口", "8765"), 8765, 1024, 65535)
    cfg.access_token = data.get("访问令牌", "").strip()
    cfg.heartbeat_sec = _as_int(data.get("心跳间隔", "15"), 15, 5, 300)

    if "App目录映射" in data:
        cfg.app_map = _parse_app_map(data["App目录映射"])

    cfg.restore_dir = data.get("恢复目录", "/sdcard/DCIM/Restored").strip() or "/sdcard/DCIM/Restored"
    cfg.enable_restore = _as_bool(data.get("启用反向下载", "1"), True)

    cfg.color = _as_choice(data.get("彩色输出", "auto"), ("auto", "always", "never"), "auto")
    cfg.auto_open_browser = _as_bool(data.get("自动打开浏览器", "1"), True)
    cfg.log_level = str(data.get("日志级别", "INFO")).strip().upper() or "INFO"
    cfg.tray_icon = _as_bool(data.get("托盘图标", "1"), True)
    cfg.minimize_on_close = _as_bool(data.get("关闭时最小化", "1"), True)

    return cfg


def _split_dirs(value: str) -> list[str]:
    return [p for p in str(value).replace("；", ";").split(";") if p.strip()] or ["DCIM"]


# ---------------------------------------------------------------------------
# 备份根目录三级降级
# ---------------------------------------------------------------------------
def _validate_root(p: Path) -> tuple[bool, str]:
    """校验备份根是否可用，返回 (是否可用, 原因)。"""
    try:
        if p.exists() and not p.is_dir():
            return False, "该路径不是文件夹"
        probe = p
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        if not probe.exists():
            return False, "所在盘符不存在"
        if not paths.is_writable(probe if probe.is_dir() else probe.parent):
            return False, "没有写入权限"
        if paths.free_space(probe) <= 0:
            return False, "所在磁盘不可写或剩余空间为 0"
        return True, ""
    except OSError as exc:
        return False, str(exc)


def resolve_backup_root(cfg: Config, *, interactive: bool = True, prompt=input) -> Path:
    """三级降级确定备份根目录。

    ① 配置指定路径 → 可用则直接用；
    ② 自动扫描所有盘，取**剩余空间最大**的非系统盘；
    ③ 列出所有盘让用户点选（可输入自定义路径）。

    每一步都会在终端明确告知"本次用的是哪个目录、为什么"，
    避免用户以为备份跑到了别处。
    """
    # ---- ① 配置指定 ----
    if cfg.backup_root:
        p = Path(cfg.backup_root)
        ok, why = _validate_root(p)
        if ok:
            log.info("备份根目录 = %s（来源：配置文件）", p)
            return _ensure_root(p, cfg)
        log.warning("配置的备份根目录不可用（%s）：%s", p, why)

    drives = list_drives()
    if not drives:
        raise EnvError("E301", "未检测到任何可用的本地磁盘")

    # 优先非系统盘
    preferred = [d for d in drives if not d.is_system] or drives
    preferred.sort(key=lambda d: d.free, reverse=True)

    # ---- ② 自动选择 ----
    if not interactive:
        best = preferred[0]
        root = Path(best.root) / "安卓相册备份"
        log.info("备份根目录 = %s（来源：自动选择剩余空间最大的盘，剩余 %s）",
                 root, paths.human_size(best.free))
        return _ensure_root(root, cfg)

    # 交互模式下也先给推荐，但让用户有机会改
    print()
    print("检测到以下磁盘（按剩余空间排序）：")
    for i, d in enumerate(drives, 1):
        tag = "（系统盘）" if d.is_system else ""
        mark = "  ★ 推荐" if d is preferred[0] else ""
        print(f"  [{i}] {d.root:<4} 剩余 {paths.human_size(d.free):>10} / 共 "
              f"{paths.human_size(d.total)}{tag}{mark}")
    print()

    default_idx = drives.index(preferred[0]) + 1
    try:
        raw = prompt(f"请输入要使用的盘序号（直接回车使用 [{default_idx}]，"
                     f"或直接粘贴一个完整路径）：").strip()
    except (EOFError, KeyboardInterrupt):
        raw = ""

    if raw and (os.sep in raw or "/" in raw or (len(raw) > 1 and raw[1] == ":")):
        # 用户直接给了路径
        custom = Path(raw.strip('"').strip())
        root = custom if custom.name == "安卓相册备份" else custom / "安卓相册备份"
        ok, why = _validate_root(root)
        if ok:
            log.info("备份根目录 = %s（来源：手动指定）", root)
            return _ensure_root(root, cfg)
        log.warning("手动指定的路径不可用（%s）：%s，回退到推荐项", root, why)

    idx = _as_int(raw, default_idx, 1, len(drives)) if raw else default_idx
    chosen = drives[idx - 1]
    root = Path(chosen.root) / "安卓相册备份"
    log.info("备份根目录 = %s（来源：手动选择，剩余 %s）", root, paths.human_size(chosen.free))
    return _ensure_root(root, cfg)


def _ensure_root(root: Path, cfg: Config) -> Path:
    """创建备份根及其内部结构。"""
    try:
        paths.ensure_dir(root)
        paths.ensure_dir(root / DATA_DIRNAME)
        paths.ensure_dir(root / DATA_DIRNAME / "tmp")
        paths.ensure_dir(root / DATA_DIRNAME / "logs")
        paths.ensure_dir(root / DATA_DIRNAME / "reports")
        paths.ensure_dir(root / DATA_DIRNAME / "backup")
        paths.ensure_dir(root / cfg.archive_dir)
        paths.ensure_dir(root / cfg.mirror_dir)
    except OSError as exc:
        raise EnvError("E308", f"无法创建备份目录 {root}：{exc}") from exc
    cfg.backup_root = str(root)
    cfg.data_dir = root / DATA_DIRNAME
    return root


# ---------------------------------------------------------------------------
# 默认配置文件
# ---------------------------------------------------------------------------
def write_default(path: Path) -> None:
    """写出带完整注释的默认配置文件。"""
    paths.ensure_dir(path.parent)
    path.write_text(DEFAULT_CONFIG_TEXT, encoding="utf-8")


DEFAULT_CONFIG_TEXT = """# ============================================================
#  安卓手机相册局域网备份工具 —— 配置文件
#  纯文本，记事本可直接编辑；以 # 开头的是注释
#  修改后需要重启程序才会生效
#  等号左边的键名请不要改动或删除
# ============================================================

# ---------- 存储 ----------
# 备份根目录。留空 = 自动选择剩余空间最大的非系统盘
# 示例：备份根目录=D:\\安卓相册备份
备份根目录=

# 镜像树链接方式：auto(推荐) / hardlink / copy / off
#   auto     = 优先硬链接（同一份实体、两棵树都能看到，不占额外空间），失败自动复制
#   hardlink = 只允许硬链接，失败则报错
#   copy     = 直接复制（占用双倍空间，最保险）
#   off      = 不建镜像树，只保留按时间的归档树（最省空间）
镜像树链接方式=auto

# 归档目录名 / 镜像目录名（可改成你喜欢的中文或英文）
归档目录名=照片归档
镜像目录名=手机镜像

# ---------- 扫描范围 ----------
# 默认包含的目录（分号分隔，相对手机存储根 /sdcard）
包含目录=DCIM;Pictures;Movies;Download

# 是否包含 Android/data（各 App 的私有目录，可能含大量缓存）
包含AndroidData=0

# 是否忽略 .nomedia 标记
#   0 = 遇到 .nomedia 就跳过该目录（推荐，避免把 App 缓存当照片备份）
#   1 = 忽略该标记，仍然备份
忽略nomedia=0

# 扩展名白名单（分号分隔，不含点，不区分大小写）
图片扩展名=jpg;jpeg;png;gif;webp;bmp;heic;heif;dng;tif;tiff;avif
视频扩展名=mp4;mov;3gp;3gpp;mkv;avi;wmv;flv;m4v;mpg;mpeg;ts;webm

# 是否包含音频（0=不备份音乐/录音）
包含音频=0
音频扩展名=m4a;aac;mp3;wav;flac;ogg;amr

# ---------- 校验与安全 ----------
# 写盘后二次读校验（防止硬盘静默损坏）：
#   auto   = 只对超过下面阈值的文件复读（推荐）
#   always = 全部文件都复读（最安全，耗时增加约 30-60%）
#   never  = 不复读（最快）
二次读校验=auto
二次读阈值MB=100

# 哈希校验失败时最多重试几次
最大重试次数=3

# 单文件大小上限（MB）。超过上限的文件会被**拒绝并跳过**，不会上传。
# 默认 4096 = 4GB。若你有超大视频需要备份，可调大，例如 8192。
单文件上限MB=4096

# 时间存疑的文件（时间来自文件修改时间，可能是转存/下载当天）是否允许进入「可放心删」
#   0 = 不允许（推荐，更安全）
时间存疑可删=0

# 是否把"相机原片"归入「建议保留」档（0=直接进「可放心删」）
保留相机原片=0

# ---------- 网络 ----------
# 本地管理页端口（被占用时会自动 +1 递增）
端口=8765

# 访问令牌。留空 = 每次启动随机生成（推荐），终端与二维码中会显示
访问令牌=

# WebSocket 心跳间隔（秒）
心跳间隔=15

# ---------- App 目录映射（用于在清单里标注文件来源） ----------
# 格式：路径关键词:标签,路径关键词:标签
App目录映射=dcim/camera:Camera,dcim/screenshots:Screenshot,pictures/screenshots:Screenshot,pictures/weixin:WeiXin,tencent/micromsg:WeiXin,tencent/qq_images:QQ,tencent/qqfile_recv:QQ,pictures/微博:Weibo,download:Download

# ---------- 反向下载（把电脑上的照片放回手机） ----------
# 手机上接收恢复文件的目录
恢复目录=/sdcard/DCIM/Restored
# 是否启用反向下载（0=禁用，本工具就完全不会写入手机）
启用反向下载=1

# ---------- 界面 ----------
# 彩色输出：auto / always / never
彩色输出=auto
# 是否自动打开浏览器管理页
自动打开浏览器=1
# 日志级别：DEBUG / INFO / WARN / ERROR
日志级别=INFO
# 是否显示托盘图标（关闭窗口时最小化到托盘）
托盘图标=1
# 点关闭按钮时最小化到托盘而不是退出
关闭时最小化=1
"""
