"""Differences between Frappe v15 and v16 the app has to bridge."""

import frappe

V16 = int(frappe.__version__.split(".")[0]) >= 16


def count(alias: str = "count"):
	"""A COUNT(*) column for get_list/get_all: v16 refuses SQL functions written as strings,
	v15 does not understand the dict form."""
	return {"COUNT": "*", "as": alias} if V16 else f"count(*) as {alias}"


def file_bytes(file) -> bytes:
	"""A File's raw bytes. File.get_content() in v16 decodes anything it can read as text
	(windows-1252 accepts almost any bytes), which corrupts PDFs and images."""
	with open(file.get_full_path(), "rb") as f:
		return f.read()
