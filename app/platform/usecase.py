"""Use-case packs.

A pack is a YAML file in config/usecases that names the Python modules providing
its tools, which tools and policy families it uses, the instructions specific to
the workflow, and what the customer's app shows. Launching a new workflow means
adding a pack and its domain module; the runtime, connectors, policy engine,
action framework, telemetry and UI are reused unchanged.
"""

import importlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .tools import REGISTRY, ToolContext, ToolSpec

ContextProvider = Callable[[ToolContext], Awaitable[str]]
PanelProvider = Callable[[ToolContext], Awaitable[dict]]
CONTEXT_PROVIDERS: dict[str, ContextProvider] = {}
PANEL_PROVIDERS: dict[str, PanelProvider] = {}


def context_provider(name: str):
    """Registers a function that summarises the customer's products for the system prompt."""
    def register(fn: ContextProvider) -> ContextProvider:
        CONTEXT_PROVIDERS[name] = fn
        return fn
    return register


def panel(name: str):
    """Registers a data provider for a panel in the customer's app."""
    def register(fn: PanelProvider) -> PanelProvider:
        PANEL_PROVIDERS[name] = fn
        return fn
    return register


@dataclass
class UseCase:
    id: str
    name: str
    tagline: str
    modules: list[str]
    tools: list[str]
    policy_families: list[str]
    context: list[str]
    panels: list[str]
    effort: str
    instructions: str
    order: int = 100
    suggested_prompts: list[str] = field(default_factory=list)

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "tagline": self.tagline, "panels": self.panels,
                "suggested_prompts": self.suggested_prompts}


def load_usecases(directory: Path) -> dict[str, UseCase]:
    usecases: dict[str, UseCase] = {}
    for path in sorted(directory.glob("*.yaml")):
        data = yaml.safe_load(path.read_text())
        for module in data.get("modules", []):
            importlib.import_module(module)
        uc = UseCase(id=data["id"], name=data["name"], tagline=data.get("tagline", ""), modules=data.get("modules", []),
                     tools=data["tools"], policy_families=data.get("policy_families", []),
                     context=data.get("context", []), panels=data.get("panels", []), effort=data.get("effort", "low"),
                     instructions=data.get("instructions", "").strip(),
                     suggested_prompts=data.get("suggested_prompts", []), order=data.get("order", 100))
        missing = [t for t in uc.tools if t not in REGISTRY]
        missing += [c for c in uc.context if c not in CONTEXT_PROVIDERS]
        missing += [p for p in uc.panels if p not in PANEL_PROVIDERS]
        if missing:
            raise ValueError(f"Use case {uc.id} references unknown tools/providers: {missing}")
        usecases[uc.id] = uc
    return dict(sorted(usecases.items(), key=lambda kv: (kv[1].order, kv[0])))


def tools_for(usecases: list[UseCase]) -> list[ToolSpec]:
    names: list[str] = []
    for uc in usecases:
        names += [t for t in uc.tools if t not in names]
    return [REGISTRY[n] for n in names]


def unique(items):
    seen = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen
