"""Finding an order from whatever the person happens to remember.

The old search looked in two places — the order number and the buyer's name —
which is fine right up until somebody is holding a parcel with a tracking
number on it, or has been asked "did we ever make the cable bracket for that
trade customer", or wrote a note last Tuesday and can remember three words of
it. In all of those the order exists, the search says *No orders match*, and
the person goes and scrolls two hundred rows instead.

So: one box, and it looks everywhere a person might know the order from.

**Every word has to match something, but not the same something.** Typing
`dana bracket` finds Dana's order containing a bracket, which is how anybody
expects a search box to behave and is the thing that makes a two-word query
worth typing at all. Each word is matched against every field; the words are
then ANDed, so adding a word always narrows.

**A row says why it matched** when the reason is not already on it. A result
whose match is invisible — the needle was in a note, or in a line nobody can
see from the list — reads as a bug, and the fix is a sentence, not a narrower
search.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import ColumnElement, or_, select

from ..models import Order, OrderLine, Product

# How many words of a query are worth honouring. Each one is another OR
# across six fields and an EXISTS over the lines, so this is a bound on the
# query rather than on the person: nobody types eight words into a search box
# with a plan.
TERM_LIMIT = 6


def terms(query: str) -> list[str]:
    """The words to look for, in the order they were typed.

    Split on whitespace rather than taken whole, because "dana bracket" is
    two facts about one order and almost never a string that appears in it.
    """
    found: list[str] = []
    for word in (query or "").split():
        word = word.strip()
        if word and word.lower() not in (entry.lower() for entry in found):
            found.append(word)
        if len(found) >= TERM_LIMIT:
            break
    return found


def _needle(term: str) -> str:
    """One word, as a LIKE pattern that cannot do anything clever.

    `%` and `_` are wildcards to SQL and ordinary characters to the person
    typing them, so a search for `50%` has to mean fifty per cent rather than
    everything. Escaped with a backslash, which the queries below name.
    """
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _matches(term: str) -> ColumnElement[bool]:
    """Everywhere one word is allowed to match.

    The order of this list is the order of `explain` below, and both are the
    list of things somebody might know an order by. Deliberately not here:
    the shipping address, which is a JSON column — searching it would mean
    either a containment operator that compiles to a LIKE against serialised
    text on anything but Postgres, or matching the word `city` in every
    order because it is a key. The buyer's name is the identity people
    actually search by, and it is already in.
    """
    needle = _needle(term)
    return or_(
        Order.order_number.ilike(needle, escape="\\"),
        Order.buyer_name.ilike(needle, escape="\\"),
        Order.tracking_number.ilike(needle, escape="\\"),
        Order.qbo_invoice_doc_number.ilike(needle, escape="\\"),
        Order.notes.ilike(needle, escape="\\"),
        # The things in the order. An EXISTS rather than a join, so an order
        # with four matching lines is one row and not four — the list is of
        # orders, and a join would quietly turn the page limit into a limit
        # on something else.
        select(OrderLine.id)
        .outerjoin(Product, Product.id == OrderLine.product_id)
        .where(
            OrderLine.order_id == Order.id,
            or_(
                OrderLine.title.ilike(needle, escape="\\"),
                OrderLine.sku_raw.ilike(needle, escape="\\"),
                Product.sku.ilike(needle, escape="\\"),
                Product.name.ilike(needle, escape="\\"),
            ),
        )
        .exists(),
    )


def conditions(query: str) -> list[ColumnElement[bool]]:
    """The query as a list of things that must all be true.

    One per word, so adding a word always narrows and never widens. An empty
    query is an empty list, which is every order — the right answer to being
    asked nothing.
    """
    return [_matches(term) for term in terms(query)]


def _hit(term: str, value: str | None) -> bool:
    return bool(value) and term.lower() in str(value).lower()


def explain(order: Order, query: str) -> list[str]:
    """Why this order is in the results, when the reason is not on the row.

    Only the invisible ones. The number, the buyer and the status are already
    drawn, so saying the match was on the buyer's name next to the buyer's
    name is noise; saying it was on a note nobody can see is the difference
    between a search that works and one that looks broken.

    Phrased for a person and deduplicated: three lines matching one word is
    one reason, not three.
    """
    reasons: list[str] = []

    def add(reason: str) -> None:
        if reason not in reasons:
            reasons.append(reason)

    for term in terms(query):
        if _hit(term, order.tracking_number):
            add(f"tracking {order.tracking_number}")
        if _hit(term, order.qbo_invoice_doc_number):
            add(f"invoice {order.qbo_invoice_doc_number}")
        if _hit(term, order.notes):
            add("the note")
        for line in order.lines:
            product: Product | None = line.product
            if (
                _hit(term, line.title)
                or _hit(term, line.sku_raw)
                or (product and (_hit(term, product.sku) or _hit(term, product.name)))
            ):
                # What the person would call it, which is the listing's own
                # name where there is one: a code they have never seen is not
                # an explanation.
                add(
                    "item “%s”"
                    % (
                        (product.name if product else None)
                        or line.title
                        or line.sku_raw
                        or "unknown"
                    )
                )
                break

    return reasons


def describe(found: int, query: str) -> dict[str, Any]:
    """What the search did, for a screen that has to say something honest.

    The words are handed back as the search understood them, because a query
    that was trimmed at six words, or had a repeat dropped, should say so
    rather than quietly answer a different question.
    """
    words = terms(query)
    return {"terms": words, "results": found, "trimmed": len(words) >= TERM_LIMIT}
