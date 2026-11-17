"""数据解析层：WorkBuddy / DeepSeek Harness -> 统一的 Session 列表。"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Iterable

import zstandard as zstd

from .config import (
    SOURCE_DSH,
    SOURCE_WORKBUDDY,
    WORKBUDDY_DB,
    WORKBUDDY_PROJECTS,
    DSH_SESSIONS,
    DSH_WORKSPACE_JSON,
)
from .models import Message, Session

# 匹配 <user_query>...</user_query>，非贪婪、跨行
_USER_QUERY_RE = re.compile(r"<user_query>(.*?)</user_query>", re.DOTALL)
# 系统注入块，整段剔除
_SYSTEM_RE = re.compile(r"<system-reminder.*?</system-reminder>", re.DOTALL)


def _clean_text(text: str, *, user: bool) -> str:
    """清洗单条消息正文。user 消息优先取 <user_query> 内真实提问。"""
    if not text:
        return ""
    if user:
        m = _USER_QUERY_RE.search(text)
        if m:
            return m.group(1).strip()
        # 无 user_query 标签时，剔除 system-reminder 后取剩余
        text = _SYSTEM_RE.sub("", text)
        # 去掉其它常见注入头
        text = re.sub(r"<system-reminder.*?(?=</system-reminder>|$)", "", text, flags=re.DOTALL)
    return text.strip()


def _iter_jsonl(path: Path) -> Iterable[dict]:
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


# ---------------------------------------------------------------------------
# WorkBuddy
# ---------------------------------------------------------------------------

def _load_wb_meta() -> dict[str, dict]:
    """从 workbuddy.db sessions 表读取元数据：标题（custom_title > title）与归档状态。

    返回 {id: {"title": str, "archived": bool}}。archived = status=='archived'。
    DSH 会话不在此表，解析时 archived 默认 False。
    """
    meta: dict[str, dict] = {}
    if not WORKBUDDY_DB.exists():
        return meta
    try:
        con = sqlite3.connect(f"file:{WORKBUDDY_DB}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT id, title, custom_title, status FROM sessions WHERE deleted_at IS NULL"
        ).fetchall()
        con.close()
        for sid, title, custom, status in rows:
            t = (custom or title or "").strip()
            meta[sid] = {"title": t, "archived": (status == "archived")}
    except Exception:
        pass
    return meta


# 兼容旧名（若有外部调用）
_load_wb_titles = _load_wb_meta


def workbuddy_sid(path: Path) -> str:
    """WorkBuddy 会话 key：相对路径（含项目 slug）保证全局唯一。"""
    try:
        return path.relative_to(WORKBUDDY_PROJECTS).with_suffix("").as_posix()
    except ValueError:
        return path.stem


def parse_workbuddy_file(path: Path, db_meta: dict[str, dict] | None = None) -> Session | None:
    """解析单个 WorkBuddy jsonl 文件为 Session；无有效消息返回 None。"""
    db_meta = db_meta if db_meta is not None else _load_wb_meta()
    title = ""
    created = 0
    updated = 0
    sid = workbuddy_sid(path)
    db_sid = path.stem

    # WorkBuddy 的 user 消息里绝大多数是 <system-reminder> / <task-notification> /
    # <conversation_history_summary> 注入，只有 <user_query> 才是真实提问。
    # assistant 则是一问多答的流式片段（常夹杂 "Interrupted by user"）。
    # 按真实提问分轮：每轮产出 1 条 user + 1 条合并后的 assistant，
    # 与 DSH 一致，避免一轮对话被拆成 N 条、信息密度过低。
    turns: list[dict] = []
    current: dict | None = None

    def _new_turn() -> dict:
        t = {"user": [], "asst": [], "user_ts": 0, "asst_ts": 0}
        turns.append(t)
        return t

    for obj in _iter_jsonl(path):
        typ = obj.get("type")
        ts = obj.get("timestamp") or 0
        if ts:
            if not created:
                created = ts
            updated = max(updated, ts)
        if typ == "ai-title" and obj.get("aiTitle"):
            title = obj["aiTitle"].strip()
        elif typ == "message":
            role = obj.get("role")
            if role not in ("user", "assistant"):
                continue
            chunks: list[str] = []
            for c in obj.get("content", []):
                if not isinstance(c, dict):
                    continue
                ctype = c.get("type")
                if role == "user" and ctype in ("input_text", "text"):
                    chunks.append(c.get("text", ""))
                elif role == "assistant" and ctype in ("output_text", "text"):
                    chunks.append(_clean_text(c.get("text", ""), user=False))
            raw = "\n".join(chunks)

            if role == "user":
                m = _USER_QUERY_RE.search(raw)
                if not m:
                    continue  # 注入消息，不开新轮也不入库
                q = m.group(1).strip()
                if not q:
                    continue
                current = _new_turn()
                current["user"].append(q)
                current["user_ts"] = ts
            else:
                text = raw.strip()
                # WB 会把中断/占位写成 assistant 消息，不是真实回复
                if not text or text in ("Interrupted by user", "interrupted by user"):
                    continue
                if current is None:
                    current = _new_turn()  # 会话开头无 user_query 的兼容
                current["asst"].append(text)
                current["asst_ts"] = ts

    msgs: list[Message] = []
    seq = 0
    for turn in turns:
        user_text = "\n\n".join(turn["user"]).strip()
        asst_text = "\n\n".join(turn["asst"]).strip()
        if user_text:
            msgs.append(Message(role="user", text=user_text, ts=turn["user_ts"] or turn["asst_ts"], seq=seq))
            seq += 1
        if asst_text:
            msgs.append(Message(role="assistant", text=asst_text, ts=turn["asst_ts"] or turn["user_ts"], seq=seq))
            seq += 1

    if not msgs:
        return None
    entry = db_meta.get(db_sid, {})
    archived = bool(entry.get("archived", False))
    if not title:
        title = entry.get("title", "")
    if not title:
        first = next((m.text for m in msgs if m.role == "user"), "")
        title = (first[:40] + "…") if len(first) > 40 else (first or "未命名会话")
    return Session(
        source=SOURCE_WORKBUDDY,
        sid=sid,
        title=title,
        messages=msgs,
        cwd=str(path.parent.name),
        archived=archived,
        created_at=created,
        updated_at=updated,
    )


def parse_workbuddy() -> list[Session]:
    """解析 ~/.workbuddy/projects/**/*.jsonl。每个文件即一个会话。"""
    if not WORKBUDDY_PROJECTS.exists():
        return []
    db_meta = _load_wb_meta()
    out: list[Session] = []
    for path in WORKBUDDY_PROJECTS.rglob("*.jsonl"):
        s = parse_workbuddy_file(path, db_meta)
        if s:
            out.append(s)
    return out


# ---------------------------------------------------------------------------
# DeepSeek Harness
# ---------------------------------------------------------------------------

def _normalize_dsh_sid(sid: str) -> str:
    """DSH 会话 id 统一去掉 'session-' 前缀，便于跨文件比对。"""
    return sid[len("session-") :] if sid.startswith("session-") else sid


def _load_dsh_archived() -> set[str]:
    """从 ~/.dsh/storages/workspace.json 读取已归档会话 id 集合。

    DSH 的「归档会话」只改 workspace.json 的 global.archivedSessionIds，
    不动 session.v4.jsonl.zstd，所以必须单独读。返回的是去掉 session- 前缀的 uuid。
    """
    if not DSH_WORKSPACE_JSON.exists():
        return set()
    try:
        data = json.loads(DSH_WORKSPACE_JSON.read_text(encoding="utf-8"))
        ids = data.get("global", {}).get("archivedSessionIds") or []
        return {_normalize_dsh_sid(str(x)) for x in ids}
    except (OSError, json.JSONDecodeError, AttributeError):
        return set()


def _read_dsh_jsonl(path: Path) -> list[dict]:
    """zstd 解压按行解析（DSH 帧头无 content size，须用 stream_reader().read()）。"""
    try:
        with path.open("rb") as fh:
            data = zstd.ZstdDecompressor().stream_reader(fh).read()
    except (zstd.ZstdError, OSError):
        return []
    out: list[dict] = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def dsh_sid(path: Path) -> str:
    sid_dir = path.parent.name  # session-<uuid>
    return _normalize_dsh_sid(sid_dir)


def parse_dsh_file(path: Path, archived_ids: set[str] | None = None) -> Session | None:
    """解析单个 DSH session.v4.jsonl.zstd 文件为 Session。

    archived_ids 为已归档会话 id 集合（无 session- 前缀）；不传则实时读取。
    """
    sid = dsh_sid(path)
    if archived_ids is None:
        archived_ids = _load_dsh_archived()
    archived = sid in archived_ids
    title = ""
    cwd = ""
    created = 0
    updated = 0

    # DSH 是 agent：一次提问（turn）里 assistant 会分几十个 step 流式输出
    # 多段 text，原生 UI 把它们拼成一条连续回复。这里按 turn 聚合，
    # 每个 turn 只产出 1 条 user + 1 条合并后的 assistant，避免把一轮对话
    # 拆成 N 条、信息密度过低。
    turns: dict[int, dict] = {}

    def _bucket(turn: int) -> dict:
        return turns.setdefault(
            turn,
            {"user": [], "asst": [], "user_ts": 0, "asst_ts": 0,
             "user_seq": 0, "asst_seq": 0},
        )

    for obj in _read_dsh_jsonl(path):
        typ = obj.get("type")
        ts = obj.get("time") or 0
        if ts:
            if not created:
                created = ts
            updated = max(updated, ts)

        if typ == "session":
            created = obj.get("createdAt") or created
            cwd = obj.get("cwd", "")
        elif typ == "session/title":
            t = (obj.get("data", {}).get("title") or "").strip()
            if t:
                title = t
        elif typ == "user/message":
            data = obj.get("data", {})
            source = data.get("source", {}) or {}
            if source.get("kind") != "user":
                continue  # time-context / runtime-context / skill-catalog 等注入
            parts: list[str] = []
            for c in data.get("content", []):
                if isinstance(c, dict) and c.get("type") in ("text", "input_text"):
                    t = _clean_text(c.get("text", ""), user=True)
                    if t:
                        parts.append(t)
            text = "\n".join(parts).strip()
            if text:
                b = _bucket(data.get("turn") or 0)
                b["user"].append(text)
                if not b["user_ts"]:
                    b["user_ts"] = ts
                b["user_seq"] = obj.get("seq") or b["user_seq"]
        elif typ == "assistant/message":
            data = obj.get("data", {})
            parts = []
            for c in data.get("message", {}).get("content", []):
                if isinstance(c, dict) and c.get("type") in ("text", "output_text"):
                    t = _clean_text(c.get("text", ""), user=False)
                    if t:
                        parts.append(t)
            text = "\n".join(parts).strip()
            if text:
                b = _bucket(data.get("turn") or 0)
                b["asst"].append(text)  # reasoning / tool-call 已在上层被过滤
                b["asst_ts"] = ts
                b["asst_seq"] = obj.get("seq") or b["asst_seq"]

    msgs: list[Message] = []
    seq = 0
    for turn in sorted(turns):
        b = turns[turn]
        user_text = "\n\n".join(b["user"]).strip()
        asst_text = "\n\n".join(b["asst"]).strip()
        if user_text:
            msgs.append(Message(role="user", text=user_text, ts=b["user_ts"] or b["asst_ts"], seq=seq))
            seq += 1
        if asst_text:
            msgs.append(Message(role="assistant", text=asst_text, ts=b["asst_ts"] or b["user_ts"], seq=seq))
            seq += 1

    if not msgs:
        return None
    if not title:
        first = next((m.text for m in msgs if m.role == "user"), "")
        title = (first[:40] + "…") if len(first) > 40 else (first or "未命名会话")
    return Session(
        source=SOURCE_DSH,
        sid=sid,
        title=title,
        messages=msgs,
        cwd=cwd,
        archived=archived,
        created_at=created,
        updated_at=updated,
    )


def parse_dsh() -> list[Session]:
    """解析 ~/.dsh/sessions/**/session-*/session.v4.jsonl.zstd。"""
    if not DSH_SESSIONS.exists():
        return []
    archived_ids = _load_dsh_archived()
    out: list[Session] = []
    for path in DSH_SESSIONS.rglob("session.v4.jsonl.zstd"):
        s = parse_dsh_file(path, archived_ids)
        if s:
            out.append(s)
    return out


def parse_file(
    path: Path,
    db_meta: dict[str, dict] | None = None,
    dsh_archived: set[str] | None = None,
) -> Session | None:
    """按源文件路径自动分派解析器。"""
    name = path.name
    if name.endswith(".jsonl"):
        return parse_workbuddy_file(path, db_meta)
    if name == "session.v4.jsonl.zstd":
        return parse_dsh_file(path, dsh_archived)
    return None


def parse_all() -> list[Session]:
    return parse_workbuddy() + parse_dsh()
