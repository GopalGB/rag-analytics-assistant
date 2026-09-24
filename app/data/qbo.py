"""Read-only QuickBooks Online sandbox boundary."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

SANDBOX_BASE = "https://sandbox-quickbooks.api.intuit.com"


class QBOSandboxClient:
    def __init__(
        self,
        access_token: str | None = None,
        realm_id: str | None = None,
        transport: Callable[..., Any] | None = None,
    ):
        self.access_token = access_token
        self.realm_id = realm_id
        self.transport = transport

    @property
    def status(self) -> str:
        return "configured" if self.access_token and self.realm_id else "not_configured"

    def list_invoices(self) -> list[dict[str, Any]]:
        return self._query("Invoice")

    def list_vendors(self) -> list[dict[str, Any]]:
        return self._query("Vendor")

    def _query(self, entity: str) -> list[dict[str, Any]]:
        if self.status != "configured":
            return []
        query = f"SELECT * FROM {entity} MAXRESULTS 50"
        url = f"{SANDBOX_BASE}/v3/company/{self.realm_id}/query"
        transport = self.transport
        if transport is None:
            import requests

            transport = requests.get
        response = transport(
            url,
            headers={"Authorization": f"Bearer {self.access_token}", "Accept": "application/json"},
            params={"query": query},
            timeout=10,
        )
        response.raise_for_status()
        return response.json().get("QueryResponse", {}).get(entity, [])
