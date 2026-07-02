"""Tests for the pure segment model (core/segments.py).

The invariant under test everywhere: a SegmentPlan always paves [0, duration_ms]
with contiguous, sorted, non-overlapping segments.
"""

from core import segments as sg


def test_new_plan_is_single_kept_segment():
    plan = sg.new_plan(1000)
    assert plan.duration_ms == 1000
    assert len(plan.segments) == 1
    assert plan.segments[0].start_ms == 0
    assert plan.segments[0].end_ms == 1000
    assert plan.segments[0].keep is True
    assert sg.kept_regions(plan) == [(0, 1000)]
    assert sg.kept_duration_ms(plan) == 1000


def test_new_plan_clamps_negative_duration():
    plan = sg.new_plan(-5)
    assert plan.duration_ms == 0


def test_split_at_creates_boundary_and_returns_right_index():
    plan = sg.new_plan(1000)
    idx = sg.split_at(plan, 500)
    assert idx == 1
    assert [(s.start_ms, s.end_ms) for s in plan.segments] == [(0, 500), (500, 1000)]


def test_split_at_out_of_range_is_noop():
    plan = sg.new_plan(1000)
    assert sg.split_at(plan, 0) == -1
    assert sg.split_at(plan, 1000) == -1
    assert sg.split_at(plan, 5000) == -1
    assert len(plan.segments) == 1


def test_split_at_existing_boundary_is_noop():
    plan = sg.new_plan(1000)
    sg.split_at(plan, 500)
    assert sg.split_at(plan, 500) == -1
    assert len(plan.segments) == 2


def test_kept_regions_merges_adjacent_keeps():
    plan = sg.new_plan(1000)
    sg.split_at(plan, 500)  # two adjacent kept segments
    assert sg.kept_regions(plan) == [(0, 1000)]


def test_mark_region_discard_carves_a_hole():
    plan = sg.new_plan(1000)
    assert sg.mark_region(plan, 200, 500, keep=False) is True
    assert sg.kept_regions(plan) == [(0, 200), (500, 1000)]
    assert sg.kept_duration_ms(plan) == 700


def test_mark_region_swaps_reversed_bounds():
    plan = sg.new_plan(1000)
    assert sg.mark_region(plan, 500, 200, keep=False) is True
    assert sg.kept_regions(plan) == [(0, 200), (500, 1000)]


def test_mark_region_clamps_to_duration():
    plan = sg.new_plan(1000)
    assert sg.mark_region(plan, -100, 5000, keep=False) is True
    assert sg.kept_regions(plan) == []


def test_toggle_and_set_keep():
    plan = sg.new_plan(1000)
    assert sg.toggle_keep(plan, 0) is True
    assert plan.segments[0].keep is False
    assert sg.set_keep(plan, 0, True) is True
    assert plan.segments[0].keep is True
    assert sg.set_keep(plan, 9, True) is False


def test_remove_boundary_merges_neighbours():
    plan = sg.new_plan(1000)
    sg.split_at(plan, 500)
    assert sg.remove_boundary(plan, 0) is True
    assert [(s.start_ms, s.end_ms) for s in plan.segments] == [(0, 1000)]
    # Removing on the last segment is a no-op.
    assert sg.remove_boundary(plan, 0) is False or len(plan.segments) == 1


def test_set_segment_start_and_end_move_boundaries():
    plan = sg.new_plan(1000)
    sg.split_at(plan, 500)
    assert sg.set_segment_start(plan, 1, 400) is True
    assert plan.segments[0].end_ms == 400
    assert plan.segments[1].start_ms == 400
    assert sg.set_segment_end(plan, 0, 300) is True
    assert plan.segments[0].end_ms == 300
    # First segment has no movable start; last has no movable end.
    assert sg.set_segment_start(plan, 0, 100) is False
    assert sg.set_segment_end(plan, len(plan.segments) - 1, 900) is False


def test_plan_dict_round_trip():
    plan = sg.new_plan(1000)
    sg.mark_region(plan, 200, 500, keep=False)
    data = sg.plan_to_dict(plan)
    restored = sg.plan_from_dict(data)
    assert restored.duration_ms == plan.duration_ms
    assert sg.kept_regions(restored) == sg.kept_regions(plan)


def test_plan_from_dict_reclamps_to_new_duration():
    data = {"duration_ms": 1000, "segments": [{"start_ms": 0, "end_ms": 1000, "keep": True}]}
    restored = sg.plan_from_dict(data, duration_ms=800)
    assert restored.duration_ms == 800
    assert sg.kept_regions(restored) == [(0, 800)]


def test_validate_rejects_zero_duration_and_all_discard():
    assert sg.validate(sg.new_plan(0)) is not None
    plan = sg.new_plan(1000)
    sg.set_keep(plan, 0, False)
    assert sg.validate(plan) is not None
    assert sg.validate(sg.new_plan(1000)) is None
