"""批量并发：多份文档互不干扰，单个失败不拖垮整批。"""

from __future__ import annotations

from atp.pipeline.batch import run_batch


def test_batch_runs_both_documents(settings):
    result = run_batch(settings, ["coupon-center-prd", "account-login-prd"], mode="bare", workers=2)
    assert len(result.items) == 2
    assert result.success == 2, [i.error for i in result.items]
    assert result.failed == 0
    assert len({i.run_id for i in result.items}) == 2, "run_id 必须唯一，否则产物会互相覆盖"
    assert all(i.points > 0 and i.cases > 0 for i in result.items)
    assert all(i.gate in {"pass", "revise"} for i in result.items)

    summary = result.summary()
    assert summary["documents"] == 2
    assert summary["failure_rate"] == 0.0
    assert summary["wall_ms"] > 0
    assert summary["throughput_per_sec"] > 0


def test_batch_records_failure_without_raising(settings):
    result = run_batch(settings, ["coupon-center-prd", "不存在的PRD"], mode="bare", workers=2)
    assert result.failed == 1
    bad = next(i for i in result.items if i.prd_id == "不存在的PRD")
    assert bad.ok is False
    assert "FileNotFoundError" in bad.error
    # 成功的那个不受影响
    good = next(i for i in result.items if i.prd_id == "coupon-center-prd")
    assert good.ok is True
