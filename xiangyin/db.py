"""
乡音方言翻译平台 - 数据库层
============================
使用标准库 sqlite3，无需额外依赖。

三张表：
  feedbacks      - 社区反馈（原文 / 译文 / 用户修正 / 投票数 / 匿名地理位置）
  votes          - 投票去重（同一条反馈 + 同一 IP 哈希只能一票）
  culture_cache  - 风土科普缓存（按归属地缓存 LLM 生成的科普内容）

所有涉及用户 IP 的存储 **仅存 SHA256 哈希**，绝不存明文 IP。
"""

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from flask import Flask, g, current_app

from config import CONFIG


# ============================================================
# SQL
# ============================================================

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS feedbacks (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    direction        TEXT    NOT NULL,   -- dialect_to_english / english_to_dialect
    source_text      TEXT    NOT NULL,   -- 原文
    target_text      TEXT,               -- 系统给出的译文（供对照）
    origin_region    TEXT,               -- 归属地 / 目标方言名
    suggested_text   TEXT    NOT NULL,   -- 用户提交的修正/正确版本
    ip_hash          TEXT    NOT NULL,   -- 提交者 IP 的 SHA256
    submitter_location TEXT,             -- 如 "来自 广东省/广州市 的用户"
    model_id         TEXT,               -- 使用的模型 ID（如 DASHSCOPE/qwen-plus）
    temperature      REAL,
    upvotes          INTEGER NOT NULL DEFAULT 0,
    downvotes        INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_feedbacks_created ON feedbacks(created_at DESC);

CREATE TABLE IF NOT EXISTS votes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    feedback_id INTEGER NOT NULL REFERENCES feedbacks(id) ON DELETE CASCADE,
    ip_hash     TEXT    NOT NULL,
    vote_type   TEXT    NOT NULL CHECK (vote_type IN ('up','down')),
    created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE (feedback_id, ip_hash)
);
CREATE INDEX IF NOT EXISTS idx_votes_fb ON votes(feedback_id);

CREATE TABLE IF NOT EXISTS culture_cache (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    origin_region TEXT    NOT NULL UNIQUE,
    content_json  TEXT    NOT NULL,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
"""


# ============================================================
# 工具
# ============================================================

def ip_hash(ip: str) -> str:
    """对 IP 做 SHA256，结果中加盐以避免彩虹表反查。"""
    salt = CONFIG.secret_key or "xiangyin-default-salt"
    return hashlib.sha256(f"{salt}|{ip}".encode("utf-8")).hexdigest()


def _parse_db_path(database_url: str) -> str:
    """从 sqlite:///relative/path 或 sqlite:////abs/path 提取文件路径。"""
    m = re.match(r"^sqlite:///(.+)$", database_url)
    if not m:
        raise ValueError(f"仅支持 sqlite:/// URL，收到: {database_url}")
    path = m.group(1)
    # sqlite:///relative -> relative；sqlite:////abs -> /abs (Windows 盘符也可)
    if path.startswith("/") and len(path) >= 3 and path[1] == ":":
        path = path.lstrip("/")  # /D:/foo -> D:/foo
    return path


_DB_PATH = _parse_db_path(CONFIG.database_url)
_LOCK = threading.Lock()


# ============================================================
# 连接管理
# ============================================================

def _connect() -> sqlite3.Connection:
    # 相对于项目根目录（即本文件所在目录）解析相对路径
    db_path = _DB_PATH
    if not os.path.isabs(db_path):
        db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), db_path)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn


def init_db(app: Flask) -> None:
    """应用启动时调用：确保表结构存在。"""
    # 相对于项目目录建库
    db_path = _DB_PATH
    if not os.path.isabs(db_path):
        db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), db_path)
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    with _LOCK:
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(_SCHEMA_SQL)
            conn.commit()
        finally:
            conn.close()

    # 注册请求级连接
    @app.before_request
    def _before_req():
        g.db = _connect()

    @app.teardown_appcontext
    def _teardown(exc):
        db = g.pop("db", None)
        if db is not None:
            try:
                db.close()
            except Exception:
                pass


def get_conn() -> sqlite3.Connection:
    """优先取请求上下文里的连接，否则创建一个短连接。"""
    try:
        return g.db
    except Exception:
        return _connect()


@contextmanager
def _auto_close(conn: sqlite3.Connection):
    """如果不是请求级连接，用完就关。"""
    from_ctx = False
    try:
        from_ctx = (conn is getattr(g, "db", None))
    except Exception:
        from_ctx = False
    try:
        yield conn
    finally:
        if not from_ctx:
            try:
                conn.close()
            except Exception:
                pass


# ============================================================
# 反馈表 CRUD
# ============================================================

def create_feedback(
    direction: str,
    source_text: str,
    suggested_text: str,
    client_ip: str,
    *,
    target_text: Optional[str] = None,
    origin_region: Optional[str] = None,
    submitter_location: Optional[str] = None,
    model_id: Optional[str] = None,
    temperature: Optional[float] = None,
) -> int:
    """插入一条反馈，返回新记录的 id。"""
    if not suggested_text or not suggested_text.strip():
        raise ValueError("suggested_text 不能为空")

    sql = """
    INSERT INTO feedbacks
      (direction, source_text, target_text, origin_region, suggested_text,
       ip_hash, submitter_location, model_id, temperature)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    conn = get_conn()
    with _auto_close(conn):
        cur = conn.execute(
            sql,
            (
                direction,
                source_text or "",
                target_text,
                origin_region,
                suggested_text.strip(),
                ip_hash(client_ip),
                submitter_location,
                model_id,
                temperature,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def list_feedbacks(limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    """按时间倒序返回反馈列表，包含所有字段。"""
    limit = max(1, min(limit, 200))
    sql = """
    SELECT id, direction, source_text, target_text, origin_region,
           suggested_text, submitter_location, model_id, temperature,
           upvotes, downvotes, created_at
    FROM feedbacks
    ORDER BY created_at DESC, id DESC
    LIMIT ? OFFSET ?
    """
    conn = get_conn()
    with _auto_close(conn):
        rows = conn.execute(sql, (limit, offset)).fetchall()
        return [dict(r) for r in rows]


def get_feedback(fid: int) -> Optional[Dict[str, Any]]:
    sql = "SELECT * FROM feedbacks WHERE id = ?"
    conn = get_conn()
    with _auto_close(conn):
        row = conn.execute(sql, (fid,)).fetchone()
        return dict(row) if row else None


# ============================================================
# 投票（点赞 / 点踩）
# ============================================================

VOTE_OK = "ok"
VOTE_ALREADY_SAME = "already_same"   # 同类型重复投，忽略
VOTE_ALREADY_CHANGED = "changed"     # 之前投过另一类，已切换


def vote_feedback(fid: int, client_ip: str, vote_type: str) -> Tuple[str, Dict[str, int]]:
    """
    对 feedback 投票。基于 ip_hash 去重。
    返回 (状态, {'upvotes':.., 'downvotes':..})
    状态: VOTE_OK / VOTE_ALREADY_SAME / VOTE_ALREADY_CHANGED
    """
    if vote_type not in ("up", "down"):
        raise ValueError("vote_type 必须是 up 或 down")
    ih = ip_hash(client_ip)

    conn = get_conn()
    with _auto_close(conn):
        # 行锁保证并发安全
        with conn:
            # 1. 确认反馈存在，读出当前票数
            row = conn.execute(
                "SELECT id, upvotes, downvotes FROM feedbacks WHERE id = ?",
                (fid,),
            ).fetchone()
            if not row:
                raise KeyError(fid)

            # 2. 查询该 IP 是否已投过
            vrow = conn.execute(
                "SELECT id, vote_type FROM votes WHERE feedback_id = ? AND ip_hash = ?",
                (fid, ih),
            ).fetchone()

            up, down = int(row["upvotes"]), int(row["downvotes"])

            if vrow is None:
                # 首次投票：插入 + 计票+1
                conn.execute(
                    "INSERT INTO votes (feedback_id, ip_hash, vote_type) VALUES (?,?,?)",
                    (fid, ih, vote_type),
                )
                if vote_type == "up":
                    up += 1
                else:
                    down += 1
                status = VOTE_OK
            else:
                old_type = vrow["vote_type"]
                if old_type == vote_type:
                    status = VOTE_ALREADY_SAME
                else:
                    # 切换：删旧票、插新票、调整两边计数
                    conn.execute(
                        "DELETE FROM votes WHERE id = ?", (int(vrow["id"]),)
                    )
                    conn.execute(
                        "INSERT INTO votes (feedback_id, ip_hash, vote_type) VALUES (?,?,?)",
                        (fid, ih, vote_type),
                    )
                    if old_type == "up":
                        up -= 1
                    else:
                        down -= 1
                    if vote_type == "up":
                        up += 1
                    else:
                        down += 1
                    status = VOTE_ALREADY_CHANGED

            conn.execute(
                "UPDATE feedbacks SET upvotes = ?, downvotes = ? WHERE id = ?",
                (up, down, fid),
            )
            return status, {"upvotes": up, "downvotes": down}


# ============================================================
# 科普内容缓存
# ============================================================

def get_culture_cache(origin_region: str) -> Optional[Dict[str, Any]]:
    """读取缓存。返回解析后的 JSON 对象，未命中返回 None。"""
    if not origin_region or not origin_region.strip():
        return None
    key = origin_region.strip()
    conn = get_conn()
    with _auto_close(conn):
        row = conn.execute(
            "SELECT content_json FROM culture_cache WHERE origin_region = ?",
            (key,),
        ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["content_json"])
        except Exception:
            return None


def set_culture_cache(origin_region: str, content: Any) -> None:
    """写入（或覆盖）缓存。content 必须是可 JSON 序列化的对象。"""
    if not origin_region or not origin_region.strip():
        return
    key = origin_region.strip()
    payload = json.dumps(content, ensure_ascii=False)
    conn = get_conn()
    with _auto_close(conn):
        with conn:
            conn.execute(
                """
                INSERT INTO culture_cache (origin_region, content_json)
                VALUES (?, ?)
                ON CONFLICT(origin_region) DO UPDATE SET
                    content_json = excluded.content_json,
                    created_at   = datetime('now','localtime')
                """,
                (key, payload),
            )
