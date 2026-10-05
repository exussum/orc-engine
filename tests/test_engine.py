from datetime import UTC, datetime, time, timedelta

import pytest
from freezegun import freeze_time

from orc_engine import engine as e
from orc_engine import model as em


LIGHT = em.Command("light", "on")
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
    return e.Runtime(UTC, bypass="SYSTEM", override_key="s")


def test_is_holds_reads_the_world():
    condition = em.Eq("ac", "on")
    assert condition.holds(world_of({"ac": "on"}))
    assert not condition.holds(world_of({"ac": "off"}))


def test_in_holds_when_value_is_among_the_reading():
    condition = em.In("weather", "sunny")
    assert condition.holds(world_of({"weather": frozenset({"sunny", "mild"})}))
    assert not condition.holds(world_of({"weather": frozenset({"cloudy"})}))
    assert not condition.holds(world_of({"weather": frozenset()}))


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


def test_snapshots_active_until_deadline(runtime, clock):
    runtime.save_snapshot("s", "scene", T1)
    assert runtime.snapshot_active("s") is True
    assert runtime.snapshot_active("missing") is False
    clock.move_to(T1)
    assert runtime.snapshot_active("s") is True
    clock.move_to(T1 + timedelta(seconds=1))
    assert runtime.snapshot_active("s") is False



def test_snapshots_get_pops_live_payload(runtime):
    runtime.save_snapshot("s", "scene", T1)
    assert runtime.pop_snapshot("s") == "scene"
    assert runtime.pop_snapshot("s") is None


def test_snapshots_get_expired_returns_none_but_pops(runtime, clock):
    runtime.save_snapshot("s", "scene", T0)
    clock.move_to(T1)
    assert runtime.pop_snapshot("s") is None
    assert runtime.snapshots() == {}


def test_snapshots_lists_only_live(runtime, clock):
    runtime.save_snapshot("live", "a", T1)
    runtime.save_snapshot("dead", "b", T0)
    clock.move_to(T0 + timedelta(minutes=1))
    assert runtime.snapshots() == {"live": "a"}


def test_evaluate_keeps_only_rules_whose_condition_holds(runtime):
    rule = em.Rule((em.Step(em.Eq("ac", "on"), LIGHT),))
    assert runtime.evaluate([rule], read=read_from({"ac": "on"}), force=True) == (em.Report(rule, (LIGHT,)),)
    assert runtime.evaluate([rule], read=read_from({"ac": "off"}), force=True) == (em.Report(rule, ()),)


OPEN = em.Eq("door", "open")
CLOSED = em.Eq("door", "closed")


def test_a_delayed_automation_defers_and_cancels_on_its_cancel_condition(runtime):
    watch = em.Watch(OPEN, em.Rule((em.Step(em.And(), LIGHT),)), delay=timedelta(minutes=5), cancel=CLOSED)
    assert runtime.evaluate([watch], read=read_from({"door": "open"}), force=True) == (em.Deferred(watch, T0 + timedelta(minutes=5)),)
    assert runtime.evaluate([watch], read=read_from({"door": "closed"}), force=True) == (em.Cancel(watch),)
    assert runtime.evaluate([watch], read=read_from({"door": "ajar"}), force=True) == (em.Report(watch, ()),)


def test_deferred_rechecks_its_conditions_when_run(runtime):
    watch = em.Watch(OPEN, em.Rule((em.Step(em.Eq("ac", "on"), LIGHT),)), delay=timedelta(minutes=5))
    (deferred,) = runtime.evaluate([watch], read=read_from({"door": "open"}), force=True)
    assert runtime.evaluate([deferred], read=read_from({"ac": "off"}), force=True) == (em.Report(deferred, ()),)


def test_cooldown_silences_a_repeat_within_the_window(runtime, clock):
    watch = em.Watch(OPEN, em.Rule((em.Step(em.And(), LIGHT),)), cooldown=timedelta(seconds=10))
    open_door = read_from({"door": "open"})
    assert runtime.evaluate([watch], read=open_door, force=True) == (em.Report(watch, (LIGHT,)),)
    clock.move_to(T0 + timedelta(seconds=5))
    assert runtime.evaluate([watch], read=open_door, force=True) == (em.Report(watch, ()),)
    clock.move_to(T0 + timedelta(seconds=10))
    assert runtime.evaluate([watch], read=open_door, force=True) == (em.Report(watch, (LIGHT,)),)


def _gate(runtime, commands, *, force):
    (report,) = runtime.evaluate([em.Action(commands)], read=read_from({}), force=force)
    return report.commands


def test_evaluate_forced_passes_all_without_recording(overriding_runtime):
    overriding_runtime.save_snapshot("s", em.SnapShot((), T1), T1)
    cmd = em.Command("light", "on", tag="Alice")
    assert _gate(overriding_runtime, (cmd,), force=True) == (cmd,)
    assert overriding_runtime.snapshots()["s"].routine == ()


def test_evaluate_passes_all_when_no_snapshot_active(overriding_runtime):
    cmd = em.Command("light", "on", tag="Alice")
    assert _gate(overriding_runtime, (cmd,), force=False) == (cmd,)


def test_evaluate_suppresses_non_bypass_while_override_snapshot_active(overriding_runtime):
    overriding_runtime.save_snapshot("s", em.SnapShot((), T1), T1)
    cmd = em.Command("light", "on", tag="Alice")
    assert _gate(overriding_runtime, (cmd,), force=False) == ()


def test_evaluate_ignores_snapshots_under_other_keys(overriding_runtime):
    overriding_runtime.save_snapshot("entrance_sensor", em.SnapShot((), T1), T1)
    cmd = em.Command("light", "on", tag="Alice")
    assert _gate(overriding_runtime, (cmd,), force=False) == (cmd,)


def test_evaluate_records_bypass_into_override_snapshot_only(overriding_runtime):
    overriding_runtime.save_snapshot("s", em.SnapShot((em.Command("light", "off"),), T1), T1)
    overriding_runtime.save_snapshot("other", em.SnapShot((), T1), T1)
    cmd = em.Command("light", "on", tag="SYSTEM")
    assert _gate(overriding_runtime, (cmd,), force=False) == (cmd,)
    assert overriding_runtime.snapshots()["s"].routine == (cmd,)
    assert overriding_runtime.snapshots()["other"].routine == ()


def test_changed_reports_old_and_new_across_evaluations(runtime):
    world = {"temp": 70}
    (report,) = runtime.evaluate([em.Rule((em.Step(em.Changed("temp"), LIGHT),))], read=read_from(world), force=True)
    assert report.commands == (LIGHT,)
    (report,) = runtime.evaluate([em.Rule((em.Step(em.Changed("temp"), LIGHT),))], read=read_from(world), force=True)
    assert report.commands == ()
    world["temp"] = 71
    assert runtime.world(read_from(world)).changed("temp") == (70, 71)


def test_changed_is_unknown_for_an_unreadable_subject(runtime):
    (report,) = runtime.evaluate([em.Rule((em.Step(em.Changed("ghost"), LIGHT),))], read=read_from({}), force=True)
    assert report.commands == ()


def test_a_plain_read_seeds_the_memory_for_changed(runtime):
    runtime.evaluate([em.Rule((em.Step(em.Eq("switch", "on"), LIGHT),))], read=read_from({"switch": "on"}), force=True)
    (report,) = runtime.evaluate([em.Rule((em.Step(em.Changed("switch"), LIGHT),))], read=read_from({"switch": "on"}), force=True)
    assert report.commands == ()
