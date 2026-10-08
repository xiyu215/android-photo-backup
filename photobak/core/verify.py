"""校验闭环：本项目的核心安全机制。

流程（对应设计文档 §7.1）
------------------------
    传输（可断点） → 落盘后本地全量 SHA-256 → 与源端哈希比对
      ├─ 一致 →（可选）写后二次读校验 → 原子替换到最终路径
      └─ 不一致 → 删除 .part，指数退避重试；连续失败则**拦截**

"拦截"的含义（用户明确要求）
----------------------------
校验失败的文件**绝不会**进入「可放心删」清单，即使用户在手机上把它删了，
也不会因为本工具的错误而丢数据。这是本项目的第一优先级。

关于"源端哈希不可得"
--------------------
MTP 等弱能力通道拿不到源端哈希（本项目已按 Q2 决定不做 MTP，但接口保留）。
此时无法做双端比对，只能保证"落盘字节 == 传输字节"。这种情况会在清单里
标记为「仅单向校验」，安全等级低于双端校验，用户可据此判断。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..common import paths
from ..common.errors import VerifyError
from ..common.logger import get_logger
from . import hashutil
from .config import Config

log = get_logger("verify")

#: 重试的指数退避延迟（秒）。次数不足时取最后一个值。
RETRY_DELAYS = (2.0, 5.0, 12.0, 30.0)


@dataclass
class CheckResult:
    """一次落盘校验的结论。"""

    ok: bool
    local_sha: str
    size: int
    second_read_ok: bool | None = None
    dual_sided: bool = True          # 是否做了双端比对
    detail: str = ""

    def __str__(self) -> str:  # pragma: no cover
        return (f"CheckResult(ok={self.ok}, sha={self.local_sha[:16]}…, "
                f"size={self.size}, second={self.second_read_ok}, dual={self.dual_sided})")


@dataclass
class VerifyOutcome:
    """一次"传输 + 校验"的整体结果。"""

    ok: bool
    local_sha: str | None
    size: int
    attempts: int
    second_read_ok: bool | None
    dual_sided: bool
    error_code: str = ""
    detail: str = ""


def check_landed(
    part_path: str | Path,
    *,
    expected_sha: str | None,
    expected_size: int | None = None,
    second_read: bool = False,
    progress: Callable[[int, int], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> CheckResult:
    """对已落盘的临时文件做完整校验。

    :param expected_sha: 源端哈希；None 表示通道拿不到（只能单向校验）。
    :param second_read: 是否做写后复读校验（防硬盘静默损坏）。
    """
    p = Path(part_path)
    try:
        size = p.stat().st_size
    except OSError as exc:
        return CheckResult(False, "", 0, detail=f"临时文件不可读：{paths.errno_name(exc)}")

    if expected_size is not None and size != expected_size:
        return CheckResult(
            False, "", size,
            detail=f"落盘大小与源端不一致（源端 {expected_size} / 本地 {size}）",
        )

    try:
        result = hashutil.hash_file(p, progress=progress, cancel=cancel)
    except OSError as exc:
        return CheckResult(False, "", size, detail=f"计算哈希失败：{paths.errno_name(exc)}")
    except Exception as exc:  # noqa: BLE001 —— 含取消信号
        return CheckResult(False, "", size, detail=f"校验被中断：{exc}")

    local_sha = result.sha256
    dual = expected_sha is not None

    if dual and local_sha != expected_sha:
        return CheckResult(
            False, local_sha, size, dual_sided=True,
            detail=(f"哈希不一致（源端 {expected_sha[:16]}… / 本地 {local_sha[:16]}…）"),
        )

    # 写后复读校验：重新打开文件再算一遍，捕获"写下去的和读回来的是否一致"
    second_ok: bool | None = None
    if second_read:
        try:
            again = hashutil.hash_file(p)
            second_ok = (again.sha256 == local_sha)
            if not second_ok:
                return CheckResult(
                    False, local_sha, size, second_read_ok=False, dual_sided=dual,
                    detail="写盘后复读校验失败，疑似磁盘静默损坏",
                )
        except OSError as exc:
            second_ok = False
            log.warning("复读校验读取失败：%s", exc)

    detail = "双端校验通过" if dual else "仅单向校验（通道无法提供源端哈希）"
    return CheckResult(True, local_sha, size, second_read_ok=second_ok, dual_sided=dual, detail=detail)


class Verifier:
    """把"传输 + 校验 + 重试 + 拦截"打包成一次可复用的调用。"""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    def run(
        self,
        *,
        do_transfer: Callable[[bool], None],
        part_path: str | Path,
        expected_sha: str | None,
        expected_size: int,
        cancel: Callable[[], bool] | None = None,
        progress: Callable[[int, int], None] | None = None,
        on_retry: Callable[[int, str], None] | None = None,
    ) -> VerifyOutcome:
        """执行"传输→校验"，失败自动重试，最终失败则拦截。

        :param do_transfer: 回调，参数为 ``resume``（是否续传已有 .part）。
                            由编排层负责实现（调用具体通道）。
        :param on_retry: 重试前回调 ``(第几次, 原因)``，用于日志与 UI 提示。
        """
        attempts = 0
        last_detail = ""
        max_tries = max(1, self.cfg.max_retries)

        while attempts < max_tries:
            attempts += 1
            resume = attempts > 1 and Path(part_path).exists()
            try:
                do_transfer(resume)
            except Exception as exc:  # noqa: BLE001 —— 通道异常统一转成重试/拦截
                last_detail = f"传输失败：{exc}"
                log.warning("第 %d/%d 次传输失败：%s", attempts, max_tries, exc)
                if attempts >= max_tries:
                    break
                self._sleep(attempts, on_retry, last_detail)
                continue

            result = check_landed(
                part_path,
                expected_sha=expected_sha,
                expected_size=expected_size,
                second_read=self.cfg.second_read_enabled(expected_size),
                progress=progress,
                cancel=cancel,
            )

            if result.ok:
                return VerifyOutcome(
                    ok=True, local_sha=result.local_sha, size=result.size,
                    attempts=attempts, second_read_ok=result.second_read_ok,
                    dual_sided=result.dual_sided, detail=result.detail,
                )

            last_detail = result.detail
            log.warning("第 %d/%d 次校验未通过：%s", attempts, max_tries, result.detail)
            # 校验不通过：丢弃临时文件，避免把"部分正确"的内容续传下去
            remove_part(part_path)
            if attempts >= max_tries:
                break
            self._sleep(attempts, on_retry, last_detail)

        # 连续失败 → 拦截
        err = VerifyError("E503", last_detail or "校验连续失败")
        return VerifyOutcome(
            ok=False, local_sha=None, size=0, attempts=attempts,
            second_read_ok=None, dual_sided=expected_sha is not None,
            error_code=err.code, detail=err.user_message(),
        )

    @staticmethod
    def _sleep(attempt: int, on_retry, reason: str) -> None:
        delay = RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)]
        if on_retry:
            try:
                on_retry(attempt, reason)
            except Exception:  # noqa: BLE001
                pass
        time.sleep(delay)


# ---------------------------------------------------------------------------
# 落地辅助
# ---------------------------------------------------------------------------
def atomic_place(part_path: str | Path, dest: str | Path) -> None:
    """把临时文件原子地移动到最终位置。

    用 ``os.replace``（同卷内是原子操作）：要么旧文件，要么新文件，
    绝不会出现"写了一半的目标文件"。跨卷时 os.replace 会失败，
    此时退化为"复制 + 删除"，并在复制完成后才删除源文件。
    """
    src, dst = Path(part_path), Path(dest)
    paths.ensure_dir(dst.parent)
    try:
        os.replace(str(src), str(dst))
    except OSError as exc:
        if exc.errno == getattr(os, "EXDEV", 18):
            import shutil

            shutil.copy2(str(src), str(dst))
            remove_part(src)
        else:
            raise


def remove_part(part_path: str | Path) -> None:
    """安全删除临时文件（不存在也不报错）。"""
    try:
        Path(part_path).unlink()
    except OSError:
        pass


def fsync_file(path: str | Path) -> None:
    """把文件内容刷到物理磁盘。

    写索引之前必须做这一步：否则断电时可能出现"索引说文件已落盘，
    但文件内容还在系统缓存里丢失"——这正是我们要杜绝的"索引与文件不一致"。
    """
    try:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass
