"""SQLite-хранилище.

Используется стандартный sqlite3 в режиме WAL. Запросы к локальной базе занимают
доли миллисекунды (сотни строк), поэтому вызываются напрямую; сетевой ввод-вывод
(Telegram) полностью асинхронный.

Хранятся ТОЛЬКО метаданные: ID чатов/сообщений/отправителей и время. Текст сообщений
клиентов и сотрудников нигде не сохраняется.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterable

from .config import DEFAULT_SETTINGS

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS folders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    name_key TEXT NOT NULL UNIQUE      -- casefold(): SQLite NOCASE не понимает кириллицу
);

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    session TEXT NOT NULL DEFAULT '',
    user_id INTEGER,
    label TEXT NOT NULL DEFAULT '',
    added_ts INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS people (
    user_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS payments (
    chat_id INTEGER NOT NULL,
    topic_id INTEGER NOT NULL DEFAULT 0,
    since_ts INTEGER NOT NULL,
    done INTEGER NOT NULL DEFAULT 0,
    reminded INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, topic_id)
);

CREATE TABLE IF NOT EXISTS employees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    folder_id INTEGER NOT NULL,
    username TEXT NOT NULL,
    user_id INTEGER,
    UNIQUE(folder_id, username)
);

CREATE TABLE IF NOT EXISTS chats (
    chat_id INTEGER PRIMARY KEY,
    folder_id INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'sync',
    is_forum INTEGER NOT NULL DEFAULT 0       -- группа с топиками: лид = топик
);

-- топики форум-групп (иконка-эмодзи топика = статус лида: 🔥 / ⚡ / 💊)
CREATE TABLE IF NOT EXISTS topics (
    chat_id INTEGER NOT NULL,
    topic_id INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    icon TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (chat_id, topic_id)
);

CREATE TABLE IF NOT EXISTS emoji_cache (doc_id INTEGER PRIMARY KEY, ch TEXT NOT NULL);

-- «нить» = топик форума (topic_id>0) либо весь обычный чат (topic_id=0)
CREATE TABLE IF NOT EXISTS thread_state (
    chat_id INTEGER NOT NULL,
    topic_id INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'idle',      -- idle | pending | alerted
    last_lead_msg_id INTEGER,
    last_lead_ts INTEGER,
    pending_since INTEGER,
    last_reply_ts INTEGER,
    alert_id INTEGER,
    PRIMARY KEY (chat_id, topic_id)
);

CREATE TABLE IF NOT EXISTS leads (
    chat_id INTEGER NOT NULL,
    topic_id INTEGER NOT NULL DEFAULT 0,
    sender_id INTEGER,
    first_seen INTEGER NOT NULL,
    last_seen INTEGER NOT NULL,               -- последнее сообщение лида
    last_msg_id INTEGER NOT NULL,
    last_activity_ts INTEGER NOT NULL DEFAULT 0,   -- любое последнее сообщение в нити (лид или менеджер)
    last_push_ts INTEGER,
    msg_count INTEGER NOT NULL DEFAULT 0,      -- сообщений не от сотрудников (для контроля автоставок)
    watch_warned INTEGER NOT NULL DEFAULT 0,
    bet_count INTEGER NOT NULL DEFAULT 0,
    newlead_warned INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, topic_id)
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    folder_id INTEGER NOT NULL,
    remaining TEXT NOT NULL,                  -- JSON-список chat_id без ответа
    text TEXT NOT NULL,
    group_chat_id INTEGER,
    message_id INTEGER,
    status TEXT NOT NULL DEFAULT 'open',      -- open | done | stale
    created_ts INTEGER NOT NULL,
    closed_ts INTEGER,
    closed_by TEXT
);

CREATE TABLE IF NOT EXISTS breaks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    username TEXT,
    start_ts INTEGER NOT NULL,
    end_ts INTEGER,                           -- NULL = сотрудник ещё не написал «тут»
    reminded INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pauses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    folder_id INTEGER NOT NULL,
    start_ts INTEGER NOT NULL,
    end_ts INTEGER
);

CREATE TABLE IF NOT EXISTS bot_users (
    user_id INTEGER PRIMARY KEY,
    started_ts INTEGER NOT NULL,
    blocked INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ignored_msgs (
    chat_id INTEGER NOT NULL,
    msg_id INTEGER NOT NULL,
    ts INTEGER NOT NULL,
    PRIMARY KEY (chat_id, msg_id)
);

CREATE TABLE IF NOT EXISTS broadcasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_ts INTEGER NOT NULL,
    finished_ts INTEGER,
    mode TEXT NOT NULL,
    trigger TEXT NOT NULL,
    total INTEGER NOT NULL DEFAULT 0,
    sent INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running'    -- running | done | stopped | aborted | interrupted
);

-- ответы сотрудников (только время и ID — для отчётов и рейтинга)
CREATE TABLE IF NOT EXISTS replies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    topic_id INTEGER NOT NULL,
    folder_id INTEGER NOT NULL,
    emp_id INTEGER,                           -- user_id сотрудника (NULL = сам аккаунт юзербота)
    lead_ts INTEGER NOT NULL,
    reply_ts INTEGER NOT NULL,
    work_seconds INTEGER NOT NULL             -- ожидание в рабочее время
);
CREATE INDEX IF NOT EXISTS idx_replies_ts ON replies(reply_ts);

CREATE TABLE IF NOT EXISTS admins (
    user_id INTEGER PRIMARY KEY,
    role TEXT NOT NULL DEFAULT 'view',        -- view | push | full
    added_ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    media TEXT NOT NULL DEFAULT ''            -- JSON {kind,file_id,path,label} или пусто
);

CREATE INDEX IF NOT EXISTS idx_leads_last_seen ON leads(last_seen);
CREATE INDEX IF NOT EXISTS idx_state_status ON thread_state(status);
"""


def now_ts() -> int:
    return int(time.time())


class Database:
    def __init__(self, path: Path | str):
        self._path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._migrate_old()
        self._conn.executescript(SCHEMA)
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(chats)")}
        lcols = {r[1] for r in self._conn.execute("PRAGMA table_info(leads)")}
        for col in ("msg_count", "watch_warned", "bet_count", "newlead_warned"):
            if col not in lcols:
                self._conn.execute(f"ALTER TABLE leads ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
        fcols = {r[1] for r in self._conn.execute("PRAGMA table_info(folders)")}
        for col, ddl in (("account_id", "INTEGER"), ("delay_on", "INTEGER NOT NULL DEFAULT 1"),
                         ("push_on", "INTEGER NOT NULL DEFAULT 1"), ("fine", "INTEGER NOT NULL DEFAULT 0"),
                         ("push_limit", "INTEGER NOT NULL DEFAULT 0"), ("quiet_from", "TEXT NOT NULL DEFAULT ''"),
                         ("quiet_to", "TEXT NOT NULL DEFAULT ''")):
            if col not in fcols:
                self._conn.execute(f"ALTER TABLE folders ADD COLUMN {col} {ddl}")
        if "first_lead_msg_id" not in {r[1] for r in self._conn.execute("PRAGMA table_info(thread_state)")}:
            self._conn.execute("ALTER TABLE thread_state ADD COLUMN first_lead_msg_id INTEGER")
        if "paused" not in {r[1] for r in self._conn.execute("PRAGMA table_info(accounts)")}:
            self._conn.execute("ALTER TABLE accounts ADD COLUMN paused INTEGER NOT NULL DEFAULT 0")
        acols = {r[1] for r in self._conn.execute("PRAGMA table_info(alerts)")}
        if "n_items" not in acols:
            self._conn.execute("ALTER TABLE alerts ADD COLUMN n_items INTEGER NOT NULL DEFAULT 0")
        if "is_forum" not in cols:
            self._conn.execute("ALTER TABLE chats ADD COLUMN is_forum INTEGER NOT NULL DEFAULT 0")

    def _migrate_old(self) -> None:
        """Старая версия (лид = пользователь): таблицы лидов/состояний пересоздаются, остальное сохраняется."""
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(leads)")}
        if cols and "topic_id" not in cols:
            self._conn.execute("DROP TABLE leads")
            self._conn.execute("DROP TABLE IF EXISTS chat_state")
            self._conn.execute("UPDATE alerts SET status='stale' WHERE status='open'")

    # ------------------------------------------------------------ низкий уровень
    def execute(self, sql: str, params: Iterable = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def one(self, sql: str, params: Iterable = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchone()

    def all(self, sql: str, params: Iterable = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------ настройки
    def get(self, key: str) -> str:
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        if row is not None:
            return row["value"]
        return DEFAULT_SETTINGS.get(key, "")

    def get_int(self, key: str) -> int:
        try:
            return int(self.get(key))
        except ValueError:
            return int(DEFAULT_SETTINGS.get(key, "0") or 0)

    def set(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    # ------------------------------------------------------------ папки
    def add_folder(self, name: str) -> int | None:
        name = name.strip()
        try:
            cur = self.execute("INSERT INTO folders(name, name_key) VALUES(?, ?)", (name, name.casefold()))
            return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            return None

    def folders(self) -> list[sqlite3.Row]:
        return sorted(self.all("SELECT * FROM folders"), key=lambda r: r["name_key"])

    def folder(self, folder_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM folders WHERE id=?", (folder_id,))

    def folder_by_title(self, title: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM folders WHERE name_key=?", (title.strip().casefold(),))

    def rename_folder(self, folder_id: int, name: str) -> bool:
        name = name.strip()
        try:
            self.execute("UPDATE folders SET name=?, name_key=? WHERE id=?", (name, name.casefold(), folder_id))
            return True
        except sqlite3.IntegrityError:
            return False

    def delete_folder(self, folder_id: int) -> None:
        with self._lock:
            chat_ids = [r["chat_id"] for r in self.all("SELECT chat_id FROM chats WHERE folder_id=?", (folder_id,))]
            for cid in chat_ids:
                self.execute("DELETE FROM thread_state WHERE chat_id=?", (cid,))
                self.execute("DELETE FROM topics WHERE chat_id=?", (cid,))
            self.execute("DELETE FROM chats WHERE folder_id=?", (folder_id,))
            self.execute("DELETE FROM employees WHERE folder_id=?", (folder_id,))
            self.execute("DELETE FROM pauses WHERE folder_id=?", (folder_id,))
            self.execute("DELETE FROM folders WHERE id=?", (folder_id,))

    # ------------------------------------------------------------ сотрудники
    def add_employee(self, folder_id: int, username: str) -> bool:
        known = self.one("SELECT user_id FROM employees WHERE username=? AND user_id IS NOT NULL", (username,))
        try:
            self.execute(
                "INSERT INTO employees(folder_id, username, user_id) VALUES(?,?,?)",
                (folder_id, username, known["user_id"] if known else None),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def employees(self, folder_id: int) -> list[sqlite3.Row]:
        return self.all("SELECT * FROM employees WHERE folder_id=? ORDER BY username", (folder_id,))

    def employee(self, emp_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM employees WHERE id=?", (emp_id,))

    def delete_employee(self, emp_id: int) -> None:
        self.execute("DELETE FROM employees WHERE id=?", (emp_id,))

    def unresolved_employees(self) -> list[sqlite3.Row]:
        return self.all("SELECT DISTINCT username FROM employees WHERE user_id IS NULL")

    def set_employee_user_id(self, username: str, user_id: int) -> None:
        self.execute("UPDATE employees SET user_id=? WHERE username=?", (user_id, username))

    def employee_user_ids(self) -> set[int]:
        return {r["user_id"] for r in self.all("SELECT DISTINCT user_id FROM employees WHERE user_id IS NOT NULL")}

    def employee_folder_ids(self, user_id: int | None, username: str | None) -> list[int]:
        rows = self.all(
            "SELECT DISTINCT folder_id FROM employees WHERE (user_id IS NOT NULL AND user_id=?) OR username=?",
            (user_id if user_id is not None else -1, (username or "").lower() or "\0"),
        )
        return [r["folder_id"] for r in rows]

    # ------------------------------------------------------------ чаты
    def upsert_chat(self, chat_id: int, folder_id: int, title: str = "", source: str = "sync") -> None:
        self.execute(
            "INSERT INTO chats(chat_id, folder_id, title, source) VALUES(?,?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET folder_id=excluded.folder_id, "
            "title=CASE WHEN excluded.title<>'' THEN excluded.title ELSE chats.title END, "
            "source=excluded.source",
            (chat_id, folder_id, title, source),
        )

    def delete_chat(self, chat_id: int) -> None:
        self.execute("DELETE FROM chats WHERE chat_id=?", (chat_id,))
        self.execute("DELETE FROM thread_state WHERE chat_id=?", (chat_id,))
        self.execute("DELETE FROM topics WHERE chat_id=?", (chat_id,))

    def chats(self, folder_id: int) -> list[sqlite3.Row]:
        return self.all("SELECT * FROM chats WHERE folder_id=? ORDER BY title COLLATE NOCASE, chat_id", (folder_id,))

    def chat(self, chat_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM chats WHERE chat_id=?", (chat_id,))

    def chat_ids(self) -> set[int]:
        return {r["chat_id"] for r in self.all("SELECT chat_id FROM chats")}

    def forum_ids(self) -> set[int]:
        return {r["chat_id"] for r in self.all("SELECT chat_id FROM chats WHERE is_forum=1")}

    def set_chat_title(self, chat_id: int, title: str) -> None:
        self.execute("UPDATE chats SET title=? WHERE chat_id=?", (title, chat_id))

    def set_forum(self, chat_id: int, is_forum: bool) -> None:
        self.execute("UPDATE chats SET is_forum=? WHERE chat_id=?", (1 if is_forum else 0, chat_id))

    # ------------------------------------------------------------ топики и эмодзи
    def upsert_topic(self, chat_id: int, topic_id: int, title: str, icon: str) -> None:
        self.execute(
            "INSERT INTO topics(chat_id, topic_id, title, icon) VALUES(?,?,?,?) "
            "ON CONFLICT(chat_id, topic_id) DO UPDATE SET title=excluded.title, "
            "icon=CASE WHEN excluded.icon<>'' THEN excluded.icon ELSE topics.icon END",
            (chat_id, topic_id, title, icon),
        )

    def topics(self, chat_id: int) -> list[sqlite3.Row]:
        return self.all("SELECT * FROM topics WHERE chat_id=? ORDER BY topic_id", (chat_id,))

    def emoji_chars(self, doc_ids: Iterable[int]) -> dict[int, str]:
        ids = list(doc_ids)
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        return {r["doc_id"]: r["ch"] for r in self.all(f"SELECT * FROM emoji_cache WHERE doc_id IN ({marks})", ids)}

    def set_emoji_char(self, doc_id: int, ch: str) -> None:
        self.execute("INSERT OR REPLACE INTO emoji_cache(doc_id, ch) VALUES(?,?)", (doc_id, ch))

    def sync_folder_chats(self, folder_id: int, items: dict[int, str]) -> tuple[int, int]:
        """Привести авто-чаты папки к списку из Telegram. Ручные чаты не трогаем.
        Возвращает (добавлено, удалено)."""
        added = removed = 0
        with self._lock:
            current = {r["chat_id"]: r for r in self.all("SELECT * FROM chats WHERE folder_id=?", (folder_id,))}
            for cid, title in items.items():
                row = self.chat(cid)
                if row is not None and row["folder_id"] != folder_id and row["source"] == "manual":
                    continue  # чат закреплён вручную за другой папкой
                if cid not in current:
                    added += 1
                self.upsert_chat(cid, folder_id, title, current[cid]["source"] if cid in current else "sync")
            for cid, row in current.items():
                if row["source"] == "sync" and cid not in items:
                    self.delete_chat(cid)
                    removed += 1
        return added, removed

    # ------------------------------------------------------------ состояние нитей (топиков)
    def state(self, chat_id: int, topic_id: int = 0) -> sqlite3.Row | None:
        return self.one("SELECT * FROM thread_state WHERE chat_id=? AND topic_id=?", (chat_id, topic_id))

    def set_pending(self, chat_id: int, topic_id: int, msg_id: int, ts: int, restart_streak: bool) -> None:
        """Новое сообщение лида без ответа. restart_streak=True — начать отсчёт заново."""
        with self._lock:
            row = self.state(chat_id, topic_id)
            if row is None:
                self.execute(
                    "INSERT INTO thread_state(chat_id, topic_id, status, last_lead_msg_id, last_lead_ts, pending_since, "
                    "first_lead_msg_id) VALUES(?, ?, 'pending', ?, ?, ?, ?)",
                    (chat_id, topic_id, msg_id, ts, ts, msg_id),
                )
                return
            fresh = restart_streak or row["status"] != "pending" or row["pending_since"] is None
            since = ts if fresh else row["pending_since"]
            first = msg_id if (fresh or not row["first_lead_msg_id"]) else row["first_lead_msg_id"]
            self.execute(
                "UPDATE thread_state SET status='pending', last_lead_msg_id=?, last_lead_ts=?, "
                "pending_since=?, first_lead_msg_id=?, alert_id=NULL WHERE chat_id=? AND topic_id=?",
                (msg_id, ts, since, first, chat_id, topic_id),
            )

    def set_idle(self, chat_id: int, topic_id: int, ts: int) -> bool:
        """Сотрудник ответил. Возвращает True, если до этого был неотвеченный лид."""
        with self._lock:
            row = self.state(chat_id, topic_id)
            if row is None:
                self.execute(
                    "INSERT INTO thread_state(chat_id, topic_id, status, last_reply_ts) VALUES(?, ?, 'idle', ?)",
                    (chat_id, topic_id, ts),
                )
                return False
            was_open = row["status"] in ("pending", "alerted")
            self.execute(
                "UPDATE thread_state SET status='idle', last_reply_ts=?, pending_since=NULL, alert_id=NULL "
                "WHERE chat_id=? AND topic_id=?",
                (ts, chat_id, topic_id),
            )
            return was_open

    LABEL_SQL = (
        "CASE WHEN {a}.topic_id<>0 THEN TRIM(COALESCE(t.icon,'') || ' ' || COALESCE(t.title,'')) "
        "ELSE COALESCE(c.title,'') END"
    )

    def pending_rows(self) -> list[sqlite3.Row]:
        label = self.LABEL_SQL.format(a="s")
        return self.all(
            f"SELECT s.*, c.folder_id AS folder_id, {label} AS label FROM thread_state s "
            "JOIN chats c ON c.chat_id = s.chat_id "
            "LEFT JOIN topics t ON t.chat_id = s.chat_id AND t.topic_id = s.topic_id "
            "WHERE s.status='pending'"
        )

    def mark_alerted(self, threads: Iterable[tuple[int, int]], alert_id: int) -> None:
        for cid, tid in threads:
            self.execute(
                "UPDATE thread_state SET status='alerted', alert_id=? WHERE chat_id=? AND topic_id=?",
                (alert_id, cid, tid),
            )

    def count_by_status(self) -> dict[str, int]:
        return {r["status"]: r["n"] for r in self.all(
            "SELECT s.status AS status, COUNT(*) AS n FROM thread_state s "
            "JOIN chats c ON c.chat_id=s.chat_id GROUP BY s.status")}

    # ------------------------------------------------------------ лиды (лид = нить/топик)
    def touch_lead(self, chat_id: int, topic_id: int, sender_id: int | None, msg_id: int, ts: int) -> None:
        self.execute(
            "INSERT INTO leads(chat_id, topic_id, sender_id, first_seen, last_seen, last_msg_id, last_activity_ts) "
            "VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(chat_id, topic_id) DO UPDATE SET first_seen=MIN(leads.first_seen, excluded.first_seen), "
            "sender_id=CASE WHEN excluded.last_seen>=leads.last_seen THEN excluded.sender_id ELSE leads.sender_id END, "
            "last_msg_id=CASE WHEN excluded.last_seen>=leads.last_seen THEN excluded.last_msg_id ELSE leads.last_msg_id END, "
            "last_seen=MAX(leads.last_seen, excluded.last_seen), "
            "last_activity_ts=MAX(leads.last_activity_ts, excluded.last_activity_ts)",
            (chat_id, topic_id, sender_id, ts, ts, msg_id, ts),
        )

    def bump_count(self, chat_id: int, topic_id: int, bet: bool = False) -> None:
        col = "bet_count" if bet else "msg_count"
        self.execute(f"UPDATE leads SET {col}={col}+1 WHERE chat_id=? AND topic_id=?", (chat_id, topic_id))

    def silent_bot_threads(self, since_first: int, max_count: int, first_before: int) -> list[sqlite3.Row]:
        """Новые нити, где к назначенному сроку пришло меньше ожидаемого числа ставок (кружков)."""
        return self.all(
            "SELECT l.*, COALESCE(c.title,'') AS chat_title, COALESCE(t.title,'') AS topic_title "
            "FROM leads l JOIN chats c ON c.chat_id=l.chat_id "
            "LEFT JOIN topics t ON t.chat_id=l.chat_id AND t.topic_id=l.topic_id "
            "WHERE l.first_seen>=? AND l.bet_count<? AND l.first_seen<=? AND l.watch_warned=0",
            (since_first, max_count, first_before))

    def lead_bets(self, chat_id: int, topic_id: int) -> tuple[int, int] | None:
        r = self.one("SELECT first_seen, bet_count FROM leads WHERE chat_id=? AND topic_id=?", (chat_id, topic_id))
        return (int(r["first_seen"]), int(r["bet_count"])) if r else None

    def lead_msg_total(self, chat_id: int, topic_id: int) -> int:
        """Сколько сообщений клиента и ставок уже учтено в нити (0 — нити ещё нет)."""
        r = self.one("SELECT msg_count+bet_count AS n FROM leads WHERE chat_id=? AND topic_id=?", (chat_id, topic_id))
        return int(r["n"]) if r else 0

    def mark_newlead_warned(self, chat_id: int, topic_id: int) -> None:
        self.execute("UPDATE leads SET newlead_warned=1 WHERE chat_id=? AND topic_id=?", (chat_id, topic_id))

    def mark_watch_warned(self, chat_id: int, topic_id: int) -> None:
        self.execute("UPDATE leads SET watch_warned=1 WHERE chat_id=? AND topic_id=?", (chat_id, topic_id))

    def touch_activity(self, chat_id: int, topic_id: int, ts: int) -> None:
        """Любое сообщение в нити (в том числе менеджера) — для паузы «не писать недавно активным»."""
        self.execute(
            "UPDATE leads SET last_activity_ts=MAX(last_activity_ts, ?) WHERE chat_id=? AND topic_id=?",
            (ts, chat_id, topic_id),
        )

    def leads_with_labels(self) -> list[sqlite3.Row]:
        """Лиды в контролируемых чатах + подпись (иконка+название топика либо название чата)."""
        label = self.LABEL_SQL.format(a="l")
        return self.all(
            f"SELECT l.*, {label} AS label, COALESCE(c.title,'') AS chat_title FROM leads l "
            "JOIN chats c ON c.chat_id = l.chat_id "
            "LEFT JOIN topics t ON t.chat_id = l.chat_id AND t.topic_id = l.topic_id"
        )

    def leads_since(self, since_ts: int, limit: int = 20, offset: int = 0) -> list[sqlite3.Row]:
        rows = [r for r in self.leads_with_labels() if r["last_seen"] >= since_ts]
        rows.sort(key=lambda r: -r["last_seen"])
        return rows[offset:offset + limit]

    def count_leads(self, since_ts: int = 0) -> int:
        return sum(1 for r in self.leads_with_labels() if r["last_seen"] >= since_ts)

    def mark_pushed(self, chat_id: int, topic_id: int, ts: int) -> None:
        self.execute("UPDATE leads SET last_push_ts=? WHERE chat_id=? AND topic_id=?", (ts, chat_id, topic_id))

    # ------------------------------------------------------------ ответы / отчёты
    def record_reply(self, chat_id: int, topic_id: int, folder_id: int, emp_id: int | None,
                     lead_ts: int, reply_ts: int, work_seconds: int) -> None:
        self.execute(
            "INSERT INTO replies(chat_id, topic_id, folder_id, emp_id, lead_ts, reply_ts, work_seconds) "
            "VALUES(?,?,?,?,?,?,?)", (chat_id, topic_id, folder_id, emp_id, lead_ts, reply_ts, work_seconds))

    def reply_stats(self, since: int, until: int, late_s: int = 0) -> list[sqlite3.Row]:
        """По сотрудникам: число ответов, среднее и максимальное ожидание (сек, рабочее время) и сколько ответов
        пришли позже лимита (late_s; 0 — не считать). Сверху — кто ответил на большее число клиентов."""
        return self.all(
            "SELECT emp_id, COUNT(*) AS n, AVG(work_seconds) AS avg_s, MAX(work_seconds) AS max_s, "
            "COALESCE(SUM(CASE WHEN ?>0 AND work_seconds>=? THEN 1 ELSE 0 END),0) AS late "
            "FROM replies WHERE reply_ts>=? AND reply_ts<? GROUP BY emp_id ORDER BY n DESC, avg_s",
            (late_s, late_s, since, until))

    def lead_periods(self, today: int, yesterday: int, week_from: int, track_from: int) -> dict[int, tuple[int, int, int, int]]:
        """Новые лиды по папкам: {folder_id: (сегодня, вчера, 7 дней, всего с даты начала учёта)} — по первому сообщению."""
        rows = self.all(
            "SELECT c.folder_id AS fid, "
            "COALESCE(SUM(l.first_seen>=?),0) AS d0, COALESCE(SUM(l.first_seen>=? AND l.first_seen<?),0) AS d1, "
            "COALESCE(SUM(l.first_seen>=?),0) AS w, COALESCE(SUM(l.first_seen>=?),0) AS t "
            "FROM leads l JOIN chats c ON c.chat_id=l.chat_id GROUP BY c.folder_id",
            (today, yesterday, today, week_from, track_from))
        return {r["fid"]: (int(r["d0"]), int(r["d1"]), int(r["w"]), int(r["t"])) for r in rows}

    def alerts_by_folder(self, since: int, until: int) -> list[sqlite3.Row]:
        return self.all(
            "SELECT f.id AS fid, f.name AS name, f.fine AS fine, "
            "SUM(CASE WHEN a.n_items>0 THEN a.n_items ELSE 1 END) AS n FROM alerts a JOIN folders f ON f.id=a.folder_id "
            "WHERE a.created_ts>=? AND a.created_ts<? GROUP BY f.id ORDER BY n DESC", (since, until))

    def new_leads_by_folder(self, since: int, until: int) -> list[sqlite3.Row]:
        return self.all(
            "SELECT f.name AS name, COUNT(*) AS n FROM leads l JOIN chats c ON c.chat_id=l.chat_id "
            "JOIN folders f ON f.id=c.folder_id WHERE l.first_seen>=? AND l.first_seen<? "
            "GROUP BY f.id ORDER BY n DESC", (since, until))

    def breaks_between(self, since: int, until: int) -> int:
        return int(self.one("SELECT COUNT(*) AS n FROM breaks WHERE start_ts>=? AND start_ts<?", (since, until))["n"])

    def pushed_between(self, since: int, until: int) -> int:
        return int(self.one(
            "SELECT COALESCE(SUM(sent),0) AS n FROM broadcasts WHERE started_ts>=? AND started_ts<?",
            (since, until))["n"])

    def emp_name(self, user_id: int | None) -> str:
        if user_id is None:
            return "аккаунт юзербота"
        row = self.one("SELECT username FROM employees WHERE user_id=? LIMIT 1", (user_id,))
        if row:
            return "@" + row["username"]
        p = self.one("SELECT name FROM people WHERE user_id=?", (user_id,))
        return p["name"] if p and p["name"] else f"ID {user_id}"

    def remember_person(self, user_id: int, name: str) -> None:
        """Имя/ник живого человека, ответившего в чатах (из профиля отправителя — не из текста сообщений)."""
        self.execute("INSERT INTO people(user_id, name) VALUES(?, ?) ON CONFLICT(user_id) DO UPDATE SET name=excluded.name",
                     (user_id, name[:60]))

    # ------------------------------------------------------------ аккаунты Push
    def accounts(self) -> list[sqlite3.Row]:
        return self.all("SELECT * FROM accounts ORDER BY id")

    def account(self, acc_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM accounts WHERE id=?", (acc_id,))

    def add_account(self, name: str, session: str, user_id: int | None, label: str, ts: int) -> int:
        return int(self.execute("INSERT INTO accounts(name, session, user_id, label, added_ts) VALUES(?,?,?,?,?)",
                                (name, session, user_id, label, ts)).lastrowid)

    def set_account_paused(self, acc_id: int, paused: bool) -> None:
        self.execute("UPDATE accounts SET paused=? WHERE id=?", (1 if paused else 0, acc_id))

    def rename_account(self, acc_id: int, name: str) -> None:
        self.execute("UPDATE accounts SET name=? WHERE id=?", (name, acc_id))

    def leads_per_folder(self, since: int) -> dict[int, int]:
        rows = self.all("SELECT c.folder_id AS fid, COUNT(*) AS n FROM leads l JOIN chats c ON c.chat_id=l.chat_id "
                        "WHERE l.first_seen>=? GROUP BY c.folder_id", (since,))
        return {r["fid"]: r["n"] for r in rows}

    def set_account_info(self, acc_id: int, user_id: int | None, label: str) -> None:
        self.execute("UPDATE accounts SET user_id=?, label=? WHERE id=?", (user_id, label, acc_id))

    def delete_account(self, acc_id: int) -> None:
        self.execute("UPDATE folders SET account_id=NULL WHERE account_id=?", (acc_id,))
        self.execute("DELETE FROM accounts WHERE id=?", (acc_id,))

    def account_user_ids(self) -> set[int]:
        return {r["user_id"] for r in self.accounts() if r["user_id"]}

    def set_folder_field(self, folder_id: int, col: str, value) -> None:
        if col not in ("account_id", "delay_on", "push_on", "fine", "push_limit", "quiet_from", "quiet_to"):
            raise ValueError(col)
        self.execute(f"UPDATE folders SET {col}=? WHERE id=?", (value, folder_id))

    def chat_folder_map(self) -> dict[int, int]:
        return {r["chat_id"]: r["folder_id"] for r in self.all("SELECT chat_id, folder_id FROM chats")}

    # ------------------------------------------------------------ напоминание об оплате
    def start_payment(self, chat_id: int, topic_id: int, ts: int) -> None:
        self.execute("INSERT INTO payments(chat_id, topic_id, since_ts, done, reminded) VALUES(?,?,?,0,0) "
                     "ON CONFLICT(chat_id, topic_id) DO UPDATE SET since_ts=excluded.since_ts, done=0, reminded=0",
                     (chat_id, topic_id, ts))

    def done_payment(self, chat_id: int, topic_id: int) -> None:
        self.execute("UPDATE payments SET done=1 WHERE chat_id=? AND topic_id=?", (chat_id, topic_id))

    def due_payments(self, border: int) -> list[sqlite3.Row]:
        return self.all("SELECT * FROM payments WHERE done=0 AND reminded=0 AND since_ts<=?", (border,))

    def mark_payment_reminded(self, chat_id: int, topic_id: int) -> None:
        self.execute("UPDATE payments SET reminded=1 WHERE chat_id=? AND topic_id=?", (chat_id, topic_id))

    # ------------------------------------------------------------ админы
    def admin_role(self, user_id: int) -> str | None:
        row = self.one("SELECT role FROM admins WHERE user_id=?", (user_id,))
        return row["role"] if row else None

    def admins(self) -> list[sqlite3.Row]:
        return self.all("SELECT * FROM admins ORDER BY added_ts")

    def set_admin(self, user_id: int, role: str, ts: int) -> None:
        self.execute(
            "INSERT INTO admins(user_id, role, added_ts) VALUES(?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET role=excluded.role", (user_id, role, ts))

    def delete_admin(self, user_id: int) -> None:
        self.execute("DELETE FROM admins WHERE user_id=?", (user_id,))

    # ------------------------------------------------------------ шаблоны Push
    def templates(self) -> list[sqlite3.Row]:
        return self.all("SELECT * FROM templates ORDER BY id")

    def template(self, tid: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM templates WHERE id=?", (tid,))

    def add_template(self, name: str, text: str, media: str) -> int:
        return int(self.execute("INSERT INTO templates(name, text, media) VALUES(?,?,?)", (name, text, media)).lastrowid)

    def delete_template(self, tid: int) -> None:
        self.execute("DELETE FROM templates WHERE id=?", (tid,))

    def media_paths_in_use(self) -> set[str]:
        import json as _json
        paths = set()
        for raw in [self.get("push_media")] + [r["media"] for r in self.templates()]:
            if raw:
                try:
                    paths.add(_json.loads(raw).get("path", ""))
                except ValueError:
                    pass
        return paths

    # ------------------------------------------------------------ резервная копия (без сессии аккаунта!)
    def backup_to(self, path: str) -> None:
        import os
        if os.path.exists(path):
            os.remove(path)
        with self._lock:
            self._conn.execute("VACUUM INTO ?", (path,))
        copy = sqlite3.connect(path)
        try:
            copy.execute("UPDATE settings SET value='' WHERE key='userbot_session'")
            copy.commit()
        finally:
            copy.close()

    # ------------------------------------------------------------ алерты «Проёб»
    def create_alert(self, folder_id: int, chat_ids: list[int], text: str, ts: int) -> int:
        cur = self.execute(
            "INSERT INTO alerts(folder_id, remaining, text, created_ts, n_items) VALUES(?,?,?,?,?)",
            (folder_id, json.dumps(chat_ids), text, ts, len(chat_ids)),
        )
        return int(cur.lastrowid)

    def set_alert_message(self, alert_id: int, group_chat_id: int, message_id: int) -> None:
        self.execute("UPDATE alerts SET group_chat_id=?, message_id=? WHERE id=?", (group_chat_id, message_id, alert_id))

    def delete_alert(self, alert_id: int) -> None:
        self.execute("DELETE FROM alerts WHERE id=?", (alert_id,))

    def alert(self, alert_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM alerts WHERE id=?", (alert_id,))

    def open_alerts(self) -> list[sqlite3.Row]:
        return self.all("SELECT * FROM alerts WHERE status='open' ORDER BY id")

    def set_alert_remaining(self, alert_id: int, remaining: list[int]) -> None:
        self.execute("UPDATE alerts SET remaining=? WHERE id=?", (json.dumps(remaining), alert_id))

    def close_alert(self, alert_id: int, status: str, by: str, ts: int) -> None:
        self.execute(
            "UPDATE alerts SET status=?, closed_by=?, closed_ts=?, remaining='[]' WHERE id=? AND status='open'",
            (status, by, ts, alert_id),
        )

    # ------------------------------------------------------------ перерывы / паузы
    def add_break(self, user_id: int | None, username: str | None, ts: int) -> int:
        cur = self.execute(
            "INSERT INTO breaks(user_id, username, start_ts) VALUES(?,?,?)", (user_id, (username or "").lower() or None, ts)
        )
        return int(cur.lastrowid)

    def _break_match(self, user_id: int | None, username: str | None) -> tuple[str, tuple]:
        return (
            "((user_id IS NOT NULL AND user_id=?) OR (username IS NOT NULL AND username=?))",
            (user_id if user_id is not None else -1, (username or "").lower() or "\0"),
        )

    def open_break(self, user_id: int | None, username: str | None) -> sqlite3.Row | None:
        cond, params = self._break_match(user_id, username)
        return self.one(f"SELECT * FROM breaks WHERE end_ts IS NULL AND {cond} ORDER BY id DESC LIMIT 1", params)

    def breaks_since(self, user_id: int | None, username: str | None, since_ts: int) -> int:
        cond, params = self._break_match(user_id, username)
        return int(self.one(f"SELECT COUNT(*) AS n FROM breaks WHERE start_ts>=? AND {cond}", (since_ts, *params))["n"])

    def all_open_breaks(self) -> list[sqlite3.Row]:
        return self.all("SELECT * FROM breaks WHERE end_ts IS NULL")

    def recent_breaks(self, since_ts: int) -> list[sqlite3.Row]:
        return self.all("SELECT * FROM breaks WHERE start_ts>=? OR end_ts IS NULL", (since_ts,))

    def close_break(self, break_id: int, ts: int) -> None:
        self.execute("UPDATE breaks SET end_ts=? WHERE id=? AND end_ts IS NULL", (ts, break_id))

    def mark_break_reminded(self, break_id: int) -> None:
        self.execute("UPDATE breaks SET reminded=1 WHERE id=?", (break_id,))

    def open_pause(self, folder_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM pauses WHERE folder_id=? AND end_ts IS NULL", (folder_id,))

    def start_pause(self, folder_id: int, ts: int) -> None:
        self.execute("INSERT INTO pauses(folder_id, start_ts) VALUES(?,?)", (folder_id, ts))

    def end_pause(self, folder_id: int, ts: int) -> None:
        self.execute("UPDATE pauses SET end_ts=? WHERE folder_id=? AND end_ts IS NULL", (ts, folder_id))

    def pauses(self, folder_id: int, since_ts: int) -> list[sqlite3.Row]:
        return self.all(
            "SELECT * FROM pauses WHERE folder_id=? AND (end_ts IS NULL OR end_ts>?)", (folder_id, since_ts)
        )

    # ------------------------------------------------------------ пользователи бота
    def add_bot_user(self, user_id: int, ts: int) -> None:
        self.execute(
            "INSERT INTO bot_users(user_id, started_ts) VALUES(?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET blocked=0",
            (user_id, ts),
        )

    def mark_blocked(self, user_id: int) -> None:
        self.execute("UPDATE bot_users SET blocked=1 WHERE user_id=?", (user_id,))

    def reachable_bot_users(self) -> set[int]:
        return {r["user_id"] for r in self.all("SELECT user_id FROM bot_users WHERE blocked=0")}

    # ------------------------------------------------------------ игнорируемые сообщения (рассылка)
    def ignore_msg(self, chat_id: int, msg_id: int, ts: int) -> None:
        self.execute("INSERT OR IGNORE INTO ignored_msgs(chat_id, msg_id, ts) VALUES(?,?,?)", (chat_id, msg_id, ts))

    def is_ignored(self, chat_id: int, msg_id: int) -> bool:
        return self.one("SELECT 1 FROM ignored_msgs WHERE chat_id=? AND msg_id=?", (chat_id, msg_id)) is not None

    def prune(self, now: int) -> None:
        self.execute("DELETE FROM ignored_msgs WHERE ts<?", (now - 3 * 86400,))
        self.execute("UPDATE alerts SET status='stale', closed_ts=? WHERE status='open' AND created_ts<?", (now, now - 2 * 86400))
        self.execute("DELETE FROM breaks WHERE end_ts IS NOT NULL AND end_ts<?", (now - 3 * 86400,))
        self.execute("DELETE FROM pauses WHERE end_ts IS NOT NULL AND end_ts<?", (now - 30 * 86400,))
        self.execute("DELETE FROM replies WHERE reply_ts<?", (now - 120 * 86400,))

    # ------------------------------------------------------------ журнал рассылок
    def start_broadcast(self, mode: str, trigger: str, total: int, ts: int) -> int:
        cur = self.execute(
            "INSERT INTO broadcasts(started_ts, mode, trigger, total) VALUES(?,?,?,?)", (ts, mode, trigger, total)
        )
        return int(cur.lastrowid)

    def update_broadcast(self, bid: int, sent: int, failed: int) -> None:
        self.execute("UPDATE broadcasts SET sent=?, failed=? WHERE id=?", (sent, failed, bid))

    def finish_broadcast(self, bid: int, status: str, ts: int) -> None:
        self.execute("UPDATE broadcasts SET status=?, finished_ts=? WHERE id=?", (status, ts, bid))

    def interrupted_broadcasts(self) -> list[sqlite3.Row]:
        rows = self.all("SELECT * FROM broadcasts WHERE status='running'")
        self.execute("UPDATE broadcasts SET status='interrupted' WHERE status='running'")
        return rows

    def last_broadcasts(self, n: int = 5) -> list[sqlite3.Row]:
        return self.all("SELECT * FROM broadcasts ORDER BY id DESC LIMIT ?", (n,))
