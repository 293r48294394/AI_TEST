"""接口层：响应构建、导出闸门标记、采纳率聚合。"""

from __future__ import annotations

from app import api
from atp.models import AdoptionVerdict, Issue, ReviewReport, RunState


def _state(**kw) -> RunState:
    return RunState(run_id="r-test", prd_id="p", mode="rag_hitl", **kw)


def test_health_reports_mode():
    h = api.health()
    assert h["ok"] is True
    assert h["llm_mode"] in {"mock", "deepseek"}
    assert isinstance(h["live_llm"], bool)


def test_state_payload_blocks_export_when_revise():
    state = _state()
    state.review = ReviewReport(
        score=30,
        gate="revise",
        coverage={},
        issues=[Issue(type="coverage", severity="blocker", message="缺边界")],
        suggestions=[],
    )
    payload = api._state_payload(state)
    assert payload["export_allowed"] is False


def test_state_payload_allows_export_when_pass():
    state = _state()
    state.review = ReviewReport(score=90, gate="pass", coverage={}, issues=[], suggestions=[])
    assert api._state_payload(state)["export_allowed"] is True


def test_adoption_rate_unmeasured_is_none():
    payload = api._state_payload(_state())
    assert payload["adoption_rate"]["rate"] is None
    assert payload["adoption_rate"]["total"] == 0


def test_adoption_rate_math():
    state = _state()
    state.adoption = [
        AdoptionVerdict(case_id="TC-1", verdict="usable"),
        AdoptionVerdict(case_id="TC-2", verdict="needs_edit"),
    ]
    rate = api._adoption_rate(state)
    assert rate["total"] == 2 and rate["usable"] == 1 and rate["rate"] == 0.5
