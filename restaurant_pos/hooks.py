app_name = "restaurant_pos"
app_title = "Restaurant POS"
app_publisher = "restaurant-pos"
app_description = "Multi-tenant restaurant POS integrating with ERPNext"
app_email = "noreply@example.com"
app_license = "mit"

# Ticket P3-13: menu items are entered through ERPNext's native Item/BOM forms — these fields
# are what that form needs beyond core Item, and after_migrate makes sure every environment gets
# them on the next `bench migrate`, no manual fixtures import.
after_migrate = [
	"restaurant_pos.restaurant_pos.setup.custom_fields.create_custom_fields_for_menu",
	"restaurant_pos.restaurant_pos.setup.accounting_setup.ensure_branch_accounting_dimension",
	"restaurant_pos.restaurant_pos.setup.billing_setup.ensure_billing_masters",
	"restaurant_pos.restaurant_pos.setup.access_rights_setup.ensure_base_roles",
	"restaurant_pos.restaurant_pos.setup.access_rights_setup.seed_owner_defaults",
	"restaurant_pos.restaurant_pos.setup.security_setup.enforce_admin_two_factor",
	"restaurant_pos.restaurant_pos.setup.web_setup.ensure_login_branding",
	"restaurant_pos.restaurant_pos.setup.report_indexes.ensure_report_indexes",
]

# Tickets P3-29 / P3-05: transactions and audit records are read-only once synced from the Hub.
from restaurant_pos.restaurant_pos.guards.immutability import APPEND_ONLY_DOCTYPES, AUDIT_DOCTYPES, GUARD_EVENTS, HUB_ORIGINATED_DOCTYPES

doc_events = {
	"Item": {"validate": "restaurant_pos.restaurant_pos.api.menu.validate_item"},
	"Item Price": {"validate": "restaurant_pos.restaurant_pos.api.menu.validate_item_price"},
}
for _doctype in (*AUDIT_DOCTYPES, *APPEND_ONLY_DOCTYPES, *HUB_ORIGINATED_DOCTYPES):
	doc_events[_doctype] = GUARD_EVENTS
# Ticket P3-32: a change to master data the Hub caches queues a (coalesced) signed change hint for the outlets it affects.
from restaurant_pos.restaurant_pos.api.hub_push import MASTER_DOCTYPES as _HUB_MASTER_DOCTYPES

for _doctype in _HUB_MASTER_DOCTYPES:
	_events = doc_events.setdefault(_doctype, {})
	for _event in ("after_insert", "on_update", "on_trash"):
		_hook = "restaurant_pos.restaurant_pos.api.hub_push.on_master_change"
		_existing = _events.get(_event)
		_events[_event] = _hook if not _existing else [*([_existing] if isinstance(_existing, str) else _existing), _hook]
# Ticket P4-31: keep 2FA limited to the admin roles even if the setting is toggled in Desk.
doc_events["System Settings"] = {"on_update": "restaurant_pos.restaurant_pos.setup.security_setup.reassert_admin_only_two_factor"}

# Tickets P8.5-03 / P8.5-13: outlet/branch scope for every outlet-bearing doctype, enforced in the data layer
# (lists and single-record checks) rather than screen by screen. See api/scope.py.
from restaurant_pos.restaurant_pos.api.scope import SCOPED_HOOK_DOCTYPES

permission_query_conditions = {d: "restaurant_pos.restaurant_pos.api.scope.outlet_scope_conditions" for d in SCOPED_HOOK_DOCTYPES}
has_permission = {d: "restaurant_pos.restaurant_pos.api.scope.outlet_scope_permission" for d in SCOPED_HOOK_DOCTYPES}

# Ticket P8.5-01: the Back Office is a client-routed SPA (react-router), so any deep link under
# /back-office (e.g. /back-office/staff) must still resolve to the same www/back_office.py page,
# not a 404 — the second rule is the catch-all for that; www files can't have a hyphen in their
# module name, hence the rewrite to back_office.
website_route_rules = [
	{"from_route": "/back-office", "to_route": "back_office"},
	{"from_route": "/back-office/<path:app_path>", "to_route": "back_office"},
]

# Apps
# ------------------

# Ticket P4-26: the Aurora look of Desk and the login page comes from the separate `aurora_ui` theme app, so it
# must be installed first (its tokens are what public/css/web.css reads). aurora_ui is never edited from here.
required_apps = ["erpnext"]

# Each item in the list will be shown as an app in the apps page
# Ticket P8.5-02's own "Back Office entries" in Desk — the same mechanism Frappe's own CRM and
# Helpdesk apps use to appear in the app switcher (Tech Architecture §12.1), rather than a
# hand-rolled navbar link or editing the separate aurora_ui theme app's own shell.js (a
# different repo — out of scope here; see STATUS_NOTES for why).
add_to_apps_screen = [
	{
		"name": "restaurant_pos",
		"title": "Back Office",
		"route": "/back-office",
	}
]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# Tickets P8.5-14 / P4-26 / P4-28: Desk additions (Back-to-Back-Office bar, Staff PIN form actions) and the
# styling for them, in Frappe's own colour variables so the active theme (Aurora) carries through.
app_include_css = "/assets/restaurant_pos/css/desk.css"
app_include_js = "/assets/restaurant_pos/js/desk.js"

# include js, css files in header of web template
# Ticket P4-26: login / website pages, in aurora_ui's own tokens (loads after aurora_ui_web.bundle.css).
web_include_css = "/assets/restaurant_pos/css/web.css"
# web_include_js = "/assets/restaurant_pos/js/restaurant_pos.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "restaurant_pos/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "restaurant_pos/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# automatically load and sync documents of this doctype from downstream apps
# importable_doctypes = [doctype_1]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "restaurant_pos.utils.jinja_methods",
# 	"filters": "restaurant_pos.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "restaurant_pos.install.before_install"
# after_install = "restaurant_pos.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "restaurant_pos.uninstall.before_uninstall"
# after_uninstall = "restaurant_pos.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "restaurant_pos.utils.before_app_install"
# after_app_install = "restaurant_pos.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "restaurant_pos.utils.before_app_uninstall"
# after_app_uninstall = "restaurant_pos.utils.after_app_uninstall"

# Build
# ------------------
# To hook into the build process

# after_build = "restaurant_pos.build.after_build"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "restaurant_pos.notifications.get_notification_config"

# Awesome Bar
# -----------
# Extra search results: list of dicts with label, description, route, index.
# route: ["List", "ToDo"], "/desk/docs/some/page", or "https://example.com"
# awesomebar_search = ["restaurant_pos.search.awesomebar_results"]

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# Document Events
# ---------------
# Hook on document methods and events

# doc_events = {
# 	"*": {
# 		"on_update": "method",
# 		"on_cancel": "method",
# 		"on_trash": "method"
# 	}
# }

# Scheduled Tasks
# ---------------

# Ticket P3-35: archiving does NOTHING until the site's retention policy is configured (restaurant_pos_archive_after_days).
scheduler_events = {
	"daily": ["restaurant_pos.restaurant_pos.archiving.archive_due"],
	# Ticket P3-38: starts the shift-close jobs that are due, under the per-minute cap.
	"cron": {"* * * * *": ["restaurant_pos.restaurant_pos.api.shift_close.dispatch_due"]},
}

# scheduler_events = {
# 	"all": [
# 		"restaurant_pos.tasks.all"
# 	],
# 	"daily": [
# 		"restaurant_pos.tasks.daily"
# 	],
# 	"hourly": [
# 		"restaurant_pos.tasks.hourly"
# 	],
# 	"weekly": [
# 		"restaurant_pos.tasks.weekly"
# 	],
# 	"monthly": [
# 		"restaurant_pos.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "restaurant_pos.install.before_tests"

# Extend DocType Class
# ------------------------------
#
# Specify custom mixins to extend the standard doctype controller.
# extend_doctype_class = {
# 	"Task": "restaurant_pos.custom.task.CustomTaskMixin"
# }

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "restaurant_pos.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "restaurant_pos.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["restaurant_pos.utils.before_request"]
# after_request = ["restaurant_pos.utils.after_request"]

# Job Events
# ----------
# before_job = ["restaurant_pos.utils.before_job"]
# after_job = ["restaurant_pos.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"restaurant_pos.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []

