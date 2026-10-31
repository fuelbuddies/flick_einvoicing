"""Purchase invoices made from received e-invoices, brought to the supplier's figures.

ERPNext keeps a line's rate to the site's currency precision, two decimals on most sites, while a
supplier may price to six. Such a line comes out some fils away from the supplier's, and the net
total, VAT and total with it. That difference goes into the invoice's Additional Discount on Net
Total, together with the supplier's own invoice-level allowances less its invoice-level charges.
ERPNext spreads the discount over the lines' net amounts and works VAT out on what is left, so the
invoice comes to the supplier's figures with standard fields only. A negative discount raises the
total.

Nothing else is absorbed. A line whose quantity or price differs from the supplier's, because our
receipt or order says otherwise or the reviewer changed it, keeps its difference, and differences()
says so for whoever reviews the invoice.
"""

import json

import frappe
from frappe.utils import cint, cstr, flt, fmt_money
from frappe.utils.data import round_based_on_smallest_currency_fraction

from flick_einvoicing.einvoice import codes

RECEIVED = "E Invoice Received"
FIL = 0.01


def document_of(invoice) -> dict:
	"""The supplier's e-invoice a purchase invoice was made from."""
	if not invoice.get("einvoice_received"):
		return {}
	return json.loads(frappe.db.get_value(RECEIVED, invoice.einvoice_received, "document_json") or "{}")


# The supplier's figures


def line_amount(line: dict) -> float:
	"""The supplier's amount for a line, after its own allowances and charges: as stated, else as
	the supplier works it out."""
	stated = line.get("line_extension_amount")
	if stated not in (None, ""):
		return flt(stated)
	extra = sum(
		flt(entry.get("amount")) * (1 if entry.get("charge_indicator") else -1)
		for entry in line.get("allowance_charges") or []
	)
	return flt(flt(line.get("quantity")) * _price(line) + extra, 2)


def unit_rate(line: dict) -> float:
	"""What the supplier charges for one unit: the line's amount over its quantity, which takes in
	the price's base quantity and the line's own allowances and charges."""
	qty = flt(line.get("quantity"))
	return line_amount(line) / qty if qty else _price(line)


def _price(line: dict) -> float:
	# A price may be for more than one unit: 25.00 for 100 screws, say.
	return flt(line.get("unit_price")) / (flt(line.get("base_quantity")) or 1)


def figures(document: dict) -> frappe._dict:
	"""The supplier's totals, as stated, else worked out from what is."""
	totals = document.get("invoice_totals") or {}

	def stated(key):
		return totals.get(key) not in (None, "")

	lines = flt(sum(line_amount(line) for line in document.get("invoice_lines") or []), 2)
	if stated("tax_exclusive_amount"):
		net = flt(totals["tax_exclusive_amount"])
	else:
		net = flt(lines - flt(totals.get("allowance_total_amount")) + flt(totals.get("charge_total_amount")), 2)
	prepaid = flt(totals.get("prepaid_amount"))
	rounding = flt(totals.get("payable_rounding_amount"))
	vat = flt(totals["tax_amount"]) if stated("tax_amount") else None
	if stated("tax_inclusive_amount"):
		total = flt(totals["tax_inclusive_amount"])
	elif vat is not None:
		total = flt(net + vat, 2)
	elif stated("payable_amount"):
		total = flt(flt(totals["payable_amount"]) + prepaid - rounding, 2)
	else:
		total = None
	if vat is None and total is not None:
		vat = flt(total - net, 2)
	return frappe._dict(
		lines=lines,
		stated_lines=flt(totals["line_extension_amount"]) if stated("line_extension_amount") else None,
		net=net,
		# The supplier's invoice-level allowances less its charges, as its totals have them.
		invoice_level=flt(lines - net, 2),
		vat=vat,
		total=total,
		prepaid=prepaid,
		rounding=rounding,
		payable=flt(totals["payable_amount"]) if stated("payable_amount") else total,
		vat_in_aed=flt(totals["total_tax_amount_in_aed"]) if stated("total_tax_amount_in_aed") else None,
	)


# Rows and lines


def pairs(invoice, document) -> tuple[list[tuple[dict, object]], list, bool]:
	"""Each of the supplier's lines with the row that bills it (None once taken off), the rows that
	bill none of them, and whether rows could be told apart at all."""
	lines = document.get("invoice_lines") or []
	rows = list(invoice.items)
	numbered = [row for row in rows if cint(row.get("einvoice_line_no"))]
	if not numbered:
		if len(rows) == len(lines):
			# Made before rows carried their line: one row a line, in the supplier's order.
			return list(zip(lines, rows)), [], True
		return [(line, None) for line in lines], rows, False
	by_number = {}
	for row in numbered:
		by_number.setdefault(cint(row.einvoice_line_no), row)
	matched = [(line, by_number.get(number)) for number, line in enumerate(lines, 1)]
	used = {id(row) for _line, row in matched if row is not None}
	return matched, [row for row in rows if id(row) not in used], True


def billed_as_stated(row, line: dict, sign: int) -> bool:
	"""Whether the row bills the supplier's quantity at the supplier's price, as far as this site
	holds prices: then whatever its amount lacks is rounding."""
	return same(row.qty, sign * flt(line.get("quantity")), row.precision("qty")) and same(
		row.rate, unit_rate(line), row.precision("rate")
	)


def same(a, b, precision) -> bool:
	return abs(flt(a, precision) - flt(b, precision)) < 0.5 / 10**precision


def _sign(invoice) -> int:
	return -1 if invoice.get("is_return") else 1


# The Additional Discount


def absorbed(invoice, document) -> frappe._dict:
	"""The Additional Discount that brings the invoice to the supplier's figures: the rounding on
	lines billed as the supplier states them, plus the supplier's invoice-level allowances less
	its charges. Signed as the invoice's amounts are, so negative on a return."""
	sign = _sign(invoice)
	precision = invoice.precision("discount_amount")
	matched, _extra, _paired = pairs(invoice, document)
	rounding, rounded = 0.0, []
	for line, row in matched:
		if row is None or not billed_as_stated(row, line, sign):
			continue
		difference = flt(row.amount) - sign * line_amount(line)
		if flt(difference, precision):
			rounded.append(cstr(line.get("id")))
		rounding += difference
	invoice_level = sign * figures(document).invoice_level
	return frappe._dict(
		rounding=flt(rounding, precision),
		invoice_level=flt(invoice_level, precision),
		discount=flt(rounding + invoice_level, precision),
		lines=rounded,
	)


def apply(invoice, document) -> frappe._dict:
	"""Bring the invoice to the supplier's figures: the Additional Discount on Net Total takes in
	the rounding and the supplier's invoice-level allowances and charges, and the total stays
	unrounded unless rounding it gives what the supplier asks. The invoice is not saved."""
	result = absorbed(invoice, document)
	invoice.apply_discount_on = "Net Total"
	invoice.additional_discount_percentage = 0
	invoice.discount_amount = result.discount
	invoice.disable_rounded_total = 1
	invoice.calculate_taxes_and_totals()
	supplier = figures(document)
	if supplier.rounding and supplier.total is not None:
		precision = invoice.precision("rounded_total")
		rounded = round_based_on_smallest_currency_fraction(invoice.grand_total, invoice.currency, precision)
		if same(rounded, _sign(invoice) * (supplier.total + supplier.rounding), precision):
			invoice.disable_rounded_total = 0
			invoice.calculate_taxes_and_totals()
	return result


def explain(invoice, result) -> list[str]:
	"""What the invoice's Additional Discount is made of, in words."""
	if not result.discount:
		return []
	precision = invoice.precision("discount_amount")
	parts = []
	if result.rounding:
		decimals = invoice.items[0].precision("rate") if invoice.items else precision
		which = f"line{'s' if len(result.lines) > 1 else ''} {', '.join(result.lines)}"
		parts.append(
			f"{money(result.rounding, precision)} from the supplier's prices on {which}, which have more "
			f"decimals than the {decimals} this site keeps for rates"
		)
	if result.invoice_level:
		parts.append(f"{money(result.invoice_level, precision)} for the supplier's invoice-level allowances less charges")
	return [f"Additional Discount {money(result.discount, precision)} on the net total: {'; '.join(parts)}."]


# Differences


def differences(invoice, document, lines: bool = True) -> list[str]:
	"""Where the invoice, as it stands, differs from the supplier's e-invoice, in words."""
	sign = _sign(invoice)
	supplier = figures(document)
	precision = invoice.precision("grand_total")
	found = []

	def amount(value):
		return money(value, precision)

	matched, extra, paired = pairs(invoice, document)
	if lines and paired:
		for line, row in matched:
			label = f"Line {line.get('id')}"
			if row is None:
				found.append(
					f"{label} ({cstr(line.get('name') or line.get('description'))[:60]}) is not on this invoice: "
					f"{amount(line_amount(line))}."
				)
				continue
			qty = sign * flt(line.get("quantity"))
			if not same(row.qty, qty, row.precision("qty")):
				found.append(f"{label}: row {row.idx} bills {written(row.qty)}, the supplier {written(qty)}.")
			if not same(row.rate, unit_rate(line), row.precision("rate")):
				found.append(
					f"{label}: row {row.idx} is at {written(row.rate)}, the supplier's price {written(unit_rate(line))}."
				)
		for row in extra:
			found.append(
				f"Row {row.idx} ({row.item_name or row.item_code or ''}) is not on the supplier's invoice: "
				f"{amount(sign * flt(row.amount))}."
			)

	if supplier.stated_lines is not None and not same(supplier.stated_lines, supplier.lines, 2):
		found.append(f"The supplier's lines add up to {amount(supplier.lines)}, its totals say {amount(supplier.stated_lines)}.")
	net_differs = not same(sign * flt(invoice.net_total), supplier.net, precision)
	if net_differs:
		found.append(f"Net total {amount(sign * flt(invoice.net_total))}, the supplier's {amount(supplier.net)}.")
	vat = sign * flt(invoice.total_taxes_and_charges)
	vat_differs = supplier.vat is not None and not same(vat, supplier.vat, precision)
	if vat_differs:
		why = _why_vat_differs(invoice, document, vat - supplier.vat, net_differs)
		found.append(
			f"VAT {amount(vat)}, the supplier's {amount(supplier.vat)}: claim only what the invoice states.{' ' + why if why else ''}"
		)
	if supplier.total is not None and not (net_differs or vat_differs):
		if not same(sign * flt(invoice.grand_total), supplier.total, precision):
			found.append(f"Total {amount(sign * flt(invoice.grand_total))}, the supplier's {amount(supplier.total)}.")
	found += _category_differences(invoice, document, matched)

	due = sign * flt(invoice.grand_total if invoice.get("disable_rounded_total") else invoice.rounded_total)
	if supplier.total is not None and supplier.rounding and not same(due, supplier.total + supplier.rounding, precision):
		found.append(
			f"The supplier rounds the amount due by {amount(supplier.rounding)}; this invoice's is {amount(due)}. "
			"Settle the difference when paying."
		)
	if supplier.prepaid and not same(abs(flt(invoice.get("total_advance"))), supplier.prepaid, precision):
		found.append(
			f"The supplier counts {amount(supplier.prepaid)} as paid already: allocate that advance "
			"(Get Advances Paid) before submitting."
		)
	company_currency = frappe.get_cached_value("Company", invoice.company, "default_currency")
	if supplier.vat_in_aed is not None and invoice.currency != company_currency:
		# In fils, as the e-invoice states it: a site keeping 3 decimals converts to sub-fils.
		vat_in_aed = sign * flt(invoice.base_total_taxes_and_charges)
		if not same(vat_in_aed, supplier.vat_in_aed, 2):
			found.append(
				f"VAT in {company_currency} {amount(vat_in_aed)}, the supplier's {amount(supplier.vat_in_aed)}, "
				f"at the exchange rate {written(invoice.conversion_rate)}."
			)
	return found


def _why_vat_differs(invoice, document, difference: float, net_differs: bool) -> str:
	if net_differs:
		return "It follows from the net total."
	if not any(flt(tax.tax_amount) for tax in invoice.taxes):
		return (
			"This invoice charges no VAT: pick its Purchase Taxes and Charges Template, or set Purchase Taxes "
			"for Standard-Rated Lines in E Invoicing Settings for the next ones."
		)
	categories = {cstr(line.get("tax_category")) for line in document.get("invoice_lines") or []} - {""}
	if len(categories) > 1 and flt(invoice.discount_amount):
		return "The Additional Discount spreads over the lines of every VAT category; the supplier's keeps to its own."
	decimals = invoice.taxes[0].precision("tax_amount") if invoice.taxes else 2
	if decimals > 2 and abs(difference) <= FIL / 2 + 1e-9:
		return f"This site works VAT out to {decimals} decimals; the e-invoice states it in fils."
	if cint(frappe.db.get_single_value("Accounts Settings", "round_row_wise_tax")):
		return "This site rounds VAT line by line (Round Tax Amount Row-wise); the e-invoice rounds it once."
	if abs(difference) <= FIL + 1e-9 and frappe.get_system_settings("rounding_method") == "Banker's Rounding":
		return "Half a fil goes to the even fil on this site (Banker's Rounding); Flick rounds it up."
	return ""


def _category_differences(invoice, document, matched) -> list[str]:
	"""Each VAT category's net on the invoice against the supplier's taxable amount, where the
	invoice mixes categories: the Additional Discount spreads over all of them."""
	categories = {cstr(line.get("tax_category")) for line, _row in matched} - {""}
	if len(categories) < 2:
		return []
	sign = _sign(invoice)
	precision = invoice.precision("net_total")
	nets = {}
	for line, row in matched:
		if row is not None:
			category = cstr(line.get("tax_category"))
			nets[category] = nets.get(category, 0) + sign * flt(row.net_amount)
	found = []
	for entry in document.get("tax_totals") or []:
		category = cstr(entry.get("tax_category"))
		if category in nets and not same(nets[category], flt(entry.get("taxable_amount")), precision):
			found.append(
				f"{codes.TAX_CATEGORIES.get(category, category)} lines come to {money(nets[category], precision)} "
				f"after the discount, the supplier's taxable amount {money(flt(entry.get('taxable_amount')), precision)}."
			)
	return found


# The supplier's VAT


def vat_row_to_fix(invoice, document):
	"""The one VAT row that can take the supplier's VAT, once everything else agrees: a draft whose
	lines are all in one VAT category, whose net total is the supplier's, and whose VAT differs by
	no more than rounding can account for. None otherwise."""
	if invoice.docstatus != 0:
		return None
	supplier = figures(document)
	lines = document.get("invoice_lines") or []
	if supplier.vat is None or len({cstr(line.get("tax_category")) for line in lines}) != 1:
		return None
	sign = _sign(invoice)
	precision = invoice.precision("grand_total")
	difference = sign * flt(invoice.total_taxes_and_charges) - supplier.vat
	if not same(sign * flt(invoice.net_total), supplier.net, precision) or same(difference, 0, precision):
		return None
	# A fil, or half a fil a line where the supplier worked VAT out line by line.
	if abs(difference) > max(FIL, FIL / 2 * len(lines)) + 1e-9:
		return None
	taxed = [tax for tax in invoice.taxes if flt(tax.tax_amount)]
	if len(taxed) != 1:
		return None
	tax = taxed[0]
	if (
		tax.charge_type not in ("On Net Total", "Actual")
		or tax.get("included_in_print_rate")
		or tax.get("add_deduct_tax", "Add") != "Add"
		or tax.get("category") == "Valuation"
	):
		return None
	return tax


def use_supplier_vat(invoice, document):
	"""Charge the VAT the supplier's e-invoice states, as an Actual amount on the VAT row. The
	invoice is not saved."""
	tax = vat_row_to_fix(invoice, document)
	if not tax:
		frappe.throw(
			"Only a draft whose lines and net total already match the supplier's, and whose VAT differs by "
			"rounding alone, takes the supplier's VAT this way."
		)
	tax.charge_type = "Actual"
	tax.tax_amount = _sign(invoice) * figures(document).vat
	invoice.calculate_taxes_and_totals()


def money(value, precision) -> str:
	return fmt_money(flt(value, precision), precision)


def written(value) -> str:
	"""A quantity or rate as written: no trailing zeros, at most six decimals."""
	text = f"{flt(value):,.6f}".rstrip("0").rstrip(".")
	return text if text not in ("", "-0") else "0"
