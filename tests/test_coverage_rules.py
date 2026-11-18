"""质量规则：覆盖门槛、查重、空话检测、可追溯性、闸门判定。"""

from __future__ import annotations

from atp.coverage.rules import (
    GATE_MIN_SCORE,
    coverage_by_category,
    coverage_by_feature,
    find_duplicates,
    gate_of,
    is_vague,
    review_rules,
)
from atp.models import Issue, TestCase, TestMap, TestPoint

TM = TestMap(
    prd_id="t",
    features=[
        {"id": "F-01", "name": "领取", "summary": "s", "rules": ["限领 3 张"]},
        {"id": "F-02", "name": "使用", "summary": "s", "rules": []},
    ],
)


def _point(pid, fid="F-01", cat="normal", title="用户第1次领取优惠券成功，库存减1，埋点 stock 差值=1"):
    return TestPoint(id=pid, feature_id=fid, category=cat, title=title, priority="P1")


def _case(cid, pid, title="用户第1次领取优惠券成功，库存减1，埋点 stock 差值=1"):
    return TestCase(
        id=cid,
        test_point_id=pid,
        title=title,
        category="normal",
        priority="P1",
        steps=[{"action": "调用领取接口 couponId=C1", "expected": "HTTP 200，库存 10→9"}],
        test_data={"couponId": "C1"},
    )


def test_is_vague():
    assert is_vague("功能正常") is True
    assert is_vague("页面显示正常") is True
    assert is_vague("领取成功后库存从 10 减为 9，埋点 stock 差值=1") is False


def test_find_duplicates():
    a = _case("TC-001", "TP-001", "满100减20券在订单金额100元时抵扣20元")
    b = _case("TC-002", "TP-002", "满100减20券在订单金额100元时抵扣20元")
    c = _case("TC-003", "TP-003", "满100减20券在订单金额99.99元时提示未达门槛")
    dupes = find_duplicates([a, b, c])
    assert (dupes[0][0], dupes[0][1]) == ("TC-001", "TC-002")
    assert len(dupes) == 1


def test_missing_category_is_blocker():
    points = [_point("TP-1"), _point("TP-2", cat="exception"), _point("TP-3", fid="F-02", cat="exception")]
    issues, counts, score = review_rules(TM, points, [_case("TC-1", "TP-1")])
    assert counts["normal"] == 1
    blockers = [i for i in issues if i.severity == "blocker"]
    assert any("boundary" in i.message for i in blockers)
    assert score < 100


def test_feature_without_points_blocks():
    points = [_point("TP-1", cat="boundary"), _point("TP-2", cat="exception")]
    issues, _, _ = review_rules(TM, points, [_case("TC-1", "TP-1")])
    assert any("F-02" in i.message and i.severity == "blocker" for i in issues)


def test_orphan_case_blocks():
    points = [_point("TP-1"), _point("TP-2", fid="F-02")]
    cases = [_case("TC-1", "TP-999")]
    issues, _, _ = review_rules(TM, points, cases)
    assert any(i.type == "traceability" and i.severity == "blocker" for i in issues)


def test_gate_logic():
    assert gate_of([], 100) == "pass"
    assert gate_of([Issue(type="duplicate", severity="major", message="x")], 60) == "revise"
    assert gate_of([], GATE_MIN_SCORE - 1) == "revise"


def test_coverage_helpers():
    points = [_point("TP-1"), _point("TP-2", cat="security"), _point("TP-3", fid="F-02")]
    cats = coverage_by_category(points)
    assert cats["normal"] == 2 and cats["security"] == 1 and cats["compat"] == 0
    by_feat = coverage_by_feature(points, TM)
    assert by_feat == {"F-01": 2, "F-02": 1}
