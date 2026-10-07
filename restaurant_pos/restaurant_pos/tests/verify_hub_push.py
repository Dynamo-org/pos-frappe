# Tickets P3-27 / P3-32 / P3-04 (ERPNext half): signed pushes to a Hub, change hints with a cached version check, and the
# event ledger. Proven in-process against a stand-in Hub (a real HTTP server in this test that VERIFIES the signature the
# way the Node Hub does: the same fixed vector is asserted on both sides), plus the real database:
#   - the signature is the documented HMAC-SHA256 over `timestamp.nonce.body`, with a fixed vector shared with the Hub's test;
#   - the push secret is created once, stored encrypted (the table column holds only stars), rotated with a one-hour
#     grace for the previous secret, and handed out only by the Hub-facing pull;
#   - a master-data change moves the outlet's version (and only an affected outlet's), an unchanged outlet's version is
#     stable (so the Hub's poll is a cheap no-op), and the version is read BEFORE a pull collects;
#   - an event is written to the ledger first; with the Hub unreachable it stays Pending, is handed over by pull_changes,
#     and is closed by the Hub's acknowledgement; with the Hub reachable the push delivers it and marks it Delivered;
#   - nothing here raises into the save that triggered it.
#   bench --site dev.localhost execute restaurant_pos.restaurant_pos.tests.verify_hub_push.run

import hashlib
import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import frappe
from frappe.utils import add_to_date, now_datetime

from restaurant_pos.restaurant_pos.api import hub_push
from restaurant_pos.restaurant_pos.api.v1.sync import get_master_version, pull_changes

failures = []
OUTLET = "KOR"
VECTOR_BODY = b'{"event":"payment_confirmed","event_uuid":"ev-0001-abcdef","outlet":"KOR","data":{"amount_paise":12500},"sent_at":"2026-10-04T10:00:00"}'
VECTOR_SIGNATURE = "v1=c257df068103df4c929029350e6e1cf55a3ac863eb506fc10d55c3bac038e66e"


def check(cond, label):
	print(("PASS  " if cond else "FAIL  ") + label)
	if not cond:
		failures.append(label)


class StandInHub(BaseHTTPRequestHandler):
	secret = ""
	received = []

	def do_POST(self):  # noqa: N802
		body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
		ts, nonce, sig = self.headers.get("X-Push-Timestamp", ""), self.headers.get("X-Push-Nonce", ""), self.headers.get("X-Push-Signature", "")
		expected = "v1=" + hmac.new(self.secret.encode(), f"{ts}.{nonce}.".encode() + body, hashlib.sha256).hexdigest()
		ok = hmac.compare_digest(expected, sig) and abs(time.time() - int(ts)) < 300 and self.path == "/api/inbound/push"
		if ok:
			StandInHub.received.append({"headers": dict(self.headers), "body": json.loads(body)})
		self.send_response(200 if ok else 401)
		self.end_headers()
		self.wfile.write(b"{}")

	def log_message(self, *args):  # silence
		pass


def with_override(url, fn):
	previous = frappe.conf.get("restaurant_pos_hub_push_url_overrides")
	frappe.conf.restaurant_pos_hub_push_url_overrides = {OUTLET: url}
	try:
		return fn()
	finally:
		if previous is None:
			frappe.conf.pop("restaurant_pos_hub_push_url_overrides", None)
		else:
			frappe.conf.restaurant_pos_hub_push_url_overrides = previous


def run():
	frappe.set_user("Administrator")
	server = None
	original = {f: frappe.db.get_value("Outlet", OUTLET, f) for f in ("hub_push_secret_rotated_at",)}
	saved_secret = hub_push.get_push_secret(OUTLET)
	saved_prev = hub_push.get_push_secret(OUTLET, "previous")
	created_events = []
	try:
		# 1. the signature
		check(hub_push.sign("push-secret-for-test", "1700000000", "abcdef0123456789abcdef", VECTOR_BODY) == VECTOR_SIGNATURE, "the signature matches the fixed vector the Hub's test asserts too")

		# 2. the secret
		secret = hub_push.ensure_push_secret(OUTLET)
		check(len(secret) == 64 and hub_push.ensure_push_secret(OUTLET) == secret, "an outlet's push secret is 32 random bytes and stays the same once created")
		column = frappe.db.sql("select hub_push_secret from `tabOutlet` where name=%s", OUTLET)[0][0]
		check(secret not in str(column), f"the secret is not in the Outlet table (the column holds {column!r})")
		check(frappe.db.get_value("Outlet", OUTLET, "hub_push_secret") != secret, "a normal read of the Outlet does not return it")

		hub_push.rotate_push_secret(OUTLET)
		fresh = hub_push.get_push_secret(OUTLET)
		check(fresh != secret and hub_push.get_push_secret(OUTLET, "previous") == secret, "rotation makes a new current secret and keeps the old one as previous")
		handed = hub_push.push_secrets_for_hub(OUTLET)
		check(handed["current"] == fresh and handed["previous"] == secret and handed["previous_valid_until"], "the Hub is handed both, with the time the previous one stops being valid")
		frappe.db.set_value("Outlet", OUTLET, "hub_push_secret_rotated_at", add_to_date(now_datetime(), hours=-2), update_modified=False)
		check(hub_push.push_secrets_for_hub(OUTLET)["previous"] is None, "two hours after a rotation the previous secret is no longer handed out")

		# 3. a push, verified by a stand-in Hub
		StandInHub.secret = hub_push.get_push_secret(OUTLET)
		server = HTTPServer(("127.0.0.1", 0), StandInHub)
		threading.Thread(target=server.serve_forever, daemon=True).start()
		url = f"http://127.0.0.1:{server.server_port}"
		ok = with_override(url, lambda: hub_push.deliver(OUTLET, "changed", {"version": "x"}))
		check(ok is True and StandInHub.received and StandInHub.received[-1]["body"]["event"] == "changed", "ERPNext's signed push is accepted by a verifier written independently of it")
		h = StandInHub.received[-1]["headers"]
		check(h.get("X-Push-Outlet") == OUTLET and h.get("X-Push-Signature", "").startswith("v1=") and len(h.get("X-Push-Nonce", "")) == 32, "it carries the outlet, a one-time nonce and the signature")
		check(abs(int(h["X-Push-Timestamp"]) - time.time()) < 10, "...and a current timestamp")
		StandInHub.secret = "not-the-secret"
		check(with_override(url, lambda: hub_push.deliver(OUTLET, "changed", {})) is False, "a Hub that cannot verify it refuses, and deliver reports False without raising")
		check(with_override("http://127.0.0.1:9", lambda: hub_push.deliver(OUTLET, "changed", {})) is False, "an unreachable Hub is False, not an exception")
		check(with_override("http://example.com", lambda: hub_push._hub_url(OUTLET)) is None, "a plain-http push address that is not local is not used (https only)") if not frappe.conf.get("restaurant_pos_allow_http_hub") else None

		# 4. versions
		v0 = get_master_version(OUTLET)["version"]
		check(get_master_version(OUTLET)["version"] == v0, "an idle outlet's version is stable between checks (the Hub's poll is a no-op)")
		hub_push.bump_master_version(OUTLET)
		v1 = get_master_version(OUTLET)["version"]
		check(v1 != v0 and v1.split(".")[0] == str(int(v0.split(".")[0]) + 1), "a change moves the version")
		before_pull = hub_push.master_version(OUTLET)
		page = pull_changes(OUTLET)
		check(page["master_version"] == before_pull, "a pull reports the version read BEFORE it collected")
		check(set(("push_secrets", "events", "master_version")) <= set(page), "pull_changes carries the push secrets, the pending events and the version (additions to the v1 contract)")

		# a real Item Price change moves the counter of the outlets on that price list
		price = frappe.db.get_value("Outlet", OUTLET, "price_list")
		row = frappe.get_all("Item Price", filters={"price_list": price}, fields=["name", "price_list_rate"], limit=1)
		if row:
			c0 = int(hub_push.master_version(OUTLET).split(".")[0])
			doc = frappe.get_doc("Item Price", row[0].name)
			original_rate = doc.price_list_rate
			doc.price_list_rate = original_rate + 1
			doc.save(ignore_permissions=True)
			c1 = int(hub_push.master_version(OUTLET).split(".")[0])
			doc.reload()
			doc.price_list_rate = original_rate
			doc.save(ignore_permissions=True)
			check(c1 == c0 + 1, "saving an Item Price moves the version of an outlet on that price list")
		else:
			print("SKIP  no Item Price on this site")

		# 5. events: unreachable Hub -> stays pending, handed over by pull, closed by acknowledgement
		def emit_with_unreachable():
			uuid_ = hub_push.emit_hub_event(OUTLET, "payment_confirmed", {"amount_paise": 12500})
			created_events.append(uuid_)
			return uuid_

		ev1 = emit_with_unreachable()
		check(frappe.db.get_value("Hub Event", {"event_uuid": ev1}, "status") == "Pending", "an event is on the ledger (Pending) before any push is attempted")
		check(with_override("http://127.0.0.1:9", lambda: hub_push.push_event(ev1)) is False, "with the Hub's endpoint unreachable the push fails...")
		row1 = frappe.db.get_value("Hub Event", {"event_uuid": ev1}, ["status", "attempts"], as_dict=True)
		check(row1.status == "Pending" and row1.attempts == 1, "...the event stays Pending and the attempt is counted")
		pulled = pull_changes(OUTLET)["events"]
		check(any(e["event_uuid"] == ev1 and e["data"]["amount_paise"] == 12500 for e in pulled), "the next pull hands the Hub the pending event, with its data")
		pull_changes(OUTLET, ack_events=json.dumps([ev1]))
		check(frappe.db.get_value("Hub Event", {"event_uuid": ev1}, ["status", "delivered_via"], as_dict=True) == {"status": "Delivered", "delivered_via": "pull"}, "once the Hub acknowledges it, the event is Delivered (via pull)")
		check(all(e["event_uuid"] != ev1 for e in pull_changes(OUTLET)["events"]), "...and it is not handed over again")
		check(pull_changes(OUTLET, ack_events=json.dumps([ev1, "no-such-event"])) is not None, "acknowledging an unknown id is harmless")

		# 6. events: reachable Hub -> the push delivers and the ledger closes
		StandInHub.secret = hub_push.get_push_secret(OUTLET)
		StandInHub.received.clear()
		ev2 = emit_with_unreachable()
		check(with_override(url, lambda: hub_push.push_event(ev2)) is True and StandInHub.received[-1]["body"]["event_uuid"] == ev2, "with the Hub reachable the push delivers the event, carrying its id (the Hub dedupes by it)")
		check(frappe.db.get_value("Hub Event", {"event_uuid": ev2}, ["status", "delivered_via"], as_dict=True) == {"status": "Delivered", "delivered_via": "push"}, "...and the ledger marks it Delivered (via push)")
		check(hub_push.push_event(ev2) is True and len(StandInHub.received) == 1, "pushing an already delivered event again sends nothing")

		# 7. a hook never breaks a save
		class Boom:
			doctype = "Item"
			name = "zz"

			def get(self, *_):
				raise RuntimeError("boom")

		check(hub_push.on_master_change(Boom()) is None, "the change hook swallows its own failures: it can never break the save that triggered it")
	finally:
		if server:
			server.shutdown()
		frappe.set_user("Administrator")
		frappe.flags.hub_credential_lifecycle = False
		for uuid_ in created_events:
			frappe.db.sql("delete from `tabHub Event` where event_uuid=%s", uuid_)
		# put the outlet's secrets back exactly as they were
		if saved_secret:
			from frappe.utils.password import set_encrypted_password

			set_encrypted_password("Outlet", OUTLET, saved_secret, "hub_push_secret")
			if saved_prev:
				set_encrypted_password("Outlet", OUTLET, saved_prev, "hub_push_secret_previous")
			frappe.db.set_value("Outlet", OUTLET, "hub_push_secret_rotated_at", original["hub_push_secret_rotated_at"], update_modified=False)
		frappe.db.commit()
	print("ALL PASS" if not failures else f"{len(failures)} FAILED")
	if failures:
		raise SystemExit(1)
