"""SQLite persistence for journal reviews, plans, and training strategy text."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping


STRATEGY_DEFAULT = {
    "name": "趋势回踩策略V1.0",
    "version": "1.0",
    "rules": [
        "只使用已完成的原始日线记录观察趋势与回踩，不把复权价格当作成交价。",
        "计划应写明入场条件、止损条件和退出依据；保存计划本身不会自动下单。",
        "历史模拟只在已验证交易日历的下一交易日按开盘价检查限价委托。",
        "来源、涨跌停或公司行动未核验时不生成模拟成交；风险预算只是训练约束。",
        "本规则用于研究训练，不承诺收益，也不代表券商或实盘执行条件。",
    ],
    "training_conditions": [
        "研究用途；记录和模拟账户与现有研究及真实持仓隔离。",
        "模拟手续费、成交容量、报价和公司行动处理以当前程序校验结果为准。",
    ],
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso_date(value: str) -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError):
        raise ValueError("日期格式应为 YYYY-MM-DD") from None


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class JournalStore:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def _connection(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=20)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if write:
                conn.commit()
        except Exception:
            if write:
                conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        with self._connection(write=True) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS lab_reviews (
                    date TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                    answers_json TEXT NOT NULL, tags_json TEXT NOT NULL,
                    completed INTEGER NOT NULL, deleted INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS lab_review_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL, revision INTEGER NOT NULL,
                    event TEXT NOT NULL, content_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS lab_review_history_date
                    ON lab_review_history(date, id);
                CREATE TABLE IF NOT EXISTS lab_plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL, strategy TEXT NOT NULL,
                    status TEXT NOT NULL, revision INTEGER NOT NULL,
                    deleted INTEGER NOT NULL DEFAULT 0,
                    content_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS lab_plans_status
                    ON lab_plans(deleted, status, id);
                CREATE TABLE IF NOT EXISTS lab_plan_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_id INTEGER NOT NULL, revision INTEGER NOT NULL,
                    event TEXT NOT NULL, detail TEXT NOT NULL,
                    content_json TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS lab_plan_history_plan
                    ON lab_plan_history(plan_id, id);
                CREATE TABLE IF NOT EXISTS lab_strategy (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    name TEXT NOT NULL, version TEXT NOT NULL,
                    content_json TEXT NOT NULL, updated_at TEXT NOT NULL
                );
            """)
            row = db.execute("SELECT 1 FROM lab_strategy WHERE id=1").fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO lab_strategy(id,name,version,content_json,updated_at) VALUES(1,?,?,?,?)",
                    (STRATEGY_DEFAULT["name"], STRATEGY_DEFAULT["version"],
                     _json(STRATEGY_DEFAULT), _utc_now()),
                )

    def strategy(self) -> dict[str, Any]:
        with self._connection() as db:
            row = db.execute("SELECT content_json FROM lab_strategy WHERE id=1").fetchone()
        return json.loads(row["content_json"]) if row else dict(STRATEGY_DEFAULT)

    def save_review(self, review_date: str, answers: list[str], tags: list[str], completed: bool) -> dict[str, Any]:
        day = _iso_date(review_date)
        now = _utc_now()
        with self._connection(write=True) as db:
            old = db.execute("SELECT * FROM lab_reviews WHERE date=?", (day,)).fetchone()
            revision = int(old["revision"]) + 1 if old else 1
            content = {
                "date": day, "answers": list(answers), "tags": list(tags),
                "completed": bool(completed), "revision": revision,
                "deleted": False, "updated_at": now,
            }
            db.execute(
                "INSERT INTO lab_reviews(date,revision,answers_json,tags_json,completed,deleted,updated_at) "
                "VALUES(?,?,?,?,?,0,?) ON CONFLICT(date) DO UPDATE SET revision=excluded.revision,"
                "answers_json=excluded.answers_json,tags_json=excluded.tags_json,"
                "completed=excluded.completed,deleted=0,updated_at=excluded.updated_at",
                (day, revision, _json(answers), _json(tags), int(completed), now),
            )
            db.execute(
                "INSERT INTO lab_review_history(date,revision,event,content_json,created_at) VALUES(?,?,?,?,?)",
                (day, revision, "saved" if old else "created", _json(content), now),
            )
        return content

    def delete_review(self, review_date: str) -> dict[str, Any] | None:
        day = _iso_date(review_date)
        now = _utc_now()
        with self._connection(write=True) as db:
            old = db.execute("SELECT * FROM lab_reviews WHERE date=? AND deleted=0", (day,)).fetchone()
            if old is None:
                return None
            revision = int(old["revision"]) + 1
            content = {
                "date": day, "answers": json.loads(old["answers_json"]),
                "tags": json.loads(old["tags_json"]), "completed": bool(old["completed"]),
                "revision": revision, "deleted": True, "updated_at": now,
            }
            db.execute("UPDATE lab_reviews SET revision=?,deleted=1,updated_at=? WHERE date=?",
                       (revision, now, day))
            db.execute(
                "INSERT INTO lab_review_history(date,revision,event,content_json,created_at) VALUES(?,?,?,?,?)",
                (day, revision, "deleted", _json(content), now),
            )
        return content

    def get_review(self, review_date: str, include_deleted: bool = False) -> dict[str, Any] | None:
        day = _iso_date(review_date)
        with self._connection() as db:
            row = db.execute("SELECT * FROM lab_reviews WHERE date=?", (day,)).fetchone()
        if row is None or (row["deleted"] and not include_deleted):
            return None
        return {
            "date": day, "answers": json.loads(row["answers_json"]),
            "tags": json.loads(row["tags_json"]), "completed": bool(row["completed"]),
            "revision": int(row["revision"]), "updated_at": row["updated_at"],
            "deleted": bool(row["deleted"]),
        }

    def reviews(self, search: str = "") -> list[dict[str, Any]]:
        with self._connection() as db:
            rows = db.execute("SELECT date FROM lab_reviews WHERE deleted=0 ORDER BY date DESC").fetchall()
        query = search.strip().casefold()
        results = []
        for row in rows:
            item = self.get_review(row["date"])
            if item and (not query or query in item["date"].casefold()
                         or any(query in answer.casefold() for answer in item["answers"])
                         or any(query in tag.casefold() for tag in item["tags"])):
                results.append(item)
        return results

    def review_history(self, review_date: str) -> list[dict[str, Any]]:
        day = _iso_date(review_date)
        with self._connection() as db:
            rows = db.execute(
                "SELECT id,date,revision,event,content_json,created_at FROM lab_review_history "
                "WHERE date=? ORDER BY id DESC", (day,),
            ).fetchall()
        return [{"id": int(row["id"]), "date": row["date"], "revision": int(row["revision"]),
                 "event": row["event"], "created_at": row["created_at"],
                 **json.loads(row["content_json"])} for row in rows]

    @staticmethod
    def _plan_from_row(row: sqlite3.Row) -> dict[str, Any]:
        content = json.loads(row["content_json"])
        content.update({"id": int(row["id"]), "revision": int(row["revision"]),
                        "created_at": row["created_at"], "updated_at": row["updated_at"]})
        return content

    def create_plan(self, values: Mapping[str, Any]) -> dict[str, Any]:
        now = _utc_now()
        with self._connection(write=True) as db:
            content = dict(values)
            content.update({"revision": 1, "deleted": False, "created_at": now, "updated_at": now})
            cur = db.execute(
                "INSERT INTO lab_plans(symbol,strategy,status,revision,deleted,content_json,created_at,updated_at) "
                "VALUES(?,?,?,1,0,?,?,?)",
                (content["symbol"], content["strategy"], content["status"], _json(content), now, now),
            )
            plan_id = int(cur.lastrowid)
            content["id"] = plan_id
            db.execute("UPDATE lab_plans SET content_json=? WHERE id=?", (_json(content), plan_id))
            db.execute(
                "INSERT INTO lab_plan_history(plan_id,revision,event,detail,content_json,created_at) "
                "VALUES(?,1,'created','用户创建计划',?,?)", (plan_id, _json(content), now),
            )
        return content

    def plan(self, plan_id: int, include_deleted: bool = False) -> dict[str, Any] | None:
        with self._connection() as db:
            row = db.execute("SELECT * FROM lab_plans WHERE id=?", (plan_id,)).fetchone()
        if row is None or (row["deleted"] and not include_deleted):
            return None
        return self._plan_from_row(row)

    def plans(self) -> list[dict[str, Any]]:
        with self._connection() as db:
            rows = db.execute("SELECT * FROM lab_plans WHERE deleted=0 ORDER BY id DESC").fetchall()
        return [self._plan_from_row(row) for row in rows]

    def update_plan(self, plan_id: int, values: Mapping[str, Any], detail: str = "用户修改计划") -> dict[str, Any] | None:
        now = _utc_now()
        with self._connection(write=True) as db:
            row = db.execute("SELECT * FROM lab_plans WHERE id=? AND deleted=0", (plan_id,)).fetchone()
            if row is None:
                return None
            content = json.loads(row["content_json"])
            content.update(dict(values))
            revision = int(row["revision"]) + 1
            content.update({"revision": revision, "deleted": False, "updated_at": now})
            db.execute(
                "UPDATE lab_plans SET symbol=?,strategy=?,status=?,revision=?,content_json=?,updated_at=? WHERE id=?",
                (content["symbol"], content["strategy"], content["status"], revision,
                 _json(content), now, plan_id),
            )
            db.execute(
                "INSERT INTO lab_plan_history(plan_id,revision,event,detail,content_json,created_at) "
                "VALUES(?,?, 'updated',?,?,?)", (plan_id, revision, detail, _json(content), now),
            )
        return content

    def set_plan_status(self, plan_id: int, status: str, detail: str) -> dict[str, Any] | None:
        current = self.plan(plan_id)
        if current is None or current["status"] == status:
            return current
        return self.update_plan(plan_id, {"status": status}, detail=detail)

    def delete_plan(self, plan_id: int) -> dict[str, Any] | None:
        now = _utc_now()
        with self._connection(write=True) as db:
            row = db.execute("SELECT * FROM lab_plans WHERE id=? AND deleted=0", (plan_id,)).fetchone()
            if row is None:
                return None
            content = json.loads(row["content_json"])
            revision = int(row["revision"]) + 1
            content.update({"revision": revision, "deleted": True, "updated_at": now})
            db.execute("UPDATE lab_plans SET revision=?,deleted=1,content_json=?,updated_at=? WHERE id=?",
                       (revision, _json(content), now, plan_id))
            db.execute(
                "INSERT INTO lab_plan_history(plan_id,revision,event,detail,content_json,created_at) "
                "VALUES(?,?,'deleted','用户删除计划',?,?)", (plan_id, revision, _json(content), now),
            )
        return content

    def plan_history(self, plan_id: int) -> list[dict[str, Any]]:
        with self._connection() as db:
            rows = db.execute(
                "SELECT id,plan_id,revision,event,detail,content_json,created_at FROM lab_plan_history "
                "WHERE plan_id=? ORDER BY id DESC", (plan_id,),
            ).fetchall()
        result = []
        for row in rows:
            content = json.loads(row["content_json"])
            result.append({
                **content,
                "id": int(row["id"]), "plan_id": int(row["plan_id"]),
                "revision": int(row["revision"]), "event": row["event"],
                "detail": row["detail"], "created_at": row["created_at"],
                "snapshot": content,
            })
        return result

