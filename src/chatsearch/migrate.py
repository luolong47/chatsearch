"""跨 App 会话迁移：WorkBuddy <-> DeepSeek Harness。

两端存储完全不同：
- WorkBuddy：``~/.workbuddy/projects/<slug>/<uuid>.jsonl`` + ``workbuddy.db`` 的 sessions 行
- DSH：``~/.dsh/sessions/<slug>/session-<uuid>/session.v4.jsonl.zstd``
  + ``~/.dsh/storages/workspace.json`` 的 ``tables.workspaces.<id>.sessionIds``

迁移只搬运**文本轮次**（user / assistant 正文），不搬工具调用、推理、附件 ——
这些在另一端没有对应物，硬搬只会让目标 App 状态错乱。

写入前一律备份（备份落在 ``data/backups``），失败不留下半个会话。
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import time
import uuid
from pathlib import Path

import zstandard as zstd

from . import parsers
from .config import (
    DATA_DIR,
    DSH_SESSIONS,
    DSH_WORKSPACE_JSON,
    SOURCE_DSH,
    SOURCE_WORKBUDDY,
    WORKBUDDY_DB,
    WORKBUDDY_PROJECTS,
)
from .indexer import (
    find_same_content,
    get_migration,
    record_migration,
    session_hash,
)
from .models import Message, Session

BACKUP_DIR = DATA_DIR / "backups"
BACKUP_KEEP = 20               # 每类备份最多保留份数，防止批量同步把磁盘写满
DSH_TITLE_PREFIX = ""          # 标题保持原样，便于在目标 App 里一眼认出
MARK_SUFFIX = ""               # 需要时可加「（同步自 …）」

# WorkBuddy 用户消息的正文放在 <user_query> 里，外层是注入上下文；
# 只写标签内的正文，两端（原生 App 与本工具解析器）都只认这一段。
_WB_USER_TEMPLATE = "<user_query>{text}</user_query>"


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------

def _now_ms() -> int:
    return int(time.time() * 1000)


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _prune_backups(tag: str, keep: int = BACKUP_KEEP) -> None:
    """同类备份只保留最近 keep 份。批量同步每条都备份会把磁盘写满。"""
    try:
        files = sorted(
            (p for p in BACKUP_DIR.glob(f"{tag}-*") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    for p in files[keep:]:
        try:
            p.unlink()
        except OSError:
            pass


def _backup_file(src: Path, tag: str) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dst = BACKUP_DIR / f"{tag}-{_stamp()}{src.suffix}"
    shutil.copy2(src, dst)
    _prune_backups(tag)
    return dst


def _backup_sqlite(src: Path, tag: str) -> Path:
    """用 SQLite 在线备份 API，避免 WAL 库拿到不一致快照。"""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dst = BACKUP_DIR / f"{tag}-{_stamp()}.db"
    src_con = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=10)
    dst_con = sqlite3.connect(str(dst))
    try:
        src_con.backup(dst_con)
    finally:
        dst_con.close()
        src_con.close()
    _prune_backups(tag)
    return dst


def _wb_real_cwd(db_sid: str) -> str:
    """WorkBuddy 解析出的 cwd 是目录 slug，真实路径存在 sessions.cwd 里。"""
    if not WORKBUDDY_DB.is_file() or not db_sid:
        return ""
    try:
        con = sqlite3.connect(f"file:{WORKBUDDY_DB}?mode=ro", uri=True, timeout=5)
        row = con.execute("SELECT cwd FROM sessions WHERE id=?", (db_sid,)).fetchone()
        con.close()
        return (row[0] or "") if row else ""
    except sqlite3.Error:
        return ""


def load_session(source: str, sid: str) -> Session | None:
    """按来源 + sid 重新解析出会话（迁移走原生文件，不依赖索引缓存）。"""
    sid = (sid or "").strip()
    if not sid:
        return None
    if source == SOURCE_WORKBUDDY:
        path = WORKBUDDY_PROJECTS / f"{sid}.jsonl"
        if not path.is_file():
            return None
        sess = parsers.parse_workbuddy_file(path)
        if sess is not None:
            real = _wb_real_cwd(path.stem)
            if real:
                sess.cwd = real
        return sess
    if source == SOURCE_DSH:
        for path in DSH_SESSIONS.rglob(f"session-{sid}/session.v4.jsonl.zstd"):
            return parsers.parse_dsh_file(path)
        return None
    return None


def _slug(cwd: str) -> str:
    """把工作目录拍成目录名：去掉盘符冒号，分隔符统一成 '-'。"""
    return cwd.replace(":", "").replace("\\", "-").replace("/", "-")


# ---------------------------------------------------------------------------
# DSH 侧写入
# ---------------------------------------------------------------------------

def _dsh_workspace(ws: dict) -> tuple[str, dict]:
    """取默认工作区（只写它，避免把会话塞进不存在的工作区）。"""
    tables = ws.get("tables", {}).get("workspaces", {})
    default_id = ws.get("global", {}).get("defaultWorkspaceId")
    if default_id and default_id in tables:
        return default_id, tables[default_id]
    if tables:
        wid = next(iter(tables))
        return wid, tables[wid]
    raise RuntimeError("workspace.json 里没有任何工作区，DSH 可能还没初始化")


def _dsh_events(sess: Session, sid: str) -> list[dict]:
    """按 DSH 的事件模型拼一份完整会话日志。

    实测事件顺序（0.2.2-rc.3）：session → 权限/沙箱/审批 → session/title →
    每轮 turn/start → user/message → step/start → assistant/message → step/end → turn/end。
    seq 必须全程单调递增，DSH 回放时会校验。
    """
    created = sess.created_at or _now_ms()
    cwd = sess.cwd or str(Path.home())
    seq = 0

    def ev(typ: str, ts: int, data: dict) -> dict:
        nonlocal seq
        item = {"type": typ, "seq": seq, "time": ts or _now_ms(), "data": data}
        seq += 1
        return item

    out: list[dict] = [{
        "type": "session",
        "version": 4,
        "id": sid,
        "createdAt": created,
        "cwd": cwd,
        "isSeeded": False,
        "delegationDepth": 0,
        "agentPreset": "standard",
    }]
    out.append(ev("permission/preset", created, {"preset": "workspace-write"}))
    out.append(ev("sandbox/mode", created, {"mode": "workspace-write"}))
    out.append(ev("approval/policy", created, {"policy": "ask"}))
    out.append(ev("session/title", created, {
        "title": (sess.title or "迁移会话") + MARK_SUFFIX,
        "messageSeqs": [],
        "source": {"kind": "fallback"},
    }))

    # 成对切成轮次：一条 user 开启一轮，随后的 assistant 归入该轮
    turns: list[list[Message]] = []
    for m in sess.messages:
        if m.role == "user" or not turns:
            turns.append([m])
        else:
            turns[-1].append(m)

    for idx, group in enumerate(turns, start=1):
        user_msg = next((m for m in group if m.role == "user"), None)
        asst_msg = next((m for m in group if m.role == "assistant"), None)
        ts0 = (user_msg.ts if user_msg else (asst_msg.ts if asst_msg else created)) or created
        out.append(ev("turn/start", ts0, {"turn": idx}))
        if user_msg and user_msg.text.strip():
            out.append(ev("user/message", user_msg.ts or ts0, {
                "content": [{"type": "text", "text": user_msg.text}],
                "source": {"kind": "user", "rpcId": _new_uuid()},
                "role": "user",
                "id": _new_uuid(),
            }))
            out[-1]["surfaceOp"] = "append"
        out.append(ev("step/start", ts0, {"turn": idx, "step": 1}))
        if asst_msg and asst_msg.text.strip():
            out.append(ev("assistant/message", asst_msg.ts or ts0, {
                "turn": idx,
                "step": 1,
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": asst_msg.text}],
                    "source": {"kind": "model"},
                    "id": _new_uuid(),
                },
                "stream": [],
            }))
        out.append(ev("step/end", ts0, {"turn": idx, "step": 1}))
        out.append(ev("turn/end", ts0, {"turn": idx, "reason": {"kind": "completed"}}))
    return out


def export_to_dsh(sess: Session, backup: bool = True) -> dict:
    """把一条会话写成 DSH 原生会话，并登记到 workspace.json。"""
    if not DSH_WORKSPACE_JSON.is_file():
        return {"ok": False, "error": "找不到 DSH workspace.json，DSH 可能未安装或未初始化"}

    ws = json.loads(DSH_WORKSPACE_JSON.read_text(encoding="utf-8"))
    try:
        ws_id, workspace = _dsh_workspace(ws)
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}

    cwd = sess.cwd or workspace.get("path") or str(Path.home())
    sid = f"session-{_new_uuid()}"
    target_dir = DSH_SESSIONS / f"--{_slug(cwd)}--" / sid
    payload = "\n".join(json.dumps(e, ensure_ascii=False) for e in _dsh_events(sess, sid)) + "\n"

    backup_path = _backup_file(DSH_WORKSPACE_JSON, "workspace") if backup else None
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        f = target_dir / "session.v4.jsonl.zstd"
        f.write_bytes(zstd.ZstdCompressor(level=10).compress(payload.encode("utf-8")))

        session_ids = workspace.setdefault("sessionIds", [])
        if sid not in session_ids:
            session_ids.append(sid)
        # 归档状态跟着会话走：源端已归档，目标端也归到「已归档」
        arch = ws.setdefault("global", {}).setdefault("archivedSessionIds", [])
        if sess.archived:
            if sid not in arch:
                arch.append(sid)
        else:
            while sid in arch:
                arch.remove(sid)
        workspace["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S.") + f"{_now_ms() % 1000:03d}Z"
        DSH_WORKSPACE_JSON.write_text(
            json.dumps(ws, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as e:
        # 回滚：删掉刚写进去的目录，避免 DSH 列表里出现打不开的空会话
        shutil.rmtree(target_dir, ignore_errors=True)
        if backup_path:
            try:
                shutil.copy2(backup_path, DSH_WORKSPACE_JSON)
            except OSError:
                pass
        return {"ok": False, "error": f"写入失败：{e}"}

    return {
        "ok": True,
        "target": SOURCE_DSH,
        "target_sid": sid.replace("session-", ""),
        "path": str(target_dir),
        "turns": sum(1 for m in sess.messages if m.role == "user"),
        "messages": len(sess.messages),
        "archived": bool(sess.archived),
        "backup": str(backup_path) if backup_path else "",
        "workspace": ws_id,
    }


# ---------------------------------------------------------------------------
# WorkBuddy 侧写入
# ---------------------------------------------------------------------------

def _wb_user_id(con: sqlite3.Connection) -> str:
    """沿用最近活跃会话的 user_id，避免新会话因未知用户而不显示。"""
    row = con.execute(
        "SELECT user_id FROM sessions WHERE deleted_at IS NULL "
        "ORDER BY COALESCE(last_activity_at, updated_at) DESC LIMIT 1"
    ).fetchone()
    if row and row[0]:
        return row[0]
    row = con.execute("SELECT user_id, COUNT(*) c FROM sessions GROUP BY user_id ORDER BY c DESC LIMIT 1").fetchone()
    return row[0] if row else ""


def _wb_lines(sess: Session, sid: str, cwd: str) -> list[dict]:
    now = _now_ms()
    created = sess.created_at or now
    lines: list[dict] = [{
        "type": "session-meta",
        "id": _new_uuid(),
        "sessionId": sid,
        "timestamp": created,
        "meta": {"codebuddy.ai/hostKind": "unopted"},
    }]
    for m in sess.messages:
        if not m.text.strip():
            continue
        if m.role == "user":
            content = [{"type": "input_text", "text": _WB_USER_TEMPLATE.format(text=m.text)}]
        else:
            content = [{"providerData": {"annotations": []}, "type": "output_text", "text": m.text}]
        lines.append({
            "id": _new_uuid(),
            "timestamp": m.ts or created,
            "type": "message",
            "role": m.role,
            "content": content,
        })
    lines.append({
        "id": _new_uuid(),
        "timestamp": sess.updated_at or now,
        "type": "ai-title",
        "aiTitle": (sess.title or "迁移会话") + MARK_SUFFIX,
        "sessionId": sid,
        "cwd": cwd,
    })
    return lines


def export_to_workbuddy(sess: Session, reload_ui: bool = True, backup: bool = True) -> dict:
    """把一条会话写成 WorkBuddy 原生 jsonl，并在 workbuddy.db 里登记。"""
    if not WORKBUDDY_DB.is_file():
        return {"ok": False, "error": "找不到 WorkBuddy 数据库，WorkBuddy 可能未安装"}

    cwd = sess.cwd or str(Path.home())
    sid = _new_uuid()
    proj = WORKBUDDY_PROJECTS / _slug(cwd)
    jsonl = proj / f"{sid}.jsonl"
    now = _now_ms()
    created = sess.created_at or now
    # 归档状态跟着会话走：源端已归档，目标端也归到「已归档」
    status = "archived" if sess.archived else "completed"

    backup_path = _backup_sqlite(WORKBUDDY_DB, "workbuddy") if backup else None
    created_file = False
    con = sqlite3.connect(f"file:{WORKBUDDY_DB}", uri=True, timeout=10)
    try:
        con.execute("PRAGMA busy_timeout=8000")
        user_id = _wb_user_id(con)
        if not user_id:
            return {"ok": False, "error": "workbuddy.db 里没有可用的 user_id"}

        proj.mkdir(parents=True, exist_ok=True)
        text = "\n".join(json.dumps(x, ensure_ascii=False) for x in _wb_lines(sess, sid, cwd)) + "\n"
        jsonl.write_text(text, encoding="utf-8")
        created_file = True

        con.execute(
            "INSERT INTO sessions (id, cwd, user_id, title, custom_title, status, "
            "created_at, updated_at, last_activity_at, is_playground, source_mode, mode, "
            "transport, permission_mode, unread) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                sid, cwd, user_id, (sess.title or "迁移会话") + MARK_SUFFIX, None,
                status, created, now, now, 0, "craft", "craft",
                "local", "fullAccess", 0,
            ),
        )
        con.commit()
    except Exception as e:  # noqa: BLE001 - 任何失败都回滚到干净状态
        con.rollback()
        con.close()
        if created_file:
            jsonl.unlink(missing_ok=True)
        return {"ok": False, "error": f"写入失败：{e}"}
    finally:
        try:
            con.close()
        except Exception:  # noqa: BLE001
            pass

    refresh = {"ok": False, "error": "跳过"}
    if reload_ui:
        try:
            from .cdp import reload_workbuddy
            refresh = reload_workbuddy()
        except Exception as e:  # noqa: BLE001
            refresh = {"ok": False, "error": str(e)}

    return {
        "ok": True,
        "target": SOURCE_WORKBUDDY,
        "target_sid": sid,
        "path": str(jsonl),
        "messages": len(sess.messages),
        "cwd": cwd,
        "archived": bool(sess.archived),
        "backup": str(backup_path) if backup_path else "",
        "ui_refresh": refresh,
    }


# ---------------------------------------------------------------------------
# 删除同步出去的副本（仅「强制覆盖」时使用）
# ---------------------------------------------------------------------------

def _strip_dsh_prefix(sid: str) -> str:
    return sid[len("session-"):] if sid.startswith("session-") else sid


def _wb_jsonl_paths(sid: str) -> list[Path]:
    sid = (sid or "").strip()
    if not sid:
        return []
    direct = WORKBUDDY_PROJECTS / f"{sid}.jsonl"
    if direct.is_file():
        return [direct]
    stem = sid.split("/")[-1]
    return [p for p in WORKBUDDY_PROJECTS.rglob(f"{stem}.jsonl") if p.is_file()]


def _remove_wb_copy(sid: str) -> dict:
    paths = _wb_jsonl_paths(sid)
    if not paths:
        return {"ok": False, "error": "目标端找不到该会话文件"}
    backup = _backup_sqlite(WORKBUDDY_DB, "workbuddy")
    removed: list[str] = []
    for p in paths:
        try:
            p.unlink()
            removed.append(str(p))
        except OSError as e:
            return {"ok": False, "error": f"删除失败：{e}", "removed": removed}
    try:
        con = sqlite3.connect(f"file:{WORKBUDDY_DB}", uri=True, timeout=10)
        con.execute("PRAGMA busy_timeout=8000")
        con.execute("DELETE FROM sessions WHERE id=?", (paths[0].stem,))
        con.commit()
        con.close()
    except sqlite3.Error as e:
        return {"ok": False, "error": f"清理数据库失败：{e}", "removed": removed}
    return {"ok": True, "removed": removed, "backup": str(backup)}


def _remove_dsh_copy(sid: str) -> dict:
    short = _strip_dsh_prefix((sid or "").strip())
    if not short:
        return {"ok": False, "error": "sid 为空"}
    if not DSH_WORKSPACE_JSON.is_file():
        return {"ok": False, "error": "找不到 DSH workspace.json"}
    full = f"session-{short}"
    dirs = [d for d in DSH_SESSIONS.rglob(full) if d.is_dir()]
    if not dirs:
        return {"ok": False, "error": "目标端找不到该会话目录"}

    backup = _backup_file(DSH_WORKSPACE_JSON, "workspace")
    for d in dirs:
        shutil.rmtree(d, ignore_errors=True)
    try:
        ws = json.loads(DSH_WORKSPACE_JSON.read_text(encoding="utf-8"))
        for w in (ws.get("tables", {}).get("workspaces", {}) or {}).values():
            ids = w.get("sessionIds") or []
            while full in ids:
                ids.remove(full)
        arch = (ws.get("global") or {}).get("archivedSessionIds") or []
        while full in arch:
            arch.remove(full)
        DSH_WORKSPACE_JSON.write_text(
            json.dumps(ws, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except (OSError, json.JSONDecodeError) as e:
        return {"ok": False, "error": f"清理 workspace.json 失败：{e}"}
    return {"ok": True, "removed": [str(d) for d in dirs], "backup": str(backup)}


def remove_target_copy(target: str, target_sid: str) -> dict:
    """删除由本工具同步出去的副本（只认登记过的 sid，不动用户原生会话）。"""
    if target == SOURCE_WORKBUDDY:
        return _remove_wb_copy(target_sid)
    if target == SOURCE_DSH:
        return _remove_dsh_copy(target_sid)
    return {"ok": False, "error": f"未知目标：{target}"}


def backup_snapshot() -> dict:
    """批量同步前统一备份一次（避免每条各备一份把磁盘写满）。"""
    out: dict[str, str] = {}
    try:
        if DSH_WORKSPACE_JSON.is_file():
            out["workspace"] = str(_backup_file(DSH_WORKSPACE_JSON, "workspace"))
    except OSError as e:
        out["workspace_error"] = str(e)
    try:
        if WORKBUDDY_DB.is_file():
            out["workbuddy"] = str(_backup_sqlite(WORKBUDDY_DB, "workbuddy"))
    except (OSError, sqlite3.Error) as e:
        out["workbuddy_error"] = str(e)
    return out


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

def _label(source: str) -> str:
    return "WorkBuddy" if source == SOURCE_WORKBUDDY else "DeepSeek Harness"


def _target_index_sid(target: str, path: str) -> str:
    """把写出的文件路径转成「索引里的 sid 形式」（WB 含项目 slug）。"""
    p = Path(path)
    if target == SOURCE_WORKBUDDY:
        return parsers.workbuddy_sid(p)
    return _strip_dsh_prefix(p.name)


def migrate(
    source: str,
    sid: str,
    target: str,
    reload_ui: bool = True,
    backup: bool = True,
    force: bool = False,
) -> dict:
    """把 source 侧的一条会话同步到 target 侧。返回结构化结果。

    reload_ui=False 用于批量迁移：每条都重载一次 WorkBuddy 界面代价太大，
    由调用方在所有会话写完后统一重载一次。
    backup=False 用于批量迁移：由调用方先 backup_snapshot() 统一备份一次。
    force=True 才有「覆盖」语义：先删掉登记过的旧副本再写新的。

    幂等：同一源会话对同一目标端只保留一份副本。
    - 内容指纹相同 → already=True，不写；
    - 源端内容已变（源会话又有新对话）→ already=True + stale=True，提示需强制覆盖。
    """
    if source not in (SOURCE_WORKBUDDY, SOURCE_DSH):
        return {"ok": False, "error": f"未知来源：{source}"}
    if target not in (SOURCE_WORKBUDDY, SOURCE_DSH):
        return {"ok": False, "error": f"未知目标：{target}"}
    if source == target:
        return {"ok": False, "error": "来源与目标相同，无需同步"}

    sess = load_session(source, sid)
    if sess is None:
        return {"ok": False, "error": "找不到该会话的原始文件"}
    if not sess.messages:
        return {"ok": False, "error": "该会话没有可迁移的消息"}

    h = session_hash(sess)

    prev = get_migration(sess.key, target)
    if prev and not force:
        if prev["content_hash"] == h:
            return {
                "ok": False, "already": True, "reason": "same",
                "target": target, "target_sid": prev["target_sid"],
                "error": "目标端已有同内容副本",
                "message": f"{_label(target)} 里已有这条会话，未重复写入。",
            }
        return {
            "ok": False, "already": True, "reason": "stale",
            "target": target, "target_sid": prev["target_sid"],
            "error": "目标端副本内容不一致",
            "message": (
                f"{_label(target)} 里的副本与当前内容不一致（源端有新对话）。"
                "需要覆盖时请用强制同步。"
            ),
        }

    # 兜底：早期版本同步出去、没有登记记录的副本，按内容指纹认一次
    if not prev and not force:
        hit = find_same_content(target, h)
        if hit:
            try:
                record_migration(sess.key, target, h, hit["sid"])
            except Exception:  # noqa: BLE001
                pass
            return {
                "ok": False, "already": True, "reason": "same",
                "target": target, "target_sid": hit["sid"],
                "error": "目标端已有同内容副本",
                "message": f"{_label(target)} 里已有这条会话，未重复写入。",
            }

    if force and prev:
        removed = remove_target_copy(target, prev["target_sid"])
        if not removed.get("ok"):
            return {
                "ok": False,
                "error": f"覆盖失败，旧副本没有删掉：{removed.get('error')}",
            }

    if target == SOURCE_DSH:
        r = export_to_dsh(sess, backup=backup)
    else:
        r = export_to_workbuddy(sess, reload_ui=reload_ui, backup=backup)

    if r.get("ok"):
        try:
            record_migration(sess.key, target, h, _target_index_sid(target, r["path"]))
        except Exception:  # noqa: BLE001 - 登记失败不影响已完成的写入
            pass
    return r
