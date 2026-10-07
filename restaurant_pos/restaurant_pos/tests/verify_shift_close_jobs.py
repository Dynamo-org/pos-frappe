# Ticket P3-38: staggered, resumable shift-close jobs.
#   A. THE RUSH: 20 outlets submit their shift close in the same moment. The web side answers each in milliseconds with
#      "pending"; the dispatcher then starts at most 4 a minute (site config restaurant_pos_shift_close_per_minute), oldest
#      first, each not before its own stable offset inside a 60 s window - so all 20 are done in about five ticks, never
#      more than 4 starting together, with a controlled clock standing in for the scheduler.
#   B. RESUMING, on a REAL shift (a throwaway outlet, POS Profile and user, copied from the dev outlet): the stock step is made
#      to fail once after the closing entry is submitted; the job goes back in the queue with a back-off, and the next run does
#      only what is left - exactly ONE POS Closing Entry exists afterwards, and the stock step ran once to completion.
#      A worker that died mid-job (stuck "Running") is put back; a job that keeps failing ends Failed with the reason and is
#      reported to the Hub as an error; resubmitting the same close finds the same job; an older Hub (no accept_pending) still
#      gets the synchronous answer, from the same code.
#   bench --site dev.localhost execute restaurant_pos.restaurant_pos.tests.verify_shift_close_jobs.run

import datetime
import json
import time
import uuid
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import add_to_date, get_system_timezone, now_datetime

from restaurant_pos.restaurant_pos.api import shift_close
from restaurant_pos.restaurant_pos.api.v1 import sync

failures = []
RUSH = [f"ZZRUSH{i:02d}" for i in range(20)]
PER_MINUTE = shift_close.DEFAULT_PER_MINUTE


def check(cond, label):
	print(("PASS  " if cond else "FAIL  ") + label)
	if not cond:
		failures.append(label)


def utc_iso(local_dt):
	return local_dt.replace(tzinfo=ZoneInfo(get_system_timezone())).astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def payload(outlet, hub_uuid=None, **extra):
	return {"hub_uuid": hub_uuid or f"zzsc-{uuid.uuid4()}", "outlet": outlet, "pos_opening_entry": "X", "counted_cash_paise": 0, "closed_by_staff_id": "S", "closed_at": utc_iso(datetime.datetime.now()), "business_date": str(datetime.date.today()), "sold_items": [], "accept_pending": True, **extra}


def run():
	frappe.set_user("Administrator")
	frappe.flags.shift_close_inline = True
	real_close, real_stock = sync._close_shift, sync._create_shift_stock_entry
	state = {"opening": [], "closing": []}
	try:
		cleanup_jobs()
		# ------------------------------------------------------------------------------------------- A. the rush
		started = []

		def fake_close(p):
			started.append((p["outlet"], tick_now[0]))
			return {"status": "ok", "name": f"CLOSE-{p['outlet']}", "already_existed": False, "stock_entry_name": None, "missing_bom_items": []}

		sync._close_shift = fake_close
		tick_now = [now_datetime()]
		# the rush: nothing is started inside the requests (dispatch is held back here so the ticks below are the only starter)
		real_dispatch = shift_close.dispatch_due
		shift_close.dispatch_due = lambda *a, **k: []
		t0 = time.perf_counter()
		answers = [shift_close.submit_close(payload(o)) for o in RUSH]
		shift_close.dispatch_due = real_dispatch
		web_ms = (time.perf_counter() - t0) * 1000 / len(RUSH)
		check(all(a["status"] == "pending" and a["retry_after_seconds"] for a in answers), "every submitted close is answered 'pending' with a retry hint")
		check(web_ms < 250, f"the web side spends {web_ms:.0f} ms per close (it records the job; it does not do the closing)")
		offsets = {shift_close.stagger_offset_seconds(o) for o in RUSH}
		check(len(offsets) >= 12 and all(0 <= x < 60 for x in offsets), f"the 20 outlets are spread over the 60 s window ({len(offsets)} distinct offsets)")
		check(shift_close.stagger_offset_seconds("ZZRUSH03") == shift_close.stagger_offset_seconds("ZZRUSH03"), "an outlet always gets the same offset")
		check(frappe.db.count("Shift Close Job", {"outlet": ["like", "ZZRUSH%"], "status": "Queued"}) == 20, "all 20 are Queued: nothing ran inside the request")

		base = now_datetime()
		per_tick = []
		for minute in range(12):
			tick_now[0] = add_to_date(base, minutes=minute, seconds=1)
			per_tick.append(len(shift_close.dispatch_due(now=tick_now[0], inline=True)))
		check(max(per_tick) <= PER_MINUTE, f"at most {PER_MINUTE} closes start in any minute (per tick: {per_tick})")
		check(sum(per_tick) == 20 and frappe.db.count("Shift Close Job", {"outlet": ["like", "ZZRUSH%"], "status": "Done"}) == 20, "all 20 complete")
		finished_by = max(i for i, n in enumerate(per_tick) if n) + 1
		check(finished_by <= 7, f"...within {finished_by} minutes of the rush (target: inside the same 10)")
		check(len({o for o, _ in started}) == 20, "each outlet's close ran exactly once")
		results = [shift_close.answer_for(frappe.db.get_value("Shift Close Job", {"outlet": o}, "name")) for o in RUSH]
		check(all(r["status"] == "ok" and r["name"].startswith("CLOSE-") for r in results), "the Hub's next push gets each finished result (the same dict the synchronous path returns)")
		check(shift_close.dispatch_due(now=add_to_date(base, minutes=30), inline=True) == [], "once everything is done the dispatcher has nothing to do")

		cleanup_jobs()  # (the rush's simulated start times are in the future; a clean slate for the real clock)
		# light load: the close that has just arrived starts at once, whatever its stagger offset
		light = payload("ZZRUSH09")
		first_answer = shift_close.submit_close(light)
		check(first_answer["status"] == "ok" and started[-1][0] == "ZZRUSH09", "under light load a close starts the moment it is submitted (the stagger orders waiting jobs; it never delays a free slot)")
		# ...but once the minute's capacity is used, the next one waits for its turn
		burst = [shift_close.submit_close(payload(f"ZZRUSH1{i}")) for i in range(PER_MINUTE + 1)]
		check([b["status"] for b in burst].count("pending") >= 1 and [b["status"] for b in burst].count("ok") <= PER_MINUTE - 1, f"with the minute's capacity used (the light close above took one slot), further closes are 'pending' ({[b['status'] for b in burst]})")
		cleanup_jobs()
		sync._close_shift = fake_close
		tick_now[0] = now_datetime()
		shift_close.submit_close(payload("ZZRUSH00"))
		again = None
		# resubmitting the same close finds the same job
		again = shift_close.submit_close(payload("ZZRUSH00", hub_uuid=frappe.db.get_value("Shift Close Job", {"outlet": "ZZRUSH00"}, "hub_uuid")))
		check(again["status"] == "ok" and frappe.db.count("Shift Close Job", {"outlet": "ZZRUSH00"}) == 1, "resubmitting a close returns its finished result and makes no second job")
		cleanup_jobs()

		# a worker that died: stuck Running goes back in the queue
		sync._close_shift = fake_close
		p = payload("ZZRUSH05")
		shift_close.submit_close(p)
		job = frappe.db.get_value("Shift Close Job", {"hub_uuid": p["hub_uuid"]}, "name")
		frappe.db.set_value("Shift Close Job", job, {"status": "Running", "started_at": add_to_date(now_datetime(), minutes=-20), "attempts": 1}, update_modified=False)
		frappe.db.commit()
		shift_close.dispatch_due(now=add_to_date(now_datetime(), minutes=2), inline=True)
		check(frappe.db.get_value("Shift Close Job", job, "status") == "Done", "a job stuck Running for 20 minutes (its worker died) is put back and finished")
		cleanup_jobs()

		# a job that keeps failing ends Failed with the reason
		def always_fail(_):
			raise frappe.ValidationError("ERPNext said no")

		sync._close_shift = always_fail
		p = payload("ZZRUSH06")
		shift_close.submit_close(p)
		job = frappe.db.get_value("Shift Close Job", {"hub_uuid": p["hub_uuid"]}, "name")
		clock = now_datetime()
		for attempt in range(shift_close.MAX_ATTEMPTS + 2):
			clock = add_to_date(clock, minutes=20)
			shift_close.dispatch_due(now=clock, inline=True)
		row = frappe.db.get_value("Shift Close Job", job, ["status", "attempts", "last_error"], as_dict=True)
		check(row.status == "Failed" and row.attempts == shift_close.MAX_ATTEMPTS and "ERPNext said no" in row.last_error, f"a close that keeps failing is tried {row.attempts} times, then Failed with the reason")
		answer = shift_close.answer_for(job)
		check(answer["status"] == "error" and "ERPNext said no" in answer["message"], "...and the Hub is told it is an error (so it dead-letters it like any other failing record)")
		cleanup_jobs()
		sync._close_shift = real_close

		# ------------------------------------------------------------------------------------------- B. a real shift
		staff = frappe.get_all("Staff PIN", pluck="name", limit=1)[0]
		pos_user = "zzsc-pos@example.test"
		if frappe.db.exists("User", pos_user):
			frappe.delete_doc("User", pos_user, force=1, ignore_permissions=True)
		frappe.get_doc({"doctype": "User", "email": pos_user, "first_name": "ZZSC", "send_welcome_email": 0, "roles": [{"role": "System Manager"}, {"role": "Accounts User"}, {"role": "Sales User"}, {"role": "Stock User"}, {"role": "Hub Service"}]}).insert(ignore_permissions=True)
		state["user"] = pos_user
		frappe.db.commit()
		frappe.set_user(pos_user)
		kor = frappe.get_doc("Outlet", "KOR")
		profile = frappe.copy_doc(frappe.get_doc("POS Profile", kor.pos_profile))
		profile.name = "ZZ ShiftClose Profile"
		profile.set("applicable_for_users", [])
		profile.insert(ignore_permissions=True)
		state["profile"] = profile.name
		outlet = frappe.copy_doc(kor)
		outlet.short_code, outlet.outlet_name, outlet.pos_profile = "ZZSC", "ZZ Shift Close Test", profile.name
		outlet.insert(ignore_permissions=True)
		frappe.db.commit()

		opened_local = datetime.datetime.now() - datetime.timedelta(hours=3)
		opened = sync.push_shift_open(json.dumps({"hub_uuid": f"zzsc-{uuid.uuid4()}", "outlet": "ZZSC", "opening_cash_paise": 0, "opened_by_staff_id": staff, "opened_at": utc_iso(opened_local), "business_date": str(datetime.date.today()), "pos_user": pos_user}))
		state["opening"].append(opened["name"])
		close_uuid = f"{opened['name']}-zzsc-close"
		close = payload("ZZSC", hub_uuid=close_uuid, pos_opening_entry=opened["name"], counted_cash_paise=0, closed_by_staff_id=staff, sold_items=[{"item_code": "ZZ-ITEM", "name": "x", "qty": 1}])

		stock_calls = []

		def stock_fails_once(p):
			stock_calls.append("called")
			if len(stock_calls) == 1:
				raise RuntimeError("stock posting blew up (simulated)")
			return "SE-ZZ-FAKE", []

		sync._create_shift_stock_entry = stock_fails_once
		frappe.set_user("Administrator")
		first = sync.push_shift_close(json.dumps(close))
		check(first["status"] == "pending", "a Hub that sends accept_pending is answered 'pending' for a real shift close")
		jobname = frappe.db.get_value("Shift Close Job", {"hub_uuid": close_uuid}, "name")
		# (light load: the first attempt already ran, inside the submit)
		job = frappe.db.get_value("Shift Close Job", jobname, ["status", "attempts", "last_error", "run_after"], as_dict=True)
		check(job.status == "Queued" and job.attempts == 1 and "blew up" in job.last_error and job.run_after > now_datetime(), "the stock step failed: the job is Queued again with a back-off, the reason kept")
		closings = frappe.get_all("POS Closing Entry", filters={"hub_uuid": close_uuid}, fields=["name", "docstatus"])
		state["closing"] += [c.name for c in closings]
		check(len(closings) == 1 and closings[0].docstatus == 1, "the closing entry (step 1) is already submitted and committed: that work is not lost")
		check(frappe.db.get_value("Shift Close Job", jobname, "status") != "Done", "...and the Hub is still told 'pending', not success")

		shift_close.dispatch_due(now=add_to_date(now_datetime(), minutes=10), inline=True)
		job = frappe.db.get_value("Shift Close Job", jobname, ["status", "attempts", "result_json"], as_dict=True)
		result = json.loads(job.result_json or "{}")
		check(job.status == "Done" and job.attempts == 2, "the next run completes the job (attempt 2)")
		check(frappe.db.count("POS Closing Entry", {"hub_uuid": close_uuid}) == 1, "still exactly ONE POS Closing Entry: nothing was duplicated")
		check(len(stock_calls) == 2 and result.get("stock_entry_name") == "SE-ZZ-FAKE" and result["already_existed"] is True, "the resumed run did only what was left (it found the closing entry, finished the stock step)")
		final = sync.push_shift_close(json.dumps(close))
		check(final["status"] == "ok" and final["name"] == closings[0].name and final["stock_entry_name"] == "SE-ZZ-FAKE", "the Hub's next push gets the real result: closing entry and stock entry")

		# an older Hub: synchronous, from the same code, idempotent
		old = dict(close)
		old.pop("accept_pending")
		sync._create_shift_stock_entry = lambda p: ("SE-ZZ-FAKE", [])
		legacy = sync.push_shift_close(json.dumps(old))
		check(legacy["status"] == "ok" and legacy["name"] == closings[0].name and legacy["already_existed"] is True, "a Hub that does not send accept_pending still gets the synchronous answer")
		check(frappe.db.count("POS Closing Entry", {"hub_uuid": close_uuid}) == 1, "...without a second closing entry")

		# the bug this ticket exposed: a retry after a half-finished synchronous close used to return early, never finishing the stock step
		calls = []
		sync._create_shift_stock_entry = lambda p: (calls.append(1), ("SE-ZZ-LATE", []))[1]
		again = sync._close_shift(old)
		check(again["stock_entry_name"] == "SE-ZZ-LATE" and calls, "a retry after a closing entry exists now runs the stock step (it used to return early and never post stock)")
	finally:
		sync._close_shift, sync._create_shift_stock_entry = real_close, real_stock
		frappe.flags.shift_close_inline = False
		frappe.set_user("Administrator")
		cleanup_jobs()
		cleanup_real(state)
	print("ALL PASS" if not failures else f"{len(failures)} FAILED")
	if failures:
		raise SystemExit(1)


def cleanup_jobs():
	frappe.db.sql("delete from `tabShift Close Job` where outlet like 'ZZRUSH%%' or outlet = 'ZZSC' or hub_uuid like 'zzsc-%%'")
	frappe.db.commit()


def cleanup_real(state):
	frappe.flags.allow_audit_purge = True
	for doctype, names in (("POS Closing Entry", state.get("closing", [])), ("POS Opening Entry", state.get("opening", []))):
		for name in names:
			try:
				if not frappe.db.exists(doctype, name):
					continue
				doc = frappe.get_doc(doctype, name)
				if doc.docstatus == 1:
					doc.cancel()
				frappe.delete_doc(doctype, name, force=1, ignore_permissions=True)
			except Exception as err:  # noqa: BLE001
				frappe.db.rollback()
				print(f"   cleanup: could not remove {doctype} {name}: {str(err)[:120]}")
	for doctype, name in (("Outlet", "ZZSC"), ("POS Profile", state.get("profile"))):
		try:
			if name and frappe.db.exists(doctype, name):
				frappe.delete_doc(doctype, name, force=1, ignore_permissions=True)
		except Exception as err:  # noqa: BLE001
			frappe.db.rollback()
			print(f"   cleanup: could not remove {doctype} {name}: {str(err)[:120]}")
	frappe.flags.allow_audit_purge = False
	if state.get("user") and frappe.db.exists("User", state["user"]):
		try:
			frappe.delete_doc("User", state["user"], force=1, ignore_permissions=True)
		except Exception as err:  # noqa: BLE001
			frappe.db.rollback()
			print(f"   cleanup: could not remove user: {str(err)[:120]}")
	frappe.db.commit()
