"""Integration tests for Daybook authentication and account isolation."""

from __future__ import annotations

import http.cookiejar
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import server


class ApiClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.csrf_token = ""
        cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))

    def request(self, method: str, path: str, payload: object | None = None) -> tuple[int, dict[str, object]]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Origin": self.base_url}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if method not in {"GET", "HEAD"} and self.csrf_token:
            headers["X-CSRF-Token"] = self.csrf_token
        request = urllib.request.Request(self.base_url + path, data=body, headers=headers, method=method)
        try:
            response = self.opener.open(request)
        except urllib.error.HTTPError as error:
            response = error
        result = json.loads(response.read().decode("utf-8"))
        if isinstance(result, dict) and isinstance(result.get("csrfToken"), str):
            self.csrf_token = result["csrfToken"]
        return response.status, result


class DaybookApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        server.DATABASE_PATH = Path(self.temp_directory.name) / "test-daybook.sqlite3"
        server.initialize_database()
        self.setup_code = server.owner_setup_code()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.DaybookHandler)
        self.base_url = f"http://127.0.0.1:{self.httpd.server_port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.owner = ApiClient(self.base_url)
        status, owner = self.owner.request(
            "POST",
            "/api/register",
            {"username": "owner_user", "password": "owner-password-123", "setupCode": self.setup_code},
        )
        self.assertEqual(status, 201)
        self.assertTrue(owner["user"]["isAdmin"])

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        self.temp_directory.cleanup()

    def test_invited_accounts_only_see_their_own_transactions(self) -> None:
        status, invite = self.owner.request("POST", "/api/invites", {})
        self.assertEqual(status, 201)
        friend = ApiClient(self.base_url)
        status, user = friend.request(
            "POST",
            "/api/register",
            {"username": "friend_user", "password": "friend-password-123", "inviteCode": invite["code"]},
        )
        self.assertEqual(status, 201)
        self.assertFalse(user["user"]["isAdmin"])

        owner_transaction = {
            "id": "owner-lunch",
            "type": "expense",
            "amount": 125,
            "category": "Food",
            "date": "2026-10-09",
            "note": "Owner lunch",
        }
        self.assertEqual(self.owner.request("POST", "/api/transactions", owner_transaction)[0], 201)
        self.assertEqual(self.owner.request("GET", "/api/transactions")[1][0]["id"], "owner-lunch")
        self.assertEqual(friend.request("GET", "/api/transactions")[1], [])

        friend_transaction = {**owner_transaction, "id": "friend-lunch", "note": "Friend lunch"}
        self.assertEqual(friend.request("POST", "/api/transactions", friend_transaction)[0], 201)
        self.assertEqual(friend.request("GET", "/api/transactions")[1][0]["id"], "friend-lunch")
        self.assertEqual(self.owner.request("GET", "/api/transactions")[1][0]["id"], "owner-lunch")
        self.assertEqual(self.owner.request("DELETE", "/api/transactions/friend-lunch")[0], 404)

    def test_invitation_is_single_use(self) -> None:
        _, invite = self.owner.request("POST", "/api/invites", {})
        first_friend = ApiClient(self.base_url)
        self.assertEqual(first_friend.request(
            "POST", "/api/register", {"username": "first_friend", "password": "friend-password-123", "inviteCode": invite["code"]}
        )[0], 201)
        second_friend = ApiClient(self.base_url)
        self.assertEqual(second_friend.request(
            "POST", "/api/register", {"username": "other_friend", "password": "friend-password-123", "inviteCode": invite["code"]}
        )[0], 403)

    def test_writes_require_a_valid_csrf_token(self) -> None:
        self.owner.csrf_token = "invalid-token"
        transaction = {
            "id": "csrf-test",
            "type": "expense",
            "amount": 1,
            "category": "Other",
            "date": "2026-10-09",
            "note": "",
        }
        self.assertEqual(self.owner.request("POST", "/api/transactions", transaction)[0], 403)
        self.assertEqual(self.owner.request("GET", "/api/transactions")[1], [])

    def test_amounts_with_more_than_two_decimal_places_are_rejected(self) -> None:
        transaction = {
            "id": "amount-rounding-test",
            "type": "expense",
            "amount": 1.234,
            "category": "Food",
            "date": "2026-10-09",
            "note": "",
        }
        status, result = self.owner.request("POST", "/api/transactions", transaction)
        self.assertEqual(status, 400)
        self.assertIn("no more than two decimal places", result["error"])
        self.assertEqual(self.owner.request("GET", "/api/transactions")[1], [])


if __name__ == "__main__":
    unittest.main()