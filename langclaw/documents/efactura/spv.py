"""
SPV clients — where e-Factura invoices come from.

:class:`AnafSpvClient` talks to ANAF's e-Factura REST API (``listaMesajeFactura``
to list, ``descarcare`` to download the signed zip). It needs an OAuth access
token, which ANAF only issues to a holder of a qualified digital certificate.

:class:`DemoSpvClient` serves realistic dummy invoices (CIUS-RO, current VAT rates)
in the same shapes, so everything downstream can be built and tested before the
certificate exists. Pick one with ``documents.efactura.mode``.
"""

from __future__ import annotations

import hashlib
import io
import random
import re
import zipfile
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

import httpx

from langclaw.documents.efactura.samples import Party, make_invoice

_KINDS = {
    "FACTURA PRIMITA": "received",
    "FACTURA TRIMISA": "sent",
    "ERORI FACTURA": "error",
}
#: ANAF lists at most this many days back.
MAX_DAYS = 60


class SpvError(RuntimeError):
    """An SPV call failed (no rights for the CIF, bad token, unreachable...)."""


@dataclass(slots=True, frozen=True)
class SpvMessage:
    id: str
    """Download id (``id_descarcare``)."""
    kind: str
    """``received`` | ``sent`` | ``error`` | ``buyer_message``."""
    cif: str
    created: str
    """ISO timestamp, minute precision."""
    details: str = ""
    upload_id: str = ""


class SpvClient(Protocol):
    async def list_messages(self, cif: str, days: int) -> list[SpvMessage]: ...

    async def download(self, message_id: str) -> bytes: ...


def cif_digits(cif: str) -> str:
    """``RO12345678`` → ``12345678`` (SPV wants the bare number)."""
    return re.sub(r"\D", "", cif or "")


def unzip_invoice(archive: bytes) -> bytes:
    """The invoice XML from an SPV zip (which also holds ``semnatura_<id>.xml``).

    Raises:
        SpvError: not a zip, or no invoice inside.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            for name in zf.namelist():
                if name.endswith(".xml") and not name.startswith("semnatura_"):
                    return zf.read(name)
    except zipfile.BadZipFile as exc:
        raise SpvError(f"The SPV download is not a zip: {exc}") from None
    raise SpvError("The SPV zip holds no invoice XML.")


# -- ANAF ---------------------------------------------------------------------------------


class AnafSpvClient:
    """ANAF's e-Factura API.

    Args:
        token: OAuth2 access token (from ``logincert.anaf.ro``, issued to the
            holder of a qualified certificate with SPV rights for the CIFs).
        environment: ``prod`` or ``test`` (ANAF's test endpoints).
        transport: An ``httpx`` transport (tests inject a mock).
    """

    def __init__(
        self, token: str, *, environment: str = "prod", transport: Any | None = None
    ) -> None:
        if not token:
            raise SpvError(
                "The ANAF e-Factura API needs an OAuth token: set "
                "LANGCLAW__DOCUMENTS__EFACTURA__TOKEN (or use mode=demo until then)."
            )
        self._http = httpx.AsyncClient(
            base_url=f"https://api.anaf.ro/{environment}/FCTEL/rest/",
            headers={"Authorization": f"Bearer {token}"},
            timeout=60.0,
            transport=transport,
        )

    async def list_messages(self, cif: str, days: int) -> list[SpvMessage]:
        body = await self._get_json(
            "listaMesajeFactura", {"zile": max(1, min(days, MAX_DAYS)), "cif": cif_digits(cif)}
        )
        if body.get("eroare"):
            if "nu exista mesaje" in body["eroare"].lower():
                return []
            raise SpvError(f"SPV: {body['eroare']}")
        return [
            SpvMessage(
                id=str(m.get("id", "")),
                kind=_KINDS.get(str(m.get("tip", "")).upper(), "buyer_message"),
                cif=str(m.get("cif", "")),
                created=_created(str(m.get("data_creare", ""))),
                details=str(m.get("detalii", "")),
                upload_id=str(m.get("id_solicitare", "")),
            )
            for m in body.get("mesaje", [])
        ]

    async def download(self, message_id: str) -> bytes:
        resp = await self._get("descarcare", {"id": message_id})
        if "json" in resp.headers.get("content-type", ""):
            raise SpvError(f"SPV: {resp.json().get('eroare', resp.text)}")
        return resp.content

    async def _get(self, path: str, params: dict[str, Any]) -> httpx.Response:
        try:
            resp = await self._http.get(path, params=params)
        except httpx.HTTPError as exc:
            raise SpvError(f"Cannot reach ANAF: {exc}") from exc
        if resp.status_code in (401, 403):
            raise SpvError("ANAF rejected the token (expired or without SPV rights).")
        if resp.status_code >= 400:
            raise SpvError(f"ANAF answered HTTP {resp.status_code}: {resp.text[:200]}")
        return resp

    async def _get_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        resp = await self._get(path, params)
        try:
            return resp.json()
        except ValueError as exc:
            raise SpvError(f"ANAF sent something that isn't JSON: {resp.text[:200]}") from exc


def _created(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y%m%d%H%M").strftime("%Y-%m-%dT%H:%M")
    except ValueError:
        return value


# -- demo ---------------------------------------------------------------------------------

_SUPPLIERS = [
    (
        Party("ENEL ENERGIE SA", "RO22000460", "J40/1234/2007", "RO49BACX0000001234567890"),
        [("Energie electrica activa", 1, 412.35, 21), ("Abonament", 1, 18.50, 21)],
    ),
    (
        Party("ORANGE ROMANIA SA", "RO9010105", "J40/10178/1996", "RO57BRDE450SV01234567890"),
        [("Abonament Business 20", 3, 39.90, 21), ("Trafic date suplimentar", 1, 25.00, 21)],
    ),
    (
        Party("DEDEMAN SRL", "RO2816464", "J04/2621/1992", "RO22RZBR0000060001234567"),
        [("Rafturi metalice", 2, 349.00, 21), ("Surubelnite set", 1, 89.90, 21)],
    ),
    (
        Party("LIBRARIA CARTURESTI SRL", "RO13900345", "J40/1502/2001", "RO11INGB0000999901234567"),
        [("Codul fiscal comentat 2026", 1, 189.00, 11), ("Agenda 2027", 2, 45.00, 21)],
    ),
    (
        Party(
            "OMV PETROM MARKETING SRL", "RO11201891", "J40/8302/1997", "RO63RNCB0082000012345678"
        ),
        [("Motorina Standard", 48.5, 7.62, 21)],
    ),
]
_CUSTOMERS = [
    Party("CLIENT DEMO UNU SRL", "RO31234567", "J40/100/2015"),
    Party("CLIENT DEMO DOI SRL", "RO41234567", "J12/200/2019"),
]


class DemoSpvClient:
    """Deterministic dummy SPV: the same CIF always gets the same invoices.

    Most are *received* (supplier invoices to the client); some are *sent*
    (the client invoicing its customers). Nothing here is real data.
    """

    def __init__(self, *, invoices: int = 6, today: date | None = None) -> None:
        self._count = invoices
        self._today = today or datetime.now(UTC).date()
        self._archives: dict[str, bytes] = {}

    async def list_messages(self, cif: str, days: int) -> list[SpvMessage]:
        digits = cif_digits(cif)
        rng = random.Random(f"langclaw-demo-spv:{digits}")
        messages = []
        for i in range(self._count):
            msg_id = str(int(hashlib.sha256(f"{digits}:{i}".encode()).hexdigest()[:10], 16))
            sent = i % 4 == 3
            issued = self._today - timedelta(days=rng.randint(0, max(0, min(days, MAX_DAYS) - 1)))
            me = Party(
                f"CLIENT {digits} SRL", f"RO{digits}", "J40/999/2020", "RO09BTRL0000000000000001"
            )
            if sent:
                other = _CUSTOMERS[i % len(_CUSTOMERS)]
                supplier, customer = me, other
                lines = [
                    ("Servicii contabilitate", 1, 1500.00, 21),
                    ("Consultanta fiscala", 2, 350.00, 21),
                ]
            else:
                supplier, lines = _SUPPLIERS[rng.randrange(len(_SUPPLIERS))]
                customer = me
            xml = make_invoice(
                number=f"{supplier.name.split()[0][:4]}-{issued:%Y%m}-{100 + i}",
                issue_date=issued.isoformat(),
                due_date=(issued + timedelta(days=30)).isoformat(),
                supplier=supplier,
                customer=customer,
                lines=lines,
            )
            self._archives[msg_id] = _zip(msg_id, xml)
            messages.append(
                SpvMessage(
                    id=msg_id,
                    kind="sent" if sent else "received",
                    cif=digits,
                    created=f"{issued.isoformat()}T09:{i:02d}",
                    details=f"Factura demo pentru cif_beneficiar={cif_digits(customer.cui)}",
                    upload_id=str(4_000_000 + i),
                )
            )
        return messages

    async def download(self, message_id: str) -> bytes:
        try:
            return self._archives[message_id]
        except KeyError:
            raise SpvError(f"SPV: no message {message_id!r} (list messages first).") from None


def _zip(msg_id: str, xml: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{msg_id}.xml", xml)
        zf.writestr(
            f"semnatura_{msg_id}.xml", b"<SignatureDemo>not a real signature</SignatureDemo>"
        )
    return buf.getvalue()
