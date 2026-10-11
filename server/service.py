import asyncio
from typing import Any

import httpx

from config.settings import Settings
from providers.upstream import upstream_headers
from router.account_catalog import AccountCatalog
from router.classifier import ModelClassifier, heuristic_analysis
from router.decision import DecisionEngine
from router.input_context import extract_context
from router.project_analyzer import ProjectAnalyzer
from router.route_cache import RouteCache
from router.types import RouteDecision, RoutePreview


class MissingRouteError(ValueError):
    pass


class RouterService:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client
        self.engine = DecisionEngine(settings.rules_file, settings.models_file)
        self.analyzer = ProjectAnalyzer(settings.project_roots, settings.max_scan_files)
        self.classifier = ModelClassifier(
            settings.classifier_model, settings.classifier_timeout_seconds
        )
        if settings.classifier_enabled:
            if settings.auth_mode == "chatgpt":
                raise ValueError(
                    "The optional model classifier currently needs API mode; "
                    "use deterministic policy for ChatGPT account routing"
                )
            self.engine.validate_route(settings.classifier_model, "low")
        self.cache = RouteCache(settings.route_capacity, settings.route_ttl_seconds)
        self.account_catalog = AccountCatalog(client)

    async def preview(self, task: str, project_root: str | None = None) -> RoutePreview:
        project = await asyncio.to_thread(self.analyzer.analyze, project_root)
        analysis = heuristic_analysis(task, project)
        heuristic = analysis.model_copy()
        allow_simple = analysis.complexity == 1 and analysis.risk == "low"
        decision = self.engine.hard_rule(task, project)
        status = "disabled"
        if decision:
            status = "skipped"
            analysis.recommended_reasoning = decision.reasoning
        else:
            if self.settings.classifier_enabled and analysis.risk != "high":
                key = self.settings.upstream_api_key
                classified = (
                    await self.classifier.classify(
                        self.client, task, project, upstream_headers(key.get_secret_value())
                    )
                    if key and key.get_secret_value()
                    else None
                )
                if classified:
                    analysis = classified
                    # A classifier cannot weaken locally detected task/context risk.
                    if heuristic.risk == "high":
                        analysis.risk = "high"
                    elif heuristic.risk == "medium" and analysis.risk == "low":
                        analysis.risk = "medium"
                        analysis.complexity = max(3, analysis.complexity)
                    status = "used"
                else:
                    status = "fallback"
            elif self.settings.classifier_enabled:
                status = "skipped"
            decision = self.engine.decide(
                analysis, classified=status == "used", allow_simple=allow_simple
            )
        return RoutePreview(
            analysis=analysis, project=project, decision=decision, classifier_status=status
        )

    async def resolve(self, body: dict[str, Any], headers: dict[str, str]) -> RouteDecision:
        context = extract_context(body)
        if self.settings.auth_mode == "chatgpt" and body["model"] != "smart-auto":
            # Manual selection belongs to Codex and the account backend. In particular,
            # do not require local routing state for an existing manual conversation.
            return RouteDecision(
                model=body["model"],
                reasoning=(body.get("reasoning") or {}).get("effort") or "",
                source="manual",
                reason="Preserve the model and reasoning selected in Codex",
            )
        previous = body.get("previous_response_id")
        if previous:
            cached = self.cache.get("response:" + previous)
            if cached is None:
                raise MissingRouteError(
                    "Routing state for previous_response_id expired or was lost. "
                    "Start a new conversation; opaque provider state cannot be safely rerouted."
                )
            return cached.model_copy(update={"source": "continuation"})
        session_id = next(
            (
                headers[key]
                for key in ("x-codex-thread-id", "x-codex-session-id", "session_id")
                if headers.get(key)
            ),
            None,
        ) or body.get("prompt_cache_key")
        task_key = context.task_key(session_id)
        cached = self.cache.get(task_key)
        if cached:
            return cached.model_copy(update={"source": "continuation"})
        if context.has_tool_history or context.has_opaque_history:
            # Codex can resend the complete transcript with a new user message.
            # Continue the known chain rather than rerouting opaque prior reasoning.
            for previous_task in reversed(context.tasks[:-1]):
                cached = self.cache.get(context.task_key(session_id, previous_task))
                if cached:
                    self.cache.put(task_key, cached)
                    return cached.model_copy(update={"source": "continuation"})
            raise MissingRouteError(
                "No routing state for this tool/reasoning chain. Start a new conversation "
                "after a router restart or expired routing state."
            )
        if not context.task:
            raise MissingRouteError("No user task or known response chain was supplied")
        preview = await self.preview(context.task, context.project_root)
        self.cache.put(task_key, preview.decision)
        return preview.decision
