"""评审接地：评审必须看到 PRD 原文，否则会把 PRD 里有的错误码判成"幻觉"。

背景（2026-11-20 实跑发现）：真实运行里评审报了 6 条问题，逐条对 PRD 原文核对后
3 条是误判——`COUPON_DISCOUNT_INVALID` 写在 PRD 4.1.3、`coupon_receive_blocked`
写在表 7-1。根因是 s4 的 prompt 只给了规则列表，一条原文都没有。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from atp.models import FeatureItem, SourceRef, Step, TestCase, TestMap, TestPoint
from atp.skills import s4_review

PRD_CHUNK_TEXT = "折扣率区间 [0.80, 0.99]，超出返回 COUPON_DISCOUNT_INVALID；券发布后仅可下架。"


def _tm() -> TestMap:
    return TestMap(
        prd_id="p",
        features=[
            FeatureItem(
                id="F-01",
                name="券模板创建",
                summary="",
                rules=["折扣率区间 [0.80, 0.99]，超出返回 COUPON_DISCOUNT_INVALID"],
                sources=[SourceRef(chunk_id="P-0004", quote="超出返回 COUPON_DISCOUNT_INVALID")],
            )
        ],
    )


def _chunks() -> list:
    return [SimpleNamespace(chunk_id="P-0004", heading="4.1 优惠券创建", text=PRD_CHUNK_TEXT)]


def test_evidence_includes_cited_chunk_text():
    block = s4_review._evidence(_tm(), _chunks())
    assert "[P-0004]" in block
    assert "COUPON_DISCOUNT_INVALID" in block
    assert "4.1 优惠券创建" in block


def test_evidence_without_chunks_tells_judge_not_to_cry_hallucination():
    block = s4_review._evidence(_tm(), None)
    assert "不得判幻觉" in block


def test_evidence_skips_uncited_chunks():
    extra = SimpleNamespace(chunk_id="P-0099", heading="没人引用", text="无关内容")
    block = s4_review._evidence(_tm(), _chunks() + [extra])
    assert "无关内容" not in block


def test_system_prompt_requires_grounding():
    """判幻觉前必须核对原文；这条约束不能只写在注释里。"""
    assert "原文" in s4_review.SYSTEM
    assert "不算幻觉" in s4_review.SYSTEM


def test_semantic_prompt_carries_evidence(monkeypatch):
    """真跑一遍 s4.run，确认喂给模型的 user 里带着原文。"""
    captured: dict = {}

    def fake_call_json(client, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(value=s4_review.SemanticReview(semantic_issues=[], suggestions=[]))

    monkeypatch.setattr(s4_review, "call_json", fake_call_json)

    point = TestPoint(id="TP-001", feature_id="F-01", category="boundary", title="折扣率边界值校验")
    case = TestCase(
        id="TC-001",
        test_point_id="TP-001",
        title="折扣率 0.79 与 1.00 均被拒绝",
        category="boundary",
        steps=[Step(action="提交折扣率 0.79", expected="返回 COUPON_DISCOUNT_INVALID")],
    )

    s4_review.run(
        client=object(),  # call_json 已打桩，client 不被使用
        settings=SimpleNamespace(max_repair=1),
        tm=_tm(),
        points=[point],
        cases=[case],
        chunks=_chunks(),
    )

    user = captured["user"]
    assert "PRD 原文" in user
    assert "COUPON_DISCOUNT_INVALID" in user
    assert "P-0004" in user


def test_review_runs_without_chunks(monkeypatch):
    """没有原文时也要能跑完，只是不允许判幻觉。"""

    class Boom:
        def chat(self, *a, **kw):  # pragma: no cover - 不该被调用到
            raise AssertionError("不应调用模型")

    monkeypatch.setattr(
        s4_review, "call_json", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("模拟模型失败"))
    )
    report = s4_review.run(
        client=Boom(),
        settings=SimpleNamespace(max_repair=1),
        tm=_tm(),
        points=[],
        cases=[
            TestCase(
                id="TC-001",
                test_point_id="TP-001",
                title="折扣率 0.79 被拒绝",
                category="boundary",
                steps=[Step(action="提交 0.79", expected="报错")],
            )
        ],
        chunks=None,
    )
    assert report.suggestions  # 语义评审失败会降级成一条说明，而不是整条流水线失败
    assert any("语义评审未能完成" in s for s in report.suggestions)


@pytest.mark.parametrize("score_floor", [0])
def test_score_never_negative(score_floor):
    report = s4_review.run(
        client=object(),
        settings=SimpleNamespace(max_repair=1),
        tm=_tm(),
        points=[],
        cases=[],
        chunks=None,
        use_semantic=False,
    )
    assert report.score >= score_floor
