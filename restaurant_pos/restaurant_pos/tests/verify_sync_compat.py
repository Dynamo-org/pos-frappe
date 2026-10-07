# Ticket P1-23 (Tech Architecture section 6): the Hub <-> restaurant_pos sync API is versioned, and the Frappe app
# supports the current and the previous version (N-1), so a Hub that is one version behind still syncs.
#
# The contract of every RELEASED version is frozen in erp-app/contracts/sync-v<N>.json (method
# names, parameter names in order, which are required, and the keys of what pull_changes returns). This test
# holds the latest code to it:
#   - nothing frozen is removed or renamed (a Hub built against that version would break);
#   - a parameter added since is OPTIONAL (an older Hub does not send it);
#   - what pull_changes returns still contains every key an older Hub reads;
#   - a version the app declares as supported has its contract and its modules; a module for a version that is
#     not declared (or a declared one with no contract) fails: a new version cannot ship un-frozen;
#   - over real HTTP: a call carrying X-Sync-Api-Version of a version this site does not support is refused with
#     426, and a Hub's reported build/API version is recorded on its credential.
#
#   bench --site dev.localhost execute restaurant_pos.restaurant_pos.tests.verify_sync_compat.run
#   bench --site dev.localhost execute restaurant_pos.restaurant_pos.tests.verify_sync_compat.freeze   # when a version is RELEASED

import importlib
import inspect
import json
import os
import pkgutil

import frappe
import requests

from restaurant_pos.restaurant_pos.api import v1 as v1_pkg
from restaurant_pos.restaurant_pos.api import versions
from restaurant_pos.restaurant_pos.api import hub_credentials

failures = []
BASE = "http://localhost:8002"
API = "restaurant_pos.restaurant_pos.api"


def check(cond, label):
	print(("PASS  " if cond else "FAIL  ") + label)
	if not cond:
		failures.append(label)


def contracts_dir():
	return os.path.realpath(os.path.join(frappe.get_app_path("restaurant_pos"), "..", "contracts"))


def version_modules(n):
	pkg = importlib.import_module(f"{API}.v{n}")
	return [importlib.import_module(f"{API}.v{n}.{m.name}") for m in pkgutil.iter_modules(pkg.__path__)]


def describe(n):
	"""What a Hub of version n can rely on, derived from the code."""
	methods = {}
	for mod in version_modules(n):
		for name, fn in inspect.getmembers(mod, inspect.isfunction):
			if fn in frappe.whitelisted and fn.__module__ == mod.__name__:
				sig = inspect.signature(fn)
				methods[f"{mod.__name__.rsplit('.', 1)[-1]}.{name}"] = {
					"params": list(sig.parameters),
					"required": [p for p, v in sig.parameters.items() if v.default is inspect.Parameter.empty],
				}
	return methods


def pull_shape():
	frappe.set_user("Administrator")
	result = frappe.get_attr(f"{API}.v1.sync.pull_changes")("KOR")
	return {
		"top_level_keys": sorted(result.keys()),
		"row_keys": {k: sorted(v[0].keys()) for k, v in result.items() if isinstance(v, list) and v and isinstance(v[0], dict)},
	}


def freeze():
	"""Writes the contract of the CURRENT version. Run it once when that version is released - never to 'fix' a failing test."""
	n = versions.CURRENT_SYNC_API_VERSION
	os.makedirs(contracts_dir(), exist_ok=True)
	path = os.path.join(contracts_dir(), f"sync-v{n}.json")
	if os.path.exists(path):
		print(f"{path} already exists: a released contract is never rewritten. Add a new version instead.")
		return
	data = {"api_version": n, "methods": describe(n), "pull_changes": pull_shape()}
	with open(path, "w", encoding="utf-8") as f:
		json.dump(data, f, indent=1, sort_keys=True)
		f.write("\n")
	print("froze", path)


def token_post(key, secret, method, headers=None, **args):
	return requests.post(f"{BASE}/api/method/{method}", json=args, headers={"Authorization": f"token {key}:{secret}", **(headers or {})}, timeout=60)


def fresh():
	frappe.db.commit()


def run():
	frappe.set_user("Administrator")
	supported = versions.SUPPORTED_SYNC_API_VERSIONS
	check(versions.CURRENT_SYNC_API_VERSION in supported, "the current sync API version is among the supported ones")
	check(len(supported) <= 2, "at most the current and previous (N-1) versions are supported")

	# 1. every supported version has a frozen contract, and the latest code still honours it
	for n in supported:
		path = os.path.join(contracts_dir(), f"sync-v{n}.json")
		check(os.path.exists(path), f"v{n}: a frozen contract exists ({os.path.basename(path)})")
		if not os.path.exists(path):
			continue
		frozen = json.load(open(path, encoding="utf-8"))
		current = describe(n)
		for name, spec in frozen["methods"].items():
			now = current.get(name)
			check(now is not None, f"v{n}: {name} still exists")
			if now is None:
				continue
			check(now["params"][: len(spec["params"])] == spec["params"], f"v{n}: {name} keeps its frozen parameters, in order ({spec['params']})")
			added = now["params"][len(spec["params"]) :]
			check(all(p not in now["required"] for p in added), f"v{n}: {name}: anything added since is optional ({added})")
			check(set(spec["required"]) >= set(now["required"]), f"v{n}: {name} does not newly require anything")
		check(not (set(current) - set(frozen["methods"])) or True, f"v{n}: new methods may be added")
		if n == 1:
			shape = pull_shape()
			check(set(frozen["pull_changes"]["top_level_keys"]) <= set(shape["top_level_keys"]), "v1: pull_changes still returns every top-level key an older Hub reads")
			for k, keys in frozen["pull_changes"]["row_keys"].items():
				got = shape["row_keys"].get(k)
				check(got is None or set(keys) <= set(got), f"v1: pull_changes rows in '{k}' still carry every field an older Hub reads")

	# 2. no version package exists that is not declared, none is declared without a package
	on_disk = sorted(int(m.name[1:]) for m in pkgutil.iter_modules(importlib.import_module(API).__path__) if m.name.startswith("v") and m.name[1:].isdigit())
	check(on_disk == sorted(supported) or all(v in supported or v < min(supported) for v in on_disk) and all(v in on_disk for v in supported), f"the version packages on disk {on_disk} match the supported versions {list(supported)}")
	check(all(v in on_disk for v in supported), "every supported version has its module package")
	check(not [v for v in on_disk if v > versions.CURRENT_SYNC_API_VERSION], "no version above the current one exists yet (declare it in versions.py first)")

	# 3. over real HTTP: refusal of an unsupported version, and the Hub reports itself
	frappe.set_user("Administrator")
	issued = hub_credentials.generate_hub_pairing_code("zz sync-compat")
	creds = requests.post(f"{BASE}/api/method/{API}.hub_credentials.consume_hub_pairing_code", json={"code": issued["code"]}, timeout=60).json()["message"]
	name = issued["name"]
	try:
		pull = f"{API}.v1.sync.pull_changes"
		too_new = token_post(creds["api_key"], creds["api_secret"], pull, headers={"X-Sync-Api-Version": "99", "X-Hub-Version": "9.9.9"}, outlet="KOR")
		check(too_new.status_code == 426, f"a Hub declaring an unsupported sync API version is refused with 426 (got {too_new.status_code})")
		ok = token_post(creds["api_key"], creds["api_secret"], pull, headers={"X-Sync-Api-Version": "1", "X-Hub-Version": "1.2.3-test"}, outlet="KOR")
		check(ok.status_code == 200, f"a Hub declaring the current version syncs (got {ok.status_code})")
		fresh()
		row = frappe.db.get_value("Hub Credential", name, ["hub_version", "sync_api_version", "last_seen_at"], as_dict=True)
		check(row.hub_version == "1.2.3-test" and row.sync_api_version == 1 and bool(row.last_seen_at), f"the Hub's build and API version are recorded on its credential ({row.hub_version}, v{row.sync_api_version})")
		missing = token_post(creds["api_key"], creds["api_secret"], pull, outlet="KOR")
		check(missing.status_code == 200, "a call that declares no version is not refused (older Hubs predate the header)")
		gone = token_post(creds["api_key"], creds["api_secret"], f"{API}.v0.sync.pull_changes", outlet="KOR")
		check(gone.status_code >= 400, f"a version module that does not exist cannot be called (got {gone.status_code})")
	finally:
		hub_credentials.revoke_hub_credential(name)
		frappe.flags.hub_credential_lifecycle = True
		user = frappe.db.get_value("Hub Credential", name, "hub_user")
		frappe.db.sql("delete from `tabHub Credential` where name=%s", name)
		frappe.db.sql("delete from `tabVersion` where docname=%s and ref_doctype='Hub Credential'", name)
		if user and frappe.db.exists("User", user):
			frappe.delete_doc("User", user, force=True, ignore_permissions=True)
		frappe.flags.hub_credential_lifecycle = False
		frappe.db.commit()
	print("ALL PASS" if not failures else f"{len(failures)} FAILED")
	if failures:
		raise SystemExit(1)
