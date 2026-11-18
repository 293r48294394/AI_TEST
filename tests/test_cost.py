"""成本核算：估算口径要可解释，金额缺失时不能编。"""

from __future__ import annotations

from atp.eval.cost import estimate_tokens, per_case_cost, render_markdown, summarize

CALLS = [
    {"task": "s1_testmap", "ok": True, "prompt_chars": 6000, "response_chars": 1500, "est_prompt_tokens": 3200},
    {"task": "s2_testpoints", "ok": True, "prompt_chars": 4000, "response_chars": 3000},
    {"task": "s3_testcases", "ok": False, "prompt_chars": 5000, "response_chars": 0},
]


def test_estimate_tokens_mixed_text():
    assert estimate_tokens(0) == 0
    # 全中文：1 字 ≈ 1 token
    assert estimate_tokens(100, cjk_ratio=1.0) == 100
    # 全英文：约 3.5 字符 1 token
    assert estimate_tokens(350, cjk_ratio=0.0) == 100


def test_summarize_totals_and_failures():
    s = summarize(CALLS)
    assert s.calls == 3
    assert s.failed == 1
    assert s.prompt_chars == 15000
    assert s.response_chars == 4500
    assert s.est_input_tokens == 3200 + estimate_tokens(4000) + estimate_tokens(5000)
    assert set(s.by_task) == {"s1_testmap", "s2_testpoints", "s3_testcases"}
    assert s.by_task["s1_testmap"]["calls"] == 1


def test_cost_requires_prices():
    s = summarize(CALLS)
    assert s.est_cost is None, "没给单价就不许算钱"
    priced = summarize(CALLS, price_in=1.0, price_out=2.0)
    expected = priced.est_input_tokens / 1e6 * 1.0 + priced.est_output_tokens / 1e6 * 2.0
    assert abs(priced.est_cost - round(expected, 4)) < 1e-6


def test_per_case_cost():
    s = summarize(CALLS, price_in=1.0, price_out=2.0)
    assert per_case_cost(s, 0) is None
    assert per_case_cost(s, 20) == round(s.est_cost / 20, 4)


def test_render_uses_dash_when_unpriced():
    md = render_markdown([("run-1", summarize(CALLS), 20)])
    assert "| — |" in md
    assert "非账单值" in md
