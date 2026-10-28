"""Code lists for PINT-AE as accepted by Flick's UAE Regional API.

Sources: Flick UAE developer guide and OpenAPI 1.1, and the PINT-AE billing specification
(docs.peppol.eu/poac/ae/v1.0.1), snapshots of 2026-10-28. Never add a value here that does
not come from Flick or the specification: a plausible wrong code passes our checks and is
rejected, or worse accepted, by the FTA.

Select fields store "<code> - <label>"; `code_of()` turns either form back into the code.
"""

EMIRATE_CODES = {
	"Abu Dhabi": "AUH",
	"Dubai": "DXB",
	"Sharjah": "SHJ",
	"Ajman": "AJM",
	"Umm Al Quwain": "UAQ",
	"Ras Al Khaimah": "RAK",
	"Fujairah": "FUJ",
}

TAX_CATEGORIES = {
	"S": "Standard rated (5%)",
	"Z": "Zero rated",
	"E": "Exempt",
	"O": "Outside scope of tax",
	"AE": "Reverse charge",
	"N": "Profit margin scheme",
}
STANDARD_RATE = 5
# Categories whose lines alone make a document out of scope (ibr-151-ae).
EXEMPT_OR_OUT_OF_SCOPE = {"E", "O"}
# Categories that keep the 5% rate on the line but carry no VAT for the seller to charge:
# reverse charge is accounted for by the buyer (Flick renders AE at 5% with 0.00 tax).
RATE_WITHOUT_TAX = {"AE"}
MARGIN = "N"  # Flick rejects margin-scheme documents in every combination today (known defect)

EXEMPTION_REASONS = {
	"DL8.46.1": "Certain financial services",
	"DL8.46.2": "Residential units (lease or sale)",
	"DL8.46.3": "Bare land",
	"DL8.46.4": "Local passenger transport",
}

REVERSE_CHARGE_ITEM_TYPES = {
	"DL8.48.8.1": "Gold and diamonds",
	"DL8.48.8.2": "Electronic devices",
	"DL8.48.3.1": "Crude or refined oil",
	"DL8.48.3.2": "Natural gas",
	"DL8.48.3.3": "Pure hydrocarbons",
}

CREDIT_NOTE_REASONS = {
	"DL8.61.1.A": "Supply cancelled",
	"DL8.61.1.B": "Tax treatment changed",
	"DL8.61.1.C": "Consideration altered",
	"DL8.61.1.D": "Goods or services returned",
	"DL8.61.1.E": "Tax charged in error",
	"VD": "Volume discount",
}
# The one reason that needs no reference to the original invoice (ibr-055-ae).
REASON_WITHOUT_REFERENCE = "VD"

PAYMENT_MEANS = {
	"1": "Instrument not defined",
	"10": "Cash",
	"20": "Cheque",
	"21": "Banker's draft",
	"30": "Credit transfer",
	"49": "Direct debit",
	"54": "Credit card",
	"55": "Debit card",
	"68": "Online payment service",
}
# Credit transfer needs the account the buyer pays into (ibr-192-ae).
PAYMENT_MEANS_NEEDING_ACCOUNT = {"30"}
DIRECT_DEBIT = "49"

# Allowance reasons are UNCL 5189 and charge reasons UNCL 7161; Flick checks each against its
# own list. Discounts go as 95; charge codes are entered per account in settings (e.g. FC).
DISCOUNT_REASON_CODE = "95"

INCOTERMS = ("EXW", "FCA", "FOB", "CIF", "DAP", "DDP")

COMMODITY_GOODS, COMMODITY_SERVICES, COMMODITY_BOTH = "G", "S", "B"

# Document types (UNTDID 1001, PINT-AE subset).
INVOICE = "380"
INVOICE_OUT_OF_SCOPE = "480"
CREDIT_NOTE = "381"
CREDIT_NOTE_OUT_OF_SCOPE = "81"
SELF_BILLED_INVOICE = "389"
SELF_BILLED_CREDIT_NOTE = "261"
CREDIT_NOTE_TYPES = {CREDIT_NOTE, CREDIT_NOTE_OUT_OF_SCOPE, SELF_BILLED_CREDIT_NOTE}

FLAG_KEYS = (
	"is_selfbilled",
	"is_ftz",
	"is_deemed",
	"is_margin",
	"is_summary",
	"is_continuous",
	"is_dab",
	"is_ecommerce",
	"is_export",
)

# Predefined recipients (PINT-AE §1.5.3): the document is reported to the FTA only, not
# exchanged, so no real buyer endpoint exists.
DEEMED_SUPPLY_ENDPOINT = "0235:9900000097"
NOT_SUBJECT_ENDPOINT = "0235:9900000098"
EXPORT_NOT_ON_PEPPOL_ENDPOINT = "0235:9900000099"

ROUTING_PEPPOL = "Peppol ID"
ROUTING_EXPORT = "Export buyer not on Peppol"
ROUTING_NOT_SUBJECT = "Not subject to UAE e-invoicing"
ROUTING_ENDPOINTS = {ROUTING_EXPORT: EXPORT_NOT_ON_PEPPOL_ENDPOINT, ROUTING_NOT_SUBJECT: NOT_SUBJECT_ENDPOINT}

# Marks a supporting document reference as an invoiced object (UNCL 1001 code 130).
INVOICED_OBJECT = "130"

# Item barcode types whose values are GTINs (scheme 0160), needed on reverse-charge lines.
# (ERPNext v15 barcode types; JAN is an EAN-13 and UPC a GTIN-12.)
GTIN_BARCODE_TYPES = {"EAN", "EAN-8", "EAN-12", "EAN-13", "GTIN", "JAN", "UPC", "UPC-A", "UPC-E"}
GTIN_SCHEME = "0160"

ATTACHMENT_LIMIT_BYTES = 20 * 1024 * 1024  # Flick's ceiling per embedded attachment

PEPPOL_ID_PATTERN = r"^0235:1\d{9}$"
TRN_PATTERN = r"^\d{15}$"


def options(codes: dict, blank: bool = True) -> str:
	"""Select options for a code list, in the "<code> - <label>" form."""
	values = [f"{code} - {label}" for code, label in codes.items()]
	return "\n".join(([""] if blank else []) + values)


def code_of(value: str | None) -> str | None:
	"""The code part of a stored Select value ("S - Standard rated (5%)" -> "S")."""
	if not value:
		return None
	return value.split(" - ", 1)[0].strip()
