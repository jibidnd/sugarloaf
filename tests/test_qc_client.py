"""QC REST client: documented authentication, credentials, encodings and failures.

Response bodies are recorded from QC's API (8 November 2026) with identifiers
replaced; no network access is used.
"""

import base64
import hashlib
import json
from urllib.parse import parse_qs

import pytest

from sugarloaf.adapters.quantconnect.cloud.client import (
    QCClient,
    QCError,
    Response,
    auth_headers,
    load_credentials,
)

RECORDED = {
    "authenticate": {"success": True},
    "account/read": {
        "organizationId": "org123",
        "creditBalance": 0,
        "card": None,
        "success": True,
    },
    "research/read missing": {"success": False, "errors": ["Connection not found."]},
    "research/read stuck": {
        "success": False,
        "errors": ["Timeout waiting for a node allocation, please retry. R-abc"],
    },
    "hash": {"success": False, "errors": ["Hash doesn't match."]},
}


class Recorder:
    """A transport that returns queued responses and records each request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, method, url, headers, body, timeout):
        self.requests.append({"method": method, "url": url, "headers": headers, "body": body})
        status, payload = self.responses.pop(0)
        return Response(status, json.dumps(payload).encode() if payload is not None else b"")


def client(*responses):
    transport = Recorder(*responses)
    return QCClient("42", "secret-token", transport=transport), transport


def test_documented_authentication_hashes_token_with_timestamp():
    headers = auth_headers("42", "secret-token", timestamp=1700000000)
    assert headers["Timestamp"] == "1700000000"
    digest = hashlib.sha256(b"secret-token:1700000000").hexdigest()
    assert headers["Authorization"] == "Basic " + base64.b64encode(
        f"42:{digest}".encode()
    ).decode()


def test_requests_carry_authentication_and_json_body():
    qc, transport = client((200, RECORDED["account/read"]))
    assert qc.organization_id == "org123"
    assert qc.organization_id == "org123"  # cached: one request
    (request,) = transport.requests
    assert request["url"] == "https://www.quantconnect.com/api/v2/account/read"
    assert request["headers"]["Authorization"].startswith("Basic ")
    assert request["headers"]["Timestamp"].isdigit()
    assert request["headers"]["Content-Type"] == "application/json"
    assert "secret-token" not in json.dumps(request["headers"])
    assert "secret-token" not in repr(qc)


def test_success_false_raises_with_qc_message():
    qc, _ = client((200, RECORDED["research/read missing"]))
    with pytest.raises(QCError, match="Connection not found"):
        qc.post("research/read", {"projectId": 1})


def test_http_error_and_one_retry_on_5xx():
    qc, transport = client((502, None), (200, RECORDED["authenticate"]))
    assert qc.authenticate()
    assert len(transport.requests) == 2
    qc, transport = client((503, None), (503, {"errors": ["down"]}))
    with pytest.raises(QCError, match="HTTP 503: down") as error:
        qc.get("authenticate")
    assert error.value.status == 503
    qc, transport = client((401, RECORDED["hash"]))
    with pytest.raises(QCError, match="Hash doesn't match"):
        qc.get("authenticate")
    assert len(transport.requests) == 1


def test_update_project_uses_lean_form_encoding():
    qc, transport = client((200, {"success": True}))
    qc.update_project(
        7,
        files=[{"name": "main.py", "content": "x = 1\n"}],
        parameters={"lookback": "20", "budget": "0.5"},
    )
    request = transport.requests[0]
    assert request["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    form = parse_qs(request["body"].decode())
    assert form == {
        "projectId": ["7"],
        "files[0][name]": ["main.py"],
        "files[0][content]": ["x = 1\n"],
        "parameters[0][key]": ["lookback"],
        "parameters[0][value]": ["20"],
        "parameters[1][key]": ["budget"],
        "parameters[1][value]": ["0.5"],
    }


def test_multipart_upload_for_object_store():
    qc, transport = client((200, {"success": True}))
    qc.request("POST", "object/set", form={"organizationId": "org", "key": "lab/x.json"},
               files={"objectData": b"{\"a\": 1}"})
    request = transport.requests[0]
    assert request["headers"]["Content-Type"].startswith("multipart/form-data; boundary=")
    assert b'name="key"\r\n\r\nlab/x.json' in request["body"]
    assert b'{"a": 1}' in request["body"]


def test_credentials_from_environment_then_lean_login(tmp_path):
    saved = tmp_path / "credentials"
    saved.write_text(json.dumps({"user-id": "7", "api-token": "lean-token"}))
    assert load_credentials({"QC_USER_ID": "1", "QC_API_TOKEN": "t"}, saved) == ("1", "t")
    assert load_credentials({}, saved) == ("7", "lean-token")
    with pytest.raises(QCError, match="QC_USER_ID"):
        load_credentials({}, tmp_path / "missing")
