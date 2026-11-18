"""评测指标：场景级匹配、隐性规则召回、格式统计、采纳率。"""

from __future__ import annotations

from atp.eval.metrics import (
    GoldenPoint,
    adoption_stats,
    fidelity_heuristic,
    format_stats,
    match_points,
)
from atp.models import AdoptionVerdict, RunState, StageRecord, TestPoint


def _g(i, title, cat="normal", implicit=False):
    return GoldenPoint(
        id=f"G-{i:03d}",
        feature="领取",
        category=cat,
        title=title,
        expectation="",
        priority="P1",
        source="PRD 4.2 脚注①",
        implicit_rule=implicit,
    )


def _p(i, title, cat="normal"):
    return TestPoint(id=f"TP-{i:03d}", feature_id="F-01", category=cat, title=title, priority="P1")


def test_recall_precision_on_exact_matches():
    golden = [_g(1, "单用户第4次领取同一券时报错且库存不扣减"), _g(2, "订单金额恰好100元时可使用满100减20券", "boundary")]
    points = [
        _p(1, "单用户第4次领取同一券时报错且库存不扣减"),
        _p(2, "订单金额恰好100元时可使用满100减20券", "boundary"),
        _p(3, "完全不相关的场景：修改用户头像"),
    ]
    res = match_points(points, golden)
    assert res.recall == 1.0
    assert res.matched_generated == 2
    assert res.precision == round(2 / 3, 4)
    assert res.unmatched_generated == ["TP-003"]


def test_implicit_recall_separates_hidden_rules():
    golden = [
        _g(1, "单用户限领 3 张含跨活动累计", implicit=True),
        _g(2, "券过期后不可恢复", implicit=False),
        _g(3, "部分退款按比例返还最低 0.01 元", implicit=True),
    ]
    points = [_p(1, "券过期后不可恢复"), _p(2, "部分退款按实付比例返还，最低 0.01 元")]
    res = match_points(points, golden)
    assert res.implicit_total == 2
    assert res.implicit_matched == 1
    assert res.implicit_recall == 0.5
    assert res.recall == round(2 / 3, 4)


def test_threshold_excludes_weak_matches():
    golden = [_g(1, "库存并发扣减不超卖，1000 并发下库存从 100 减为 0")]
    points = [_p(1, "创建优惠券模板成功入库")]
    res = match_points(points, golden, threshold=0.55)
    assert res.recall == 0.0
    assert res.missed[0].id == "G-001"


def test_empty_inputs_are_safe():
    res = match_points([], [_g(1, "x")])
    assert res.recall == 0.0 and len(res.missed) == 1
    res2 = match_points([_p(1, "用户领取优惠券成功且库存扣减 1")], [])
    assert res2.recall == 0.0


def test_fidelity_heuristic_rewards_numbers():
    golden = [_g(1, "单用户第4次领取同一券时报错"), _g(2, "订单金额恰好100元时可使用满100减20券", "boundary")]
    points = [_p(1, "单用户第4次领取同一券时报错"), _p(2, "订单金额恰好100元时可使用满100减20券", "boundary")]
    res = match_points(points, golden)
    fid = fidelity_heuristic(points, golden, res.pairs)
    assert fid.judged == 2
    assert fid.faithful_rate == 1.0


def test_format_stats_counts_repairs():
    state = RunState(run_id="r", prd_id="p", mode="rag")
    state.stages = [
        StageRecord(stage="s1_testmap", metrics={"attempts": 1.0, "first_try_ok": 1.0}),
        StageRecord(stage="s2_testpoints", metrics={"attempts": 2.0, "first_try_ok": 0.0}),
        StageRecord(stage="s3_testcases", metrics={"batches": 3.0, "first_try_ok_batches": 2.0, "repair_attempts": 1.0}),
    ]
    stats = format_stats(state)
    # 单元：s1(1) + s2(1) + s3(3 批) = 5；一次通过：s1 + s3 的 2 批 = 3
    assert stats["units"] == 5
    assert stats["first_try"] == 3
    assert stats["total_repairs"] == 2
    assert stats["first_try_rate"] == 0.6


def test_adoption_stats_and_empty_case():
    state = RunState(run_id="r", prd_id="p", mode="rag")
    assert adoption_stats(state)["rate"] is None
    state.adoption = [
        AdoptionVerdict(case_id="TC-1", verdict="usable"),
        AdoptionVerdict(case_id="TC-2", verdict="needs_edit"),
        AdoptionVerdict(case_id="TC-3", verdict="unusable"),
        AdoptionVerdict(case_id="TC-4", verdict="usable"),
    ]
    stats = adoption_stats(state)
    assert stats["rate"] == 0.5
    assert stats["total"] == 4
