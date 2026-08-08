"""Pure state-machine rules from §5 of the build spec."""

from __future__ import annotations

import pytest

from app.services.state import (
    LineView,
    compute_bundle_line_state,
    compute_leaf_line_state,
    compute_order_status,
    order_needs_attention,
)


def leaf(**overrides):
    base = dict(
        matched=True,
        qty_to_print=0,
        job_statuses=(),
        decided=True,
    )
    base.update(overrides)
    return compute_leaf_line_state(**base)


class TestLeafStates:
    def test_unmatched_sku_flags_the_line(self):
        assert leaf(matched=False) == "unmatched"

    def test_matched_but_undecided_line_is_new(self):
        assert leaf(decided=False) == "new"

    def test_fully_from_stock_becomes_ready(self):
        assert leaf(qty_to_print=0) == "ready"

    def test_planned_but_undispatched_plates_stay_new(self):
        # "New: ingested, decisions made, nothing printing yet" (§5).
        assert leaf(qty_to_print=4, job_statuses=("pending", "pending")) == "new"

    def test_any_dispatched_plate_moves_to_printing(self):
        assert leaf(qty_to_print=4, job_statuses=("queued", "pending")) == "printing"
        assert leaf(qty_to_print=4, job_statuses=("printing",)) == "printing"

    def test_all_plates_done_becomes_ready(self):
        assert leaf(qty_to_print=4, job_statuses=("done", "done")) == "ready"

    def test_a_failed_plate_holds_the_line_in_production(self):
        # Not "printed": the operator must re-queue or override first.
        assert leaf(qty_to_print=4, job_statuses=("done", "failed")) == "printing"

    def test_decided_to_print_but_no_plates_planned_is_new(self):
        # e.g. the SKU has no Bambuddy mapping yet.
        assert leaf(qty_to_print=2, job_statuses=()) == "new"

    def test_override_marks_a_line_printed_without_jobs(self):
        assert leaf(qty_to_print=5, override_state="printed") == "ready"

    def test_cancel_override_wins_over_everything(self):
        assert leaf(matched=False, override_state="cancelled") == "cancelled"

    def test_order_level_label_and_ship_flow_down_to_lines(self):
        assert leaf(order_labeled=True) == "labeled"
        assert leaf(order_shipped=True) == "shipped"


class TestBundleAssemblyGate:
    def test_component_waits_for_assembly_before_ready(self):
        state = leaf(qty_to_print=0, parent_is_bundle=True, parent_assembled=False)
        assert state == "allocated"

        printed = leaf(
            qty_to_print=2,
            job_statuses=("done",),
            parent_is_bundle=True,
            parent_assembled=False,
        )
        assert printed == "printed"

    def test_component_is_ready_once_the_bundle_is_assembled(self):
        assert (
            leaf(qty_to_print=0, parent_is_bundle=True, parent_assembled=True) == "ready"
        )

    def test_bundle_rolls_up_its_children(self):
        assert compute_bundle_line_state(child_states=["printed", "ready"]) == "exploded"
        assert compute_bundle_line_state(child_states=["ready", "ready"]) == "ready"

    def test_bundle_with_every_child_cancelled_is_cancelled(self):
        assert compute_bundle_line_state(child_states=["cancelled"]) == "cancelled"

    def test_cancelled_children_are_ignored_by_the_roll_up(self):
        assert (
            compute_bundle_line_state(child_states=["ready", "cancelled"]) == "ready"
        )


class TestOrderRollUp:
    def test_empty_order_is_new(self):
        assert compute_order_status([]) == "new"

    def test_unmatched_line_keeps_the_order_in_new(self):
        views = [LineView(state="unmatched")]
        assert compute_order_status(views) == "new"
        assert order_needs_attention(views) is True

    def test_unmatched_line_outranks_a_printing_sibling(self):
        # The card must stay in New so the "link product" fix path is visible.
        views = [LineView(state="unmatched"), LineView(state="printing")]
        assert compute_order_status(views) == "new"
        assert order_needs_attention(views) is True

    def test_printing_line_moves_the_order_into_production(self):
        assert compute_order_status([LineView(state="printing")]) == "in_production"

    def test_ready_when_every_leaf_is_ready(self):
        views = [LineView(state="ready"), LineView(state="ready")]
        assert compute_order_status(views) == "ready_to_ship"

    def test_one_unready_leaf_blocks_ready_to_ship(self):
        views = [LineView(state="ready"), LineView(state="printing")]
        assert compute_order_status(views) == "in_production"

    def test_assembly_column_only_once_production_is_finished(self):
        bundle = LineView(state="exploded", is_bundle=True, is_leaf=False, assembled=False)
        done = LineView(state="printed")
        assert compute_order_status([bundle, done]) == "assembly"

        still_printing = LineView(state="printing")
        assert compute_order_status([bundle, still_printing]) == "in_production"

    def test_orders_without_bundles_skip_assembly(self):
        assert compute_order_status([LineView(state="ready")]) == "ready_to_ship"

    def test_assembled_bundle_advances_to_ready_to_ship(self):
        bundle = LineView(state="ready", is_bundle=True, is_leaf=False, assembled=True)
        child = LineView(state="ready")
        assert compute_order_status([bundle, child]) == "ready_to_ship"

    def test_label_creation_moves_the_order_to_shipped(self):
        views = [LineView(state="ready")]
        assert compute_order_status(views, label_created=True) == "shipped"

    def test_all_lines_cancelled_cancels_the_order(self):
        assert compute_order_status([LineView(state="cancelled")]) == "cancelled"

    def test_failed_print_job_raises_attention(self):
        views = [LineView(state="printing", has_jobs=True, has_failed_job=True)]
        assert order_needs_attention(views) is True

    @pytest.mark.parametrize("state", ["ready", "allocated", "printed"])
    def test_cancelled_lines_do_not_block_the_roll_up(self, state):
        views = [LineView(state=state), LineView(state="cancelled")]
        assert compute_order_status(views) in {"ready_to_ship", "in_production"}
