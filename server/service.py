import asyncio
import hashlib
import json
import logging
from typing import Any

import httpx

from config.settings import Settings
from providers.upstream import upstream_headers
from router.account_catalog import AccountCatalog
from router.classifier import ModelClassifier, heuristic_analysis
from router.decision import DecisionEngine
from router.input_context import extract_context, history_keys
from router.project_analyzer import ProjectAnalyzer
from router.route_cache import RouteCache
from router.types import ProjectContext, RouteDecision, RoutePreview

logger = logging.getLogger(__name__)


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
        self.cache = RouteCache(
            settings.route_capacity, settings.route_ttl_seconds, settings.route_state_file
        )
        self.account_catalog = AccountCatalog(client)

    async def preview(self, task: str, project_root: str | None = None) -> RoutePreview:
        project = await asyncio.to_thread(self.analyzer.analyze, project_root)
        analysis = heuristic_analysis(task, project)
        heuristic = analysis.model_copy()
        allow_simple = analysis.complexity <= 2 and analysis.risk == "low"
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

    def route_scope(self, headers: dict[str, str]) -> str:
        # Stable account IDs survive token refresh; never persist credentials or account IDs.
        if self.settings.auth_mode == "chatgpt":
            account = headers.get("chatgpt-account-id") or headers.get("authorization", "")
        else:
            key = self.settings.upstream_api_key
            account = key.get_secret_value() if key else ""
        identity = [self.settings.auth_mode, self.settings.inference_base_url, account]
        return hashlib.sha256(json.dumps(identity).encode()).hexdigest() + ":"

    def remember_response(
        self, response: dict, decision: RouteDecision, headers: dict[str, str]
    ) -> None:
        keys = list(history_keys(response.get("output")))
        response_id = response.get("id")
        if isinstance(response_id, str) and response_id:
            keys.append("response:" + response_id)
        scope = self.route_scope(headers)
        self.cache.put_many([scope + key for key in keys], decision)

    async def resolve(
        self, body: dict[str, Any], headers: dict[str, str], *, new_tasks: bool = True
    ) -> RouteDecision:
        context = extract_context(body)
        scope = self.route_scope(headers)
        session_id = next(
            (
                headers[key]
                for key in ("x-codex-thread-id", "x-codex-session-id", "session_id")
                if headers.get(key)
            ),
            None,
        ) or body.get("prompt_cache_key")
        session_key = (
            "session:" + hashlib.sha256(session_id.encode()).hexdigest() if session_id else None
        )
        task_key = context.task_key(session_id)
        turn_key = context.turn_key(session_id)
        history = history_keys(body.get("input"))

        def remember(decision: RouteDecision) -> RouteDecision:
            keys = [*context.active_history]
            # A followup's text is not a new chain identity. Persisting it as a
            # task route can attach a different chat with that text to this model.
            if context.task and not (
                body.get("previous_response_id")
                or context.has_tool_history
                or context.has_opaque_history
                or len(context.tasks) > 1
            ):
                keys.append(task_key)
            if session_key:
                keys.append(session_key)
            self.cache.put_many([scope + key for key in keys], decision)
            return decision

        if self.settings.auth_mode == "chatgpt" and body["model"] != "smart-auto":
            # Manual selection belongs to Codex and the account backend. In particular,
            # do not require local routing state for an existing manual conversation.
            return remember(
                RouteDecision(
                    model=body["model"],
                    reasoning=(body.get("reasoning") or {}).get("effort") or "",
                    source="manual",
                    reason="Preserve the model and reasoning selected in Codex",
                )
            )
        # A new user task is a routing boundary; historical tools/reasoning are
        # preserved but must not force that task onto the previous task's model.
        # A tool result, compact request or pending tool call is not a boundary.
        if new_tasks and context.task:
            cached_turn = self.cache.get(scope + turn_key)
            if cached_turn and context.new_user_turn:
                return remember(cached_turn.model_copy(update={"source": "continuation"}))
            if context.new_user_turn:
                analysis = heuristic_analysis(context.task, ProjectContext())
                # Short approvals and vague followups need the previous task's
                # route. An explicit task (including Git commit) can be selected anew.
                if analysis.category != "general" or not (
                    body.get("previous_response_id")
                    or history
                    or len(context.tasks) > 1
                    or (session_key and self.cache.get(scope + session_key))
                ):
                    preview = await self.preview(context.task, context.project_root)
                    self.cache.put(scope + turn_key, preview.decision)
                    return remember(preview.decision)
        previous = body.get("previous_response_id")
        if context.task and (not context.new_user_turn or not new_tasks):
            # Full transcripts identify the active turn even when a client
            # carries an older response ID or a session has another branch.
            for key in [*reversed(context.active_history), turn_key]:
                cached = self.cache.get(scope + key)
                if cached:
                    return remember(cached.model_copy(update={"source": "continuation"}))
        if previous:
            cached = self.cache.get(scope + "response:" + previous)
            if cached:
                return remember(cached.model_copy(update={"source": "continuation"}))
        continuation = bool(previous or context.has_tool_history or context.has_opaque_history)
        if continuation:
            # Opaque and tool IDs remain usable when compact drops the original task
            # or resume changes a session header/project directory.
            lookup = list(reversed(context.active_history))
            if context.task:
                lookup.append(turn_key)
            if session_key:
                lookup.append(session_key)
            lookup.extend(reversed(history))
            # Only the original task may identify a pre-existing chain. Later
            # user messages can match entirely unrelated conversations.
            lookup.extend(context.task_key(session_id, task) for task in context.tasks[:1])
            for key in lookup:
                cached = self.cache.get(scope + key)
                if cached:
                    return remember(cached.model_copy(update={"source": "continuation"}))
            # Upgrade recovery for conversations predating the durable store. Reconstruct
            # from the earliest supplied task, not the latest followup or a tool's output.
            # No classifier request, project scan, history stripping or tool replay.
            original = context.tasks[0] if context.tasks else ""
            project = ProjectContext()
            if original:
                decision = self.engine.hard_rule(original, project) or self.engine.decide(
                    heuristic_analysis(original, project)
                )
            else:
                decision = RouteDecision(
                    model=self.engine.default.model,
                    reasoning=self.engine.default.reasoning,
                    source="recovery",
                    reason="No saved route or original task; use configured default",
                )
            logger.warning(
                "Recovered missing continuation route model=%s reasoning=%s",
                decision.model,
                decision.reasoning,
            )
            return remember(decision.model_copy(update={"source": "recovery"}))
        if context.new_user_turn and session_key:
            cached = self.cache.get(scope + session_key)
            if cached and heuristic_analysis(context.task, ProjectContext()).category == "general":
                return remember(cached.model_copy(update={"source": "continuation"}))
        cached = self.cache.get(scope + task_key)
        if cached:
            return remember(cached.model_copy(update={"source": "continuation"}))
        if not context.task:
            raise MissingRouteError("No user task or known response chain was supplied")
        preview = await self.preview(context.task, context.project_root)
        return remember(preview.decision)
