"""Windows 系统托盘控制器：托管检索服务并提供快捷操作。

- 托盘是唯一入口，服务的 HTTP server 在后台线程 serve_forever。
- 托盘窗口与消息循环跑在独立线程（traywin），主线程做服务健康巡检与自愈。
- 启动时接管端口（若有旧实例则清理），保证托盘与真实状态一致。
- 图标颜色反映状态：蓝色=运行中，灰色=已停止。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from PIL import Image, ImageDraw

from . import app as appmod
from . import watcher
from .config import DATA_DIR, DEFAULT_HOST, DEFAULT_PORT, PROJECT_ROOT
from .traywin import TrayThread

URL = f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"
_NO_WINDOW = 0x08000000 if sys.platform.startswith("win") else 0
# 绕过系统代理，直连本机服务（环境里可能配了 HTTP_PROXY）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_ICON_RUNNING = (58, 110, 226, 255)
_ICON_STOPPED = (122, 130, 144, 255)
_WHITE = (255, 255, 255, 255)


def _make_icon(running: bool) -> Image.Image:
    """蓝色/灰色圆底 + 白色放大镜。"""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([2, 2, 62, 62], fill=_ICON_RUNNING if running else _ICON_STOPPED)
    d.ellipse([15, 12, 41, 38], outline=_WHITE, width=6)
    d.line([37, 34, 50, 47], fill=_WHITE, width=7)
    return img


def icon_path(running: bool = True) -> Path:
    return DATA_DIR / ("chatsearch.ico" if running else "chatsearch_off.ico")


def export_icons(force: bool = False) -> Path:
    """导出运行/停止两态 .ico，供托盘与快捷方式使用。"""
    for state in (True, False):
        path = icon_path(state)
        if path.exists() and not force:
            continue
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _make_icon(state).save(
                path, format="ICO",
                sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64)],
            )
        except Exception as e:
            print(f"[chatsearch] 导出图标失败：{e}")
    return icon_path(True)


# ---------------------------------------------------------------------------
# 开机自启：只操作「启动」目录里的一个 VBS（静默启动，不弹任何窗口）
# 不使用计划任务 / Powershell 脚本，避免定时拉起控制台造成闪窗
# ---------------------------------------------------------------------------
AUTOSTART_VBS_NAME = "ChatSearchTray.vbs"


def _launcher_exe() -> Path:
    """要启动的可执行文件：打包后就是 exe 自身，源码运行时用同目录 pythonw。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable)
    exe = Path(sys.executable)
    if exe.name.lower() != "pythonw.exe":
        cand = exe.with_name("pythonw.exe")
        if cand.exists():
            return cand
    return exe


def _launcher_args() -> list[str]:
    """启动参数：打包后入口已是托盘，源码运行才需要 -m chatsearch.tray。"""
    if getattr(sys, "frozen", False):
        return []
    return ["-m", "chatsearch.tray"]


def _startup_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def autostart_path() -> Path:
    return _startup_dir() / AUTOSTART_VBS_NAME


def autostart_enabled() -> bool:
    try:
        return autostart_path().is_file()
    except OSError:
        return False


def set_autostart(enable: bool) -> tuple[bool, str]:
    """写入/删除启动目录 VBS，返回 (是否成功, 提示文案)。"""
    path = autostart_path()
    try:
        if not enable:
            if path.exists():
                path.unlink()
            return True, "已关闭开机自启"
        exe = _launcher_exe()
        args = " ".join(_launcher_args() + ["--no-browser"])
        path.parent.mkdir(parents=True, exist_ok=True)
        # WSH 按 Unicode(UTF-16 LE + BOM) 读取，中文注释与含中文的路径都不会乱码
        # newline="\r\n" 显式指定，避免 text mode 把 \r\n 二次转义成 \r\r\n
        body = "\n".join((
            "' ChatSearch tray autostart - silent, no console window",
            "' Written by tray menu. Uncheck it there to remove.",
            'Set sh = CreateObject("WScript.Shell")',
            f'sh.CurrentDirectory = "{PROJECT_ROOT}"',
            f'sh.Run """{exe}"" {args}", 0, False',
            "",
        ))
        path.write_text(body, encoding="utf-16", newline="\r\n")
        return True, "已开启开机自启"
    except OSError as e:
        return False, f"设置失败：{e}"


def _port_in_use(host: str, port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex((host, port)) == 0


# ---------------------------------------------------------------------------
# 桌面快捷方式：直接调 IShellLink / IPersistFile COM，不依赖 pywin32
# ---------------------------------------------------------------------------
_CLSID_SHELLLINK = "{00021401-0000-0000-C000-000000000046}"
_IID_ISHELLLINKW = "{000214F9-0000-0000-C000-000000000046}"
_IID_IPERSISTFILE = "{0000010B-0000-0000-C000-000000000046}"
SHORTCUT_NAME = "聊天记录检索.lnk"


def _guid_struct(text: str):
    import uuid
    from ctypes import Structure, c_ubyte, c_ulong, c_ushort

    class GUID(Structure):
        _fields_ = [
            ("Data1", c_ulong),
            ("Data2", c_ushort),
            ("Data3", c_ushort),
            ("Data4", c_ubyte * 8),
        ]

    u = uuid.UUID(text)
    g = GUID()
    g.Data1, g.Data2, g.Data3 = u.time_low, u.time_mid, u.time_hi_version
    for i, b in enumerate(u.bytes[8:]):
        g.Data4[i] = b
    return g


def _com_method(ptr, index: int, restype, *argtypes):
    """从 COM 接口指针的 vtable 取第 index 个方法。"""
    import ctypes
    from ctypes import CFUNCTYPE, POINTER, c_void_p

    vtbl = ctypes.cast(ptr, POINTER(POINTER(c_void_p)))[0]
    return CFUNCTYPE(restype, c_void_p, *argtypes)(vtbl[index])


def _desktop_dir() -> Path:
    """用 SHGetFolderPathW 取真实桌面路径（兼容 OneDrive 重定向）。"""
    if sys.platform.startswith("win"):
        try:
            import ctypes

            buf = ctypes.create_unicode_buffer(260)
            # CSIDL_DESKTOPDIRECTORY = 0x0010
            if ctypes.windll.shell32.SHGetFolderPathW(None, 0x0010, None, 0, buf) == 0:
                if buf.value:
                    return Path(buf.value)
        except Exception:
            pass
    return Path.home() / "Desktop"


def _pythonw() -> Path:
    return _launcher_exe()


def create_desktop_shortcut() -> tuple[bool, str]:
    """在桌面创建/覆盖指向托盘的快捷方式，返回 (是否成功, 提示文案)。"""
    if not sys.platform.startswith("win"):
        return False, "仅支持 Windows"
    try:
        import ctypes
        from ctypes import byref, c_int, c_void_p, wintypes

        ole32 = ctypes.WinDLL("ole32")
        ole32.CoInitialize(None)
        export_icons()

        clsid = _guid_struct(_CLSID_SHELLLINK)
        iid_sl = _guid_struct(_IID_ISHELLLINKW)
        iid_pf = _guid_struct(_IID_IPERSISTFILE)

        p_sl = c_void_p()
        # CLSCTX_INPROC_SERVER = 1
        if ole32.CoCreateInstance(byref(clsid), None, 1, byref(iid_sl),
                                  byref(p_sl)) != 0:
            return False, "创建 IShellLink 失败"

        set_path = _com_method(p_sl, 20, c_int, wintypes.LPCWSTR)
        set_desc = _com_method(p_sl, 7, c_int, wintypes.LPCWSTR)
        set_wd = _com_method(p_sl, 9, c_int, wintypes.LPCWSTR)
        set_args = _com_method(p_sl, 11, c_int, wintypes.LPCWSTR)
        set_icon = _com_method(p_sl, 17, c_int, wintypes.LPCWSTR, c_int)
        release_sl = _com_method(p_sl, 2, ctypes.c_ulong)

        set_path(p_sl, str(_launcher_exe()))
        set_args(p_sl, " ".join(_launcher_args()))
        set_wd(p_sl, str(PROJECT_ROOT))
        set_icon(p_sl, str(icon_path(True)), 0)
        set_desc(p_sl, "本地聊天记录全文检索（WorkBuddy / DSH）")

        p_pf = c_void_p()
        query_interface = _com_method(
            p_sl, 0, ctypes.c_long, ctypes.POINTER(type(iid_pf)), ctypes.POINTER(c_void_p)
        )
        if query_interface(p_sl, byref(iid_pf), byref(p_pf)) != 0:
            release_sl(p_sl)
            return False, "获取 IPersistFile 失败"

        save = _com_method(p_pf, 6, ctypes.c_long, wintypes.LPCWSTR, wintypes.BOOL)
        target = _desktop_dir() / SHORTCUT_NAME
        target.parent.mkdir(parents=True, exist_ok=True)
        hr = save(p_pf, str(target), True)
        _com_method(p_pf, 2, ctypes.c_ulong)(p_pf)
        release_sl(p_sl)
        ole32.CoUninitialize()
        if hr != 0:
            return False, f"保存快捷方式失败 hr=0x{hr & 0xFFFFFFFF:08X}"
        return True, f"快捷方式已创建：{target.name}"
    except Exception as e:
        return False, f"创建失败：{type(e).__name__}: {e}"


def _pid_on_port(port: int) -> int | None:
    """用 GetExtendedTcpTable 直接查 IPv4 监听端口所属 PID（不启动任何子进程）。"""
    if not sys.platform.startswith("win"):
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class MIB_TCPROW_OWNER_PID(ctypes.Structure):
            _fields_ = [
                ("dwState", wintypes.DWORD),
                ("dwLocalAddr", wintypes.DWORD),
                ("dwLocalPort", wintypes.DWORD),
                ("dwRemoteAddr", wintypes.DWORD),
                ("dwRemotePort", wintypes.DWORD),
                ("dwOwningPid", wintypes.DWORD),
            ]

        iphlpapi = ctypes.WinDLL("iphlpapi")
        family, table_class = 2, 3  # AF_INET, TCP_TABLE_OWNER_PID_LISTENER
        size = wintypes.DWORD(0)
        iphlpapi.GetExtendedTcpTable(None, ctypes.byref(size), False,
                                     family, table_class, 0)
        if not size.value:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if iphlpapi.GetExtendedTcpTable(buf, ctypes.byref(size), False,
                                        family, table_class, 0) != 0:
            return None
        count = wintypes.DWORD.from_buffer(buf).value
        offset = ctypes.sizeof(wintypes.DWORD)
        stride = ctypes.sizeof(MIB_TCPROW_OWNER_PID)
        for i in range(count):
            row = MIB_TCPROW_OWNER_PID.from_buffer(buf, offset + i * stride)
            # dwLocalPort 为网络字节序，取低 16 位后换回主机序
            local = ((row.dwLocalPort & 0xFF) << 8) | ((row.dwLocalPort >> 8) & 0xFF)
            if local == port:
                return int(row.dwOwningPid)
    except Exception:
        return None
    return None


def _kill_port_owners(port: int) -> None:
    """结束占用指定端口的进程（仅用于接管旧实例）。"""
    pid = _pid_on_port(port)
    if not pid or pid == os.getpid():
        return
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                       capture_output=True, timeout=15, creationflags=_NO_WINDOW)
    except Exception:
        pass


def _redirect_output() -> None:
    """pythonw 无控制台时 sys.stdout 为 None，print 会崩；重定向到日志文件。"""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        stream = open(DATA_DIR / "tray.log", "a", encoding="utf-8", buffering=1)
    except Exception:
        import io
        stream = io.StringIO()
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


class TrayController:
    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 open_on_start: bool = True) -> None:
        self.host = host
        self.port = port
        self.open_on_start = open_on_start
        self._server = None
        self._thread: threading.Thread | None = None
        self._tray: TrayThread | None = None
        self._running = False
        self._stopping = False
        self._busy = ""

    # ---------------- 基础 ----------------
    def _api(self, path: str, timeout: float = 6.0) -> dict | None:
        try:
            with _OPENER.open(f"{URL}{path}", timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None

    def status_text(self) -> str:
        if self._busy:
            return self._busy
        return f"运行中 · {URL}" if self._running else "服务未运行"

    def _menu_spec(self) -> list:
        return [
            ("item", "打开检索页面", self.open_page, True),
            ("status", self.status_text()),
            ("sep",),
            ("item", "更新索引（增量）", self.sync_index, self._running),
            ("item", "重建索引（全量）", self.rebuild_index, self._running),
            ("sep",),
            ("item", "重启服务", self.restart, True),
            ("item", "打开数据目录", self.open_data_dir, True),
            ("item", "发送到桌面快捷方式", self.make_shortcut, True),
            ("sep",),
            ("check", "开机自动启动", autostart_enabled(), self.toggle_autostart, True),
            ("sep",),
            ("item", "退出（停止服务）", self.quit, True),
        ]

    def _apply_state(self) -> None:
        """把当前状态同步到托盘：图标颜色、tooltip、菜单可用项。"""
        if self._tray is None:
            return
        tray = self._tray.tray
        tray.set_icon(icon_path(self._running))
        tray.set_tooltip(f"聊天记录检索 · {self.status_text()}")
        tray.set_menu(self._menu_spec())

    def _notify(self, message: str, title: str = "聊天记录检索") -> None:
        if self._tray is not None:
            self._tray.tray.notify(title, message)

    # ---------------- 服务生命周期 ----------------
    def start_service(self) -> tuple[bool, str]:
        if self._server is not None:
            return True, ""
        # 接管：清掉先前由 bat / 计划任务拉起的旧实例，避免托盘与真实状态不一致
        if _port_in_use(self.host, self.port):
            _kill_port_owners(self.port)
            time.sleep(1.2)
        server, err = appmod.create_server(self.host, self.port)
        if server is None:
            return False, err
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever, name="chatsearch-serve", daemon=True
        )
        self._thread.start()
        return True, ""

    def stop_service(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except Exception:
                pass
            if self._thread is not None:
                self._thread.join(timeout=8)
                self._thread = None
        # 必须停监听线程，否则解释器被挂住、进程残留、互斥锁不释放
        watcher.stop_watcher()

    def _restart_service(self) -> tuple[bool, str]:
        self.stop_service()
        time.sleep(0.5)
        return self.start_service()

    # ---------------- 菜单动作 ----------------
    def open_page(self) -> None:
        if not self._running:
            ok, err = self.start_service()
            self._running = ok
            self._apply_state()
            if not ok:
                self._notify(f"启动失败：{err}")
                return
        webbrowser.open(URL)

    def sync_index(self) -> None:
        def work() -> None:
            result = self._api("/api/rebuild", timeout=180)
            if result is None:
                self._notify("更新失败：服务不可达")
                return
            if "error" in result:
                self._notify(f"更新失败：{result['error']}")
                return
            self._notify(f"已更新：{result.get('sessions')} 会话 / "
                         f"{result.get('messages')} 消息")
        threading.Thread(target=work, name="chatsearch-sync", daemon=True).start()

    def rebuild_index(self) -> None:
        self._busy = "重建索引中…"
        self._apply_state()

        def work() -> None:
            result = self._api("/api/rebuild?full=1", timeout=900)
            self._busy = ""
            self._apply_state()
            if result is None:
                self._notify("重建失败：服务不可达")
                return
            if "error" in result:
                self._notify(f"重建失败：{result['error']}")
                return
            self._notify(f"重建完成：{result.get('sessions')} 会话 / "
                         f"{result.get('messages')} 消息")
        threading.Thread(target=work, name="chatsearch-rebuild", daemon=True).start()

    def restart(self) -> None:
        self._busy = "重启中…"
        self._apply_state()

        def work() -> None:
            ok, err = self._restart_service()
            self._busy = ""
            self._running = ok
            self._apply_state()
            self._notify("服务已重启" if ok else f"重启失败：{err}")
        threading.Thread(target=work, name="chatsearch-restart", daemon=True).start()

    def toggle_autostart(self) -> None:
        target = not autostart_enabled()
        ok, msg = set_autostart(target)
        print(f"[chatsearch] 开机自启 -> {target} : {msg}")
        self._notify(msg)
        self._apply_state()

    def make_shortcut(self) -> None:
        ok, msg = create_desktop_shortcut()
        print(f"[chatsearch] 桌面快捷方式：{msg}")
        self._notify(msg)

    def open_data_dir(self) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            if sys.platform.startswith("win"):
                subprocess.Popen(["explorer", str(DATA_DIR)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(DATA_DIR)])
            else:
                subprocess.Popen(["xdg-open", str(DATA_DIR)])
        except Exception:
            pass

    def quit(self) -> None:
        self._stopping = True
        self.stop_service()

    # ---------------- 运行 ----------------
    def _monitor(self) -> None:
        """主线程巡检：图标随状态变色；服务异常时自愈（托盘兼作看门狗）。"""
        fails = 0
        while not self._stopping:
            time.sleep(4)
            if self._stopping:
                return
            ok = self._server is not None and self._api("/api/status", timeout=2.5) is not None
            if ok:
                fails = 0
                if not self._running:
                    self._running = True
                    self._apply_state()
                continue
            fails += 1
            if self._running:
                self._running = False
                self._apply_state()
            if fails >= 2:  # 连续两次探测失败：尝试自愈
                fails = 0
                if self._server is None:
                    started, _err = self.start_service()
                    self._running = started
                    self._apply_state()

    def run(self) -> None:
        export_icons()
        self._tray = TrayThread("聊天记录检索")
        self._tray.tray.set_left_click(self.open_page)
        self._apply_state()
        if not self._tray.start():
            print("[chatsearch] 托盘窗口创建失败，退出")
            return
        print("[chatsearch] 托盘已就绪")

        ok, err = self.start_service()
        self._running = ok
        self._apply_state()
        if ok:
            # 开机自启（--no-browser）只挂图标，不抢用户浏览器
            if self.open_on_start:
                self.open_page()
        else:
            print(f"[chatsearch] 服务启动失败：{err}")
            self._notify(f"启动失败：{err}")

        self._monitor()          # 直到 quit() 把 _stopping 置位
        self._tray.stop()
        print("[chatsearch] 托盘关闭")


# ---------------------------------------------------------------------------
# 托盘单实例：避免用户重复双击产生多个图标
# ---------------------------------------------------------------------------
_TRAY_MUTEX = None


def _hold_tray_lock() -> bool:
    global _TRAY_MUTEX
    if not sys.platform.startswith("win"):
        _TRAY_MUTEX = _TRAY_MUTEX or object()
        return True
    if _TRAY_MUTEX is not None:
        return True
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [wintypes.LPCWSTR, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        mutex = kernel32.CreateMutexW(None, True, "Local\\ChatSearchTray")
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            return False
        _TRAY_MUTEX = mutex
        return True
    except Exception:
        _TRAY_MUTEX = object()
        return True


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="聊天记录检索托盘控制器")
    parser.add_argument("--export-icon", action="store_true", help="导出图标文件后退出")
    parser.add_argument("--no-browser", action="store_true",
                        help="启动后不自动打开浏览器（开机自启使用）")
    args = parser.parse_args()

    _redirect_output()
    export_icons()
    if args.export_icon:
        print(f"[chatsearch] 图标已导出：{icon_path(True)} / {icon_path(False)}")
        return

    if not _hold_tray_lock():
        # 托盘已在运行：静默退出；只有用户主动启动时才顺手开页面
        if args.no_browser:
            print("[chatsearch] 托盘已在运行，静默退出")
        else:
            print("[chatsearch] 托盘已在运行，直接打开页面")
            webbrowser.open(URL)
        return
    print(f"[chatsearch] 托盘启动 {time.strftime('%Y-%m-%d %H:%M:%S')}")
    TrayController(open_on_start=not args.no_browser).run()
    print("[chatsearch] 托盘已退出")


if __name__ == "__main__":
    main()
