"""Simple dice-rolling utilities. All randomness happens here in code,
never left to the language model, so results are fair and verifiable."""

import random
from dataclasses import dataclass


@dataclass
class RollResult:
    sides: int
    value: int
    modifier: int

    @property
    def total(self) -> int:
        return self.value + self.modifier

    def describe(self) -> str:
        if self.modifier:
            sign = "+" if self.modifier >= 0 else "-"
            return f"d{self.sides}: {self.value} {sign} {abs(self.modifier)} = {self.total}"
        return f"d{self.sides}: {self.value}"


def roll(sides: int = 20, modifier: int = 0) -> RollResult:
    """Roll a single die with `sides` faces and add `modifier`."""
    value = random.randint(1, sides)
    return RollResult(sides=sides, value=value, modifier=modifier)


def roll_check(difficulty_class: int, sides: int = 20, modifier: int = 0) -> tuple[RollResult, bool]:
    """Roll against a DC (difficulty class). Returns (result, success)."""
    result = roll(sides=sides, modifier=modifier)
    return result, result.total >= difficulty_class


def hp_penalty(hp: int, max_hp: int) -> int:
    """Injuries make actions a bit harder, but only when seriously wounded —
    kept mild so the story can keep going instead of spiraling quickly."""
    if not max_hp or max_hp <= 0:
        return 0
    ratio = hp / max_hp
    if ratio <= 0.2:
        return -2
    if ratio <= 0.45:
        return -1
    return 0


def classify(natural: int, total: int) -> str:
    """Classify a d20 check into one of five outcome tiers. A natural 1 or
    20 always overrides the total (classic critical fail/success)."""
    if natural == 1:
        return "critical_failure"
    if natural == 20:
        return "critical_success"
    if total <= 8:
        return "failure"
    if total <= 13:
        return "partial_success"
    return "success"
