# 简历叙事

规则先说清楚：

1. **`{{ }}` 里的数字必须来自真实运行**（`reports/eval-*.json`、`pytest` 输出、`llm_calls.jsonl`）。
   没跑过就删掉那句话，不要留占位符投出去，也不要估一个数。
2. 每条 bullet 的主语是"我做了什么验证"，不是"我用了什么技术"。
3. 技术栈写在末尾一行即可，不要堆在每条 bullet 里。

---

## 项目标题

**AI 测试用例生成平台（个人作品集项目）** ｜ Python · FastAPI · Vue3 · DeepSeek API

---

## 四条 bullet（填数后用）

1. 将 PRD→测试用例拆解为需求拆解、测试点生成、用例生成、质量评审四个可独立验证的 Skill，
   以显式状态机串联并在需求拆解后插入人工确认闸门，阻断未经复核的中间产物流入下游；
   人工改动以 `+{{n}}/-{{n}}/~{{n}}` 量化记录。

2. 构建 **42 条基准集**（其中 20 条来自 PRD 表格脚注/修订记录等隐性规则），
   并用确定性规则校验其质量（来源可定位/标题可判定/期望可观测/场景去重，当前 0 error），
   对比裸提示词 / RAG / RAG+人工确认三种模式：场景匹配率从 `{{x}}%` 提升至 `{{y}}%`，
   **隐性规则召回率从 `{{a}}%` 提升至 `{{b}}%`**，定位模型在 `{{具体类型}}` 场景上的稳定失效模式。

3. 针对 LLM 输出不可解析与幻觉问题，设计 JSON Schema 校验 + 错误回喂重试 + 证据引用完整性校验
   （引用 id 必须来自本次检索结果），格式一次通过率 `{{p}}%`，重试 `{{r}}` 次后收敛；
   评审闸门为 `revise` 时导出接口直接拒绝，避免低质量用例进入用例库。

4. 为平台自身编写 **70 个 pytest**（解析/检索/契约修复/覆盖规则/闸门/导出/指标，全离线可跑），
   并将 AI 质量指标纳入评测报告（Match Rate / Fidelity / 格式合规率 / 采纳率 `{{q}}%`）。

**关键词行**：FastAPI、pydantic、BM25 检索、human-in-the-loop、JSON Schema 约束生成、
pytest、评测基准集设计

---

## 英文版（投外企/远程岗时用）

1. Decomposed PRD-to-test-case generation into four independently verifiable skills
   (requirement mapping, test-point design, case generation, quality review), orchestrated by an
   explicit state machine with a human-in-the-loop gate before case generation; human edits are
   quantified as `+{{n}}/-{{n}}/~{{n}}`.

2. Built a 42-item golden set (20 items derived from buried rules in tables,
   footnotes and revision logs), validated by deterministic checks for source traceability,
   decidability, observability and scenario de-duplication; compared bare-prompt / RAG /
   RAG+human-in-the-loop: scenario match rate `{{x}}% → {{y}}%`, implicit-rule recall
   `{{a}}% → {{b}}%`, isolating consistent failure modes on `{{type}}` scenarios.

3. Enforced schema-constrained generation (pydantic + error-feedback retry + citation-integrity
   checks against retrieved chunks); first-pass format compliance `{{p}}%`, converging in `{{r}}`
   repair rounds; export is blocked when the review gate returns `revise`.

4. Wrote 70 offline pytest cases covering parsing, retrieval, contract repair, coverage rules,
   gating and metrics; tracked Match Rate / Fidelity / format compliance / adoption (`{{q}}%`).

---

## 推荐写法 vs 禁止写法

| ❌ 不推荐 | ✅ 推荐 | 为什么 |
|---|---|---|
| 基于 LangChain 和 DeepSeek 实现了 PRD 到测试用例的自动生成 | 把生成拆成四个可独立验证的阶段，并量化每阶段的失败模式 | 前者谁都能写，后者证明你会验证 |
| 使用 AI 提升测试效率 70% | 采纳率 {{q}}%（{{u}}/{{n}} 条可用），其余需人工改写 | 前者无出处，后者有分母 |
| 熟悉 Prompt Engineering | 用 JSON Schema 约束输出并设计 2 轮错误回喂，一次通过率 {{p}}% | 前者是形容词，后者是事实 |
| 完成了一个测试平台 | 70 个测试覆盖四阶段与闸门，导出在评审不通过时拒绝 | 可验证的工程量 |

---

## 投递时的两个提醒

1. **别把 mock 数字写进简历**。仓库里那张表当前是离线基线（2.4% 召回），
   投出去前必须用真实模型重跑，否则面试官一问"这个 2.4% 怎么这么低"就崩了。
2. **准备好讲一个"AI 说错、你纠正"的具体例子**（`hitl_diff.json` 里有原始记录）。
   面试官问的从来不是"AI 多强"，而是"你怎么发现它不行"。
