"""Per-shard worker. One model call per shard, recorded to the ledger."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from budgeteer.adapters.anthropic_adapter import AdapterMessage, AnthropicAdapter
from budgeteer.fleet.ledger import Shard, ShardLedger
from budgeteer.pricing import PricingTable

_LOG = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are one of several parallel workers. Work on the assigned shard only. "
    "Produce a small, self-contained change or output. Do not speculate about "
    "other shards."
)


@dataclass(frozen=True)
class WorkerResult:
    shard_id: str
    success: bool
    cost_usd: float
    tokens_in: int
    tokens_out: int
    output_text: str
    error: str | None = None


class Worker:
    """Synchronous worker. Safe to call from a thread pool."""

    def __init__(
        self,
        worker_id: str,
        adapter: AnthropicAdapter,
        pricing: PricingTable,
        model: str,
        max_tokens: int,
        ledger: ShardLedger,
    ) -> None:
        self._worker_id = worker_id
        self._adapter = adapter
        self._pricing = pricing
        self._model = model
        self._max_tokens = max_tokens
        self._ledger = ledger

    @property
    def worker_id(self) -> str:
        return self._worker_id

    def run_shard(self, shard: Shard, worktree_path: str | None) -> WorkerResult:
        messages = [
            AdapterMessage(role="system", content=_SYSTEM_PROMPT),
            AdapterMessage(role="user", content=shard.description),
        ]
        try:
            response = self._adapter.get_response(
                messages,
                model=self._model,
                max_tokens=self._max_tokens,
            )
        except Exception as exc:  # noqa: BLE001 - adapter SDKs raise heterogeneous errors
            _LOG.error(
                "shard_failed shard_id=%s worker_id=%s model=%s error=%s",
                shard.shard_id,
                self._worker_id,
                self._model,
                exc,
            )
            self._ledger.fail_shard(shard.shard_id, error=str(exc))
            return WorkerResult(
                shard_id=shard.shard_id,
                success=False,
                cost_usd=0.0,
                tokens_in=0,
                tokens_out=0,
                output_text="",
                error=str(exc),
            )

        cost = self._pricing.cost(self._model, response.tokens_in, response.tokens_out)
        self._ledger.complete_shard(
            shard.shard_id,
            result_text=response.text,
            cost_usd=cost,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            worktree_path=worktree_path,
        )
        return WorkerResult(
            shard_id=shard.shard_id,
            success=True,
            cost_usd=cost,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            output_text=response.text,
        )
