"""Pricing table loader and per-call cost computation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from agentcore.pricing import cost_for as _core_cost_for


@dataclass(frozen=True)
class ModelPrice:
    input_per_mtok: float
    output_per_mtok: float


class PricingTable:
    """Loads pricing from `policy.yaml` and computes per-call cost."""

    def __init__(self, prices: dict[str, ModelPrice]) -> None:
        self._prices = prices

    @classmethod
    def from_yaml(cls, path: Path) -> PricingTable:
        with path.open("r", encoding="utf-8") as f:
            raw: dict[str, Any] = yaml.safe_load(f)
        pricing_block = raw.get("pricing", {})
        prices: dict[str, ModelPrice] = {}
        for model, entry in pricing_block.items():
            prices[model] = ModelPrice(
                input_per_mtok=float(entry["input_per_mtok"]),
                output_per_mtok=float(entry["output_per_mtok"]),
            )
        return cls(prices)

    def has(self, model: str) -> bool:
        return model in self._prices

    def cost(self, model: str, tokens_in: int, tokens_out: int) -> float:
        if model not in self._prices:
            raise KeyError(f"no pricing entry for model {model!r}")
        p = self._prices[model]
        return _core_cost_for(tokens_in, tokens_out, p.input_per_mtok, p.output_per_mtok)

    def models(self) -> list[str]:
        return list(self._prices.keys())
