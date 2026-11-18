# 声称 → 证据对照表

写简历、答面试、改 README 之前先过一遍这张表。
**规则：没有"强"级证据的句子，不许写进简历。**

强度定义：

| 强度 | 含义 | 能不能写进简历 |
|---|---|---|
| **强** | 代码 + 自动化测试双重证据，任何人 clone 后能复现 | 可以 |
| **中** | 有实现，但只做了局部验证（如未做端到端、未做致率抽检） | 可以，但必须限定范围 |
| **弱** | 仅单次观察或仅基线数据，不能外推 | 不可以，只能作为"下一步"讲 |

---

## 一、平台能力

| 声称 | 证据 | 复现 | 强度 |
|---|---|---|---|
| PRD→测试点→用例 四阶段流水线 | `atp/skills/s1~s4.py`、`atp/pipeline/orchestrator.py` | `python run.py run --prd ...` | 强 |
| 每个 Skill 可独立调用与单测 | `tests/test_pipeline.py` 等 | `pytest tests/test_pipeline.py` | 强 |
| 人工确认闸门阻塞下游 | `orchestrator.resume()` 仅接受 `stage=gate1`；`test_resume_twice_is_rejected` | 同左 | 强 |
| 人工改动可审计 | `hitl_diff.json`、`docs/examples/hitl-diff-example.json` | 控制台改测试地图后提交 | 强 |
| JSON 契约校验 + 错误回喂重试 | `atp/llm/schema.py`、`tests/test_schema_repair.py` | `pytest tests/test_schema_repair.py` | 强 |
| 引用完整性校验（只能引用检索块） | `make_citation_validator`、`test_citation_validator_rejects_unknown_chunk` | 同左 | 强（逻辑）／中（真实模型触发次数未测） |
| 评审不过禁止导出 | `atp/export/xlsx.py::build_workbook`、`tests/test_export.py::test_export_blocked_when_revise` | 同左 | 强 |
| RAG 检索（BM25 + 符号加权 + RRF） | `atp/retrieval/bm25.py`、`tests/test_retrieval.py` | 同左 | 强 |
| 跨领域不依赖（两份 PRD） | `tests/test_cross_domain.py`、`data/prd/account-login-prd.md` | `pytest tests/test_cross_domain.py` | 强 |
| 批量并发产物隔离 | `atp/pipeline/batch.py`、`tests/test_batch.py` | `python run.py batch --prds a,b` | 强 |
| 前端可实际操作 | `web/index.html` + `scripts/check_web.js`（模板编译） + API 链路冒烟 | `node scripts/check_web.js`、`python run.py serve` | 中（无浏览器端交互自动化） |

## 二、评测与质量

| 声称 | 证据 | 复现 | 强度 |
|---|---|---|---|
| 三模式对比评测器 | `atp/eval/runner.py` | `python run.py eval ...` | 强（机制） |
| 42 条基准集（20 条隐性规则） | `data/golden/coupon-golden.json`、`docs/examples/golden-review-applied.diff` | `python run.py lint-golden ...` | 强（已人工复核：39 通过 / 3 修正；单一复核人，无 κ 一致性） |
| 基准集机器质检 0 error | `atp/eval/golden_lint.py`、`tests/test_golden_lint.py` | 同左 | 强 |
| Match Rate 30.9% → 33.3% → 35.7%（三模式） | `reports/eval-coupon-center-prd-*.md`（2026-11-18 真实模型） | `python run.py eval ...` | 强（真实模型、可复现；样本 42 条，无置信区间） |
| 隐性规则召回 20% → 25% → 35%（4→5→7 / 20） | 同上 | 同上 | 强（同上，样本仅 20 条隐性规则） |
| 阈值敏感性分析 | `sweep_threshold`、`tests/test_eval_judge.py` | `eval --sweep` | 中（曲线有了，解读待真实模型） |
| Fidelity 13–29%（三模式，LLM judge） | `atp/eval/judge.py`、报告 `fidelity_*` 字段 | `eval --judge llm` | 中（judge 未与人工评分做致率抽检，只能当"参考方向"） |
| 采纳率 | `POST /adoption` + `adoption_stats` | 界面逐条裁决 | **弱**（无分母，未实测） |

## 三、成本与性能

| 声称 | 证据 | 复现 | 强度 |
|---|---|---|---|
| 单份 PRD 约 2.57 万输入 / 1.24 万输出 token | `llm_calls.jsonl` + `atp/eval/cost.py` | `python run.py cost --run <id>` | 中（估算，非账单） |
| 金额 | —（不内置单价） | `--price-in/--price-out` | 不适用（由使用者传入） |
| 31 份/秒吞吐 | mock 批量输出 | `python run.py batch ...` | **弱**（管道开销，不调模型） |

## 四、明确不能声称的事

| 不能声称 | 原因 |
|---|---|
| "AI 生成用例准确率 89%" | 89 是评审分，不是准确率；且来自规则基线 |
| "提升测试效率 X%" | 没有任何计时对照实验 |
| "人工标注基准集" | 尚未人工复核，只有机器质检 |
| "支持任意 PRD / 大规模" | 只验了 2 份中文需求文档、单机同步执行 |
| "Fidelity 达 X%" | 代理指标不是语义判断；judge 未抽检 |

---

## 五、把"弱"变"强"的最小动作

| 现在 | 变成强证据需要 |
|---|---|
| 基准集未经复核 | 用 `reports/golden-review-sheet.md` 逐条复核，改 `annotator`/`version` |
| 只有基线数字 | 配 `LLM_API_KEY`，跑 `eval --judge llm` |
| 采纳率无分母 | 界面逐条裁决 ≥30 条 |
| judge 未抽检 | 抽 10 对人工打分，`agreement_with_human()` ≥80% |
| 吞吐是管道开销 | 真实模型下测 10 份，记录 P50/P95 与失败率 |
