"""A generic rule evaluator: a rule's steps apply when their conditions hold.

The caller supplies the world as a `Read` callback (and, per event, the reported `Change`s) and
performs the returned commands. `Runtime` reads the clock as `datetime.now(tz)`, holds in-memory
cooldown, snapshot and last-read state, calls no scheduler, and starts no threads.
"""

from collections import defaultdict
from collections.abc import Callable, Hashable, Iterable
from datetime import datetime, tzinfo, timezone
from operator import eq
from threading import RLock

from orc_engine import model as m

min_dt = datetime.min.replace(tzinfo=timezone.utc)


class Runtime[C: Hashable = Hashable]:
    def __init__(
        self,
        tz: tzinfo,
        *,
        bypass: m.Condition = m.Always(),
        override_key: str | None = None,
        same: Callable[[m.Value, m.Value], bool] = eq,
    ) -> None:
        self._tz = tz
        self._same = same

        self._bypass = bypass
        self._snapshots: dict[str, m.SnapShot[C]] = {}

        self._previous: defaultdict[m.Subject, m.Value] = defaultdict(lambda: None)
        self._fired_at: dict[m.Watch[C], datetime] = {}
        self._held: defaultdict[m.Watch[C], dict[m.Subject, m.Value]] = defaultdict(dict)

        self._lock = RLock()
        self._override_key = override_key

    def save_snapshot(self, key: str, snapshot: m.SnapShot[C]) -> None:
        with self._lock:
            self._snapshots[key] = snapshot

    def snapshot_active(self, key: str) -> bool:
        with self._lock:
            snapshot = self._snapshots.get(key)
            return bool(snapshot and datetime.now(self._tz) <= snapshot.end)

    def pop_snapshot(self, key: str) -> m.SnapShot[C] | None:
        with self._lock:
            snapshot = self._snapshots.pop(key, None)
            return snapshot if snapshot and datetime.now(self._tz) <= snapshot.end else None

    def snapshots(self) -> dict[str, m.SnapShot[C]]:
        with self._lock:
            now = datetime.now(self._tz)
            return {key: snapshot for key, snapshot in self._snapshots.items() if now <= snapshot.end}

    def apply_override(self, commands: Iterable[m.Command[m.Condition, C]]) -> tuple[m.Command[m.Condition, C], ...]:
        with self._lock:
            key = self._override_key
            snapshot = self._snapshots.get(key) if key is not None else None
            active = snapshot is not None and datetime.now(self._tz) <= snapshot.end
            out: list[m.Command[m.Condition, C]] = []
            for command in commands:
                if command.tag == self._bypass:
                    if key is not None and snapshot is not None and active:
                        merged = {c.subject: c for c in snapshot.routine}
                        merged[command.subject] = command
                        self._snapshots[key] = snapshot._replace(routine=tuple(merged.values()))
                elif active:
                    continue
                out.append(command)
            return tuple(out)

    def evaluate(self, items: Iterable[m.Item[C]], *, read: m.Read, force: bool, changes: Iterable[m.Change] = ()) -> tuple[m.Outcome[C], ...]:
        with self._lock:
            world = WrappedWorld(self, read, changes)
            when = world.now()
            out: list[m.Outcome[C]] = []
            for item in items:
                match item:
                    case m.Defer(watch, _):
                        out.append(self._applied_once(watch, when, world, force))
                    case m.Watch():
                        own = world.for_watch(item)
                        outcome = self._watched(item, when, own, force)
                        match outcome:
                            case m.Cooldown():
                                own.hold()
                            case _:
                                own.release()
                        out.append(outcome)
                    case m.Action(plain):
                        rule = m.Rule(tuple(m.Step(m.And(), c) for c in plain))
                        out.append(self._reported(item, self._applied(rule, when, world, force)))
                    case m.Rule():
                        out.append(self._reported(item, self._applied(item, when, world, force)))
                    case m.Drop() | m.Cooldown():
                        pass
                    case _:
                        raise Exception(f"Unexpected type: {item}")
            world.commit()
            return tuple(out)

    def world(self, read: m.Read, changes: Iterable[m.Change] = ()) -> m.World:
        return WrappedWorld(self, read, changes)

    @staticmethod
    def _reported(item: m.Source[C], commands: tuple[m.Command[m.Condition, C], ...]) -> m.Outcome[C]:
        return m.Report(item, commands) if commands else m.Drop(item)

    def _watched(self, watch: m.Watch[C], now: datetime, world: m.World, force: bool) -> m.Outcome[C]:
        if not watch.condition.holds(world):
            if watch.delay and watch.cancel.holds(world):
                return m.Cancel(watch)
            else:
                return m.Drop(watch)
        if watch.delay:
            self._fired_at[watch] = now
            return m.Defer(watch, now + watch.delay)
        return self._applied_once(watch, now, world, force)

    def _applied_once(self, watch: m.Watch[C], now: datetime, world: m.World, force: bool) -> m.Outcome[C]:
        commands = self._applied(watch.rule, now, world, force)
        if not commands:
            return m.Drop(watch)
        if now - self._fired_at.get(watch, min_dt) < max(watch.cooldown, watch.delay):
            return m.Cooldown(watch)
        self._fired_at[watch] = now
        return m.Report(watch, commands)

    def _applied(self, rule: m.Rule[C], now: datetime, world: m.World, force: bool) -> tuple[m.Command[m.Condition, C], ...]:
        override_key = self._override_key
        snapshot = self._snapshots.get(override_key) if override_key is not None else None
        active = snapshot is not None and now <= snapshot.end
        out: list[m.Command[m.Condition, C]] = []
        for step in rule.steps:
            if not step.holds(world):
                continue
            command = step.command
            if not force:
                if command.tag == self._bypass:
                    if override_key is not None and snapshot is not None and active:
                        merged = {c.subject: c for c in snapshot.routine}
                        merged[command.subject] = command
                        self._snapshots[override_key] = snapshot._replace(routine=tuple(merged.values()))
                elif active:
                    continue
            out.append(command)
        return tuple(out)


class WrappedWorld[C: Hashable = Hashable]:
    def __init__(self, runtime: Runtime[C], read: m.Read, changes: Iterable[m.Change] = ()) -> None:
        self._runtime = runtime
        self._read = read
        self._changes = {change.subject: change for change in changes}
        self._seen: dict[m.Subject, m.Value] = {}
        self._watch: m.Watch[C] | None = None
        self._olds: dict[m.Subject, m.Value] = {}

    def for_watch(self, watch: m.Watch[C]) -> WrappedWorld[C]:
        world = WrappedWorld(self._runtime, self._read, self._changes.values())
        world._seen = self._seen
        world._watch = watch
        return world

    def read(self, subject: m.Subject) -> m.Value:
        value = self._changes[subject].new if subject in self._changes else self._read(subject)
        self._seen[subject] = value
        return value

    def changed(self, subject: m.Subject) -> tuple[m.Value, m.Value] | None:
        try:
            new = self.read(subject)
        except KeyError:
            return None
        held = self._runtime._held.get(self._watch, {}) if self._watch is not None else {}
        if subject in held:
            old = held[subject]
        else:
            if subject in self._changes and self._changes[subject].old is not None:
                old = self._changes[subject].old
            else:
                old = self._runtime._previous[subject]
        self._olds[subject] = old
        return None if self._runtime._same(old, new) else (old, new)

    def now(self) -> datetime:
        return datetime.now(self._runtime._tz)

    def commit(self) -> None:
        self._runtime._previous.update(self._seen)

    def hold(self) -> None:
        if self._watch is not None:
            self._runtime._held[self._watch].update(self._olds)

    def release(self) -> None:
        if self._watch is not None:
            self._runtime._held.pop(self._watch, None)
