"""QuickBooks Online client.

QBO is the inventory system of record. Deciding print-or-pull only ever reads
QtyOnHand (§4.2); the writes are few, deliberate, and each has a service that
explains itself:

* **manufacturing** — posting a made-items sheet creates a Purchase, which is
  how QuickBooks raises quantity on hand;
* **books** — a printed line takes its units back out again, and an invoice
  records what the order sold for without touching stock a second time.

Every write is started by a person pressing a button, with one exception the
operator controls: a print finishing can book its own stock removal while the
setting under Settings says so.
"""

from __future__ import annotations

import base64
import re
import time
from datetime import date
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import PROVIDER_QBO
from ..services import credentials
from .base import AuthExpiredError, IntegrationError, new_client, request

AUTHORIZE_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
SCOPE = "com.intuit.quickbooks.accounting"
MINOR_VERSION = "75"

API_BASES = {
    "production": "https://quickbooks.api.intuit.com",
    "sandbox": "https://sandbox-quickbooks.api.intuit.com",
}

# §6: refresh proactively at <10 minutes remaining.
REFRESH_MARGIN_SECONDS = 600


def authorize_url(*, client_id: str, redirect_uri: str, state: str) -> str:
    query = urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "scope": SCOPE,
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def _basic_auth(client_id: str, client_secret: str) -> str:
    raw = f"{client_id}:{client_secret}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


async def _token_request(
    *, client_id: str, client_secret: str, form: dict[str, str]
) -> dict[str, Any]:
    async with new_client() as client:
        response = await request(
            client,
            "POST",
            TOKEN_URL,
            provider=PROVIDER_QBO,
            headers={
                "Authorization": _basic_auth(client_id, client_secret),
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            data=form,
        )
    data = response.json()
    if "access_token" not in data:
        raise IntegrationError(PROVIDER_QBO, f"Token response missing access_token: {data}")
    return {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token"),
        "expires_at": time.time() + float(data.get("expires_in", 3600)),
        "refresh_expires_at": time.time()
        + float(data.get("x_refresh_token_expires_in", 8726400)),
    }


async def exchange_code(
    *, client_id: str, client_secret: str, redirect_uri: str, code: str
) -> dict[str, Any]:
    return await _token_request(
        client_id=client_id,
        client_secret=client_secret,
        form={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
    )


async def refresh_tokens(
    *, client_id: str, client_secret: str, refresh_token: str
) -> dict[str, Any]:
    return await _token_request(
        client_id=client_id,
        client_secret=client_secret,
        form={"grant_type": "refresh_token", "refresh_token": refresh_token},
    )


def escape_literal(value: str) -> str:
    """Escape a value for a QBO query string literal."""
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


class QboClient:
    def __init__(self, session: AsyncSession, payload: dict[str, Any]) -> None:
        self.session = session
        self.payload = payload

    @property
    def realm_id(self) -> str | None:
        realm = self.payload.get("realm_id")
        return str(realm) if realm else None

    @property
    def api_base(self) -> str:
        env = str(self.payload.get("environment") or "production")
        return API_BASES.get(env, API_BASES["production"])

    async def _access_token(self) -> str:
        if float(self.payload.get("expires_at") or 0) - REFRESH_MARGIN_SECONDS <= time.time():
            await self.refresh()
        token = self.payload.get("access_token")
        if not token:
            raise AuthExpiredError(PROVIDER_QBO, "No QuickBooks access token stored")
        return str(token)

    async def refresh(self) -> None:
        client_id = self.payload.get("client_id")
        client_secret = self.payload.get("client_secret")
        refresh_token = self.payload.get("refresh_token")
        if not (client_id and client_secret and refresh_token):
            raise AuthExpiredError(
                PROVIDER_QBO, "QuickBooks refresh token missing; reconnect QuickBooks"
            )
        tokens = await refresh_tokens(
            client_id=str(client_id),
            client_secret=str(client_secret),
            refresh_token=str(refresh_token),
        )
        self.payload.update(
            {
                "access_token": tokens["access_token"],
                "refresh_token": tokens.get("refresh_token") or refresh_token,
                "expires_at": tokens["expires_at"],
                "refresh_expires_at": tokens["refresh_expires_at"],
            }
        )
        await credentials.merge(
            self.session,
            PROVIDER_QBO,
            {
                "access_token": self.payload["access_token"],
                "refresh_token": self.payload["refresh_token"],
                "expires_at": self.payload["expires_at"],
                "refresh_expires_at": self.payload["refresh_expires_at"],
            },
        )

    def seconds_until_expiry(self) -> float:
        return float(self.payload.get("expires_at") or 0) - time.time()

    async def query(self, statement: str) -> dict[str, Any]:
        if not self.realm_id:
            raise AuthExpiredError(PROVIDER_QBO, "No QuickBooks company (realm) stored")
        headers = {
            "Authorization": f"Bearer {await self._access_token()}",
            "Accept": "application/json",
        }
        async with new_client(base_url=self.api_base) as client:
            response = await request(
                client,
                "GET",
                f"/v3/company/{self.realm_id}/query",
                provider=PROVIDER_QBO,
                headers=headers,
                params={"query": statement, "minorversion": MINOR_VERSION},
            )
        return response.json().get("QueryResponse", {})

    async def _write(
        self,
        entity: str,
        body: dict[str, Any],
        *,
        params: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """POST to an entity endpoint. Writes are never retried automatically.

        A create that times out may well have succeeded, and a blind retry would
        duplicate a transaction in someone's books. `request_id` is QuickBooks'
        own idempotency key: replaying the same one returns the original result
        instead of creating a second document.
        """
        if not self.realm_id:
            raise AuthExpiredError(PROVIDER_QBO, "No QuickBooks company (realm) stored")
        query: dict[str, Any] = {"minorversion": MINOR_VERSION, **(params or {})}
        if request_id:
            query["requestid"] = request_id
        headers = {
            "Authorization": f"Bearer {await self._access_token()}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        async with new_client(base_url=self.api_base) as client:
            response = await request(
                client,
                "POST",
                f"/v3/company/{self.realm_id}/{entity}",
                provider=PROVIDER_QBO,
                headers=headers,
                params=query,
                json=body,
                retries=0,
            )
        return response.json()

    async def create_purchase(
        self, purchase: dict[str, Any], *, request_id: str | None = None
    ) -> dict[str, Any]:
        """Create an Expense. Item lines are what move quantity on hand."""
        data = await self._write("purchase", purchase, request_id=request_id)
        return data.get("Purchase") or {}

    async def void_purchase(self, purchase_id: str, sync_token: str) -> dict[str, Any]:
        """Delete the Purchase, reversing every quantity it moved.

        QuickBooks has no void for a Purchase — delete is the reversal, and it
        is what the UI's "Void" does. The sheet keeps the id and the payload, so
        the history of what was posted survives the deletion.
        """
        data = await self._write(
            "purchase",
            {"Id": str(purchase_id), "SyncToken": str(sync_token)},
            params={"operation": "delete"},
        )
        return data.get("Purchase") or {}

    async def create_invoice(
        self, invoice: dict[str, Any], *, request_id: str | None = None
    ) -> dict[str, Any]:
        """Create an Invoice. Lines must not name inventory items.

        An invoice line for an Inventory item takes the unit out of stock and
        books its cost — which is right for a shop that only sells, and wrong
        here, where the printed line already did exactly that. See
        services/books.py for the arrangement this is one half of.
        """
        data = await self._write("invoice", invoice, request_id=request_id)
        return data.get("Invoice") or {}

    async def void_invoice(self, invoice_id: str, sync_token: str) -> dict[str, Any]:
        """Void the Invoice, leaving a zero-value document in its place.

        Void rather than delete: an invoice number that simply vanishes is a
        gap somebody has to explain to an accountant, and QuickBooks keeps a
        voided invoice visible with its total zeroed.
        """
        data = await self._write(
            "invoice",
            {"Id": str(invoice_id), "SyncToken": str(sync_token)},
            params={"operation": "void"},
        )
        return data.get("Invoice") or {}

    async def preferences(self) -> dict[str, Any]:
        """The company's own settings, as QuickBooks holds them.

        Two of them decide what an invoice body may contain: whether the
        company numbers its own transactions, and whether it allows a discount
        line at all. Both are per-company and neither can be guessed.
        """
        result = await self.query("select * from Preferences")
        rows = result.get("Preferences") or []
        return rows[0] if rows else {}

    async def last_invoice_doc_number(self) -> str | None:
        """The reference on the most recently created invoice.

        QuickBooks has no "next number" to ask for — the sequence lives inside
        the company file and is only applied as a document is saved. The most
        recent invoice is the nearest thing there is, so it is what the next
        number is worked out from.

        Ordered by creation time rather than by DocNumber, because DocNumber is
        a string: sorted as text, "1009" comes after "999".
        """
        result = await self.query(
            "select * from Invoice orderby MetaData.CreateTime desc maxresults 1"
        )
        rows = result.get("Invoice") or []
        number = str((rows[0] if rows else {}).get("DocNumber") or "").strip()
        return number or None

    async def get_invoice(self, invoice_id: str) -> dict[str, Any] | None:
        result = await self.query(
            f"select * from Invoice where Id = '{escape_literal(str(invoice_id))}'"
        )
        rows = result.get("Invoice") or []
        return rows[0] if rows else None

    async def find_customer(self, display_name: str) -> dict[str, Any] | None:
        """The customer with exactly this display name, if there is one.

        Exact rather than `like`: QuickBooks makes DisplayName unique, so this
        is an identity check. A fuzzy match here would bill one buyer's order
        to a different buyer who happens to share a first name.
        """
        needle = escape_literal(display_name.strip())
        if not needle:
            return None
        result = await self.query(
            f"select * from Customer where DisplayName = '{needle}'"
        )
        rows = result.get("Customer") or []
        return rows[0] if rows else None

    async def create_customer(self, customer: dict[str, Any]) -> dict[str, Any]:
        data = await self._write("customer", customer)
        return data.get("Customer") or {}

    async def get_accounts(
        self, account_types: tuple[str, ...] = (), limit: int = 500
    ) -> list[dict[str, Any]]:
        """Chart of accounts, for choosing what the manufacturing cost comes out of."""
        where = " where Active = true"
        if account_types:
            joined = ",".join(f"'{escape_literal(t)}'" for t in account_types)
            where += f" and AccountType in ({joined})"
        result = await self.query(
            f"select * from Account{where} maxresults {max(1, min(limit, 1000))}"
        )
        return list(result.get("Account") or [])

    async def get_account(self, account_id: str) -> dict[str, Any] | None:
        result = await self.query(
            f"select * from Account where Id = '{escape_literal(str(account_id))}'"
        )
        rows = result.get("Account") or []
        return rows[0] if rows else None

    async def search_vendors(self, term: str = "", limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 200))
        if term.strip():
            needle = escape_literal(term.strip())
            statement = (
                f"select * from Vendor where DisplayName like '%{needle}%' "
                f"maxresults {limit}"
            )
        else:
            statement = f"select * from Vendor maxresults {limit}"
        result = await self.query(statement)
        return list(result.get("Vendor") or [])

    async def company_info(self) -> dict[str, Any]:
        result = await self.query("select * from CompanyInfo")
        rows = result.get("CompanyInfo") or []
        return rows[0] if rows else {}

    async def get_items(self, item_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Fetch items by Id, batched. Returns {item_id: item}."""
        out: dict[str, dict[str, Any]] = {}
        unique = [i for i in dict.fromkeys(str(i) for i in item_ids if i)]
        # QBO caps a query at 1000 results; 100 ids per batch keeps URLs sane.
        for start in range(0, len(unique), 100):
            batch = unique[start : start + 100]
            joined = ",".join(f"'{escape_literal(i)}'" for i in batch)
            result = await self.query(f"select * from Item where Id in ({joined})")
            for item in result.get("Item") or []:
                out[str(item.get("Id"))] = item
        return out

    async def search_items(
        self,
        term: str = "",
        limit: int = 50,
        item_types: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        """Item picker backing query.

        `item_types` narrows the picker to the kinds of item the caller can
        actually use — an invoice that must not move stock has no business
        offering an Inventory item to choose.
        """
        limit = max(1, min(limit, 200))
        clauses = []
        if term.strip():
            clauses.append(f"Name like '%{escape_literal(term.strip())}%'")
        if item_types:
            joined = ",".join(f"'{escape_literal(t)}'" for t in item_types)
            clauses.append(f"Type in ({joined})")
        where = f" where {' and '.join(clauses)}" if clauses else ""
        result = await self.query(f"select * from Item{where} maxresults {limit}")
        return list(result.get("Item") or [])


def custom_transaction_numbers(preferences: dict[str, Any]) -> bool:
    """Does this company number its own sales documents?

    Off — the default — and QuickBooks assigns the next reference itself as the
    document is saved, which is the arrangement PrintFlow wants: one sequence,
    owned by the books. On, and QuickBooks assigns nothing; a document sent
    without a DocNumber simply has no reference. So the answer decides whether
    PrintFlow has to work the number out.
    """
    sales = preferences.get("SalesFormsPrefs") or {}
    return bool(sales.get("CustomTxnNumbers"))


def allows_discount(preferences: dict[str, Any]) -> bool:
    """Will this company accept a discount line on a sales form?

    QuickBooks rejects the whole invoice when discounts are switched off in the
    company's settings, so this is asked before one is added rather than
    discovered from a failed write.
    """
    sales = preferences.get("SalesFormsPrefs") or {}
    return bool(sales.get("AllowDiscount"))


def next_doc_number(previous: str | None) -> str | None:
    """One on from the last reference, keeping whatever shape it had.

    References are strings and shops give them shapes: `1042`, `INV-1042`,
    `0042` with the zeros meaning something to whoever reads the file. Only the
    trailing digits move, the padding is kept, and anything in front is left
    alone — so a company numbering `INV-1042` gets `INV-1043` rather than a bare
    number that breaks its own sequence.

    None when there is nothing to count from, or nothing countable in it: an
    invented reference is worse than letting QuickBooks decide.
    """
    if not previous:
        return None
    digits = re.search(r"(\d+)(?!.*\d)", previous)
    if digits is None:
        return None
    body = digits.group(1)
    nextn = str(int(body) + 1)
    # Keep the width when it was padded, unless carrying over needs the room.
    if len(nextn) < len(body):
        nextn = nextn.rjust(len(body), "0")
    candidate = previous[: digits.start(1)] + nextn + previous[digits.end(1) :]
    # QuickBooks caps DocNumber at 21 characters and refuses anything longer.
    return candidate if len(candidate) <= 21 else None


def item_purchase_cost(item: dict[str, Any]) -> Decimal | None:
    """What QuickBooks last recorded as this item's cost, if anything.

    Decimal, not float: these values are summed into a figure that is posted to
    someone's books, and 0.1 + 0.2 must not be 0.30000000000000004 there.
    """
    raw = item.get("PurchaseCost")
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except (ArithmeticError, TypeError, ValueError):
        return None


def item_is_inventory(item: dict[str, Any]) -> bool:
    """Only Inventory items carry a quantity that a Purchase can move."""
    return str(item.get("Type") or "") == "Inventory"


def item_moves_stock(item: dict[str, Any]) -> bool:
    """Would putting this item on an invoice line change quantity on hand?

    The question the income-item setting has to answer before it is saved. An
    Inventory item on an invoice relieves stock and books cost, which is the
    right behaviour for a shop that only sells and the wrong one here, where
    the printed line has already done it.
    """
    return item_is_inventory(item) or bool(item.get("TrackQtyOnHand"))


def item_inventory_start(item: dict[str, Any]) -> date | None:
    """The day QuickBooks began counting this item, if it counts it at all.

    Nothing touching an inventory item may be dated before this — QuickBooks
    refuses with code 6270, because a quantity cannot move on a day it did not
    yet have one. Only Inventory items have it, and an item created without one
    may not carry it at all, so this is often None and None means "no floor".
    """
    if not item_is_inventory(item):
        return None
    raw = str(item.get("InvStartDate") or "").strip()
    if not raw:
        return None
    try:
        # QuickBooks sends a plain date; be forgiving about a datetime.
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def item_qty_on_hand(item: dict[str, Any]) -> float | None:
    """QtyOnHand is only meaningful for inventory-tracked items."""
    if not item.get("TrackQtyOnHand", False) and "QtyOnHand" not in item:
        return None
    raw = item.get("QtyOnHand")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


async def client_for(session: AsyncSession) -> QboClient:
    payload = await credentials.require(session, PROVIDER_QBO)
    return QboClient(session, payload)
