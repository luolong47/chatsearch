"""纯 Win32 系统托盘实现（不依赖第三方库）。

为什么不用 pystray：在其实现里 `Shell_NotifyIcon` 的返回值完全不作检查，
一旦失败就是静默的（图标消失且无任何报错）；且 64 位下 `hID=id(obj)` 超出
UINT 范围、缺少 NIM_SETVERSION/NIF_SHOWTIP。本模块直接调 Win32 API，
每一步都有返回值可验证。

用法：
    tray = Win32Tray()
    tray.set_icon("data/chatsearch.ico")
    tray.set_tooltip("聊天记录检索")
    tray.set_menu([("item", "打开页面", on_open, True), ("sep",), ...])
    tray.run()          # 阻塞，直到 stop()
"""
from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Callable

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# ---------------------------------------------------------------- 常量
WM_DESTROY = 0x0002
WM_NULL = 0x0000
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_USER = 0x0400
WM_TRAY = WM_USER + 102
WM_STOP = WM_USER + 103
WM_REPROMOTE = WM_USER + 104

NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x1, 0x2, 0x4
NIF_STATE, NIF_INFO = 0x8, 0x10
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0x0, 0x1, 0x2
NIS_HIDDEN = 0x1

MF_STRING, MF_GRAYED, MF_SEPARATOR, MF_DEFAULT = 0x0, 0x1, 0x800, 0x1000
MF_CHECKED = 0x8
TPM_RIGHTBUTTON, TPM_NONOTIFY, TPM_RETURNCMD = 0x2, 0x80, 0x100

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x10
LR_DEFAULTSIZE = 0x40

ERROR_CLASS_ALREADY_EXISTS = 1410

# ---------------------------------------------------------------- 结构体
WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", wintypes.HICON),
    ]


class NOTIFYICONIDENTIFIER(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("guidItem", ctypes.c_byte * 16),
    ]


# ------------------------------------------------ 函数签名（缺一个就会 64 位溢出）
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wintypes.ATOM
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                              ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.LoadImageW.restype = wintypes.HANDLE
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                               wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = wintypes.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = ctypes.c_ssize_t
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.CreatePopupMenu.argtypes = []
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, wintypes.WPARAM, wintypes.LPCWSTR]
user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, wintypes.HWND, ctypes.c_void_p]
user32.TrackPopupMenu.restype = wintypes.BOOL
user32.DestroyMenu.argtypes = [wintypes.HMENU]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL
shell32.Shell_NotifyIconGetRect.argtypes = [ctypes.POINTER(NOTIFYICONIDENTIFIER),
                                            ctypes.POINTER(wintypes.RECT)]
shell32.Shell_NotifyIconGetRect.restype = ctypes.HRESULT


def promote_in_registry(tooltip_prefix: str) -> bool:
    """把托盘图标在 Win11 任务栏里设为「始终显示」。

    Win11 会把新注册的托盘图标默认收进隐藏区，且**没有任何 API**可以直接提升；
    系统把用户对每个图标的选择存在
        HKCU\\Control Panel\\NotifyIconSettings\\<explorer 生成的哈希>
    键名无法预知，只能按 InitialTooltip 前缀匹配。改完 IsPromoted=1 后需要
    重新注册图标（NIM_DELETE + NIM_ADD），explorer 才会读取新设置。

    返回是否发生了修改。
    """
    import winreg

    changed = False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Control Panel\NotifyIconSettings",
                            0, winreg.KEY_READ | winreg.KEY_WRITE) as root:
            idx = 0
            while True:
                try:
                    sub = winreg.EnumKey(root, idx)
                except OSError:
                    break
                idx += 1
                try:
                    with winreg.OpenKey(root, sub, 0,
                                        winreg.KEY_READ | winreg.KEY_WRITE) as sk:
                        tip = winreg.QueryValueEx(sk, "InitialTooltip")[0]
                        if not isinstance(tip, str) or not tip.startswith(tooltip_prefix):
                            continue
                        try:
                            cur = winreg.QueryValueEx(sk, "IsPromoted")[0]
                        except FileNotFoundError:
                            cur = 0
                        if cur != 1:
                            winreg.SetValueEx(sk, "IsPromoted", 0, winreg.REG_DWORD, 1)
                            changed = True
                except OSError:
                    continue
    except OSError as e:
        print(f"[tray] 读取托盘设置失败：{e}")
    return changed


class Win32Tray:
    """一个托盘图标 + 右键菜单，运行在调用 run() 的线程（必须有消息循环）。"""

    CLASS_NAME = "ChatSearchTrayWnd"
    ICON_ID = 1

    def __init__(self, promote_prefix: str = "") -> None:
        self._hwnd: int | None = None
        self._hicon: int | None = None
        self._nid = NOTIFYICONDATAW()
        self._proc = None          # 保持 WNDPROC 引用，否则被 GC 会崩
        self._menu_spec: list = []
        self._left_click: Callable[[], None] | None = None
        self._tooltip = "聊天记录检索"
        # 图标注册完成（或失败）时回调，参数为是否成功
        self.on_ready: Callable[[bool], None] | None = None
        # 用于在 Win11 里把图标从"隐藏区"提升到常显区（按 tooltip 前缀匹配注册表）
        self._promote_prefix = promote_prefix

    # ---------------- 配置 ----------------
    def set_icon(self, ico_path: str | Path | None) -> bool:
        """加载 .ico 并（在图标已注册时）立即刷新。"""
        if not ico_path:
            return False
        h = user32.LoadImageW(None, str(ico_path), IMAGE_ICON, 0, 0,
                              LR_LOADFROMFILE | LR_DEFAULTSIZE)
        if not h:
            print(f"[tray] 加载图标失败：{ico_path} err={ctypes.get_last_error()}")
            return False
        self._hicon = h
        if self._hwnd:
            self._nid.uFlags = NIF_ICON
            self._nid.hIcon = h
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._nid))
        return True

    def set_tooltip(self, text: str) -> None:
        self._tooltip = text
        if self._hwnd:
            self._nid.uFlags = NIF_TIP
            self._nid.szTip = text[:127]
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._nid))

    def notify(self, title: str, message: str) -> bool:
        """气泡通知（Windows 10/11 会转成系统通知横幅）。"""
        if not self._hwnd:
            return False
        nid = self._nid
        nid.uFlags = NIF_INFO
        nid.szInfoTitle = title[:63]
        nid.szInfo = message[:255]
        nid.dwInfoFlags = 0x1  # NIIF_INFO
        return bool(shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid)))

    def set_menu(self, spec: list) -> None:
        """菜单规格：
        ("item", 文本, 回调, 是否可用)
        ("check", 文本, 是否勾选, 回调, 是否可用)
        ("status", 文本) / ("sep",)
        """
        self._menu_spec = spec

    def set_left_click(self, callback: Callable[[], None]) -> None:
        self._left_click = callback

    # ---------------- 菜单 ----------------
    def _popup_menu(self) -> None:
        hmenu = user32.CreatePopupMenu()
        if not hmenu:
            return
        actions: dict[int, Callable] = {}
        cmd = 1
        for entry in list(self._menu_spec):
            kind = entry[0]
            if kind == "sep":
                user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
            elif kind == "status":
                user32.AppendMenuW(hmenu, MF_STRING | MF_GRAYED, 0, entry[1])
            elif kind == "item":
                _, text, cb, enabled = entry
                flags = MF_STRING
                if not enabled:
                    flags |= MF_GRAYED
                if cmd == 1:
                    flags |= MF_DEFAULT
                user32.AppendMenuW(hmenu, flags, cmd, text)
                if enabled:
                    actions[cmd] = cb
                cmd += 1
            elif kind == "check":
                _, text, checked, cb, enabled = entry
                flags = MF_STRING
                if checked:
                    flags |= MF_CHECKED
                if not enabled:
                    flags |= MF_GRAYED
                user32.AppendMenuW(hmenu, flags, cmd, text)
                if enabled:
                    actions[cmd] = cb
                cmd += 1

        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        # 经典要求：先抢前台，否则菜单不消失
        user32.SetForegroundWindow(self._hwnd)
        chosen = user32.TrackPopupMenu(
            hmenu, TPM_RETURNCMD | TPM_RIGHTBUTTON | TPM_NONOTIFY,
            pt.x, pt.y, 0, self._hwnd, None,
        )
        user32.PostMessageW(self._hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(hmenu)
        print(f"[tray] 菜单关闭，选中 id={int(chosen)}")

        action = actions.get(int(chosen))
        if action is not None:
            try:
                action()
            except Exception as e:  # 回调异常不能穿透 ctypes 边界
                print(f"[tray] 菜单动作异常：{type(e).__name__}: {e}")

    # ---------------- 窗口过程 ----------------
    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAY:
                event = int(lparam) & 0xFFFF
                if event in (WM_RBUTTONUP, WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    print(f"[tray] 图标事件 event=0x{event:04X}")
                if event in (WM_RBUTTONUP,):
                    self._popup_menu()
                elif event in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    if self._left_click:
                        self._left_click()
                return 0
            if msg == WM_REPROMOTE:
                # 重新注册一次，让 explorer 读取更新后的 IsPromoted
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
                self._add_icon()
                return 0
            if msg == WM_STOP:
                self._remove_icon()
                user32.DestroyWindow(hwnd)
                user32.PostQuitMessage(0)
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception as e:  # 绝不让异常穿过 ctypes 回调
            print(f"[tray] wndproc 异常：{type(e).__name__}: {e}")
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # ---------------- 生命周期 ----------------
    def _add_icon(self) -> bool:
        nid = self._nid
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = self.ICON_ID
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self._hicon
        nid.szTip = self._tooltip[:127]
        ok = shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
        if not ok:
            print(f"[tray] NIM_ADD 失败 err={ctypes.get_last_error()}")
            return False
        # 明确清除 NIS_HIDDEN：部分 Win11 版本会因此把图标放进常显区
        nid.uFlags = NIF_STATE
        nid.dwState = 0
        nid.dwStateMask = NIS_HIDDEN
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        return True

    def icon_rect(self) -> tuple[int, int, int, int] | None:
        """查询图标在屏幕上的矩形（供调试/定位用）。"""
        if not self._hwnd:
            return None
        nii = NOTIFYICONIDENTIFIER()
        nii.cbSize = ctypes.sizeof(NOTIFYICONIDENTIFIER)
        nii.hWnd = self._hwnd
        nii.uID = self.ICON_ID
        rc = wintypes.RECT()
        try:
            shell32.Shell_NotifyIconGetRect(ctypes.byref(nii), ctypes.byref(rc))
        except OSError:
            return None
        return (rc.left, rc.top, rc.right, rc.bottom)

    def _remove_icon(self) -> None:
        if self._hwnd:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))

    def _create_window(self) -> bool:
        hinst = kernel32.GetModuleHandleW(None)
        self._proc = WNDPROC(self._wndproc)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.lpszClassName = self.CLASS_NAME
        atom = user32.RegisterClassW(ctypes.byref(wc))
        if not atom:
            err = ctypes.get_last_error()
            if err != ERROR_CLASS_ALREADY_EXISTS:
                print(f"[tray] RegisterClass 失败 err={err}")
                return False
        self._hwnd = user32.CreateWindowExW(
            0, self.CLASS_NAME, "ChatSearchTray", 0, 0, 0, 0, 0,
            None, None, hinst, None,
        )
        if not self._hwnd:
            print(f"[tray] CreateWindowEx 失败 err={ctypes.get_last_error()}")
            return False
        return True

    def _promote_loop(self) -> None:
        """Win11 会把新图标丢进隐藏区：轮询注册表，等 explorer 建立条目后提升它。"""
        for attempt in range(4):
            time.sleep(2.5)
            try:
                if promote_in_registry(self._promote_prefix):
                    print(f"[tray] 已设为始终显示（第 {attempt + 1} 次尝试），重新注册生效")
                    if self._hwnd:
                        user32.PostMessageW(self._hwnd, WM_REPROMOTE, 0, 0)
                    return
            except Exception as e:
                print(f"[tray] 提升图标失败：{type(e).__name__}: {e}")

    def run(self) -> None:
        """创建窗口 + 注册图标 + 跑消息循环（阻塞）。"""
        ok = self._create_window() and self._add_icon()
        if ok:
            rect = self.icon_rect()
            print(f"[tray] 图标已注册 rect={rect}")
        if self.on_ready is not None:
            try:
                self.on_ready(ok)
            except Exception:
                pass
        if not ok:
            return
        if self._promote_prefix:
            threading.Thread(target=self._promote_loop, name="chatsearch-promote",
                             daemon=True).start()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        print("[tray] 消息循环结束")

    def stop(self, hwnd: int | None = None) -> None:
        """从任意线程请求退出。"""
        target = hwnd or self._hwnd
        if target:
            user32.PostMessageW(target, WM_STOP, 0, 0)


class TrayThread:
    """把 Win32Tray 放到独立线程跑消息循环，方便主线程做别的事。"""

    def __init__(self, promote_prefix: str = "") -> None:
        self.tray = Win32Tray(promote_prefix)
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._ok = False

    def _on_ready(self, ok: bool) -> None:
        self._ok = ok
        self._ready.set()

    def start(self, timeout: float = 6.0) -> bool:
        self.tray.on_ready = self._on_ready
        self._thread = threading.Thread(target=self.tray.run, name="chatsearch-tray", daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        return self._ok

    def stop(self) -> None:
        self.tray.stop()
        if self._thread is not None:
            self._thread.join(timeout=5)
