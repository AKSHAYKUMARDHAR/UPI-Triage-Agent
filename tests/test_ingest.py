"""Statement import on made-up exports in each bank's layout (never real statements)."""
import io
from datetime import datetime
from pathlib import Path

import pytest

from ingest import StatementError, parse_statement

ROOT = Path(__file__).resolve().parent.parent

HDFC_CSV = """HDFC BANK Ltd.,,,,,,
Statement of account,,,,,,
Account No :,50100123456789,,,,,
,,,,,,
Date,Narration,Chq./Ref.No.,Value Dt,Withdrawal Amt.,Deposit Amt.,Closing Balance
01/09/26,UPI-SWIGGY-SWIGGY8@YBL-YESB0YBLUPI-412345678901-PAYMENT FROM PH,0000412345678901,01/09/26,"1,234.50",,"98,765.50"
02/09/26,NEFT CR-ICIC0000123-ACME TECHNOLOGIES PVT LTD-SALARY SEP,0000N123456789,02/09/26,,"1,20,000.00","2,18,765.50"
03/09/26,ATW-438628XXXXXX1234-S1ANBL465-BANGALORE,0000004321,03/09/26,"5,000.00",,"2,13,765.50"
,,,,,,
STATEMENT SUMMARY :-,,,,,,
Opening Balance,Dr Count,Cr Count,Debits,Credits,Closing Bal,
"1,00,000.00",2,1,"6,234.50","1,20,000.00","2,13,765.50",
"""

SBI_TSV = ("Account Name\t:\tMR TEST USER\nAddress\t:\tBANGALORE\n\n"
           "Txn Date\tValue Date\tDescription\tRef No./Cheque No.\tDebit\tCredit\tBalance\n"
           "1 Sep 2026\t1 Sep 2026\tTO TRANSFER-UPI/DR/412345678901/SWIGGY/YESB/swiggy8@ybl/Paymen--\t"
           "TRANSFER TO 4897\t250.00\t \t9,750.00\n"
           "2 Sep 2026\t2 Sep 2026\tBY TRANSFER-NEFT*HDFC0000123*N123456789*RAHUL SHARMA--\t"
           "TRANSFER FROM 1234\t \t1,000.00\t10,750.00\n")

KOTAK_CSV = """Statement for account 1234567890
Sl. No.,Transaction Date,Value Date,Description,Chq / Ref No.,Amount,Dr / Cr,Balance,Dr / Cr
1,05-09-2026,05-09-2026,UPI/RAHUL SHARMA/412345678901/rent sept,UPI-412345678901,"18,000.00",DR,"32,000.00",CR
2,06-09-2026,06-09-2026,IMPS/P2A/412345678902/TEST USER/HDFC/self,IMPS-412345678902,"5,000.00",CR,"37,000.00",CR
"""

AXIS_CSV = """Statement of Account No - 912010012345678
Tran Date,CHQNO,PARTICULARS,DR,CR,BAL,SOL
07-09-2026,,UPI/P2M/412345678903/ZOMATO LIMITED/Pay/HDFC BANK,450.00,,"12,000.00",1234
08-09-2026,,POS 438628XXXXXX1234 AMAZON PAY INDIA,"1,299.00",,"10,701.00",1234
"""


def test_hdfc_csv_with_title_rows_and_footer():
    rows = parse_statement(HDFC_CSV.encode(), "Acct_Statement_XX1234.csv")
    assert [r["date"] for r in rows] == ["2026-09-01", "2026-09-02", "2026-09-03"]
    assert rows[0]["debit"] == 1234.50 and rows[0]["credit"] is None
    assert rows[1]["credit"] == 120000.00 and rows[1]["debit"] is None   # Indian digit grouping
    assert rows[0]["statement_id"] == "Acct_Statement_XX1234"
    assert all(r["txn_id"].startswith("X") for r in rows)                  # the summary block is not a row


def test_sbi_tab_separated_file_named_xls():
    rows = parse_statement(SBI_TSV.encode(), "statement.xls")
    assert len(rows) == 2 and rows[0]["date"] == "2026-09-01"
    assert rows[0]["debit"] == 250.0 and rows[1]["credit"] == 1000.0


def test_kotak_single_amount_column_with_dr_cr_flag():
    rows = parse_statement(KOTAK_CSV.encode(), "kotak.csv")
    assert (rows[0]["debit"], rows[0]["credit"]) == (18000.0, None)
    assert (rows[1]["debit"], rows[1]["credit"]) == (None, 5000.0)


def test_axis_dr_cr_columns():
    rows = parse_statement(AXIS_CSV.encode(), "axis.csv")
    assert [r["debit"] for r in rows] == [450.0, 1299.0]


def test_xlsx_with_real_dates_and_a_wrapped_narration(tmp_path):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["ICICI Bank: detailed statement"])
    ws.append([])
    ws.append(["S No.", "Value Date", "Transaction Date", "Cheque Number", "Transaction Remarks",
               "Withdrawal Amount (INR )", "Deposit Amount (INR )", "Balance (INR )"])
    ws.append([1, datetime(2026, 9, 10), datetime(2026, 9, 10), None, "BIL/ONL/000123456/BESCOM", 2100.0, 0, 5000.0])
    ws.append([2, datetime(2026, 9, 11), datetime(2026, 9, 11), None, "MMT/IMPS/412345678904/RAHUL", 500.0, 0, 4500.0])
    ws.append([None, None, None, None, "SHARMA/HDFC Bank", None, None, None])  # narration wrapped to a 2nd line
    buf = io.BytesIO()
    wb.save(buf)
    rows = parse_statement(buf.getvalue(), "OpTransactionHistory.xlsx")
    assert [r["date"] for r in rows] == ["2026-09-10", "2026-09-11"]
    assert rows[1]["narration"] == "MMT/IMPS/412345678904/RAHUL SHARMA/HDFC Bank"


def test_html_table_saved_as_xls():
    html = ("<html><body><table><tr><td>Date</td><td>Description</td><td>Debit</td><td>Credit</td></tr>"
            "<tr><td>12/09/2026</td><td>UPI/DR/1/SWIGGY/YESB/swiggy8@ybl/x</td><td>99.00</td><td></td></tr>"
            "</table></body></html>")
    rows = parse_statement(html.encode(), "statement.xls")
    assert rows[0]["debit"] == 99.0 and rows[0]["date"] == "2026-09-12"


def test_pipeline_csv_keeps_its_txn_ids():
    data = (ROOT / "data" / "demo_statement.csv").read_bytes()
    rows = parse_statement(data, "demo_statement.csv")
    assert rows[0]["txn_id"] == "D001" and rows[0]["statement_id"] == "DEMO" and len(rows) == 16


def test_ids_are_stable_and_duplicates_stay_distinct():
    dup = HDFC_CSV.replace("02/09/26,NEFT", "01/09/26,UPI-SWIGGY-SWIGGY8@YBL-YESB0YBLUPI-412345678901-PAYMENT FROM "
                                             "PH,0000412345678901,01/09/26,\"1,234.50\",,\"1.00\"\n02/09/26,NEFT")
    first, again = parse_statement(dup.encode(), "a.csv"), parse_statement(dup.encode(), "a.csv")
    assert [r["txn_id"] for r in first] == [r["txn_id"] for r in again]
    assert len({r["txn_id"] for r in first}) == len(first) == 4


@pytest.mark.parametrize("data, message", [
    (b"%PDF-1.7 ...", "PDF statements are not supported"),
    (b"name,age\nrahul,30\n", "could not find the transaction table"),
    (b"", "empty"),
])
def test_unreadable_files_say_why(data, message):
    with pytest.raises(StatementError, match=message):
        parse_statement(data, "x.csv")
