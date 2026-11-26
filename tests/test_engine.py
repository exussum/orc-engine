from datetime import UTC, datetime, time, timedelta

import pytest
from freezegun import freeze_time

from orc_engine import engine as e
from orc_engine import model as em


LIGHT = em.Command("light", "on")
ALICE = em.Eq("person", "Alice")
T0 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
T1 = T0 + timedelta(hours=1)


def read_from(world):
    def read(subject):
        return world[subject]

    return read


def world_of(values):
    return e.Runtime(UTC).world(read_from(values))


@pytest.fixture
def clock():
    with freeze_time(T0) as frozen:
        yield frozen


@pytest.fixture
def runtime(clock):
    return e.Runtime(UTC)


@pytest.fixture
def overriding_runtime(clock):
    return e.Runtime(UTC, override_key="s")


def test_is_holds_reads_the_world():
    condition = em.Eq("ac", "on")
    assert condition.holds(world_of({"ac": "on"}))
    assert not condition.holds(world_of({"ac": "off"}))


def test_in_holds_when_value_is_among_the_reading():
    condition = em.In("weather", "sunny")
    assert condition.holds(world_of({"weather": frozenset({"sunny", "mild"})}))
    assert not condition.holds(world_of({"weather": frozenset({"cloudy"})}))
    assert not condition.holds(world_of({"weather": frozenset()}))


def test_not_inverts():
    condition = em.Not(em.Eq("ac", "on"))
    assert condition.holds(world_of({"ac": "off"}))
    assert not condition.holds(world_of({"ac": "on"}))


@pytest.mark.parametrize(
    ("start", "stop", "now", "expected"),
    [
        (time(8), time(22), time(15), True),
        (time(8), time(22), time(8), True),
        (time(8), time(22), time(22), False),
        (time(8), time(22), time(3), False),
        (time(8), time(8), time(8), False),
        (time(22), time(8), time(23), True),
        (time(22), time(8), time(3), True),
        (time(22), time(8), time(8), False),
        (time(22), time(8), time(15), False),
    ],
    ids=["inside", "at-start", "at-stop", "before", "empty", "wrap-evening", "wrap-morning", "wrap-at-stop", "wrap-outside"],
)
def test_during_is_half_open_and_wraps_midnight(start, stop, now, expected):
    with freeze_time(datetime.combine(T0.date(), now, tzinfo=UTC)):
        assert em.During(start, stop).holds(world_of({})) is expected


SCENE = em.SnapShot((), T1)


def test_snapshots_active_until_deadline(runtime, clock):
    runtime.save_snapshot("s", SCENE)
    assert runtime.snapshot_active("s") is True
    assert runtime.snapshot_active("missing") is False
    clock.move_to(T1)
    assert runtime.snapshot_active("s") is True
    clock.move_to(T1 + timedelta(seconds=1))
    assert runtime.snapshot_active("s") is False


def test_snapshots_get_pops_live_payload(runtime):
    runtime.save_snapshot("s", SCENE)
    assert runtime.pop_snapshot("s") == SCENE
    assert runtime.pop_snapshot("s") is None


def test_snapshots_get_expired_returns_none_but_pops(runtime, clock):
    runtime.save_snapshot("s", em.SnapShot((), T0))
    clock.move_to(T1)
    assert runtime.pop_snapshot("s") is None
    assert runtime.snapshots() == {}


def test_snapshots_lists_only_live(runtime, clock):
    runtime.save_snapshot("live", SCENE)
    runtime.save_snapshot("dead", em.SnapShot((), T0))
    clock.move_to(T0 + timedelta(minutes=1))
    assert runtime.snapshots() == {"live": SCENE}


def test_evaluate_keeps_only_rules_whose_condition_holds(runtime):
    rule = em.Rule((em.Step(em.Eq("ac", "on"), LIGHT),))
    assert runtime.evaluate([rule], read=read_from({"ac": "on"}), force=True) == (em.Report(rule, (LIGHT,)),)
    assert runtime.evaluate([rule], read=read_from({"ac": "off"}), force=True) == (em.Drop(rule),)


OPEN = em.Eq("door", "open")
CLOSED = em.Eq("door", "closed")


def test_a_delayed_automation_defers_and_cancels_on_its_cancel_condition(runtime):
    watch = em.Watch(OPEN, em.Rule((em.Step(em.And(), LIGHT),)), delay=timedelta(minutes=5), cancel=CLOSED)
    assert runtime.evaluate([watch], read=read_from({"door": "open"}), force=True) == (em.Defer(watch, T0 + timedelta(minutes=5)),)
    assert runtime.evaluate([watch], read=read_from({"door": "closed"}), force=True) == (em.Cancel(watch),)
    assert runtime.evaluate([watch], read=read_from({"door": "ajar"}), force=True) == (em.Drop(watch),)


def test_deferred_rechecks_its_conditions_when_run(runtime, clock):
    watch = em.Watch(OPEN, em.Rule((em.Step(em.Eq("ac", "on"), LIGHT),)), delay=timedelta(minutes=5))

    clock.move_to(T0)
    (deferred,) = runtime.evaluate([watch], read=read_from({"door": "open"}), force=True)

    clock.move_to(T0 + timedelta(minutes=4, seconds=59))
    assert runtime.evaluate([deferred], read=read_from({"ac": "on", "switch": "on"}), force=True) == (em.Cooldown(watch),)

    clock.move_to(T0 + timedelta(minutes=5))
    assert runtime.evaluate([deferred], read=read_from({"ac": "on", "switch": "on"}), force=True) == (
        em.Report(deferred.watch, commands=(em.Command(subject="light", value="on", tag=None),)),
    )


def test_cooldown_silences_a_repeat_within_the_window(runtime, clock):
    watch = em.Watch(OPEN, em.Rule((em.Step(em.And(), LIGHT),)), cooldown=timedelta(seconds=10))
    open_door = read_from({"door": "open"})
    assert runtime.evaluate([watch], read=open_door, force=True) == (em.Report(watch, (LIGHT,)),)
    clock.move_to(T0 + timedelta(seconds=5))
    assert runtime.evaluate([watch], read=open_door, force=True) == (em.Cooldown(watch),)
    clock.move_to(T0 + timedelta(seconds=10))
    assert runtime.evaluate([watch], read=open_door, force=True) == (em.Report(watch, (LIGHT,)),)


def test_a_change_during_cooldown_is_reported_after_it(runtime, clock):
    watch = em.Watch(em.Changed("door"), em.Rule((em.Step(em.And(), LIGHT),)), cooldown=timedelta(seconds=10))
    door = {"door": "closed"}
    assert runtime.evaluate([watch], read=read_from(door), force=True) == (em.Report(watch, (LIGHT,)),)
    clock.move_to(T0 + timedelta(seconds=5))
    door["door"] = "open"
    assert runtime.evaluate([watch], read=read_from(door), force=True) == (em.Cooldown(watch),)
    clock.move_to(T0 + timedelta(seconds=10))
    assert runtime.evaluate([watch], read=read_from(door), force=True) == (em.Report(watch, (LIGHT,)),)
    clock.move_to(T0 + timedelta(seconds=20))
    assert runtime.evaluate([watch], read=read_from(door), force=True) == (em.Drop(watch),)


def test_a_held_change_does_not_leak_into_rules(runtime, clock):
    watch = em.Watch(em.Changed("door"), em.Rule((em.Step(em.And(), LIGHT),)), cooldown=timedelta(seconds=10))
    rule = em.Rule((em.Step(em.Changed("door"), LIGHT),))
    door = {"door": "closed"}
    runtime.evaluate([watch], read=read_from(door), force=True)
    clock.move_to(T0 + timedelta(seconds=5))
    door["door"] = "open"
    assert runtime.evaluate([watch], read=read_from(door), force=True) == (em.Cooldown(watch),)
    assert runtime.evaluate([rule], read=read_from(door), force=True) == (em.Drop(rule),)


def _gate(runtime, commands, *, force):
    (outcome,) = runtime.evaluate([em.Action(commands)], read=read_from({}), force=force)
    match outcome:
        case em.Report(_, passed):
            return passed
        case _:
            return ()


def test_evaluate_forced_passes_all_without_recording(overriding_runtime):
    overriding_runtime.save_snapshot("s", em.SnapShot((), T1))
    cmd = em.Command("light", "on", tag=ALICE)
    assert _gate(overriding_runtime, (cmd,), force=True) == (cmd,)
    assert overriding_runtime.snapshots()["s"].routine == ()


def test_evaluate_passes_all_when_no_snapshot_active(overriding_runtime):
    cmd = em.Command("light", "on", tag=ALICE)
    assert _gate(overriding_runtime, (cmd,), force=False) == (cmd,)


def test_evaluate_suppresses_non_bypass_while_override_snapshot_active(overriding_runtime):
    overriding_runtime.save_snapshot("s", em.SnapShot((), T1))
    cmd = em.Command("light", "on", tag=ALICE)
    assert _gate(overriding_runtime, (cmd,), force=False) == ()


def test_evaluate_ignores_snapshots_under_other_keys(overriding_runtime):
    overriding_runtime.save_snapshot("entrance_sensor", em.SnapShot((), T1))
    cmd = em.Command("light", "on", tag=ALICE)
    assert _gate(overriding_runtime, (cmd,), force=False) == (cmd,)


def test_evaluate_records_bypass_into_override_snapshot_only(overriding_runtime):
    overriding_runtime.save_snapshot("s", em.SnapShot((em.Command("light", "off"),), T1))
    overriding_runtime.save_snapshot("other", em.SnapShot((), T1))
    cmd = em.Command("light", "on", tag=em.Always())
    assert _gate(overriding_runtime, (cmd,), force=False) == (cmd,)
    assert overriding_runtime.snapshots()["s"].routine == (cmd,)
    assert overriding_runtime.snapshots()["other"].routine == ()


def test_apply_override_passes_all_when_no_snapshot_active(overriding_runtime):
    cmd = em.Command("light", "on", tag=ALICE)
    assert overriding_runtime.apply_override((cmd,)) == (cmd,)


def test_apply_override_suppresses_non_bypass_while_override_snapshot_active(overriding_runtime):
    overriding_runtime.save_snapshot("s", em.SnapShot((), T1))
    cmd = em.Command("light", "on", tag=ALICE)
    assert overriding_runtime.apply_override((cmd,)) == ()
    assert overriding_runtime.snapshots()["s"].routine == ()


def test_apply_override_records_bypass_into_override_snapshot_only(overriding_runtime):
    overriding_runtime.save_snapshot("s", em.SnapShot((em.Command("light", "off"),), T1))
    overriding_runtime.save_snapshot("other", em.SnapShot((), T1))
    cmd = em.Command("light", "on", tag=em.Always())
    assert overriding_runtime.apply_override((cmd,)) == (cmd,)
    assert overriding_runtime.snapshots()["s"].routine == (cmd,)
    assert overriding_runtime.snapshots()["other"].routine == ()


def test_apply_override_ignores_expired_and_other_snapshots(overriding_runtime, runtime):
    cmd = em.Command("light", "on", tag=ALICE)
    overriding_runtime.save_snapshot("entrance_sensor", em.SnapShot((), T1))
    overriding_runtime.save_snapshot("s", em.SnapShot((), T0 - timedelta(seconds=1)))
    assert overriding_runtime.apply_override((cmd,)) == (cmd,)
    runtime.save_snapshot("s", em.SnapShot((), T1))
    assert runtime.apply_override((cmd,)) == (cmd,)


def test_changed_reports_old_and_new_across_evaluations(runtime):
    world = {"temp": 70}
    rule = em.Rule((em.Step(em.Changed("temp"), LIGHT),))
    assert runtime.evaluate([rule], read=read_from(world), force=True) == (em.Report(rule, (LIGHT,)),)
    assert runtime.evaluate([rule], read=read_from(world), force=True) == (em.Drop(rule),)
    world["temp"] = 71
    assert runtime.world(read_from(world)).changed("temp") == (70, 71)


def _seeded(runtime, world):
    runtime.evaluate([em.Rule((em.Step(em.Has("temp"), LIGHT),))], read=read_from(world), force=True)


def test_changed_uses_the_runtime_comparator(clock):
    world = {"temp": 70}
    runtime = e.Runtime(UTC, same=lambda old, new: old is not None and abs(old - new) <= 1)
    _seeded(runtime, world)
    world["temp"] = 71
    assert runtime.world(read_from(world)).changed("temp") is None
    world["temp"] = 73
    assert runtime.world(read_from(world)).changed("temp") == (70, 73)


def test_changed_compares_with_equality_by_default(runtime):
    world = {"temp": 70}
    _seeded(runtime, world)
    world["temp"] = 71
    assert runtime.world(read_from(world)).changed("temp") == (70, 71)


def test_changed_is_unknown_for_an_unreadable_subject(runtime):
    rule = em.Rule((em.Step(em.Changed("ghost"), LIGHT),))
    assert runtime.evaluate([rule], read=read_from({}), force=True) == (em.Drop(rule),)


def test_change_supplies_the_old_value_on_the_first_read(runtime):
    rule = em.Rule((em.Step(em.Changed("door"), LIGHT),))
    door = {"door": "open"}
    assert runtime.world(read_from(door), (em.Change("door", "closed", "open"),)).changed("door") == ("closed", "open")
    assert runtime.evaluate([rule], read=read_from(door), changes=(em.Change("door", "closed", "open"),), force=True) == (em.Report(rule, (LIGHT,)),)


def test_change_overrides_the_remembered_value(runtime):
    rule = em.Rule((em.Step(em.Changed("door"), LIGHT),))
    door = {"door": "open"}
    runtime.evaluate([rule], read=read_from(door), force=True)
    assert runtime.evaluate([rule], read=read_from(door), changes=(em.Change("door", "open", "open"),), force=True) == (em.Drop(rule),)
    assert runtime.evaluate([rule], read=read_from(door), changes=(em.Change("door", "closed", "open"),), force=True) == (em.Report(rule, (LIGHT,)),)


def test_a_change_with_old_none_falls_back_to_the_remembered_value(runtime):
    rule = em.Rule((em.Step(em.Changed("button"), LIGHT),))
    runtime.evaluate([rule], read=read_from({"button": 2}), force=True)
    assert runtime.world(read_from({"button": 1}), (em.Change("button", None, 1),)).changed("button") == (2, 1)
    assert runtime.evaluate([rule], read=read_from({"button": 2}), changes=(em.Change("button", None, 2),), force=True) == (em.Drop(rule),)


def test_change_only_applies_to_its_subject(runtime):
    world = {"door": "open", "temp": 70}
    runtime.evaluate([em.Rule((em.Step(em.Eq("temp", 70), LIGHT),))], read=read_from(world), force=True)
    assert runtime.world(read_from(world), (em.Change("door", "closed", "open"),)).changed("temp") is None


def test_a_held_value_wins_over_change(runtime, clock):
    watch = em.Watch(em.Changed("door"), em.Rule((em.Step(em.And(), LIGHT),)), cooldown=timedelta(seconds=10))
    door = {"door": "closed"}
    runtime.evaluate([watch], read=read_from(door), force=True)
    clock.move_to(T0 + timedelta(seconds=5))
    door["door"] = "open"
    assert runtime.evaluate([watch], read=read_from(door), changes=(em.Change("door", "closed", "open"),), force=True) == (em.Cooldown(watch),)
    clock.move_to(T0 + timedelta(seconds=10))
    assert runtime.evaluate([watch], read=read_from(door), changes=(em.Change("door", "open", "open"),), force=True) == (em.Report(watch, (LIGHT,)),)


def test_several_subjects_can_change_in_one_event(runtime):
    rule = em.Rule((em.Step(em.Changed("device"), LIGHT),))
    changes = (em.Change("door", "closed", "open"), em.Change("device", "closed", "open"))
    assert runtime.evaluate([rule], read=read_from({}), changes=changes, force=True) == (em.Report(rule, (LIGHT,)),)


def test_a_subject_takes_its_old_value_from_the_event_not_the_last_read(runtime):
    rule = em.Rule((em.Step(em.Changed("device"), LIGHT),))
    assert runtime.evaluate([rule], read=read_from({}), changes=(em.Change("device", 69, 70),), force=True) == (em.Report(rule, (LIGHT,)),)
    assert runtime.evaluate([rule], read=read_from({}), changes=(em.Change("device", 65, 70),), force=True) == (em.Report(rule, (LIGHT,)),)


def test_an_unchanged_event_does_not_change_its_subject(runtime):
    rule = em.Rule((em.Step(em.Changed("device"), LIGHT),))
    assert runtime.evaluate([rule], read=read_from({}), changes=(em.Change("device", "on", "on"),), force=True) == (em.Drop(rule),)


def test_a_plain_read_seeds_the_memory_for_changed(runtime):
    runtime.evaluate([em.Rule((em.Step(em.Eq("switch", "on"), LIGHT),))], read=read_from({"switch": "on"}), force=True)
    rule = em.Rule((em.Step(em.Changed("switch"), LIGHT),))
    assert runtime.evaluate([rule], read=read_from({"switch": "on"}), force=True) == (em.Drop(rule),)


def test_a_change_new_overrides_the_read_value_and_is_remembered(runtime):
    rule = em.Rule((em.Step(em.Eq("door", "open"), LIGHT),))
    stale = read_from({"door": "closed"})
    assert runtime.evaluate([rule], read=stale, changes=(em.Change("door", "closed", "open"),), force=True) == (em.Report(rule, (LIGHT,)),)
    assert runtime.world(stale).changed("door") == ("open", "closed")
