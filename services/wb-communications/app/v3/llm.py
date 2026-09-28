"""Thin LLM adapter for v3 (classifier + generator), with call accounting for cost reporting."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class LLMStats:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    errors: int = 0
    by_step: dict = field(default_factory=dict)


class V3LLM:
    """Wraps OpenAIClient.structured(); one instance per shadow run (stats are per run)."""

    def __init__(self, openai_client, *, classifier_max_tokens: int = 1500, generator_max_tokens: int = 1000):
        self._c = openai_client
        self._cls_max = classifier_max_tokens
        self._gen_max = generator_max_tokens
        self.stats = LLMStats()

    def _account(self, step: str, usage: dict, latency_ms: int) -> None:
        self.stats.calls += 1
        self.stats.input_tokens += int((usage or {}).get("input_tokens") or 0)
        self.stats.output_tokens += int((usage or {}).get("output_tokens") or 0)
        self.stats.latency_ms += int(latency_ms or 0)
        s = self.stats.by_step.setdefault(step, {"calls": 0, "latency_ms": 0})
        s["calls"] += 1
        s["latency_ms"] += int(latency_ms or 0)

    def structured(self, system: str, user: str, name: str, schema: dict):
        """Classifier call: returns (parsed, usage)."""
        try:
            parsed, usage, latency, _model = self._c.structured(system, user, name, schema,
                                                                max_output_tokens=self._cls_max)
        except Exception:
            self.stats.errors += 1
            raise
        self._account("classifier", usage, latency)
        return parsed, usage

    def structured_timed(self, system: str, user: str, name: str, schema: dict):
        """Generator call: returns (parsed, usage, latency_ms, model)."""
        try:
            parsed, usage, latency, model = self._c.structured(system, user, name, schema,
                                                               max_output_tokens=self._gen_max)
        except Exception:
            self.stats.errors += 1
            raise
        self._account("generator", usage, latency)
        return parsed, usage, latency, model
