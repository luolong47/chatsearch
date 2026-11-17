"""索引自动更新：watchdog 事件监听 + 防抖 + 搜索前兜底 + 降级轮询。

设计目标：
- 平时零开销：由文件系统事件驱动，不再每 15 秒全量扫 721 个文件。
- 实时感知：源文件一变（WorkBuddy 每句话落盘 / DSH 会话写入）就自动增量同步。
- 防抖合并：WorkBuddy 一次提问会触发多次 modified 事件，合并成一次同步。
- 兜底：搜索前若距上次同步过久，做一次轻量检查（防事件丢失 / 服务刚启动）。
- 降级：watchdog 不可用（导入失败 / Observer 启动失败）时回退到低频定时轮询。
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable

from . import indexer
from .config import DSH_SESSIONS, DSH_STORAGES, WORKBUDDY_PROJECTS

log = logging.getLogger("chatsearch.watcher")

DEBOUNCE_SEC = 1.5       # 事件合并窗口：窗口内多次事件只触发一次同步
POLL_FALLBACK_SEC = 60   # watchdog 降级时的轮询间隔（秒）
SEARCH_STALE_SEC = 30    # 搜索前兜底：距上次同步超过此值才检查签名
MIN_TRIGGER_SEC = 0.25   # 事件触发频率下限，避免极端情况下 Timer 抖动

# 源文件正文，以及 DSH 存放归档状态的 workspace.json
def _is_watched(path: str | Path) -> bool:
    low = str(path).lower()
    if low.endswith((".jsonl", ".zstd")):
        return True
    name = Path(low).name
    return name == "workspace.json" or name.startswith("workspace.json.")

# 模块级状态
_started = False
_mode = "off"            # off / watchdog / poll
_last_sync_ts = 0.0      # 上次同步完成的时间戳（time.time）
_state_lock = threading.Lock()
_observer = None         # 强引用，防止被 GC
_stop_event = threading.Event()  # 降级轮询的停止信号


# ---------------------------------------------------------------------------
# watchdog 事件处理器（watchdog 缺失时用 stub 保证模块可导入）
# ---------------------------------------------------------------------------
try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    _HAS_WATCHDOG = True
except ImportError:  # pragma: no cover
    _HAS_WATCHDOG = False

    class FileSystemEventHandler:  # type: ignore[no-redef]
        pass

    Observer = None  # type: ignore[assignment]


class _Debouncer:
    """把密集事件合并成一次延迟回调（重置式定时器）。"""

    def __init__(self, delay: float, callback: Callable[[], None]) -> None:
        self.delay = delay
        self.callback = callback
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def trigger(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.delay, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        with self._lock:
            self._timer = None
        try:
            self.callback()
        except Exception as e:  # 同步失败不能拖垮监听线程
            log.warning("[chatsearch] 自动同步失败：%s", e)


class _SourceEventHandler(FileSystemEventHandler):
    """只对源文件及归档元数据（workspace.json）的增删改移感兴趣。"""

    def __init__(self, debouncer: _Debouncer) -> None:
        self.debouncer = debouncer
        self._last_trigger = 0.0

    def on_any_event(self, event) -> None:  # watchdog 回调
        if event.event_type not in ("modified", "created", "deleted", "moved"):
            return
        if event.is_directory:
            return
        path = getattr(event, "dst_path", None) or getattr(event, "src_path", "") or ""
        if not _is_watched(path):
            return
        now = time.monotonic()
        if now - self._last_trigger < MIN_TRIGGER_SEC:
            return
        self._last_trigger = now
        self.debouncer.trigger()


# ---------------------------------------------------------------------------
# 同步执行
# ---------------------------------------------------------------------------
def _do_sync() -> dict | None:
    global _last_sync_ts
    try:
        result = indexer.sync_index()
    except Exception as e:
        log.warning("[chatsearch] 索引同步失败：%s", e)
        return None
    if result.get("updated", 0):
        log.info(
            "[chatsearch] 自动同步 %d 个文件 (+%d/~%d/-%d)，%dms",
            result["updated"], result.get("added", 0), result.get("changed", 0),
            result.get("removed", 0), result.get("elapsed_ms", 0),
        )
        print(f"[chatsearch] 自动同步 {result['updated']} 个文件 "
              f"({result.get('elapsed_ms', 0)}ms)")
    with _state_lock:
        _last_sync_ts = time.time()
    return result


def maybe_sync_on_search() -> dict | None:
    """搜索前兜底：距上次同步过久才做一次轻量签名检查，变了才同步。

    绝大多数情况下 watchdog 已实时同步，这里几乎零开销；
    仅在事件丢失 / 服务刚启动 / 外部进程改文件时兜底。
    """
    with _state_lock:
        age = time.time() - _last_sync_ts
    if age < SEARCH_STALE_SEC:
        return None
    return _do_sync()


def _poll_loop() -> None:
    """降级路径：watchdog 不可用时低频定时检查。"""
    while not _stop_event.wait(POLL_FALLBACK_SEC):
        _do_sync()


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------
def start_watcher() -> str:
    """启动自动更新监听，返回实际模式：watchdog / poll。"""
    global _started, _mode, _observer, _last_sync_ts

    if _started:
        return _mode
    _started = True
    _stop_event.clear()
    _last_sync_ts = time.time()

    debouncer = _Debouncer(DEBOUNCE_SEC, _do_sync)

    if _HAS_WATCHDOG:
        try:
            observer = Observer()
            handler = _SourceEventHandler(debouncer)
            scheduled = 0
            for root in (WORKBUDDY_PROJECTS, DSH_SESSIONS, DSH_STORAGES):
                if root.exists():
                    observer.schedule(handler, str(root), recursive=True)
                    scheduled += 1
            if scheduled:
                observer.daemon = True
                observer.start()
                _observer = observer
                _mode = "watchdog"
                print(f"[chatsearch] 文件监听已启动（watchdog，{scheduled} 个目录）")
                return _mode
        except Exception as e:
            log.warning("[chatsearch] watchdog 启动失败，降级为定时轮询：%s", e)

    # 降级：定时轮询
    t = threading.Thread(target=_poll_loop, name="chatsearch-poll", daemon=True)
    t.start()
    _mode = "poll"
    print(f"[chatsearch] 文件监听降级为定时轮询（每 {POLL_FALLBACK_SEC}s）")
    return _mode


def note_synced() -> None:
    """外部（如手动 /api/rebuild）完成同步后调用，重置兜底计时。"""
    global _last_sync_ts
    with _state_lock:
        _last_sync_ts = time.time()


def stop_watcher() -> None:
    """停止监听线程，让宿主进程能干净退出（托盘点「退出」/ 重启服务时调用）。

    不停这一步的话，watchdog 的 observer 线程会挂住解释器，进程残留、
    单实例互斥锁不释放，下次启动会误判"已在运行"。
    """
    global _started, _mode, _observer

    _stop_event.set()
    observer, _observer = _observer, None
    if observer is not None:
        try:
            observer.stop()
            observer.join(timeout=5)
        except Exception as e:  # pragma: no cover
            log.warning("[chatsearch] 停止文件监听失败：%s", e)
    _started = False
    _mode = "off"
    print("[chatsearch] 文件监听已停止")


def get_status() -> dict:
    """供 /api/status 展示监听状态。"""
    with _state_lock:
        age = round(time.time() - _last_sync_ts, 1) if _last_sync_ts else None
    return {
        "mode": _mode,
        "last_sync_ago_sec": age,
        "debounce_sec": DEBOUNCE_SEC,
        "search_stale_sec": SEARCH_STALE_SEC,
    }
