"""Code tables published in the LHDN MyInvois SDK (sdk.myinvois.hasil.gov.my/codes)."""

DOC_TYPES = {
    "01": "Invoice",
    "02": "Credit Note",
    "03": "Debit Note",
    "04": "Refund Note",
    "11": "Self-billed Invoice",
    "12": "Self-billed Credit Note",
    "13": "Self-billed Debit Note",
    "14": "Self-billed Refund Note",
}
SELF_BILLED_TYPES = {"11", "12", "13", "14"}
ADJUSTMENT_TYPES = {"02", "03", "04", "12", "13", "14"}   # must reference an original document
REDUCING_TYPES = {"02", "04", "12", "14"}                 # reduce the original amount

TAX_TYPES = {
    "01": "Sales Tax",
    "02": "Service Tax",
    "03": "Tourism Tax",
    "04": "High-Value Goods Tax",
    "05": "Sales Tax on Low Value Goods",
    "06": "Not Applicable",
    "E": "Tax exemption (where applicable)",
}

PAYMENT_MODES = {
    "01": "Cash", "02": "Cheque", "03": "Bank Transfer", "04": "Credit Card",
    "05": "Debit Card", "06": "e-Wallet / Digital Wallet", "07": "Digital Bank", "08": "Others",
}

STATES = {
    "01": "Johor", "02": "Kedah", "03": "Kelantan", "04": "Melaka", "05": "Negeri Sembilan",
    "06": "Pahang", "07": "Pulau Pinang", "08": "Perak", "09": "Perlis", "10": "Selangor",
    "11": "Terengganu", "12": "Sabah", "13": "Sarawak", "14": "Wilayah Persekutuan Kuala Lumpur",
    "15": "Wilayah Persekutuan Labuan", "16": "Wilayah Persekutuan Putrajaya", "17": "Not Applicable",
}

ID_TYPES = {"BRN": "Business Registration No.", "NRIC": "MyKad / MyTentera (NRIC)",
            "PASSPORT": "Passport No.", "ARMY": "Army No."}

UNITS = {
    "C62": "Unit (one)", "H87": "Piece", "EA": "Each", "SET": "Set", "HUR": "Hour", "DAY": "Day",
    "MON": "Month", "ANN": "Year", "KGM": "Kilogram", "LTR": "Litre", "MTR": "Metre", "XBX": "Box",
}

CLASSIFICATIONS = {
    "001": "Breastfeeding equipment", "002": "Child care centres and kindergartens fees",
    "003": "Computer, smartphone or tablet", "004": "Consolidated e-Invoice",
    "005": "Construction materials", "006": "Disbursement", "007": "Donation",
    "008": "e-Commerce - e-Invoice to buyer / purchaser",
    "009": "e-Commerce - Self-billed e-Invoice to seller, logistics, etc.",
    "010": "Education fees", "011": "Goods on consignment (Consignor)",
    "012": "Goods on consignment (Consignee)", "013": "Gym membership",
    "014": "Insurance - Education and medical benefits", "015": "Insurance - Takaful or life insurance",
    "016": "Interest and financing expenses", "017": "Internet subscription", "018": "Land and building",
    "019": "Medical examination for learning disabilities and early intervention or rehabilitation",
    "020": "Medical examination or vaccination expenses", "021": "Medical expenses for serious diseases",
    "022": "Others", "023": "Petroleum operations",
    "024": "Private retirement scheme or deferred annuity scheme", "025": "Motor vehicle",
    "026": "Subscription of books / journals / magazines / newspapers / other similar publications",
    "027": "Reimbursement", "028": "Rental of motor vehicle",
    "029": "EV charging facilities (Installation, rental, sale / purchase or subscription fees)",
    "030": "Repair and maintenance", "031": "Research and development", "032": "Foreign income",
    "033": "Self-billed - Betting and gaming", "034": "Self-billed - Importation of goods",
    "035": "Self-billed - Importation of services", "036": "Self-billed - Others",
    "037": "Self-billed - Monetary payment to agents, dealers or distributors",
    "038": "Sports equipment, rental / entry fees for sports facilities, registration in sports competition or sports training fees",
    "039": "Supporting equipment for disabled person",
    "040": "Voluntary contribution to approved provident fund", "041": "Dental examination or treatment",
    "042": "Fertility treatment",
    "043": "Treatment and home care nursing, daycare centres and residential care centers",
    "044": "Vouchers, gift cards, loyalty points, etc",
    "045": "Self-billed - Non-monetary payment to agents, dealers or distributors",
}

CURRENCIES = ["MYR", "USD", "SGD", "EUR", "GBP", "CNY", "JPY", "AUD", "THB", "IDR", "HKD"]

# Generic TINs prescribed by LHDN
GENERIC_TIN = {
    "EI00000000010": "General Public",
    "EI00000000020": "Foreign Buyer",
    "EI00000000030": "Foreign Supplier",
    "EI00000000040": "Government / Statutory bodies",
}
