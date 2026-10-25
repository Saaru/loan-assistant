"""Versioned business rules, fetched from the Policy Knowledge system.

Rules carry an ID, the exact text to cite, and machine-readable params that domain
logic computes with. That way the answer and the calculation always use the same
rule version, and changing a fee or limit is a policy edit, not a code change.
"""

import re
import time
from dataclasses import dataclass, field
from datetime import date

from .connectors import Connectors, SystemUnavailable


@dataclass
class Rule:
    id: str
    title: str
    text: str
    params: dict
    tags: list[str]
    version: str
    effective_from: str
    applies_to: dict = field(default_factory=dict)
    change_note: str | None = None

    def citation(self) -> dict:
        return {"type": "rule", "id": self.id, "title": self.title, "text": self.text,
                "version": self.version, "effective_from": self.effective_from, "change_note": self.change_note}


@dataclass
class PolicyDocument:
    family: str
    title: str
    version: str
    resolved_on: str
    rules: dict[str, Rule]
    reference: dict
    stale: bool = False

    def rule(self, rule_id: str) -> Rule:
        return self.rules[rule_id]

    def search(self, query: str, limit: int = 4) -> list[Rule]:
        words = [w for w in re.findall(r"[a-z0-9-]+", query.lower()) if len(w) > 2]
        scored = []
        for rule in self.rules.values():
            title, text, tags = rule.title.lower(), rule.text.lower(), " ".join(rule.tags)
            score = sum(3 * (w in tags) + 2 * (w in title) + (w in text) for w in words)
            if score:
                scored.append((score, rule.id, rule))
        return [r for _, _, r in sorted(scored, key=lambda x: (-x[0], x[1]))[:limit]]


class PolicyEngine:
    def __init__(self, connectors: Connectors, ttl_s: float = 600):
        self.connectors = connectors
        self.ttl_s = ttl_s
        self._cache: dict[tuple[str, str], tuple[float, PolicyDocument]] = {}

    async def document(self, family: str, on: date) -> PolicyDocument:
        key = (family, on.isoformat())
        cached = self._cache.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        try:
            data = await self.connectors["policy"].get(f"/policy/documents/{family}", {"on": on.isoformat()}, cache=False)
        except SystemUnavailable:
            if cached:  # serve the last known version rather than fail the answer
                cached[1].stale = True
                return cached[1]
            raise
        doc = PolicyDocument(
            family=data["family"], title=data["title"], version=data["version"], resolved_on=data["resolved_on"],
            rules={r["id"]: Rule(id=r["id"], title=r["title"], text=r["text"], params=r.get("params", {}),
                                 tags=r.get("tags", []), version=r["version"], effective_from=r["effective_from"],
                                 applies_to=r.get("applies_to", {}), change_note=r.get("change_note"))
                   for r in data["rules"]},
            reference=data.get("reference", {}))
        self._cache[key] = (time.monotonic() + self.ttl_s, doc)
        return doc

    def invalidate(self):
        self._cache.clear()
