"""Minimal sync HTTP GraphQL endpoint serving the toy `Note` Hasura-shaped
schema, so the stock @refinedev/hasura provider (Node, refine-client) can drive
it.

Sync `schema.execute_sync` in a single-threaded http.server → sync Django ORM
is safe (no async), and an in-memory SQLite persists for the process lifetime.

Run from the repo root (background):
    uv run python examples/server.py
Serves POST /graphql at http://127.0.0.1:8099/graphql
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import django
from django.conf import settings

# Import the co-located ``demo_schema`` regardless of the cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent))

settings.configure(
    INSTALLED_APPS=["django.contrib.contenttypes", "django.contrib.auth"],
    DATABASES={
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": ":memory:",
        }
    },
    DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
    USE_TZ=True,
)
django.setup()

import demo_schema  # noqa: E402

demo_schema.create_table()
demo_schema.seed()

PORT = 8099


class Handler(BaseHTTPRequestHandler):
    def _send(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # CORS preflight (for a browser client)
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "content-type")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.end_headers()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send({"errors": [{"message": "invalid json"}]}, 400)
            return
        self._send(
            schema_execute(body.get("query", ""), body.get("variables") or {})
        )

    def log_message(self, *args) -> None:  # quiet
        return


def schema_execute(query: str, variables: dict) -> dict:
    result = demo_schema.schema.execute_sync(query, variable_values=variables)
    payload: dict = {"data": result.data}
    if result.errors:
        payload["errors"] = [{"message": str(err)} for err in result.errors]
    return payload


if __name__ == "__main__":
    print(
        f"serving Hasura Note schema at http://127.0.0.1:{PORT}/graphql",
        flush=True,
    )
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
