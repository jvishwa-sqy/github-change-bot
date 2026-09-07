"""Full pipeline: real git repository → diff → context → LLM → Slack."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from app.analyzer import ChangeAnalyzer
from app.models import ChangeAnalysisRequest, ChangeSummary, PushEvent
from app.repo_map import RepoMapStore
from app.worker import Worker

ZERO = "0" * 40

LISTENER_V1 = '''"""Dotcom listener."""
from app.language_tools import register_language_tools


class DotcomListener:
    def __init__(self, config):
        self.config = config

    def register_tools(self):
        tools = ["transfer"]
        return tools

    def teardown(self):
        return None
'''

LISTENER_V2 = LISTENER_V1.replace(
    '        tools = ["transfer"]\n        return tools\n',
    '        tools = ["transfer"]\n'
    '        if self.config.channel == "DOTCOM":\n'
    "            tools.extend(register_language_tools())\n"
    "        return tools\n",
)


@dataclass
class FakeLLM:
    """Records every call so tests can assert on cost."""

    name: str = "fake"
    requests: list[ChangeAnalysisRequest] = field(default_factory=list)
    syntheses: list[list[tuple[str, ChangeSummary]]] = field(default_factory=list)

    async def summarize_change(self, request: ChangeAnalysisRequest) -> ChangeSummary:
        self.requests.append(request)
        return ChangeSummary(
            summary="Dotcom inbound calls can now register language-switching tools.",
            changes=["DOTCOM registers the existing language tools."],
            affected_components=["Dotcom inbound listener"],
            impact=["Callers can switch language mid-call."],
            risk="medium",
            risk_reason="Live call path changed.",
            recommended_tests=["English → Hindi → English."],
        )

    async def synthesize(self, request, summaries):
        self.syntheses.append(summaries)
        merged = await self.summarize_change(request)
        self.requests.pop()  # the synthesis pass is counted separately
        return merged

    async def aclose(self) -> None:
        return None

    @property
    def call_count(self) -> int:
        return len(self.requests) + len(self.syntheses)


@dataclass
class FakeSlack:
    messages: list[dict] = field(default_factory=list)
    enabled: bool = True

    async def send(self, *, text, blocks):
        self.messages.append({"text": text, "blocks": blocks})

    async def send_change(self, event, summary, diff):
        self.messages.append({"kind": "change", "summary": summary, "diff": diff})

    async def send_simple(self, event, title, detail, *, link=None):
        self.messages.append({"kind": "simple", "title": title, "detail": detail})

    async def aclose(self) -> None:
        return None


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def slack() -> FakeSlack:
    return FakeSlack()


@pytest.fixture
def analyzer(settings, llm, slack) -> ChangeAnalyzer:
    return ChangeAnalyzer(settings, llm=llm, slack=slack)


def make_event(sandbox, before: str, after: str, ref: str = "refs/heads/feature/dotcom-fix"):
    return PushEvent(
        project_id=1,
        project_name="acme/ai-caller-core",
        repo_url=sandbox.url,
        web_url="https://github.com/acme/ai-caller-core",
        ref=ref,
        before_sha=before,
        after_sha=after,
        author_name="Vishwa",
        commit_count=1,
        commit_messages=["Enable language tools for dotcom"],
        default_branch="main",
    )


# ------------------------------------------------------------- happy path
async def test_normal_push_makes_exactly_one_llm_call(sandbox, analyzer, llm, slack) -> None:
    sandbox.write("app/listener.py", LISTENER_V1)
    sandbox.write("package-lock.json", '{"v": 1}\n')
    before = sandbox.commit("A")

    sandbox.write("app/listener.py", LISTENER_V2)
    sandbox.write("package-lock.json", '{"v": 2}\n')
    after = sandbox.commit("B")

    outcome = await analyzer.analyze(make_event(sandbox, before, after))

    assert outcome.status == "analyzed"
    assert outcome.llm_calls == 1
    assert llm.call_count == 1
    assert outcome.changed_files == 1  # the lockfile was filtered out

    request = llm.requests[0]
    assert [f.path for f in request.files] == ["app/listener.py"]
    assert request.branch == "feature/dotcom-fix"
    # The changed method arrived as context; the untouched one did not.
    assert "def register_tools(self):" in request.files[0].code_context
    assert "def teardown" not in request.files[0].code_context

    assert slack.messages[0]["kind"] == "change"
    assert outcome.summary.risk == "medium"


async def test_repo_map_is_updated_incrementally(sandbox, analyzer, settings) -> None:
    sandbox.write("app/listener.py", LISTENER_V1)
    sandbox.write("app/keep.py", "def keep():\n    return 1\n")
    before = sandbox.commit("A")

    sandbox.write("app/listener.py", LISTENER_V2)
    sandbox.write("app/added.py", "def brand_new():\n    return 2\n")
    after = sandbox.commit("B")

    await analyzer.analyze(make_event(sandbox, before, after))

    repo_map = RepoMapStore(settings.indexes_dir, 1).load()
    assert repo_map.commit_sha == after
    assert set(repo_map.files) == {"app/listener.py", "app/added.py"}
    assert "brand_new" in repo_map.files["app/added.py"].symbol_names
    # app/keep.py was never touched by the push, so it was never indexed here.
    assert "app/keep.py" not in repo_map.files


async def test_deleted_and_renamed_files_update_the_index(sandbox, analyzer, settings) -> None:
    body = "\n".join(f"def fn{i}():\n    return {i}\n" for i in range(20))
    sandbox.write("app/old_name.py", body)
    sandbox.write("app/doomed.py", "def doomed():\n    pass\n")
    before = sandbox.commit("A")

    sandbox.move("app/old_name.py", "app/new_name.py")
    sandbox.remove("app/doomed.py")
    after = sandbox.commit("B")

    await analyzer.analyze(make_event(sandbox, before, after))

    repo_map = RepoMapStore(settings.indexes_dir, 1).load()
    assert "app/new_name.py" in repo_map.files
    assert "app/old_name.py" not in repo_map.files
    assert "app/doomed.py" not in repo_map.files


async def test_secrets_never_reach_the_model(sandbox, analyzer, llm) -> None:
    sandbox.write("app/config.py", "TIMEOUT = 30\n")
    before = sandbox.commit("A")
    sandbox.write(
        "app/config.py",
        'TIMEOUT = 30\nAWS_KEY = "AKIAIOSFODNN7EXAMPLE"\nDB = "postgres://u:hunter2pw@db/app"\n',
    )
    after = sandbox.commit("B")

    await analyzer.analyze(make_event(sandbox, before, after))

    payload = llm.requests[0].model_dump_json()
    assert "AKIAIOSFODNN7EXAMPLE" not in payload
    assert "hunter2pw" not in payload
    assert "[REDACTED]" in payload


# --------------------------------------------------------- no-LLM paths
async def test_branch_deletion_skips_the_llm(sandbox, analyzer, llm, slack) -> None:
    sandbox.write("a.py", "x = 1\n")
    head = sandbox.commit("A")

    outcome = await analyzer.analyze(make_event(sandbox, head, ZERO))

    assert outcome.status == "branch_deleted"
    assert llm.call_count == 0
    assert slack.messages[0]["title"] == "Branch deleted"


async def test_ignored_only_push_skips_the_llm(sandbox, analyzer, llm, slack) -> None:
    sandbox.write("app/a.py", "x = 1\n")
    before = sandbox.commit("A")
    sandbox.write("package-lock.json", '{"v": 2}\n')
    sandbox.write("node_modules/x/index.js", "module.exports = 2;\n")
    after = sandbox.commit("B")

    outcome = await analyzer.analyze(make_event(sandbox, before, after))

    assert outcome.status == "ignored_only"
    assert llm.call_count == 0
    assert slack.messages == []  # notify_on_ignored_only defaults to False


async def test_ignored_only_push_can_notify_when_configured(sandbox, settings, llm, slack) -> None:
    configured = type(settings)(
        github_webhook_secret="x",
        llm_provider="null",
        bot_data_dir=settings.bot_data_dir,
        notify_on_ignored_only=True,
    )
    analyzer = ChangeAnalyzer(configured, llm=llm, slack=slack)

    sandbox.write("app/a.py", "x = 1\n")
    before = sandbox.commit("A")
    sandbox.write("yarn.lock", "# lock\n")
    after = sandbox.commit("B")

    outcome = await analyzer.analyze(make_event(sandbox, before, after))
    assert outcome.status == "ignored_only"
    assert llm.call_count == 0
    assert slack.messages[0]["title"] == "Push skipped"


async def test_new_branch_without_a_baseline_is_not_summarised(
    sandbox, analyzer, llm, slack
) -> None:
    sandbox.write("a.py", "x = 1\n")
    head = sandbox.commit("root commit")

    outcome = await analyzer.analyze(make_event(sandbox, ZERO, head))

    assert outcome.status == "branch_created"
    assert llm.call_count == 0
    assert slack.messages[0]["title"] == "Branch created"


async def test_new_branch_with_a_baseline_is_analysed(sandbox, analyzer, llm) -> None:
    sandbox.write("app/listener.py", LISTENER_V1)
    sandbox.commit("A")
    sandbox.git("checkout", "-q", "-b", "feature/dotcom-fix")
    sandbox.write("app/listener.py", LISTENER_V2)
    head = sandbox.commit("B")

    outcome = await analyzer.analyze(make_event(sandbox, ZERO, head))

    assert outcome.status == "analyzed"
    assert llm.call_count == 1
    assert [f.path for f in llm.requests[0].files] == ["app/listener.py"]


async def test_unwatched_branch_is_skipped(sandbox, settings, llm, slack) -> None:
    configured = type(settings)(
        github_webhook_secret="x",
        llm_provider="null",
        bot_data_dir=settings.bot_data_dir,
        watched_branches=["main", "release"],
    )
    analyzer = ChangeAnalyzer(configured, llm=llm, slack=slack)

    sandbox.write("a.py", "x = 1\n")
    before = sandbox.commit("A")
    sandbox.write("a.py", "x = 2\n")
    after = sandbox.commit("B")

    outcome = await analyzer.analyze(make_event(sandbox, before, after))
    assert outcome.status == "skipped"
    assert llm.call_count == 0
    assert slack.messages == []


# ------------------------------------------------------------ large pushes
async def test_large_push_uses_hierarchical_summarisation(sandbox, settings, llm, slack) -> None:
    configured = type(settings)(
        github_webhook_secret="x",
        llm_provider="null",
        bot_data_dir=settings.bot_data_dir,
        hierarchical_file_threshold=5,
        hierarchical_max_groups=4,
    )
    analyzer = ChangeAnalyzer(configured, llm=llm, slack=slack)

    sandbox.write("seed.py", "x = 1\n")
    before = sandbox.commit("A")
    for area in ("routing", "mcp", "listener"):
        for index in range(4):
            sandbox.write(f"app/{area}/mod_{index}.py", f"def fn_{index}():\n    return {index}\n")
    after = sandbox.commit("B")

    outcome = await analyzer.analyze(make_event(sandbox, before, after))

    assert outcome.status == "analyzed"
    assert len(llm.requests) == 3  # one per subsystem
    assert len(llm.syntheses) == 1  # plus one synthesis
    assert outcome.llm_calls == 4
    assert {name for name, _ in llm.syntheses[0]} == {"app/routing", "app/mcp", "app/listener"}


async def test_context_stays_bounded_for_a_large_file(sandbox, settings, llm, slack) -> None:
    configured = type(settings)(
        github_webhook_secret="x",
        llm_provider="null",
        bot_data_dir=settings.bot_data_dir,
        max_context_chars=6000,
        max_file_context_chars=3000,
        max_diff_chars=8000,
    )
    analyzer = ChangeAnalyzer(configured, llm=llm, slack=slack)

    huge = "\n".join(f"def fn_{i}():\n    return {i}\n" for i in range(3000))
    sandbox.write("app/huge.py", huge)
    before = sandbox.commit("A")
    sandbox.write("app/huge.py", huge.replace("return 5\n", "return 55\n"))
    after = sandbox.commit("B")

    await analyzer.analyze(make_event(sandbox, before, after))

    request = llm.requests[0]
    assert request.char_size() <= 12_000
    assert len(request.files[0].code_context) <= 3000


# ---------------------------------------------------------------- worker
async def test_worker_processes_a_queued_job(sandbox, settings, llm, slack) -> None:
    analyzer = ChangeAnalyzer(settings, llm=llm, slack=slack)
    worker = Worker(settings, analyzer=analyzer)

    sandbox.write("app/listener.py", LISTENER_V1)
    before = sandbox.commit("A")
    sandbox.write("app/listener.py", LISTENER_V2)
    after = sandbox.commit("B")

    job, created = worker.queue.enqueue(make_event(sandbox, before, after))
    assert created

    claimed = worker.queue.claim_next()
    await worker.process(claimed)

    assert worker.queue.get(job.id).status.value == "completed"
    assert llm.call_count == 1
    await worker.aclose()


async def test_worker_retries_a_failing_job(sandbox, settings, slack) -> None:
    class BrokenLLM(FakeLLM):
        async def summarize_change(self, request):
            raise RuntimeError("model exploded")

    analyzer = ChangeAnalyzer(settings, llm=BrokenLLM(), slack=slack)
    worker = Worker(settings, analyzer=analyzer)

    sandbox.write("a.py", "x = 1\n")
    before = sandbox.commit("A")
    sandbox.write("a.py", "x = 2\n")
    after = sandbox.commit("B")

    job, _ = worker.queue.enqueue(make_event(sandbox, before, after))
    await worker.process(worker.queue.claim_next())

    stored = worker.queue.get(job.id)
    assert stored.status.value == "queued"  # scheduled for retry
    assert "model exploded" in stored.last_error
    await worker.aclose()


async def test_worker_fails_permanently_on_unknown_revision(sandbox, settings, llm, slack) -> None:
    analyzer = ChangeAnalyzer(settings, llm=llm, slack=slack)
    worker = Worker(settings, analyzer=analyzer)

    sandbox.write("a.py", "x = 1\n")
    sandbox.commit("A")

    job, _ = worker.queue.enqueue(make_event(sandbox, "c" * 40, "d" * 40))
    await worker.process(worker.queue.claim_next())

    stored = worker.queue.get(job.id)
    assert stored.status.value == "failed"
    assert stored.attempt_count == 1  # no retries burned
    assert llm.call_count == 0
    await worker.aclose()
