from abc import ABC, abstractmethod
from collections.abc import Callable, Collection, Hashable
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import NamedTuple, Protocol

type Value = Hashable
type Read = Callable[[Subject], Value]
type Source[C: Hashable = Hashable] = Rule[C] | Action[C] | Watch[C]
type Item[C: Hashable = Hashable] = Source[C] | Defer[C] | Drop[C] | Cooldown[C]
type Outcome[C: Hashable = Hashable] = Report[C] | Defer[C] | Cancel[C] | Drop[C] | Cooldown[C]


class Subject:
    pass


@dataclass(frozen=True)
class Command[T: Condition = Condition, C: Hashable = Hashable]:
    subject: C
    value: Value
    tag: T | None = None


class World(Protocol):
    def read(self, subject: Subject) -> Value: ...

    def changed(self, subject: Subject) -> tuple[Value, Value] | None: ...

    def now(self) -> datetime: ...


class Condition(ABC):
    @abstractmethod
    def holds(self, world: World) -> bool: ...


@dataclass(frozen=True)
class Never(Condition):
    def holds(self, world: World) -> bool:
        return False


NEVER = Never()


@dataclass(frozen=True)
class Always(Condition):
    def holds(self, world: World) -> bool:
        return True


@dataclass(frozen=True)
class And(Condition):
    conditions: tuple[Condition, ...]

    def __init__(self, *conditions: Condition) -> None:
        object.__setattr__(self, "conditions", conditions)

    def holds(self, world: World) -> bool:
        return all(cond.holds(world) for cond in self.conditions)


@dataclass(frozen=True)
class Or(Condition):
    conditions: tuple[Condition, ...]

    def __init__(self, *conditions: Condition) -> None:
        object.__setattr__(self, "conditions", conditions)

    def holds(self, world: World) -> bool:
        return any(cond.holds(world) for cond in self.conditions)


@dataclass(frozen=True)
class Not(Condition):
    condition: Condition

    def holds(self, world: World) -> bool:
        return not self.condition.holds(world)


@dataclass(frozen=True)
class Eq(Condition):
    subject: Subject
    value: Value

    def holds(self, world: World) -> bool:
        return world.read(self.subject) == self.value


@dataclass(frozen=True)
class In(Condition):
    subject: Subject
    value: Value

    def holds(self, world: World) -> bool:
        values = world.read(self.subject)
        if not isinstance(values, Collection):
            raise TypeError(f"{self.subject} read {values!r}, not a collection")
        return self.value in values


@dataclass(frozen=True)
class Has(Condition):
    subject: Subject

    def holds(self, world: World) -> bool:
        return world.read(self.subject) is not None


@dataclass(frozen=True)
class Changed(Condition):
    subject: Subject

    def holds(self, world: World) -> bool:
        return world.changed(self.subject) is not None


@dataclass(frozen=True)
class During(Condition):
    start: time
    stop: time

    def holds(self, world: World) -> bool:
        now = world.now().time()
        if self.start <= self.stop:
            return self.start <= now < self.stop
        return now >= self.start or now < self.stop


@dataclass(frozen=True)
class Step[C: Hashable = Hashable](Condition):
    condition: Condition
    command: Command[Condition, C]

    def holds(self, world: World) -> bool:
        return self.condition.holds(world)


@dataclass(frozen=True)
class Rule[C: Hashable = Hashable](Condition):
    steps: tuple[Step[C], ...]
    name: str = ""
    tags: frozenset[str] = frozenset()
    condition: Or = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "condition", Or(*self.steps))

    @property
    def commands(self) -> tuple[Command[Condition, C], ...]:
        return tuple(step.command for step in self.steps)

    def holds(self, world: World) -> bool:
        return self.condition.holds(world)

    def where(self, keep: Callable[[Command[Condition, C]], bool]) -> Rule[C]:
        return Rule(tuple(step for step in self.steps if keep(step.command)), self.name, self.tags)


@dataclass(frozen=True)
class Action[C: Hashable = Hashable]:
    commands: tuple[Command[Condition, C], ...] = ()


@dataclass(frozen=True)
class Watch[C: Hashable = Hashable]:
    condition: Condition
    rule: Rule[C]
    delay: timedelta = timedelta()
    cooldown: timedelta = timedelta()
    cancel: Condition = NEVER


class Report[C: Hashable = Hashable](NamedTuple):
    item: Source[C]
    commands: tuple[Command[Condition, C], ...]


@dataclass(frozen=True)
class Defer[C: Hashable = Hashable]:
    watch: Watch[C]
    when: datetime


@dataclass(frozen=True)
class Cancel[C: Hashable = Hashable]:
    watch: Watch[C]


@dataclass(frozen=True)
class Drop[C: Hashable = Hashable]:
    item: Source[C]


@dataclass(frozen=True)
class Cooldown[C: Hashable = Hashable]:
    watch: Watch[C]


@dataclass(frozen=True)
class Change:
    subject: Subject
    old: Value
    new: Value


class SnapShot[C: Hashable = Hashable](NamedTuple):
    routine: tuple[Command[Condition, C], ...]
    end: datetime
    label: str = ""
