# 交接说明（HANDOFF）

> 给"过几天回来继续"的你看的。当前状态、下一步、以及我需要你给什么。

## 一、现在是什么状态

平台侧已完成并验证：

| 模块 | 状态 | 证据 |
|---|---|---|
| 四 Skill 流水线 + 人工闸门 | 完成 | `pytest tests/test_pipeline.py` |
| JSON 契约 + 引用完整性校验 + 重试 | 完成 | `pytest tests/test_schema_repair.py` |
| RAG 检索（BM25 + 符号加权） | 完成 | `pytest tests/test_retrieval.py` |
| 评审规则 + 导出闸门 | 完成 | `pytest tests/test_export.py` |
| FastAPI + 单文件 Vue 控制台 | 完成 | `python run.py serve` |
| 三模式评测 + 阈值敏感性 + LLM judge | 完成（judge 未抽检） | `python run.py eval ... --sweep --judge llm` |
| 基准集质检（14 条规则） | 完成 | `python run.py lint-golden ...` |
| 跨文档泛化（2 份 PRD） | 完成 | `pytest tests/test_cross_domain.py` |
| 批量并发 + 成本核算 | 完成 | `python run.py batch ...` / `cost --run` |
| 测试 | `python -m pytest` 全绿（当前 105 个） | `python -m pytest` |
| 一键验证 | 6 步全绿 | `python scripts/verify.py` |

**未完成的只剩一件**：采纳率（需要你在控制台逐条裁决 ≥30 条用例）。

基准集已于 2026-11-18 人工复核完成（屈永康：39 通过 / 3 修正，机器质检 0 error），
证据在 `docs/examples/golden-review-applied.diff`。
真实模型评测也已跑完（deepseek-chat，2026-11-18）：三模式召回 30.9%/33.3%/35.7%，
隐性规则 20%/25%/35%，Fidelity 15.4%/28.6%/13.3%，数字已写入 README。

## 二、你回来后的四条命令

```bash
cd ai-testcase-platform

# 1) 复核基准集（判定标准与例子见 docs/review-guide.md）
python run.py review-export --golden data/golden/coupon-golden.json --out reports/golden-review.xlsx
#    用 Excel 打开，G 列选 通过/改/删，说明页 B1 填名字
python run.py review-import --golden data/golden/coupon-golden.json --xlsx reports/golden-review.xlsx --dry-run
python run.py review-import --golden data/golden/coupon-golden.json --xlsx reports/golden-review.xlsx

# 2) 跑真实模型（先把 LLM_API_KEY 填进 .env，ATP_LLM_MODE=deepseek）
python run.py eval --prd coupon-center-prd --golden data/golden/coupon-golden.json --sweep --judge llm

# 3) 起控制台，逐条裁决用例（采纳率需要有分母）
python run.py serve --port 8000

# 4) 全量自检（改完任何东西都跑这个）
python scripts/verify.py
```

## 三、需要你提供的两样东西

| 需要 | 用途 | 给完之后会发生什么 |
|---|---|---|
| `LLM_API_KEY`（写进 `.env`） | 真实三模式评测 | 替换 README 那张表、跑 `--judge llm`、做致率抽检 |

（基准集复核已完成，这一项不再需要你做。）

## 四、数字都在哪

| 问题 | 看哪里 |
|---|---|
| 评测结果 | `reports/eval-coupon-center-prd-*.md`、`reports/eval-account-login-prd-*.md` |
| 基准集质量 | `python run.py lint-golden ...` 的输出 |
| 单次运行的全部中间产物 | `data/runs/<run_id>/`（state / events / llm_calls / artifacts） |
| 人工改动示例 | `docs/examples/hitl-diff-example.json` |
| 哪些话能写进简历 | `docs/claims-and-evidence.md`（强/中/弱） |
| 面试怎么答 | `docs/interview-faq.md` |

## 五、不要做的事

1. **不要把 mock 数字写进简历**（2.4% 召回会让人以为项目没用，其实是基线没用）；
2. **不要在没有复核记录的情况下声称"人工标注"**——本项目已有 diff 证据；换 PRD 时要重新走一遍 `docs/review-guide.md`；
3. **不要填估算的采纳率**——没有分母就没有这个指标；
4. 不要把 `ATP_LLM_MODE` 留在 `deepseek` 就提交 `data/runs/`（含 PRD 全文，已在 gitignore，但别改）。
