"""Unit codes PINT-AE allows on invoice lines: UN/ECE Recommendation 20, with Recommendation 21
package codes prefixed X.

The codes ship in unit_codes.json, read from the code list of the PINT-AE specification, and
every site loads them into its UOM Code List on install and migrate. Which code an ERPNext UOM
stands for stays the client's decision, so no UOM is linked here.

When the specification publishes a new list, refresh the file (from the bench folder, no site
needed), review the diff, commit and deploy; the next migrate loads the new codes everywhere:

    python apps/flick_einvoicing/flick_einvoicing/einvoice/unit_codes.py [code list page URL]

Runs on the standard library alone, so it needs no bench environment.
"""

import argparse
import html
import json
import re
import urllib.request
from functools import lru_cache
from pathlib import Path

SOURCE_URL = "https://docs.peppol.eu/poac/ae/v1.0.1/pint-ae/trn-invoice/codelist/UNECERec20/"
DATA_FILE = Path(__file__).with_name("unit_codes.json")
# A page that parses to fewer codes, or without these, is not the code list (an error page, or a
# layout this parser no longer reads), so it never replaces the file.
MINIMUM_CODES = 1000
EXPECTED_CODES = ("C62", "DAY", "H87", "KGM", "LTR", "MTR", "XBX")

_ROW = re.compile(r'<tr id="[^"]*">\s*<td><code>([^<]+)</code></td>\s*<td>(.*?)</tr>', re.S)
_TAG = re.compile(r"<[^>]+>")
_ISSUED = re.compile(r'<meta name="issued" content="([^"]+)"')
_TITLE = re.compile(r"<title>([^<|]+)")


@lru_cache(maxsize=1)
def read() -> dict:
	return json.loads(DATA_FILE.read_text(encoding="utf-8"))


def official() -> dict[str, list[str]]:
	"""Code -> [name, description]."""
	return read()["codes"]


def load(code_list: str) -> int:
	"""Bring a site's code list in line with the bundled codes; return how many were added.

	Missing codes are added and changed names updated. A code the specification has dropped is
	kept, since UOMs may still link to it: the invoice check refuses to send it."""
	import frappe
	from frappe.utils import now

	existing = {
		row.common_code: row
		for row in frappe.get_all(
			"Common Code",
			filters={"code_list": code_list},
			fields=["name", "common_code", "title", "description"],
		)
	}
	timestamp, user = now(), frappe.session.user
	new_rows = []
	for code, (name, description) in official().items():
		title, description = name or code, description or None
		row = existing.get(code)
		if not row:
			new_rows.append(
				(
					frappe.generate_hash(length=10),
					timestamp,
					timestamp,
					user,
					user,
					code_list,
					code,
					title,
					description,
				)
			)
		elif (row.title, row.description or None) != (title, description):
			frappe.db.set_value(
				"Common Code", row.name, {"title": title, "description": description}, update_modified=False
			)
	if new_rows:
		frappe.db.bulk_insert(
			"Common Code",
			(
				"name",
				"creation",
				"modified",
				"owner",
				"modified_by",
				"code_list",
				"common_code",
				"title",
				"description",
			),
			new_rows,
		)
	return len(new_rows)


# Refreshing the bundled file


def parse(page: str, source: str) -> dict:
	codes = {}
	for code, cell in _ROW.findall(page):
		name, _, description = cell.partition("<p>")
		codes[_text(code)] = [_text(name), _text(description)]
	missing = [code for code in EXPECTED_CODES if code not in codes]
	if len(codes) < MINIMUM_CODES or missing:
		raise ValueError(
			f"{source} does not look like the unit code list: {len(codes)} codes, missing {missing or 'none'}."
		)
	issued, title = _ISSUED.search(page), _TITLE.search(page)
	return {
		"source": source,
		"title": _text(title.group(1)) if title else None,
		"issued": issued.group(1) if issued else None,
		"codes": dict(sorted(codes.items())),
	}


def _text(fragment: str) -> str:
	return " ".join(html.unescape(_TAG.sub(" ", fragment)).split())


def write(data: dict) -> None:
	"""One code per line, so a refresh reads as a short diff."""
	head = [
		f"\t{json.dumps(key)}: {json.dumps(data[key], ensure_ascii=False)},\n"
		for key in ("source", "title", "issued")
	]
	rows = [
		f"\t\t{json.dumps(code)}: {json.dumps(value, ensure_ascii=False)}"
		for code, value in data["codes"].items()
	]
	DATA_FILE.write_text(
		"{\n" + "".join(head) + '\t"codes": {\n' + ",\n".join(rows) + "\n\t}\n}\n", encoding="utf-8"
	)


def changes(old: dict, new: dict) -> list[str]:
	before, after = old.get("codes", {}), new["codes"]
	added = sorted(set(after) - set(before))
	dropped = sorted(set(before) - set(after))
	renamed = sorted(code for code in set(before) & set(after) if before[code] != after[code])
	return [
		f"issued {old.get('issued')} -> {new['issued']}",
		f"{len(after)} codes: {len(added)} added, {len(dropped)} dropped, {len(renamed)} with new wording",
		*(f"  + {code} {after[code][0]}" for code in added),
		*(
			f"  - {code} {before[code][0]} (stays on sites; invoices using it are refused)"
			for code in dropped
		),
		*(f"  ~ {code} {before[code][0]} -> {after[code][0]}" for code in renamed),
	]


def refresh(url: str = SOURCE_URL, saved_page: str | None = None) -> list[str]:
	if saved_page:
		page = Path(saved_page).read_text(encoding="utf-8")
	else:
		request = urllib.request.Request(url, headers={"User-Agent": "flick_einvoicing unit code refresh"})
		with urllib.request.urlopen(request, timeout=60) as response:
			page = response.read().decode("utf-8")
	new = parse(page, url)
	old = json.loads(DATA_FILE.read_text(encoding="utf-8")) if DATA_FILE.exists() else {}
	write(new)
	return changes(old, new)


if __name__ == "__main__":
	parser = argparse.ArgumentParser(description="Refresh unit_codes.json from the PINT-AE code list.")
	parser.add_argument("url", nargs="?", default=SOURCE_URL, help="code list page (default: %(default)s)")
	parser.add_argument(
		"--saved-page", help="read a copy of the page saved earlier instead of downloading it"
	)
	arguments = parser.parse_args()
	print("\n".join(refresh(arguments.url, arguments.saved_page)))
	print(f"Wrote {DATA_FILE}. Review the diff, commit, deploy, then migrate each site.")
