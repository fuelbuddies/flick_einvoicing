"""Report Flick errors and tickets to the support hub, from inside this app.

The support client for sites that take Flick but not techountant_support. It speaks the same
protocol to the hub and only ever sends what concerns this app. Off until a System Manager turns
it on in Flick Support Settings; while off it captures nothing and makes no network calls.
"""

import base64
import hashlib
import hmac
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import frappe
import requests
from frappe import _
from frappe.utils import (
	add_to_date,
	cint,
	get_datetime,
	get_fullname,
	get_system_timezone,
	get_url,
	now_datetime,
)

SETTINGS = "Flick Support Settings"
REPORT = "E Invoice Support Report"
APP = "flick_einvoicing"
SCHEMA_VERSION = 1
TIMEOUT = 20
MAX_ATTEMPTS = 10
MAX_TEXT = 20_000
MAX_MESSAGE = 5_000
MAX_SCREENSHOT_BYTES = 4 * 1024 * 1024
MAX_UPDATE_BYTES = 20 * 1024 * 1024
DEDUPE_HOURS = 24
AUTH_FAIL_LIMIT = 20  # per IP per 10 minutes
PERMANENT_ERRORS = (400, 401, 403, 404, 413)
TICKET_STATUSES = ("Open", "Triaged", "In Progress", "Waiting on Client", "Resolved", "Closed")

_SECRET_KV = re.compile(
	r"""(?ix)
	(["']?(?:password|passwd|pwd|secret|api[_-]?key|api[_-]?secret|client[_-]?secret|token|access[_-]?token|
	refresh[_-]?token|authorization|auth|sid|csrf[_-]?token|cookie|private[_-]?key)["']?
	\s*[:=]\s*)
	("[^"]*"|'[^']*'|[^\s,;&}]+)
	"""
)
_BEARER = re.compile(r"(?i)\b(bearer|token|basic)\s+[A-Za-z0-9._~+/=:-]{8,}")
_EMAIL = re.compile(r"\b([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
_LONG_DIGITS = re.compile(r"\b\d{9,}\b")
_SQL_LITERAL = re.compile(r"(?i)((?:=|like|in\s*\(|,)\s*)'([^']{3,})'")
_FRAME = re.compile(r'File "([^"]+)", line \d+, in (\S+)')


# ------------------------------------------------------------------ what leaves the site


def redact(text, keep_emails=False):
	"""Strip secrets and obvious personal data from free text before it leaves the site."""
	if not text:
		return text
	text = _SECRET_KV.sub(lambda m: m.group(1) + "'***'", str(text))
	text = _BEARER.sub(lambda m: m.group(1) + " ***", text)
	if not keep_emails:
		text = _EMAIL.sub(lambda m: f"{m.group(1)}***@{m.group(2)}", text)
	text = _LONG_DIGITS.sub(lambda m: m.group(0)[:3] + "***", text)
	return text[:MAX_TEXT]


def code_only(tb):
	"""A traceback without its local variables.

	frappe.log_error records every frame's variables, and here those hold invoices and customers.
	Only the frames, their source lines and the exception may leave the site."""
	lines = (tb or "").rstrip().splitlines()
	kept = []
	for i, line in enumerate(lines):
		# a frame sits at two spaces; deeper text that looks like one is a variable's value
		if not (line.startswith('  File "') and _FRAME.search(line)):
			continue
		kept.append(line)
		following = lines[i + 1] if i + 1 < len(lines) else ""
		if following.startswith("    ") and not following.startswith("     "):
			kept.append(following)
	if lines and (not kept or lines[-1] != kept[-1]):
		kept.append(lines[-1])
	return "\n".join(kept)


def to_wire_time(value):
	"""What the hub expects: UTC with an offset, so sites in other time zones agree."""
	return get_datetime(value).replace(tzinfo=ZoneInfo(get_system_timezone())).astimezone(timezone.utc).isoformat()


def from_wire_time(value):
	"""A hub time in this site's own time zone, as the database stores it. Now when unreadable."""
	try:
		parsed = datetime.fromisoformat(str(value))
	except ValueError:
		return now_datetime()
	if parsed.tzinfo is None:
		return parsed
	return parsed.astimezone(ZoneInfo(get_system_timezone())).replace(tzinfo=None)


def is_ours(tb):
	"""An error belongs to this app when one of its own files is in the stack."""
	return any(f"{APP}/" in path for path, _func in _FRAME.findall(tb or ""))


def fingerprint_of(tb, title=None):
	"""Stable id for "the same error": exception type + innermost frames, ignoring line numbers.
	Same calculation as the full support client, so the hub groups them alike."""
	tb = tb or ""
	lines = [ln.strip() for ln in tb.strip().splitlines() if ln.strip()]
	exc_line = lines[-1] if lines else (title or "")
	frames = [f"{path.replace(chr(92), '/').split('/apps/')[-1]}:{func}" for path, func in _FRAME.findall(tb)]
	key = exc_line.split(":", 1)[0][:120] + "|" + "|".join(frames[-4:])
	return hashlib.sha1(key.encode()).hexdigest()[:16]


# ------------------------------------------------------------------ settings


def get_settings():
	return frappe.get_cached_doc(SETTINGS)


def is_enabled(settings=None):
	settings = settings or get_settings()
	return bool(cint(settings.enabled) and settings.hub_url and settings.site_key)


def validate_hub_url(url):
	parsed = urlparse(url or "")
	host = parsed.hostname or ""
	local = host in ("localhost", "127.0.0.1") or host.endswith((".local", ".localhost"))
	if not host or (parsed.scheme != "https" and not (parsed.scheme == "http" and local)):
		frappe.throw(_("Hub URL must use https://"))


def boot_session(bootinfo):
	if frappe.session.user != "Guest" and is_enabled():
		bootinfo.flick_support = 1


# ------------------------------------------------------------------ transport


class HubRequestError(Exception):
	def __init__(self, message, status_code=None):
		super().__init__(message)
		self.status_code = status_code


def _post(method, payload, settings=None):
	settings = settings or get_settings()
	validate_hub_url(settings.hub_url)
	resp = requests.post(
		settings.hub_url.rstrip("/") + f"/api/method/techountant_support.hub.{method}",
		data=json.dumps(payload, default=str),
		headers={
			"Content-Type": "application/json",
			"Accept": "application/json",
			"X-Support-Key": settings.site_key,
			"X-Support-Secret": settings.get_password("site_secret", raise_exception=False) or "",
		},
		timeout=TIMEOUT,
	)
	try:
		body = resp.json()
	except ValueError:
		body = {}
	if resp.status_code >= 400:
		message = body.get("message")
		detail = (message.get("error") if isinstance(message, dict) else None) or body.get("exc_type") or resp.text[:300]
		raise HubRequestError(f"{resp.status_code}: {detail}", resp.status_code)
	return body.get("message") or {}


def _commit(app):
	import subprocess

	try:
		out = subprocess.run(
			["git", "rev-parse", "HEAD"], cwd=frappe.get_app_source_path(app), capture_output=True, text=True, timeout=5
		)
		return out.stdout.strip() or None if out.returncode == 0 else None
	except Exception:
		return None


def _versions():
	"""This app and what it runs on; never the rest of the site's apps."""
	from frappe.utils.change_log import get_versions

	try:
		return {
			app: {**{k: info.get(k) for k in ("version", "branch")}, "commit": _commit(app) if app == APP else None}
			for app, info in get_versions().items()
			if app in (APP, "frappe", "erpnext")
		}
	except Exception:
		return {}


@frappe.whitelist()
def test_connection():
	frappe.only_for("System Manager")
	settings = get_settings()
	if not (settings.hub_url and settings.site_key):
		frappe.throw(_("Set Hub URL, Site Key and Site Secret first"))
	try:
		result = _post("ping", {"schema_version": SCHEMA_VERSION}, settings)
		status = _("Connected as {0} at {1}").format(result.get("site"), now_datetime())
		_store_ignored(result)
	except Exception as e:
		status = _("Failed at {0}: {1}").format(now_datetime(), str(e)[:300])
	frappe.db.set_single_value(SETTINGS, "connection_status", status)
	return status


def _store_ignored(result):
	ignored = result.get("ignored_errors")
	if isinstance(ignored, str) and ignored != (get_settings().hub_ignored_errors or ""):
		frappe.db.set_single_value(SETTINGS, "hub_ignored_errors", ignored)
		frappe.clear_document_cache(SETTINGS)


# ------------------------------------------------------------------ errors


def on_error_log(doc, method=None):
	"""doc_events hook on Error Log. Must never raise."""
	try:
		settings = get_settings()
		if not is_enabled(settings):
			return
		tb = code_only(doc.error)
		if not is_ours(tb) or f"{APP}/support.py" in tb:
			return  # another app's error, or our own delivery failing (a loop)
		if any(p.strip() and p.strip() in tb for p in (settings.hub_ignored_errors or "").splitlines()):
			return
		fingerprint = fingerprint_of(tb, doc.method)
		existing = frappe.db.get_value(
			REPORT,
			{
				"fingerprint": fingerprint,
				"report_type": "Error",
				"creation": [">", add_to_date(now_datetime(), hours=-DEDUPE_HOURS)],
			},
		)
		if existing:
			frappe.db.sql(
				"""update `tabE Invoice Support Report`
				set occurrence_count = occurrence_count + 1, unsent_occurrences = unsent_occurrences + 1
				where name = %s""",
				existing,
			)
			return
		lines = [ln for ln in tb.splitlines() if ln.strip()]
		report = frappe.get_doc(
			{
				"doctype": REPORT,
				"report_type": "Error",
				"priority": "Medium",
				"title": redact(lines[-1] if lines else doc.method or "Server error")[:200],
				"description": redact(f"Method: {doc.method}\nError Log: {doc.name}"),
				"traceback": _mask(tb),
				"fingerprint": fingerprint,
				"reported_by": doc.owner if doc.owner != "Guest" else None,
			}
		)
		report.flags.from_error_log = True
		report.insert(ignore_permissions=True)
		# retry_pending delivers it within 5 minutes; no job from inside error logging
	except Exception:
		pass


def _mask(tb):
	return redact(_SQL_LITERAL.sub(lambda m: m.group(1) + "'?'", tb))


# ------------------------------------------------------------------ reports


def enqueue_send(name):
	frappe.enqueue(
		"flick_einvoicing.support.send_report",
		name=name,
		queue="short",
		job_id=f"flick-support-send-{name}",
		deduplicate=True,
		enqueue_after_commit=True,
	)


def _screenshot(report):
	if not report.screenshot:
		return []
	try:
		file = frappe.get_doc("File", {"file_url": report.screenshot})
		content = file.get_content()
	except Exception:
		return []
	if isinstance(content, str):
		content = content.encode()
	if len(content) > MAX_SCREENSHOT_BYTES:
		return []
	return [{"file_name": file.file_name, "content": base64.b64encode(content).decode()}]


def _payload(report):
	context = frappe.parse_json(report.page_context) if report.page_context else {}
	sent = bool(report.remote_ticket)
	return {
		"schema_version": SCHEMA_VERSION,
		"versions": _versions(),
		"report": {
			"id": report.name,
			"report_type": report.report_type,
			"priority": report.priority,
			"title": report.title,
			"description": report.description,
			"route": report.route,
			"reported_by": report.reported_by,
			"reporter_name": get_fullname(report.reported_by) if report.reported_by else None,
			"page_context": context,
			"traceback": report.traceback,
			"fingerprint": report.fingerprint,
			# first delivery carries the full count, later ones only what happened since
			"occurrences": (cint(report.unsent_occurrences) if sent else cint(report.occurrence_count)) or 1,
		},
		"attachments": [] if sent else _screenshot(report),
	}


def send_report(name):
	settings = get_settings()
	if not is_enabled(settings):
		return
	report = frappe.get_doc(REPORT, name)
	if report.send_status == "Sent" and not cint(report.unsent_occurrences):
		return
	attempts = cint(report.attempts) + 1
	values = {"attempts": attempts}
	try:
		result = _post("receive_report", _payload(report), settings)
	except Exception as e:
		permanent = isinstance(e, HubRequestError) and e.status_code in PERMANENT_ERRORS
		giving_up = permanent or attempts >= MAX_ATTEMPTS
		values.update(
			send_status="Failed",
			last_error=str(e)[:1000],
			next_retry_at=None if giving_up else add_to_date(now_datetime(), minutes=min(2**attempts, 360)),
		)
	else:
		values.update(
			send_status="Sent",
			sent_at=now_datetime(),
			last_error=None,
			next_retry_at=None,
			unsent_occurrences=0,
			remote_ticket=result.get("ticket"),
			remote_status=result.get("status"),
		)
	frappe.db.set_value(REPORT, name, values, update_modified=False)
	frappe.db.commit()


def retry_pending():
	"""Every 5 minutes: anything still waiting (hub was down, new error occurrences...)."""
	if not is_enabled():
		return
	now = now_datetime()
	for row in frappe.get_all(
		REPORT,
		filters={"send_status": ["in", ["Queued", "Failed"]], "attempts": ["<", MAX_ATTEMPTS]},
		fields=["name", "send_status", "next_retry_at"],
		limit=50,
	):
		if row.next_retry_at and row.next_retry_at > now:
			continue
		# a failed report with no retry time hit a permanent error; leave it alone
		if row.send_status == "Failed" and not row.next_retry_at:
			continue
		send_report(row.name)

	for name in frappe.get_all(REPORT, filters={"send_status": "Sent", "unsent_occurrences": [">", 0]}, pluck="name", limit=50):
		send_report(name)

	for parent in frappe.get_all(
		"E Invoice Support Message",
		filters={"sender": "Client", "delivery_status": "Pending", "attempts": ["<", MAX_ATTEMPTS]},
		pluck="parent",
		distinct=True,
		limit=50,
	):
		send_replies(parent)


def hold_unsent():
	"""Support was turned off: what was waiting must not go out by itself if it is turned on again."""
	frappe.db.set_value(
		REPORT, {"send_status": ["in", ["Queued", "Failed"]]}, {"send_status": "Not Sent", "next_retry_at": None}, update_modified=False
	)
	frappe.db.sql("update `tabE Invoice Support Report` set unsent_occurrences = 0")
	frappe.db.set_value(
		"E Invoice Support Message",
		{"sender": "Client", "delivery_status": "Pending"},
		"delivery_status",
		"Not Sent",
		update_modified=False,
	)


@frappe.whitelist(methods=["POST"])
def retry_report(name):
	"""A person sends a report again, after a failure or after support was off."""
	report = frappe.get_doc(REPORT, name)
	report.check_permission("read")
	if not is_enabled():
		frappe.throw(_("Support is turned off. A System Manager can enable it in Flick Support Settings."))
	frappe.db.set_value(REPORT, name, {"attempts": 0, "send_status": "Queued", "next_retry_at": None})
	enqueue_send(name)


# ------------------------------------------------------------------ conversation


@frappe.whitelist(methods=["POST"])
def reply_to_report(name, message, reopen=0):
	"""The reporter answers support, or reopens a resolved or closed report."""
	report = frappe.get_doc(REPORT, name)
	report.check_permission("read")
	if not is_enabled():
		frappe.throw(_("Support is turned off. A System Manager can enable it in Flick Support Settings."))
	if not report.remote_ticket:
		frappe.throw(_("This report has not reached support yet. Try again in a few minutes."))
	text = (redact(message, keep_emails=True) or "").strip()[:MAX_MESSAGE]
	if not text:
		frappe.throw(_("Write a message first"))
	report.append(
		"messages",
		{
			"message_id": frappe.generate_hash(length=20),
			"sender": "Client",
			"sender_name": get_fullname(frappe.session.user),
			"sender_email": frappe.session.user,
			"message": text,
			"sent_at": now_datetime(),
			"reopen": cint(reopen),
			"delivery_status": "Pending",
		},
	)
	report.save(ignore_permissions=True)
	frappe.enqueue(
		"flick_einvoicing.support.send_replies",
		name=name,
		queue="short",
		job_id=f"flick-support-reply-{name}",
		deduplicate=True,
		enqueue_after_commit=True,
	)


def send_replies(name):
	settings = get_settings()
	if not is_enabled(settings):
		return
	report = frappe.get_doc(REPORT, name)
	for row in report.messages:
		if row.sender != "Client" or row.delivery_status != "Pending":
			continue
		values = {"attempts": cint(row.attempts) + 1}
		try:
			result = _post(
				"receive_reply",
				{
					"schema_version": SCHEMA_VERSION,
					"reply": {
						"id": row.message_id,
						"ticket": report.remote_ticket,
						"report": report.name,
						"message": row.message,
						"reopen": cint(row.reopen),
						"sender_name": row.sender_name,
						"sender_email": row.sender_email,
						"sent_at": to_wire_time(row.sent_at),
					},
				},
				settings,
			)
		except Exception as e:
			permanent = isinstance(e, HubRequestError) and e.status_code in PERMANENT_ERRORS
			values["last_error"] = str(e)[:1000]
			if permanent or values["attempts"] >= MAX_ATTEMPTS:
				values["delivery_status"] = "Failed"
		else:
			values.update(delivery_status="Delivered", last_error=None)
			if result.get("status") in TICKET_STATUSES:
				frappe.db.set_value(REPORT, name, "remote_status", result["status"], update_modified=False)
		frappe.db.set_value(row.doctype, row.name, values, update_modified=False)
	frappe.db.commit()


# ------------------------------------------------------------------ what the hub calls


def _reject(code, message):
	frappe.local.response["http_status_code"] = code
	return {"error": message}


def _refuse_unless_hub():
	"""None when the caller holds this site's key and secret (so it is the hub), else the refusal."""
	settings = get_settings()
	if not is_enabled(settings):
		return _reject(503, "Support is not enabled on this site")
	ip = frappe.local.request_ip or "unknown"
	fail_key = f"flick_support:hubauthfail:{ip}"
	if cint(frappe.cache.get_value(fail_key)) >= AUTH_FAIL_LIMIT:
		return _reject(429, "Too many failed attempts")
	if (frappe.request.content_length or 0) > MAX_UPDATE_BYTES:
		return _reject(413, "Payload too large")
	key = (frappe.get_request_header("X-Support-Key") or "").strip()
	secret = (frappe.get_request_header("X-Support-Secret") or "").strip()
	stored = settings.get_password("site_secret", raise_exception=False) or ""
	if not (
		stored
		and hmac.compare_digest((settings.site_key or "").encode(), key.encode())
		and hmac.compare_digest(stored.encode(), secret.encode())
	):
		frappe.cache.set_value(fail_key, cint(frappe.cache.get_value(fail_key)) + 1, expires_in_sec=600)
		return _reject(401, "Invalid credentials")
	return None


@frappe.whitelist(allow_guest=True, methods=["POST"])
def report_versions():
	"""The hub asks what is running, to confirm a deploy without waiting for the heartbeat."""
	refused = _refuse_unless_hub()
	if refused:
		return refused
	return {"ok": True, "versions": _versions()}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def receive_update():
	"""The hub pushes ticket status, resolution and replies here."""
	refused = _refuse_unless_hub()
	if refused:
		return refused
	try:
		updates = json.loads(frappe.request.get_data(as_text=True) or "{}").get("updates")
		if not isinstance(updates, list):
			raise ValueError
	except (ValueError, AttributeError):
		return _reject(400, "Invalid JSON")
	user = frappe.session.user
	frappe.set_user("Administrator")  # the caller is the hub, not a user
	try:
		applied = sum(1 for update in updates[:50] if apply_update(update))
	finally:
		frappe.set_user(user)
	return {"ok": True, "applied": applied}


def _save_images(name, images):
	for image in (images or [])[:3]:
		try:
			content = base64.b64decode(image.get("content") or "", validate=True)
		except Exception:
			continue
		if not content or len(content) > MAX_SCREENSHOT_BYTES:
			continue
		frappe.get_doc(
			{
				"doctype": "File",
				"file_name": str(image.get("file_name") or "screenshot.png").rsplit("/", 1)[-1][:100],
				"attached_to_doctype": REPORT,
				"attached_to_name": name,
				"is_private": 1,
				"content": content,
			}
		).insert(ignore_permissions=True)


def apply_update(update):
	"""Bring a report in line with its hub ticket. Safe to repeat: known replies are skipped."""
	if not isinstance(update, dict) or not update.get("ticket"):
		return False
	# one hub ticket can cover several reports of the same error; replies go on the newest
	names = frappe.get_all(REPORT, filters={"remote_ticket": str(update["ticket"])}, pluck="name", order_by="creation desc")
	if not names:
		return False
	status = update.get("status") if update.get("status") in TICKET_STATUSES else None
	resolution = str(update.get("resolution") or "")[:MAX_TEXT]
	status_changed = False
	for name in names:
		current = frappe.db.get_value(REPORT, name, ["remote_status", "resolution"], as_dict=True)
		values = {}
		if status and status != current.remote_status:
			values["remote_status"] = status
			status_changed = True
		if resolution != (current.resolution or ""):
			values["resolution"] = resolution
		if values:
			frappe.db.set_value(REPORT, name, values, update_modified=False)

	report = frappe.get_doc(REPORT, names[0])
	known = {row.message_id for row in report.messages}
	replies = 0
	for m in (update.get("messages") if isinstance(update.get("messages"), list) else [])[:100]:
		if not (isinstance(m, dict) and m.get("id") and m.get("message")) or m["id"] in known:
			continue
		report.append(
			"messages",
			{
				"message_id": str(m["id"])[:64],
				"sender": "Support",
				"sender_name": str(m.get("sender_name") or "")[:140],
				"message": str(m["message"])[:MAX_TEXT],
				"sent_at": from_wire_time(m.get("sent_at")),
				"delivery_status": "Received",
			},
		)
		_save_images(report.name, m.get("attachments"))
		replies += 1
	if replies:
		report.save(ignore_permissions=True)
	if report.report_type != "Error" and (replies or status_changed):
		subject = _("Support replied on: {0}") if replies else _("Your report is now {0}: {1}").format(_(status), "{0}")
		_notify(report, subject.format(report.title))
	return True


def _notify(report, subject):
	if not report.reported_by or report.reported_by == "Administrator":
		return
	frappe.get_doc(
		{
			"doctype": "Notification Log",
			"for_user": report.reported_by,
			"type": "Alert",
			"document_type": REPORT,
			"document_name": report.name,
			"subject": subject,
		}
	).insert(ignore_permissions=True)


# ------------------------------------------------------------------ heartbeat


def _collect_heartbeat():
	"""Only this app's own errors and jobs. Disk, backups and the scheduler belong to whoever runs
	the site, so the hub never hears about them."""
	since = add_to_date(now_datetime(), hours=-24)
	jobs = frappe.get_all("Scheduled Job Type", filters={"method": ["like", f"{APP}.%"]}, pluck="name")
	failed = (
		frappe.get_all(
			"Scheduled Job Log",
			filters={"status": "Failed", "creation": [">", since], "scheduled_job_type": ["in", jobs]},
			fields=["scheduled_job_type as job", {"COUNT": "*", "as": "count"}],
			group_by="scheduled_job_type",
			order_by="count desc",
			limit=5,
		)
		if jobs
		else []
	)
	return {
		"site_url": get_url(),
		"error_logs_24h": frappe.db.count(REPORT, {"report_type": "Error", "creation": [">", since]}),
		"failed_jobs_24h": sum(cint(row.count) for row in failed),
		"failed_jobs": failed,
	}


def send_heartbeat():
	settings = get_settings()
	if not is_enabled(settings):
		return
	tickets = frappe.get_all(
		REPORT,
		filters={"send_status": "Sent", "remote_ticket": ["is", "set"], "remote_status": ["!=", "Closed"]},
		pluck="remote_ticket",
		limit=200,
	)
	payload = {"schema_version": SCHEMA_VERSION, "versions": _versions(), "heartbeat": _collect_heartbeat(), "tickets": tickets}
	try:
		result = _post("heartbeat", payload, settings)
	except Exception as e:
		frappe.db.set_single_value(SETTINGS, "last_heartbeat_error", str(e)[:500])
		return
	_store_ignored(result)
	for update in result.get("updates") or []:
		try:
			apply_update(update)
		except Exception:
			frappe.log_error("Flick support: update failed")
	frappe.db.set_single_value(SETTINGS, {"last_heartbeat_at": now_datetime(), "last_heartbeat_error": None})
