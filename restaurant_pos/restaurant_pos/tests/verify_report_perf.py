# Ticket P3-35: "Reports over a year of simulated data for 20 outlets return within target." Simulates a year of kitchen tickets for
# 20 outlets (292,000 KOT rows: 20 outlets x 365 days x 40 tickets) with direct bulk inserts, then times the report-shaped
# queries WITH the composite index (outlet, date) and WITHOUT it, to show the index is what keeps them fast.
#
# THE TARGET is not defined in the ticket; this test uses 500 ms per query on this machine's database, a deliberately plain
# number to be confirmed against what a report user will accept (flagged in the ticket's status note). The indexed run must
# meet it AND be several times faster than the unindexed one.
#
# Every generated row is removed at the end (rows are named zzperf-*, outlets ZZPERF01..20); the live indexes are put back.
#   bench --site dev.localhost execute restaurant_pos.restaurant_pos.tests.verify_report_perf.run

import datetime
import time

import frappe

from restaurant_pos.restaurant_pos.setup.report_indexes import ensure_report_indexes

failures = []
OUTLETS = [f"ZZPERF{i:02d}" for i in range(1, 21)]
DAYS, PER_DAY = 365, 40
TARGET_MS = 500
INDEX = "rp_outlet_fired"


def check(cond, label):
	print(("PASS  " if cond else "FAIL  ") + label)
	if not cond:
		failures.append(label)


def timed(fn, repeat=3):
	best = None
	for _ in range(repeat):
		t = time.perf_counter()
		fn()
		best = min(best, time.perf_counter() - t) if best is not None else time.perf_counter() - t
	return round(best * 1000, 1)


def generate():
	today = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
	fields = ["name", "creation", "modified", "modified_by", "owner", "docstatus", "idx", "outlet", "restaurant_order", "line_item_id", "item_code", "item_name", "station", "qty", "course", "fired_at", "served_at"]
	batch, total = [], 0
	for o, outlet in enumerate(OUTLETS):
		for d in range(DAYS):
			day = today - datetime.timedelta(days=d + 1)
			for n in range(PER_DAY):
				fired = day + datetime.timedelta(hours=11 + (n * 12) // PER_DAY, minutes=n % 60)
				batch.append([f"zzperf-{o:02d}-{d:03d}-{n:03d}", fired, fired, "Administrator", "Administrator", 1, 0, outlet, f"zzperf-ord-{o:02d}-{d:03d}-{n // 4:02d}", f"l{n}", "ZZ-ITEM", "Item", "ZZPERF-S", 1, "main", fired, fired + datetime.timedelta(minutes=12)])
				if len(batch) >= 10000:
					frappe.db.bulk_insert("KOT", fields, batch)
					total += len(batch)
					batch = []
	if batch:
		frappe.db.bulk_insert("KOT", fields, batch)
		total += len(batch)
	frappe.db.commit()
	return total


def run():
	frappe.set_user("Administrator")
	ensure_report_indexes()
	try:
		frappe.db.sql("delete from `tabKOT` where name like 'zzperf-%%'")
		frappe.db.commit()
		t = time.perf_counter()
		total = generate()
		frappe.db.sql("analyze table `tabKOT`")
		print(f"      generated {total:,} KOT rows for {len(OUTLETS)} outlets x {DAYS} days in {time.perf_counter() - t:.1f} s")
		check(total == len(OUTLETS) * DAYS * PER_DAY, f"a year of data for 20 outlets is in place ({total:,} tickets)")

		outlet = OUTLETS[7]
		end = datetime.datetime.now() - datetime.timedelta(days=30)
		start = end - datetime.timedelta(days=30)
		one_day = (datetime.datetime.now() - datetime.timedelta(days=100)).date()

		queries = {
			"one outlet, 30 days, newest 500 tickets": lambda: frappe.get_all("KOT", filters={"outlet": outlet, "fired_at": ["between", [start, end]]}, fields=["name", "item_code", "station", "fired_at", "served_at"], order_by="fired_at desc", limit=500),
			"one outlet, tickets per day over 30 days": lambda: frappe.db.sql("select date(fired_at), count(*) from `tabKOT` where outlet=%s and fired_at >= %s and fired_at < %s group by date(fired_at)", (outlet, start, end)),
			"every outlet, one day, tickets per outlet": lambda: frappe.db.sql("select outlet, count(*) from `tabKOT` where fired_at >= %s and fired_at < %s + interval 1 day group by outlet", (one_day, one_day)),
			"one outlet, one month, average serve time": lambda: frappe.db.sql("select avg(timestampdiff(second, fired_at, served_at)) from `tabKOT` where outlet=%s and fired_at >= %s and fired_at < %s", (outlet, start, end)),
		}
		with_index = {label: timed(fn) for label, fn in queries.items()}
		order_lookup = timed(lambda: frappe.get_all("KOT", filters={"restaurant_order": "zzperf-ord-07-050-03"}, fields=["name"]))

		# the same queries with the (outlet, date) index removed
		frappe.db.sql(f"alter table `tabKOT` drop index `{INDEX}`")
		frappe.db.sql("alter table `tabKOT` drop index `rp_fired`")
		without_index = {label: timed(fn, repeat=1) for label, fn in queries.items()}
		ensure_report_indexes()

		for label in queries:
			a, b = with_index[label], without_index[label]
			print(f"      {label}: {a} ms with the index, {b} ms without")
			check(a <= TARGET_MS, f"{label}: {a} ms is within the {TARGET_MS} ms target")
		check(order_lookup <= TARGET_MS, f"looking up one order's tickets: {order_lookup} ms (index on restaurant_order)")
		ratios = [without_index[l] / max(with_index[l], 0.1) for l in ("one outlet, 30 days, newest 500 tickets", "one outlet, tickets per day over 30 days", "one outlet, one month, average serve time", "every outlet, one day, tickets per outlet")]
		check(all(r >= 1.5 for r in ratios[3:]) and all(r >= 3 for r in ratios[:3]), f"the indexes make the outlet-scoped reports at least 3x faster than scanning, and the all-outlets day report faster too ({', '.join(f'{r:.0f}x' for r in ratios)})")
		explain = frappe.db.sql("explain select name from `tabKOT` where outlet=%s and fired_at >= %s and fired_at < %s", (outlet, start, end), as_dict=True)
		check(explain and explain[0].get("key") == INDEX, f"the optimiser uses the report index (key = {explain[0].get('key') if explain else None})")
	finally:
		frappe.db.sql("delete from `tabKOT` where name like 'zzperf-%%'")
		frappe.db.commit()
		ensure_report_indexes()
		frappe.db.sql("analyze table `tabKOT`")
		left = frappe.db.sql("select count(*) from `tabKOT` where name like 'zzperf-%%'")[0][0]
		check(left == 0, "every generated row was removed")
	print("ALL PASS" if not failures else f"{len(failures)} FAILED")
	if failures:
		raise SystemExit(1)
