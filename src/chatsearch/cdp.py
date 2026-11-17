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


def _eval(sock: socket.socket, expr: str, await_promise: bool = False) -> object:
    _ws_send(sock, json.dumps({
        "id": 1,
        "method": "Runtime.evaluate",
        "params": {
            "expression": expr,
            "returnByValue": True,
            "awaitPromise": await_promise,
        },
    }))
    msg = json.loads(_ws_recv(sock))
    return (msg.get("result") or {}).get("result", {}).get("value")


def _eval_value(sock: socket.socket, expr: str, await_promise: bool = True) -> object:
    """Runtime.evaluate 并返回真正的值；页面抛异常时上抛，避免"静默成功"。"""
    _ws_send(sock, json.dumps({
        "id": 1,
        "method": "Runtime.evaluate",
        "params": {
            "expression": expr,
            "returnByValue": True,
            "awaitPromise": await_promise,
            "userGesture": True,
        },
    }))
    msg = json.loads(_ws_recv(sock))
    if msg.get("error"):
        raise RuntimeError(f"CDP 错误：{msg['error'].get('message')}")
    result = msg.get("result") or {}
    if result.get("exceptionDetails"):
        d = result["exceptionDetails"]
        desc = (d.get("exception") or {}).get("description") or d.get("text") or "未知异常"
        raise RuntimeError(f"页面脚本异常：{str(desc).splitlines()[0]}")
    return (result.get("result") or {}).get("value")


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
    """重载 DeepSeek Harness 的渲染进程（**不足以刷新归档状态**）。

    端口来源优先用 DevToolsActivePort（快捷方式带 --remote-debugging-port=0 才会生成）；
    没有该文件说明 DSH 是用旧方式启动的。

    实测结论（0.2.2-rc.3）：取消归档后重载界面无效 —— 归档列表由 host 进程在内存里
    持有，渲染进程重载后向 host 请求到的仍是旧列表，只有重启应用才会重读
    storages/workspace.json。保留此函数是为了其它需要让界面重绘的场景。
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


# 在 DSH 自己的界面里点「取消归档」，让 host 进程自己改状态并广播给界面。
# 走 UI 是唯一能做到"立刻生效"的路径：直接改 workspace.json 时，host 内存里的
# 归档集合不会重读，界面也不监听文件，必须重启。
_DSH_UNARCHIVE_JS = """
(async () => {
  const SID = __SID__;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const all = (sel) => [...document.querySelectorAll(sel)];
  const btnByLabel = (l) => all('button').find((b) => b.getAttribute('aria-label') === l);
  const menuItem = (t) => all('[role=menuitem],[role=menuitemradio]').find((e) => e.innerText.trim() === t);
  const rowSel = '[data-row-key="session:session-' + SID + '"]';
  const row = () => document.querySelector(rowSel);
  const FILTERS = ['隐藏已归档', '全部对话（显示已归档）', '仅显示已归档'];

  if (!btnByLabel('视图选项')) return { ok: false, err: 'no-view-options' };

  // 1. 切到「仅显示已归档」，同时记下用户原来的过滤方式
  btnByLabel('视图选项').click();
  await sleep(250);
  const cur = all('[role=menuitem]').find(
    (e) => /selected/i.test(String(e.className)) && FILTERS.includes(e.innerText.trim()));
  const current = cur ? cur.innerText.trim() : null;
  menuItem('仅显示已归档')?.click();
  await sleep(350);

  const restore = async () => {
    const target = current && current !== '仅显示已归档' ? current : '隐藏已归档';
    btnByLabel('视图选项')?.click();
    await sleep(250);
    menuItem(target)?.click();
    await sleep(300);
  };

  // 2. 找到这一行（归档视图里才有）
  let tries = 0;
  while (!row() && tries++ < 12) await sleep(120);
  if (!row()) {
    await restore();
    return { ok: false, err: 'not-archived' };
  }

  // 3. 打开行操作菜单 → 取消归档
  const trigger = row().querySelector('.wnFoaW_rowActions button');
  if (!trigger) { await restore(); return { ok: false, err: 'no-row-actions' }; }
  trigger.click();
  let act = null;
  tries = 0;
  while (!act && tries++ < 10) { await sleep(120); act = menuItem('取消归档'); }
  if (!act) return { ok: false, err: 'no-unarchive-item' };
  act.click();

  tries = 0;
  while (row() && tries++ < 15) await sleep(120);
  const changed = !row();

  // 4. 还原用户原来的过滤方式
  await restore();
  return { ok: true, changed, filter: current, rowSel };
})()
"""


def unarchive_dsh_via_ui(
    sid: str,
    port: int | None = None,
    timeout: float = 8.0,
) -> dict:
    """在 DSH 界面里完成「取消归档」，状态立刻生效（无需重启应用）。

    返回 {"ok","changed","error","port"}：
    - ok/changed 都为真：确实执行了取消归档，DSH 界面已刷新；
    - ok 真、changed 假：host 里本来就没归档（幂等）；
    - ok 假：DSH 没开或没带调试端口，调用方应退化为直接改 workspace.json。
    """
    norm = (sid or "").strip()
    for prefix in ("session:", "session-"):
        if norm.startswith(prefix):
            norm = norm[len(prefix):]
    if not norm:
        return {"ok": False, "changed": False, "error": "缺少 DSH 会话 id"}

    if port is None:
        port = read_devtools_port()
        if port is None:
            return {
                "ok": False,
                "changed": False,
                "port": None,
                "error": "未发现 DSH 调试端口（DevToolsActivePort 不存在）",
            }

    try:
        pages = list_pages(port)
    except Exception as e:  # noqa: BLE001 - 端口未开按不可用处理
        return {"ok": False, "changed": False, "port": port,
                "error": f"DSH 调试端口 {port} 不可达：{e}"}

    target = next(
        (
            p for p in pages
            if p.get("type") == "page"
            and p.get("webSocketDebuggerUrl")
            and str(p.get("url", "")).startswith("dsh-app://")
        ),
        None,
    )
    if target is None:
        return {"ok": False, "changed": False, "port": port,
                "error": "没找到 DSH 主窗口"}

    sock = None
    try:
        sock = _ws_open(target["webSocketDebuggerUrl"], timeout=timeout)
        sock.settimeout(timeout)
        expr = _DSH_UNARCHIVE_JS.replace("__SID__", json.dumps(norm))
        value = _eval_value(sock, expr, await_promise=True)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "changed": False, "port": port,
                "error": f"操作 DSH 界面失败：{e}"}
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    if not isinstance(value, dict):
        return {"ok": False, "changed": False, "port": port,
                "error": "DSH 界面返回了异常结果"}

    err = value.get("err")
    if value.get("ok"):
        return {
            "ok": True,
            "changed": bool(value.get("changed")),
            "via": "ui",
            "focus_restored": value.get("filter"),
            "port": port,
        }
    if err == "not-archived":
        return {"ok": True, "changed": False, "via": "ui", "port": port,
                "note": "DSH 归档列表里没有该会话"}
    return {"ok": False, "changed": False, "port": port,
            "error": f"DSH 界面操作未完成（{err or 'unknown'}）"}
