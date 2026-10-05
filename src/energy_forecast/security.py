import hashlib
import hmac
import os
import secrets
from datetime import timedelta

from fastapi import HTTPException, Request

from .store import now, stamp


def token_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(value, salt):
    return hashlib.scrypt(value.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()


def bootstrap(store):
    path = store.root / "setup-key"
    if not store.meta("admin") and not path.exists():
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as output:
            output.write(secrets.token_urlsafe(32))


def setup(store, password, key):
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM metadata WHERE key='admin'").fetchone():
            raise HTTPException(409, "Administrator already configured")
        path = store.root / "setup-key"
        if not path.exists() or not hmac.compare_digest(path.read_text().strip(), key):
            raise HTTPException(403, "Read the local setup key from the data directory")
        if len(password) < 12 or len(password) > 256:
            raise HTTPException(422, "Password must be 12–256 characters")
        import json

        salt = secrets.token_hex(16)
        db.execute(
            "INSERT INTO metadata VALUES ('admin',?)",
            (json.dumps({"salt": salt, "hash": password_hash(password, salt)}),),
        )
    path.unlink(missing_ok=True)


def login(store, password):
    admin = store.meta("admin")
    if not admin or not hmac.compare_digest(password_hash(password, admin["salt"]), admin["hash"]):
        raise HTTPException(401, "Invalid password")
    session, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with store.connect() as db:
        db.execute("DELETE FROM sessions WHERE expires<?", (stamp(),))
        db.execute(
            "INSERT INTO sessions VALUES (?,?,?)",
            (token_hash(session), csrf, (now() + timedelta(hours=12)).isoformat()),
        )
    return session, csrf


def authorize(store, request: Request, scope="read"):
    authorization = request.headers.get("authorization", "")
    if authorization.startswith("Bearer "):
        with store.connect() as db:
            row = db.execute(
                "SELECT scope FROM tokens WHERE hash=? AND revoked=0",
                (token_hash(authorization[7:]),),
            ).fetchone()
        if row and (
            row[0] == "admin"
            or (row[0] == "integration" and scope in ("read", "ingest"))
            or (row[0] == "viewer" and scope == "read")
        ):
            return "token"
        raise HTTPException(403, "Invalid token or insufficient scope")
    session = request.cookies.get("energy_session", "")
    with store.connect() as db:
        row = db.execute(
            "SELECT csrf,expires FROM sessions WHERE hash=?", (token_hash(session),)
        ).fetchone()
    if not row or row["expires"] <= stamp():
        raise HTTPException(401, "Sign in required")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        csrf = request.headers.get("x-csrf-token", "")
        if not hmac.compare_digest(row["csrf"], csrf):
            raise HTTPException(403, "CSRF token required")
    return "session"


def mint(store, scope):
    value = secrets.token_urlsafe(40)
    with store.connect() as db:
        db.execute(
            "INSERT INTO tokens(hash,scope,created) VALUES (?,?,?)",
            (token_hash(value), scope, stamp()),
        )
    return value
