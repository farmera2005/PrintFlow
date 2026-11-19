"""The shop's own two marks on an order: a note, and some flags.

Everything else an order carries is a fact some system reported — what Etsy
sold, what QuickBooks was told, what the carrier last said. These two are the
only things on an order a person wrote, and the claims worth testing are
about exactly that difference.

A note is the shop's, so nothing upstream may overwrite it: not a re-poll,
not a status change, not re-running intake. A flag is the shop's vocabulary,
so an order stores an **id** and the words live in one place — rename "Rush"
to "Priority" and nine hundred orders are renamed, not none. And deleting a
flag is a decision about the vocabulary rather than an instruction to go and
edit history: the orders keep it, it stops being drawn, and putting it back
brings them back.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import AuditLog, Order, Product
from app.services import intake, settings_store

pytestmark = pytest.mark.asyncio


async def _order(db, number="9001", **fields) -> Order:
    order = Order(
        source="etsy",
        etsy_receipt_id=int(number),
        order_number=number,
        buyer_name="Dana Buyer",
        **fields,
    )
    db.add(order)
    await db.flush()
    return order


# --------------------------------------------------------------------------
# The note
# --------------------------------------------------------------------------


class TestTheNote:
    async def test_an_order_starts_with_nothing_written_on_it(self, db):
        order = await _order(db)
        await db.commit()
        assert order.notes is None
        assert order.flags == []

    async def test_writing_changing_and_clearing(self, db, signed_in):
        order = await _order(db)
        await db.commit()

        written = await signed_in.patch(
            f"/api/orders/{order.id}/notes",
            json={"notes": "Buyer wants it by the 20th."},
        )
        assert written.status_code == 200
        assert written.json()["notes"] == "Buyer wants it by the 20th."

        changed = await signed_in.patch(
            f"/api/orders/{order.id}/notes", json={"notes": "Buyer moved it to the 25th."}
        )
        assert changed.json()["notes"] == "Buyer moved it to the 25th."

        cleared = await signed_in.patch(
            f"/api/orders/{order.id}/notes", json={"notes": ""}
        )
        assert cleared.json()["notes"] is None

    async def test_whitespace_is_not_a_note(self, db, signed_in):
        """Otherwise the card draws an empty bubble and the drawer offers
        "Edit" on nothing."""
        order = await _order(db)
        await db.commit()

        response = await signed_in.patch(
            f"/api/orders/{order.id}/notes", json={"notes": "   \n  "}
        )
        assert response.json()["notes"] is None

    async def test_the_note_is_trimmed_but_its_shape_is_kept(self, db, signed_in):
        """A note somebody laid out in lines is a note they laid out in
        lines. Only the ends go."""
        order = await _order(db)
        await db.commit()

        body = await signed_in.patch(
            f"/api/orders/{order.id}/notes",
            json={"notes": "  first line\nsecond line  "},
        )
        assert body.json()["notes"] == "first line\nsecond line"

    async def test_a_note_too_long_for_a_note_is_refused(self, db, signed_in):
        order = await _order(db)
        await db.commit()
        response = await signed_in.patch(
            f"/api/orders/{order.id}/notes", json={"notes": "x" * 4001}
        )
        assert response.status_code == 422

    async def test_moving_the_card_does_not_clear_it(self, db, signed_in):
        """`status_note` answers "why is this card here" and is written when
        a card moves. If the two were the same field, a note would vanish the
        next time somebody dragged the card."""
        order = await _order(db)
        await db.commit()
        await signed_in.patch(
            f"/api/orders/{order.id}/notes", json={"notes": "Reprint — first one warped."}
        )

        await signed_in.put(
            f"/api/orders/{order.id}/status",
            json={"status": "in_production", "note": "Started it."},
        )

        await db.refresh(order)
        assert order.notes == "Reprint — first one warped."
        assert order.status_note == "Started it."

    async def test_re_running_intake_does_not_touch_it(self, db, signed_in):
        """Intake re-reads what the channel sent. The note did not come from
        the channel, so nothing in that payload can have an opinion on it."""
        db.add(Product(sku="PART-X", name="Printed part X", fulfillment="stocked"))
        await db.flush()
        order, _ = await intake.ingest_receipt(
            db,
            {
                "receipt_id": 7001,
                "transactions": [{"transaction_id": 1, "sku": "PART-X", "quantity": 1}],
            },
        )
        await db.commit()
        await signed_in.patch(
            f"/api/orders/{order.id}/notes", json={"notes": "Gift wrap, no receipt."}
        )

        await signed_in.post(f"/api/orders/{order.id}/reprocess")

        await db.refresh(order)
        assert order.notes == "Gift wrap, no receipt."

    async def test_the_note_is_on_the_card_and_in_the_drawer(self, db, signed_in):
        order = await _order(db)
        await db.commit()
        await signed_in.patch(
            f"/api/orders/{order.id}/notes", json={"notes": "Ring before delivery."}
        )

        board = (await signed_in.get("/api/board")).json()
        card = board["columns"][0]["orders"][0]
        assert card["notes"] == "Ring before delivery."

        drawer = (await signed_in.get(f"/api/orders/{order.id}")).json()
        assert drawer["notes"] == "Ring before delivery."

    async def test_the_log_records_the_edit_without_copying_the_note(
        self, db, signed_in
    ):
        """The note is already on the order. A log that copied every version
        of it would be a second place to go looking for the current one."""
        order = await _order(db)
        await db.commit()
        await signed_in.patch(
            f"/api/orders/{order.id}/notes", json={"notes": "Private: buyer's address is wrong."}
        )

        rows = (
            (
                await db.execute(
                    select(AuditLog).where(AuditLog.entity_id == order.id)
                )
            )
            .scalars()
            .all()
        )
        assert [row.action for row in rows] == ["note_written"]
        assert "buyer" not in str(rows[0].detail).lower()
        assert rows[0].detail["length"] == len("Private: buyer's address is wrong.")

    async def test_clearing_is_logged_as_clearing(self, db, signed_in):
        order = await _order(db)
        await db.commit()
        await signed_in.patch(f"/api/orders/{order.id}/notes", json={"notes": "x"})
        await signed_in.patch(f"/api/orders/{order.id}/notes", json={"notes": ""})

        rows = (
            (
                await db.execute(
                    select(AuditLog)
                    .where(AuditLog.entity_id == order.id)
                    .order_by(AuditLog.created_at)
                )
            )
            .scalars()
            .all()
        )
        assert [row.action for row in rows] == ["note_written", "note_cleared"]

    async def test_it_takes_a_signed_in_user(self, db, client):
        order = await _order(db)
        await db.commit()
        response = await client.patch(
            f"/api/orders/{order.id}/notes", json={"notes": "hello"}
        )
        assert response.status_code == 401


# --------------------------------------------------------------------------
# The flags on an order
# --------------------------------------------------------------------------


class TestFlaggingAnOrder:
    async def test_a_flag_goes_on_and_comes_off(self, db, signed_in):
        order = await _order(db)
        await db.commit()

        on = await signed_in.put(
            f"/api/orders/{order.id}/flags", json={"flags": ["rush"]}
        )
        assert on.status_code == 200
        assert on.json()["flags"] == ["rush"]

        off = await signed_in.put(f"/api/orders/{order.id}/flags", json={"flags": []})
        assert off.json()["flags"] == []

    async def test_an_order_can_carry_several(self, db, signed_in):
        """"Rush" and "Fragile" are both true of the same parcel, so one
        flag at a time would be a shop having to choose which half to say."""
        order = await _order(db)
        await db.commit()

        body = await signed_in.put(
            f"/api/orders/{order.id}/flags", json={"flags": ["rush", "fragile"]}
        )
        assert body.json()["flags"] == ["rush", "fragile"]

    async def test_a_flag_the_shop_does_not_have_is_refused(self, db, signed_in):
        """Stored, it would be an id nothing can draw and nobody can
        explain."""
        order = await _order(db)
        await db.commit()

        response = await signed_in.put(
            f"/api/orders/{order.id}/flags", json={"flags": ["made-up"]}
        )
        assert response.status_code == 400
        assert "made-up" in response.json()["detail"]
        await db.refresh(order)
        assert order.flags == []

    async def test_the_same_flag_twice_is_once(self, db, signed_in):
        order = await _order(db)
        await db.commit()
        body = await signed_in.put(
            f"/api/orders/{order.id}/flags", json={"flags": ["rush", "rush"]}
        )
        assert body.json()["flags"] == ["rush"]

    async def test_the_card_carries_ids_and_the_board_carries_the_words(
        self, db, signed_in
    ):
        """Four hundred cards saying "Rush" is four hundred copies of a word
        that can be renamed in one place."""
        order = await _order(db)
        await db.commit()
        await signed_in.put(f"/api/orders/{order.id}/flags", json={"flags": ["rush"]})

        board = (await signed_in.get("/api/board")).json()
        assert board["columns"][0]["orders"][0]["flags"] == ["rush"]
        assert {flag["id"] for flag in board["flag_options"]} >= {"rush", "gift"}

    async def test_renaming_a_flag_renames_it_on_every_order(self, db, signed_in):
        """The point of storing an id. The order is not touched at all."""
        order = await _order(db)
        await db.commit()
        await signed_in.put(f"/api/orders/{order.id}/flags", json={"flags": ["rush"]})

        flags = (await signed_in.get("/api/settings/order-flags")).json()["flags"]
        renamed = [
            {**flag, "label": "Priority"} if flag["id"] == "rush" else flag
            for flag in flags
        ]
        await signed_in.put("/api/settings/order-flags", json={"flags": renamed})

        board = (await signed_in.get("/api/board")).json()
        assert board["columns"][0]["orders"][0]["flags"] == ["rush"]
        words = {flag["id"]: flag["label"] for flag in board["flag_options"]}
        assert words["rush"] == "Priority"

    async def test_deleting_a_flag_leaves_the_orders_holding_it(self, db, signed_in):
        """Deleting is a decision about the shop's vocabulary, not an
        instruction to go and edit history — so putting it back brings the
        orders back with it."""
        order = await _order(db)
        await db.commit()
        await signed_in.put(f"/api/orders/{order.id}/flags", json={"flags": ["rush"]})

        flags = (await signed_in.get("/api/settings/order-flags")).json()["flags"]
        await signed_in.put(
            "/api/settings/order-flags",
            json={"flags": [flag for flag in flags if flag["id"] != "rush"]},
        )

        await db.refresh(order)
        assert order.flags == ["rush"]
        # Nothing draws it: the board no longer has a word for that id.
        board = (await signed_in.get("/api/board")).json()
        assert "rush" not in {flag["id"] for flag in board["flag_options"]}

        # Put it back, and the order is flagged again.
        await signed_in.put(
            "/api/settings/order-flags",
            json={"flags": [*flags]},
        )
        again = (await signed_in.get("/api/board")).json()
        assert "rush" in {flag["id"] for flag in again["flag_options"]}
        assert again["columns"][0]["orders"][0]["flags"] == ["rush"]

    async def test_an_order_can_be_edited_free_of_a_deleted_flag(self, db, signed_in):
        """The other direction: once a flag is gone, setting this order's
        flags must not fail because of the one it is still carrying."""
        order = await _order(db)
        await db.commit()
        await signed_in.put(
            f"/api/orders/{order.id}/flags", json={"flags": ["rush", "gift"]}
        )
        flags = (await signed_in.get("/api/settings/order-flags")).json()["flags"]
        await signed_in.put(
            "/api/settings/order-flags",
            json={"flags": [flag for flag in flags if flag["id"] != "rush"]},
        )

        body = await signed_in.put(
            f"/api/orders/{order.id}/flags", json={"flags": ["gift"]}
        )
        assert body.status_code == 200
        assert body.json()["flags"] == ["gift"]

    async def test_the_log_says_what_changed_not_what_it_now_is(self, db, signed_in):
        order = await _order(db)
        await db.commit()
        await signed_in.put(
            f"/api/orders/{order.id}/flags", json={"flags": ["rush", "gift"]}
        )
        await signed_in.put(f"/api/orders/{order.id}/flags", json={"flags": ["gift"]})

        rows = (
            (
                await db.execute(
                    select(AuditLog)
                    .where(AuditLog.entity_id == order.id)
                    .order_by(AuditLog.created_at)
                )
            )
            .scalars()
            .all()
        )
        assert rows[0].detail == {"added": ["gift", "rush"], "removed": []}
        assert rows[1].detail == {"added": [], "removed": ["rush"]}

    async def test_it_takes_a_signed_in_user(self, db, client):
        order = await _order(db)
        await db.commit()
        response = await client.put(
            f"/api/orders/{order.id}/flags", json={"flags": []}
        )
        assert response.status_code == 401


# --------------------------------------------------------------------------
# The shop's own list of flags
# --------------------------------------------------------------------------


class TestTheFlagList:
    async def test_a_new_shop_gets_a_starting_point(self, db):
        """Not a fixture — every one of them can be renamed, recoloured or
        deleted. They exist because an empty list looks like a feature that
        failed to load."""
        flags = await settings_store.get_order_flags(db)
        assert [flag["id"] for flag in flags] == [
            "rush",
            "gift",
            "fragile",
            "waiting-on-buyer",
        ]

    async def test_a_new_flag_is_given_a_readable_id(self, db, signed_in):
        """These ids end up stored on orders and read in a backup. `reprint`
        tells somebody what they are looking at where `f7c21a` does not."""
        body = await signed_in.put(
            "/api/settings/order-flags",
            json={"flags": [{"label": "Reprint — second go", "color": "amber"}]},
        )
        assert body.status_code == 200
        assert body.json()["flags"][0]["id"] == "reprint-second-go"

    async def test_an_id_already_on_orders_survives_a_rename(self, db, signed_in):
        flags = (await signed_in.get("/api/settings/order-flags")).json()["flags"]
        renamed = [{**flag, "label": "Priority"} if flag["id"] == "rush" else flag for flag in flags]
        body = await signed_in.put("/api/settings/order-flags", json={"flags": renamed})
        kept = {flag["id"]: flag["label"] for flag in body.json()["flags"]}
        assert kept["rush"] == "Priority"

    async def test_two_flags_called_the_same_thing_get_different_ids(self, db, signed_in):
        """A shop's business what it calls them; a bug if the second one
        silently became the first."""
        body = await signed_in.put(
            "/api/settings/order-flags",
            json={"flags": [{"label": "Hold"}, {"label": "Hold"}]},
        )
        ids = [flag["id"] for flag in body.json()["flags"]]
        assert ids == ["hold", "hold-2"]

    async def test_a_colour_nobody_can_draw_falls_back_rather_than_failing(
        self, db, signed_in
    ):
        """The screen turns these into Tailwind classes, and a colour nobody
        generated draws as nothing at all. A visible wrong colour beats an
        invisible flag."""
        body = await signed_in.put(
            "/api/settings/order-flags",
            json={"flags": [{"label": "Trade", "color": "#ff0000"}]},
        )
        assert body.json()["flags"][0]["color"] == "slate"

    async def test_a_flag_with_no_name_is_refused(self, db, signed_in):
        """Refused and not dropped: a flag that vanished on save, having
        been typed and seen, is noticed after the order it was for shipped."""
        response = await signed_in.put(
            "/api/settings/order-flags", json={"flags": [{"label": "   "}]}
        )
        assert response.status_code == 400

    async def test_a_name_too_long_to_read_on_a_card_is_refused(self, db, signed_in):
        response = await signed_in.put(
            "/api/settings/order-flags",
            json={"flags": [{"label": "x" * 25}]},
        )
        assert response.status_code == 422

    async def test_the_order_they_are_put_in_is_the_order_they_come_back(
        self, db, signed_in
    ):
        """It is the order they are offered in and drawn in, so a shop that
        puts its most-used flag first should see it first everywhere."""
        body = await signed_in.put(
            "/api/settings/order-flags",
            json={
                "flags": [
                    {"label": "Third"},
                    {"label": "First"},
                    {"label": "Second"},
                ]
            },
        )
        assert [flag["label"] for flag in body.json()["flags"]] == [
            "Third",
            "First",
            "Second",
        ]

    async def test_it_says_how_many_orders_carry_each_flag(self, db, signed_in):
        """The number that makes deleting one a decision rather than a
        click."""
        for index in range(3):
            order = await _order(db, number=f"920{index}")
            await db.commit()
            await signed_in.put(
                f"/api/orders/{order.id}/flags", json={"flags": ["rush"]}
            )

        counts = {
            flag["id"]: flag["orders"]
            for flag in (await signed_in.get("/api/settings/order-flags")).json()["flags"]
        }
        assert counts["rush"] == 3
        assert counts["gift"] == 0

    async def test_a_flag_orders_carry_but_settings_lost_is_reported(
        self, db, signed_in
    ):
        """The only place that leftover is visible, and the only way
        somebody can decide to bring the flag back rather than wonder where
        it went."""
        order = await _order(db)
        await db.commit()
        await signed_in.put(f"/api/orders/{order.id}/flags", json={"flags": ["rush"]})
        flags = (await signed_in.get("/api/settings/order-flags")).json()["flags"]
        await signed_in.put(
            "/api/settings/order-flags",
            json={"flags": [flag for flag in flags if flag["id"] != "rush"]},
        )

        body = (await signed_in.get("/api/settings/order-flags")).json()
        assert body["orphans"] == [{"id": "rush", "orders": 1}]

    async def test_deleting_is_logged_with_what_it_cost(self, db, signed_in):
        order = await _order(db)
        await db.commit()
        await signed_in.put(f"/api/orders/{order.id}/flags", json={"flags": ["gift"]})
        flags = (await signed_in.get("/api/settings/order-flags")).json()["flags"]
        await signed_in.put(
            "/api/settings/order-flags",
            json={"flags": [flag for flag in flags if flag["id"] != "gift"]},
        )

        row = (
            (
                await db.execute(
                    select(AuditLog)
                    .where(AuditLog.action == "order_flags_changed")
                    .order_by(AuditLog.created_at.desc())
                )
            )
            .scalars()
            .first()
        )
        assert row.detail["removed"] == [{"label": "Gift", "orders": 1}]

    async def test_a_shop_may_have_no_flags_at_all(self, db, signed_in):
        """Nothing breaks: the board simply offers none."""
        body = await signed_in.put("/api/settings/order-flags", json={"flags": []})
        assert body.json()["flags"] == []
        board = (await signed_in.get("/api/board")).json()
        assert board["flag_options"] == []

    async def test_more_flags_than_a_card_can_carry_is_refused(self, db, signed_in):
        response = await signed_in.put(
            "/api/settings/order-flags",
            json={"flags": [{"label": f"Flag {n}"} for n in range(25)]},
        )
        assert response.status_code == 422

    async def test_unreadable_stored_flags_do_not_take_the_board_down(self, db):
        """The board matters more than the flags do."""
        await settings_store.set_setting(
            db, settings_store.KEY_ORDER_FLAGS, "not a list at all"
        )
        await db.commit()
        assert await settings_store.get_order_flags(db) == []

    async def test_it_takes_a_signed_in_user(self, db, client):
        assert (await client.get("/api/settings/order-flags")).status_code == 401
        assert (
            await client.put("/api/settings/order-flags", json={"flags": []})
        ).status_code == 401
