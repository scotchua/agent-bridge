"""Durable, conservative stage ownership and capacity-aware routing.
This module decides *who may own a stage*.  It does not execute work, call a
provider, or change the consultation bridge.  Capacity observations are an
explicit input and expire closed: missing, unknown, or stale observations can
never make a route eligible.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable


ROUTES = frozenset({"claude", "codex", "local"})
TERMINAL_STATES = frozenset({"complete", "blocked"})


class RoutingError(ValueError):
    """The requested ownership transition is unsafe or invalid."""


@dataclass(frozen=True)
class CapacityObservation:
    route: str
    observed_at: float
    fresh_until: float
    available: bool
    source: str


class StageRouter:
    """SQLite-backed assignment ledger with one owner per item/stage.

    Lease expiry is only a signal to reconcile.  It never releases ownership.
    A caller must make and record a liveness-safe inactive-owner assertion
    before the stage can be assigned again.
    """

    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.clock = clock
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    @contextmanager
    def _db(self):
        db = self._connect()
        try:
            yield db
        finally:
            db.close()

    def _init_db(self) -> None:
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("""CREATE TABLE IF NOT EXISTS capacity (
                route TEXT PRIMARY KEY, observed_at REAL NOT NULL,
                fresh_until REAL NOT NULL, available INTEGER NOT NULL,
                source TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS stages (
                item_id TEXT NOT NULL, stage TEXT NOT NULL, state TEXT NOT NULL,
                allowed_routes TEXT NOT NULL, preferred_routes TEXT NOT NULL,
                owner_id TEXT, owner_route TEXT, lease_until REAL,
                author_owner TEXT, author_route TEXT, is_review INTEGER NOT NULL,
                blocked_reason TEXT, revision INTEGER NOT NULL,
                updated_at REAL NOT NULL, PRIMARY KEY(item_id, stage))""")
            db.execute("""CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT NOT NULL,
                stage TEXT NOT NULL, at REAL NOT NULL, event TEXT NOT NULL,
                actor TEXT, detail TEXT NOT NULL)""")

    @staticmethod
    def _routes(values: Iterable[str]) -> tuple[str, ...]:
        result = tuple(dict.fromkeys(values))
        if not result or any(route not in ROUTES for route in result):
            raise RoutingError("routes_invalid")
        return result

    def observe_capacity(self, observation: CapacityObservation) -> None:
        if observation.route not in ROUTES:
            raise RoutingError("route_invalid")
        if (not observation.source or observation.fresh_until <= observation.observed_at
                or observation.observed_at > self.clock() + 1):
            raise RoutingError("capacity_observation_invalid")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""INSERT INTO capacity(route,observed_at,fresh_until,available,source)
                VALUES(?,?,?,?,?) ON CONFLICT(route) DO UPDATE SET
                observed_at=excluded.observed_at,fresh_until=excluded.fresh_until,
                available=excluded.available,source=excluded.source""",
                       (observation.route, observation.observed_at,
                        observation.fresh_until, int(observation.available),
                        observation.source))
            db.execute("COMMIT")

    def register(self, item_id: str, stage: str, *, allowed_routes: Iterable[str],
                 preferred_routes: Iterable[str] | None = None,
                 is_review: bool = False, author_owner: str | None = None,
                 author_route: str | None = None) -> dict:
        if not item_id or not stage:
            raise RoutingError("identity_invalid")
        allowed = self._routes(allowed_routes)
        preferred = self._routes(preferred_routes or allowed)
        if any(route not in allowed for route in preferred):
            raise RoutingError("preference_not_allowed")
        if is_review and (not author_owner or author_route not in ROUTES):
            raise RoutingError("review_author_required")
        now = self.clock()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("""INSERT INTO stages(item_id,stage,state,allowed_routes,
                    preferred_routes,author_owner,author_route,is_review,revision,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,0,?)""",
                           (item_id, stage, "pending", json.dumps(allowed),
                            json.dumps(preferred), author_owner, author_route,
                            int(is_review), now))
            except sqlite3.IntegrityError as exc:
                db.execute("ROLLBACK")
                raise RoutingError("stage_exists") from exc
            self._event(db, item_id, stage, "registered", None, {})
            db.execute("COMMIT")
        return self.get(item_id, stage)

    def _event(self, db: sqlite3.Connection, item_id: str, stage: str,
               event: str, actor: str | None, detail: dict) -> None:
        db.execute("INSERT INTO events(item_id,stage,at,event,actor,detail) VALUES(?,?,?,?,?,?)",
                   (item_id, stage, self.clock(), event, actor,
                    json.dumps(detail, sort_keys=True, separators=(",", ":"))))

    def _row(self, db: sqlite3.Connection, item_id: str, stage: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM stages WHERE item_id=? AND stage=?",
                         (item_id, stage)).fetchone()
        if row is None:
            raise RoutingError("stage_not_found")
        return row

    def _fresh_routes(self, db: sqlite3.Connection, now: float) -> set[str]:
        return {row["route"] for row in db.execute(
            "SELECT route FROM capacity WHERE available=1 AND observed_at<=? AND fresh_until>=?",
            (now + 1, now)).fetchall()}

    def assign(self, item_id: str, stage: str, *, owner_id: str,
               lease_seconds: float, expected_revision: int,
               paid_fallback: bool = False) -> dict:
        if paid_fallback:
            raise RoutingError("paid_fallback_forbidden")
        if not owner_id or lease_seconds <= 0:
            raise RoutingError("assignment_invalid")
        now = self.clock()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, item_id, stage)
            if row["revision"] != expected_revision:
                db.execute("ROLLBACK")
                raise RoutingError("revision_conflict")
            if row["state"] in TERMINAL_STATES:
                db.execute("ROLLBACK")
                raise RoutingError("stage_terminal")
            if row["state"] == "owned":
                if row["lease_until"] is not None and row["lease_until"] < now:
                    db.execute("""UPDATE stages SET state='reconcile_required',revision=revision+1,
                        updated_at=? WHERE item_id=? AND stage=?""", (now, item_id, stage))
                    self._event(db, item_id, stage, "lease_expired_reconcile_required",
                                owner_id, {"previous_owner": row["owner_id"]})
                    db.execute("COMMIT")
                    raise RoutingError("liveness_reconciliation_required")
                db.execute("ROLLBACK")
                raise RoutingError("already_owned")
            if row["state"] == "reconcile_required":
                db.execute("ROLLBACK")
                raise RoutingError("liveness_reconciliation_required")
            fresh = self._fresh_routes(db, now)
            allowed = json.loads(row["allowed_routes"])
            preferred = json.loads(row["preferred_routes"])
            eligible = [route for route in preferred if route in allowed and route in fresh]
            if row["is_review"]:
                eligible = [route for route in eligible if route != row["author_route"]]
            if not eligible:
                reason = "no_fresh_eligible_route"
                db.execute("""UPDATE stages SET state='blocked',blocked_reason=?,
                    revision=revision+1,updated_at=? WHERE item_id=? AND stage=?""",
                           (reason, now, item_id, stage))
                self._event(db, item_id, stage, "blocked", owner_id, {"reason": reason})
                db.execute("COMMIT")
                return self.get(item_id, stage)
            route = eligible[0]
            if row["is_review"] and owner_id == row["author_owner"]:
                db.execute("ROLLBACK")
                raise RoutingError("review_independence_required")
            db.execute("""UPDATE stages SET state='owned',owner_id=?,owner_route=?,lease_until=?,
                revision=revision+1,updated_at=? WHERE item_id=? AND stage=?""",
                       (owner_id, route, now + lease_seconds, now, item_id, stage))
            self._event(db, item_id, stage, "assigned", owner_id, {"route": route})
            db.execute("COMMIT")
        return self.get(item_id, stage)

    def renew(self, item_id: str, stage: str, *, owner_id: str,
              lease_seconds: float, expected_revision: int) -> dict:
        if lease_seconds <= 0:
            raise RoutingError("lease_invalid")
        now = self.clock()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, item_id, stage)
            if row["revision"] != expected_revision:
                db.execute("ROLLBACK")
                raise RoutingError("revision_conflict")
            if row["state"] != "owned" or row["owner_id"] != owner_id:
                db.execute("ROLLBACK")
                raise RoutingError("not_owner")
            db.execute("UPDATE stages SET lease_until=?,revision=revision+1,updated_at=? WHERE item_id=? AND stage=?",
                       (now + lease_seconds, now, item_id, stage))
            self._event(db, item_id, stage, "renewed", owner_id, {})
            db.execute("COMMIT")
        return self.get(item_id, stage)

    def reconcile_release(self, item_id: str, stage: str, *, actor: str,
                          expected_revision: int, owner_checked_inactive: bool,
                          liveness_evidence: str) -> dict:
        if not actor or not owner_checked_inactive or not liveness_evidence.strip():
            raise RoutingError("liveness_evidence_required")
        now = self.clock()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, item_id, stage)
            if row["revision"] != expected_revision:
                db.execute("ROLLBACK")
                raise RoutingError("revision_conflict")
            if row["state"] != "reconcile_required":
                db.execute("ROLLBACK")
                raise RoutingError("reconciliation_not_required")
            previous = {"owner_id": row["owner_id"], "owner_route": row["owner_route"],
                        "liveness_evidence": liveness_evidence}
            db.execute("""UPDATE stages SET state='pending',owner_id=NULL,owner_route=NULL,
                lease_until=NULL,revision=revision+1,updated_at=? WHERE item_id=? AND stage=?""",
                       (now, item_id, stage))
            self._event(db, item_id, stage, "reconciled_release", actor, previous)
            db.execute("COMMIT")
        return self.get(item_id, stage)

    def complete(self, item_id: str, stage: str, *, owner_id: str,
                 expected_revision: int) -> dict:
        now = self.clock()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, item_id, stage)
            if row["revision"] != expected_revision:
                db.execute("ROLLBACK")
                raise RoutingError("revision_conflict")
            if row["state"] != "owned" or row["owner_id"] != owner_id:
                db.execute("ROLLBACK")
                raise RoutingError("not_owner")
            db.execute("""UPDATE stages SET state='complete',lease_until=NULL,
                revision=revision+1,updated_at=? WHERE item_id=? AND stage=?""",
                       (now, item_id, stage))
            self._event(db, item_id, stage, "completed", owner_id, {})
            db.execute("COMMIT")
        return self.get(item_id, stage)

    def get(self, item_id: str, stage: str) -> dict:
        with self._db() as db:
            row = self._row(db, item_id, stage)
        result = dict(row)
        result["allowed_routes"] = json.loads(result["allowed_routes"])
        result["preferred_routes"] = json.loads(result["preferred_routes"])
        result["is_review"] = bool(result["is_review"])
        return result

    def report(self) -> dict:
        now = self.clock()
        with self._db() as db:
            states = {row["state"]: row["n"] for row in db.execute(
                "SELECT state,COUNT(*) AS n FROM stages GROUP BY state")}
            capacities = {}
            for row in db.execute("SELECT * FROM capacity ORDER BY route"):
                status = "available" if row["available"] and row["fresh_until"] >= now else "unavailable"
                if row["fresh_until"] < now:
                    status = "stale"
                capacities[row["route"]] = {"status": status, "observed_at": row["observed_at"],
                                            "fresh_until": row["fresh_until"], "source": row["source"]}
            blocked = [dict(row) for row in db.execute(
                "SELECT item_id,stage,blocked_reason FROM stages WHERE state='blocked' ORDER BY item_id,stage")]
        return {"states": states, "capacity": capacities, "blocked": blocked}
