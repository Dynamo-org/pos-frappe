# Ticket P3-39 (ERPNext half): an item's picture reaches the Hub resized, as WebP, and versioned.
#   - the menu payload carries the picture's VERSION (never the picture): none without one, a stable value with one, a different
#     value when the picture changes;
#   - get_item_image resizes to one of three widths (aspect kept, never enlarged), returns WebP, and refuses any other width, an
#     item with no picture, an unreadable file, and any caller that is not the Hub service account (or not allowed that outlet);
#   - a big picture really gets smaller (the point of resizing).
# Uses a real picture (generated with Pillow) attached to an existing item, and puts that item back exactly as it was.
#   bench --site dev.localhost execute restaurant_pos.restaurant_pos.tests.verify_item_images.run

import io

import frappe
from PIL import Image

from restaurant_pos.restaurant_pos.api.v1 import sync

failures = []


def check(cond, label):
	print(("PASS  " if cond else "FAIL  ") + label)
	if not cond:
		failures.append(label)


def png_bytes(width, height, colour):
	img = Image.new("RGB", (width, height), colour)
	# some detail so the file is not trivially tiny
	for x in range(0, width, 7):
		for y in range(0, height, 11):
			img.putpixel((x, y), (x % 255, y % 255, (x + y) % 255))
	buf = io.BytesIO()
	img.save(buf, format="PNG")
	return buf.getvalue()


def get_image(outlet, item, width):
	frappe.local.response.pop("filecontent", None)
	sync.get_item_image(outlet, item, width)
	return frappe.local.response.get("filecontent")


def run():
	frappe.set_user("Administrator")
	item = frappe.db.get_value("Item", {"disabled": 0, "is_sales_item": 1, "restaurant_station": ["is", "set"]}, "name")
	original_image = frappe.db.get_value("Item", item, "image")
	files = []
	try:
		price_list = frappe.db.get_value("Outlet", "KOR", "price_list")
		frappe.db.set_value("Item", item, "image", None, update_modified=False)
		frappe.db.commit()
		check(sync._menu_item_payload(item, price_list)["image_version"] is None, "an item with no picture has no image_version")

		def attach(content, name):
			f = frappe.get_doc({"doctype": "File", "file_name": name, "content": content, "is_private": 0, "attached_to_doctype": "Item", "attached_to_name": item, "attached_to_field": "image"}).insert(ignore_permissions=True)
			files.append(f.name)
			frappe.db.set_value("Item", item, "image", f.file_url, update_modified=False)
			frappe.db.commit()
			return f

		big = png_bytes(1600, 1000, (200, 60, 30))
		attach(big, "zz-p339-big.png")
		v1 = sync._menu_item_payload(item, price_list)["image_version"]
		check(bool(v1) and len(v1) == 12 and v1 == sync._menu_item_payload(item, price_list)["image_version"], f"with a picture the payload carries a stable 12-character version ({v1}); the picture itself is not in the payload")
		check("image" not in sync._menu_item_payload(item, price_list), "...and never the file or its path")

		sizes = {}
		for width in sync.IMAGE_WIDTHS:
			data = get_image("KOR", item, width)
			picture = Image.open(io.BytesIO(data))
			sizes[width] = len(data)
			check(picture.format == "WEBP" and max(picture.size) == width, f"width {width}: WebP, longest side {max(picture.size)}")
			check(abs(picture.size[0] / picture.size[1] - 1.6) < 0.05, f"width {width}: the aspect ratio is kept ({picture.size[0]}x{picture.size[1]})")
		check(sizes[160] < sizes[320] < sizes[640] < len(big), f"resizing makes it smaller: {len(big):,} bytes original -> {sizes[640]:,} / {sizes[320]:,} / {sizes[160]:,}")
		check(frappe.local.response.get("type") == "download" and frappe.local.response.get("filename", "").endswith("-640.webp"), "it is returned as a file download (the Hub reads bytes, not JSON)")

		small = png_bytes(100, 60, (10, 120, 200))
		attach(small, "zz-p339-small.png")
		tiny = Image.open(io.BytesIO(get_image("KOR", item, 640)))
		check(tiny.size == (100, 60), "a small picture is never enlarged")
		v2 = sync._menu_item_payload(item, price_list)["image_version"]
		check(v2 != v1, "a different picture is a different version (so every cached copy is replaced)")

		# refusals
		for bad in (0, 100, 321, 1280, "abc"):
			try:
				sync.get_item_image("KOR", item, bad)
				check(False, f"width {bad!r} is refused")
			except (frappe.ValidationError, ValueError):
				check(True, f"width {bad!r} is refused")
		frappe.db.set_value("Item", item, "image", None, update_modified=False)
		try:
			sync.get_item_image("KOR", item, 320)
			check(False, "an item with no picture is refused")
		except frappe.DoesNotExistError:
			check(True, "an item with no picture is refused (does not exist)")
		frappe.db.set_value("Item", item, "image", "/files/zz-p339-not-there.png", update_modified=False)
		try:
			sync.get_item_image("KOR", item, 320)
			check(False, "a missing file is refused")
		except Exception:  # noqa: BLE001
			check(True, "a picture whose file is missing or unreadable is refused cleanly")
		frappe.db.rollback()
	finally:
		frappe.set_user("Administrator")
		frappe.db.set_value("Item", item, "image", original_image, update_modified=False)
		for name in files:
			frappe.delete_doc("File", name, force=1, ignore_permissions=True)
		frappe.db.commit()
		check(frappe.db.get_value("Item", item, "image") == original_image, "the item's picture is back exactly as it was")

	# access: only the Hub service account (the whole-site harness proves the full persona table; this proves THIS method)
	from restaurant_pos.restaurant_pos.api import scope

	user = "zz-p339-owner@example.test"
	try:
		if frappe.db.exists("User", user):
			frappe.delete_doc("User", user, force=1, ignore_permissions=True)
		u = frappe.get_doc({"doctype": "User", "email": user, "first_name": "ZZ", "send_welcome_email": 0}).insert(ignore_permissions=True)
		u.add_roles("Owner/Manager", "System Manager")
		frappe.db.commit()
		frappe.set_user(user)
		try:
			sync.get_item_image("KOR", item, 320)
			check(False, "an Owner/Manager (even with System Manager) cannot fetch Hub pictures")
		except frappe.PermissionError:
			check(True, "an Owner/Manager (even with System Manager) cannot fetch Hub pictures: it is the Hub service account's method")
		check(scope.HUB_SERVICE_ROLE in frappe.get_roles("Administrator"), "(the Hub service role exists and Administrator holds it)")
	finally:
		frappe.set_user("Administrator")
		if frappe.db.exists("User", user):
			frappe.delete_doc("User", user, force=1, ignore_permissions=True)
		frappe.db.commit()
	print("ALL PASS" if not failures else f"{len(failures)} FAILED")
	if failures:
		raise SystemExit(1)
