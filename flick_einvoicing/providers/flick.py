"""HTTP client for Flick's UAE Regional API.

It knows URLs, the auth header and the shapes Flick answers in; nothing about ERPNext.
Every call is written to E Invoice Log without headers, so the API key never reaches a log.
"""

import json
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode

import frappe
import requests

UUID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
# The documented answer, while a known defect lasted, for a number already filed.
RETENTION_RULE = "The operation was blocked by a retention rule."
FEED = "outgoing"
TIMEOUT = 30
MAX_LOGGED_CHARS = 100_000


@dataclass
class Response:
	status: int | None
	body: dict | None = None
	text: str = ""
	retry_after: int | None = None
	error: str | None = None
	headers: dict = field(default_factory=dict)

	@property
	def ok(self) -> bool:
		return self.status is not None and 200 <= self.status < 300

	@property
	def data(self):
		return (self.body or {}).get("data")

	@property
	def message(self) -> str:
		"""Flick's own words, whichever of its envelopes it used."""
		if self.error:
			return self.error
		body = self.body or {}
		parts = [body.get("error"), body.get("message")]
		errors = body.get("errors")
		if isinstance(errors, dict) and errors.get("message"):
			parts.append(errors["message"])
		elif isinstance(errors, str):
			parts.append(errors)
		text = ": ".join(p for p in parts if p)
		return text or (self.text[:300] if self.text else f"HTTP {self.status}")

	@property
	def issues(self) -> list[dict]:
		"""Validation issues of a 400, field or Schematron phase alike."""
		errors = (self.body or {}).get("errors")
		return errors if isinstance(errors, list) else []


class FlickClient:
	def __init__(self, base_url: str, api_key: str, participant_id: str, company: str | None = None):
		self.base_url = (base_url or "").rstrip("/")
		self.api_key = api_key
		self.participant_id = participant_id
		self.company = company

	@classmethod
	def from_settings(cls, settings) -> "FlickClient":
		return cls(
			base_url=settings.base_url,
			api_key=settings.get_password("api_key", raise_exception=False),
			participant_id=settings.participant_id,
			company=settings.company,
		)

	# Documents

	def submit(self, payload: dict, submission: str | None = None) -> Response:
		# ?no_error answers an identifier Flick already holds with 200 and that document,
		# so a retry after a timeout adopts the original instead of failing.
		return self._request(
			"POST",
			f"{self._participant_path()}/documents",
			params={"no_error": "true"},
			body=payload,
			operation="submit",
			submission=submission,
		)

	def get_document(self, document_id: str, submission: str | None = None) -> Response:
		if not UUID_PATTERN.match(document_id or ""):
			# Flick answers a non-UUID here with an HTML 500, so refuse it before calling.
			return Response(status=None, error=f"Not a Flick document id: {document_id!r}")
		return self._request(
			"GET",
			f"{self._participant_path()}/documents/{document_id}",
			operation="get_document",
			submission=submission,
		)

	def find_by_identifier(self, document_identifier: str, submission: str | None = None) -> Response:
		return self._request(
			"GET",
			f"{self._participant_path()}/documents",
			params={"direction": FEED, "document_identifier": document_identifier},
			operation="find_by_identifier",
			submission=submission,
		)

	# Participants and events

	def list_participants(self) -> Response:
		return self._request("GET", "/v1/participants", operation="list_participants")

	def events(self, cursor: str | None = None, limit: int = 100) -> Response:
		params = {"direction": FEED, "limit": limit}
		if cursor:
			params["cursor"] = cursor  # reads without moving Flick's stored position
		return self._request("GET", f"{self._participant_path()}/events", params=params, operation="events")

	def ack(self, cursor: str) -> Response:
		return self._request(
			"POST",
			f"{self._participant_path()}/events/ack",
			body={"direction": FEED, "cursor": str(cursor)},
			operation="ack",
		)

	# Plumbing

	def _participant_path(self) -> str:
		return f"/v1/participants/{self.participant_id}"

	def _request(
		self,
		method: str,
		path: str,
		*,
		params: dict | None = None,
		body: dict | None = None,
		operation: str,
		submission: str | None = None,
	) -> Response:
		endpoint = path + (f"?{urlencode(params)}" if params else "")
		started = time.monotonic()
		response = self._http(method, self.base_url + endpoint, body)
		duration_ms = int((time.monotonic() - started) * 1000)
		self._log(operation, method, endpoint, body, response, duration_ms, submission)
		return response

	def _http(self, method: str, url: str, body: dict | None) -> Response:
		headers = {"X-Flick-Auth-Key": self.api_key or "", "Accept": "application/json"}
		if body is not None:
			headers["Content-Type"] = "application/json"
		try:
			raw = requests.request(
				method,
				url,
				data=json.dumps(body) if body is not None else None,
				headers=headers,
				timeout=TIMEOUT,
			)
		except requests.RequestException as e:
			# No answer at all: the request may or may not have reached Flick.
			return Response(status=None, error=f"{type(e).__name__}: {e}")

		parsed = None
		if "json" in (raw.headers.get("Content-Type") or ""):
			try:
				parsed = raw.json()
			except ValueError:
				parsed = None
		retry_after = raw.headers.get("Retry-After")
		return Response(
			status=raw.status_code,
			body=parsed if isinstance(parsed, dict) else None,
			text=raw.text,
			retry_after=int(retry_after) if retry_after and retry_after.isdigit() else None,
			headers={
				k: v for k, v in raw.headers.items() if k.lower().startswith(("x-ratelimit", "retry-after"))
			},
		)

	def _log(self, operation, method, endpoint, body, response: Response, duration_ms, submission):
		try:
			frappe.get_doc(
				{
					"doctype": "E Invoice Log",
					"submission": submission,
					"company": self.company,
					"direction": "Outbound",
					"operation": operation,
					"method": method,
					"endpoint": endpoint,
					"http_status": response.status,
					"duration_ms": duration_ms,
					"request": json.dumps(body, indent=1) if body is not None else None,
					"response": (
						json.dumps(response.body, indent=1) if response.body is not None else response.text
					)[:MAX_LOGGED_CHARS],
					"error": response.error,
				}
			).insert(ignore_permissions=True)
		except Exception:
			# A logging problem must never change what happens to the invoice.
			frappe.log_error(title="E Invoice Log could not be written")
