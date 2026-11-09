"""A small client for QuantConnect's documented REST API (v2).

Authentication follows QC's documentation: HTTP basic authentication with the
user ID and the SHA-256 hash of ``"<api token>:<unix timestamp>"``, plus a
``Timestamp`` header carrying the same timestamp. Credentials come from
``QC_USER_ID`` and ``QC_API_TOKEN``, or from the LEAN CLI's saved login
(``~/.lean/credentials``). The client uses only the standard library, so QC
tooling runs in an ordinary environment without the LEAN CLI. Credentials stay
in memory and are never written to records.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_URL = "https://www.quantconnect.com/api/v2/"


class QCError(RuntimeError):
    """A request QC refused (``success: false``) or an HTTP failure."""

    def __init__(self, endpoint: str, message: str, *, status: int | None = None, body=None):
        super().__init__(f"{endpoint}: {message}")
        self.endpoint = endpoint
        self.status = status
        self.body = body


@dataclass(frozen=True)
class Response:
    """What a transport returns: HTTP status and raw body."""

    status: int
    body: bytes


Transport = Callable[[str, str, Mapping[str, str], bytes | None, float], Response]


def urllib_transport(
    method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float
) -> Response:
    """Send one HTTP request with the standard library; HTTP errors are returned, not raised."""
    request = Request(url, data=body, headers=dict(headers), method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            return Response(response.status, response.read())
    except HTTPError as error:
        return Response(error.code, error.read())


def load_credentials(
    environ: Mapping[str, str] | None = None, lean_path: Path | None = None
) -> tuple[str, str]:
    """Return (user ID, API token) from the environment or LEAN's saved login."""
    environ = os.environ if environ is None else environ
    user, token = environ.get("QC_USER_ID"), environ.get("QC_API_TOKEN")
    if user and token:
        return str(user), str(token)
    path = lean_path or Path.home() / ".lean" / "credentials"
    if path.is_file():
        saved = json.loads(path.read_text(encoding="utf-8"))
        if saved.get("user-id") and saved.get("api-token"):
            return str(saved["user-id"]), str(saved["api-token"])
    raise QCError(
        "credentials", "set QC_USER_ID and QC_API_TOKEN, or log in once with the LEAN CLI"
    )


def auth_headers(user_id: str, api_token: str, timestamp: int | None = None) -> dict[str, str]:
    """QC's documented request authentication for one request."""
    stamp = str(int(time.time() if timestamp is None else timestamp))
    digest = hashlib.sha256(f"{api_token}:{stamp}".encode()).hexdigest()
    basic = base64.b64encode(f"{user_id}:{digest}".encode()).decode()
    return {"Authorization": "Basic " + basic, "Timestamp": stamp}


def _form_fields(data: Mapping[str, Any]) -> list[tuple[str, str]]:
    # QC's form endpoints take nested lists as files[0][name]=...; booleans as true/false.
    fields = []
    for key, value in data.items():
        if isinstance(value, bool):
            value = "true" if value else "false"
        fields.append((key, str(value)))
    return fields


def _multipart(fields: list[tuple[str, str]], files: Mapping[str, bytes]) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
            + value.encode()
            + b"\r\n"
        )
    for name, content in files.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
            f'filename="{name}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
            + content
            + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


@dataclass
class QCClient:
    """Authenticated calls to QC's REST API.

    ``get`` and ``post`` return the parsed JSON body when QC reports success and
    raise :class:`QCError` otherwise. One retry is made on an HTTP 5xx response,
    as the LEAN CLI does; other failures are surfaced to the caller.
    """

    user_id: str
    api_token: str = field(repr=False)
    base_url: str = API_URL
    timeout: float = 120
    transport: Transport = field(default=urllib_transport, repr=False)
    _organization_id: str | None = field(default=None, init=False, repr=False)

    @classmethod
    def from_environment(cls, **kwargs) -> QCClient:
        """Client using QC_USER_ID/QC_API_TOKEN or LEAN's saved login."""
        user, token = load_credentials()
        return cls(user, token, **kwargs)

    def request(
        self,
        method: str,
        endpoint: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        form: Mapping[str, Any] | None = None,
        files: Mapping[str, bytes] | None = None,
        timeout: float | None = None,
    ) -> dict:
        url = self.base_url + endpoint.lstrip("/")
        if params:
            url += "?" + urlencode(_form_fields(params))
        body, content_type = None, None
        if files:
            body, content_type = _multipart(_form_fields(form or {}), files)
        elif form is not None:
            body, content_type = urlencode(_form_fields(form)).encode(), (
                "application/x-www-form-urlencoded"
            )
        elif json_body is not None:
            body, content_type = json.dumps(json_body).encode(), "application/json"
        for attempt in (1, 2):
            headers = {**auth_headers(self.user_id, self.api_token), "User-Agent": "sugarloaf"}
            if content_type:
                headers["Content-Type"] = content_type
            try:
                response = self.transport(method, url, headers, body, timeout or self.timeout)
            except URLError as error:
                raise QCError(endpoint, f"network error: {error.reason}") from error
            if 500 <= response.status < 600 and attempt == 1:
                continue
            break
        try:
            data = json.loads(response.body.decode("utf-8") or "null")
        except (UnicodeDecodeError, json.JSONDecodeError):
            data = None
        if not 200 <= response.status < 300:
            detail = _errors(data) or response.body[:200].decode("utf-8", "replace")
            raise QCError(endpoint, f"HTTP {response.status}: {detail}",
                          status=response.status, body=data)
        if not isinstance(data, dict):
            raise QCError(endpoint, "response is not a JSON object", status=response.status)
        if data.get("success") is False:
            raise QCError(endpoint, _errors(data) or "request failed", status=response.status,
                          body=data)
        return data

    def get(self, endpoint: str, params: Mapping[str, Any] | None = None, **kwargs) -> dict:
        return self.request("GET", endpoint, params=params, **kwargs)

    def post(self, endpoint: str, data: Mapping[str, Any] | None = None, **kwargs) -> dict:
        return self.request("POST", endpoint, json_body=data or {}, **kwargs)

    # Convenience calls used by Sugarloaf's cloud tools.

    def authenticate(self) -> bool:
        return bool(self.get("authenticate").get("success"))

    @property
    def organization_id(self) -> str:
        """The account's default organization (cached)."""
        if self._organization_id is None:
            self._organization_id = self.post("account/read")["organizationId"]
        return self._organization_id

    def update_project(
        self,
        project_id: int,
        *,
        files: list[Mapping[str, str]] | None = None,
        parameters: Mapping[str, str] | None = None,
        name: str | None = None,
    ) -> dict:
        """Replace a project's files and/or parameters, or rename it.

        Uses the form encoding the LEAN CLI sends to ``projects/update``
        (``files[i][name]``, ``parameters[i][key]``), so behaviour matches the
        uploads Sugarloaf made through LEAN.
        """
        form: dict[str, Any] = {"projectId": project_id}
        if name is not None:
            form["name"] = name
        if parameters is not None:
            if parameters:
                for index, (key, value) in enumerate(parameters.items()):
                    form[f"parameters[{index}][key]"] = key
                    form[f"parameters[{index}][value]"] = value
            else:
                form["parameters"] = ""
        if files is not None:
            for index, item in enumerate(files):
                form[f"files[{index}][name]"] = item["name"]
                form[f"files[{index}][content]"] = item["content"]
        return self.request("POST", "projects/update", form=form)


def _errors(data) -> str:
    if not isinstance(data, dict):
        return ""
    items = data.get("errors") or data.get("messages") or []
    if isinstance(items, str):
        items = [items]
    message = "; ".join(str(item) for item in items)
    return message or str(data.get("Message") or data.get("message") or "")


def default_client() -> QCClient:
    """The client real runs use; tests replace this function."""
    return QCClient.from_environment()
