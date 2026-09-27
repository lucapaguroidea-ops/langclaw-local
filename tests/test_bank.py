"""Bank statements (RO): MT940 / CAMT.053 parsing and payment matching."""

from __future__ import annotations

from decimal import Decimal

import pytest

from langclaw.accounting.bank.match import match_payments
from langclaw.accounting.bank.parse import BankStatementError, parse_statement

D = Decimal

MT940 = """:20:STMT260930
:25:RO49AAAA1B31007593840000
:28C:00009/001
:60F:C260901RON1000,00
:61:2609150915C1210,00NTRFNONREF//BT0001
:86:Incasare factura FC-0042 CLIENT SRL RO66BACX0000001234567890
:61:2609200920D326,70NTRFNONREF//BT0002
:86:Plata Furnizor & Co SRL
fact 17
:62F:C260930RON1883,30
"""

CAMT = """<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02">
 <BkToCstmrStmt><Stmt>
  <Id>STMT-09</Id>
  <Acct><Id><IBAN>RO49AAAA1B31007593840000</IBAN></Id><Ccy>RON</Ccy></Acct>
  <Bal><Tp><CdOrPrtry><Cd>OPBD</Cd></CdOrPrtry></Tp><Amt Ccy="RON">1000.00</Amt>
   <CdtDbtInd>CRDT</CdtDbtInd><Dt><Dt>2026-09-01</Dt></Dt></Bal>
  <Bal><Tp><CdOrPrtry><Cd>CLBD</Cd></CdOrPrtry></Tp><Amt Ccy="RON">1883.30</Amt>
   <CdtDbtInd>CRDT</CdtDbtInd><Dt><Dt>2026-09-30</Dt></Dt></Bal>
  <Ntry><Amt Ccy="RON">1210.00</Amt><CdtDbtInd>CRDT</CdtDbtInd>
   <BookgDt><Dt>2026-09-15</Dt></BookgDt><AcctSvcrRef>BT0001</AcctSvcrRef>
   <NtryDtls><TxDtls><RltdPties><Dbtr><Nm>CLIENT SRL</Nm></Dbtr>
    <DbtrAcct><Id><IBAN>RO66BACX0000001234567890</IBAN></Id></DbtrAcct></RltdPties>
    <RmtInf><Ustrd>Incasare factura FC-0042</Ustrd></RmtInf></TxDtls></NtryDtls></Ntry>
  <Ntry><Amt Ccy="RON">326.70</Amt><CdtDbtInd>DBIT</CdtDbtInd>
   <BookgDt><Dt>2026-09-20</Dt></BookgDt><AcctSvcrRef>BT0002</AcctSvcrRef>
   <NtryDtls><TxDtls><RltdPties><Cdtr><Nm>Furnizor &amp; Co SRL</Nm></Cdtr></RltdPties>
    <RmtInf><Ustrd>fact 17</Ustrd></RmtInf></TxDtls></NtryDtls></Ntry>
 </Stmt></BkToCstmrStmt>
</Document>
"""


@pytest.mark.parametrize("data", [MT940, CAMT], ids=["mt940", "camt053"])
def test_both_formats_read_the_same_statement(data: str) -> None:
    s = parse_statement(data.encode())
    assert s.iban == "RO49AAAA1B31007593840000" and s.currency == "RON"
    assert (s.opening, s.closing) == (D("1000.00"), D("1883.30"))
    assert (s.date_from, s.date_to) == ("2026-09-01", "2026-09-30")
    first, second = s.transactions
    assert (first.booked, first.amount, first.reference) == ("2026-09-15", D("1210.00"), "BT0001")
    assert first.iban == "RO66BACX0000001234567890"
    assert "FC-0042" in first.description
    assert second.amount == D("-326.70") and "Furnizor" in (
        second.counterparty + second.description
    )
    assert s.check() == []


def test_a_statement_that_does_not_add_up_is_flagged() -> None:
    s = parse_statement(MT940.replace("1883,30", "1900,00").encode())
    assert any("1883.30" in p for p in s.check())


def test_unknown_files_are_refused() -> None:
    with pytest.raises(BankStatementError, match="MT940 or CAMT.053"):
        parse_statement(b"%PDF-1.4 not a statement")


def _invoice(key, direction, number, gross, cui="", iban="", partner=""):
    return {
        "bucket_key": key,
        "doc_type": "invoice",
        "amount": gross,
        "sender" if direction == "in" else "receiver": partner,
        "fields": {"direction": direction, "invoice_number": number,
                   "supplier_iban": iban if direction == "in" else ""},
    }  # fmt: skip


def test_payments_match_invoices_by_amount_plus_a_second_signal() -> None:
    s = parse_statement(MT940.encode())
    invoices = [
        _invoice("sale", "out", "FC-0042", 1210.00, partner="CLIENT SRL"),
        _invoice("buy", "in", "17", 326.70, partner="Furnizor & Co SRL"),
        _invoice("other", "in", "99", 326.70, partner="Altcineva SRL"),
    ]
    matches = {m.pop("reference"): m for m in match_payments(s.transactions, invoices)}
    assert matches["BT0001"].pop("key") == s.transactions[0].key
    assert matches["BT0001"].pop("allocations") == [{"bucket_key": "sale", "amount": "1210.00"}]
    assert matches["BT0001"] == {
        "bucket_key": "sale", "kind": "certain", "because": "amount + invoice number"
    }  # fmt: skip
    assert matches["BT0002"]["bucket_key"] == "buy" and matches["BT0002"]["kind"] == "certain"


def test_amount_alone_is_only_probable_and_ambiguity_is_left_alone() -> None:
    s = parse_statement(
        MT940.replace("fact 17", "plata").replace("Furnizor & Co SRL", "X").encode()
    )
    one = match_payments(s.transactions[1:], [_invoice("buy", "in", "17", 326.70, partner="Y")])
    assert one[0]["kind"] == "probable" and one[0]["because"] == "amount only"
    two = match_payments(
        s.transactions[1:],
        [_invoice("a", "in", "1", 326.70, partner="Y"), _invoice("b", "in", "2", 326.70)],
    )
    assert two == []
    wrong_side = match_payments(s.transactions[:1], [_invoice("x", "in", "FC-0042", 1210.0)])
    assert wrong_side == []  # money in never pays a supplier invoice


def _tx(amount, description="", counterparty=""):
    from langclaw.accounting.bank.parse import Transaction

    return Transaction("2026-09-20", D(str(amount)), "RON", reference="R1",
                       counterparty=counterparty, description=description)  # fmt: skip


def test_one_payment_for_several_invoices_named_in_the_description() -> None:
    invoices = [
        _invoice("a", "in", "F-101", 100.00, partner="Furnizor SRL"),
        _invoice("b", "in", "F-102", 250.50, partner="Furnizor SRL"),
        _invoice("c", "in", "F-103", 999.00, partner="Furnizor SRL"),
    ]
    (m,) = match_payments([_tx(-350.50, "plata F-101 si F-102")], invoices)
    assert m["kind"] == "certain" and m["because"] == "sum of invoices named"
    assert m["allocations"] == [{"bucket_key": "a", "amount": "100.00"},
                                {"bucket_key": "b", "amount": "250.50"}]  # fmt: skip


def test_one_payment_equal_to_a_unique_set_of_the_partners_invoices() -> None:
    invoices = [
        _invoice("a", "in", "1", 100.00, partner="Furnizor SRL"),
        _invoice("b", "in", "2", 200.00, partner="Furnizor SRL"),
        _invoice("c", "in", "3", 450.00, partner="Furnizor SRL"),
        _invoice("x", "in", "4", 300.00, partner="Altul SRL"),
    ]
    (m,) = match_payments([_tx(-550, "plata", counterparty="FURNIZOR SRL")], invoices)
    assert {a["bucket_key"] for a in m["allocations"]} == {"a", "c"}
    assert m["because"] == "sum of the partner's open invoices"
    ambiguous = [*invoices[:3], _invoice("d", "in", "5", 150.00, partner="Furnizor SRL"),
                 _invoice("e", "in", "6", 150.00, partner="Furnizor SRL")]  # fmt: skip
    # a+b and d+e both make 300 → leave it to a person
    assert match_payments([_tx(-300, "plata", counterparty="Furnizor SRL")], ambiguous) == []


def test_a_partial_payment_needs_the_invoice_number_and_counts_the_outstanding() -> None:
    inv = _invoice("a", "out", "FC-7", 1000.00, partner="Client SRL")
    (m,) = match_payments([_tx(400, "avans FC-7")], [inv])
    assert m["kind"] == "partial" and m["allocations"] == [{"bucket_key": "a", "amount": "400.00"}]
    assert match_payments([_tx(400, "avans")], [inv]) == []  # no number → no guess
    inv["fields"]["paid_amount"] = "400.00"
    (rest,) = match_payments([_tx(600, "rest FC-7")], [inv])
    assert rest["kind"] == "certain" and rest["allocations"][0]["amount"] == "600.00"
