"""极简 CDP 客户端：只做「重载 WorkBuddy 渲染进程」这一件事。

为什么需要它：WorkBuddy 的侧边栏会话列表是渲染进程启动/操作时拉取的，
外部直接改 `workbuddy.db` 后它不会自己重查，用户必须重启应用才看得到。
Electron 默认开着本地调试端口（9222），重载渲染进程即可让它重新拉列表。

只依赖标准库：手写 WebSocket 握手 + 单帧收发（CDP 消息都很小）。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
from urllib.parse import urlparse
from urllib.request import urlopen

DEFAULT_CDP_PORT = 9222
_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_LIST_SELECTOR = ".conversation-list-body-host"


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


def reload_workbuddy(port: int = DEFAULT_CDP_PORT, timeout: float = 3.0) -> dict:
    """重载 WorkBuddy 的会话列表窗口。

    只看 `file://` 且带会话列表容器的页面（设置窗口不动），逐个发送
    Page.reload。返回 {"ok": bool, "pages": n, "error": str|None}。
    """
    try:
        pages = list_pages(port)
    except Exception as e:  # noqa: BLE001 - 端口未开/被占用都按不可用处理
        return {"ok": False, "pages": 0, "error": f"调试端口 {port} 不可达：{e}"}

    targets = [
        p for p in pages
        if p.get("type") == "page"
        and str(p.get("url", "")).startswith("file://")
        and p.get("webSocketDebuggerUrl")
    ]
    if not targets:
        return {"ok": False, "pages": 0, "error": "没有找到 WorkBuddy 渲染窗口"}

    reloaded = 0
    last_error = None
    for target in targets:
        sock = None
        try:
            sock = _ws_open(target["webSocketDebuggerUrl"], timeout=timeout)
            if not _eval(sock, f"!!document.querySelector('{_LIST_SELECTOR}')"):
                continue  # 不是会话列表窗口（比如设置页）
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
        return {"ok": True, "pages": reloaded, "error": None}
    return {"ok": False, "pages": 0, "error": last_error or "没有可重载的会话列表窗口"}
