"""Single-site SQLite journal; raw revisions and vintages are append-only."""

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path


def now():
    return datetime.now(UTC)


def stamp():
    return now().isoformat()


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class Conflict(Exception):
    pass


class Store:
    def __init__(self, directory):
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.path = self.root / "energy.sqlite"
        self.lock = threading.RLock()
        with self.connect() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT OR IGNORE INTO metadata VALUES ('schema_version','1');
            CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY, hash TEXT, received TEXT, count INT);
            CREATE TABLE IF NOT EXISTS observations(
              id INTEGER PRIMARY KEY, feature TEXT, source TEXT, epoch TEXT,
              start TEXT, end TEXT, revision INT, received TEXT, payload TEXT,
              UNIQUE(source,epoch,feature,start,end,revision));
            CREATE INDEX IF NOT EXISTS observations_time ON observations(feature,start,end);
            CREATE TABLE IF NOT EXISTS quality_reviews(
              id INTEGER PRIMARY KEY, observation_id INT, quality TEXT, reason TEXT, created TEXT);
            CREATE TABLE IF NOT EXISTS configurations(revision INT PRIMARY KEY, hash TEXT, created TEXT, payload TEXT);
            CREATE TABLE IF NOT EXISTS weather(id TEXT PRIMARY KEY, received TEXT, configuration_hash TEXT, payload TEXT);
            CREATE TABLE IF NOT EXISTS forecasts(id TEXT PRIMARY KEY, created TEXT, configuration_hash TEXT, payload TEXT);
            CREATE INDEX IF NOT EXISTS forecasts_created ON forecasts(created);
            CREATE INDEX IF NOT EXISTS forecasts_issue ON forecasts(json_extract(payload,'$.issued_at'));
            CREATE INDEX IF NOT EXISTS weather_origin ON weather(COALESCE(json_extract(payload,'$.provider_available_at'),json_extract(payload,'$.provider_run_at'),received));
            CREATE INDEX IF NOT EXISTS weather_epoch_available ON weather(json_extract(payload,'$.source_epoch'), COALESCE(json_extract(payload,'$.provider_available_at'),received));
            CREATE INDEX IF NOT EXISTS weather_received ON weather(received);
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, kind TEXT, status TEXT, created TEXT, updated TEXT, payload TEXT);
            CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, created TEXT, action TEXT, payload TEXT);
            CREATE TABLE IF NOT EXISTS tokens(hash TEXT PRIMARY KEY, scope TEXT, created TEXT, revoked INT DEFAULT 0);
            CREATE TABLE IF NOT EXISTS sessions(hash TEXT PRIMARY KEY, csrf TEXT, expires TEXT);
            CREATE TABLE IF NOT EXISTS models(id TEXT PRIMARY KEY, created TEXT, status TEXT, payload TEXT);
            """)

    @contextmanager
    def connect(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=30)
            db.row_factory = sqlite3.Row
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

    def meta(self, key, value=None):
        with self.connect() as db:
            if value is not None:
                db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, json.dumps(value)))
            row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def audit(self, action, payload):
        with self.connect() as db:
            db.execute(
                "INSERT INTO audit(created,action,payload) VALUES (?,?,?)",
                (stamp(), action, json.dumps(payload)),
            )

    def configuration(self):
        with self.connect() as db:
            row = db.execute(
                "SELECT payload FROM configurations ORDER BY revision DESC LIMIT 1"
            ).fetchone()
            return json.loads(row[0]) if row else None

    def save_configuration(self, config):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT MAX(revision) FROM configurations").fetchone()
            revision = row[0] or 0
            if config.revision != revision:
                raise Conflict("Configuration changed; reload before saving")
            config = config.model_copy(update={"revision": revision + 1})
            payload = config.model_dump(mode="json")
            db.execute(
                "INSERT INTO configurations VALUES (?,?,?,?)",
                (config.revision, digest(payload), stamp(), json.dumps(payload)),
            )
            # Invalidate ALL old snapshots, including rollback to an old configuration.
            db.execute(
                "INSERT OR REPLACE INTO metadata VALUES ('invalidated_at',?)",
                (json.dumps(stamp()),),
            )
            db.execute(
                "INSERT INTO audit(created,action,payload) VALUES (?,?,?)",
                (stamp(), "configuration_saved", json.dumps({"revision": config.revision})),
            )
        return payload

    def ingest(self, batch):
        payload = batch.model_dump(mode="json")
        hashed = digest(payload)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT hash,count FROM batches WHERE id=?", (batch.batch_id,)
            ).fetchone()
            if previous:
                if previous[0] != hashed:
                    raise Conflict("Batch ID reused with different content")
                return {"accepted": previous[1], "duplicate": True}
            count = 0
            for obs in batch.observations:
                record = obs.model_dump(mode="json")
                key = (
                    obs.source,
                    obs.epoch,
                    obs.feature,
                    record["start"],
                    record["end"],
                    obs.revision,
                )
                old = db.execute(
                    "SELECT payload FROM observations WHERE source=? AND epoch=? AND feature=? AND start=? AND end=? AND revision=?",
                    key,
                ).fetchone()
                if old and json.loads(old[0]) != record:
                    raise Conflict("Observation revision is immutable; increment revision")
                if not old:
                    db.execute(
                        "INSERT INTO observations(feature,source,epoch,start,end,revision,received,payload) VALUES (?,?,?,?,?,?,?,?)",
                        (
                            obs.feature,
                            obs.source,
                            obs.epoch,
                            record["start"],
                            record["end"],
                            obs.revision,
                            stamp(),
                            json.dumps(record),
                        ),
                    )
                    count += 1
            db.execute(
                "INSERT INTO batches VALUES (?,?,?,?)", (batch.batch_id, hashed, stamp(), count)
            )
            # Fresh state requires a new plan; do not reuse an older budget after new SoC/limits.
            if any(
                o.feature in ("battery_soc", "export_limit", "battery_alarm")
                for o in batch.observations
            ):
                db.execute(
                    "INSERT OR REPLACE INTO metadata VALUES ('invalidated_at',?)",
                    (json.dumps(stamp()),),
                )
        return {"accepted": count, "duplicate": False}

    def observations(
        self,
        feature=None,
        limit=100000,
        before=None,
        after=None,
        as_of=None,
        minimum_seconds=0,
        recent_raw_after=None,
        sources=None,
    ):
        conditions, args = [], []
        if feature:
            conditions.append("o.feature=?")
            args.append(feature)
        if before:
            conditions.append("o.end<=?")
            args.append(before.isoformat())
        if after:
            conditions.append("julianday(o.end)>julianday(?)")
            args.append(after.isoformat())
        if sources:
            conditions.append("o.source IN (" + ",".join("?" for _ in sources) + ")")
            args.extend(sources)
        if minimum_seconds:
            duration = "(julianday(o.end)-julianday(o.start))*86400>=?"
            args.append(minimum_seconds - 0.01)
            if recent_raw_after:
                duration = "(" + duration + " OR julianday(o.start)>=julianday(?))"
                args.append(recent_raw_after.isoformat())
            conditions.append(duration)
        if as_of:
            conditions.append("o.received<=?")
            args.append(as_of.isoformat())
        latest = "o.revision=(SELECT MAX(x.revision) FROM observations x WHERE x.source=o.source AND x.epoch=o.epoch AND x.feature=o.feature AND x.start=o.start AND x.end=o.end"
        if as_of:
            latest += " AND x.received<=?"
            args.append(as_of.isoformat())
        conditions.append(latest + ")")
        args.append(-1 if limit is None else limit)
        with self.connect() as db:
            rows = db.execute(
                "SELECT o.* FROM observations o WHERE "
                + " AND ".join(conditions)
                + " ORDER BY o.start DESC LIMIT ?",
                args,
            ).fetchall()
            reviews = {
                r["observation_id"]: r
                for r in db.execute(
                    "SELECT * FROM quality_reviews"
                    + (" WHERE created<=?" if as_of else "")
                    + " ORDER BY id",
                    (as_of.isoformat(),) if as_of else (),
                ).fetchall()
            }
        result = []
        for row in reversed(rows):
            value = json.loads(row["payload"])
            value["id"] = row["id"]
            value["received"] = row["received"]
            if row["id"] in reviews:
                value["quality"] = reviews[row["id"]]["quality"]
                value["reasons"] = [reviews[row["id"]]["reason"]]
            result.append(value)
        return result

    def insert_document(self, table, identifier, payload, config_hash=None):
        if table not in ("weather", "forecasts", "models"):
            raise ValueError("Unsupported document")
        with self.connect() as db:
            if table == "models":
                db.execute(
                    "INSERT INTO models VALUES (?,?,?,?)",
                    (identifier, stamp(), "candidate", json.dumps(payload)),
                )
            else:
                db.execute(
                    f"INSERT INTO {table} VALUES (?,?,?,?)",
                    (identifier, stamp(), config_hash, json.dumps(payload)),
                )

    def documents(self, table, limit=100):
        if table not in ("weather", "forecasts", "models", "jobs"):
            raise ValueError("Unsupported document")
        with self.connect() as db:
            field = "received" if table == "weather" else "created"
            rows = db.execute(
                f"SELECT payload FROM {table} ORDER BY {field} DESC LIMIT ?", (limit,)
            ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def origins(self, table="forecasts", step_hours=48, limit=2000, config_hash=None):
        """Stream bounded separated origins across the full journal, not only recent pages."""
        if table not in ("forecasts", "weather"):
            raise ValueError("Unsupported journal")
        from datetime import timedelta

        field = (
            "COALESCE(json_extract(payload,'$.provider_available_at'),json_extract(payload,'$.provider_run_at'),received)"
            if table == "weather"
            else "json_extract(payload,'$.issued_at')"
        )
        cursor = "0001-01-01T00:00:00+00:00"
        for _ in range(limit):
            condition = " AND configuration_hash=?" if config_hash else ""
            arguments = [cursor] + ([config_hash] if config_hash else [])
            with self.connect() as db:
                row = db.execute(
                    f"SELECT {field} AS origin,payload FROM {table} WHERE {field}>?"
                    + condition
                    + f" ORDER BY {field} LIMIT 1",
                    arguments,
                ).fetchone()
            if row is None:
                return
            yield json.loads(row["payload"])
            cursor = (
                datetime.fromisoformat(row["origin"]) + timedelta(hours=step_hours)
            ).isoformat()

    def latest(self, table):
        values = self.documents(table, 1)
        return values[0] if values else None

    def backup(self, target):
        with self.connect() as source, sqlite3.connect(target) as destination:
            source.backup(destination)
