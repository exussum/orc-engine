"""A generic rule evaluator: a rule's steps apply when their conditions hold.

The caller supplies the world as a `Read` callback (and, per event, the reported `Changes`) and
performs the returned commands. `Runtime` reads the clock as `datetime.now(tz)`, holds in-memory
cooldown, snapshot and last-read state, calls no scheduler, and starts no threads.
"""

from collections import defaultdict
from collections.abc import Hashable, Iterable
from datetime import datetime, tzinfo
from threading import RLock
from typing import Any, NoReturn, overload

from orc_engine import model as m


class Runtime:
    def __init__(self, tz: tzinfo, *, bypass: Any = None, override_key: str | None = None) -> None:
        self._tz = tz
        self._snapshots: dict[str, tuple[Any, datetime]] = {}
        self._last: defaultdict[m.Subject, m.Value] = defaultdict(lambda: None)
        self._last_fired: dict[m.Watch[Any], datetime] = {}
        self._lock = RLock()
        self._bypass = bypass
        self._override_key = override_key

    def save_snapshot(self, key: str, payload: Any, deadline: datetime) -> None:
        with self._lock:
            self._snapshots[key] = (payload, deadline)

    def snapshot_active(self, key: str) -> bool:
        with self._lock:
            entry = self._snapshots.get(key)
            return bool(entry and datetime.now(self._tz) <= entry[1])

    def read_snapshot(self, key: str) -> Any:
        with self._lock:
            entry = self._snapshots.get(key)
            return entry[0] if entry and datetime.now(self._tz) <= entry[1] else None

    def pop_snapshot(self, key: str) -> Any:
        with self._lock:
            entry = self._snapshots.pop(key, None)
            return entry[0] if entry and datetime.now(self._tz) <= entry[1] else None

    def snapshots(self) -> dict[str, Any]:
        with self._lock:
            now = datetime.now(self._tz)
            return {key: payload for key, (payload, deadline) in self._snapshots.items() if now <= deadline}

    @overload
    def evaluate[C: Hashable](
        self,
        items: Iterable[m.Rule[C] | m.Action[C] | m.Deferred[C]],
        *,
        read: m.Read,
        force: bool,
        changes: m.Changes | None = None,
    ) -> tuple[m.Report[C], ...]: ...

    @overload
    def evaluate[C: Hashable](
        self, items: Iterable[m.Item[C]], *, read: m.Read, force: bool, changes: m.Changes | None = None
    ) -> tuple[m.Outcome[C], ...]: ...

    def evaluate[C: Hashable](
        self, items: Iterable[m.Item[C]], *, read: m.Read, force: bool, changes: m.Changes | None = None
    ) -> tuple[m.Outcome[C], ...]:
        with self._lock:
            seen: dict[m.Subject, m.Value] = {}
            world = _Tracking(read, changes or nothing, self._tz, self._last, seen)
            when = world.now()
            out: list[m.Outcome[C]] = []
            for item in items:
                match item:
                    case m.Deferred(watch, _):
                        out.append(m.Report(item, self._applied_once(watch, when, world, force)))
                    case m.Watch():
                        out.append(self._watched(item, when, world, force))
                    case m.Action(plain):
                        out.append(m.Report(item, self._applied(m.Rule(tuple(m.Step(m.And(), c) for c in plain)), when, world, force)))
                    case _:
                        out.append(m.Report(item, self._applied(item, when, world, force)))
            self._last.update(seen)
            return tuple(out)

    def world(self, read: m.Read, *, changes: m.Changes | None = None) -> m.World:
        return _Tracking(read, changes or nothing, self._tz, self._last, {})

    def _watched[C: Hashable](self, watch: m.Watch[C], now: datetime, world: m.World, force: bool) -> m.Outcome[C]:
        if not watch.condition.holds(world):
            if watch.delay and watch.cancel.holds(world):
                return m.Cancel(watch)
            return m.Report(watch, ())
        if watch.delay:
            return m.Deferred(watch, now + watch.delay)
        return m.Report(watch, self._applied_once(watch, now, world, force))

    def _applied_once[C: Hashable](self, watch: m.Watch[C], now: datetime, world: m.World, force: bool) -> tuple[m.Command[Any, C], ...]:
        last = self._last_fired.get(watch)
        if watch.cooldown and last is not None and now - last < watch.cooldown:
            return ()
        commands = self._applied(watch.rule, now, world, force)
        if commands:
            self._last_fired[watch] = now
        return commands

    def _applied[C: Hashable](self, rule: m.Rule[C], now: datetime, world: m.World, force: bool) -> tuple[m.Command[Any, C], ...]:
        override_key = self._override_key
        snapshot = self._snapshots.get(override_key) if override_key is not None else None
        active = snapshot is not None and now <= snapshot[1]
        out: list[m.Command[Any, C]] = []
        for step in rule.steps:
            if not step.holds(world):
                continue
            command = step.command
            if not force:
                if command.tag == self._bypass:
                    if override_key is not None and snapshot is not None and active:
                        payload, deadline = snapshot
                        merged = {c.subject: c for c in payload.routine}
                        merged[command.subject] = command
                        self._snapshots[override_key] = (payload._replace(routine=tuple(merged.values())), deadline)
                elif active:
                    continue
            out.append(command)
        return tuple(out)


class _Tracking:
    def __init__(self, read: m.Read, changes: m.Changes, tz: tzinfo, last: defaultdict[m.Subject, m.Value], seen: dict[m.Subject, m.Value]) -> None:
        self._read = read
        self._changes = changes
        self._tz = tz
        self._last = last
        self._seen = seen

    def read(self, subject: m.Subject) -> m.Value:
        value = self._read(subject)
        self._seen[subject] = value
        return value

    def changed(self, subject: m.Subject) -> tuple[m.Value, m.Value] | None:
        try:
            old, new = self._changes(subject)
        except KeyError:
            try:
                old, new = None, self.read(subject)
            except KeyError:
                return None
        if old is None:
            old = self._last[subject]
        self._seen[subject] = new
        return None if old == new else (old, new)

    def now(self) -> datetime:
        return datetime.now(self._tz)


def nothing(subject: m.Subject) -> NoReturn:
    raise KeyError(subject)
