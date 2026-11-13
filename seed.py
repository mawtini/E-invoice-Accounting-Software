"""Demo master data (customers, suppliers, employees)."""

CUSTOMERS = [
    dict(kind="customer", name="Syarikat Pelanggan Sdn Bhd", tin="C20880050010", id_type="BRN", id_value="201901012345",
         sst_no="W10-1808-32000001", email="ap@pelanggan.my", phone="+60388889999", addr1="No. 8, Jalan Teknologi",
         addr2="Taman Sains Selangor", city="Kota Damansara", postcode="47810", state_code="10"),
    dict(kind="customer", name="Global Widgets Pte Ltd (Singapore)", tin="EI00000000020", id_type="BRN",
         id_value="NA", phone="+6561234567", addr1="1 Raffles Place", city="Singapore", postcode="048616",
         state_code="17", country_code="SGP"),
]
SUPPLIERS = [
    dict(kind="supplier", name="Ahmad bin Ali (freelance designer)", tin="IG11223344050", id_type="NRIC",
         id_value="900101145678", phone="+60123456789", addr1="23 Jalan Melati", city="Shah Alam", postcode="40000",
         state_code="10", msic="74103", activity="Graphic design"),
    dict(kind="supplier", name="Cloud Hosting Inc (USA)", tin="EI00000000030", id_type="BRN", id_value="NA",
         phone="+14155550100", addr1="500 Market St", city="San Francisco", postcode="94105", state_code="17",
         country_code="USA", msic="00000", activity="NOT APPLICABLE"),
    dict(kind="supplier", name="TNB Utility Berhad", tin="C10000000000", id_type="BRN", id_value="199001009294",
         phone="+601300885454", addr1="Jalan Bangsar", city="Kuala Lumpur", postcode="59200", state_code="14",
         msic="35101", activity="Electricity generation"),
]
EMPLOYEES = [
    dict(emp_no="E001", name="Tan Mei Ling", nric="880512-14-5566", residency="citizen", epf_no="12345678",
         socso_no="880512145566", tax_no="IG20011223040", basic_salary=4500, fixed_allowance=300, pcb_monthly=105.50,
         bank_name="Maybank", bank_account="514011223344"),
    dict(emp_no="E002", name="Muhammad Hafiz", nric="950303-10-1123", residency="citizen", epf_no="23456789",
         socso_no="950303101123", tax_no="IG30022334050", basic_salary=7200, fixed_allowance=500, pcb_monthly=480,
         bank_name="CIMB", bank_account="8001234567"),
    dict(emp_no="E003", name="Lim Ah Kow", nric="620815-07-3311", residency="citizen", epf_no="34567890",
         socso_no="620815073311", tax_no="IG40033445060", basic_salary=3000, fixed_allowance=0, pcb_monthly=0,
         bank_name="Public Bank", bank_account="3123456789"),
    dict(emp_no="E004", name="Rahul Kumar (foreign worker)", nric="P1234567", dob="1993-06-01", residency="foreign",
         epf_no="45678901", socso_no="P1234567", basic_salary=2200, fixed_allowance=200, pcb_monthly=0,
         bank_name="RHB", bank_account="21234500001"),
]


def seed_demo(db):
    for p in CUSTOMERS + SUPPLIERS:
        if not db.execute("SELECT 1 FROM parties WHERE name=?", (p["name"],)).fetchone():
            db.execute(f"INSERT INTO parties({', '.join(p)}) VALUES ({', '.join('?' * len(p))})", list(p.values()))
    for e in EMPLOYEES:
        if not db.execute("SELECT 1 FROM employees WHERE emp_no=?", (e["emp_no"],)).fetchone():
            db.execute(f"INSERT INTO employees({', '.join(e)}) VALUES ({', '.join('?' * len(e))})", list(e.values()))
    db.commit()
