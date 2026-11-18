"""会话是不是真的在跑：WorkBuddy 的 agent 心跳文件 + 进程身份校验。

WorkBuddy 每起一轮对话就 spawn 一个 host 进程，并在
`~/.workbuddy/sessions/<pid>.json` 里每 10~15 秒刷一次 lastHeartbeat：

    {"pid": 4956, "lastHeartbeat": 1795002748425,
     "sessionId": "f16c5956-ca25-43a1-8be4-3fcae94c40e1",
     "kind": "interactive", "startedAt": 1795002628188,
     "processIdentity": {"procStart": "2026-11-18T11:50:25.519Z",
                         "procStartFt": "134394762255190000"},
     "endpoint": "http://127.0.0.1:63138", "version": "2.176.0"}

这是唯一能直接回答「App 此刻正在为哪个会话干活」的信号，比看会话文件 mtime
准得多：进程退出后文件会残留（实测遗留 1 分钟以上才被下一轮的同名文件覆盖），
所以必须同时满足三条才算活——

1. lastHeartbeat 够新（HEARTBEAT_TTL_MS 内）；
2. pid 对应的进程真的存在；
3. 进程创建时间与 procStartFt 一致（排除 Windows 的 PID 复用）。

任何一条不成立都不算。`kind`/`sessionId` 不是会话 id 的（如 interactive-6520，
是常驻宿主进程）只用来判断「App 还活着」，不参与会话匹配。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from .config import HOME

AGENT_STATE_DIR = HOME / ".workbuddy" / "sessions"

# 心跳多久不刷新就当作进程已死。实测刷新间隔在 1~28 秒之间抖动，留三次余量。
HEARTBEAT_TTL_MS = 45_000

# 进程创建时间比对容差（100ns 计数，1 秒）。文件里存的是截断到毫秒的值，
# 与内核值实测差 10~20ms，取 1 秒足够宽松又足以区分 PID 复用。
_FT_TOLERANCE = 10_000_000

# 一次搜索会连着调好几次判定，缓存两秒，避免反复开进程句柄。
_CACHE_TTL_S = 2.0
_cache: tuple[float, frozenset[str], bool] | None = None


def _creation_filetime(pid: int) -> int | None:
    """返回进程创建时间的 FILETIME 计数（100ns 自 1601）；拿不到返回 None。"""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes as wt

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wt.HANDLE
        k32.OpenProcess.argtypes = (wt.DWORD, wt.BOOL, wt.DWORD)
        k32.GetProcessTimes.argtypes = (
            wt.HANDLE, ctypes.POINTER(wt.FILETIME), ctypes.POINTER(wt.FILETIME),
            ctypes.POINTER(wt.FILETIME), ctypes.POINTER(wt.FILETIME),
        )
        k32.CloseHandle.argtypes = (wt.HANDLE,)
        # PROCESS_QUERY_LIMITED_INFORMATION：权限最小，普通用户也能开自己的进程
        h = k32.OpenProcess(0x1000, False, int(pid))
        if not h:
            return None  # 进程不存在（或无权打开，视为不在）
        try:
            c, e, k, u = wt.FILETIME(), wt.FILETIME(), wt.FILETIME(), wt.FILETIME()
            if not k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e),
                                       ctypes.byref(k), ctypes.byref(u)):
                return None
            return (c.dwHighDateTime << 32) | c.dwLowDateTime
        finally:
            k32.CloseHandle(h)
    except Exception:
        return None


def _is_session_id(value: object) -> bool:
    """形如 uuid 的会话 id；'interactive-6520' 这类宿主占位 id 不算。"""
    s = str(value or "")
    return len(s) >= 32 and "-" in s


def _scan(now_ms: int) -> tuple[set[str], bool]:
    """扫一遍心跳目录，返回（正在跑的会话 id 集合，App 是否活着）。"""
    live: set[str] = set()
    app_up = False
    try:
        files = list(AGENT_STATE_DIR.glob("*.json"))
    except OSError:
        return live, app_up
    for f in files:
        try:
            info = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # 正在被 App 重写，下一轮再看
        if not isinstance(info, dict):
            continue
        try:
            hb = int(info.get("lastHeartbeat") or 0)
            pid = int(info.get("pid") or 0)
        except (TypeError, ValueError):
            continue
        if not hb or now_ms - hb > HEARTBEAT_TTL_MS:
            continue  # 心跳停 = 这轮已经结束（或进程被杀），文件是残留
        want_ft = int((info.get("processIdentity") or {}).get("procStartFt") or 0)
        if want_ft:
            got_ft = _creation_filetime(pid)
            if got_ft is None or abs(got_ft - want_ft) > _FT_TOLERANCE:
                continue  # 进程没了，或这个 pid 已经被别的进程复用
        app_up = True
        sid = info.get("sessionId")
        if _is_session_id(sid):
            live.add(str(sid))
    return live, app_up


def snapshot(now_ms: int | None = None) -> tuple[frozenset[str], bool]:
    """返回（正在跑的会话 id，App 是否活着），带 2 秒缓存。"""
    global _cache
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    if _cache is not None and abs(now - _cache[0]) < _CACHE_TTL_S * 1000:
        return _cache[1], _cache[2]
    live, app_up = _scan(now)
    _cache = (now, frozenset(live), app_up)
    return frozenset(live), app_up


def live_sessions(now_ms: int | None = None) -> frozenset[str]:
    """此刻有活着的 agent 进程在跑的会话 id 集合。"""
    return snapshot(now_ms)[0]


def app_alive(now_ms: int | None = None) -> bool:
    """WorkBuddy 是否还在运行（只要有一个心跳新鲜就算）。"""
    return snapshot(now_ms)[1]


def reset_cache() -> None:
    """测试用：丢掉缓存。"""
    global _cache
    _cache = None
