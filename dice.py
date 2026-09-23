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
