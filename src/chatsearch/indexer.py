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
from .cdp import reload_workbuddy, unarchive_dsh_via_ui
from . import runtime
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
    dedup_hash  TEXT,
    native_status TEXT,
    last_active INTEGER,
    tail_closed INTEGER
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
-- 跨 App 同步登记：记录「哪个源会话同步到哪一端、写出的 sid、当时的内容指纹」。
-- 用途：1) 幂等，重复点同步不再生成新副本；2) 源内容变化时定位旧副本做替换。
-- 这张表不参与索引重建（_build_full 只清 sessions/messages/files）。
CREATE TABLE IF NOT EXISTS migrations (
    source_key    TEXT NOT NULL,
    target_source TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    target_sid    TEXT NOT NULL,
    updated_at    INTEGER,
    PRIMARY KEY (source_key, target_source)
);
CREATE INDEX IF NOT EXISTS idx_migrations_hash ON migrations(target_source, content_hash);
"""


# ---------------------------------------------------------------------------
# 连接 / schema
# ---------------------------------------------------------------------------

def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    # 运行时读模块级 INDEX_DB（而非默认参数绑定），便于测试整体切到临时库
    con = sqlite3.connect(str(db_path or INDEX_DB))
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
    # 原生状态列：用于判定「会话进行中」（不可归档 / 不可同步）
    if cols and "native_status" not in cols:
        con.execute("ALTER TABLE sessions ADD COLUMN native_status TEXT")
    if cols and "last_active" not in cols:
        con.execute("ALTER TABLE sessions ADD COLUMN last_active INTEGER")
    # 尾部收尾状态：NULL=不知道（DSH / 旧库），1=这一轮已答完，0=写到一半
    if cols and "tail_closed" not in cols:
        con.execute("ALTER TABLE sessions ADD COLUMN tail_closed INTEGER")


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
        "SELECT key,sid,source,archived,native_status,last_active FROM sessions "
        "WHERE source IN ('workbuddy','dsh')"
    ).fetchall()
    for row in rows:
        if row["source"] == "workbuddy":
            entry = wb_meta.get(row["sid"].split("/")[-1])  # sid 尾段是纯 uuid
            if entry is None:
                continue  # db 里无记录（已删除/未知）保持原状
            want = 1 if entry.get("archived") else 0
            status = entry.get("status") or ""
            last = int(entry.get("last_active") or 0)
        else:
            want = 1 if _normalize_dsh_sid(row["sid"]) in dsh_archived else 0
            status = "archived" if want else "active"
            last = row["last_active"] or 0
        if (row["archived"] != want
                or (row["native_status"] or "") != status
                or (row["last_active"] or 0) != last):
            con.execute(
                "UPDATE sessions SET archived=?,native_status=?,last_active=? WHERE key=?",
                (want, status, last, row["key"]),
            )
            changed += 1
    return changed


# 兜底时间窗（只有在拿不到进程心跳时才用，见下）。
# 尾部未收尾（写到一半）+ 文件这么久内被写过 → 认为还在跑。
OPEN_TAIL_GRACE_MS = 90 * 1000
# 尾部状态未知（DSH、老索引）时，只好按文件新鲜度猜。
UNKNOWN_TAIL_WINDOW_MS = 120 * 1000

# 索引里「尾部状态」的版本标记。老库缺这一列时全量重建一次。
TAIL_SCHEMA_VERSION = "1"


def in_progress_keys(con: sqlite3.Connection, now_ms: int | None = None) -> set[str]:
    """返回「正在进行中」的会话 key 集合。

    判定顺序（前一条成立就不再看后面）：

    1. **有活着的 agent 进程**：`~/.workbuddy/sessions/<pid>.json` 里该会话的心跳
       新鲜、pid 还在、进程创建时间对得上 → 正在生成。这是唯一的一手信号，
       不依赖任何时间窗，长工具调用跑半小时也不会误判（见 runtime.py）。
    2. **App 自报 working**：原生 status 为 working/running 且 App 确实在运行
       （有任意心跳新鲜）。兜住心跳文件还没来得及写的瞬间；App 已经退出时这条
       不算，否则被强杀的会话会永久锁在 working 上。
    3. **尾部已收尾**：会话文件最后一条有效事件是 assistant 的最终回复 →
       这一轮结束了。哪怕文件 1 秒前才被写过、3 秒前才答完，也不算进行中。
       （旧实现只看 mtime，刚聊完的会话会被白锁 5 分钟。）
    4. **兜底**：尾部未收尾且 90 秒内被写过；尾部未知（DSH）且 120 秒内被写过。
       用来识别「生成中但心跳缺失」和「被杀在半路、文件停在 function_call」的区别
       ——后者时间一过就不再锁。

    进行中的会话不允许归档 / 跨 App 同步，避免写坏正在被 App 写入的会话。
    """
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    try:
        live, app_up = runtime.snapshot(now)
    except Exception:  # 心跳目录不可读时退回纯文件兜底，别让功能整块失效
        live, app_up = frozenset(), False

    mtimes = {
        r["session_key"]: r["mtime_ns"]
        for r in con.execute(
            "SELECT session_key, MAX(mtime_ns) AS mtime_ns FROM files "
            "WHERE session_key IS NOT NULL GROUP BY session_key"
        )
    }
    out: set[str] = set()
    for row in con.execute(
        "SELECT key,sid,native_status,tail_closed FROM sessions"
    ):
        key = row["key"]
        # 1) 会话 id 匹配活进程（WorkBuddy 的 sid 是 "<slug>/<uuid>"，取尾段）
        uid = (row["sid"] or "").split("/")[-1]
        if uid and uid in live:
            out.add(key)
            continue
        status = (row["native_status"] or "").lower()
        if app_up and status in ("working", "running", "in_progress"):
            out.add(key)
            continue
        tail = row["tail_closed"]
        if tail == 1:
            continue  # 3) 这一轮已经答完
        mt = mtimes.get(key)
        if not mt:
            continue
        age_ms = now - mt / 1_000_000
        window = OPEN_TAIL_GRACE_MS if tail == 0 else UNKNOWN_TAIL_WINDOW_MS
        if age_ms < window:
            out.add(key)
    return out


def session_hash(s: Session) -> str:
    """按「角色+正文」序列计算内容指纹，用于跨文件去重与同步幂等判定。

    同一段对话在两端 App 里解析出的 role/text 序列相同，所以指纹可直接跨源比对。
    """
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
        """INSERT INTO sessions(key,source,sid,title,cwd,archived,created_at,updated_at,
                                msg_count,dedup_hash,native_status,last_active,tail_closed)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            s.key, s.source, s.sid, s.title, s.cwd, int(s.archived),
            s.created_at, s.updated_at,
            sum(1 for m in s.messages if m.is_indexable()),
            session_hash(s),
            s.native_status or "",
            s.last_active or 0,
            None if s.tail_closed is None else int(s.tail_closed),
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
        # 索引结构升级（旧库没有 tail_closed）：一次性全量重建，把每个会话的
        # 尾部状态补上。否则老会话永远是「不知道」，只能退回 120 秒窗口兜底。
        if _get_meta(con, "tail_schema") != TAIL_SCHEMA_VERSION:
            con.execute("DELETE FROM files")
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

        # 3) 去重用的现有（来源, 内容）组合
        # 必须带上 source：同一段对话被迁移到另一端 App 后内容完全相同，
        # 只按内容去重会把迁移出来的副本整条丢掉（全量构建也是按 source 分的）。
        existing_hashes = {
            (r["source"], r["dedup_hash"]) for r in con.execute("SELECT source, dedup_hash FROM sessions")
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
            fp = (s.source, session_hash(s))
            if fp in existing_hashes:
                # 内容与其他会话重复：跳过入库，但记录文件指纹，避免反复重解析
                dup_key = con.execute(
                    "SELECT key FROM sessions WHERE dedup_hash=? AND source=? LIMIT 1", (fp[1], fp[0])
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
        fp = (s.source, session_hash(s))
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
            fp = (s.source, session_hash(s))
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
    _set_meta(con, "tail_schema", TAIL_SCHEMA_VERSION)
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
        busy = in_progress_keys(con)
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
                "in_progress": meta["key"] in busy,
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
        busy = session_key in in_progress_keys(con)
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
            "in_progress": busy,
            "native_status": meta["native_status"] or "",
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
        busy = in_progress_keys(con)
        out = []
        for r in rows:
            d = dict(r)
            d["cwd"] = _visible_cwd(d["cwd"])
            d["archived"] = bool(d["archived"])
            d["in_progress"] = d["key"] in busy
            d.pop("native_status", None)
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


def _wb_variant_ids(con: sqlite3.Connection, sids: list[str]) -> list[str]:
    """把一条会话在 WorkBuddy 里的所有「同内容副本」展开出来。

    WorkBuddy 按 user_id 给同一段对话各存一行 + 一份 jsonl（换登录态后旧行仍留着），
    侧边栏只列当前 user_id 的行。只改其中一份的话，App 里看的那份没变，
    用户就会遇到「点了取消归档但应用里还是没有」。这里按 cwd + created_at + 标题
    认作同一会话，操作时整组一起改。
    """
    base = [s for s in (sids or []) if s]
    if not base:
        return []
    marks = ",".join("?" * len(base))
    keys = con.execute(
        f"SELECT DISTINCT cwd, created_at, coalesce(custom_title, title, '') AS t FROM sessions WHERE id IN ({marks})",
        base,
    ).fetchall()
    out: set[str] = set(base)
    for k in keys:
        for r in con.execute(
            "SELECT id FROM sessions WHERE cwd=? AND created_at=? AND coalesce(custom_title, title, '')=?",
            (k["cwd"], k["created_at"], k["t"]),
        ):
            out.add(r["id"])
    return sorted(out)


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
        if row["deleted_at"] is not None:
            return {"ok": False, "error": "该会话在 WorkBuddy 中已被删除，未改动"}

        ids = _wb_variant_ids(con, [sid])
        marks = ",".join("?" * len(ids))
        pending = [
            r["id"]
            for r in con.execute(
                f"SELECT id FROM sessions WHERE id IN ({marks}) "
                "AND status='archived' AND deleted_at IS NULL",
                ids,
            )
        ]
        if not pending:
            return {"ok": True, "changed": False, "native_status": row["status"]}

        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"workbuddy-{_backup_stamp()}.db"
        _backup_sqlite(db_path, backup)

        now_ms = int(time.time() * 1000)
        pmarks = ",".join("?" * len(pending))
        # last_activity_at 一起刷新：WorkBuddy 侧边栏「最近」按它排序且只显示少数几条，
        # 只改 status 的话会话虽然回到未归档，仍会沉在几百条之后，用户看不到。
        cur = con.execute(
            f"UPDATE sessions SET status='active', updated_at=?, last_activity_at=? "
            f"WHERE id IN ({pmarks}) AND status='archived' AND deleted_at IS NULL",
            [now_ms, now_ms, *pending],
        )
        con.commit()
        if cur.rowcount != len(pending):
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
            "variants": len(pending),
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
        # 优先在 DSH 自己的界面里点「取消归档」：只有走它自己的 RPC，host 进程才会
        # 同步内存状态并广播给界面，效果是立刻可见。直接改 workspace.json 做不到这点
        # （host 不重读文件，界面也不监听），只能等重启。
        ui = unarchive_dsh_via_ui(sid)
        if ui.get("ok"):
            result = {
                "ok": True,
                "changed": bool(ui.get("changed")),
                "native_status": "active",
                "ui_attempt": {k: ui.get(k) for k in ("ok", "changed", "error", "port")},
            }
            hint = (
                "已在 DeepSeek Harness 里取消归档，界面已同步，无需重启。"
                if ui.get("changed") else
                "DSH 中已是未归档状态，已刷新本地索引。"
            )
        else:
            # 界面不可用（DSH 没开 / 没带调试端口）→ 退化为直接改文件，需重启才可见
            result = _unarchive_dsh(sid)
            result["ui_attempt"] = {k: ui.get(k) for k in ("ok", "changed", "error", "port")}
            hint = (
                "已从 DSH 归档列表移除，但 DSH 界面暂时连不上（"
                f"{ui.get('error') or '未知原因'}），重启 DeepSeek Harness 后可见。"
                if result.get("changed") else
                "DSH 中已是未归档状态，已刷新本地索引。"
            )
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


# ---------------------------------------------------------------------------
# 批量归档 / 取消归档
# ---------------------------------------------------------------------------
# 单条操作走上面的 *_session()，每次都开一次备份 + 一次界面重载；
# 批量时按来源合并：WorkBuddy 一次备份 + 一个事务，DSH 一次原子写，
# 全部完成后只重载一次界面、只增量同步一次索引。

def _archive_workbuddy_many(
    sids: list[str],
    db_path: Path = WORKBUDDY_DB,
    backup_dir: Path = _BACKUP_DIR,
    archived: bool = True,
) -> dict:
    """批量改 WorkBuddy 会话状态（archived=True 归档，False → active）。"""
    uniq = sorted({s for s in (sids or []) if s})
    if not uniq:
        return {"ok": True, "changed": [], "skipped": [], "backup": None}
    con = sqlite3.connect(f"file:{db_path}", uri=True, timeout=10)
    backup: Path | None = None
    try:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=8000")
        marks = ",".join("?" * len(uniq))
        rows = con.execute(
            f"SELECT id, cwd, created_at, coalesce(custom_title, title, '') AS t, status, deleted_at "
            f"FROM sessions WHERE id IN ({marks})",
            uniq,
        ).fetchall()
        found = {r["id"]: r for r in rows}

        # 同一会话可能按 user_id 存了多份（换登录态后旧行仍留着），侧边栏只列当前
        # user_id 那份，所以整组一起改，否则用户会觉得「点了没反应」。
        group_ids: dict[tuple, list[str]] = {}
        for key in {(r["cwd"], r["created_at"], r["t"]) for r in rows}:
            group_ids[key] = [
                x["id"]
                for x in con.execute(
                    "SELECT id FROM sessions WHERE cwd=? AND created_at=? AND coalesce(custom_title, title, '')=?",
                    key,
                )
            ]
        flat = sorted({i for ids in group_ids.values() for i in ids})
        fmarks = ",".join("?" * len(flat))
        meta = {
            r["id"]: r
            for r in con.execute(
                f"SELECT id, status, deleted_at FROM sessions WHERE id IN ({fmarks})", flat
            )
        } if flat else {}

        changed: list[str] = []
        skipped: list[dict] = []
        targets: set[str] = set()
        for sid in uniq:
            row = found.get(sid)
            if row is None:
                skipped.append({"sid": sid, "reason": "WorkBuddy 数据库中找不到该会话"})
                continue
            if row["deleted_at"] is not None:
                skipped.append({"sid": sid, "reason": "会话已在 WorkBuddy 中被删除"})
                continue
            members = [
                meta[i]
                for i in group_ids.get((row["cwd"], row["created_at"], row["t"]), [sid])
                if i in meta and meta[i]["deleted_at"] is None
            ]
            if archived:
                todo = [m["id"] for m in members if (m["status"] or "").lower() != "archived"]
            else:
                todo = [m["id"] for m in members if (m["status"] or "").lower() == "archived"]
            if not todo:
                skipped.append({
                    "sid": sid,
                    "reason": "已是归档状态" if archived else "已是未归档状态",
                })
                continue
            targets.update(todo)
            changed.append(sid)

        if targets:
            targets = sorted(targets)
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup = backup_dir / f"workbuddy-{_backup_stamp()}.db"
            _backup_sqlite(db_path, backup)
            now_ms = int(time.time() * 1000)
            tmarks = ",".join("?" * len(targets))
            if archived:
                cur = con.execute(
                    f"UPDATE sessions SET status='archived', updated_at=? "
                    f"WHERE id IN ({tmarks}) AND deleted_at IS NULL",
                    [now_ms, *targets],
                )
            else:
                # 与 WorkBuddy 自己的 setArchived(false) 一致：回到 active 并刷新
                # last_activity_at，否则会话回到未归档但埋在几百条之后。
                cur = con.execute(
                    f"UPDATE sessions SET status='active', updated_at=?, last_activity_at=? "
                    f"WHERE id IN ({tmarks}) AND status='archived' AND deleted_at IS NULL",
                    [now_ms, now_ms, *targets],
                )
            con.commit()
            if cur.rowcount != len(targets):
                return {
                    "ok": False,
                    "error": f"写入条数不符（期望 {len(targets)}，实际 {cur.rowcount}），"
                             "状态可能已被其它进程修改",
                    "backup": str(backup) if backup else None,
                }
        return {"ok": True, "changed": changed, "skipped": skipped,
                "backup": str(backup) if backup else None}
    except Exception as e:  # noqa: BLE001
        con.rollback()
        return {"ok": False, "error": f"写入 WorkBuddy 失败：{e}",
                "backup": str(backup) if backup else None}
    finally:
        con.close()


def _set_archived_dsh_many(
    sids: list[str],
    workspace_path: Path = DSH_WORKSPACE_JSON,
    backup_dir: Path = _BACKUP_DIR,
    archived: bool = True,
) -> dict:
    """批量改 DSH 归档集合（一次读、一次原子写）。"""
    norms = sorted({_normalize_dsh_sid((s or "").strip()) for s in (sids or []) if s})
    norms = [n for n in norms if n]
    if not norms:
        return {"ok": True, "changed": [], "skipped": [], "backup": None}
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

    current = {_normalize_dsh_sid(str(x)) for x in ids}
    changed: list[str] = []
    skipped: list[dict] = []
    for norm in norms:
        if archived and norm in current:
            skipped.append({"sid": norm, "reason": "已是归档状态"})
        elif not archived and norm not in current:
            skipped.append({"sid": norm, "reason": "已是未归档状态"})
        else:
            changed.append(norm)
    if not changed:
        return {"ok": True, "changed": [], "skipped": skipped, "backup": None}

    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"dsh-workspace-{_backup_stamp()}.json"
    shutil.copy2(workspace_path, backup)

    if archived:
        # DSH 自己写的是带前缀的 "session-<uuid>"，保持同一写法
        kept = [x for x in ids if _normalize_dsh_sid(str(x)) not in set(changed)]
        global_obj["archivedSessionIds"] = kept + [f"session-{n}" for n in changed]
    else:
        drop = set(changed)
        global_obj["archivedSessionIds"] = [
            x for x in ids if _normalize_dsh_sid(str(x)) not in drop
        ]

    tmp = workspace_path.with_name(f"{workspace_path.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(tmp, workspace_path)
    except OSError as e:
        return {"ok": False, "error": f"写入 DSH workspace.json 失败：{e}",
                "backup": str(backup)}
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return {"ok": True, "changed": changed, "skipped": skipped, "backup": str(backup)}


def session_rows(keys: list[str]) -> dict[str, sqlite3.Row]:
    """按 key 批量取会话元数据（key/source/sid/title/archived）。"""
    keys = [k for k in (keys or []) if k]
    if not keys:
        return {}
    con = _connect()
    try:
        _prepare(con)
        marks = ",".join("?" * len(keys))
        rows = con.execute(
            f"SELECT key,source,sid,title,archived FROM sessions WHERE key IN ({marks})",
            list(keys),
        ).fetchall()
        return {r["key"]: r for r in rows}
    finally:
        con.close()


def in_progress_set(keys: list[str] | None = None) -> set[str]:
    """当前进行中的会话 key 集合（可选只取指定 key 的交集）。"""
    con = _connect()
    try:
        _prepare(con)
        busy = in_progress_keys(con)
    finally:
        con.close()
    return busy & set(keys) if keys else busy


def set_archived(keys: list[str], archived: bool = True) -> dict:
    """批量归档 / 取消归档，返回逐条结果。

    - 进行中的会话（正在被 App 写入）拒绝操作，记入 skipped；
    - 按来源合并写入：WorkBuddy 一次备份 + 单事务，DSH 一次原子写；
    - 完成后重载 WorkBuddy 界面、增量同步索引各一次。
    """
    keys = [k for k in dict.fromkeys(keys or []) if k]
    if not keys:
        return {"ok": False, "error": "没有选中会话"}
    rows = session_rows(keys)

    con = _connect()
    try:
        _prepare(con)
        busy = in_progress_keys(con)
    finally:
        con.close()

    actions = "归档" if archived else "取消归档"
    changed: list[str] = []
    skipped: list[dict] = []
    backups: list[str] = []
    errors: list[str] = []

    wb_sids: list[str] = []
    wb_keys: dict[str, str] = {}
    dsh_sids: list[str] = []
    dsh_keys: dict[str, str] = {}
    for key in keys:
        row = rows.get(key)
        if row is None:
            skipped.append({"key": key, "reason": "会话不存在"})
            continue
        if archived and key in busy:
            skipped.append({"key": key, "reason": "会话进行中，不可归档"})
            continue
        sid = row["sid"] or ""
        if row["source"] == "workbuddy":
            real = sid.split("/")[-1]
            wb_sids.append(real)
            wb_keys[real] = key
        elif row["source"] == "dsh":
            dsh_sids.append(sid)
            dsh_keys[_normalize_dsh_sid(sid)] = key
        else:
            skipped.append({"key": key, "reason": f"不支持的来源：{row['source']}"})

    if wb_sids:
        r = _archive_workbuddy_many(wb_sids, archived=archived)
        if not r.get("ok"):
            errors.append(r.get("error") or "WorkBuddy 写入失败")
        else:
            changed += [wb_keys[s] for s in r.get("changed", []) if s in wb_keys]
            for s in r.get("skipped", []):
                k = wb_keys.get(s.get("sid", ""))
                if k:
                    skipped.append({"key": k, "reason": s.get("reason") or "跳过"})
            if r.get("backup"):
                backups.append(r["backup"])
    if dsh_sids:
        r = _set_archived_dsh_many(dsh_sids, archived=archived)
        if not r.get("ok"):
            errors.append(r.get("error") or "DSH 写入失败")
        else:
            changed += [
                dsh_keys[n] for n in r.get("changed", []) if n in dsh_keys
            ]
            for s in r.get("skipped", []):
                k = dsh_keys.get(s.get("sid", ""))
                if k:
                    skipped.append({"key": k, "reason": s.get("reason") or "跳过"})
            if r.get("backup"):
                backups.append(r["backup"])

    ui_refresh: dict = {}
    if changed:
        wb_touched = any(rows[k]["source"] == "workbuddy" for k in changed if k in rows)
        if wb_touched:
            try:
                ui_refresh = reload_workbuddy()
            except Exception as e:  # noqa: BLE001
                ui_refresh = {"ok": False, "error": str(e)}
        try:
            sync_index()
        except Exception as e:  # noqa: BLE001
            errors.append(f"索引刷新失败：{e}")

    dsh_touched = any(rows[k]["source"] == "dsh" for k in changed if k in rows)
    return {
        "ok": not errors,
        "action": "archive" if archived else "unarchive",
        "changed": changed,
        "skipped": skipped,
        "backups": backups,
        "ui_refresh": ui_refresh,
        "error": "；".join(errors) if errors else None,
        "message": _archive_message(actions, changed, skipped, dsh_touched, errors),
    }


def _archive_message(
    action: str, changed: list[str], skipped: list[dict],
    dsh_touched: bool, errors: list[str],
) -> str:
    parts = [f"已{action} {len(changed)} 条会话"]
    if skipped:
        parts.append(f"跳过 {len(skipped)} 条（{skipped[0].get('reason')}）")
    if dsh_touched:
        parts.append("DSH 侧需重启应用才会显示最新归档状态")
    if errors:
        parts.append("；".join(errors))
    return "，".join(parts) + "。"


# ---------------------------------------------------------------------------
# 跨 App 同步登记（幂等）
# ---------------------------------------------------------------------------

def get_migration(source_key: str, target_source: str) -> dict | None:
    """取某条源会话同步到某一端的登记记录。"""
    con = _connect()
    try:
        _prepare(con)
        row = con.execute(
            "SELECT source_key,target_source,content_hash,target_sid,updated_at "
            "FROM migrations WHERE source_key=? AND target_source=?",
            (source_key, target_source),
        ).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def record_migration(
    source_key: str, target_source: str, content_hash: str, target_sid: str
) -> None:
    """写入/更新同步登记。target_sid 用「索引里的 sid 形式」（WB 含项目 slug）。"""
    con = _connect()
    try:
        _prepare(con)
        con.execute(
            "INSERT INTO migrations(source_key,target_source,content_hash,target_sid,updated_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(source_key,target_source) DO UPDATE SET "
            "content_hash=excluded.content_hash, target_sid=excluded.target_sid, "
            "updated_at=excluded.updated_at",
            (source_key, target_source, content_hash, target_sid, int(time.time() * 1000)),
        )
        con.commit()
    finally:
        con.close()


def forget_migration(source_key: str, target_source: str) -> None:
    con = _connect()
    try:
        _prepare(con)
        con.execute(
            "DELETE FROM migrations WHERE source_key=? AND target_source=?",
            (source_key, target_source),
        )
        con.commit()
    finally:
        con.close()


def find_same_content(target_source: str, content_hash: str) -> dict | None:
    """在索引里找目标端同内容的会话（兜底：v0.3.0 及以前同步出来的副本没有登记）。"""
    con = _connect()
    try:
        _prepare(con)
        row = con.execute(
            "SELECT key,sid,source FROM sessions WHERE source=? AND dedup_hash=? LIMIT 1",
            (target_source, content_hash),
        ).fetchone()
        return dict(row) if row else None
    finally:
        con.close()
