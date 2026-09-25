# QuickBooks Online (read-only)

The prototype supports two modes:

| `QBO_MODE` | What it is | Internet |
|---|---|---|
| `mock` (default) | A bundled, fictional sandbox company (`data/qbo_sandbox/sandbox_company.json`) in the exact QuickBooks API response format. The demo and tests use it. | No |
| `sandbox` | A live **Intuit sandbox company** via the QuickBooks Online Accounting API and OAuth 2.0 | Yes (Intuit only) |

Production companies are refused unless `QBO_ALLOW_PRODUCTION=true`. That's out of scope for this stage.

## How "read-only" is enforced

Intuit's accounting permission (`com.intuit.quickbooks.accounting`) allows reading **and** writing;
there is no read-only scope. So the restriction is enforced in the client code
(`app/integrations/quickbooks.py`):

- it only sends HTTP **GET** requests to the `/query` endpoint; there is no code path that creates,
  updates or deletes;
- every query must match `SELECT * FROM <Entity>` over an allowlist (CompanyInfo, Vendor, Customer,
  Bill, Invoice, Account, Purchase, Payment, BillPayment); anything else is rejected;
- tests assert that writes are impossible and that only GETs reach the API.

Data is copied into local tables (`qbo_vendors`, `qbo_bills`, `qbo_invoices`, `qbo_accounts`,
`qbo_customers`, `qbo_company`), so asking questions never calls Intuit.

## Connecting an Intuit sandbox company

1. Create a free account at <https://developer.intuit.com>, then **Create an app** →
   *QuickBooks Online and Payments*. Intuit automatically provides a sandbox company with test data.
2. In the app's **Keys & credentials** (Development/Sandbox), copy the **Client ID** and **Client
   Secret**, and add the redirect URI `http://localhost:8000/qbo/callback`.
3. In `.env`:

   ```
   QBO_MODE=sandbox
   QBO_CLIENT_ID=...
   QBO_CLIENT_SECRET=...
   QBO_REDIRECT_URI=http://localhost:8000/qbo/callback
   # optional: keep tokens in the macOS Keychain instead of a 0600 file
   # SECRETS_BACKEND=keyring      (pip install keyring)
   ```

4. `make run`, open **QuickBooks → Connect sandbox company**, sign in to Intuit and pick the sandbox
   company. You return to the app connected.
5. **Sync now** pulls the data; reconciliation runs automatically against the invoices on file.

**Disconnect & revoke** revokes the refresh token at Intuit, deletes the stored token and deletes
the local QuickBooks tables. Every connect, sync and disconnect is written to the activity log.

## What leaves the Mac in sandbox mode

- The OAuth sign-in (in your browser, directly with Intuit).
- Read-only query requests (`SELECT * FROM Bill`, …) to `sandbox-quickbooks.api.intuit.com`.

Accounting data is downloaded; no documents, invoices or questions are sent to Intuit.

## Notes for a later production stage

- Production access needs an Intuit production app (Intuit reviews apps before issuing production keys)
  and a named admin user on the company to authorise it.
- Check Intuit's current API terms and pricing (the Intuit App Partner Program meters some API usage
  for production apps) before going live.
- Writing to QuickBooks (e.g. recording approved bills) would be a separate, explicitly approved stage.
  It would execute only items approved in the Approvals queue.
