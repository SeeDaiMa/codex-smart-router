from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Effort = Literal["none", "low", "medium", "high", "xhigh", "max"]
Risk = Literal["low", "medium", "high"]


class TaskAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: Literal[
        "style", "documentation", "implementation", "debugging", "architecture", "security"
    ]
    complexity: int = Field(ge=1, le=5)
    risk: Risk
    technology: list[str]
    recommended_reasoning: Effort


class ProjectContext(BaseModel):
    stack: list[str] = Field(default_factory=list)
    files_changed: int = 0
    changed_paths: list[str] = Field(default_factory=list)
    risk: Risk = "low"
    scan_status: Literal["disabled", "unavailable", "complete", "partial"] = "disabled"
    git_status: Literal["not_checked", "not_repository", "complete", "unavailable"] = "not_checked"


class RouteDecision(BaseModel):
    model: str
    reasoning: str
    source: Literal["rule", "heuristic", "classifier", "continuation", "manual"]
    rule_id: str | None = None
    reason: str


class RoutePreview(BaseModel):
    analysis: TaskAnalysis
    project: ProjectContext
    decision: RouteDecision
    classifier_status: Literal["disabled", "skipped", "used", "fallback"]
