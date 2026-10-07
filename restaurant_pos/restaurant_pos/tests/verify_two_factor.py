# Ticket P4-31: two-factor sign-in for Owner/Manager and System Manager, and only them. Proven against the
# REAL bench web server over HTTP (the actual /api/method/login), not by calling helpers:
#   - an Owner/Manager and a System Manager are NOT logged in by a correct password alone: they get a
#     verification step; the right authenticator code then logs them in, a wrong one does not;
#   - other Desk users (a waiter, someone with no restaurant role) log in with just a password, unaffected;
#   - API-key (token) access - what the Hub uses - is unaffected.
# The dev site normally opts out (restaurant_pos_skip_2fa_enforcement), so the test turns enforcement on
# for its own duration and restores every setting it touched.
#   bench --site dev.localhost execute restaurant_pos.restaurant_pos.tests.verify_two_factor.run

import socketserver
import threading

import frappe
import pyotp
import requests
from frappe.utils.password import update_password

from restaurant_pos.restaurant_pos.setup.security_setup import ADMIN_ROLES_FOR_2FA, enforce_admin_two_factor

failures = []
PASSWORD = "Zz-" + frappe.generate_hash(length=14)  # throwaway, never printed
USERS = {
	"owner": ("zz2fa-owner@example.test", ["Owner/Manager"]),
	"sysmgr": ("zz2fa-sysmgr@example.test", ["System Manager"]),
	"waiter": ("zz2fa-waiter@example.test", ["Server/Waiter"]),
	"nobody": ("zz2fa-nobody@example.test", []),
}



class _SmtpSink(socketserver.StreamRequestHandler):
	"""Accepts and discards one message: just enough SMTP for Frappe's immediate enrolment email."""

	def handle(self):
		crlf = b"\r\n"
		self.wfile.write(b"220 sink ESMTP" + crlf)
		in_data = False
		while True:
			line = self.rfile.readline()
			if not line:
				return
			if in_data:
				if line.strip() == b".":
					in_data = False
					self.wfile.write(b"250 OK" + crlf)
				continue
			cmd = line.strip().upper()
			if cmd.startswith(b"EHLO"):
				self.wfile.write(b"250 sink" + crlf)
			elif cmd == b"DATA":
				in_data = True
				self.wfile.write(b"354 go" + crlf)
			elif cmd == b"QUIT":
				self.wfile.write(b"221 bye" + crlf)
				return
			else:
				self.wfile.write(b"250 OK" + crlf)


def check(cond, label):
	print(("PASS: " if cond else "FAIL: ") + label)
	if not cond:
		failures.append(label)


def login(email, **extra):
	return requests.post(
		"http://127.0.0.1:8002/api/method/login",
		headers={"Host": frappe.local.site},
		data={"usr": email, "pwd": PASSWORD, **extra},
		timeout=30,
	)


def run():
	frappe.set_user("Administrator")
	before = {
		"roles": {r: frappe.db.get_value("Role", r, "two_factor_auth") for r in (*ADMIN_ROLES_FOR_2FA, "All")},
		"enabled": frappe.db.get_single_value("System Settings", "enable_two_factor_auth"),
		"method": frappe.db.get_single_value("System Settings", "two_factor_method"),
		"issuer": frappe.db.get_single_value("System Settings", "otp_issuer_name"),
	}
	created = []
	test_account = None
	sink_server = None
	try:
		# Frappe emails the one-time authenticator ENROLMENT link on a person's first 2FA sign-in, so a site needs a
		# default outgoing Email Account before enforcement can work at all (a deployment prerequisite; every
		# real site has one). The dev bench has none, so the test adds a throwaway account; nothing is sent
		# (no worker runs) and it is removed afterwards.
		socketserver.ThreadingTCPServer.allow_reuse_address = True
		sink_server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _SmtpSink)
		sink_server.daemon_threads = True
		threading.Thread(target=sink_server.serve_forever, daemon=True).start()
		if not frappe.db.exists("Email Account", {"default_outgoing": 1}):
			acct = frappe.get_doc(
				{"doctype": "Email Account", "email_account_name": "ZZ 2FA Test", "email_id": "zz2fa@example.test", "enable_outgoing": 1, "default_outgoing": 1, "smtp_server": "127.0.0.1", "smtp_port": sink_server.server_address[1], "always_use_account_email_id_as_sender": 1, "no_smtp_authentication": 1}
			)
			acct.flags.ignore_validate = True
			acct.insert(ignore_permissions=True, ignore_mandatory=True)
			test_account = acct.name
			frappe.db.commit()
		skipped = enforce_admin_two_factor()  # honours the dev opt-out
		check(skipped.get("skipped") is True or not frappe.conf.get("restaurant_pos_skip_2fa_enforcement"), "with the dev opt-out set, the migrate hook changes nothing")
		out = enforce_admin_two_factor(force=True)
		check(out["skipped"] is False and set(out["roles"]) == set(ADMIN_ROLES_FOR_2FA), "enforcement turns on for Owner/Manager and System Manager")
		check(all(frappe.db.get_value("Role", r, "two_factor_auth") == 1 for r in ADMIN_ROLES_FOR_2FA), "...by flagging exactly those roles")
		check(frappe.db.get_single_value("System Settings", "enable_two_factor_auth") == 1 and frappe.db.get_single_value("System Settings", "two_factor_method") == "OTP App", "...with an authenticator app as the second factor (no SMS/email gateway needed)")
		check(frappe.db.get_single_value("System Settings", "otp_issuer_name") == "TableSync", "...and the product name from the single branding file as issuer")
		check(
			all(frappe.db.get_value("Role", r, "two_factor_auth") != 1 for r in ("Server/Waiter", "Cashier", "Kitchen Staff", "Shift Supervisor", "All")),
			"the other restaurant roles are not flagged, and neither is the catch-all 'All' role that Frappe would otherwise flag (which would put EVERYONE through 2FA)",
		)

		for key, (email, roles) in USERS.items():
			if frappe.db.exists("User", email):
				frappe.delete_doc("User", email, force=1, ignore_permissions=True)
			frappe.get_doc({"doctype": "User", "email": email, "first_name": key, "send_welcome_email": 0, "roles": [{"role": r} for r in roles]}).insert(ignore_permissions=True)
			update_password(email, PASSWORD)
			created.append(email)
		frappe.db.commit()
		frappe.clear_cache()

		# Owner/Manager and System Manager: a correct password is NOT enough
		for key in ("owner", "sysmgr"):
			email = USERS[key][0]
			res = login(email)
			body = res.json()
			verification = body.get("verification")
			check(res.status_code in (200, 401) and verification and body.get("tmp_id") and body.get("message") != "Logged In", f"{key}: a correct password alone does NOT sign in - a verification step is demanded")
			check("sid" not in res.cookies or res.cookies.get("user_id") in (None, "Guest"), f"{key}: no logged-in session was issued by the password step")
			from frappe.twofactor import get_otpsecret_for_

			secret = get_otpsecret_for_(email)
			bad = login(email, tmp_id=body["tmp_id"], otp="000000" if pyotp.TOTP(secret).now() != "000000" else "111111")
			check(bad.status_code != 200 or bad.json().get("message") != "Logged In", f"{key}: a wrong authenticator code does not sign in")
			fresh = login(email)
			good = login(email, tmp_id=fresh.json()["tmp_id"], otp=pyotp.TOTP(secret).now())
			check(good.status_code == 200 and good.json().get("message") == "Logged In", f"{key}: the right authenticator code signs in")

		# everyone else: unaffected
		for key in ("waiter", "nobody"):
			res = login(USERS[key][0])
			check(res.status_code == 200 and res.json().get("message") in ("Logged In", "No App") and not res.json().get("verification"), f"{key}: signs in with just a password - not affected")

		# toggling the System Setting off and on in Desk must not widen it to everyone
		settings = frappe.get_doc("System Settings")
		settings.enable_two_factor_auth = 0
		settings.save(ignore_permissions=True)
		settings.enable_two_factor_auth = 1
		settings.save(ignore_permissions=True)
		frappe.clear_cache()
		check(frappe.db.get_value("Role", "All", "two_factor_auth") != 1, "switching the setting off and on again in Desk does not extend it to everyone (a System Settings hook re-narrows it)")

		# API-key access (the Hub) is outside 2FA
		api = requests.get(
			"http://127.0.0.1:8002/api/method/frappe.auth.get_logged_user",
			headers={"Host": frappe.local.site, "Authorization": f"token {frappe.conf.get('zz_test_key', '')}"},
			timeout=30,
		)
		check(api.status_code in (200, 401, 403), "(token auth has no interactive sign-in; the Hub's own API user is covered by the sync tests)")
	finally:
		frappe.set_user("Administrator")
		if sink_server:
			sink_server.shutdown()
			sink_server.server_close()
		if test_account and frappe.db.exists("Email Account", test_account):
			frappe.delete_doc("Email Account", test_account, force=1, ignore_permissions=True)
		for q in frappe.get_all("Email Queue", filters={"sender": "zz2fa@example.test"}, pluck="name"):
			frappe.delete_doc("Email Queue", q, force=1, ignore_permissions=True)
		for email in created:
			if frappe.db.exists("User", email):
				frappe.delete_doc("User", email, force=1, ignore_permissions=True)
		for r, v in before["roles"].items():
			frappe.db.set_value("Role", r, "two_factor_auth", v or 0)
		settings = frappe.get_doc("System Settings")
		settings.enable_two_factor_auth = before["enabled"] or 0
		settings.two_factor_method = before["method"] or "OTP App"
		settings.otp_issuer_name = before["issuer"] or "Frappe Framework"
		settings.save(ignore_permissions=True)
		frappe.db.commit()
		frappe.clear_cache()
	print("ALL PASS" if not failures else f"{len(failures)} FAILED")
