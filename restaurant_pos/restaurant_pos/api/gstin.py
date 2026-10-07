# Copyright (c) 2026, the project contributors
# For license information, please see license.txt

# Ticket P1-22's GSTIN validation (Validation & Conventions §3): format, the official check
# digit, and that the GSTIN's state code matches the outlet's registered address. A pure module
# so it's unit-testable directly (the ticket's own "Unit: GSTIN checksum validator" test case)
# without needing a live Outlet record for every case.

import re

import frappe

# GST's own check-digit alphabet: index = code point, used by both the weighting pass and the
# final checksum character lookup.
_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# 15 chars: 2-digit state code, 10-char PAN (5 letters, 4 digits, 1 letter), 1 entity code
# (1-9 then A-Z — entity numbering starts at 1, never 0), literal 'Z', 1 check digit. Must match
# docs/06_Validation_and_Conventions.md §3's own regex exactly — ticket P4-19 found and fixed a
# real discrepancy here: this used to allow a leading '0' in the entity-code position
# ([0-9A-Z]), which the docs' own pattern ([1-9A-Z]) correctly excludes.
_GSTIN_SHAPE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")

# Official 2-digit GST state/UT codes (CBIC) — the ones a live outlet is realistically in.
GST_STATE_CODES = {
	"01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh",
	"05": "Uttarakhand", "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh",
	"10": "Bihar", "11": "Sikkim", "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur",
	"15": "Mizoram", "16": "Tripura", "17": "Meghalaya", "18": "Assam", "19": "West Bengal",
	"20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh", "23": "Madhya Pradesh",
	"24": "Gujarat", "26": "Dadra and Nagar Haveli and Daman and Diu", "27": "Maharashtra",
	"28": "Andhra Pradesh (Old)", "29": "Karnataka", "30": "Goa", "31": "Lakshadweep",
	"32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry", "35": "Andaman and Nicobar Islands",
	"36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh", "97": "Other Territory",
}


def gstin_checksum(gstin_without_check_digit: str) -> str:
	"""The 36 (mod-36 Luhn-style) check digit over the first 14 characters."""
	total = 0
	factor = 2
	for ch in reversed(gstin_without_check_digit):
		code_point = _ALPHABET.index(ch)
		product = factor * code_point
		total += (product // 36) + (product % 36)
		factor = 1 if factor == 2 else 2
	remainder = total % 36
	return _ALPHABET[(36 - remainder) % 36]


def validate_gstin(gstin: str) -> None:
	"""Raises frappe.ValidationError with the ticket's own copy on any failure; returns
	normally for a real, well-formed, checksum-correct GSTIN."""
	value = (gstin or "").strip().upper()
	if len(value) != 15 or not _GSTIN_SHAPE.match(value):
		frappe.throw("This GSTIN doesn't look right. Check the 15 characters.", frappe.ValidationError)
	if gstin_checksum(value[:14]) != value[14]:
		frappe.throw("This GSTIN doesn't look right. Check the 15 characters.", frappe.ValidationError)
	state_code = value[:2]
	if state_code not in GST_STATE_CODES:
		frappe.throw("This GSTIN doesn't look right. Check the 15 characters.", frappe.ValidationError)


def gstin_state_name(gstin: str) -> str | None:
	value = (gstin or "").strip().upper()
	return GST_STATE_CODES.get(value[:2]) if len(value) >= 2 else None
