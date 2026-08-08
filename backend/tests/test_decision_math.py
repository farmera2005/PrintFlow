"""Stock/print split (§4.2) and plate maths (§3)."""

from __future__ import annotations

import pytest

from app.integrations.qbo import item_qty_on_hand
from app.services.allocation import split_quantity
from app.services.printing import plates_needed
from app.services.shipping import next_attempt_due


class TestSplitQuantity:
    def test_stock_covers_the_whole_line(self):
        assert split_quantity(3, qty_on_hand=10, reserved_elsewhere=0) == (3, 0)

    def test_shortfall_goes_to_print(self):
        assert split_quantity(5, qty_on_hand=2, reserved_elsewhere=0) == (2, 3)

    def test_no_stock_prints_everything(self):
        assert split_quantity(4, qty_on_hand=0, reserved_elsewhere=0) == (0, 4)

    def test_soft_reservation_stops_two_orders_claiming_the_last_unit(self):
        # One unit on hand, already claimed by another open order.
        assert split_quantity(1, qty_on_hand=1, reserved_elsewhere=1) == (0, 1)

    def test_over_reservation_never_produces_negative_stock(self):
        assert split_quantity(2, qty_on_hand=1, reserved_elsewhere=5) == (0, 2)

    def test_unknown_quantity_prints_the_full_line(self):
        assert split_quantity(3, qty_on_hand=None, reserved_elsewhere=0) == (0, 3)

    def test_fractional_quantity_on_hand_is_floored(self):
        assert split_quantity(3, qty_on_hand=2.9, reserved_elsewhere=0) == (2, 1)


class TestPlateMath:
    @pytest.mark.parametrize(
        ("qty", "per_plate", "expected"),
        [
            (0, 1, 0),
            (1, 1, 1),
            (4, 1, 4),
            (4, 4, 1),
            (5, 4, 2),  # partial plate still costs a whole print
            (9, 3, 3),
            (10, 3, 4),
        ],
    )
    def test_one_job_per_plate(self, qty, per_plate, expected):
        assert plates_needed(qty, per_plate) == expected

    def test_zero_units_per_plate_is_rejected(self):
        with pytest.raises(ValueError):
            plates_needed(5, 0)


class TestQboQuantityParsing:
    def test_tracked_item_returns_quantity(self):
        assert item_qty_on_hand({"TrackQtyOnHand": True, "QtyOnHand": 7}) == 7.0

    def test_untracked_item_has_no_quantity(self):
        assert item_qty_on_hand({"TrackQtyOnHand": False}) is None

    def test_garbage_quantity_is_ignored(self):
        assert item_qty_on_hand({"TrackQtyOnHand": True, "QtyOnHand": "n/a"}) is None


class TestShipStationBackoff:
    def test_first_attempt_runs_immediately(self):
        assert next_attempt_due(0, None) is True

    def test_second_attempt_waits(self):
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        assert next_attempt_due(1, now) is False
        assert next_attempt_due(1, now - timedelta(minutes=6)) is True

    def test_gives_up_after_the_attempt_cap(self):
        assert next_attempt_due(48, None) is False
