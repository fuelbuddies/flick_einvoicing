"""Code lists bundled from the PINT-AE specification, too long to keep by hand in codes.py:

- units: UN/ECE Recommendation 20, with Recommendation 21 package codes prefixed X. Every site
  loads them into its UOM Code List on install and migrate. Which code an ERPNext UOM stands
  for stays the client's decision, so no UOM is linked here.
- charge-reasons: UNCL 7161, the reason codes for document-level charges, offered in
  E Invoicing Settings.

Each list ships as a JSON file beside this module, read from the specification's code list
page. When the specification publishes new lists, refresh the files (from the bench folder, no
site needed), review the diff, commit and deploy; the next migrate loads the new codes
everywhere:

    python apps/flick_einvoicing/flick_einvoicing/einvoice/code_lists.py [units] [charge-reasons]

Runs on the standard library alone, so it needs no bench environment.
"""

import argparse
import html
import json
import re
import urllib.request
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# The specification's code list folder; a new version of PINT-AE lives at a new one.
BASE_URL = "https://docs.peppol.eu/poac/ae/v1.0.1/pint-ae/trn-invoice/codelist/"


@dataclass(frozen=True)
class CodeList:
	page: str
	file: str
	# A page that parses to fewer codes, or without these, is not the code list (an error page,
	# or a layout this parser no longer reads), so it never replaces the file.
	minimum: int
	expected: tuple[str, ...]

	@property
	def path(self) -> Path:
		return Path(__file__).with_name(self.file)


UNITS = "units"
CHARGE_REASONS = "charge-reasons"
LISTS = {
	UNITS: CodeList(
		"UNECERec20/", "unit_codes.json", 1000, ("C62", "DAY", "H87", "KGM", "LTR", "MTR", "XBX")
	),
	CHARGE_REASONS: CodeList("UNCL7161/", "charge_reason_codes.json", 100, ("AA", "ABK", "FC", "ZZZ")),
}

_ROW = re.compile(r'<tr id="[^"]*">\s*<td><code>([^<]+)</code></td>\s*<td>(.*?)</tr>', re.S)
_TAG = re.compile(r"<[^>]+>")
_ISSUED = re.compile(r'<meta name="issued" content="([^"]+)"')
_TITLE = re.compile(r"<title>([^<|]+)")


@lru_cache(maxsize=len(LISTS))
def read(name: str) -> dict:
	return json.loads(LISTS[name].path.read_text(encoding="utf-8"))


def codes(name: str) -> dict[str, list[str]]:
	"""Code -> [name, description]."""
	return read(name)["codes"]


def load_units(code_list: str) -> int:
	"""Bring a site's UOM code list in line with the bundled codes; return how many were added.

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
	for code, (name, description) in codes(UNITS).items():
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


# Refreshing the bundled files


def parse(page: str, source: str, code_list: CodeList) -> dict:
	found = {}
	for code, cell in _ROW.findall(page):
		name, _, description = cell.partition("<p>")
		found[_text(code)] = [_text(name), _text(description)]
	missing = [code for code in code_list.expected if code not in found]
	if len(found) < code_list.minimum or missing:
		raise ValueError(
			f"{source} does not look like the code list: {len(found)} codes, missing {missing or 'none'}."
		)
	issued, title = _ISSUED.search(page), _TITLE.search(page)
	return {
		"source": source,
		"title": _text(title.group(1)) if title else None,
		"issued": issued.group(1) if issued else None,
		"codes": dict(sorted(found.items())),
	}


def _text(fragment: str) -> str:
	return " ".join(html.unescape(_TAG.sub(" ", fragment)).split())


def write(path: Path, data: dict) -> None:
	"""One code per line, so a refresh reads as a short diff."""
	head = [
		f"\t{json.dumps(key)}: {json.dumps(data[key], ensure_ascii=False)},\n"
		for key in ("source", "title", "issued")
	]
	rows = [
		f"\t\t{json.dumps(code)}: {json.dumps(value, ensure_ascii=False)}"
		for code, value in data["codes"].items()
	]
	path.write_text(
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


def refresh(name: str, base_url: str = BASE_URL, saved_page: str | None = None) -> list[str]:
	code_list = LISTS[name]
	url = base_url.rstrip("/") + "/" + code_list.page
	if saved_page:
		page = Path(saved_page).read_text(encoding="utf-8")
	else:
		request = urllib.request.Request(url, headers={"User-Agent": "flick_einvoicing code list refresh"})
		with urllib.request.urlopen(request, timeout=60) as response:
			page = response.read().decode("utf-8")
	new = parse(page, url, code_list)
	old = json.loads(code_list.path.read_text(encoding="utf-8")) if code_list.path.exists() else {}
	write(code_list.path, new)
	return changes(old, new)


if __name__ == "__main__":
	parser = argparse.ArgumentParser(description="Refresh the code lists bundled from PINT-AE.")
	parser.add_argument(
		"lists", nargs="*", metavar="list", help=f"{' or '.join(LISTS)} (default: every list)"
	)
	parser.add_argument(
		"--base-url", default=BASE_URL, help="the specification's code list folder (default: %(default)s)"
	)
	parser.add_argument(
		"--saved-page", help="read a copy of the page saved earlier instead of downloading it (one list)"
	)
	arguments = parser.parse_args()
	names = arguments.lists or list(LISTS)
	if unknown := [name for name in names if name not in LISTS]:
		parser.error(f"no list called {', '.join(unknown)}; choose from {', '.join(LISTS)}.")
	if arguments.saved_page and len(names) != 1:
		parser.error("--saved-page reads one list; name it.")
	for name in names:
		print(f"{name}:", *refresh(name, arguments.base_url, arguments.saved_page), sep="\n")
		print(f"Wrote {LISTS[name].path}.")
	print("Review the diff, commit, deploy, then migrate each site.")
