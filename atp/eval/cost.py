"""成本核算：把 llm_calls.jsonl 换算成 token 与金额。

两个刻意的设计：
1. **不内置价格**。不同时间/不同模型的价目会变，写死一个数字就是在制造假数据。
   单价通过参数或环境变量传入（元 / 百万 token），没传就只报 token、成本显示"—"。
2. **token 是估算，不是计费值**。审计日志只记了字符数（模型返回的 usage 字段不在
   统一契约里），这里按「70% 中文按 1 字 1 token，30% 英文按 3.5 字符 1 token」折算，
   并在报告里标注这是估算。真实计费以平台账单为准。
"""

from __future__ import annotations

from dataclasses import dataclass, field


def estimate_tokens(chars: int, cjk_ratio: float = 0.7) -> int:
    """按中英混排比例把字符数折算成 token。"""
    if chars <= 0:
        return 0
    cjk = chars * cjk_ratio
    other = chars * (1 - cjk_ratio)
    return int(cjk + other / 3.5)


@dataclass
class CostSummary:
    calls: int = 0
    failed: int = 0
    prompt_chars: int = 0
    response_chars: int = 0
    est_input_tokens: int = 0
    est_output_tokens: int = 0
    by_task: dict[str, dict] = field(default_factory=dict)
    price_in: float | None = None  # 元 / 百万 token
    price_out: float | None = None
    est_cost: float | None = None

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "failed": self.failed,
            "prompt_chars": self.prompt_chars,
            "response_chars": self.response_chars,
            "est_input_tokens": self.est_input_tokens,
            "est_output_tokens": self.est_output_tokens,
            "by_task": self.by_task,
            "price_in": self.price_in,
            "price_out": self.price_out,
            "est_cost": self.est_cost,
        }


def summarize(
    llm_calls: list[dict],
    *,
    price_in: float | None = None,
    price_out: float | None = None,
) -> CostSummary:
    summary = CostSummary(price_in=price_in, price_out=price_out)
    for call in llm_calls:
        task = str(call.get("task") or "unknown")
        p_chars = int(call.get("prompt_chars") or 0)
        r_chars = int(call.get("response_chars") or 0)
        in_tok = int(call.get("est_prompt_tokens") or estimate_tokens(p_chars))
        out_tok = estimate_tokens(r_chars)

        summary.calls += 1
        summary.failed += 0 if call.get("ok") else 1
        summary.prompt_chars += p_chars
        summary.response_chars += r_chars
        summary.est_input_tokens += in_tok
        summary.est_output_tokens += out_tok

        row = summary.by_task.setdefault(task, {"calls": 0, "in_tokens": 0, "out_tokens": 0})
        row["calls"] += 1
        row["in_tokens"] += in_tok
        row["out_tokens"] += out_tok

    if price_in is not None and price_out is not None:
        cost = summary.est_input_tokens / 1_000_000 * price_in + summary.est_output_tokens / 1_000_000 * price_out
        summary.est_cost = round(cost, 4)
    return summary


def per_case_cost(summary: CostSummary, cases: int) -> float | None:
    """单条用例成本 —— 面试里最容易被问的"这玩意儿贵不贵"。"""
    if summary.est_cost is None or cases <= 0:
        return None
    return round(summary.est_cost / cases, 4)


def render_markdown(rows: list[tuple[str, CostSummary, int]], title: str = "成本估算") -> str:
    """rows: [(标签, 汇总, 用例数)]"""
    lines = [
        f"# {title}",
        "",
        "> token 为估算值（中文 1 字≈1 token，英文 3.5 字符≈1 token），非账单值；",
        "> 未配置单价时只给 token，不猜金额。",
        "",
        "| 范围 | LLM 调用 | 失败调用 | 输入字符 | 输出字符 | 估算输入 token | 估算输出 token | 估算成本(元) | 单条用例(元) |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for label, s, cases in rows:
        cost = f"{s.est_cost:.4f}" if s.est_cost is not None else "—"
        unit = per_case_cost(s, cases)
        unit_s = f"{unit:.4f}" if unit is not None else "—"
        lines.append(
            f"| {label} | {s.calls} | {s.failed} | {s.prompt_chars} | {s.response_chars} | "
            f"{s.est_input_tokens} | {s.est_output_tokens} | {cost} | {unit_s} |"
        )
    return "\n".join(lines)
