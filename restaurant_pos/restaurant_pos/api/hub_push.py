# Copyright (c) 2026, the project contributors
# For license information, please see license.txt

# Tickets P3-27 (signed Frappe -> Hub pushes), P3-32 (change hints + cached version check) and P3-04 (latency-sensitive
# events). Tech Architecture section 6:
#
#   * Frappe tells an outlet's Hub about a change by calling the Hub's URL (stored on the Outlet): an internal address
#     for the Cloud Hub, the tunnel hostname for an Edge Hub - the SAME code path in both tiers.
#   * Every push is SIGNED: HMAC-SHA256 with a per-outlet secret over `timestamp.nonce.body`. The Hub rejects unsigned,
#     badly signed, stale (> 5 min) and replayed pushes.
#   * Two kinds of push, with different guarantees:
#       - a CHANGE HINT ("something changed, pull now") carries no data and needs no ledger: a lost hint is caught by
#         the Hub's next poll, which first asks a cheap, cached version check (master_version) and pulls only if it moved;
#       - an EVENT (a UPI payment confirmed, a WhatsApp delivery status) carries data, is written to the Hub Event ledger
#         BEFORE the push, is marked Delivered when the Hub acknowledges it, and - if the push never gets through - is
#         handed to the Hub in its next pull (pull_changes returns the pending events), so nothing is lost.
#   * Nothing here ever blocks or breaks the save that triggered it: failures are logged and the next pull catches up.

import hashlib
import hmac
import json
import secrets
import time
import uuid
from urllib.parse import urlparse

import frappe
import requests
from frappe.utils import add_to_date, cint, now_datetime
from frappe.utils.password import get_decrypted_password, set_encrypted_password

SIGNATURE_VERSION = "v1"
PUSH_PATH = "/api/inbound/push"
# The Hub accepts the PREVIOUS secret for this long after a rotation, so pushes already in flight (and a Hub that has
# not pulled the new secret yet) are not rejected. After that only the current secret works.
PREVIOUS_SECRET_VALID_SECONDS = 3600
CONNECT_TIMEOUT, READ_TIMEOUT = 1.5, 3.0
# The direct push of a latency-sensitive event, made inside the request that caused it, right after its commit: short, because a
# Hub that does not answer must not hold that request up (the queue and the next pull take over).
FAST_CONNECT_TIMEOUT, FAST_READ_TIMEOUT = 0.5, 1.0

# Master data the Hub caches and pulls (everything pull_changes returns). A change to any of these may need a hint.
MASTER_DOCTYPES = ("Item", "Item Price", "Floor", "Restaurant Table", "Station", "Printer", "Staff PIN", "Modifier Group", "Device", "Floor Permission", "Outlet")
OUTLET_FIELD_DOCTYPES = ("Floor", "Restaurant Table", "Station", "Printer", "Device", "Floor Permission")
MAXMOD_TTL_SECONDS = 120


# ---------------------------------------------------------------------------------------------------------------------
# signing

def sign(secret: str, timestamp: str, nonce: str, body: bytes) -> str:
	mac = hmac.new(secret.encode(), f"{timestamp}.{nonce}.".encode() + body, hashlib.sha256).hexdigest()
	return f"{SIGNATURE_VERSION}={mac}"


def get_push_secret(outlet: str, which: str = "current") -> str | None:
	field = "hub_push_secret" if which == "current" else "hub_push_secret_previous"
	return get_decrypted_password("Outlet", outlet, field, raise_exception=False)


def ensure_push_secret(outlet: str) -> str:
	"""The outlet's current push secret, created on first use. 32 random bytes; stored in a Frappe Password field
	(encrypted at rest), never returned by any browser-facing method - only by the Hub-facing pull."""
	secret = get_push_secret(outlet)
	if secret:
		return secret
	secret = secrets.token_hex(32)
	set_encrypted_password("Outlet", outlet, secret, "hub_push_secret")
	frappe.db.set_value("Outlet", outlet, "hub_push_secret_rotated_at", now_datetime(), update_modified=False)
	return secret


def rotate_push_secret(outlet: str) -> None:
	"""New current secret; the old one stays acceptable to the Hub for an hour (PREVIOUS_SECRET_VALID_SECONDS)."""
	old = ensure_push_secret(outlet)
	set_encrypted_password("Outlet", outlet, old, "hub_push_secret_previous")
	set_encrypted_password("Outlet", outlet, secrets.token_hex(32), "hub_push_secret")
	frappe.db.set_value("Outlet", outlet, "hub_push_secret_rotated_at", now_datetime(), update_modified=False)


def push_secrets_for_hub(outlet: str) -> dict:
	"""What pull_changes hands the Hub (Hub Service only): the current secret, and the previous one while it is still valid."""
	current = ensure_push_secret(outlet)
	rotated_at = frappe.db.get_value("Outlet", outlet, "hub_push_secret_rotated_at")
	previous = get_push_secret(outlet, "previous")
	valid_until = None
	if previous and rotated_at:
		valid_until = add_to_date(rotated_at, seconds=PREVIOUS_SECRET_VALID_SECONDS)
		if valid_until <= now_datetime():
			previous, valid_until = None, None
	return {"current": current, "previous": previous, "previous_valid_until": valid_until.isoformat() if valid_until else None}


# ---------------------------------------------------------------------------------------------------------------------
# delivery

def _hub_url(outlet: str) -> str | None:
	"""Where ERPNext reaches this outlet's Hub. Normally the Outlet's Hub URL. Site config `restaurant_pos_hub_push_url_overrides`
	({"KOR": "https://hub-internal.example"}) names a different address for PUSHES only (devices keep using the Outlet's
	Hub URL): for a Hub reachable from ERPNext by an internal address, or a development bench in WSL that cannot reach the
	Windows host as localhost. A signed push crosses the network: https only, except a local development address
	(or when site config `restaurant_pos_allow_http_hub` is set, for the same development case)."""
	override = (frappe.conf.get("restaurant_pos_hub_push_url_overrides") or {}).get(outlet)
	url = (override or frappe.db.get_value("Outlet", outlet, "hub_url") or "").strip()
	if not url:
		return None
	parsed = urlparse(url)
	local = parsed.hostname in ("localhost", "127.0.0.1", "::1") or (parsed.hostname or "").endswith(".localhost")
	if parsed.scheme != "https" and not (parsed.scheme == "http" and (local or frappe.conf.get("restaurant_pos_allow_http_hub"))):
		return None
	return url.rstrip("/")


def deliver(outlet: str, event: str, data: dict | None = None, event_uuid: str | None = None, fast: bool = False) -> bool:
	"""One signed POST to the outlet's Hub. True only if the Hub accepted it. Never raises."""
	base = _hub_url(outlet)
	if not base:
		return False
	body = json.dumps({"event": event, "event_uuid": event_uuid or uuid.uuid4().hex, "outlet": outlet, "data": data or {}, "sent_at": now_datetime().isoformat()}, separators=(",", ":"), sort_keys=True).encode()
	timestamp, nonce = str(int(time.time())), uuid.uuid4().hex
	headers = {
		"Content-Type": "application/json",
		"X-Push-Outlet": outlet,
		"X-Push-Timestamp": timestamp,
		"X-Push-Nonce": nonce,
		"X-Push-Signature": sign(ensure_push_secret(outlet), timestamp, nonce, body),
	}
	try:
		response = requests.post(base + PUSH_PATH, data=body, headers=headers, timeout=(FAST_CONNECT_TIMEOUT, FAST_READ_TIMEOUT) if fast else (CONNECT_TIMEOUT, READ_TIMEOUT))
		return response.status_code == 200
	except requests.RequestException as err:
		frappe.logger("restaurant_pos").info(f"push to Hub of {outlet} failed ({type(err).__name__}); the next pull will catch up")
		return False


# ---------------------------------------------------------------------------------------------------------------------
# change hints (P3-32)

def _counter_key(outlet: str) -> bytes:
	return frappe.cache.make_key(f"restaurant_pos:master_counter:{outlet}")


def bump_master_version(outlet: str) -> None:
	frappe.cache.incrby(_counter_key(outlet), 1)


def _max_modified() -> str:
	"""Latest `modified` across the master doctypes: the safety net for a change no hook saw (direct SQL, an import).
	Cached for MAXMOD_TTL_SECONDS, so an idle outlet's poll costs a cache read, not a database query."""
	cached = frappe.cache.get_value("restaurant_pos:master_maxmod")
	if cached:
		return cached
	latest = None
	for doctype in MASTER_DOCTYPES:
		value = frappe.db.sql(f"select max(modified) from `tab{doctype}`")[0][0]
		if value and (latest is None or value > latest):
			latest = value
	stamp = str(int(latest.timestamp())) if latest else "0"
	frappe.cache.set_value("restaurant_pos:master_maxmod", stamp, expires_in_sec=MAXMOD_TTL_SECONDS)
	return stamp


def master_version(outlet: str) -> str:
	counter = cint(frappe.cache.get(_counter_key(outlet)) or 0)
	return f"{counter}.{_max_modified()}"


def send_changed_hint(outlet: str) -> None:
	"""Background job: tell the Hub to pull now. The job id is the outlet, so a burst of saves queues ONE hint."""
	deliver(outlet, "changed", {"version": master_version(outlet)})


def _affected_outlets(doc) -> list[str]:
	if doc.doctype == "Outlet":
		return [doc.name]
	if doc.doctype in OUTLET_FIELD_DOCTYPES:
		return [doc.get("outlet")] if doc.get("outlet") else []
	if doc.doctype == "Staff PIN":
		before = doc.get_doc_before_save()
		rows = list(doc.get("assignments") or []) + (list(before.get("assignments") or []) if before else [])
		return sorted({r.outlet for r in rows if r.outlet})
	if doc.doctype == "Item Price":
		return frappe.get_all("Outlet", filters={"price_list": doc.price_list}, pluck="name") if doc.get("price_list") else []
	# Item, Modifier Group: the menu is shared by every outlet
	return frappe.get_all("Outlet", pluck="name")


def on_master_change(doc, method=None):
	"""doc_events hook for every master doctype. Bumps the version of each affected outlet and queues one coalesced hint
	per outlet that has a Hub URL. Never raises: it must not break the save that triggered it."""
	try:
		for outlet in _affected_outlets(doc):
			bump_master_version(outlet)
			if _hub_url(outlet):
				frappe.enqueue(
					"restaurant_pos.restaurant_pos.api.hub_push.send_changed_hint",
					queue="short",
					job_id=f"hub-hint:{frappe.local.site}:{outlet}",
					deduplicate=True,
					enqueue_after_commit=True,
					outlet=outlet,
				)
	except Exception:  # noqa: BLE001
		frappe.logger("restaurant_pos").exception("could not queue a Hub change hint")


# ---------------------------------------------------------------------------------------------------------------------
# events (P3-04)

def emit_hub_event(outlet: str, event_type: str, data: dict) -> str:
	"""Record a latency-sensitive event for the outlet's Hub and push it. The ledger row comes first, so the event
	survives an unreachable Hub: it is delivered by the push (marked Delivered on the Hub's acknowledgement) or, failing
	that, in the Hub's next pull. Returns the event uuid."""
	event_uuid = uuid.uuid4().hex
	frappe.get_doc(
		{"doctype": "Hub Event", "outlet": outlet, "event_type": event_type, "event_uuid": event_uuid, "data_json": json.dumps(data, sort_keys=True), "status": "Pending", "attempts": 0}
	).insert(ignore_permissions=True)

	def _push_after_commit():
		# Latency: a Frappe background job takes ~1 s just to start, so the first attempt is made HERE, straight after the
		# commit, with short timeouts. If it fails, a queued job tries once more; and whatever happens the event is on the
		# ledger, so the Hub's next pull delivers it.
		try:
			delivered = push_event(event_uuid, fast=True)
			frappe.db.commit()  # records the attempt / the Delivered mark (a no-op for the callback list: it has already run)
			if not delivered:
				frappe.enqueue("restaurant_pos.restaurant_pos.api.hub_push.push_event", queue="short", event_uuid=event_uuid)
		except Exception:  # noqa: BLE001
			frappe.logger("restaurant_pos").exception("could not push a Hub event")

	frappe.db.after_commit.add(_push_after_commit)
	return event_uuid


def push_event(event_uuid: str, fast: bool = False) -> bool:
	name = frappe.db.get_value("Hub Event", {"event_uuid": event_uuid, "status": "Pending"}, "name")
	if not name:
		return True  # already delivered (by push or by pull)
	row = frappe.db.get_value("Hub Event", name, ["outlet", "event_type", "data_json", "attempts"], as_dict=True)
	ok = deliver(row.outlet, row.event_type, json.loads(row.data_json), event_uuid, fast=fast)
	values = {"attempts": cint(row.attempts) + 1}
	if ok:
		values.update({"status": "Delivered", "delivered_at": now_datetime(), "delivered_via": "push"})
	frappe.db.set_value("Hub Event", name, values, update_modified=False)
	return ok  # no commit here: a background job commits on success, and _push_after_commit commits for the direct path


def pending_events_for_hub(outlet: str, limit: int = 100) -> list[dict]:
	rows = frappe.get_all("Hub Event", filters={"outlet": outlet, "status": "Pending"}, fields=["event_uuid", "event_type", "data_json", "creation"], order_by="creation asc", limit=limit)
	return [{"event_uuid": r.event_uuid, "event_type": r.event_type, "data": json.loads(r.data_json or "{}"), "created_at": r.creation.isoformat()} for r in rows]


def acknowledge_events(outlet: str, event_uuids: list[str]) -> int:
	count = 0
	for event_uuid in event_uuids[:500]:
		name = frappe.db.get_value("Hub Event", {"event_uuid": event_uuid, "outlet": outlet, "status": "Pending"}, "name")
		if name:
			frappe.db.set_value("Hub Event", name, {"status": "Delivered", "delivered_at": now_datetime(), "delivered_via": "pull"}, update_modified=False)
			count += 1
	return count
