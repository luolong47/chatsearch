"""后端：标准库 http.server 提供检索 API 与静态页面。"""
from __future__ import annotations

import json
import mimetypes
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import indexer, watcher
from .config import DEFAULT_HOST, DEFAULT_PORT

def _resolve_static_dir() -> Path:
    """静态资源目录：源码运行取包内 static；PyInstaller 单文件取解包目录。"""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return base / "chatsearch" / "static"
    return Path(__file__).resolve().parent / "static"


STATIC_DIR = _resolve_static_dir()


def _parse_archived(qs: dict) -> bool | None:
    """解析归档筛选参数：all/空 -> None；active -> False；archived -> True。"""
    raw = (qs.get("archived", ["all"])[0] or "all").lower()
    if raw in ("archived", "1", "true", "yes"):
        return True
    if raw in ("active", "unarchived", "0", "false", "no"):
        return False
    return None


def _parse_cwd(qs: dict) -> str | None:
    return qs.get("cwd", [""])[0] or None


class APIHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        # 精简日志：只打印非静态请求
        if "/api/" in (self.path or ""):
            print(f"[chatsearch] {self.command} {self.path}")

    def _send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/api/status":
            data = indexer.index_status()
            data["watcher"] = watcher.get_status()
            return self._send_json(data)
        if path == "/api/rebuild":
            full = qs.get("full", ["0"])[0] in ("1", "true", "yes")
            try:
                if full:
                    result = indexer.rebuild_index(force=True)
                else:
                    result = indexer.sync_index()
                watcher.note_synced()
                return self._send_json(result)
            except Exception as e:
                return self._send_json({"error": str(e)}, status=500)
        if path == "/api/search":
            # 搜索前兜底：距上次同步过久才检查签名并增量更新（防事件丢失）
            watcher.maybe_sync_on_search()
            q = qs.get("q", [""])[0]
            source = qs.get("source", [""])[0] or None
            if source not in ("workbuddy", "dsh"):
                source = None
            try:
                return self._send_json(indexer.search(
                    q, source=source, cwd=_parse_cwd(qs), archived=_parse_archived(qs)
                ))
            except Exception as e:
                return self._send_json({"error": str(e)}, status=500)
        if path == "/api/session":
            key = qs.get("key", [""])[0]
            data = indexer.get_session(key)
            if data is None:
                return self._send_json({"error": "session not found"}, status=404)
            return self._send_json(data)
        if path == "/api/sessions":
            source = qs.get("source", [""])[0] or None
            if source not in ("workbuddy", "dsh"):
                source = None
            return self._send_json(indexer.list_sessions(
                source=source, cwd=_parse_cwd(qs), archived=_parse_archived(qs)
            ))
        if path == "/api/filters":
            # 筛选选项：工作目录列表（含计数）+ 归档统计；随来源/归档联动
            source = qs.get("source", [""])[0] or None
            if source not in ("workbuddy", "dsh"):
                source = None
            return self._send_json(indexer.filter_options(
                source=source, archived=_parse_archived(qs)
            ))
        if path == "/api/cwd":
            # 会话的真实工作目录（供复制 / 判断是否可打开）
            key = qs.get("key", [""])[0]
            data = indexer.resolve_cwd(key)
            if data is None:
                return self._send_json({"error": "cwd not found"}, status=404)
            return self._send_json(data)
        if path == "/api/open-cwd":
            # 在系统文件管理器中打开会话工作目录
            key = qs.get("key", [""])[0]
            data = indexer.resolve_cwd(key)
            if data is None:
                return self._send_json({"error": "cwd not found"}, status=404)
            if not data["exists"]:
                return self._send_json({"error": "目录不存在", "cwd": data["cwd"]}, status=404)
            ok, msg = _open_in_explorer(data["cwd"])
            return self._send_json({"ok": ok, "cwd": data["cwd"], "error": None if ok else msg})

        # 静态文件
        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/unarchive":
            return self.send_error(404)
        qs = parse_qs(parsed.query)
        key = qs.get("key", [""])[0]
        if not key:
            return self._send_json({"ok": False, "error": "缺少 key"}, status=400)
        try:
            result = indexer.unarchive_session(key)
        except Exception as e:
            return self._send_json({"ok": False, "error": str(e)}, status=500)
        if not result.get("ok"):
            status = 404 if "不存在" in (result.get("error") or "") else 400
            return self._send_json(result, status=status)
        watcher.note_synced()
        return self._send_json(result)


# 持有 Mutex 句柄的模块级引用，防止被 GC 释放；托盘重启服务时复用
_MUTEX_HANDLE = None


def _hold_single_instance() -> bool:
    """确保本进程持有单实例 Mutex。

    返回 True 表示本进程持有（新获取或之前已持有）；False 表示被其它进程占用。
    进程退出时系统自动释放，无残留锁文件。
    """
    global _MUTEX_HANDLE
    if not sys.platform.startswith("win"):
        _MUTEX_HANDLE = _MUTEX_HANDLE or object()
        return True
    if _MUTEX_HANDLE is not None:
        return True  # 本进程已持有（托盘内重启服务会走到这里）
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [wintypes.LPCWSTR, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        mutex = kernel32.CreateMutexW(None, True, "Local\\ChatSearchSingleInstance")
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            return False
        _MUTEX_HANDLE = mutex
        return True
    except Exception:
        _MUTEX_HANDLE = object()  # 无法加锁时降级为不保护
        return True


class _SinglePortServer(ThreadingHTTPServer):
    """禁止端口复用：Windows 下 SO_REUSEADDR 会让多个进程绑定同一端口，
    禁用后由内核保证只有一个实例能监听，配合 Mutex 双保险。"""
    allow_reuse_address = False


def prepare_index(verbose: bool = True) -> dict:
    """增量同步索引，返回同步结果。"""
    result = indexer.sync_index()
    if verbose:
        if result.get("updated", 0):
            print(f"[chatsearch] 增量更新 {result['updated']} 个文件 "
                  f"(+{result.get('added', 0)} / ~{result.get('changed', 0)} / -{result.get('removed', 0)}) "
                  f"耗时 {result.get('elapsed_ms', 0)}ms")
        else:
            print("[chatsearch] 索引已是最新（签名命中，无需解析文件）")
        print(f"[chatsearch] 索引就绪：{result['sessions']} 会话 / {result['messages']} 消息")
    return result


def create_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT
                  ) -> tuple[ThreadingHTTPServer | None, str]:
    """准备索引并创建 HTTP server（不阻塞）。返回 (server, "") 或 (None, 失败原因)。

    供托盘等外部控制器托管服务生命周期；CLI 的 serve() 也复用它。
    """
    if not _hold_single_instance():
        return None, "已有另一个实例在运行（单实例保护）"

    prepare_index()

    try:
        server = _SinglePortServer((host, port), APIHandler)
    except OSError as e:
        return None, f"端口 {port} 绑定失败（可能已有实例）：{e}"

    # 启动索引自动更新监听（watchdog 事件驱动，失败自动降级轮询）
    watcher.start_watcher()
    return server, ""


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, *, open_browser: bool = True) -> None:
    """启动服务并阻塞运行；首次自动构建索引。"""
    server, err = create_server(host, port)
    if server is None:
        print(f"[chatsearch] 启动失败：{err}")
        return
    url = f"http://{host}:{port}"
    print(f"[chatsearch] 服务已启动：{url}")
    if open_browser:
        threading.Timer(0.8, lambda: _open_browser(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[chatsearch] 已停止")
    finally:
        server.server_close()
        watcher.stop_watcher()


def _open_in_explorer(path: str) -> tuple[bool, str]:
    """用系统文件管理器打开目录。返回 (是否成功, 错误信息)。"""
    import subprocess
    import sys

    native = path.replace("\\", "/") if not sys.platform.startswith("win") else path.replace("/", "\\")
    try:
        if sys.platform.startswith("win"):
            # explorer 对含空格路径直接传参即可（list 形式不经 shell 解析）
            subprocess.Popen(["explorer", native])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", native])
        else:
            subprocess.Popen(["xdg-open", native])
        return True, ""
    except Exception as e:
        return False, str(e)


def _open_browser(url: str) -> None:
    import webbrowser
    try:
        webbrowser.open(url)
    except Exception:
        pass


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="聊天记录全文检索 Web 界面")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--rebuild", action="store_true", help="强制全量重建索引后启动")
    args = parser.parse_args()

    if args.rebuild:
        print("[chatsearch] 强制全量重建索引...")
        r = indexer.rebuild_index(force=True)
        print(f"[chatsearch] {r['sessions']} 会话 / {r['messages']} 消息 ({r.get('elapsed_ms', 0)}ms)")

    serve(args.host, args.port, open_browser=not args.no_browser)


if __name__ == "__main__":
    main()
