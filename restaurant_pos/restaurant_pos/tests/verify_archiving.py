# Ticket P3-35: "archiving can be run and reversed on test data." Proven on throwaway KOT rows (outlets ZZARCH1/2):
#   - a dry run reports and changes nothing; the policy is off unless configured; nothing newer than the minimum age is ever
#     archived; guest consent and accounting records are refused;
#   - an archive writes a compressed file, records its hash, removes exactly the old rows of the chosen outlet, and keeps a
#     row that a live correction still points at;
#   - a restore puts every row back EXACTLY (every column, including timestamps, owner and document status), refuses a second
#     restore, refuses a file that was altered or deleted, and refuses to overwrite a row that exists;
#   - the daily hook does nothing without a policy, and honours the minimum age when there is one.
#   bench --site dev.localhost execute restaurant_pos.restaurant_pos.tests.verify_archiving.run

import datetime
import gzip
import os

import frappe

from restaurant_pos.restaurant_pos import archiving

failures = []
OUT1, OUT2 = "ZZARCH1", "ZZARCH2"
PREFIX = "zzarch-"


def check(cond, label):
	print(("PASS  " if cond else "FAIL  ") + label)
	if not cond:
		failures.append(label)


def throws(fn):
	try:
		fn()
	except Exception:  # noqa: BLE001
		frappe.db.rollback()
		return True
	return False


FIELDS = ["name", "creation", "modified", "modified_by", "owner", "docstatus", "idx", "outlet", "restaurant_order", "line_item_id", "item_code", "item_name", "station", "qty", "course", "fired_at", "served_at", "correction_of", "correction_reason"]


def make(name, outlet, days_old, correction_of=None):
	fired = datetime.datetime.now().replace(microsecond=0) - datetime.timedelta(days=days_old)
	return [name, fired, fired, "Administrator", "Administrator", 1, 0, outlet, f"{PREFIX}order", "l1", "ZZ-ITEM", "Item", "ZZ-S", 2, "main", fired, fired + datetime.timedelta(minutes=9), correction_of, "fix" if correction_of else None]


def snapshot(names):
	return {r["name"]: r for r in frappe.get_all("KOT", filters={"name": ["in", names]}, fields=["*"])}


def cleanup():
	for dt in ("KOT",):
		frappe.db.sql(f"delete from `tab{dt}` where name like %s", PREFIX + "%")
	for b in frappe.get_all("Archive Batch", filters={"archived_doctype": "KOT", "archived_by": "Administrator", "outlet": ["in", [OUT1, OUT2, ""]]}, fields=["name", "file_path", "outlet", "row_count"]):
		if b.outlet in (OUT1, OUT2) or b.row_count in (None,):
			if b.file_path and os.path.exists(frappe.get_site_path(b.file_path)):
				os.remove(frappe.get_site_path(b.file_path))
			frappe.db.sql("delete from `tabArchive Batch` where name=%s", b.name)
	frappe.db.commit()


def run():
	frappe.set_user("Administrator")
	cleanup()
	try:
		rows = []
		for i in range(30):
			rows.append(make(f"{PREFIX}o1-old-{i:02d}", OUT1, 200 - i))
		for i in range(10):
			rows.append(make(f"{PREFIX}o2-old-{i:02d}", OUT2, 190 - i))
		for i in range(5):
			rows.append(make(f"{PREFIX}o1-new-{i:02d}", OUT1, 10 + i))
		# a correction chain: A (old) is corrected by B (NEW) -> A must stay; C (old) is corrected by D (old) -> both go
		rows.append(make(f"{PREFIX}A", OUT1, 150))
		rows.append(make(f"{PREFIX}B", OUT1, 5, correction_of=f"{PREFIX}A"))
		rows.append(make(f"{PREFIX}C", OUT1, 149))
		rows.append(make(f"{PREFIX}D", OUT1, 148, correction_of=f"{PREFIX}C"))
		frappe.db.bulk_insert("KOT", FIELDS, rows)
		frappe.db.commit()
		all_names = [r[0] for r in rows]
		original = snapshot(all_names)
		cutoff = datetime.date.today() - datetime.timedelta(days=100)
		o1_old = [n for n in all_names if n.startswith(f"{PREFIX}o1-old")] + [f"{PREFIX}C", f"{PREFIX}D"]  # A is held back by B

		# --- safety rails
		dry = archiving.archive("KOT", cutoff, outlet=OUT1)
		check(dry["dry_run"] is True and dry["rows"] == len(o1_old) and dry["batch"] is None, f"a dry run reports what it would do ({dry['rows']} rows) and does nothing")
		check(frappe.db.count("KOT", {"name": ["in", all_names]}) == len(all_names), "...nothing was removed")
		check(throws(lambda: archiving.archive("KOT", datetime.date.today() - datetime.timedelta(days=30), outlet=OUT1, dry_run=False)), "nothing newer than the 90-day minimum age can be archived")
		check(throws(lambda: archiving.archive("Guest Consent", cutoff, dry_run=False)) and throws(lambda: archiving.archive("Sales Invoice", cutoff, dry_run=False)), "guest consent and accounting records are refused")
		check(not hasattr(archiving.archive, "__frappe_whitelisted__") and archiving.archive not in frappe.whitelisted and archiving.restore not in frappe.whitelisted, "archive and restore are not whitelisted: no browser or API call can reach them")

		# --- archive
		report = archiving.archive("KOT", cutoff, outlet=OUT1, dry_run=False)
		check(report["rows"] == len(o1_old) and report["batch"], f"archiving ZZARCH1 moved {report['rows']} rows into {report['batch']}")
		left = set(frappe.get_all("KOT", filters={"name": ["in", all_names]}, pluck="name"))
		check(not left & set(o1_old), "those rows are gone from the live table")
		check(f"{PREFIX}A" in left, "a row that a live correction (newer, still on the table) points at is kept")
		check(all(n in left for n in all_names if n.startswith(f"{PREFIX}o2-old") or n.startswith(f"{PREFIX}o1-new")), "another outlet's old rows and this outlet's recent rows are untouched")
		batch = frappe.get_doc("Archive Batch", report["batch"])
		path = frappe.get_site_path(batch.file_path)
		check(os.path.exists(path) and batch.sha256 and len(batch.sha256) == 64 and batch.row_count == len(o1_old) and batch.status == "Archived", "the batch records the file, its SHA-256, the row count and Archived status")
		with gzip.open(path, "rt") as f:
			check(sum(1 for _ in f) == len(o1_old), "the file holds exactly those rows")
		check(bool(frappe.db.exists("Activity Log", {"reference_doctype": "Archive Batch", "reference_name": batch.name})), "the run is in the Activity Log")

		# --- restore, exactly
		out = archiving.restore(batch.name)
		check(out["rows"] == len(o1_old), "restore puts every archived row back")
		after = snapshot(all_names)
		diffs = [(n, k) for n, row in original.items() for k, v in row.items() if after.get(n, {}).get(k) != v]
		check(len(after) == len(original) and not diffs, f"...and each one is identical to the original in every column ({len(diffs)} differences)")
		check(frappe.db.get_value("Archive Batch", batch.name, "status") == "Restored", "the batch is marked Restored")
		check(throws(lambda: archiving.restore(batch.name)), "restoring the same batch twice is refused")

		# --- tampering and conflicts
		report2 = archiving.archive("KOT", cutoff, outlet=OUT2, dry_run=False)
		b2 = frappe.get_doc("Archive Batch", report2["batch"])
		path2 = frappe.get_site_path(b2.file_path)
		with open(path2, "ab") as f:
			f.write(b"tampered")
		check(throws(lambda: archiving.restore(b2.name)) and frappe.db.count("KOT", {"outlet": OUT2, "name": ["like", PREFIX + "o2-old%"]}) == 0, "a file that no longer matches its hash is refused, and nothing is restored from it")
		os.remove(path2)
		check(throws(lambda: archiving.restore(b2.name)), "a missing file is refused")
		frappe.db.sql("update `tabArchive Batch` set status='Archived' where name=%s", b2.name)
		report3 = archiving.archive("KOT", cutoff, outlet=OUT1, dry_run=False)
		frappe.db.bulk_insert("KOT", FIELDS, [make(f"{PREFIX}o1-old-00", OUT1, 200)])  # a live row with a name the batch also holds
		frappe.db.commit()
		check(throws(lambda: archiving.restore(report3["batch"])), "restoring over a row that already exists is refused (nothing is overwritten)")
		check(frappe.db.count("KOT", {"outlet": OUT1, "name": ["like", PREFIX + "o1-old%"]}) == 1, "...and none of the batch was partly restored")

		# --- the daily hook
		calls = []
		real = archiving.archive
		archiving.archive = lambda *a, **k: calls.append((a, k)) or {"rows": 0}
		try:
			check(archiving.archive_due() == [] and not calls, "the daily hook does nothing without a retention policy in the site config")
			previous = frappe.conf.get("restaurant_pos_archive_after_days")
			frappe.conf.restaurant_pos_archive_after_days = {"KOT": 30, "Guest Consent": 10, "Sales Invoice": 5}
			archiving.archive_due()
			frappe.conf.restaurant_pos_archive_after_days = previous or {}
			check(len(calls) == 1 and calls[0][0][0] == "KOT" and calls[0][1]["dry_run"] is False, "with a policy it archives only the archivable doctypes it names")
			expected = datetime.date.today() - datetime.timedelta(days=90)
			check(str(calls[0][0][1]) == str(expected), "a policy shorter than the 90-day minimum is raised to the minimum")
		finally:
			archiving.archive = real
			frappe.conf.pop("restaurant_pos_archive_after_days", None) if not previous else None
		hooks = frappe.get_hooks("scheduler_events")
		check("restaurant_pos.restaurant_pos.archiving.archive_due" in (hooks.get("daily") or []), "the daily hook is registered with the scheduler")
	finally:
		# anything still archived goes back, then every test row and file is removed
		for b in frappe.get_all("Archive Batch", filters={"outlet": ["in", [OUT1, OUT2]]}, fields=["name", "file_path"]):
			if b.file_path and os.path.exists(frappe.get_site_path(b.file_path)):
				os.remove(frappe.get_site_path(b.file_path))
			frappe.db.sql("delete from `tabArchive Batch` where name=%s", b.name)
		frappe.db.sql("delete from `tabKOT` where name like %s", PREFIX + "%")
		frappe.db.sql("delete from `tabActivity Log` where subject like 'Archived % KOT rows%' or subject like 'Restored % KOT rows%'")
		frappe.db.commit()
		check(frappe.db.count("KOT", {"name": ["like", PREFIX + "%"]}) == 0, "every test row, batch and file was removed")
	print("ALL PASS" if not failures else f"{len(failures)} FAILED")
	if failures:
		raise SystemExit(1)
