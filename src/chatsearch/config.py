"""数据源路径与全局配置。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

HOME = Path.home()

# WorkBuddy 会话记录目录：projects/<slug>/<session-id>.jsonl
WORKBUDDY_PROJECTS = HOME / ".workbuddy" / "projects"
WORKBUDDY_DB = HOME / ".workbuddy" / "workbuddy.db"  # sessions 表，用于补标题

# DeepSeek Harness 会话：.dsh/sessions/<workspace>/session-<id>/session.v4.jsonl.zstd
DSH_SESSIONS = HOME / ".dsh" / "sessions"
# DSH 的归档/置顶状态不在会话文件里，而在 storages/workspace.json 的
# global.archivedSessionIds（元素形如 session-<uuid>）
DSH_STORAGES = HOME / ".dsh" / "storages"
DSH_WORKSPACE_JSON = DSH_STORAGES / "workspace.json"

# DSH 桌面端（Electron）的 userData 目录。快捷方式带 --remote-debugging-port=0
# 启动时，Electron 会把随机分配的调试端口写进 DevToolsActivePort 的首行。
DSH_DESKTOP_USER_DATA = Path(os.environ.get("APPDATA") or HOME) / "@deepseek-ai" / "dsh-desktop"
DSH_DEVTOOLS_FILE = DSH_DESKTOP_USER_DATA / "DevToolsActivePort"

# 索引数据库存放位置
# 源码运行：项目根；PyInstaller 打包：exe 所在目录（便于 scoop persist 接管）
if getattr(sys, "frozen", False):
    PROJECT_ROOT = Path(sys.executable).resolve().parent
else:
    PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data"
INDEX_DB = DATA_DIR / "index.sqlite3"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

SOURCE_WORKBUDDY = "workbuddy"
SOURCE_DSH = "dsh"

SOURCE_META = {
    SOURCE_WORKBUDDY: {"label": "WorkBuddy", "color": "#7c9cff"},
    SOURCE_DSH: {"label": "DeepSeek Harness", "color": "#4ec9a5"},
}


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
