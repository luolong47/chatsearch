"""后端：标准库 http.server 提供检索 API 与静态页面。"""
from __future__ import annotations

import json
import mimetypes
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import indexer, migrate, watcher
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
        if parsed.path == "/api/migrate":
            return self._handle_migrate(parsed)
        if parsed.path == "/api/batch":
            return self._handle_batch()
        if parsed.path == "/api/archive":
            qs = parse_qs(parsed.query)
            key = (qs.get("key", [""])[0] or "").strip()
            return self._handle_batch({"action": "archive", "keys": [key]})
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

    def _handle_migrate(self, parsed) -> None:
        """把一条会话同步到另一端 App（WorkBuddy <-> DSH）。"""
        qs = parse_qs(parsed.query)
        key = (qs.get("key", [""])[0] or "").strip()
        target = (qs.get("target", [""])[0] or "").strip()
        if not key or target not in ("workbuddy", "dsh"):
            return self._send_json({"ok": False, "error": "缺少 key 或 target 非法"}, status=400)
        source, _, sid = key.partition(":")
        if not sid:
            return self._send_json({"ok": False, "error": "key 格式不对"}, status=400)
        # 进行中的会话正在被 App 写入，复制出去会得到半截内容，直接拒绝
        if key in indexer.in_progress_set([key]):
            return self._send_json(
                {"ok": False, "error": "会话进行中，不可同步"}, status=400
            )
        try:
            result = migrate.migrate(source, sid, target)
        except Exception as e:  # noqa: BLE001
            return self._send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, status=500)
        if not result.get("ok"):
            return self._send_json(result, status=400)
        # 新会话已落到原生存储，立刻补一次增量同步，让它出现在检索里
        try:
            indexer.sync_index()
        except Exception:  # noqa: BLE001
            pass
        watcher.note_synced()
        return self._send_json(result)

    # ---------- 批量操作 ----------

    def _read_json_body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0:
            return None
        try:
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _handle_batch(self, body: dict | None = None) -> None:
        """批量操作：{"action": "archive|unarchive|migrate", "keys": [...]}。

        - 归档：进行中的会话跳过（正在被 App 写入，避免写坏）；
        - 取消归档：不限；
        - 同步：进行中跳过，且按每条会话的来源自动选另一端的 App。
        """
        data = body if body is not None else self._read_json_body()
        if not data:
            return self._send_json({"ok": False, "error": "请求体不是合法 JSON"}, status=400)
        action = str(data.get("action") or "").strip()
        raw_keys = data.get("keys") or []
        if not isinstance(raw_keys, list):
            return self._send_json({"ok": False, "error": "keys 必须是数组"}, status=400)
        keys = [str(k).strip() for k in raw_keys if str(k).strip()]
        keys = list(dict.fromkeys(keys))
        if not keys:
            return self._send_json({"ok": False, "error": "没有选中会话"}, status=400)

        try:
            if action in ("archive", "unarchive"):
                result = indexer.set_archived(keys, archived=(action == "archive"))
            elif action == "migrate":
                result = self._batch_migrate(keys)
            else:
                return self._send_json(
                    {"ok": False, "error": f"未知操作：{action}"}, status=400
                )
        except Exception as e:  # noqa: BLE001
            return self._send_json(
                {"ok": False, "error": f"{type(e).__name__}: {e}"}, status=500
            )

        watcher.note_synced()
        # 批量操作可能"部分成功"：统一 200，由 ok/message 表达结果，前端才能展示明细
        return self._send_json(result)

    def _batch_migrate(self, keys: list[str]) -> dict:
        """批量跨 App 同步：每条按来源发到另一端，最后统一重载 / 同步一次。"""
        rows = indexer.session_rows(keys)
        busy = indexer.in_progress_set(keys)
        changed: list[dict] = []
        skipped: list[dict] = []
        failed: list[dict] = []
        touched_wb = False
        for key in keys:
            row = rows.get(key)
            if row is None:
                skipped.append({"key": key, "reason": "会话不存在"})
                continue
            if key in busy:
                skipped.append({"key": key, "reason": "会话进行中，不可同步"})
                continue
            source = row["source"]
            target = "dsh" if source == "workbuddy" else "workbuddy"
            try:
                r = migrate.migrate(source, row["sid"], target, reload_ui=False)
            except Exception as e:  # noqa: BLE001
                failed.append({"key": key, "error": f"{type(e).__name__}: {e}"})
                continue
            if r.get("ok"):
                changed.append({
                    "key": key, "target": target,
                    "target_sid": r.get("target_sid"), "messages": r.get("messages"),
                })
                touched_wb = touched_wb or target == "workbuddy"
            else:
                failed.append({"key": key, "error": r.get("error") or "同步失败"})

        ui_refresh: dict = {}
        if changed:
            # 只有目标端是 WorkBuddy 时才需要重载它的界面（DSH 要重启才认新会话）
            if touched_wb:
                try:
                    from .cdp import reload_workbuddy
                    ui_refresh = reload_workbuddy()
                except Exception as e:  # noqa: BLE001
                    ui_refresh = {"ok": False, "error": str(e)}
            try:
                indexer.sync_index()
            except Exception:  # noqa: BLE001
                pass

        parts = [f"已同步 {len(changed)} 条会话"]
        if skipped:
            parts.append(f"跳过 {len(skipped)} 条（{skipped[0].get('reason')}）")
        if failed:
            parts.append(f"失败 {len(failed)} 条（{failed[0].get('error')}）")
        if changed:
            parts.append("目标是 DSH 的需要重启它才能看到新会话")
        return {
            "ok": not failed,
            "action": "migrate",
            "changed": changed,
            "skipped": skipped,
            "failed": failed,
            "ui_refresh": ui_refresh,
            "error": None,
            "message": "，".join(parts) + "。",
        }


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
