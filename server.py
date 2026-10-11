"""Serve Daybook locally and persist transactions in SQLite."""

from __future__ import annotations

import json
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import unquote, urlsplit


HOST = "127.0.0.1"
PORT = int(os.environ.get("DAYBOOK_PORT", "8765"))
DATABASE_PATH = Path(os.environ.get("DAYBOOK_DB_PATH", Path(__file__).with_name("daybook.sqlite3")))
MAX_REQUEST_BYTES = 256_000
SESSION_COOKIE = "daybook_session"
SESSION_LIFETIME = 30 * 24 * 60 * 60
INVITE_LIFETIME = 7 * 24 * 60 * 60
PASSWORD_ITERATIONS = 600_000
USERNAME_PATTERN = re.compile(r"^[a-z0-9._-]{3,32}$")
CATEGORIES = {
    "expense": {"Food", "Transport", "Utilities", "Fun", "Other"},
    "income": {"Salary", "Freelance", "Business", "Gift", "Other"},
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_salt TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf_token TEXT NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_expiry_idx ON sessions (expires_at);
CREATE TABLE IF NOT EXISTS invites (
    code_hash TEXT PRIMARY KEY,
    created_by TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at INTEGER NOT NULL,
    used_at INTEGER
);
CREATE TABLE IF NOT EXISTS transactions (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL CHECK (type IN ('expense', 'income')),
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    category TEXT NOT NULL,
    date TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    user_id TEXT REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@contextmanager
def connect_database() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database() -> None:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect_database() as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(SCHEMA)
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(transactions)")}
        if "user_id" not in columns:
            connection.execute("ALTER TABLE transactions ADD COLUMN user_id TEXT REFERENCES users(id) ON DELETE CASCADE")
        connection.execute("CREATE INDEX IF NOT EXISTS transactions_user_date_idx ON transactions (user_id, date DESC, created_at DESC)")


def owner_setup_code() -> str | None:
    with connect_database() as connection:
        if connection.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            return None
        row = connection.execute("SELECT value FROM app_state WHERE key = 'owner-setup-code'").fetchone()
        if row:
            return str(row["value"])
        setup_code = secrets.token_urlsafe(18)
        connection.execute("INSERT INTO app_state (key, value) VALUES ('owner-setup-code', ?)", (setup_code,))
        return setup_code


def transaction_from_row(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": row["id"],
        "type": row["type"],
        "amount": row["amount_cents"] / 100,
        "category": row["category"],
        "date": row["date"],
        "note": row["note"],
        "createdAt": row["created_at"],
    }


def read_transactions(user_id: str) -> list[dict[str, object]]:
    with connect_database() as connection:
        rows = connection.execute(
            "SELECT id, type, amount_cents, category, date, note, created_at "
            "FROM transactions WHERE user_id = ? ORDER BY date DESC, created_at DESC",
            (user_id,),
        ).fetchall()
    return [transaction_from_row(row) for row in rows]


def validate_transaction(payload: object, user_id: str) -> tuple[str, str, int, str, str, str, int, str]:
    if not isinstance(payload, dict):
        raise ValueError("A transaction must be a JSON object.")

    transaction_id = payload.get("id")
    transaction_type = payload.get("type", "expense")
    category = payload.get("category")
    transaction_date = payload.get("date")
    note = payload.get("note", "")

    if not isinstance(transaction_id, str) or not transaction_id or len(transaction_id) > 80:
        raise ValueError("A valid transaction ID is required.")
    if transaction_type not in CATEGORIES:
        raise ValueError("Transaction type must be income or expense.")
    if category not in CATEGORIES[transaction_type]:
        raise ValueError("Choose a valid category for this transaction type.")
    if not isinstance(transaction_date, str):
        raise ValueError("A valid transaction date is required.")
    try:
        if date.fromisoformat(transaction_date).isoformat() != transaction_date:
            raise ValueError("Date must use YYYY-MM-DD format.")
    except ValueError as error:
        raise ValueError("A valid transaction date is required in YYYY-MM-DD format.") from error
    if not isinstance(note, str) or len(note) > 80:
        raise ValueError("The note must be 80 characters or fewer.")

    try:
        amount_input = payload.get("amount")
        amount = Decimal(str(amount_input))
        if not amount.is_finite() or amount <= 0:
            raise ValueError("Amount must be greater than zero.")
        decimal_text = format(amount, "f")
        if "." in decimal_text:
            decimal_places = len(decimal_text.split(".", 1)[1])
            if decimal_places > 2:
                raise ValueError("Enter a valid amount with no more than two decimal places.")
        rounded_amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        cents = int(rounded_amount * 100)
    except (InvalidOperation, ValueError) as error:
        raise ValueError("Enter a valid amount with no more than two decimal places.") from error
    if cents <= 0 or cents > 9_000_000_000_000_000:
        raise ValueError("Amount is outside the supported range.")

    try:
        created_at = int(payload.get("createdAt", time.time() * 1000))
    except (TypeError, ValueError) as error:
        raise ValueError("A valid creation time is required.") from error
    return transaction_id, transaction_type, cents, category, transaction_date, note, created_at, user_id


def session_cookie_value(headers: object) -> str | None:
    cookie = SimpleCookie()
    try:
        cookie.load(str(headers.get("Cookie", "")))
    except (AttributeError, ValueError):
        return None
    morsel = cookie.get(SESSION_COOKIE)
    return morsel.value if morsel else None


def password_digest(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS).hex()


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class RequestDenied(Exception):
    """A request is validly formed but lacks permission to complete."""


class DaybookHandler(BaseHTTPRequestHandler):
    server_version = "Daybook/1.0"

    def send_json(self, status: int, value: object, headers: tuple[tuple[str, str], ...] = ()) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def send_cookie(self, token: str, max_age: int) -> tuple[tuple[str, str], ...]:
        forwarded_protocol = self.headers.get("X-Forwarded-Proto", "").split(",")[0].strip().lower()
        forwarded_host = self.headers.get("X-Forwarded-Host", self.headers.get("Host", "")).split(",")[0].strip().lower()
        secure = "; Secure" if forwarded_protocol == "https" or forwarded_host.endswith(".ts.net") else ""
        return (("Set-Cookie", f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}{secure}"),)

    def read_json(self) -> object:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("Invalid request length.") from error
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("Request body is empty or too large.")
        try:
            return json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ValueError("Request body must contain valid JSON.") from error

    def same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        forwarded_host = self.headers.get("X-Forwarded-Host", self.headers.get("Host", ""))
        request_host = forwarded_host.split(",")[0].strip().lower()
        return bool(request_host and urlsplit(origin).netloc.lower() == request_host)

    def current_session(self) -> sqlite3.Row | None:
        token = session_cookie_value(self.headers)
        if not token:
            return None
        with connect_database() as connection:
            return connection.execute(
                "SELECT sessions.token_hash, sessions.csrf_token, users.id, users.username, users.is_admin "
                "FROM sessions JOIN users ON users.id = sessions.user_id "
                "WHERE sessions.token_hash = ? AND sessions.expires_at > ?",
                (token_digest(token), int(time.time())),
            ).fetchone()

    def require_session(self, check_csrf: bool = False) -> sqlite3.Row | None:
        session = self.current_session()
        if not session:
            self.send_json(401, {"error": "Please sign in to continue."})
            return None
        if check_csrf and not hmac.compare_digest(session["csrf_token"], self.headers.get("X-CSRF-Token", "")):
            self.send_json(403, {"error": "Request security check failed. Refresh and try again."})
            return None
        return session

    def create_session(self, user_id: str) -> tuple[str, str]:
        token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(24)
        with connect_database() as connection:
            connection.execute(
                "INSERT INTO sessions (token_hash, user_id, csrf_token, expires_at) VALUES (?, ?, ?, ?)",
                (token_digest(token), user_id, csrf_token, int(time.time()) + SESSION_LIFETIME),
            )
        return token, csrf_token

    @staticmethod
    def public_user(row: sqlite3.Row) -> dict[str, object]:
        return {"id": row["id"], "username": row["username"], "isAdmin": bool(row["is_admin"])}

    def authenticate(self, payload: object) -> sqlite3.Row:
        if not isinstance(payload, dict):
            raise ValueError("Enter your username and password.")
        username = payload.get("username")
        password = payload.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            raise ValueError("Enter your username and password.")
        with connect_database() as connection:
            user = connection.execute("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username.strip(),)).fetchone()
        salt = bytes.fromhex(user["password_salt"]) if user else bytes(16)
        expected = user["password_hash"] if user else password_digest("invalid-account-password", bytes(16))
        valid = hmac.compare_digest(password_digest(password, salt), expected)
        if not user or not valid:
            raise RequestDenied("Username or password is incorrect.")
        return user

    def register(self, payload: object) -> sqlite3.Row:
        if not isinstance(payload, dict):
            raise ValueError("Complete the account form.")
        username_value = payload.get("username")
        password = payload.get("password")
        if not isinstance(username_value, str) or not isinstance(password, str):
            raise ValueError("Enter a username and password.")
        username = username_value.strip().lower()
        if not USERNAME_PATTERN.fullmatch(username):
            raise ValueError("Username must be 3–32 characters using letters, numbers, dots, dashes, or underscores.")
        if len(password) < 10 or len(password) > 256:
            raise ValueError("Choose a password between 10 and 256 characters.")

        now = int(time.time())
        user_id = secrets.token_urlsafe(18)
        salt = secrets.token_bytes(16)
        encoded_hash = password_digest(password, salt)
        with connect_database() as connection:
            connection.execute("BEGIN IMMEDIATE")
            first_account = connection.execute("SELECT 1 FROM users LIMIT 1").fetchone() is None
            is_admin = first_account
            if first_account:
                setup = connection.execute("SELECT value FROM app_state WHERE key = 'owner-setup-code'").fetchone()
                provided = payload.get("setupCode", "")
                if not setup or not isinstance(provided, str) or not hmac.compare_digest(setup["value"], provided.strip()):
                    raise RequestDenied("Use the owner setup code shown in the server window.")
                connection.execute("DELETE FROM app_state WHERE key = 'owner-setup-code'")
            else:
                invite = payload.get("inviteCode", "")
                if not isinstance(invite, str):
                    raise RequestDenied("A valid invitation code is required.")
                invite_row = connection.execute(
                    "SELECT code_hash FROM invites WHERE code_hash = ? AND used_at IS NULL AND expires_at > ?",
                    (token_digest(invite.strip()), now),
                ).fetchone()
                if not invite_row:
                    raise RequestDenied("That invitation code is invalid, expired, or already used.")
                connection.execute("UPDATE invites SET used_at = ? WHERE code_hash = ?", (now, invite_row["code_hash"]))
            connection.execute(
                "INSERT INTO users (id, username, password_salt, password_hash, is_admin, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, username, salt.hex(), encoded_hash, int(is_admin), now),
            )
            if is_admin:
                connection.execute("UPDATE transactions SET user_id = ? WHERE user_id IS NULL", (user_id,))
            user = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return user

    def issue_invite(self, user_id: str) -> str:
        code = secrets.token_urlsafe(12)
        with connect_database() as connection:
            connection.execute(
                "INSERT INTO invites (code_hash, created_by, expires_at) VALUES (?, ?, ?)",
                (token_digest(code), user_id, int(time.time()) + INVITE_LIFETIME),
            )
        return code

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/api/status":
            with connect_database() as connection:
                setup_required = connection.execute("SELECT 1 FROM users LIMIT 1").fetchone() is None
            self.send_json(200, {"setupRequired": setup_required, "inviteRequired": not setup_required})
            return
        if path == "/api/me":
            session = self.current_session()
            if not session:
                self.send_json(401, {"error": "Please sign in to continue."})
                return
            self.send_json(200, {"user": self.public_user(session), "csrfToken": session["csrf_token"]})
            return
        if path == "/api/transactions":
            session = self.require_session()
            if session:
                self.send_json(200, read_transactions(session["id"]))
            return
        if path not in {"/", "/index.html"}:
            self.send_error(404, "Not found")
            return

        page_path = Path(__file__).with_name("index.html")
        try:
            body = page_path.read_bytes()
        except OSError:
            self.send_error(500, "Could not read index.html")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if not self.same_origin():
            self.send_json(403, {"error": "Cross-site requests are not allowed."})
            return
        try:
            payload = self.read_json()
            if path == "/api/register":
                user = self.register(payload)
                token, csrf_token = self.create_session(user["id"])
                self.send_json(201, {"user": self.public_user(user), "csrfToken": csrf_token}, self.send_cookie(token, SESSION_LIFETIME))
                return
            if path == "/api/login":
                user = self.authenticate(payload)
                token, csrf_token = self.create_session(user["id"])
                self.send_json(200, {"user": self.public_user(user), "csrfToken": csrf_token}, self.send_cookie(token, SESSION_LIFETIME))
                return

            session = self.require_session(check_csrf=True)
            if not session:
                return
            if path == "/api/logout":
                token = session_cookie_value(self.headers)
                with connect_database() as connection:
                    connection.execute("DELETE FROM sessions WHERE token_hash = ?", (token_digest(token),))
                self.send_json(200, {"loggedOut": True}, self.send_cookie("", 0))
                return
            if path == "/api/invites":
                if not session["is_admin"]:
                    self.send_json(403, {"error": "Only the owner can create invitations."})
                    return
                self.send_json(201, {"code": self.issue_invite(session["id"]), "expiresInDays": 7})
                return
            if path == "/api/transactions/legacy-import":
                if not isinstance(payload, dict) or not isinstance(payload.get("transactions"), list):
                    raise ValueError("Import must include a transactions list.")
                if len(payload["transactions"]) > 1000:
                    raise ValueError("Import is limited to 1,000 transactions at a time.")
                validated = [validate_transaction(item, session["id"]) for item in payload["transactions"]]
                migration_key = f"legacy-import-v1:{session['id']}"
                with connect_database() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    migrated = connection.execute("SELECT 1 FROM app_state WHERE key = ?", (migration_key,)).fetchone()
                    if migrated:
                        imported = 0
                    else:
                        cursor = connection.executemany(
                            "INSERT OR IGNORE INTO transactions "
                            "(id, type, amount_cents, category, date, note, created_at, user_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            validated,
                        )
                        imported = cursor.rowcount
                        connection.execute("INSERT INTO app_state (key, value) VALUES (?, 'complete')", (migration_key,))
                self.send_json(200, {"imported": imported})
                return
            if path == "/api/transactions":
                transaction = validate_transaction(payload, session["id"])
                with connect_database() as connection:
                    connection.execute(
                        "INSERT INTO transactions (id, type, amount_cents, category, date, note, created_at, user_id) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        transaction,
                    )
                    row = connection.execute(
                        "SELECT id, type, amount_cents, category, date, note, created_at "
                        "FROM transactions WHERE id = ? AND user_id = ?",
                        (transaction[0], session["id"]),
                    ).fetchone()
                self.send_json(201, transaction_from_row(row))
                return
            self.send_error(404, "Not found")
        except ValueError as error:
            self.send_json(400, {"error": str(error)})
        except RequestDenied as error:
            self.send_json(403, {"error": str(error)})
        except sqlite3.IntegrityError:
            self.send_json(409, {"error": "This username or transaction already exists."})

    def do_DELETE(self) -> None:
        if not self.same_origin():
            self.send_json(403, {"error": "Cross-site requests are not allowed."})
            return
        session = self.require_session(check_csrf=True)
        if not session:
            return
        prefix = "/api/transactions/"
        path = urlsplit(self.path).path
        if not path.startswith(prefix):
            self.send_error(404, "Not found")
            return
        transaction_id = unquote(path[len(prefix):])
        with connect_database() as connection:
            cursor = connection.execute(
                "DELETE FROM transactions WHERE id = ? AND user_id = ?",
                (transaction_id, session["id"]),
            )
        if cursor.rowcount == 0:
            self.send_json(404, {"error": "Transaction not found."})
            return
        self.send_json(200, {"deleted": transaction_id})

    def log_message(self, format_string: str, *args: object) -> None:
        print(f"{self.address_string()} - {format_string % args}")


def main() -> None:
    initialize_database()
    setup_code = owner_setup_code()
    with ThreadingHTTPServer((HOST, PORT), DaybookHandler) as server:
        print(f"Daybook is ready at http://{HOST}:{PORT}")
        print(f"SQLite database: {DATABASE_PATH}")
        if setup_code:
            print("Owner setup code (enter this in the browser once):")
            print(setup_code)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nDaybook server stopped.")


if __name__ == "__main__":
    main()