# Copyright (c) 2026, the project contributors
# For license information, please see license.txt

# Ticket P3-38 (Tech Architecture §5, load safety rule 7): "ERP shift-close rush. POS Closing and per-shift stock posting run as
# resumable jobs on Frappe's long queue, staggered per outlet, so an 11 pm rush across a chain does not exhaust Frappe background
# workers."
#
# How it works, for a Hub that opts in (it sends `accept_pending` with the shift-close payload):
#   1. push_shift_close records a Shift Close Job (idempotent by the close's hub_uuid) and answers "pending" at once, with a
#      suggested wait: the web worker is held for milliseconds, not for the closing entry's consolidation and stock posting.
#   2. dispatch_due() - called right after the job is recorded and then every minute by the scheduler - starts at most
#      `restaurant_pos_shift_close_per_minute` jobs per minute (default 4) across the whole site, oldest first, each not before
#      its own `run_after` (a stable offset per outlet inside a stagger window, default 60 s). 20 outlets closing in the same
#      minute therefore finish in about five minutes, with at most four closes running at once - not twenty.
#   3. run_job() does the real work through sync._close_shift, whose two steps (the POS Closing Entry, then the stock entry) are
#      each idempotent by their own hub_uuid, so a job that dies half way - a worker restart, a timeout - is simply run again and
#      does only what is left: no second closing entry, no second stock entry. A job stuck "Running" past a timeout is put back.
#   4. The Hub's next push returns the finished result (the same dict the synchronous path always returned) and the Hub marks the
#      shift synced.
# A Hub that does not send accept_pending (an older one: N-1) gets the old synchronous behaviour, through the same _close_shift.

import json
import zlib

import frappe
from frappe.utils import add_to_date, cint, get_datetime, now_datetime

STAGGER_WINDOW_SECONDS = 60
DEFAULT_PER_MINUTE = 4
MAX_ATTEMPTS = 5
RUNNING_TIMEOUT_MINUTES = 15
PENDING_RETRY_AFTER_SECONDS = 30
DOCTYPE = "Shift Close Job"


def _per_minute() -> int:
	return max(1, cint(frappe.conf.get("restaurant_pos_shift_close_per_minute") or DEFAULT_PER_MINUTE))


def stagger_offset_seconds(outlet: str) -> int:
	"""A stable offset for an outlet inside the stagger window (the same outlet always lands at the same point), so a chain
	whose outlets all close at 23:00 does not present 20 jobs in the same second."""
	return zlib.crc32(outlet.encode()) % STAGGER_WINDOW_SECONDS


def submit_close(payload: dict) -> dict:
	"""The async half of push_shift_close. Returns the Hub-facing answer: the final result, or a 'pending' with a retry hint."""
	hub_uuid = payload["hub_uuid"]
	name = frappe.db.get_value(DOCTYPE, {"hub_uuid": hub_uuid}, "name")
	if not name:
		job = frappe.get_doc(
			{
				"doctype": DOCTYPE,
				"hub_uuid": hub_uuid,
				"outlet": payload["outlet"],
				"payload_json": json.dumps(payload, sort_keys=True),
				"status": "Queued",
				"attempts": 0,
				"run_after": add_to_date(now_datetime(), seconds=stagger_offset_seconds(payload["outlet"])),
			}
		)
		try:
			job.insert(ignore_permissions=True)
		except frappe.DuplicateEntryError:  # two retries racing: the other one won
			frappe.db.rollback()
		frappe.db.commit()
		name = frappe.db.get_value(DOCTYPE, {"hub_uuid": hub_uuid}, "name")
		# light load: THIS close starts right away (capacity is free); heavy load: it waits its turn, in run_after order
		dispatch_due(inline=bool(frappe.flags.get("shift_close_inline")), start_first=name)
	return answer_for(name)


def answer_for(name: str) -> dict:
	job = frappe.db.get_value(DOCTYPE, name, ["status", "result_json", "last_error", "attempts"], as_dict=True)
	if job.status == "Done":
		return json.loads(job.result_json)
	if job.status == "Failed":
		return {"status": "error", "message": f"Shift close failed after {job.attempts} attempts: {job.last_error or 'unknown error'}"}
	return {"status": "pending", "job": name, "retry_after_seconds": PENDING_RETRY_AFTER_SECONDS}


def dispatch_due(now=None, inline: bool = False, start_first: str | None = None) -> list[str]:
	"""Starts the jobs that are due and fit under the per-minute cap, oldest `run_after` first. `start_first` names one job that
	may start even before its own run_after (the close that has just arrived), if there is capacity - the stagger only decides
	the order of jobs that have to WAIT. `now` and `inline` exist for tests (a controlled clock, and running the job in this
	process instead of the long queue)."""
	now = get_datetime(now) if now else now_datetime()
	# a job whose worker died: back in the queue (its steps are idempotent, so running it again is safe)
	for name in frappe.get_all(DOCTYPE, filters={"status": "Running", "started_at": ["<", add_to_date(now, minutes=-RUNNING_TIMEOUT_MINUTES)]}, pluck="name"):
		frappe.db.set_value(DOCTYPE, name, {"status": "Queued", "last_error": "worker did not finish: put back in the queue"}, update_modified=False)
	started_recently = frappe.db.count(DOCTYPE, {"started_at": [">", add_to_date(now, seconds=-60)]})
	free = _per_minute() - started_recently
	if free <= 0:
		frappe.db.commit()
		return []
	due = frappe.get_all(DOCTYPE, filters={"status": "Queued", "run_after": ["<=", now]}, order_by="run_after asc, creation asc", pluck="name", limit=free)
	if start_first and start_first not in due and frappe.db.get_value(DOCTYPE, start_first, "status") == "Queued":
		due = [start_first, *due][:free]
	for name in due:
		frappe.db.set_value(DOCTYPE, name, {"status": "Running", "started_at": now, "attempts": cint(frappe.db.get_value(DOCTYPE, name, "attempts")) + 1}, update_modified=False)
	frappe.db.commit()
	for name in due:
		if inline:
			run_job(name)
		else:
			frappe.enqueue("restaurant_pos.restaurant_pos.api.shift_close.run_job", queue="long", timeout=600, job_id=f"shift-close:{frappe.local.site}:{name}", deduplicate=True, name=name)
	return due


def run_job(name: str) -> None:
	"""The real work of one shift close. Safe to run again after any failure: see the module comment."""
	from restaurant_pos.restaurant_pos.api.v1.sync import _close_shift

	previous = frappe.session.user
	frappe.set_user("Administrator")
	try:
		payload = json.loads(frappe.db.get_value(DOCTYPE, name, "payload_json"))
		try:
			result = _close_shift(payload)
			frappe.db.set_value(DOCTYPE, name, {"status": "Done", "finished_at": now_datetime(), "result_json": json.dumps(result), "last_error": None}, update_modified=False)
		except Exception as err:  # noqa: BLE001
			frappe.db.rollback()
			attempts = cint(frappe.db.get_value(DOCTYPE, name, "attempts"))
			frappe.log_error(title=f"shift close job {name} failed (attempt {attempts})", message=frappe.get_traceback())
			give_up = attempts >= MAX_ATTEMPTS
			frappe.db.set_value(
				DOCTYPE,
				name,
				{
					"status": "Failed" if give_up else "Queued",
					"last_error": (str(err) or type(err).__name__)[:500],
					# back off: 1, 2, 4, 8 minutes
					"run_after": add_to_date(now_datetime(), minutes=2 ** (attempts - 1)),
				},
				update_modified=False,
			)
		frappe.db.commit()
	finally:
		frappe.set_user(previous)
