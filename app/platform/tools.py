"""Tool registry and the context every tool runs with.

Domain packs register tools with @tool. A tool declares a pydantic input model
(validated before the handler runs), a short status label for the UI, and whether
it reads data or prepares an action. Tools never receive the customer ID from the
model: it comes from the authenticated session.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from .connectors import Connectors
from .policy import PolicyDocument, PolicyEngine, Rule
from .session import Session

if TYPE_CHECKING:
    from .actions import ActionManager


class ToolError(Exception):
    """A problem the model should explain to the customer (bad loan ID, not eligible, ...)."""


class NoArgs(BaseModel):
    pass


class EvidenceCollector:
    """Collects what an answer was based on: data sources, policy rules, calculations."""

    def __init__(self):
        self.items: list[dict] = []
        self._seen: set = set()

    def _add(self, key, item: dict):
        if key not in self._seen:
            self._seen.add(key)
            self.items.append(item)

    def data(self, system: str, label: str):
        self._add(("data", system, label), {"type": "data", "system": system, "label": label})

    def rule(self, rule: Rule):
        self._add(("rule", rule.id, rule.version), rule.citation())

    def rules(self, doc: PolicyDocument, *rule_ids: str):
        for rule_id in rule_ids:
            if rule_id in doc.rules:
                self.rule(doc.rules[rule_id])

    def calculation(self, title: str, inputs: dict, outputs: dict, method: str, notes: list[str] | None = None):
        self._add(("calc", title), {"type": "calculation", "title": title, "inputs": inputs, "outputs": outputs,
                                    "method": method, "notes": notes or []})


@dataclass
class ToolContext:
    session: Session
    connectors: Connectors
    policy: PolicyEngine
    actions: "ActionManager"
    evidence: EvidenceCollector
    emit: Callable[[dict], Awaitable[None]]
    today: date
    channel: str = "text"
    extras: dict = field(default_factory=dict)

    @property
    def customer_id(self) -> str:
        return self.session.customer_id


Handler = Callable[[ToolContext, Any], Awaitable[dict]]


@dataclass
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: Handler
    label: str
    kind: str  # read | action

    def schema(self) -> dict:
        input_schema = self.input_model.model_json_schema()
        _compact(input_schema)
        return {"name": self.name, "description": self.description, "input_schema": input_schema}


def _compact(node):
    """Shrink pydantic JSON schema: every token in tool definitions is paid on every model call."""
    if isinstance(node, dict):
        node.pop("title", None)
        options = node.get("anyOf")
        if options and any(o.get("type") == "null" for o in options):
            rest = [o for o in options if o.get("type") != "null"]
            if len(rest) == 1:
                node.pop("anyOf")
                node.update(rest[0])
        if "default" in node and node["default"] is None:
            node.pop("default")
        for key, value in node.items():
            if key != "properties":
                _compact(value)
        for value in node.get("properties", {}).values():
            _compact(value)
    elif isinstance(node, list):
        for item in node:
            _compact(item)


REGISTRY: dict[str, ToolSpec] = {}


def tool(name: str, description: str, label: str, input_model: type[BaseModel] = NoArgs, kind: str = "read"):
    def register(handler: Handler) -> Handler:
        if name in REGISTRY:
            raise ValueError(f"Tool {name} registered twice")
        REGISTRY[name] = ToolSpec(name, description.strip(), input_model, handler, label, kind)
        return handler
    return register
