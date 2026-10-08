"""网络工具：局域网地址探测、端口占用避让、连通性判断。

为什么需要这些？
    本项目 WiFi 通道走的是 ADB over TCP/IP：终端要给用户展示"手机该连哪个
    地址"，同时本地管理页要监听一个端口。多网卡（有线 + 无线 + 虚拟机网卡 +
    VPN）在 Windows 上非常常见，如果选错 IP，手机永远连不上，而用户很难自查。
    因此这里做一次"尽力而为但尽量准确"的地址优选。
"""

from __future__ import annotations

import ipaddress
import socket
import subprocess
import sys
from dataclasses import dataclass

from .logger import get_logger

log = get_logger("net")

# 私有网段（RFC1918），局域网备份场景下优先使用
_PRIVATE_NETS = [
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
]


@dataclass(frozen=True)
class LanCandidate:
    """一个候选的局域网地址。"""

    ip: str
    score: int
    reason: str


def _is_usable(ip: str) -> bool:
    """过滤掉回环、链路本地、组播、广播等不可用于局域网的地址。"""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.is_loopback or addr.is_link_local or addr.is_multicast:
        return False
    if addr.is_unspecified or str(addr).startswith("169.254."):
        return False
    return addr.version == 4


def _private_score(ip: str) -> int:
    """私有网段得分：192.168.x.x 最常见（家用路由器），给最高分。"""
    addr = ipaddress.ip_address(ip)
    for idx, net in enumerate(_PRIVATE_NETS):
        if addr in net:
            return (len(_PRIVATE_NETS) - idx) * 10
    return 0  # 公网地址（企业网/校园网直连）给 0 分，仍然可用


def default_route_ip() -> str | None:
    """取"默认路由出口"对应的本机 IP。

    技巧：对一个外部地址做 UDP connect——**不会真的发包**，
    只是让内核按路由表选出源地址。这是跨平台获得"最可能可用 IP"的
    最省事办法（无需依赖 psutil 或解析 ipconfig 的本地化输出）。
    """
    for probe in ("8.8.8.8", "223.5.5.5", "1.1.1.1"):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.settimeout(0.3)
            s.connect((probe, 80))
            ip = s.getsockname()[0]
            if _is_usable(ip):
                return ip
        except OSError:
            continue
        finally:
            s.close()
    return None


def _hostname_ips() -> list[str]:
    """通过主机名解析出本机所有 IPv4 地址。"""
    out: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if _is_usable(ip):
                out.append(ip)
    except (socket.gaierror, OSError):
        pass
    return out


def _ipconfig_ips() -> list[str]:
    """兜底：解析 ipconfig 输出中的 IPv4 地址。

    不依赖中文字段名，只提取形如 ``x.x.x.x`` 的 token，
    再用 :func:`_is_usable` 过滤，因此不受系统语言影响。
    """
    if sys.platform != "win32":
        return []
    try:
        raw = subprocess.run(
            ["ipconfig"], capture_output=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.decode("gbk", errors="ignore")
    except (OSError, subprocess.SubprocessError):
        return []

    out: list[str] = []
    for token in raw.replace("(", " ").replace(")", " ").split():
        token = token.strip(":. ")
        if token.count(".") == 3:
            if _is_usable(token):
                out.append(token)
    return out


def list_lan_candidates() -> list[LanCandidate]:
    """列出所有候选局域网地址，按"推荐度"降序返回。"""
    seen: dict[str, LanCandidate] = {}

    def _add(ip: str, bonus: int, reason: str) -> None:
        score = _private_score(ip) + bonus
        old = seen.get(ip)
        if old is None or score > old.score:
            seen[ip] = LanCandidate(ip, score, reason)

    route = default_route_ip()
    if route:
        _add(route, 50, "默认路由出口（最可能是当前联网的网卡）")
    for ip in _hostname_ips():
        _add(ip, 20, "主机名解析")
    for ip in _ipconfig_ips():
        _add(ip, 5, "ipconfig 枚举")

    return sorted(seen.values(), key=lambda c: c.score, reverse=True)


def best_lan_ip() -> str | None:
    """返回最推荐的局域网 IP；无可用地址返回 None。"""
    cands = list_lan_candidates()
    return cands[0].ip if cands else None


# ---------------------------------------------------------------------------
# 端口
# ---------------------------------------------------------------------------
def is_port_free(port: int, host: str = "0.0.0.0") -> bool:
    """检测端口是否可绑定。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def find_free_port(start: int, end: int | None = None) -> int:
    """从 ``start`` 开始向后找一个空闲端口。

    用户要求：端口 8765 被占用时要能自动避让，而不是直接失败。
    """
    end = end or (start + 35)
    for port in range(start, end + 1):
        if is_port_free(port):
            return port
    raise OSError(f"{start}-{end} 范围内没有可用端口")


def is_port_listening(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    """检测本机某端口是否已在监听（用于服务启动自检）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, port)) == 0
    except OSError:
        return False
    finally:
        s.close()


# ---------------------------------------------------------------------------
# 连通性
# ---------------------------------------------------------------------------
def ping(ip: str, timeout_ms: int = 1200) -> bool:
    """发送一次 ICMP ping。失败（含被防火墙丢弃）返回 False。

    注意：部分安卓机型默认不响应 ping，因此 ping 失败**不能**直接判定
    "不可达"，只能作为 AP 隔离的**辅助**证据（见 :func:`guess_ap_isolation`）。
    """
    if sys.platform != "win32":
        cmd = ["ping", "-c", "1", "-W", str(max(1, timeout_ms // 1000)), ip]
    else:
        cmd = ["ping", "-n", "1", "-w", str(timeout_ms), ip]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, timeout=timeout_ms / 1000 + 2,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def tcp_probe(ip: str, port: int, timeout: float = 1.5) -> bool:
    """尝试 TCP 连接（比 ping 更能反映"服务可达"）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex((ip, port)) == 0
    except OSError:
        return False
    finally:
        s.close()


def guess_ap_isolation(peer_ip: str) -> bool:
    """粗略判断"路由器是否开启了 AP 隔离"。

    判定逻辑（保守，宁可漏报不误报）：
        本机能拿到自己的局域网 IP，但 ping 不通对端，
        且对端地址属于同一私有网段 → 高度怀疑 AP 隔离。

    注意这只是一个**提示信号**，不阻断流程；真正的连接失败仍由 ADB 报错决定。
    """
    if not peer_ip or not _is_usable(peer_ip):
        return False
    mine = best_lan_ip()
    if not mine:
        return False
    try:
        if ipaddress.ip_address(mine).network != ipaddress.ip_address(peer_ip).network:
            return False
    except ValueError:
        return False
    return not ping(peer_ip)


def describe_network() -> str:
    """生成一段人类可读的网络状态描述，用于终端展示与日志。"""
    cands = list_lan_candidates()
    if not cands:
        return "未检测到可用的局域网地址（可能未连接任何网络）"
    lines = [f"检测到 {len(cands)} 个局域网地址："]
    for i, c in enumerate(cands, 1):
        mark = "★ 推荐" if i == 1 else "  "
        lines.append(f"  {mark} {c.ip}   （{c.reason}）")
    return "\n".join(lines)
