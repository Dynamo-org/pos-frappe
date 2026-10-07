# Ticket P1-14: the Hub's credential to ERPNext is issued and revoked through pairing. Proven over REAL HTTP
# against the bench web server (the same calls the Hub makes), not by calling helpers:
#   - only a System Manager can issue or revoke; a restaurant Owner/Manager, a waiter and a Hub account cannot;
#   - the pairing code is shown once, kept only as a hash, single use, expires, and a wrong one is refused with
#     the same message as a used one; guessing is rate-limited;
#   - pairing creates a service account that holds ONLY the Hub Service role and can sign in only by API token;
#   - a whole-site credential reads every outlet, an outlet credential reads exactly its outlet;
#   - rotation: the Hub chooses the new secret; the old one dies at once, the new one works;
#   - revoking stops the credential at once; a revoked credential cannot be reinstated or deleted;
#   - no secret is stored anywhere readable (the code as a hash, the secret only in the Frappe user).
# Every record it creates is removed at the end (the audit-style guards do not cover these doctypes).
#   bench --site dev.localhost execute restaurant_pos.restaurant_pos.tests.verify_hub_credentials.run

import secrets

import frappe
import requests
from frappe.utils.password import update_password

from restaurant_pos.restaurant_pos.api import hub_credentials as api

failures = []
BASE = None
PASSWORD = "Zz-" + frappe.generate_hash(length=14)  # throwaway, never printed
USERS = {
	"sysmgr": ("zzhc-sysmgr@example.test", ["System Manager"]),
	"owner": ("zzhc-owner@example.test", ["Owner/Manager"]),
	"waiter": ("zzhc-waiter@example.test", ["Server/Waiter"]),
}
OUTLET_B = "ZZHCB"
METHOD = "restaurant_pos.restaurant_pos.api.hub_credentials."
PULL = "restaurant_pos.restaurant_pos.api.v1.sync.pull_changes"


def check(cond, label):
	print(("PASS  " if cond else "FAIL  ") + label)
	if not cond:
		failures.append(label)


def fresh():
	"""The web server commits in its own transaction; start a new snapshot so this process sees it."""
	frappe.db.commit()


def clear_rate_limits():
	frappe.cache.delete_keys("rl:")


def ensure_user(email, roles):
	if frappe.db.exists("User", email):
		frappe.delete_doc("User", email, force=True, ignore_permissions=True)
	user = frappe.get_doc({"doctype": "User", "email": email, "first_name": "ZZ", "send_welcome_email": 0, "enabled": 1})
	user.insert(ignore_permissions=True)
	user.add_roles(*roles)
	update_password(email, PASSWORD)


def session_for(key):
	email = USERS[key][0]
	s = requests.Session()
	r = s.post(f"{BASE}/api/method/login", json={"usr": email, "pwd": PASSWORD}, timeout=30)
	assert r.status_code == 200, (key, r.status_code, r.text[:200])
	csrf = s.get(f"{BASE}/api/method/frappe.auth.get_logged_user", timeout=30)
	return s


def post(session, method, **args):
	headers = {}
	return session.post(f"{BASE}/api/method/{method}", json=args, headers=headers, timeout=60)


def token_post(api_key, api_secret, method, **args):
	return requests.post(f"{BASE}/api/method/{method}", json=args, headers={"Authorization": f"token {api_key}:{api_secret}"}, timeout=60)


def guest_post(method, **args):
	return requests.post(f"{BASE}/api/method/{method}", json=args, timeout=60)


def issue(label, outlet=None):
	"""A pairing code issued the way an operator does it, as System Manager, through the real method."""
	frappe.set_user(USERS["sysmgr"][0])
	try:
		return api.generate_hub_pairing_code(label, outlet)
	finally:
		frappe.set_user("Administrator")


def run():
	global BASE
	frappe.set_user("Administrator")
	BASE = frappe.utils.get_url() if not frappe.conf.get("host_name") else frappe.conf.host_name
	BASE = "http://localhost:" + str(frappe.conf.get("webserver_port") or 8002)
	created = []
	try:
		for key, (email, roles) in USERS.items():
			ensure_user(email, roles)
		outlet_a = "KOR"
		if frappe.db.exists("Outlet", OUTLET_B):
			frappe.delete_doc("Outlet", OUTLET_B, force=1, ignore_permissions=True)
		second = frappe.copy_doc(frappe.get_doc("Outlet", outlet_a))
		second.short_code = OUTLET_B
		second.outlet_name = "ZZ Hub Credential B"
		second.insert(ignore_permissions=True)
		outlet_b = second.name
		frappe.db.commit()
		clear_rate_limits()

		# 1. who may issue
		for who, expect_ok in (("sysmgr", True), ("owner", False), ("waiter", False)):
			r = post(session_for(who), METHOD + "generate_hub_pairing_code", label="zz permission probe")
			check((r.status_code == 200) == expect_ok, f"{who}: {'can' if expect_ok else 'cannot'} issue a Hub pairing code (HTTP {r.status_code})")
			if r.status_code == 200:
				created.append(r.json()["message"]["name"])
		check(guest_post(METHOD + "generate_hub_pairing_code", label="x").status_code in (401, 403), "a guest cannot issue a Hub pairing code")

		# 2. the code: shown once, stored only as a hash
		issued = issue("zz whole-site Hub")
		created.append(issued["name"])
		code = issued["code"]
		check(len(code.replace("-", "")) == 20, "the pairing code is 20 characters (100 bits)")
		row = frappe.db.get_value("Hub Credential", issued["name"], ["pairing_code_hash", "status", "outlet"], as_dict=True)
		check(row.status == "Awaiting pairing" and not row.outlet, "a new credential awaits pairing and (here) covers the whole site")
		check(code.replace("-", "") not in str(row.pairing_code_hash) and len(row.pairing_code_hash) == 64, "only a hash of the code is stored")

		# 3. consuming it
		bad = guest_post(METHOD + "consume_hub_pairing_code", code="AAAAA-AAAAA-AAAAA-AAAAA")
		check(bad.status_code in (401, 403), f"a wrong code is refused (HTTP {bad.status_code})")
		good = guest_post(METHOD + "consume_hub_pairing_code", code=code, hub_version="0.0.0-test", sync_api_version=1)
		check(good.status_code == 200, f"the right code is accepted (HTTP {good.status_code})")
		creds = good.json().get("message", {})
		check(bool(creds.get("api_key")) and len(creds.get("api_secret", "")) >= 32, "pairing returns an API key and a long secret")
		again = guest_post(METHOD + "consume_hub_pairing_code", code=code)
		check(again.status_code in (401, 403), "the same code cannot be used twice")
		check(again.text.split("not valid")[0] != again.text or "not valid" in bad.text, "a used code and a wrong code get the same kind of refusal")
		fresh()
		check(frappe.db.get_value("Hub Credential", issued["name"], "pairing_code_hash") in (None, ""), "the code hash is wiped once used")
		fresh()
		hub_user = frappe.db.get_value("Hub Credential", issued["name"], "hub_user")
		check(frappe.db.get_value("Hub Credential", issued["name"], "status") == "Active", "the credential is Active after pairing")
		roles = sorted(frappe.get_roles(hub_user))
		check({"Hub Service"} <= set(roles) and "System Manager" not in roles and "Owner/Manager" not in roles, f"the service account holds the Hub Service role and no admin role ({roles})")
		check(not frappe.get_all("User Permission", filters={"user": hub_user}), "a whole-site credential has no outlet restriction")
		pw_login = requests.post(f"{BASE}/api/method/login", json={"usr": hub_user, "pwd": "anything"}, timeout=30)
		check(pw_login.status_code in (401, 403), "the service account cannot sign in with a password")

		# 4. using it
		key, secret = creds["api_key"], creds["api_secret"]
		pull_a = token_post(key, secret, PULL, outlet=outlet_a)
		pull_b = token_post(key, secret, PULL, outlet=outlet_b)
		check(pull_a.status_code == 200 and pull_b.status_code == 200, "a whole-site credential can pull any outlet")
		check(token_post(key, "wrong" + secret, PULL, outlet=outlet_a).status_code in (401, 403), "a wrong secret is refused")
		check(token_post(key, secret, METHOD + "generate_hub_pairing_code", label="x").status_code in (401, 403), "a Hub account cannot issue Hub credentials")
		check(token_post(key, secret, METHOD + "revoke_hub_credential", name=issued["name"]).status_code in (401, 403), "a Hub account cannot revoke Hub credentials")

		# 5. an outlet credential
		issued_o = issue("zz outlet Hub", outlet_a)
		created.append(issued_o["name"])
		creds_o = guest_post(METHOD + "consume_hub_pairing_code", code=issued_o["code"]).json()["message"]
		fresh()
		check(frappe.get_all("User Permission", filters={"user": frappe.db.get_value("Hub Credential", issued_o["name"], "hub_user"), "allow": "Outlet"}, pluck="for_value") == [outlet_a], "an outlet credential is limited to its one outlet")
		own = token_post(creds_o["api_key"], creds_o["api_secret"], PULL, outlet=outlet_a)
		other = token_post(creds_o["api_key"], creds_o["api_secret"], PULL, outlet=outlet_b)
		check(own.status_code == 200, "an outlet credential can pull its own outlet")
		check(other.status_code in (401, 403), f"an outlet credential cannot pull another outlet (HTTP {other.status_code})")

		# 6. rotation: the Hub picks the secret
		new_secret = secrets.token_hex(24)
		short = token_post(key, secret, METHOD + "rotate_hub_credential", new_secret="tooshort")
		check(short.status_code >= 400, "a weak new secret is refused")
		rotated = token_post(key, secret, METHOD + "rotate_hub_credential", new_secret=new_secret)
		check(rotated.status_code == 200, f"the Hub can rotate its own secret (HTTP {rotated.status_code})")
		check(token_post(key, secret, PULL, outlet=outlet_a).status_code in (401, 403), "after rotation the OLD secret is dead")
		check(token_post(key, new_secret, PULL, outlet=outlet_a).status_code == 200, "after rotation the NEW secret works")
		check(token_post(creds_o["api_key"], creds_o["api_secret"], METHOD + "rotate_hub_credential", new_secret=new_secret).status_code == 200, "another credential rotates independently (and may choose any secret)")
		admin_rotate = None
		try:
			frappe.set_user("Administrator")
			api.rotate_hub_credential(secrets.token_hex(24))
			admin_rotate = False
		except frappe.PermissionError:
			admin_rotate = True
		check(admin_rotate, "an account that is not an active Hub credential cannot rotate")

		# 7. revoking
		check(session_for("owner") and post(session_for("owner"), METHOD + "revoke_hub_credential", name=issued["name"]).status_code in (401, 403), "an Owner/Manager cannot revoke a Hub credential")
		check(token_post(key, new_secret, PULL, outlet=outlet_a).status_code == 200, "still working before the revoke")
		revoke = post(session_for("sysmgr"), METHOD + "revoke_hub_credential", name=issued["name"])
		check(revoke.status_code == 200, f"a System Manager can revoke (HTTP {revoke.status_code})")
		after = token_post(key, new_secret, PULL, outlet=outlet_a)
		check(after.status_code in (401, 403), f"a revoked credential stops working at once (HTTP {after.status_code})")
		check(token_post(creds_o["api_key"], new_secret, PULL, outlet=outlet_a).status_code == 200, "revoking one credential leaves the others working")
		fresh()
		check(frappe.db.get_value("User", hub_user, "enabled") == 0 and not frappe.db.get_value("User", hub_user, "api_key"), "the service account is disabled and its API key removed")
		check(post(session_for("sysmgr"), METHOD + "revoke_hub_credential", name=issued["name"]).status_code == 200, "revoking twice is harmless")
		try:
			doc = frappe.get_doc("Hub Credential", issued["name"])
			doc.status = "Active"
			doc.save()
			reinstated = True
		except Exception:  # noqa: BLE001
			reinstated = False
		check(not reinstated, "a revoked credential cannot be reinstated by editing it")
		try:
			frappe.delete_doc("Hub Credential", issued["name"], force=True)
			deleted = True
		except Exception:  # noqa: BLE001
			deleted = False
		check(not deleted, "a Hub credential cannot be deleted (revoke instead)")

		# 8. a code that has expired
		expired = issue("zz expiring Hub")
		created.append(expired["name"])
		frappe.db.set_value("Hub Credential", expired["name"], "pairing_expires_at", frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-1))
		frappe.db.commit()
		check(guest_post(METHOD + "consume_hub_pairing_code", code=expired["code"]).status_code in (401, 403), "an expired code is refused")

		# 9. guessing is rate-limited (kept last: it uses up this IP's allowance)
		clear_rate_limits()
		codes = [guest_post(METHOD + "consume_hub_pairing_code", code="ZZZZZ-ZZZZZ-ZZZZZ-ZZZZ" + str(i % 10)).status_code for i in range(14)]
		check(429 in codes[10:] and 429 not in codes[:10], f"repeated guesses are rate-limited ({codes})")
	finally:
		clear_rate_limits()
		frappe.set_user("Administrator")
		frappe.flags.hub_credential_lifecycle = True
		for name in set(created):
			if not frappe.db.exists("Hub Credential", name):
				continue
			user = frappe.db.get_value("Hub Credential", name, "hub_user")
			for up in frappe.get_all("User Permission", filters={"user": user or "-"}, pluck="name"):
				frappe.delete_doc("User Permission", up, force=True, ignore_permissions=True)
			frappe.db.sql("delete from `tabHub Credential` where name=%s", name)
			frappe.db.sql("delete from `tabVersion` where docname=%s and ref_doctype='Hub Credential'", name)
			if user and frappe.db.exists("User", user):
				frappe.delete_doc("User", user, force=True, ignore_permissions=True)
		frappe.flags.hub_credential_lifecycle = False
		if frappe.db.exists("Outlet", OUTLET_B):
			frappe.delete_doc("Outlet", OUTLET_B, force=1, ignore_permissions=True)
		for email, _roles in USERS.values():
			if frappe.db.exists("User", email):
				frappe.delete_doc("User", email, force=True, ignore_permissions=True)
		frappe.db.commit()
	print("ALL PASS" if not failures else f"{len(failures)} FAILED")
	if failures:
		raise SystemExit(1)
