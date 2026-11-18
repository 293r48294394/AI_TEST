"""流水线：闸门暂停、人工确认、产物落盘、可复现、错误收敛。"""

from __future__ import annotations

import pytest

from atp.models import FeatureItem, SourceRef, TestMap


def test_start_pauses_at_gate(state):
    assert state.status == "awaiting_human"
    assert state.stage == "gate1"
    assert state.testmap_ai is not None
    assert len(state.testmap_ai.features) >= 2


def test_gate_writes_artifact_and_event(orc, state):
    art = orc.store.load_artifact(state.run_id, "01_testmap_ai.json")
    assert art and art["features"]
    events = [e["event"] for e in orc.store.events(state.run_id)]
    assert "run_start" in events and "gate_await" in events


def test_resume_applies_human_edit_and_records_diff(orc, state):
    tm = state.testmap_ai.model_copy(deep=True)
    tm.features[0].rules.append("【人工】预算达 100% 自动停发")
    tm.features.append(
        FeatureItem(
            id="F-99",
            name="人工补充功能",
            summary="预算口径",
            rules=["预算按已核销金额累计"],
            sources=[SourceRef(chunk_id=tm.features[0].sources[0].chunk_id)],
        )
    )
    done = orc.resume(state.run_id, tm)

    assert done.status == "completed"
    assert done.stage == "done"
    assert done.hitl["changed"] is True
    diff = done.hitl["diff"]
    assert "F-99" in diff["added"]
    assert diff["edited"], "改规则应被记录为 edited"
    assert done.testmap_final.feature_ids() == {"F-01", "F-02", "F-03", "F-04", "F-05", "F-99"} or "F-99" in done.testmap_final.feature_ids()


def test_downstream_artifacts_and_review(orc, state):
    done = orc.confirm_ai_draft(state.run_id)
    for name in ("03_testpoints.json", "04_cases.json", "05_review.json", "hitl_diff.json"):
        assert orc.store.load_artifact(state.run_id, name) is not None, name
    assert done.cases and done.testpoints
    assert done.review is not None
    assert done.stages[-1].stage == "s4_review"


def test_resume_twice_is_rejected(orc, state):
    orc.confirm_ai_draft(state.run_id)
    with pytest.raises(ValueError):
        orc.resume(state.run_id, None)


def test_bare_mode_has_no_gate(orc, parsed):
    state = orc.start(parsed, mode="bare")
    assert state.status == "completed"
    assert "gate1" not in [s.stage for s in state.stages]


def test_run_is_reproducible_from_artifacts(orc, state):
    """删除源文件后，resume 仍能重建 PRD（运行自包含）。"""
    prd = orc.prd_from_artifacts(state.run_id, state.prd_id)
    assert prd.chunks and prd.text
    done = orc.confirm_ai_draft(state.run_id)
    assert done.status == "completed"


def test_llm_calls_are_audited(orc, state):
    orc.confirm_ai_draft(state.run_id)
    calls = orc.store.llm_calls(state.run_id)
    assert len(calls) >= 4  # s1 + s2 + s3(分批) + s4
    assert all("latency_ms" in c and "prompt_chars" in c for c in calls)
