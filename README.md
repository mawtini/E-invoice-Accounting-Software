# e-Invois Accounting (prototype)

A small accounting app for Malaysian SMEs that works with **LHDN MyInvois**. It covers:

| Area | What it does |
|---|---|
| **Sales e-Invoices** | Invoice, credit, debit and refund notes (types 01–04). You can submit one at a time or in batches of up to 100. Status is polled from LHDN. Documents can be cancelled within 72 hours. The printable invoice carries the QR validation link. |
| **B2C receipts** | Cash sales to buyers who did not ask for an e-Invoice. Each month's receipts roll into one **consolidated e-Invoice** (classification 004, buyer TIN `EI00000000010`). |
| **Expenditure** | Records expenses and posts them to the ledger. A supplier's e-Invoice UUID can be checked on MyInvois. Received e-Invoices can be imported as expenses. Self-billed e-Invoices (types 11–14) are supported for foreign suppliers, individuals and agents. |
| **Payroll** | Calculates EPF, SOCSO, EIS and PCB. Handles citizen, PR and foreign workers and age 60+. Produces payslips, EPF/SOCSO/PCB/bank CSV exports and statutory payment entries. Payroll is not e-Invoiced, because employment income is exempt. |
| **Accounts** | Double-entry ledger with a chart of accounts, journal, trial balance, P&L, and the SST payable figure for the period. |

## Run

```bash
pip install -r requirements.txt
python app.py                # http://127.0.0.1:5000
python -m pytest -q          # 23 tests
```

The app starts in **mock mode**, an offline MyInvois simulator, so you can try every flow without credentials.
Click **"Load demo master data"** on the dashboard to add sample customers, suppliers and employees.

## Connecting to the real LHDN MyInvois

1. In the MyInvois portal (preprod for testing), register an ERP system to get a **Client ID / Client Secret**.
2. Open **Settings → LHDN MyInvois API**. Choose *Sandbox*, then enter the ID and secret.
   Intermediaries should also fill in *On behalf of TIN* (sent as the `onbehalfof` header).
3. Go to **MyInvois → Test login**. Then validate a buyer's TIN and submit an invoice.
4. For **document version 1.1** (digitally signed), upload the company's `.p12` certificate from an
   LHDN-recognised CA and set the version to 1.1.

## Architecture

```
app.py              Flask routes / UI
services.py         e-Invoice lifecycle: build → validate → sign → submit → poll → post / cancel
accounting.py       posting rules + balances
payroll.py          EPF / SOCSO / EIS calculations
lhdn/client.py      MyInvois REST client (OAuth2 client-credentials, token cache, 401/429 retry)
lhdn/mock.py        offline simulator with the same interface and response shapes
lhdn/ubl.py         UBL 2.1 JSON builder + local validator (mandatory fields, TIN format, totals, 72h rule)
lhdn/signer.py      v1.1 signature (SDK "Signature Creation" steps 1-8) + verifier
lhdn/codes.py       LHDN code tables (doc types, tax types, classifications, states, units, payment modes)
db.py / seed.py     SQLite schema, defaults, demo data
```

API endpoints used: `/connect/token`, `taxpayer/validate`, `taxpayer/search/tin`, `documentsubmissions` (POST/GET),
`documents/{uuid}/raw`, `documents/{uuid}/details`, `documents/state/{uuid}/state` (cancel/reject),
`documents/recent`, `documents/search`.

### Ledger posting rules
* Sales invoice / debit note: posted **when LHDN validates it**. Dr Accounts Receivable, Cr Revenue, Cr SST Payable.
  Credit and refund notes post the reverse. A cancelled document is reversed automatically.
* Receipts post when they are recorded. The consolidated e-Invoice and self-billed documents do **not** post again.
* Expense: Dr Expense, Cr Bank or Accounts Payable.
* Payroll: Dr Salaries and employer EPF/SOCSO/EIS. Cr Net salary, EPF, SOCSO, EIS and PCB payables.
  These payables are cleared by the "Pay …" buttons.

## Known limitations / next steps
* **v1.1 signature**: tested only against the built-in verifier with a self-signed certificate. It must be checked
  against the LHDN sandbox with a real certificate. Two areas are most likely to need adjustment:
  the canonical JSON used for the SignedProperties digest, and the issuer-name format.
* **PCB/MTD** is entered per employee and is not calculated. EPF/SOCSO/EIS follow the published contribution
  tables, but they are approximated by formula, so check edge bands against the official schedules.
* The CSV exports are close to Borang A / 8A / CP39, but they are not the exact upload formats used by the
  i-Akaun, ASSIST or e-PCB portals.
* There is no user login, roles or audit trail yet. The client secret is stored in plain text in SQLite.
  Use a secrets store before going to production.
* Not yet built: foreign-currency revaluation, multiple tax rates per line, document-level discounts and charges,
  submitting in XML format, and rejecting a received document (the client method exists but has no UI).
