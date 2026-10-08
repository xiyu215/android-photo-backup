"""离线自检：证明本工具在完全断网的环境下也能跑。

为什么要专门写一个自检脚本？
    用户的核心约束之一是"不使用任何 AI 模型/API"，并且要在**局域网**里工作。
    这类需求最容易在细节上翻车：某个前端页面偷偷引了一个 CDN 上的字体、
    某个库在导入时 ping 一下更新服务器——平时完全看不出来，一到断网环境就崩。
    所以这里用静态扫描把"可能联网"的地方全部揪出来，宁可误报也不放过。

检查三类东西：
    1. 前端资源引用（<script src> / <link href> / @import / fetch() 绝对地址）
    2. Python 代码里的网络调用（requests / urllib / http.client / socket.connect）
    3. 纯文本里的外链（只提示，不算失败——注释和文档里的链接是正常的）

用法：
    python tools/check_offline.py            # 检查整个项目
    python tools/check_offline.py --strict   # 连"提示"也算失败
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# 允许出现外链的文件（都是给人看的文档，程序运行时不会去访问）
# ---------------------------------------------------------------------------
DOC_SUFFIXES = {".md", ".txt", ".rst"}

# 这些链接是"安装/排错指引"，允许出现在脚本与注释里
ALLOWED_URL_PATTERNS = (
    "python.org", "pypi.org", "pypi.tuna.tsinghua.edu.cn",
    "mirrors.cloud.tencent.com", "docs.qq.com",
)

# 前端里绝不允许的外部资源特征
CDN_PATTERNS = (
    (re.compile(r"""<script[^>]+src\s*=\s*["'](https?:)?//""", re.I), "外部 <script> 引用"),
    (re.compile(r"""<link[^>]+href\s*=\s*["'](https?:)?//""", re.I), "外部 <link> 引用"),
    (re.compile(r"""<img[^>]+src\s*=\s*["'](https?:)?//""", re.I), "外部 <img> 引用"),
    (re.compile(r"""@import\s+(url\()?["']?(https?:)?//""", re.I), "CSS @import 外部样式"),
    (re.compile(r"""url\(\s*["']?(https?:)?//""", re.I), "CSS url() 外部资源"),
    (re.compile(r"""fonts\.googleapis|fonts\.gstatic|cdn\.jsdelivr|unpkg\.com|cdnjs""", re.I),
     "已知 CDN 域名"),
)

# Python 里可能联网的调用（有些是正常的，比如监听本地端口，所以只提示）
PY_NET_CALLS = (
    (re.compile(r"\brequests\.(get|post|put|delete|head|Session)\b"), "requests 发起 HTTP 请求"),
    (re.compile(r"\burllib\.request\.urlopen\b"), "urllib 发起 HTTP 请求"),
    (re.compile(r"\bhttp\.client\.(HTTPS?Connection)\b"), "http.client 发起 HTTP 请求"),
    (re.compile(r"\bhttpx\.(get|post|Client|AsyncClient)\b"), "httpx 发起 HTTP 请求"),
    (re.compile(r"\baiohttp\.(ClientSession|request)\b"), "aiohttp 发起 HTTP 请求"),
    (re.compile(r"\.connect\(\s*\(\s*[\"'](?!127\.|localhost|0\.0\.0\.0)"), "socket 连接外部地址"),
)

# 这些文件里出现网络调用是"实现本身"，不是违规
PY_ALLOW_FILES = {"netutil.py"}   # 只做本机网卡探测（UDP connect 到一个不存在的地址，不发包）


def iter_files(suffixes: set[str], skip_dirs: set[str]):
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file():
            continue
        if any(part in skip_dirs for part in p.parts):
            continue
        if p.suffix.lower() in suffixes:
            yield p


def read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def main() -> int:
    ap = argparse.ArgumentParser(description="离线可用性自检")
    ap.add_argument("--strict", action="store_true", help="把提示也当成失败")
    args = ap.parse_args()

    failures: list[str] = []
    notes: list[str] = []

    skip = {"build", "dist", "__pycache__", ".git", ".venv", "venv", "node_modules"}

    print("=" * 68)
    print("  离线可用性自检")
    print("=" * 68)
    print(f"  项目根目录：{ROOT}")
    print()

    # ---- 1. 前端 ----
    print("[1/3] 检查前端资源引用（webui/）...")
    web_files = list(iter_files({".html", ".htm", ".css", ".js"}, skip))
    web_files = [p for p in web_files if "webui" in p.parts or p.suffix in (".html", ".css", ".js")]
    hit = 0
    for p in web_files:
        text = read_text(p)
        for pat, label in CDN_PATTERNS:
            for m in pat.finditer(text):
                line = text[:m.start()].count("\n") + 1
                failures.append(f"{p.relative_to(ROOT)}:{line}  {label} —— {m.group(0)[:80]}")
                hit += 1
    print(f"      扫描 {len(web_files)} 个前端文件，发现 {hit} 处外部引用")
    print()

    # ---- 2. Python 网络调用 ----
    print("[2/3] 检查 Python 代码里的网络调用...")
    py_files = list(iter_files({".py"}, skip))
    hit = 0
    for p in py_files:
        if p.name in PY_ALLOW_FILES:
            continue
        text = read_text(p)
        for pat, label in PY_NET_CALLS:
            for m in pat.finditer(text):
                line = text[:m.start()].count("\n") + 1
                failures.append(f"{p.relative_to(ROOT)}:{line}  {label} —— {m.group(0)[:60]}")
                hit += 1
    print(f"      扫描 {len(py_files)} 个 Python 文件，发现 {hit} 处可疑网络调用")
    print()

    # ---- 3. 外链提示 ----
    # 只统计"我们自己写的代码"里的外链。内置 adb 的许可证文本、第三方包的
    # 版权声明里必然有大量 URL，把它们算进来只会淹没真正值得看的提示。
    print("[3/3] 统计文档/注释里的外链（仅提示）...")
    url_re = re.compile(r"https?://[^\s'\"<>)\]]+")
    skip_url_dirs = {"adb", "build", "dist", "__pycache__", ".git", ".venv", "venv"}
    per_file: dict[str, list[str]] = {}
    for p in iter_files({".md", ".txt", ".rst", ".bat", ".py", ".spec"}, skip):
        if any(part in skip_url_dirs for part in p.parts):
            continue
        text = read_text(p)
        for m in url_re.finditer(text):
            url = m.group(0).rstrip(".,;:")
            if any(a in url for a in ALLOWED_URL_PATTERNS):
                continue
            per_file.setdefault(str(p.relative_to(ROOT)), []).append(url)
    total = sum(len(v) for v in per_file.values())
    print(f"      自有代码/文档中共有 {total} 处外链（多为注释与使用说明）")
    print()

    # ---- 结论 ----
    print("=" * 68)
    if per_file:
        print("  提示（不影响离线运行，仅供人工确认）：")
        for fname, urls in sorted(per_file.items(), key=lambda kv: -len(kv[1]))[:8]:
            uniq = sorted(set(urls))
            print(f"    · {fname} —— {len(urls)} 处，例如：{uniq[0][:70]}")
        if len(per_file) > 8:
            print(f"    …… 另有 {len(per_file) - 8} 个文件含外链")
        print()

    if failures:
        print(f"  [失败] 发现 {len(failures)} 处会导致联网的代码：")
        for f in failures:
            print(f"    ! {f}")
        print()
        print("  必须修掉这些，否则断网环境下程序会卡住或崩溃。")
        print("=" * 68)
        return 1

    print("  [通过] 没有发现任何会导致程序联网的代码。")
    print("         本工具在完全断网的局域网里可以正常使用。")
    print("=" * 68)

    if args.strict and per_file:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
