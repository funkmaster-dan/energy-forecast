import json
import os
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field

from . import security
from .calendars import features, import_events
from .composition import dt
from .forecast import canonical, leased
from .home_assistant import BridgeMetadata, ConnectionRequest
from .jobs import Worker, enqueue
from .schemas import Batch, Configuration, ForecastResponse, StrictModel
from .store import Conflict, Store, now, stamp
from .tariff import boundaries, resolve, validate


class Credentials(StrictModel):
    password: str = Field(min_length=1, max_length=256)
    setup_key: str = Field(default="", max_length=100)


class TokenRequest(StrictModel):
    scope: Literal["integration", "viewer", "admin"] = "integration"


class JobRequest(StrictModel):
    kind: Literal[
        "weather",
        "forecast",
        "train",
        "pv_calibrate",
        "calibrate",
        "weather_history",
        "weather_run",
        "export_history",
        "bias",
        "adapt",
    ]
    options: dict = Field(default_factory=dict)


class Review(StrictModel):
    quality: Literal["valid", "suspect", "invalid"]
    reason: str = Field(min_length=3, max_length=200)


class CalendarImport(StrictModel):
    format: Literal["json", "csv", "ics"]
    content: str = Field(max_length=500000)
    source: str = Field(max_length=200)
    version: str = Field(max_length=100)
    kind: Literal["school_holiday", "public_holiday", "household", "pupil_free"]


def create_app(directory=None, start_worker=True):
    store = Store(directory or os.environ.get("ENERGY_DATA_DIR", "/data"))
    security.bootstrap(store)
    worker = Worker(store)
    failures = defaultdict(deque)

    @asynccontextmanager
    async def lifespan(app):
        if start_worker:
            worker.start()
        yield
        if start_worker:
            worker.shutdown()

    app = FastAPI(title="Energy Forecast", version="0.2.0", lifespan=lifespan)
    app.state.store = store

    @app.middleware("http")
    async def boundaries_middleware(request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            public_origin = os.environ.get("ENERGY_PUBLIC_ORIGIN", "").rstrip("/")
            expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
            if origin and origin not in (expected, public_origin):
                return JSONResponse({"detail": "Untrusted origin"}, status_code=403)
            if int(request.headers.get("content-length", "0")) > 8_000_000:
                return JSONResponse({"detail": "Payload too large"}, status_code=413)
            data = bytearray()
            async for chunk in request.stream():
                data.extend(chunk)
                if len(data) > 8_000_000:
                    return JSONResponse({"detail": "Payload too large"}, status_code=413)
            request._body = bytes(data)
        result = await call_next(request)
        result.headers["X-Content-Type-Options"] = "nosniff"
        result.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        result.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith(("/v1", "/auth")):
            result.headers["Cache-Control"] = "no-store"
        return result

    @app.exception_handler(Conflict)
    async def conflict_handler(request, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready():
        with store.connect() as db:
            db.execute("SELECT 1")
        return {"status": "ok", "export_readiness": "separate", "version": "0.2.0"}

    @app.get("/auth/status")
    def auth_status(request: Request):
        signed_in, csrf = False, None
        try:
            security.authorize(store, request)
            signed_in = True
            with store.connect() as db:
                row = db.execute(
                    "SELECT csrf FROM sessions WHERE hash=?",
                    (security.token_hash(request.cookies.get("energy_session", "")),),
                ).fetchone()
            csrf = row[0] if row else None
        except HTTPException:
            pass
        return {
            "setup_required": store.meta("admin") is None,
            "signed_in": signed_in,
            "csrf": csrf,
            "site_id": "home",
        }

    @app.post("/auth/setup")
    def setup(payload: Credentials, request: Request):
        throttle(request)
        security.setup(store, payload.password, payload.setup_key)
        store.audit("administrator_setup", {})
        return {"ok": True}

    def throttle(request):
        host = request.client.host if request.client else "local"
        attempts = failures[host]
        while attempts and attempts[0] < time.monotonic() - 60:
            attempts.popleft()
        if len(attempts) >= 10:
            raise HTTPException(429, "Try again in one minute")
        attempts.append(time.monotonic())

    @app.post("/auth/login")
    def login(payload: Credentials, request: Request, response: Response):
        throttle(request)
        session, csrf = security.login(store, payload.password)
        response.set_cookie(
            "energy_session",
            session,
            httponly=True,
            samesite="strict",
            secure=os.environ.get("ENERGY_COOKIE_SECURE", "false").lower() == "true",
            max_age=43200,
            path="/",
        )
        return {"csrf": csrf}

    @app.post("/auth/logout")
    def logout(request: Request, response: Response):
        security.authorize(store, request)
        with store.connect() as db:
            db.execute(
                "DELETE FROM sessions WHERE hash=?",
                (security.token_hash(request.cookies.get("energy_session", "")),),
            )
        response.delete_cookie("energy_session")
        return {"ok": True}

    def read(request: Request):
        return security.authorize(store, request, "read")

    def admin(request: Request):
        return security.authorize(store, request, "admin")

    def ingest_auth(request: Request):
        return security.authorize(store, request, "ingest")

    def site(site_id: str):
        if site_id != "home":
            raise HTTPException(404, "One site per instance; site ID is home")

    router = APIRouter(prefix="/v1/sites/{site_id}", dependencies=[Depends(site)])

    @router.post("/home-assistant/connect", dependencies=[Depends(admin)])
    def connect_home_assistant(payload: ConnectionRequest, request: Request):
        import httpx

        from .home_assistant import connect

        token = security.mint(store, "integration")
        origin = os.environ.get("ENERGY_PUBLIC_ORIGIN") or str(request.base_url).rstrip("/")
        try:
            result = connect(store, payload, origin, token)
            result["integration_token"] = token if result["pairing"] != "paired" else None
            return result
        except (ValueError, httpx.HTTPError, KeyError) as error:
            with store.connect() as db:
                db.execute(
                    "UPDATE tokens SET revoked=1 WHERE hash=?", (security.token_hash(token),)
                )
            raise HTTPException(
                422, "Home Assistant connection failed. Check its URL, token and reachability."
            ) from error

    @router.get("/home-assistant", dependencies=[Depends(admin)])
    def home_assistant_status():
        return store.meta("ha_bridge")

    @router.post("/bridge/metadata", dependencies=[Depends(ingest_auth)])
    def bridge_metadata(payload: BridgeMetadata):

        metadata = payload.model_dump(mode="json")
        previous = store.meta("ha_bridge") or {}
        store.meta(
            "ha_bridge", {**previous, **metadata, "connected_at": stamp(), "pairing": "paired"}
        )
        return {"ok": True}

    @router.get("/bridge/inputs", dependencies=[Depends(read)])
    def bridge_inputs():
        return store.meta("bridge_inputs") or []

    @router.put("/bridge/inputs", dependencies=[Depends(admin)])
    def bridge_inputs_save(payload: list[dict]):
        if len(payload) > 100:
            raise HTTPException(422, "Select at most 100 sources")
        allowed = {
            "source",
            "feature",
            "unit",
            "kind",
            "boundary",
            "epoch",
            "history",
            "history_period",
            "interval_seconds",
        }
        for source in payload:
            if set(source) - allowed or not {
                "source",
                "feature",
                "unit",
                "kind",
                "boundary",
                "epoch",
            } <= set(source):
                raise HTTPException(422, "Invalid selected source")
        from .schemas import Observation

        for source in payload:
            try:
                Observation.model_validate(
                    {
                        key: source[key]
                        for key in ("source", "feature", "unit", "kind", "boundary", "epoch")
                    }
                    | {
                        "start": stamp(),
                        "end": (now() + timedelta(seconds=1)).isoformat(),
                        "value": None,
                    }
                )
            except ValueError as error:
                raise HTTPException(422, "A selected sensor has incompatible metadata") from error
        store.meta("bridge_inputs", payload)
        return {"ok": True}

    @router.get("/configuration", dependencies=[Depends(admin)])
    def get_configuration():
        return store.configuration()

    @router.put("/configuration", dependencies=[Depends(admin)])
    def put_configuration(payload: Configuration):
        try:
            validate(payload)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        result = store.save_configuration(payload)
        enqueue(store, "weather")
        enqueue(store, "forecast")
        if payload.banks:
            enqueue(store, "pv_calibrate")
        return result

    @router.post("/observations", dependencies=[Depends(ingest_auth)])
    def ingest(payload: Batch):
        if not store.configuration():
            raise HTTPException(409, "Complete site configuration first")
        result = store.ingest(payload)
        if not result["duplicate"]:
            enqueue(store, "forecast")
        return {"batch_id": payload.batch_id, **result}

    @router.get("/observations", dependencies=[Depends(admin)])
    def raw_observations(feature: str | None = None, limit: int = 500):
        return store.observations(feature, max(1, min(limit, 5000)))

    @router.get("/quality", dependencies=[Depends(admin)])
    def quality(days: int = 90):
        from .quality import report

        config = store.configuration()
        return (
            report(store, Configuration.model_validate(config), max(1, min(days, 366)))
            if config
            else {"profiles": []}
        )

    @router.get("/composition", dependencies=[Depends(admin)])
    def composition(feature: str = "household_load"):
        config = store.configuration()
        return canonical(store, Configuration.model_validate(config), feature) if config else []

    @router.put("/observations/{observation_id}/review", dependencies=[Depends(admin)])
    def review(observation_id: int, payload: Review):
        with store.connect() as db:
            if not db.execute(
                "SELECT 1 FROM observations WHERE id=?", (observation_id,)
            ).fetchone():
                raise HTTPException(404, "Observation not found")
            db.execute(
                "INSERT INTO quality_reviews(observation_id,quality,reason,created) VALUES (?,?,?,?)",
                (observation_id, payload.quality, payload.reason, stamp()),
            )
        store.meta("invalidated_at", stamp())
        store.audit(
            "quality_review", {"observation_id": observation_id, "quality": payload.quality}
        )
        return {"ok": True}

    @router.get("/forecast/latest", dependencies=[Depends(read)], response_model=ForecastResponse)
    def latest():
        return leased(store, store.latest("forecasts"))

    @router.get("/forecasts", dependencies=[Depends(read)])
    def archives():
        return [
            {
                "forecast_id": f["forecast_id"],
                "issued_at": f["issued_at"],
                "status": f["status"],
                "totals": f["totals"],
            }
            for f in store.documents("forecasts", 100)
        ]

    @router.get(
        "/forecasts/{forecast_id}", dependencies=[Depends(read)], response_model=ForecastResponse
    )
    def replay(forecast_id: str):
        with store.connect() as db:
            row = db.execute("SELECT payload FROM forecasts WHERE id=?", (forecast_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Forecast not found")
        value = json.loads(row[0])
        value["historical_replay"] = True
        with store.connect() as db:
            original_config = db.execute(
                "SELECT payload FROM configurations WHERE revision=?",
                (value["configuration_revision"],),
            ).fetchone()
        if original_config:
            config = Configuration.model_validate(json.loads(original_config[0]))
            actuals = {}
            for feature in ("household_load", "pv_generation"):
                actuals[feature] = {
                    dt(r["start"]): r
                    for r in canonical(store, config, feature)
                    if r["energy_kwh"] is not None and dt(r["end"]) <= now()
                }
            observed = []
            for interval in value["series"]:
                start = dt(interval["start"]).replace(minute=0, second=0, microsecond=0)
                row = {
                    "start": interval["start"],
                    "end": interval["end"],
                    "load_kw": None,
                    "pv_kw": None,
                    "quality_flags": [],
                }
                for feature, field in (("household_load", "load_kw"), ("pv_generation", "pv_kw")):
                    record = actuals[feature].get(start)
                    if record and dt(record["end"]) >= dt(interval["end"]):
                        row[field] = record["energy_kwh"] / (
                            (dt(record["end"]) - dt(record["start"])).total_seconds() / 3600
                        )
                        row["quality_flags"].extend(record["reasons"])
                observed.append(row)
            value["observed_series"] = observed
        return leased(store, value)

    @router.get("/weather/status", dependencies=[Depends(read)])
    def weather_status():
        document = store.latest("weather")
        return (
            {
                key: document[key]
                for key in ("id", "received_at", "provider", "kind", "model", "provider_run_at")
            }
            if document
            else {"status": "missing"}
        )

    @router.get("/diagnostics", dependencies=[Depends(read)])
    def diagnostics():
        with store.connect() as db:
            count = db.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
            newest = db.execute("SELECT MAX(received) FROM observations").fetchone()[0]
            job_counts = {
                r[0]: r[1] for r in db.execute("SELECT status,COUNT(*) FROM jobs GROUP BY status")
            }
        forecast = leased(store, store.latest("forecasts"))
        return {
            "version": "0.2.0",
            "schema_version": 1,
            "observations": count,
            "last_ingestion": newest,
            "status": forecast["status"],
            "reason_codes": forecast["export_plan"]["reason_codes"],
            "jobs": job_counts,
            "battery_calibration": store.meta("battery_calibration"),
            "pv_bank_calibration": store.meta("pv_bank_calibration"),
            "calibration": store.meta("calibration") or {"status": "insufficient_data"},
            "active_model": store.meta("active_model"),
            "learning_paused": (store.configuration() or {}).get("learning_paused", False),
            "recent_adaptation": store.meta("recent_bias"),
            "drift": store.meta("drift"),
            "redacted": True,
        }

    @router.get("/jobs", dependencies=[Depends(admin)])
    def jobs():
        return store.documents("jobs", 100)

    @router.post("/jobs", dependencies=[Depends(admin)])
    def create_job(payload: JobRequest):
        if not store.configuration():
            raise HTTPException(409, "Complete site configuration first")
        if payload.options and payload.kind not in ("weather_history", "weather_run"):
            raise HTTPException(422, "This job does not accept options")
        if payload.kind == "weather_history":
            from datetime import date

            try:
                if set(payload.options) != {"start", "end"}:
                    raise ValueError("start and end required")
                start, end = (
                    date.fromisoformat(payload.options["start"]),
                    date.fromisoformat(payload.options["end"]),
                )
                if not 0 <= (end - start).days <= 31:
                    raise ValueError("Use at most 31 days per page")
            except (ValueError, TypeError) as error:
                raise HTTPException(422, str(error)) from error
        if payload.kind == "weather_run":
            try:
                if set(payload.options) - {"run", "available_at"} or "run" not in payload.options:
                    raise ValueError("Provide run and optional documented available_at")
                run = dt(payload.options["run"])
                if run.tzinfo is None or run.minute or run.second or run.microsecond or run > now():
                    raise ValueError("run must be a past UTC initialization hour")
                if payload.options.get("available_at"):
                    available = dt(payload.options["available_at"])
                    if available.tzinfo is None or not run < available <= now():
                        raise ValueError("Documented availability must follow initialization")
            except (ValueError, TypeError) as error:
                raise HTTPException(422, str(error)) from error
        try:
            return enqueue(store, payload.kind, payload.options)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @router.delete("/jobs/{job_id}", dependencies=[Depends(admin)])
    def cancel(job_id: str):
        try:
            return worker.cancel(job_id)
        except ValueError as error:
            raise HTTPException(404, str(error)) from error

    @router.get("/models", dependencies=[Depends(admin)])
    def models():
        return store.documents("models", 100)

    @router.post("/models/{model_id}/activate", dependencies=[Depends(admin)])
    def activate(model_id: str):
        from .learning import activate_model

        try:
            return activate_model(store, model_id)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @router.post("/models/rollback", dependencies=[Depends(admin)])
    def rollback():
        previous = store.meta("previous_model")
        if not previous:
            raise HTTPException(409, "No previous validated model")
        from .learning import activate_model

        return activate_model(store, previous)

    @router.post("/tokens", dependencies=[Depends(admin)])
    def token(payload: TokenRequest):
        value = security.mint(store, payload.scope)
        store.audit("token_created", {"scope": payload.scope})
        return {
            "token": value,
            "scope": payload.scope,
            "site_id": "home",
            "id": security.token_hash(value)[:12],
        }

    @router.get("/tokens", dependencies=[Depends(admin)])
    def tokens():
        with store.connect() as db:
            return [
                {
                    "id": r["hash"][:12],
                    "scope": r["scope"],
                    "created": r["created"],
                    "revoked": bool(r["revoked"]),
                }
                for r in db.execute("SELECT * FROM tokens")
            ]

    @router.delete("/tokens/{token_id}", dependencies=[Depends(admin)])
    def revoke(token_id: str):
        with store.connect() as db:
            matches = db.execute(
                "SELECT hash FROM tokens WHERE substr(hash,1,12)=?", (token_id,)
            ).fetchall()
            if len(matches) != 1:
                raise HTTPException(404, "Token not found")
            db.execute("UPDATE tokens SET revoked=1 WHERE hash=?", (matches[0][0],))
        return {"ok": True}

    @router.get("/tariff/preview", dependencies=[Depends(admin)])
    def tariff_preview(day: str):
        config = Configuration.model_validate(store.configuration())
        try:
            local = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo(config.timezone))
            start, end = (
                local.astimezone(now().tzinfo),
                (local + timedelta(days=1)).astimezone(now().tzinfo),
            )
            points = boundaries(config, start, end)
            return [
                {"start": a.isoformat(), "end": b.isoformat(), "tariff": resolve(config, a)}
                for a, b in zip(points, points[1:])
            ]
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @router.get("/calendars/preview", dependencies=[Depends(admin)])
    def calendar_preview(day: str):
        config = Configuration.model_validate(store.configuration())
        local = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo(config.timezone))
        return [
            {
                "date": (local + timedelta(days=i)).date().isoformat(),
                **features(config, local + timedelta(days=i)),
            }
            for i in range(31)
        ]

    @router.post("/calendars/import", dependencies=[Depends(admin)])
    def calendar_import(payload: CalendarImport):
        try:
            return import_events(
                payload.content,
                payload.format,
                payload.source,
                payload.version,
                payload.kind,
                (store.configuration() or {}).get("timezone", "Australia/Adelaide"),
            )
        except (ValueError, KeyError) as error:
            raise HTTPException(422, str(error)) from error

    @router.post("/backup", dependencies=[Depends(admin)])
    def backup():
        # Fixed paths; never accept a filesystem path from the browser.
        import tarfile

        directory = store.root / "backups"
        directory.mkdir(exist_ok=True)
        identifier = secrets.token_hex(8)
        database = directory / f"{identifier}.sqlite"
        target = directory / f"{identifier}.tar.gz"
        store.backup(database)
        with tarfile.open(target, "w:gz") as archive:
            archive.add(database, arcname="energy.sqlite")
            for name in ("models", "history"):
                path = store.root / name
                if path.exists():
                    archive.add(path, arcname=name)
        database.unlink()
        store.audit("backup_created", {"id": identifier})
        return {"id": identifier, "download": f"/v1/sites/home/backups/{identifier}"}

    @router.get("/backups/{backup_id}", dependencies=[Depends(admin)])
    def download_backup(backup_id: str):
        if len(backup_id) != 16 or any(c not in "0123456789abcdef" for c in backup_id):
            raise HTTPException(404)
        path = store.root / "backups" / f"{backup_id}.tar.gz"
        if not path.exists():
            raise HTTPException(404)
        return FileResponse(path, filename=f"energy-forecast-{backup_id}.tar.gz")

    app.include_router(router)
    web = Path(os.environ.get("ENERGY_WEB_DIR", str(Path(__file__).parents[2] / "web" / "dist")))
    if web.exists():
        app.mount("/", StaticFiles(directory=web, html=True), name="web")
    return app
