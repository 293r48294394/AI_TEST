"""Fidelity judge 与阈值敏感性：离线用假客户端验证逻辑正确，不花钱。"""

from __future__ import annotations

import json

from atp.config import Settings
from atp.eval.judge import agreement_with_human, judge_fidelity
from atp.eval.metrics import GoldenPoint, match_points, sweep_threshold
from atp.llm.base import CallResult
from atp.models import TestPoint


class FakeJudge:
    mode = "deepseek"
    model = "fake-judge"

    def __init__(self, scores: list[int]):
        self.scores = scores
        self.prompts: list[str] = []

    def chat(self, messages, *, task, json_mode=True, max_tokens=4096, payload=None):
        assert task == "s5_fidelity_judge"
        self.prompts.append(messages[-1].content)
        n = len((payload or {}).get("pairs", []))
        verdicts = [{"pair_index": i, "score": self.scores[i], "reason": f"假数据 {i}"} for i in range(n)]
        return CallResult(json.dumps({"verdicts": verdicts}, ensure_ascii=False))


def _g(i, title, cat="normal"):
    return GoldenPoint(
        id=f"G-{i:03d}", feature="f", category=cat, title=title, expectation="", priority="P1", source="", implicit_rule=False
    )


def _p(i, title, cat="normal"):
    return TestPoint(id=f"TP-{i:03d}", feature_id="F-01", category=cat, title=title, priority="P1")


GOLDEN = [_g(1, "部分退款按实付比例返还，最低 0.01 元"), _g(2, "订单金额恰好 100 元时可使用满减券", "boundary")]
POINTS = [_p(1, "部分退款按实付比例返还，最低 0.01 元"), _p(2, "订单金额恰好 100 元时可使用满减券", "boundary")]


def test_judge_marks_faithful_and_unfaithful():
    pairs = match_points(POINTS, GOLDEN).pairs
    assert len(pairs) == 2
    client = FakeJudge([5, 3])
    result = judge_fidelity(client, Settings(max_repair=1), POINTS, GOLDEN, pairs)
    assert result.judge == "llm"
    assert result.judged == 2
    assert result.mean_score == 4.0
    assert result.faithful_rate == 0.5
    assert len(result.details) == 2
    # 提示词必须同时包含基准点与生成点，否则 judge 无从判断
    prompt = client.prompts[0]
    assert "最低 0.01 元" in prompt and "TP-001" not in prompt  # 只给文本不给 id


def test_judge_handles_no_pairs():
    result = judge_fidelity(FakeJudge([]), Settings(), POINTS, GOLDEN, [])
    assert result.judged == 0


def test_agreement_with_human():
    rows = [{"golden": "G-001", "score": 5}, {"golden": "G-002", "score": 3}]
    assert agreement_with_human(rows, {"G-001": 4, "G-002": 2}) == 1.0
    assert agreement_with_human(rows, {"G-001": 1, "G-002": 5}) == 0.0
    assert agreement_with_human(rows, {}) is None


def test_sweep_threshold_is_monotonic_in_recall():
    rows = sweep_threshold(POINTS, GOLDEN, thresholds=(0.4, 0.6, 0.9))
    recalls = [r["recall"] for r in rows]
    assert recalls == sorted(recalls, reverse=True), f"阈值升高，召回不应上升：{recalls}"
    assert rows[-1]["matched"] <= rows[0]["matched"]
    assert all(0.0 <= r["precision"] <= 1.0 for r in rows)
