# Copyright (c) 2026, the project contributors
# For license information, please see license.txt

# Ticket P8.5-01: serves the built `apps/back-office` SPA at /back-office, same origin and
# session as Desk (Security & Access §5) — an anonymous visit is bounced to Frappe's own login
# with a redirect back here; a signed-in visit gets the shell plus a CSRF token the React app
# reads from the <meta> tag on every write (Tech Architecture §12.1).
#
# The built JS/CSS use fixed (non-hashed) filenames (vite.config.ts) so www/back_office.html can
# reference them directly — but Frappe serves /assets/ with a 12-hour Cache-Control, so a fixed
# URL alone would mean a stale bundle survives in every visitor's browser for up to 12 hours
# after any rebuild (caught live: a real browser served a bundle from a build two revisions
# earlier, 0 bytes transferred, confirmed via performance.getEntriesByType('resource')). Fixed
# by cache-busting the script/link URLs with the built file's own mtime — real and automatic on
# every rebuild, no version number to remember to bump by hand.

import os

import frappe

no_cache = 1


def get_context(context):
	if frappe.session.user == "Guest":
		frappe.local.flags.redirect_location = "/login?redirect-to=/back-office"
		raise frappe.Redirect

	context.csrf_token = frappe.sessions.get_csrf_token()
	context.asset_version = _asset_version()
	return context


def _asset_version() -> int:
	# frappe.get_app_source_path returns the bench app dir (sibling of hooks.py) — the built
	# assets live one level under that, in the "restaurant_pos" module folder's own public/
	# (the same path bench build's own asset-linking step uses; get_app_path resolves to that
	# module folder directly and would double it, one level too deep).
	js_path = os.path.join(frappe.get_app_source_path("restaurant_pos"), "restaurant_pos", "public", "back-office", "back-office.js")
	try:
		return int(os.path.getmtime(js_path))
	except OSError:
		return 0
