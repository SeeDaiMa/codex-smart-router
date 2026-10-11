from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from router.types import Category, Effort, ProjectContext, Risk, RouteDecision, TaskAnalysis


class ModelSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    supported_reasoning: list[Effort] = Field(min_length=1)


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    priority: int
    any_keywords: list[str] = Field(default_factory=list)
    any_changed_suffixes: list[str] = Field(default_factory=list)
    project_risk: Risk | None = None
    model: str
    reasoning: Effort

    @model_validator(mode="after")
    def require_condition(self) -> "Rule":
        if not (self.any_keywords or self.any_changed_suffixes or self.project_risk):
            raise ValueError("A rule must have a match condition")
        return self

    def matches(self, task: str, project: ProjectContext) -> bool:
        # Conditions within a rule are alternatives, as named by `any_*`.
        text = task.casefold()
        return (
            any(keyword.casefold() in text for keyword in self.any_keywords)
            or any(
                path.casefold().endswith(suffix.casefold())
                for path in project.changed_paths
                for suffix in self.any_changed_suffixes
            )
            or (self.project_risk is not None and project.risk == self.project_risk)
        )


class DefaultRoute(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str
    reasoning: Effort


class SimpleRoute(DefaultRoute):
    enabled: bool = True
    max_complexity: int = Field(default=1, ge=1, le=2)
    categories: list[Category] = Field(
        default_factory=lambda: ["style", "documentation", "transformation"]
    )


class RoutingTiers(BaseModel):
    model_config = ConfigDict(extra="forbid")
    high_risk: DefaultRoute
    architecture: DefaultRoute
    complex: DefaultRoute
    simple: SimpleRoute


class RuleFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default: DefaultRoute
    tiers: RoutingTiers
    rules: list[Rule] = Field(default_factory=list)


class DecisionEngine:
    def __init__(self, rules_file: Path, models_file: Path):
        models: Any = yaml.safe_load(models_file.read_text())
        self.models = {
            key: ModelSpec.model_validate(value) for key, value in models["models"].items()
        }
        rules = RuleFile.model_validate(yaml.safe_load(rules_file.read_text()))
        self.default = rules.default
        self.tiers = rules.tiers
        self.rules = sorted(rules.rules, key=lambda rule: rule.priority, reverse=True)
        if len({rule.id for rule in self.rules}) != len(self.rules):
            raise ValueError("Rule ids must be unique")
        for route in [
            self.default,
            self.tiers.high_risk,
            self.tiers.architecture,
            self.tiers.complex,
            self.tiers.simple,
            *self.rules,
        ]:
            self.validate_route(route.model, route.reasoning)

    def validate_route(self, model: str, reasoning: str) -> None:
        if model not in self.models or reasoning not in self.models[model].supported_reasoning:
            raise ValueError(f"Unsupported model/reasoning combination: {model}/{reasoning}")

    def hard_rule(self, task: str, project: ProjectContext) -> RouteDecision | None:
        for rule in self.rules:
            if rule.matches(task, project):
                return RouteDecision(
                    model=rule.model,
                    reasoning=rule.reasoning,
                    source="rule",
                    rule_id=rule.id,
                    reason=f"Matched policy {rule.id}",
                )
        return None

    def decide(
        self, analysis: TaskAnalysis, *, classified: bool = False, allow_simple: bool = True
    ) -> RouteDecision:
        source = "classifier" if classified else "heuristic"
        if analysis.risk == "high" or analysis.category == "security":
            route = self.tiers.high_risk
        elif analysis.category == "architecture" or analysis.complexity == 5:
            route = self.tiers.architecture
        elif analysis.complexity >= 4:
            route = self.tiers.complex
        elif (
            self.tiers.simple.enabled
            and allow_simple
            and analysis.complexity <= self.tiers.simple.max_complexity
            and analysis.category in self.tiers.simple.categories
            and analysis.risk == "low"
            and analysis.recommended_reasoning in {"none", "low"}
        ):
            route = self.tiers.simple
        else:
            route = self.default
        model, effort = route.model, route.reasoning
        # The classifier cannot request an unsupported level or weaken a tier's minimum.
        order = ["none", "low", "medium", "high", "xhigh", "max"]
        if classified and order.index(analysis.recommended_reasoning) > order.index(effort):
            effort = analysis.recommended_reasoning
        self.validate_route(model, effort)
        return RouteDecision(
            model=model,
            reasoning=effort,
            source=source,
            reason=f"{analysis.category}; complexity={analysis.complexity}; risk={analysis.risk}",
        )
