"""HTTP client for Flick's UAE Regional API.

It knows URLs, credentials and the shapes Flick answers in; nothing about ERPNext.
Every call is written to E Invoice Log without headers, so no credential reaches a log.
"""

import json
import re
import time
from dataclasses import dataclass, field
from urllib.parse import quote, urlencode

import frappe
import requests

UUID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
# The documented answer, while a known defect lasted, for a number already filed.
RETENTION_RULE = "The operation was blocked by a retention rule."
OUTGOING, INCOMING = "outgoing", "incoming"
ARTIFACTS = ("DOCUMENT_XML", "DOCUMENT_SBD_SIGNED", "DOCUMENT_MLS_SENT", "DOCUMENT_MLS_RECVD", "DOCUMENT_TDD")
TIMEOUT = 30
MAX_LOGGED_CHARS = 100_000
TOKEN_MARGIN_SECONDS = 120


@dataclass
class Response:
	status: int | None
	body: dict | None = None
	text: str = ""
	retry_after: int | None = None
	error: str | None = None
	headers: dict = field(default_factory=dict)
	content: bytes | None = None
	content_type: str | None = None

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
		parts = [body.get("error"), body.get("message"), body.get("error_description")]
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
	def __init__(
		self,
		base_url: str,
		api_key: str | None,
		participant_id: str,
		company: str | None = None,
		client_id: str | None = None,
		client_secret: str | None = None,
	):
		self.base_url = (base_url or "").rstrip("/")
		self.api_key = api_key
		self.participant_id = participant_id
		self.company = company
		self.client_id = client_id
		self.client_secret = client_secret

	@classmethod
	def from_settings(cls, settings) -> "FlickClient":
		oauth = settings.get("auth_method") == "OAuth Client"
		return cls(
			base_url=settings.base_url,
			api_key=None if oauth else settings.get_password("api_key", raise_exception=False),
			participant_id=settings.participant_id,
			company=settings.company,
			client_id=settings.get("client_id") if oauth else None,
			client_secret=settings.get_password("client_secret", raise_exception=False) if oauth else None,
		)

	# Documents

	def submit(self, payload: dict, submission: str | None = None) -> Response:
		# ?no_error answers an identifier Flick already holds with 200 and that document,
		# so a retry after a timeout adopts the original instead of failing.
		return self._request(
			"POST",
			self._documents(),
			params={"no_error": "true"},
			body=payload,
			operation="submit",
			submission=submission,
		)

	def submit_bulk(self, payloads: list[dict]) -> Response:
		"""Up to 50 documents, each filed on its own with its own result."""
		return self._request(
			"POST",
			self._documents("/bulk"),
			params={"no_error": "true"},
			body={"documents": payloads},
			operation="submit_bulk",
		)

	def validate(self, payload: dict, submission: str | None = None) -> Response:
		"""Every check submit runs, nothing stored. It consumes the identifier it is given, so
		callers pass a throwaway one."""
		return self._request(
			"POST", self._documents("/validate"), body=payload, operation="validate", submission=submission
		)

	def get_document(self, document_id: str, submission: str | None = None) -> Response:
		if not UUID_PATTERN.match(document_id or ""):
			# Flick answers a non-UUID here with an HTML 500, so refuse it before calling.
			return Response(status=None, error=f"Not a Flick document id: {document_id!r}")
		return self._request(
			"GET", self._documents(f"/{document_id}"), operation="get_document", submission=submission
		)

	def find_by_identifier(
		self, document_identifier: str, direction: str = OUTGOING, submission: str | None = None
	) -> Response:
		return self._request(
			"GET",
			self._documents(),
			params={"direction": direction, "document_identifier": document_identifier},
			operation="find_by_identifier",
			submission=submission,
		)

	def get_file(self, document_id: str, kind: str, submission: str | None = None) -> Response:
		"""kind: "pdf", "xml", an artifact type, or "attachment/<index>". Answers bytes."""
		if not UUID_PATTERN.match(document_id or ""):
			return Response(status=None, error=f"Not a Flick document id: {document_id!r}")
		params = None
		if kind == "pdf":
			path = f"/{document_id}/pdf"
		elif kind == "xml":
			path = f"/{document_id}/xml"
			params = {"raw": "true"}  # the bytes, not the JSON envelope
		elif kind in ARTIFACTS:
			path = f"/{document_id}/artifacts/{kind}"
		elif kind.startswith("attachment/") and kind.split("/", 1)[1].isdigit():
			path = f"/{document_id}/attachments/{kind.split('/', 1)[1]}"
		else:
			return Response(status=None, error=f"Unknown document file: {kind}")
		return self._request(
			"GET",
			self._documents(path),
			params=params,
			operation=f"get_{kind.split('/')[0]}",
			submission=submission,
			binary=True,
		)

	# Participants and the network

	def list_participants(self) -> Response:
		return self._request("GET", "/v1/participants", operation="list_participants")

	def get_participant(self) -> Response:
		return self._request("GET", self._participant(), operation="get_participant")

	def lookup(self, identifier: str, document_type: str | None = None) -> Response:
		"""Whether a Peppol participant is reachable, from the network itself (SML/SMP)."""
		path = f"/v1/peppol/lookup/{quote(identifier, safe=':')}"
		if document_type:
			path += f"/{quote(document_type, safe='')}"
		return self._request("GET", path, operation="peppol_lookup")

	# Events

	def events(self, cursor: str | None = None, limit: int = 100, direction: str = OUTGOING) -> Response:
		params = {"direction": direction, "limit": limit}
		if cursor:
			params["cursor"] = cursor  # reads without moving Flick's stored position
		return self._request(
			"GET", f"{self._participant()}/events", params=params, operation=f"events_{direction}"
		)

	def ack(self, cursor: str, direction: str = OUTGOING) -> Response:
		return self._request(
			"POST",
			f"{self._participant()}/events/ack",
			body={"direction": direction, "cursor": str(cursor)},
			operation=f"ack_{direction}",
		)

	# Webhooks

	def webhook_event_types(self) -> Response:
		return self._request("GET", "/v1/webhooks/event-types", operation="webhook_event_types")

	def create_webhook(self, name: str, endpoint: str, event_types: list[str]) -> Response:
		body = {
			"name": name,
			"endpoint": endpoint,
			"event_types": event_types,
			"participant_ids": [self.participant_id],
		}
		return self._request(
			"POST", "/v1/webhooks/subscriptions", body=body, operation="create_webhook", log_response=False
		)

	def delete_webhook(self, subscription: str) -> Response:
		return self._request(
			"DELETE", f"/v1/webhooks/subscriptions/{subscription}", operation="delete_webhook"
		)

	def rotate_webhook_secret(self, subscription: str) -> Response:
		return self._request(
			"POST",
			f"/v1/webhooks/subscriptions/{subscription}/signature-key",
			operation="rotate_webhook",
			log_response=False,
		)

	def webhook_deliveries(self, subscription: str, status: str | None = None, limit: int = 20) -> Response:
		params = {"limit": limit, **({"status": status} if status else {})}
		return self._request(
			"GET",
			f"/v1/webhooks/subscriptions/{subscription}/deliveries",
			params=params,
			operation="webhook_deliveries",
		)

	# Plumbing

	def _participant(self) -> str:
		return f"/v1/participants/{self.participant_id}"

	def _documents(self, suffix: str = "") -> str:
		return f"{self._participant()}/documents{suffix}"

	def _request(
		self,
		method: str,
		path: str,
		*,
		params: dict | None = None,
		body: dict | None = None,
		operation: str,
		submission: str | None = None,
		binary: bool = False,
		log_response: bool = True,
	) -> Response:
		endpoint = path + (f"?{urlencode(params)}" if params else "")
		started = time.monotonic()
		credential = self._credential()
		if isinstance(credential, Response):
			response = credential
		else:
			response = self._http(method, self.base_url + endpoint, body, credential, binary)
			if response.status in (401, 403) and self.client_id:
				# A token can expire between our clock and Flick's: renew once.
				credential = self._credential(renew=True)
				if not isinstance(credential, Response):
					response = self._http(method, self.base_url + endpoint, body, credential, binary)
		duration_ms = int((time.monotonic() - started) * 1000)
		self._log(operation, method, endpoint, body, response, duration_ms, submission, log_response)
		return response

	def _credential(self, renew: bool = False):
		if not self.client_id:
			return self.api_key or ""
		key = f"flick_einvoicing:token:{self.company}:{self.client_id}"
		if not renew:
			cached = frappe.cache.get_value(key)
			if cached:
				return cached
		raw = self._http_form(
			"/v1/oauth/token",
			{
				"grant_type": "client_credentials",
				"client_id": self.client_id,
				"client_secret": self.client_secret or "",
			},
		)
		if not raw.ok or not isinstance(raw.body, dict) or not raw.body.get("access_token"):
			self._log("oauth_token", "POST", "/v1/oauth/token", None, raw, 0, None, True)
			return Response(
				status=raw.status, body=raw.body, text=raw.text, error=f"Token exchange failed: {raw.message}"
			)
		token = raw.body["access_token"]
		lifetime = int(raw.body.get("expires_in") or 3600) - TOKEN_MARGIN_SECONDS
		frappe.cache.set_value(key, token, expires_in_sec=max(lifetime, 60))
		return token

	def _http_form(self, path: str, form: dict) -> Response:
		try:
			raw = requests.post(
				self.base_url + path, data=form, timeout=TIMEOUT, headers={"Accept": "application/json"}
			)
		except requests.RequestException as e:
			return Response(status=None, error=f"{type(e).__name__}: {e}")
		return self._wrap(raw, binary=False)

	def _http(self, method: str, url: str, body: dict | None, credential: str, binary: bool) -> Response:
		headers = {"X-Flick-Auth-Key": credential, "Accept": "*/*" if binary else "application/json"}
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
		return self._wrap(raw, binary)

	@staticmethod
	def _wrap(raw, binary: bool) -> Response:
		content_type = raw.headers.get("Content-Type") or ""
		parsed = None
		if "json" in content_type:
			try:
				parsed = raw.json()
			except ValueError:
				parsed = None
		retry_after = raw.headers.get("Retry-After")
		ok_binary = binary and 200 <= raw.status_code < 300 and "json" not in content_type
		return Response(
			status=raw.status_code,
			body=parsed if isinstance(parsed, dict) else None,
			text="" if ok_binary else raw.text,
			retry_after=int(retry_after) if retry_after and retry_after.isdigit() else None,
			headers={
				k: v
				for k, v in raw.headers.items()
				if k.lower().startswith(("x-ratelimit", "retry-after", "content-disposition"))
			},
			content=raw.content if ok_binary else None,
			content_type=content_type,
		)

	def _log(
		self,
		operation,
		method,
		endpoint,
		body,
		response: Response,
		duration_ms,
		submission,
		log_response=True,
	):
		if response.content is not None:
			logged = f"<{len(response.content)} bytes of {response.content_type}>"
		elif not log_response and response.ok:
			logged = "<withheld: carries a secret>"
		else:
			logged = json.dumps(response.body, indent=1) if response.body is not None else response.text
		try:
			frappe.get_doc(
				{
					"doctype": "E Invoice Log",
					"submission": submission,
					"company": self.company,
					"direction": "Inbound" if "incoming" in operation else "Outbound",
					"operation": operation,
					"method": method,
					"endpoint": endpoint,
					"http_status": response.status,
					"duration_ms": duration_ms,
					"request": _loggable(body),
					"response": (logged or "")[:MAX_LOGGED_CHARS],
					"error": response.error,
				}
			).insert(ignore_permissions=True)
		except Exception:
			# A logging problem must never change what happens to the invoice.
			frappe.log_error(title="E Invoice Log could not be written")


def _loggable(body: dict | None) -> str | None:
	"""The request as sent, with embedded file contents cut down to their size."""
	if body is None:
		return None

	def trim(value):
		if isinstance(value, dict):
			return {
				k: (f"<{len(v)} base64 chars>" if k == "content" and isinstance(v, str) else trim(v))
				for k, v in value.items()
			}
		if isinstance(value, list):
			return [trim(v) for v in value]
		return value

	return json.dumps(trim(body), indent=1)[:MAX_LOGGED_CHARS]
