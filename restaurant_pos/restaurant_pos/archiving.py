# Copyright (c) 2026, the project contributors
# For license information, please see license.txt

# Ticket P3-35 (Tech Architecture §7): an archiving hook, configurable to the data-archiving policy (decision P0-11, still
# pending) and REVERSIBLE. Old rows of the operational log doctypes are written to a compressed, hash-checked file and removed
# from the live tables; the file can put them back exactly.
#
#   * SERVER-SIDE ONLY. Nothing here is whitelisted: it is run from a bench console or the daily scheduler, never from a
#     browser or an API call. Removing rows from these doctypes deliberately bypasses the audit guards (guards/immutability.py),
#     which is the documented server-side purge, so every batch is recorded in an Archive Batch (who, when, what, a SHA-256
#     of the file) and in the Activity Log.
#   * OFF BY DEFAULT. Nothing is archived unless site config `restaurant_pos_archive_after_days` names a doctype and an age:
#         {"KOT": 400, "Availability Log": 400}
#     The numbers are the client's retention policy, not a decision made here (P0-11), so they are configuration only.
#   * SAFE BY DEFAULT. A call is a dry run unless told otherwise; nothing newer than MIN_AGE_DAYS (site config
#     `restaurant_pos_archive_min_days`, default 90) is ever archived, whatever the policy says; a row that a newer, still-live
#     correction row points at is kept (a dangling correction_of would corrupt the audit trail).
#   * NOT archivable: Guest Consent (it is the evidence of consent - it follows the consent retention rules, not this) and
#     anything accounting (Sales Invoice, POS entries): those are ERPNext's own records and are never touched here.

import gzip
import hashlib
import json
import os

import frappe
from frappe.utils import add_days, get_datetime, getdate, now_datetime, nowdate

# archivable doctype -> the column that dates a row
ARCHIVABLE = {
	"KOT": "fired_at",
	"Void Log": "occurred_at",
	"Refund Log": "occurred_at",
	"Availability Log": "occurred_at",
}
DEFAULT_MIN_AGE_DAYS = 90
CHUNK = 2000


def _min_age_days() -> int:
	return int(frappe.conf.get("restaurant_pos_archive_min_days") or DEFAULT_MIN_AGE_DAYS)


def _archive_dir(doctype: str) -> str:
	path = frappe.get_site_path("private", "restaurant_pos_archive", frappe.scrub(doctype))
	os.makedirs(path, exist_ok=True)
	return path


def _sha256(path: str) -> str:
	digest = hashlib.sha256()
	with open(path, "rb") as f:
		for block in iter(lambda: f.read(1 << 20), b""):
			digest.update(block)
	return digest.hexdigest()


def _select(doctype: str, before, outlet: str | None) -> list[dict]:
	"""The rows eligible for this batch: older than `before`, optionally one outlet, and not pointed at by a live correction."""
	date_field = ARCHIVABLE[doctype]
	filters = {date_field: ("<", get_datetime(before))}
	if outlet:
		filters["outlet"] = outlet
	rows = frappe.get_all(doctype, filters=filters, fields=["*"], order_by=f"{date_field} asc, name asc")
	in_batch = {r["name"] for r in rows}
	if not rows:
		return rows
	# a row that a live row still points at (correction_of) stays - unless the pointing row is archived in the same batch
	pointed_at = set()
	names = list(in_batch)
	for i in range(0, len(names), 1000):
		for pointer in frappe.get_all(doctype, filters={"correction_of": ("in", names[i : i + 1000])}, fields=["name", "correction_of"]):
			if pointer["name"] not in in_batch:
				pointed_at.add(pointer["correction_of"])
	return [r for r in rows if r["name"] not in pointed_at]


def archive(doctype: str, before, outlet: str | None = None, dry_run: bool = True) -> dict:
	"""Moves rows older than `before` (a date) out of the live table into a file. Dry run unless dry_run=False."""
	if doctype not in ARCHIVABLE:
		frappe.throw(f"{doctype} cannot be archived here (archivable: {', '.join(ARCHIVABLE)})")
	cutoff = getdate(before)
	newest_allowed = getdate(add_days(nowdate(), -_min_age_days()))
	if cutoff > newest_allowed:
		frappe.throw(f"Refusing to archive anything newer than {_min_age_days()} days (cutoff {cutoff} is after {newest_allowed}).")
	rows = _select(doctype, cutoff, outlet)
	report = {"doctype": doctype, "cutoff": str(cutoff), "outlet": outlet, "rows": len(rows), "dry_run": dry_run, "batch": None}
	if dry_run or not rows:
		return report

	date_field = ARCHIVABLE[doctype]
	stamp = now_datetime().strftime("%Y%m%d-%H%M%S")
	path = os.path.join(_archive_dir(doctype), f"{stamp}-{outlet or 'all'}.jsonl.gz")
	with gzip.open(path, "wt", encoding="utf-8") as f:
		for row in rows:
			f.write(json.dumps(row, default=str, sort_keys=True) + "\n")
	digest = _sha256(path)
	# the file is verified BEFORE a single row is removed
	with gzip.open(path, "rt", encoding="utf-8") as f:
		if sum(1 for _ in f) != len(rows):
			os.remove(path)
			frappe.throw("The archive file did not read back with the same number of rows; nothing was removed.")

	batch = frappe.get_doc(
		{
			"doctype": "Archive Batch",
			"archived_doctype": doctype,
			"outlet": outlet,
			"cutoff": cutoff,
			"from_timestamp": str(rows[0][date_field]),
			"to_timestamp": str(rows[-1][date_field]),
			"row_count": len(rows),
			"file_path": os.path.relpath(path, frappe.get_site_path()),
			"sha256": digest,
			"status": "Archived",
			"archived_at": now_datetime(),
			"archived_by": frappe.session.user,
		}
	).insert(ignore_permissions=True)

	names = [r["name"] for r in rows]
	for i in range(0, len(names), CHUNK):
		frappe.db.delete(doctype, {"name": ("in", names[i : i + CHUNK])})
	frappe.get_doc(
		{"doctype": "Activity Log", "subject": f"Archived {len(rows)} {doctype} rows older than {cutoff} to {batch.name}", "reference_doctype": "Archive Batch", "reference_name": batch.name, "status": "Success"}
	).insert(ignore_permissions=True)
	frappe.db.commit()
	report["batch"] = batch.name
	return report


def restore(batch_name: str) -> dict:
	"""Puts a batch back exactly as it was (same names, timestamps, owners, document status). Refuses a file whose hash no longer
	matches, a batch already restored, and a row that already exists in the live table (nothing is overwritten)."""
	batch = frappe.get_doc("Archive Batch", batch_name)
	if batch.status != "Archived":
		frappe.throw(f"{batch_name} is {batch.status}, not Archived.")
	path = frappe.get_site_path(batch.file_path)
	if not os.path.exists(path):
		frappe.throw(f"The archive file {batch.file_path} is missing.")
	if _sha256(path) != batch.sha256:
		frappe.throw(f"The archive file {batch.file_path} no longer matches its recorded hash; refusing to restore from it.")
	with gzip.open(path, "rt", encoding="utf-8") as f:
		rows = [json.loads(line) for line in f if line.strip()]
	doctype = batch.archived_doctype
	existing = set()
	for i in range(0, len(rows), CHUNK):
		existing |= set(frappe.get_all(doctype, filters={"name": ("in", [r["name"] for r in rows[i : i + CHUNK]])}, pluck="name"))
	if existing:
		frappe.throw(f"{len(existing)} of these rows already exist in {doctype} (e.g. {sorted(existing)[0]}); nothing was restored.")
	fields = sorted(rows[0].keys())
	for i in range(0, len(rows), CHUNK):
		frappe.db.bulk_insert(doctype, fields, [[r.get(f) for f in fields] for r in rows[i : i + CHUNK]])
	batch.db_set({"status": "Restored", "restored_at": now_datetime(), "restored_by": frappe.session.user})
	frappe.get_doc(
		{"doctype": "Activity Log", "subject": f"Restored {len(rows)} {doctype} rows from {batch.name}", "reference_doctype": "Archive Batch", "reference_name": batch.name, "status": "Success"}
	).insert(ignore_permissions=True)
	frappe.db.commit()
	return {"batch": batch_name, "doctype": doctype, "rows": len(rows)}


def archive_due() -> list[dict]:
	"""The daily scheduler hook. Does NOTHING unless the site's retention policy has been configured (P0-11 is pending)."""
	policy = frappe.conf.get("restaurant_pos_archive_after_days") or {}
	done = []
	for doctype, days in policy.items():
		if doctype not in ARCHIVABLE:
			continue
		days = max(int(days), _min_age_days())
		done.append(archive(doctype, add_days(nowdate(), -days), dry_run=False))
	return done
