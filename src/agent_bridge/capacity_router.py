"""Durable, conservative stage ownership and capacity-aware routing.

This module decides *who may own a stage*.  It does not execute work, call a
provider, or change the consultation bridge.  Capacity observations are an
explicit input and expire closed: missing, unknown, or stale observations can
never make a route eligible.

Two limits on what an observation can be, both added because an adversarial
review found the original table was evidence in name only:

* **Only a trusted writer counts.**  ``trusted`` is set by the code path that
  records the observation, never read from the observation itself, and only a
  trusted row can make a route eligible.  An untrusted row is stored and
  reported, so an operator can see what was claimed, and ignored when routing.
  The column defaults to untrusted, so rows an earlier version accepted from a
  model-facing tool stop counting the moment this version runs.
* **No observation outlasts the work it would authorise.**  A window longer
  than :data:`MAX_FRESHNESS_SECONDS` is refused rather than clamped: an
  observation that claims a year is a configuration statement wearing
  evidence's clothes, and the operator's policy file is where a standing
  statement belongs.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping


ROUTES = frozenset({"claude", "codex", "local"})
TERMINAL_STATES = frozenset({"complete", "blocked"})

#: The longest window one capacity observation may claim.  Matched to the
#: routing lease, because nothing else in this system claims validity for
#: longer than that and a capacity claim has no reason to be the exception.
MAX_FRESHNESS_SECONDS = 4 * 3600.0

#: Source recorded when the gate hook notices that the client calling it is,
#: by definition, running.  Named here rather than in ``autodecide`` because
#: :func:`capacity_fingerprint` has to tell that row apart from every other
#: one and must not import the module that writes it.
PRESENCE_SOURCE = "gate-hook:client-present"

#: What :func:`capacity_fingerprint` returns when no route is eligible.  A
#: word rather than an empty string, so a receipt shows the state was computed
#: rather than missing.
CAPACITY_NONE = "none"


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
                source TEXT NOT NULL,
                trusted INTEGER NOT NULL DEFAULT 0)""")
            # An installed ledger predates the column.  Adding it with a
            # default of 0 is the migration and also the fix: every row an
            # earlier version accepted through the model-facing tool becomes
            # untrusted, so upgrading does not inherit a model's claim about
            # who was available.
            if "trusted" not in {row["name"] for row in
                                 db.execute("PRAGMA table_info(capacity)")}:
                db.execute("ALTER TABLE capacity ADD COLUMN "
                           "trusted INTEGER NOT NULL DEFAULT 0")
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

    def observe_capacity(self, observation: CapacityObservation, *,
                         trusted: bool) -> None:
        """Record one observation.  ``trusted`` says whether it may route work.

        ``trusted`` is keyword-only and has no default on purpose.  A trust
        flag with a default is a trust flag somebody forgets, so every writer
        states its own provenance at the call site.  It is never taken from
        the observation, so a caller that can only supply an observation
        cannot make itself trusted by naming a convincing ``source``.
        """
        if observation.route not in ROUTES:
            raise RoutingError("route_invalid")
        if (not observation.source or observation.fresh_until <= observation.observed_at
                or observation.observed_at > self.clock() + 1):
            raise RoutingError("capacity_observation_invalid")
        if observation.fresh_until - observation.observed_at > MAX_FRESHNESS_SECONDS:
            raise RoutingError("capacity_freshness_excessive")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""INSERT INTO capacity(route,observed_at,fresh_until,available,source,trusted)
                VALUES(?,?,?,?,?,?) ON CONFLICT(route) DO UPDATE SET
                observed_at=excluded.observed_at,fresh_until=excluded.fresh_until,
                available=excluded.available,source=excluded.source,
                trusted=excluded.trusted""",
                       (observation.route, observation.observed_at,
                        observation.fresh_until, int(observation.available),
                        observation.source, int(bool(trusted))))
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
        """Routes a *trusted* writer currently reports as available.

        ``trusted=1`` is part of the WHERE clause rather than a check on the
        way out, so there is no path through this class where an untrusted row
        reaches an assignment decision.
        """
        return {row["route"] for row in db.execute(
            "SELECT route FROM capacity WHERE available=1 AND trusted=1 "
            "AND observed_at<=? AND fresh_until>=?",
            (now + 1, now)).fetchall()}

    def retract_capacity(self, route: str, *, source: str) -> bool:
        """Remove this route's row, but only if ``source`` is what wrote it.

        The operator withdrawing a declaration has to take effect at once, for
        the same reason a policy edit does: a control surface that keeps
        working for another quarter of an hour is one the operator cannot
        trust.  Matching on ``source`` is what makes the withdrawal safe: it
        deletes the evidence derived from the declaration and cannot touch a
        different writer's row, so a peer that really has been running keeps
        its own first-hand presence.
        """
        if route not in ROUTES:
            raise RoutingError("route_invalid")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute("DELETE FROM capacity WHERE route=? AND source=?",
                                (route, source))
            db.execute("COMMIT")
            return cursor.rowcount > 0

    def capacity_rows(self) -> list[dict]:
        """Every capacity row as a plain mapping, for the fingerprint."""
        with self._db() as db:
            return [dict(row) for row in
                    db.execute("SELECT * FROM capacity ORDER BY route")]

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
                elif not row["trusted"]:
                    # Graded before availability is reported, so a caller that
                    # only looks for "available" never counts it.  Reported
                    # rather than dropped: an operator should be able to see
                    # that something claimed a route was up.
                    status = "untrusted"
                capacities[row["route"]] = {"status": status, "observed_at": row["observed_at"],
                                            "fresh_until": row["fresh_until"], "source": row["source"],
                                            "trusted": bool(row["trusted"])}
            blocked = [dict(row) for row in db.execute(
                "SELECT item_id,stage,blocked_reason FROM stages WHERE state='blocked' ORDER BY item_id,stage")]
        return {"states": states, "capacity": capacities, "blocked": blocked}


def capacity_fingerprint(rows: Iterable[Mapping], now: float) -> str:
    """A digest of the capacity a routing decision actually depended on.

    Recorded in every automatic receipt for the same reason the policy
    fingerprint is: a decision made when the peer was unavailable should be
    re-made when it becomes available, not whenever the receipt happens to
    expire four hours later.

    **Route names only, no timestamps, and nothing about who is asking.**
    Both halves of that are load-bearing, and the second was learned the hard
    way.

    Names only, because the gate refreshes its own presence row on every hook
    call.  A digest over the rows themselves changes every call, so every call
    re-decides: measured at eight decisions where one was correct.  A set of
    names does not move when a timestamp does, which is the whole fix.

    Nothing about who is asking, because **a receipt is one shared
    per-repository artifact**.  An earlier version subtracted the asking
    client's own presence row, on the reasoning that a client's own presence
    is not news to itself.  That made the digest client-relative, so the two
    clients computed different values from the identical ledger, each found
    the other's receipt overtaken, and each re-decided it to route the work to
    the other.  Both were then permanently denied, each holding an instruction
    to dispatch to the other.  A livelock, not churn, and worse than the
    staleness the digest exists to fix.  Anything compared against a shared
    artifact has to be computed the same way by everyone who compares it.

    Pure, and shared: the gate hook reads the table read-only and
    ``autodecide`` reads it through the router, and both call this, so the
    value stamped in a receipt and the value checked against it cannot drift
    apart through two implementations.
    """
    eligible = sorted(
        str(row["route"]) for row in rows
        if row.get("available") and row.get("trusted")
        and float(row.get("observed_at", 0.0)) <= now + 1
        and float(row.get("fresh_until", 0.0)) >= now)
    return ",".join(eligible) if eligible else CAPACITY_NONE
