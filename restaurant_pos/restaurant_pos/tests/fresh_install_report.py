# Copyright (c) 2026, the project contributors
# For license information, please see license.txt

# Used by tools/frappe-export/fresh-install-check.sh: prints what the restaurant_pos setup has produced on a site RIGHT NOW,
# so the script can show the difference between "just installed" and "after the first migrate" on a brand-new site (which is
# what a new Frappe Cloud site is). Read-only.
#
#   bench --site <site> execute restaurant_pos.restaurant_pos.tests.fresh_install_report.report --kwargs '{"label": "after install"}'

import frappe


def report(label: str = "now") -> dict:
	from restaurant_pos.restaurant_pos.setup.access_rights_setup import BASE_ROLES, HUB_SERVICE_ROLE
	from restaurant_pos.restaurant_pos.setup.custom_fields import CUSTOM_FIELDS

	roles = {role: bool(frappe.db.exists("Role", role)) for role in [*BASE_ROLES, HUB_SERVICE_ROLE]}
	fields = {
		f"{doctype}.{field['fieldname']}": bool(frappe.db.exists("Custom Field", {"dt": doctype, "fieldname": field["fieldname"]}))
		for doctype, doctype_fields in CUSTOM_FIELDS.items()
		for field in doctype_fields
	}
	dimension = bool(frappe.db.exists("Accounting Dimension", {"document_type": "Branch"}))
	doctypes = {name: bool(frappe.db.exists("DocType", name)) for name in ("Outlet", "Hub Credential", "Hub Event", "Shift Close Job", "Staff PIN")}

	missing = (
		[f"Role {name}" for name, ok in roles.items() if not ok]
		+ [f"Custom Field {name}" for name, ok in fields.items() if not ok]
		+ ([] if dimension else ["Accounting Dimension Branch"])
		+ [f"DocType {name}" for name, ok in doctypes.items() if not ok]
	)
	print(
		f"[{label}] roles {sum(roles.values())}/{len(roles)}, custom fields {sum(fields.values())}/{len(fields)}, "
		f"Branch accounting dimension {'yes' if dimension else 'NO'}, doctypes {sum(doctypes.values())}/{len(doctypes)}, "
		f"companies {frappe.db.count('Company')}"
	)
	print(f"[{label}] MISSING: {', '.join(missing) if missing else 'nothing'}")
	return {"missing": missing}
