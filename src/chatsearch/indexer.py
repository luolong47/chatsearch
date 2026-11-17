"""索引层：SQLite + FTS5(trigram) 全文索引，支持中文子串检索。

设计要点：
- FTS5 的 trigram tokenizer 原生支持中文子串匹配（unicode61 对中文无效）。
- trigram 对 <3 字符的查询词无能为力，短词用 LIKE 兜底。
- 增量同步：files 表追踪每个源文件的 (mtime_ns, size)，sync_index() 只重解析
  新增 / 修改 / 删除的文件，FTS 通过标准 'delete' 命令增量同步，不再全量 rebuild。
- 搜索结果按会话分组返回，点击会话再取完整时间线。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .config import (
    DSH_SESSIONS,
    DSH_WORKSPACE_JSON,
    INDEX_DB,
    WORKBUDDY_DB,
    WORKBUDDY_PROJECTS,
    ensure_data_dir,
)
from .cdp import reload_dsh, reload_workbuddy
from .models import Session
from .parsers import (
    parse_all,
    parse_file,
    _load_dsh_archived,
    _load_wb_meta,
    _normalize_dsh_sid,
)

# WorkBuddy 每次启动会自动创建一个以时间戳命名的临时工作区
# （c-<用户>-WorkBuddy-YYYY-MM-DD-HH-MM-SS），数量极多且每个通常只有 1 个会话，
# 全部列出来会把工作区列表淹没。这里统一合并成下面这一个虚拟目录名。
# 该名字本身就是友好展示名，前端 cwdLabel/datalist/侧边栏无需任何改动。
WB_DEFAULT_CWD = "WorkBuddy 默认工作区"
_WB_DEFAULT_RE = re.compile(r"^c-.+-WorkBuddy-\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}$")
_WB_DEFAULT_GLOB = "c-*-WorkBuddy-????-??-??-??-??-??"


def _is_wb_default(cwd: str) -> bool:
    """是否为 WorkBuddy 自动创建的时间戳默认工作区。"""
    return bool(_WB_DEFAULT_RE.match(cwd or ""))


def _visible_cwd(cwd: str | None) -> str:
    """把内部 cwd 归一化为对外展示/筛选的键。"""
    c = cwd or ""
    return WB_DEFAULT_CWD if _is_wb_default(c) else c


def _cwd_hit(stored: str, requested: str) -> bool:
    """Python 层 cwd 匹配：虚拟键命中所有默认工作区，否则精确匹配。"""
    if not requested:
        return True
    if requested == WB_DEFAULT_CWD:
        return _is_wb_default(stored)
    return (stored or "") == requested


def _cwd_clause(cwd: str) -> tuple[str, list]:
    """SQL 层 cwd 条件：虚拟键用 GLOB 匹配时间戳目录，否则精确匹配。"""
    if cwd == WB_DEFAULT_CWD:
        return "cwd GLOB ?", [_WB_DEFAULT_GLOB]
    return "cwd = ?", [cwd]


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    key         TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    sid         TEXT NOT NULL,
    title       TEXT NOT NULL,
    cwd         TEXT,
    archived    INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER,
    updated_at  INTEGER,
    msg_count   INTEGER NOT NULL DEFAULT 0,
    dedup_hash  TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT NOT NULL,
    role        TEXT NOT NULL,
    text        TEXT NOT NULL,
    ts          INTEGER,
    seq         INTEGER
);
CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_key);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text,
    content='messages',
    content_rowid='id',
    tokenize='trigram'
);
CREATE TABLE IF NOT EXISTS files (
    path        TEXT PRIMARY KEY,
    mtime_ns    INTEGER NOT NULL,
    size        INTEGER NOT NULL,
    session_key TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""


# ---------------------------------------------------------------------------
# 连接 / schema
# ---------------------------------------------------------------------------

def _connect(db_path: Path = INDEX_DB) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def _prepare(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    # 兼容旧库：IF NOT EXISTS 不会新增列
    cols = {r["name"] for r in con.execute("PRAGMA table_info(sessions)")}
    if cols and "dedup_hash" not in cols:
        con.execute("ALTER TABLE sessions ADD COLUMN dedup_hash TEXT")
    if cols and "archived" not in cols:
        con.execute("ALTER TABLE sessions ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")


def _get_meta(con: sqlite3.Connection, key: str) -> str | None:
    row = con.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
    return row["v"] if row else None


def _set_meta(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute(
        "INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (key, value),
    )


# ---------------------------------------------------------------------------
# 源文件扫描
# ---------------------------------------------------------------------------

def _source_files() -> dict[str, tuple[int, int]]:
    """当前所有源文件 -> (mtime_ns, size)。"""
    out: dict[str, tuple[int, int]] = {}
    for root in (WORKBUDDY_PROJECTS, DSH_SESSIONS):
        if not root.exists():
            continue
        pat = "*.jsonl" if root == WORKBUDDY_PROJECTS else "*.zstd"
        for p in root.rglob(pat):
            try:
                st = p.stat()
                out[str(p)] = (st.st_mtime_ns, st.st_size)
            except OSError:
                continue
    return out


def compute_signature() -> str:
    files = sorted(_source_files().items())
    h = hashlib.sha256()
    for path, (mtime, size) in files:
        h.update(f"{path}|{mtime}|{size}\n".encode("utf-8"))
    h.update(f"count={len(files)}".encode("utf-8"))
    # 归档状态不在会话文件里：WorkBuddy 在 workbuddy.db，DSH 在 workspace.json。
    # 改归档只动这两个文件，故把它们的指纹也纳入签名。
    for label, meta_path in (("wbdb", WORKBUDDY_DB), ("dshws", DSH_WORKSPACE_JSON)):
        meta_sig = ""
        try:
            st = meta_path.stat()
            meta_sig = f"{st.st_mtime_ns}|{st.st_size}"
        except OSError:
            pass
        h.update(f"{label}={meta_sig}".encode("utf-8"))
    return h.hexdigest()


def _refresh_archived(con: sqlite3.Connection) -> int:
    """刷新两个数据源的归档状态，返回发生变化的会话数。

    WorkBuddy 归档在 workbuddy.db.sessions.status；DSH 归档在
    ~/.dsh/storages/workspace.json 的 global.archivedSessionIds。两者都不动
    会话正文文件，无法靠文件 mtime 察觉，故每次 sync 都做一次（只读一个
    sqlite db + 一个 json + 批量 UPDATE，很快）。
    """
    wb_meta = _load_wb_meta()
    dsh_archived = _load_dsh_archived()
    changed = 0
    rows = con.execute(
        "SELECT key,sid,source,archived FROM sessions WHERE source IN ('workbuddy','dsh')"
    ).fetchall()
    for row in rows:
        if row["source"] == "workbuddy":
            entry = wb_meta.get(row["sid"].split("/")[-1])  # sid 尾段是纯 uuid
            if entry is None:
                continue  # db 里无记录（已删除/未知）保持原状
            want = 1 if entry.get("archived") else 0
        else:
            want = 1 if _normalize_dsh_sid(row["sid"]) in dsh_archived else 0
        if row["archived"] != want:
            con.execute("UPDATE sessions SET archived=? WHERE key=?", (want, row["key"]))
            changed += 1
    return changed


def _session_hash(s: Session) -> str:
    """按「角色+正文」序列计算内容指纹，用于跨文件去重。"""
    d = hashlib.sha256()
    for m in s.sorted_messages():
        d.update(f"{m.role}\x00{m.text}\x01".encode("utf-8"))
    return d.hexdigest()


# ---------------------------------------------------------------------------
# 增量同步（核心）
# ---------------------------------------------------------------------------

def _delete_session_from_index(con: sqlite3.Connection, key: str) -> None:
    """删除一个会话的全部消息及其 FTS 条目（外部内容表标准 delete 命令）。"""
    rows = con.execute("SELECT id, text FROM messages WHERE session_key=?", (key,)).fetchall()
    for r in rows:
        con.execute(
            "INSERT INTO messages_fts(messages_fts,rowid,text) VALUES('delete',?,?)",
            (r["id"], r["text"]),
        )
    con.execute("DELETE FROM messages WHERE session_key=?", (key,))
    con.execute("DELETE FROM sessions WHERE key=?", (key,))


def _insert_session_into_index(con: sqlite3.Connection, s: Session) -> None:
    """插入一个会话及其消息，并增量加入 FTS。"""
    con.execute(
        """INSERT INTO sessions(key,source,sid,title,cwd,archived,created_at,updated_at,msg_count,dedup_hash)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            s.key, s.source, s.sid, s.title, s.cwd, int(s.archived),
            s.created_at, s.updated_at,
            sum(1 for m in s.messages if m.is_indexable()),
            _session_hash(s),
        ),
    )
    for m in s.sorted_messages():
        if not m.is_indexable():
            continue
        cur = con.execute(
            "INSERT INTO messages(session_key,role,text,ts,seq) VALUES(?,?,?,?,?)",
            (s.key, m.role, m.text, m.ts, m.seq),
        )
        con.execute(
            "INSERT INTO messages_fts(rowid,text) VALUES(?,?)",
            (cur.lastrowid, m.text),
        )


# watcher 线程与 HTTP 请求线程（maybe_sync_on_search）可能同时触发同步。
# 首次全量建索引时两者都读到"files 表为空"，会并发走 _build_full，
# 同一 session 被插入两次 → UNIQUE constraint failed: sessions.key。串行化即可根除。
_SYNC_LOCK = threading.RLock()


def sync_index() -> dict:
    """增量同步（串行化入口）。"""
    with _SYNC_LOCK:
        return _sync_index_locked()


def _sync_index_locked() -> dict:
    """增量同步：只处理新增 / 修改 / 删除的源文件。返回统计信息。"""
    ensure_data_dir()
    current = _source_files()
    con = _connect()
    try:
        _prepare(con)
        # 归档状态变化不体现在文件 mtime 上，每次 sync 都从 db 刷新一遍
        archived_changed = _refresh_archived(con)
        # 首轮（files 表为空）走全量构建
        if con.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0:
            return _build_full(con, current)

        previous = {
            r["path"]: (r["mtime_ns"], r["size"], r["session_key"])
            for r in con.execute("SELECT path,mtime_ns,size,session_key FROM files")
        }

        added = [p for p in current if p not in previous]
        removed = [p for p in previous if p not in current]
        changed = [
            p for p in current
            if p in previous and previous[p][:2] != current[p]
        ]

        if not added and not removed and not changed:
            if archived_changed:
                con.commit()  # 归档刷新也要落盘
            return {
                "rebuilt": False, "updated": 0,
                "archived_changed": archived_changed,
                "sessions": con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
                "messages": con.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                "signature": compute_signature()[:12],
            }

        t0 = time.time()
        db_meta = _load_wb_meta()
        dsh_archived = _load_dsh_archived()

        # 1) 删除的文件：移除对应会话
        for p in removed:
            old_key = previous[p][2]
            if old_key:
                _delete_session_from_index(con, old_key)
            con.execute("DELETE FROM files WHERE path=?", (p,))

        # 2) 删除变更文件的旧索引（稍后按新内容重建）
        touched: list[str] = []
        for p in changed:
            old_key = previous[p][2]
            if old_key:
                _delete_session_from_index(con, old_key)
            touched.append(p)

        # 3) 去重用的现有 hash 集合（排除刚被删除的变更会话）
        existing_hashes = {
            r["dedup_hash"] for r in con.execute("SELECT dedup_hash FROM sessions")
            if r["dedup_hash"]
        }

        n_added = 0
        dups = 0
        # 4) 解析新增 + 变更文件
        for p in added + changed:
            path = Path(p)
            s = parse_file(path, db_meta, dsh_archived)
            if s is None:
                con.execute(
                    "INSERT OR REPLACE INTO files(path,mtime_ns,size,session_key) VALUES(?,?,?,NULL)",
                    (p, current[p][0], current[p][1]),
                )
                continue
            fp = _session_hash(s)
            if fp in existing_hashes:
                # 内容与其他会话重复：跳过入库，但记录文件指纹，避免反复重解析
                dup_key = con.execute(
                    "SELECT key FROM sessions WHERE dedup_hash=? LIMIT 1", (fp,)
                ).fetchone()
                con.execute(
                    "INSERT OR REPLACE INTO files(path,mtime_ns,size,session_key) VALUES(?,?,?,?)",
                    (p, current[p][0], current[p][1], dup_key["key"] if dup_key else None),
                )
                dups += 1
                continue
            existing_hashes.add(fp)
            _insert_session_into_index(con, s)
            con.execute(
                "INSERT OR REPLACE INTO files(path,mtime_ns,size,session_key) VALUES(?,?,?,?)",
                (p, current[p][0], current[p][1], s.key),
            )
            n_added += 1

        _set_meta(con, "signature", compute_signature())
        _set_meta(con, "built_at", str(int(time.time() * 1000)))
        con.commit()
        return {
            "rebuilt": False,
            "updated": len(added) + len(changed) + len(removed),
            "added": len(added),
            "changed": len(changed),
            "removed": len(removed),
            "sessions_inserted": n_added,
            "duplicates_skipped": dups,
            "archived_changed": archived_changed,
            "sessions": con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
            "messages": con.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
            "signature": _get_meta(con, "signature")[:12],
            "elapsed_ms": int((time.time() - t0) * 1000),
        }
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _build_full(con: sqlite3.Connection, current: dict[str, tuple[int, int]]) -> dict:
    """首次全量构建（仅在 files 表为空时调用）。"""
    t0 = time.time()
    sessions = parse_all()
    # 内容级去重
    deduped: dict[tuple[str, str], Session] = {}
    dup_count = 0
    for s in sessions:
        fp = (s.source, _session_hash(s))
        prev = deduped.get(fp)
        if prev is None or s.updated_at > prev.updated_at:
            if prev is not None:
                dup_count += 1
            deduped[fp] = s
        else:
            dup_count += 1
    sessions = list(deduped.values())

    con.execute("DELETE FROM messages")
    con.execute("DELETE FROM sessions")
    con.execute("DELETE FROM files")
    for s in sessions:
        _insert_session_into_index(con, s)

    # 为每个源文件记录指纹与其归属会话（用于后续增量定位）
    # 通过 session.key = source:sid 反查
    key_to_session = {s.key: s for s in sessions}
    db_meta = _load_wb_meta()
    dsh_archived = _load_dsh_archived()
    for p, (mtime, size) in current.items():
        s = parse_file(Path(p), db_meta, dsh_archived)
        linked = None
        if s is not None:
            fp = (s.source, _session_hash(s))
            kept = deduped.get(fp)
            # 仅当被保留的会话与该文件 sid 一致时才直接关联；否则关联到同内容会话
            if kept is not None:
                linked = kept.key
        con.execute(
            "INSERT OR REPLACE INTO files(path,mtime_ns,size,session_key) VALUES(?,?,?,?)",
            (p, mtime, size, linked),
        )

    _set_meta(con, "signature", compute_signature())
    _set_meta(con, "built_at", str(int(time.time() * 1000)))
    con.commit()
    return {
        "rebuilt": True,
        "sessions": len(sessions),
        "messages": con.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
        "duplicates_removed": dup_count,
        "signature": _get_meta(con, "signature")[:12],
        "elapsed_ms": int((time.time() - t0) * 1000),
    }


def rebuild_index(force: bool = False) -> dict:
    """强制全量重建（串行化入口）。"""
    with _SYNC_LOCK:
        return _rebuild_index_locked(force)


def _rebuild_index_locked(force: bool = False) -> dict:
    """强制全量重建（兼容旧调用）。

    force=False 时若签名未变则跳过；日常请用 sync_index() 做增量。
    """
    ensure_data_dir()
    con = _connect()
    try:
        _prepare(con)
        sig = compute_signature()
        if not force and _get_meta(con, "signature") == sig:
            return {
                "rebuilt": False, "updated": 0,
                "sessions": con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
                "messages": con.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                "signature": sig[:12],
            }
        # 清空 files 表强制走全量构建路径
        con.execute("DELETE FROM files")
        con.commit()
        return _build_full(con, _source_files())
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def index_status() -> dict:
    if not INDEX_DB.exists():
        return {"exists": False}
    con = _connect()
    try:
        _prepare(con)
        n_s = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        n_m = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        built = _get_meta(con, "built_at")
        return {
            "exists": True,
            "sessions": n_s,
            "messages": n_m,
            "built_at": int(built) if built else 0,
            "stale": _get_meta(con, "signature") != compute_signature(),
        }
    finally:
        con.close()


@dataclass
class Hit:
    session_key: str
    source: str
    sid: str
    title: str
    cwd: str
    updated_at: int
    msg_count: int
    match_count: int
    snippets: list[dict]


def _snippet(text: str, words: list[str], width: int = 90) -> str:
    low = text.lower()
    pos = -1
    for w in words:
        i = low.find(w.lower())
        if i >= 0 and (pos < 0 or i < pos):
            pos = i
    if pos < 0:
        pos = 0
    start = max(0, pos - width // 3)
    end = min(len(text), pos + width)
    s = text[start:end].replace("\n", " ").replace("\r", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return ("…" if start > 0 else "") + s + ("…" if end < len(text) else "")


# 中英文/数字边界也算分词点：'复刻DeepSeek Harness配置' -> 复刻 / DeepSeek / Harness / 配置
_TERM_RE = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+|[A-Za-z0-9_]+"
)


def _query_terms(query: str) -> list[str]:
    """把查询拆成检索词：空格切分 + 中英文/数字边界切分。

    这样中文标题与中英混排正文都能命中，而不再要求整串连续出现。
    例：'复刻DeepSeek Harness配置' -> ['复刻', 'DeepSeek', 'Harness', '配置']
    """
    out: list[str] = []
    for chunk in (query or "").split():
        parts = _TERM_RE.findall(chunk)
        if not parts:
            continue
        if len(parts) == 1 and len(parts[0]) == 1:
            out.append(parts[0])  # 单字查询（如 "刷"）保留
        else:
            out.extend(p for p in parts if len(p) >= 2)
    seen: set[str] = set()
    terms: list[str] = []
    for t in out:
        low = t.lower()
        if low not in seen:
            seen.add(low)
            terms.append(t)
    return terms


def search(
    query: str,
    source: str | None = None,
    cwd: str | None = None,
    archived: bool | None = None,
    limit: int = 200,
) -> dict:
    query = (query or "").strip()
    if not query:
        return {"query": query, "total_sessions": 0, "hits": []}
    terms = _query_terms(query)
    if not terms:
        return {"query": query, "total_sessions": 0, "hits": []}
    lowered = [t.lower() for t in terms]
    con = _connect()
    try:
        _prepare(con)
        long_terms = [t for t in terms if len(t) >= 3]
        short_terms = [t for t in terms if len(t) < 3]
        rows: dict[int, dict] = {}

        if long_terms:
            match_expr = " AND ".join(f'"{t.replace(chr(34), chr(34)*2)}"' for t in long_terms)
            sql = """
                SELECT m.id, m.session_key, m.role, m.text, m.ts
                FROM messages_fts f
                JOIN messages m ON m.id = f.rowid
                WHERE messages_fts MATCH ?
            """
            params: list = [match_expr]
            if source:
                sql += " AND m.session_key LIKE ?"
                params.append(f"{source}:%")
            sql += " ORDER BY m.ts DESC LIMIT 5000"
            for r in con.execute(sql, params):
                rows[r["id"]] = dict(r)

        if short_terms:
            for t in short_terms:
                sql = "SELECT id,session_key,role,text,ts FROM messages WHERE text LIKE ?"
                params = [f"%{t}%"]
                if source:
                    sql += " AND session_key LIKE ?"
                    params.append(f"{source}:%")
                sql += " ORDER BY ts DESC LIMIT 5000"
                for r in con.execute(sql, params):
                    rows[r["id"]] = dict(r)

        # FTS 已保证 long_terms 全部命中；这里再统一校验所有词（含 short_terms）
        for mid, r in list(rows.items()):
            low = r["text"].lower()
            if not all(t in low for t in lowered):
                rows.pop(mid, None)

        def _group(meta) -> dict:
            return {
                "session_key": meta["key"],
                "source": meta["source"],
                "sid": meta["sid"],
                "title": meta["title"],
                "cwd": _visible_cwd(meta["cwd"]),
                "archived": bool(meta["archived"]),
                "updated_at": meta["updated_at"] or 0,
                "msg_count": meta["msg_count"],
                "match_count": 0,
                "snippets": [],
            }

        groups: dict[str, dict] = {}
        for r in rows.values():
            key = r["session_key"]
            g = groups.get(key)
            if g is None:
                meta = con.execute(
                    "SELECT key,source,sid,title,cwd,archived,updated_at,msg_count "
                    "FROM sessions WHERE key=?",
                    (key,),
                ).fetchone()
                if not meta:
                    continue
                # 工作目录 / 归档筛选（在会话元数据层面过滤）
                if not _cwd_hit(meta["cwd"] or "", cwd):
                    continue
                if archived is not None and bool(meta["archived"]) != archived:
                    continue
                g = _group(meta)
                groups[key] = g
            g["match_count"] += 1
            if len(g["snippets"]) < 3:
                g["snippets"].append({
                    "role": r["role"],
                    "text": _snippet(r["text"], terms),
                    "ts": r["ts"] or 0,
                })

        # 标题命中：会话数量有限（几百条），直接逐条 LIKE，不额外维护 FTS 表
        tsql = "SELECT key,source,sid,title,cwd,archived,updated_at,msg_count FROM sessions"
        conditions: list[str] = []
        tparams: list = []
        if source:
            conditions.append("key LIKE ?")
            tparams.append(f"{source}:%")
        if cwd:
            c_clause, c_params = _cwd_clause(cwd)
            conditions.append(c_clause)
            tparams.extend(c_params)
        if archived is not None:
            conditions.append("archived=?")
            tparams.append(int(archived))
        if conditions:
            tsql += " WHERE " + " AND ".join(conditions)
        for meta in con.execute(tsql, tparams):
            title = meta["title"] or ""
            low = title.lower()
            if not all(t in low for t in lowered):
                continue
            key = meta["key"]
            g = groups.get(key)
            if g is None:
                g = _group(meta)
                groups[key] = g
            g["match_count"] += 1
            if len(g["snippets"]) < 3:
                g["snippets"].append({
                    "role": "title", "text": title, "ts": meta["updated_at"] or 0,
                })

        hits = sorted(
            groups.values(),
            key=lambda g: (g["match_count"], g["updated_at"]),
            reverse=True,
        )[:limit]
        return {
            "query": query, "total_sessions": len(groups), "hits": hits,
            "filters": {"source": source, "cwd": cwd, "archived": archived},
        }
    finally:
        con.close()


def get_session(session_key: str) -> dict | None:
    con = _connect()
    try:
        _prepare(con)
        meta = con.execute("SELECT * FROM sessions WHERE key=?", (session_key,)).fetchone()
        if not meta:
            return None
        msgs = con.execute(
            "SELECT role,text,ts,seq FROM messages WHERE session_key=? ORDER BY ts,seq",
            (session_key,),
        ).fetchall()
        return {
            "session_key": session_key,
            "source": meta["source"],
            "sid": meta["sid"],
            "title": meta["title"],
            "cwd": meta["cwd"] or "",
            "archived": bool(meta["archived"]),
            "created_at": meta["created_at"] or 0,
            "updated_at": meta["updated_at"] or 0,
            "messages": [
                {"role": m["role"], "text": m["text"], "ts": m["ts"] or 0, "seq": m["seq"]}
                for m in msgs
            ],
        }
    finally:
        con.close()


def resolve_cwd(session_key: str) -> dict | None:
    """返回会话可操作的真实工作目录。

    WorkBuddy 索引里的 cwd 是项目目录 slug（如 c-prjs-sdbs），不是真实路径；
    真实路径在 workbuddy.db.sessions.cwd。这里按 sid 尾段回查真实路径。
    DSH 索引里的 cwd 本身就是真实路径，直接用。
    返回 {"cwd": str, "exists": bool} 或 None（会话不存在 / 无可用目录）。
    """
    con = _connect()
    try:
        meta = con.execute(
            "SELECT source, sid, cwd FROM sessions WHERE key=?", (session_key,)
        ).fetchone()
    finally:
        con.close()
    if not meta:
        return None

    source = meta["source"]
    cwd = meta["cwd"] or ""
    if source == "workbuddy":
        # sid 可能是 "<slug>/<uuid>" 或 "<uuid>"，尾段为 db 的 session id
        sid = (meta["sid"] or "").split("/")[-1]
        try:
            db = sqlite3.connect(f"file:{WORKBUDDY_DB}?mode=ro", uri=True)
            try:
                row = db.execute(
                    "SELECT cwd FROM sessions WHERE id=?", (sid,)
                ).fetchone()
                if row and row[0]:
                    cwd = row[0]
            finally:
                db.close()
        except Exception:
            pass  # db 不可读时回退到索引里的 slug
    if not cwd:
        return None
    p = Path(cwd.replace("\\", "/"))
    return {"cwd": cwd, "exists": p.is_dir()}


def list_sessions(
    source: str | None = None,
    cwd: str | None = None,
    archived: bool | None = None,
    limit: int = 100,
) -> dict:
    con = _connect()
    try:
        _prepare(con)
        clauses: list[str] = []
        params: list = []
        if source:
            clauses.append("source=?")
            params.append(source)
        if cwd:
            c_clause, c_params = _cwd_clause(cwd)
            clauses.append(c_clause)
            params.extend(c_params)
        if archived is not None:
            clauses.append("archived=?")
            params.append(int(archived))
        sql = "SELECT key,source,sid,title,cwd,archived,updated_at,msg_count FROM sessions"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        rows = con.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["cwd"] = _visible_cwd(d["cwd"])
            out.append(d)
        return {"sessions": out}
    finally:
        con.close()


def filter_options(source: str | None = None, archived: bool | None = None) -> dict:
    """返回工作目录选项（含会话数与归档计数），供前端联想下拉和侧边栏。

    source / archived 与搜索筛选保持同义：选 DSH 时只返回 DSH 的工作区。
    archived_counts 只受 source 影响，保证「全部状态」的统计不被当前归档选择截断。
    """
    con = _connect()
    try:
        _prepare(con)
        clauses: list[str] = []
        params: list = []
        if source:
            clauses.append("source=?")
            params.append(source)
        if archived is not None:
            clauses.append("archived=?")
            params.append(int(archived))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = con.execute(
            """SELECT cwd,
                      COUNT(*) AS total,
                      SUM(CASE WHEN archived=1 THEN 1 ELSE 0 END) AS archived_n
               FROM sessions""" + where + """
               GROUP BY cwd
               ORDER BY total DESC, cwd""",
            params,
        ).fetchall()
        # WorkBuddy 时间戳默认工作区合并成一条（特例），避免列表被上百个
        # 只有 1 个会话的条目淹没；其余真实工作目录原样保留。
        cwds: list[dict] = []
        wb_total = 0
        wb_archived = 0
        wb_seen = False
        for r in rows:
            cwd = r["cwd"] or ""
            if not cwd:
                continue
            if _is_wb_default(cwd):
                wb_seen = True
                wb_total += r["total"]
                wb_archived += r["archived_n"] or 0
            else:
                cwds.append({"cwd": cwd, "total": r["total"], "archived": r["archived_n"] or 0})
        if wb_seen:
            cwds.append({"cwd": WB_DEFAULT_CWD, "total": wb_total, "archived": wb_archived})
        cwds.sort(key=lambda x: (-x["total"], x["cwd"]))
        # 归档统计只按来源过滤，不受当前归档二分影响
        src_where = " WHERE source=?" if source else ""
        arc = con.execute(
            "SELECT SUM(CASE WHEN archived=1 THEN 1 ELSE 0 END) a, COUNT(*) n FROM sessions"
            + src_where,
            [source] if source else [],
        ).fetchone()
        return {
            "cwds": cwds,
            "archived_counts": {
                "all": arc["n"] or 0,
                "archived": arc["a"] or 0,
                "active": (arc["n"] or 0) - (arc["a"] or 0),
            },
        }
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 取消归档：写回 WorkBuddy / DSH 的原生存储
# ---------------------------------------------------------------------------

_BACKUP_DIR = INDEX_DB.parent / "backups"


def _backup_stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"


def _backup_sqlite(src: Path, dst: Path) -> None:
    """用 SQLite 在线备份 API，避免直接复制 WAL 库拿到不一致快照。"""
    src_con = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=10)
    dst_con = sqlite3.connect(str(dst))
    try:
        src_con.backup(dst_con)
    finally:
        dst_con.close()
        src_con.close()


def _unarchive_workbuddy(
    sid: str,
    db_path: Path = WORKBUDDY_DB,
    backup_dir: Path = _BACKUP_DIR,
) -> dict:
    """把 WorkBuddy 会话从 archived 恢复为 active（与应用自身 setArchived(false) 一致）。"""
    sid = (sid or "").strip()
    if not sid:
        return {"ok": False, "error": "缺少 WorkBuddy 会话 id"}
    con = sqlite3.connect(f"file:{db_path}", uri=True, timeout=10)
    try:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=8000")
        row = con.execute(
            "SELECT status, deleted_at FROM sessions WHERE id=?", (sid,)
        ).fetchone()
        if row is None:
            return {"ok": False, "error": "WorkBuddy 数据库中找不到该会话"}
        status = (row["status"] or "").lower()
        if status != "archived":
            return {"ok": True, "changed": False, "native_status": row["status"]}
        if row["deleted_at"] is not None:
            return {"ok": False, "error": "该会话在 WorkBuddy 中已被删除，未改动"}

        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"workbuddy-{_backup_stamp()}.db"
        _backup_sqlite(db_path, backup)

        now_ms = int(time.time() * 1000)
        # last_activity_at 一起刷新：WorkBuddy 侧边栏「最近」按它排序且只显示少数几条，
        # 只改 status 的话会话虽然回到未归档，仍会沉在几百条之后，用户看不到。
        cur = con.execute(
            "UPDATE sessions SET status='active', updated_at=?, last_activity_at=? "
            "WHERE id=? AND status='archived' AND deleted_at IS NULL",
            (now_ms, now_ms, sid),
        )
        con.commit()
        if cur.rowcount != 1:
            return {
                "ok": False,
                "error": "写入未生效（状态可能已被其它进程修改）",
                "backup": str(backup),
            }
        return {
            "ok": True,
            "changed": True,
            "native_status": "active",
            "activity_bumped": True,
            "backup": str(backup),
        }
    finally:
        con.close()


def _unarchive_dsh(
    sid: str,
    workspace_path: Path = DSH_WORKSPACE_JSON,
    backup_dir: Path = _BACKUP_DIR,
) -> dict:
    """从 DSH workspace.json 的 archivedSessionIds 中移除会话 id。"""
    norm = _normalize_dsh_sid((sid or "").strip())
    if not norm:
        return {"ok": False, "error": "缺少 DSH 会话 id"}
    if not workspace_path.exists():
        return {"ok": False, "error": "找不到 DSH workspace.json"}
    try:
        data = json.loads(workspace_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return {"ok": False, "error": f"读取 DSH workspace.json 失败：{e}"}
    global_obj = data.get("global")
    ids = global_obj.get("archivedSessionIds") if isinstance(global_obj, dict) else None
    if not isinstance(ids, list):
        return {"ok": False, "error": "DSH workspace.json 结构异常（archivedSessionIds 不是数组）"}

    def matches(value) -> bool:
        s = str(value)
        return s == norm or s == f"session-{norm}" or _normalize_dsh_sid(s) == norm

    if not any(matches(x) for x in ids):
        return {"ok": True, "changed": False, "native_status": "active"}

    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"dsh-workspace-{_backup_stamp()}.json"
    shutil.copy2(workspace_path, backup)

    global_obj["archivedSessionIds"] = [x for x in ids if not matches(x)]
    tmp = workspace_path.with_name(f"{workspace_path.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, workspace_path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return {
        "ok": True,
        "changed": True,
        "native_status": "active",
        "backup": str(backup),
    }


def unarchive_session(key: str) -> dict:
    """把一条已归档会话恢复为未归档，并同步刷新索引。

    只写原生存储（WorkBuddy sqlite / DSH workspace.json），不直接改索引；
    写前自动备份，写后 sync_index 让检索端立刻反映新状态。

    判断是否已归档**只看原生存储**，不信索引缓存：索引由 watchdog 异步刷新，
    刚在应用里归档/恢复过的会话，索引可能还是旧值，用它做前置判断会误报「无需操作」。
    """
    con = _connect()
    try:
        _prepare(con)
        row = con.execute(
            "SELECT source,sid,title FROM sessions WHERE key=?", (key,)
        ).fetchone()
    finally:
        con.close()
    if row is None:
        return {"ok": False, "error": "会话不存在"}
    source = row["source"]
    sid = row["sid"] or ""
    title = row["title"] or ""

    if source == "workbuddy":
        result = _unarchive_workbuddy(sid.split("/")[-1])
        if result.get("ok") and result.get("changed"):
            # WorkBuddy 前端不监听数据库，外部写库后必须让它自己重拉一次列表
            ui = reload_workbuddy()
            result["ui_refresh"] = ui
            hint = (
                "已在 WorkBuddy 中恢复为未归档，并重载了它的界面：侧边栏「最近」顶部即可看到。"
                if ui.get("ok") else
                "已在 WorkBuddy 中恢复为未归档；自动重载界面失败（调试端口未就绪），"
                "手动重开 WorkBuddy 窗口即可看到。"
            )
        else:
            hint = "WorkBuddy 中已是未归档状态，已刷新本地索引。"
    elif source == "dsh":
        result = _unarchive_dsh(sid)
        if result.get("ok") and result.get("changed"):
            # DSH 的归档列表由 host 进程持有，改文件后要让界面重新拉一次
            ui = reload_dsh()
            result["ui_refresh"] = ui
            hint = (
                "已从 DSH 归档列表移除，并重载了它的界面：归档目录里已看不到该会话。"
                if ui.get("ok") else
                "已从 DSH 归档列表移除；自动重载界面失败（DSH 未带调试端口启动），"
                "重启 DeepSeek Harness 后生效。"
            )
        else:
            hint = "DSH 中已是未归档状态，已刷新本地索引。"
    else:
        return {"ok": False, "error": f"不支持的来源：{source}"}

    if not result.get("ok"):
        return result

    result.update({
        "source": source,
        "title": title,
        "message": hint,
    })
    try:
        sync_index()
        con = _connect()
        try:
            row2 = con.execute(
                "SELECT archived FROM sessions WHERE key=?", (key,)
            ).fetchone()
        finally:
            con.close()
        result["index_archived"] = bool(row2["archived"]) if row2 else None
    except Exception as e:  # 写原生存储成功但索引刷新失败时，如实告知
        result["sync_error"] = str(e)
    return result
