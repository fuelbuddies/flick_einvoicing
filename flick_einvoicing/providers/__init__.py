"""The seam between payload and transport. Flick is the only provider."""

import frappe

from flick_einvoicing.providers.flick import FlickClient


def get_client(settings) -> FlickClient:
	if settings.provider == "Flick":
		return FlickClient.from_settings(settings)
	frappe.throw(f"Unknown e-invoicing provider: {settings.provider}")
