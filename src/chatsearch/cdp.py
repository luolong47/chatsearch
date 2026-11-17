"""极简 CDP 客户端：重载 Electron 应用的渲染进程。

为什么需要它：两个数据源的界面都不监听底层存储的变更——

- WorkBuddy：侧边栏会话列表是渲染进程启动/操作时拉取的，外部改 `workbuddy.db`
  后不会自己重查；
- DeepSeek Harness：归档列表来自 host 进程读取的 `storages/workspace.json`，
  外部改完需要界面重新拉一次。

两者都是 Electron，本地开着调试端口时重载渲染进程即可，不必重启整个应用。
WorkBuddy 固定 9222；DSH 用 `--remote-debugging-port=0` 随机分配，实际端口写在
`%APPDATA%\\@deepseek-ai\\dsh-desktop\\DevToolsActivePort` 的首行。

只依赖标准库：手写 WebSocket 握手 + 单帧收发（CDP 消息都很小）。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse
from urllib.request import urlopen

from .config import DSH_DEVTOOLS_FILE

DEFAULT_CDP_PORT = 9222
_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_LIST_SELECTOR = ".conversation-list-body-host"


def read_devtools_port(devtools_file: Path = DSH_DEVTOOLS_FILE) -> int | None:
    """从 Electron 的 DevToolsActivePort 文件读随机分配到的调试端口。"""
    try:
        first = devtools_file.read_text(encoding="utf-8", errors="replace").splitlines()[0]
        port = int(first.strip())
    except (OSError, ValueError, IndexError):
        return None
    return port if 0 < port < 65536 else None


def list_pages(port: int = DEFAULT_CDP_PORT, timeout: float = 2.0) -> list[dict]:
    """读取调试端口的页面列表；端口不通时抛 OSError/URLError。"""
    with urlopen(f"http://127.0.0.1:{port}/json/list", timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _read_until_headers(sock: socket.socket) -> bytes:
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise OSError("CDP 握手连接被关闭")
        buf += chunk
        if len(buf) > 65536:
            raise OSError("CDP 握手响应异常")
    return buf


def _ws_open(ws_url: str, timeout: float = 3.0) -> socket.socket:
    u = urlparse(ws_url)
    host = u.hostname or "127.0.0.1"
    port = u.port or 80
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    sock = socket.create_connection((host, port), timeout=timeout)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.sendall(req.encode("ascii"))
    head = _read_until_headers(sock)
    status = head.split(b"\r\n", 1)[0]
    if b" 101 " not in status:
        sock.close()
        raise OSError(f"WebSocket 握手失败：{status.decode('latin-1', 'replace')}")
    expect = base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest())
    if expect not in head:
        sock.close()
        raise OSError("WebSocket 握手校验失败（Accept 不匹配）")
    return sock


def _ws_send(sock: socket.socket, text: str) -> None:
    data = text.encode("utf-8")
    frame = bytearray([0x81])  # FIN + text
    n = len(data)
    if n < 126:
        frame.append(0x80 | n)
    elif n < 65536:
        frame.append(0x80 | 126)
        frame += struct.pack(">H", n)
    else:
        frame.append(0x80 | 127)
        frame += struct.pack(">Q", n)
    mask = os.urandom(4)
    frame += mask
    frame += bytes(b ^ mask[i & 3] for i, b in enumerate(data))
    sock.sendall(bytes(frame))


def _ws_recv(sock: socket.socket) -> str:
    def readn(n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise OSError("CDP 连接已关闭")
            buf += chunk
        return buf

    while True:
        b1, b2 = readn(2)
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack(">H", readn(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", readn(8))[0]
        mask = readn(4) if masked else b""
        data = readn(n)
        if mask:
            data = bytes(b ^ mask[i & 3] for i, b in enumerate(data))
        if opcode == 0x8:  # close
            raise OSError("CDP 连接被服务端关闭")
        if opcode in (0x1, 0x2):  # text / binary
            return data.decode("utf-8", "replace")


def _eval(sock: socket.socket, expr: str) -> object:
    _ws_send(sock, json.dumps({
        "id": 1,
        "method": "Runtime.evaluate",
        "params": {"expression": expr, "returnByValue": True},
    }))
    msg = json.loads(_ws_recv(sock))
    return (msg.get("result") or {}).get("result", {}).get("value")


def _reload_matching(
    port: int,
    predicate: Callable[[dict], bool],
    label: str,
    selector: str | None = None,
    timeout: float = 3.0,
) -> dict:
    """对命中的页面逐个发 Page.reload，返回 {"ok","pages","error","port"}。"""
    try:
        pages = list_pages(port)
    except Exception as e:  # noqa: BLE001 - 端口未开/被占用都按不可用处理
        return {"ok": False, "pages": 0, "port": port, "error": f"调试端口 {port} 不可达：{e}"}

    targets = [
        p for p in pages
        if p.get("type") == "page"
        and p.get("webSocketDebuggerUrl")
        and predicate(p)
    ]
    if not targets:
        return {"ok": False, "pages": 0, "port": port, "error": f"没有找到{label}"}

    reloaded = 0
    last_error = None
    for target in targets:
        sock = None
        try:
            sock = _ws_open(target["webSocketDebuggerUrl"], timeout=timeout)
            if selector and not _eval(sock, f"!!document.querySelector('{selector}')"):
                continue  # 不是目标窗口（比如设置页）
            _ws_send(sock, json.dumps({
                "id": 2,
                "method": "Page.reload",
                "params": {"ignoreCache": False},
            }))
            _ws_recv(sock)  # 等 ack，确保消息已送达
            reloaded += 1
        except Exception as e:  # noqa: BLE001
            last_error = str(e)
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    if reloaded:
        return {"ok": True, "pages": reloaded, "port": port, "error": None}
    return {"ok": False, "pages": 0, "port": port, "error": last_error or f"没有可重载的{label}"}


def reload_workbuddy(port: int | None = None, timeout: float = 3.0) -> dict:
    """重载 WorkBuddy 的会话列表窗口（只看 file:// 且带列表容器的页面）。"""
    return _reload_matching(
        port or DEFAULT_CDP_PORT,
        lambda p: str(p.get("url", "")).startswith("file://"),
        "WorkBuddy 渲染窗口",
        _LIST_SELECTOR,
        timeout,
    )


def reload_dsh(port: int | None = None, timeout: float = 3.0) -> dict:
    """重载 DeepSeek Harness 的主窗口。

    端口来源优先用 DevToolsActivePort（快捷方式带 --remote-debugging-port=0 才会生成）；
    没有该文件说明 DSH 是用旧方式启动的，只能重启应用。
    """
    if port is None:
        port = read_devtools_port()
        if port is None:
            return {
                "ok": False,
                "pages": 0,
                "port": None,
                "error": "未发现 DSH 调试端口（DevToolsActivePort 不存在，可能是未带调试参数启动）",
            }
    return _reload_matching(
        port,
        lambda p: str(p.get("url", "")).startswith("dsh-app://"),
        "DSH 主窗口",
        None,
        timeout,
    )
