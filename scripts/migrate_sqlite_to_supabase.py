"""Copy existing Daybook SQLite transactions into the signed-in Supabase account."""

from __future__ import annotations

import getpass
import json
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_DATABASE = Path(__file__).resolve().parents[1] / "daybook.sqlite3"
BATCH_SIZE = 250


def request_json(url: str, payload: object, headers: dict[str, str]) -> object:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response_body = response.read()
            if not response_body:
                return {}
            return json.loads(response_body.decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Supabase request failed ({error.code}): {detail}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Could not reach Supabase: {error.reason}") from error


def read_local_transactions(database_path: Path) -> list[dict[str, object]]:
    if not database_path.is_file():
        raise FileNotFoundError(f"SQLite database not found: {database_path}")
    connection = sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(
            "SELECT id, type, amount_cents, category, date, note, created_at "
            "FROM transactions ORDER BY date, created_at"
        )]
    finally:
        connection.close()


def as_cloud_transaction(row: dict[str, object], user_id: str) -> dict[str, object]:
    created_at = int(row["created_at"])
    created_at_iso = datetime.fromtimestamp(created_at / 1000, tz=timezone.utc).isoformat()
    return {
        "user_id": user_id,
        "transaction_type": row["type"] if row["type"] in {"expense", "income"} else "expense",
        "amount_cents": int(row["amount_cents"]),
        "category": row["category"],
        "transaction_date": row["date"],
        "note": row["note"] or "",
        "legacy_id": str(row["id"]),
        "created_at": created_at_iso,
    }


def main() -> int:
    database_path = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else DEFAULT_DATABASE
    rows = read_local_transactions(database_path)
    print(f"Found {len(rows)} local transactions in {database_path}.")
    if not rows:
        return 0

    supabase_url = input("Supabase project URL: ").strip().rstrip("/")
    publishable_key = input("Supabase publishable/anon key: ").strip()
    email = input("Owner account email: ").strip()
    password = getpass.getpass("Owner account password (input is hidden): ")
    if not all((supabase_url, publishable_key, email, password)):
        print("All four values are required.", file=sys.stderr)
        return 2

    headers = {"apikey": publishable_key, "Content-Type": "application/json"}
    session = request_json(
        f"{supabase_url}/auth/v1/token?grant_type=password",
        {"email": email, "password": password},
        headers,
    )
    if not isinstance(session, dict) or not isinstance(session.get("user"), dict):
        raise RuntimeError("Supabase did not return a signed-in user.")
    access_token = session.get("access_token")
    user_id = session["user"].get("id")
    if not isinstance(access_token, str) or not isinstance(user_id, str):
        raise RuntimeError("Supabase sign-in response was incomplete.")

    upload_headers = {
        **headers,
        "Authorization": f"Bearer {access_token}",
        "Prefer": "resolution=ignore-duplicates,return=minimal",
    }
    for start in range(0, len(rows), BATCH_SIZE):
        batch = [as_cloud_transaction(row, user_id) for row in rows[start:start + BATCH_SIZE]]
        request_json(
            f"{supabase_url}/rest/v1/transactions?on_conflict=user_id%2Clegacy_id",
            batch,
            upload_headers,
        )
        print(f"Imported or verified {min(start + len(batch), len(rows))} of {len(rows)} transactions.")

    print("Cloud import complete. Your SQLite database was not changed; keep it as a backup.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, RuntimeError, sqlite3.Error) as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from error