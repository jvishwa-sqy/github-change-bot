"""Deterministic no-LLM provider.

Set ``LLM_PROVIDER=null`` to run the whole pipeline — webhook, queue, git,
diff, context, Slack — with zero model spend. Useful for staging, for
verifying a deployment, and in tests.
"""

from __future__ import annotations

from app.llm.base import BaseLLMProvider
from app.models import ChangeAnalysisRequest, ChangeSummary


class NullProvider(BaseLLMProvider):
    name = "null"

    async def _complete(self, system_prompt: str, user_prompt: str) -> ChangeSummary:
        raise NotImplementedError  # never reached; both entry points are overridden

    async def summarize_change(self, request: ChangeAnalysisRequest) -> ChangeSummary:
        paths = [context.path for context in request.files]
        return ChangeSummary(
            summary=(
                f"{len(paths)} file(s) changed on {request.branch} "
                f"(+{request.total_additions} -{request.total_deletions}). "
                "LLM analysis is disabled (LLM_PROVIDER=null)."
            ),
            changes=[f"{c.status.value}: {c.path}" for c in request.files[:20]],
            affected_components=sorted({p.rsplit("/", 1)[0] for p in paths})[:10],
            impact=[],
            risk="low",
            risk_reason="No model analysis was performed.",
            recommended_tests=[],
        )

    async def synthesize(
        self, request: ChangeAnalysisRequest, summaries: list[tuple[str, ChangeSummary]]
    ) -> ChangeSummary:
        return await self.summarize_change(request)
