"""SQLite persistence layer: schema, connection helper and seed data."""
import os
import sqlite3
from flask import g, current_app

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS parties (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL DEFAULT 'customer',          -- customer | supplier | both
    name TEXT NOT NULL,
    tin TEXT NOT NULL,
    id_type TEXT NOT NULL DEFAULT 'BRN',            -- BRN | NRIC | PASSPORT | ARMY
    id_value TEXT NOT NULL DEFAULT 'NA',
    sst_no TEXT DEFAULT 'NA',
    ttx_no TEXT DEFAULT 'NA',
    msic TEXT DEFAULT '00000',
    activity TEXT DEFAULT 'NOT APPLICABLE',
    email TEXT, phone TEXT DEFAULT 'NA',
    addr1 TEXT DEFAULT 'NA', addr2 TEXT, addr3 TEXT,
    city TEXT DEFAULT 'NA', postcode TEXT, state_code TEXT DEFAULT '17',
    country_code TEXT DEFAULT 'MYS',
    tin_verified INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS accounts (
    code TEXT PRIMARY KEY, name TEXT NOT NULL,
    type TEXT NOT NULL                              -- asset | liability | equity | income | expense
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_type TEXT NOT NULL,                         -- LHDN e-Invoice type code 01..04, 11..14
    number TEXT NOT NULL UNIQUE,
    party_id INTEGER NOT NULL REFERENCES parties(id),
    issue_date TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'MYR',
    exchange_rate REAL NOT NULL DEFAULT 1,
    payment_mode TEXT DEFAULT '03',
    payment_terms TEXT,
    period_start TEXT, period_end TEXT, period_desc TEXT,
    ref_doc_id INTEGER REFERENCES documents(id),    -- original invoice for CN/DN/RN
    expense_id INTEGER REFERENCES expenses(id),     -- source expense for self-billed docs
    is_consolidated INTEGER DEFAULT 0,
    notes TEXT,
    subtotal REAL DEFAULT 0, tax_total REAL DEFAULT 0, total REAL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'draft',           -- draft|submitted|valid|invalid|cancelled|rejected
    paid INTEGER DEFAULT 0,
    lhdn_uuid TEXT, lhdn_submission_uid TEXT, lhdn_long_id TEXT,
    lhdn_validated_at TEXT, lhdn_errors TEXT, submitted_json TEXT,
    posted_entry_id INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS document_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    line_no INTEGER NOT NULL,
    classification TEXT NOT NULL DEFAULT '022',
    description TEXT NOT NULL,
    qty REAL NOT NULL DEFAULT 1,
    unit_code TEXT NOT NULL DEFAULT 'C62',
    unit_price REAL NOT NULL DEFAULT 0,
    discount REAL NOT NULL DEFAULT 0,
    tax_type TEXT NOT NULL DEFAULT '06',
    tax_rate REAL NOT NULL DEFAULT 0,
    exemption_reason TEXT,
    account_code TEXT
);

CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    party_id INTEGER REFERENCES parties(id),
    account_code TEXT NOT NULL,
    description TEXT NOT NULL,
    amount REAL NOT NULL, tax REAL NOT NULL DEFAULT 0, total REAL NOT NULL,
    tax_type TEXT DEFAULT '06', tax_rate REAL DEFAULT 0,
    classification TEXT DEFAULT '022',
    paid INTEGER DEFAULT 1,
    einvoice_source TEXT DEFAULT 'supplier',        -- supplier | self_billed | none
    supplier_einvoice_uuid TEXT,
    supplier_einvoice_status TEXT,
    self_billed_doc_id INTEGER,
    posted_entry_id INTEGER
);

CREATE TABLE IF NOT EXISTS employees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    emp_no TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
    nric TEXT, dob TEXT,
    residency TEXT NOT NULL DEFAULT 'citizen',      -- citizen | pr | foreign
    epf_no TEXT, socso_no TEXT, tax_no TEXT,
    basic_salary REAL NOT NULL DEFAULT 0,
    fixed_allowance REAL NOT NULL DEFAULT 0,
    pcb_monthly REAL NOT NULL DEFAULT 0,
    bank_name TEXT, bank_account TEXT,
    active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS payroll_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period TEXT NOT NULL UNIQUE,                    -- YYYY-MM
    pay_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',           -- draft | posted | paid
    posted_entry_id INTEGER
);

CREATE TABLE IF NOT EXISTS payslips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES payroll_runs(id) ON DELETE CASCADE,
    employee_id INTEGER NOT NULL REFERENCES employees(id),
    basic REAL, allowance REAL, overtime REAL DEFAULT 0, bonus REAL DEFAULT 0,
    other_deduction REAL DEFAULT 0, gross REAL,
    epf_ee REAL, epf_er REAL, socso_ee REAL, socso_er REAL,
    eis_ee REAL, eis_er REAL, pcb REAL, net REAL
);

CREATE TABLE IF NOT EXISTS journal_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL, ref TEXT, description TEXT,
    source_type TEXT, source_id INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS journal_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id INTEGER NOT NULL REFERENCES journal_entries(id) ON DELETE CASCADE,
    account_code TEXT NOT NULL REFERENCES accounts(code),
    debit REAL NOT NULL DEFAULT 0, credit REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS lhdn_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT DEFAULT CURRENT_TIMESTAMP,
    mode TEXT, method TEXT, url TEXT, status INTEGER,
    request TEXT, response TEXT
);

-- B2C sales where the buyer did not request an e-Invoice; aggregated monthly
-- into a consolidated e-Invoice (classification 004, buyer = General Public)
CREATE TABLE IF NOT EXISTS receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL, number TEXT NOT NULL UNIQUE, description TEXT NOT NULL,
    amount REAL NOT NULL, tax_type TEXT DEFAULT '06', tax_rate REAL DEFAULT 0,
    payment_mode TEXT DEFAULT '01', account_code TEXT DEFAULT '4000',
    consolidated_doc_id INTEGER REFERENCES documents(id),
    posted_entry_id INTEGER
);

-- Used only by the offline LHDN simulator (mode = mock)
CREATE TABLE IF NOT EXISTS mock_lhdn_docs (
    uuid TEXT PRIMARY KEY, submission_uid TEXT, long_id TEXT,
    internal_id TEXT, type_code TEXT, status TEXT, errors TEXT,
    raw TEXT, issuer_tin TEXT, receiver_tin TEXT, total REAL,
    received_at TEXT, cancel_reason TEXT
);
"""

DEFAULT_ACCOUNTS = [
    ("1000", "Cash at Bank", "asset"),
    ("1100", "Accounts Receivable", "asset"),
    ("2000", "Accounts Payable", "liability"),
    ("2100", "SST Payable", "liability"),
    ("2200", "Salaries Payable", "liability"),
    ("2210", "EPF Payable", "liability"),
    ("2220", "SOCSO Payable", "liability"),
    ("2230", "EIS Payable", "liability"),
    ("2240", "PCB / MTD Payable", "liability"),
    ("3000", "Owner's Equity", "equity"),
    ("4000", "Sales Revenue", "income"),
    ("4100", "Service Revenue", "income"),
    ("5000", "Cost of Sales", "expense"),
    ("6000", "General Expenses", "expense"),
    ("6010", "Rent", "expense"),
    ("6020", "Utilities", "expense"),
    ("6030", "Professional Fees", "expense"),
    ("6040", "Office Supplies", "expense"),
    ("6050", "Travel & Transport", "expense"),
    ("6060", "Purchases / Imported Services", "expense"),
    ("6100", "Salaries & Wages", "expense"),
    ("6110", "EPF - Employer Contribution", "expense"),
    ("6120", "SOCSO - Employer Contribution", "expense"),
    ("6130", "EIS - Employer Contribution", "expense"),
]

DEFAULT_SETTINGS = {
    "company_name": "Demo Sdn. Bhd.",
    "company_tin": "C25845632020",
    "company_id_type": "BRN",
    "company_brn": "202001234567",
    "company_sst": "NA",
    "company_ttx": "NA",
    "company_msic": "62010",
    "company_activity": "Computer programming activities",
    "company_email": "accounts@demo.com.my",
    "company_phone": "+60312345678",
    "company_addr1": "Lot 66, Bangunan Merdeka",
    "company_addr2": "Persiaran Jaya",
    "company_addr3": "",
    "company_city": "Kuala Lumpur",
    "company_postcode": "50480",
    "company_state": "14",
    "company_country": "MYS",
    "company_bank_account": "",
    "lhdn_mode": "mock",              # mock | sandbox | production
    "lhdn_client_id": "",
    "lhdn_client_secret": "",
    "lhdn_onbehalfof": "",           # intermediary: taxpayer TIN acting on behalf of
    "lhdn_doc_version": "1.0",       # 1.0 (unsigned) | 1.1 (signed)
    "lhdn_cert_path": "",
    "lhdn_cert_password": "",
    "invoice_prefix": "INV-",
    "cn_prefix": "CN-",
    "dn_prefix": "DN-",
    "rn_prefix": "RN-",
    "sb_prefix": "SB-",
}


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def connect(path):
    conn = sqlite3.connect(path, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.executemany("INSERT OR IGNORE INTO accounts(code,name,type) VALUES (?,?,?)", DEFAULT_ACCOUNTS)
    conn.executemany("INSERT OR IGNORE INTO settings(key,value) VALUES (?,?)", DEFAULT_SETTINGS.items())
    # Generic buyer/supplier records prescribed by LHDN
    if not conn.execute("SELECT 1 FROM parties WHERE tin='EI00000000010'").fetchone():
        conn.execute(
            "INSERT INTO parties(kind,name,tin,id_type,id_value) VALUES ('customer','General Public','EI00000000010','BRN','NA')")
    conn.commit()
    conn.close()


def get_settings(db):
    return {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM settings")}


def save_settings(db, values: dict):
    db.executemany("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   list(values.items()))
    db.commit()
