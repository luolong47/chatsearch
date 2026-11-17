"""数据源路径与全局配置。"""
from __future__ import annotations

import os
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

# 索引数据库存放位置（本工作区）
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
