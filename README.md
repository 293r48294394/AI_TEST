# AI 测试用例生成平台

把「PRD → 测试点 → 用例 → 评审」拆成四个**可单独验证**的 Skill，串成带**人工确认闸门**的流水线，
并用一套**人工标注基准集**量化它到底哪里不可靠。

这个仓库的重点不是"AI 能生成用例"，而是"AI 生成的用例怎么被证明可用/不可用"。

```
PRD → ①需求拆解 → ⏸人工确认 → ②测试点 → ③用例 → ④质量评审 → 导出
        ↑证据引用      ↑改了什么都记录      ↑六类覆盖门槛   ↑规则+语义双层
```

**核心立场：AI 不决定用例能不能用。** 断言决定测试点是否成立，规则决定评审是否通过，
人决定最终采纳；每一步的中间产物都落盘、可回放、可审计。

---

## 一、它解决什么问题

PRD 测试的真实痛点不是"写不出用例"，而是这三个：

| 痛点 | 具体表现 | 本项目的应对 |
|---|---|---|
| **漏隐性规则** | "单用户限领 3 张，含跨活动累计，不含补偿券"写在表格脚注里，正文只写"可领取" | 检索层对脚注/编号符号加权；基准集里 42 条有 20 条来自隐性规则，单独统计召回率 |
| **幻觉用例** | 编出 PRD 里不存在的错误码、字段、状态机 | JSON 契约 + **引用完整性校验**：模型只能引用本次检索返回的块 id，编造即校验失败并重试 |
| **格式不可用** | 输出多一段解释、少一个字段、id 重复，导不进用例库 | pydantic 校验 + 错误回喂重试；记录"首次通过率 / 修复重试次数" |

外加一个流程问题：**没有任何人看过 AI 的中间产物就直接下游使用了。**
所以流水线在"需求拆解"后强制暂停等人确认，并把人工改动量（+/-/~）记进运行日志。

---

## 二、四个 Skill 的输入输出契约

每个 Skill 都是纯函数：`(client, settings, 上一步产物) -> pydantic 模型`，可单独调用、单独测试。

| Skill | 输入 | 输出 | 校验器（失败会回喂模型重试） |
|---|---|---|---|
| ① 需求拆解 `s1_testmap` | PRD 全文 + 检索块 | `TestMap`：功能 + 业务规则 + 风险 + 证据引用 | 至少 2 个功能、id 唯一、引用块必须存在 |
| ② 测试点 `s2_testpoints` | **人工确认后的** TestMap | `TestPointSet`：六类测试点 | 六类覆盖门槛（正常/异常/边界各 3，安全/兼容/性能各 1）、feature_id 必须来自测试地图 |
| ③ 用例 `s3_testcases` | 测试点（按 8 条一批） | `TestCaseSet`：前置/步骤/预期/数据 | 每个测试点恰好 1 条、步骤 ≥2、本批全覆盖 |
| ④ 评审 `s4_review` | 测试地图 + 测试点 + 用例 | `ReviewReport`：分数 + 问题 + 闸门 | 规则层（覆盖/查重/可追溯/空话）+ LLM 语义层（盲区/不可执行/幻觉） |

评审 `gate=revise` 时，**导出接口返回 409**，不允许导出正式用例文件——闸门写在代码里，不是写在流程文档里。

---

## 三、快速开始

```bash
# 1. 离线跑通（不需要 key、不需要网络）
python run.py serve --port 8000        # 打开 http://127.0.0.1:8000
python run.py run --prd data/prd/coupon-center-prd.md --auto
python run.py eval --prd coupon-center-prd --golden data/golden/coupon-golden.json --sweep

# 2. 接真实模型（含 LLM judge 版 Fidelity）
cp .env.example .env                   # 填入 LLM_API_KEY
#   ATP_LLM_MODE=deepseek
python run.py eval --prd coupon-center-prd --golden data/golden/coupon-golden.json --sweep --judge llm

# 3. 测试与前端校验
python -m pytest                       # 102 passed
node scripts/check_web.js              # 模板编译校验（防白屏）
python scripts/verify.py               # 一键验证：测试 + 前端 + 基准集 + 端到端 + 评测（约 9 秒）

# 4. 基准集质检 + 人工复核（Excel 版，表格逐行展开、结论下拉、可导回）
python run.py lint-golden --prd coupon-center-prd --golden data/golden/coupon-golden.json
python run.py review-export --golden data/golden/coupon-golden.json --out reports/golden-review.xlsx
python run.py review-import --golden data/golden/coupon-golden.json --xlsx reports/golden-review.xlsx --dry-run
python run.py doctor                   # 换机器先跑这个
```

CI 走同一套命令（`.github/workflows/ci.yml`）：pytest → 前端编译 → 基准集质检 → 端到端 → 评测，
全部在 mock 模式下跑，不需要任何 API key。

前端依赖（Vue 3 + Element Plus）已本地化到 `web/vendor/`，断网也能演示。
`scripts/check_web.js` 用 Vue 编译器在 Node 里编译模板，把"页面白屏但 HTTP 200"这类问题变成一条可执行检查。

---

## 四、当前实测结果

> **重要**：下面这张表是**离线启发式基线**（`ATP_LLM_MODE=mock`，不调用任何模型）跑出来的。
> 它的作用是证明"链路 + 指标 + 闸门"都能跑通，**不代表模型能力**。
> 真实模型的三模式对比需要你的 key，跑完把 `reports/eval-*.md` 的数字替换到这里。

| 模式 | 测试点 | 用例 | 场景召回 | 隐性规则召回 | 评审分 | 闸门 |
|---|---|---|---|---|---|---|
| bare | 28 | 28 | 2.4% | 0.0% (0/20) | 81 | pass |
| rag | 20 | 20 | 2.4% | 0.0% (0/20) | 89 | pass |
| rag_hitl | 20 | 20 | 2.4% | 0.0% (0/20) | 89 | pass |

这组数字本身就是一个结论：**规则基线产出的用例"看起来完整"（20 条、评审 89 分 pass），
但对人工基准几乎零召回。** 评审分高 ≠ 有用——这正是为什么评测要独立于评审分做，
也是这个项目最值得在面试里讲的一点。
两次运行里评审分在 81 / 89 之间波动，只取决于生成条数与模板重复度，与召回毫无关系。

### 跨文档泛化（第二份 PRD：账号登录）

同一套代码换一个业务域跑，验证管道没有绑定"优惠券"：

| 文档 | 域 | 功能数 | 测试点 | 场景召回 | 隐性召回 | 评审 | 闸门 |
|---|---|---|---|---|---|---|---|
| coupon-center | 营销 | 6 | 20 | 2.4% | 0/20 | 89 | pass |
| account-login | 账号安全 | 4 | 20 | 10.0% | 1/8 | 65 | revise |

**这条验证的是"管道不依赖领域"，不是"模型在两个域的水平"**——用的是规则基线，数字差异没有质量含义。

它抓到两个真实过拟合，都已修：
1. 离线基线的功能识别原来是关键词白名单（领取/创建/支付…），在账号域只识别出 2 个功能
   （"密码登录""会话与登出"全部漏掉）。改成"章节标题 + 目录类排除"策略后才恢复到 4 个。
2. `s2` 提示词里举了"券已过期"的例子——示例即暗示。改成"资源已过期"等域中立表述，
   并加了 `test_prompts_are_domain_neutral` 防止以后再写回去。

> 没有这个第二份文档，上面两个问题都不会暴露。这也是"拿一份文档调通就宣称通用"不可信的原因。

### 批量与成本（`batch` / `cost`）

单份跑通不等于能上量。`python run.py batch --prds a,b --mode rag --workers 2`：

| 项 | 实测（mock，2 份文档） |
|---|---|
| 成功 / 失败 | 2 / 0 |
| 总耗时 | 0.1s |
| 平均 | 32ms/份 |
| 吞吐 | 31.25 份/秒 |

> 这组数字只反映**管道开销**（mock 不调模型），不能当性能结论。
> 它证明的是：并发跑多份不会互相覆盖产物（run_id 唯一）、单份失败不拖垮整批。

`python run.py cost --run <id>` 把 `llm_calls.jsonl` 折算成 token：

| 运行 | LLM 调用 | 输入字符 | 输出字符 | 估算输入 token | 估算输出 token |
|---|---|---|---|---|---|
| 20261118-158bd0 | 6 | 32690 | 15749 | 25681 | 12372 |

**刻意不内置单价**——价目会变，写死一个数字就是造数据。传入 `--price-in/--price-out`
（元/百万 token）才会算金额，并同时给出"单条用例成本"，面试问"贵不贵"时能直接答。

### 基准集质检（`python run.py lint-golden`）

基准集如果没被机器检查过，"人工标注"就只是一句话。这里用确定性规则查四类脏数据：

| 检查项 | 说明 |
|---|---|
| 来源可定位 | `source` 里的章节号 / 表号 / 脚注符号 / Q 编号必须能在 PRD 里真实找到 |
| 标题可判定 | 必须有具体条件或预期，能测出 pass/fail（"优惠券功能正常"直接判 error） |
| 期望可观测 | 必须含状态码/字段/提示/埋点/数值之一 |
| 场景去重 | 标题相似度 ≥0.92 判重复（重复计入会虚高召回） |
| 枚举与编号 | 六类分类、P0–P2、`G-001` 连续编号 |

当前状态：**已人工复核**（屈永康，2026-11-18，v1.0）：39 条通过 / 3 条修正，机器质检 **0 error / 0 warn**。
修正的三条都是同一类问题——**AI 写出了 PRD 没有的 UI 断言**（"券置灰""提示差 0.01 元可用"），
外加一条分类错误（全局兑换上限是配额边界，不是安全场景）。

分布：12 正常 / 11 异常 / 12 边界 / 3 安全 / 2 兼容 / 2 性能，20 条隐性规则，覆盖 8 个功能。

> 复核证据：`docs/examples/golden-review-applied.diff`（每一条改动的原文对比）。
> 质检器自身有 14 个测试，用脏数据逐条验证规则真的会触发——不然它只是装饰。

人工复核表单（把 PRD 原文摘录并排放在每条基准点旁，复核只需判断对/错）：

```bash
python run.py lint-golden --prd coupon-center-prd --golden data/golden/coupon-golden.json --sheet reports/golden-review-sheet.md
```

另一个细节值得注意：`bare`（无检索）生成了更多测试点（34 > 26），
但因为没约束住"六类覆盖 / 功能零覆盖"，评审反而掉到 65 分、被闸门拦下。
"产得多"和"产得对"是两件事，评审层的价值就在这。

指标定义：
- **Match Rate**：人工基准点被生成测试点命中的比例（一对一贪心、阈值 0.55），召回视角
- **Fidelity Rate**：命中对里关键约束（数值/状态）被正确带入的比例，准确视角
- **格式合规率**：一次通过 JSON 契约校验的调用单元 / 总单元
- **采纳率**：人工逐条裁决「可用/需修改/不可用」后 `可用 / 总数`，**未实测就不填**

---

## 五、为什么不用 LangGraph / ChromaDB

这两个是需求里点名的技术，最后没用，理由要能当面讲清楚：

**不用 LangGraph**：这条流水线只有 5 个节点、1 个人工闸门、1 个循环点。
显式状态机 200 行，每步可断点、可单测、`events.jsonl` 里有耗时与重试次数；
图框架会把它变成黑盒，而这个项目要证明的恰恰是"每一步都被验证过"。
真要换，Skill 层是纯函数，把 `run_stage` 换成 node 即可，不影响契约与评测。

**不用 ChromaDB / embedding**：需求文档是"关键词密集、术语精确"的语料（限额、幂等、脚注编号），
BM25 + 符号加权在这个场景召回不输小模型 embedding，而且**结果可解释**——每条召回都能指回原文块。
更关键的是**可复现**：换一版 embedding 模型就换一套评测结论，那不叫评测，叫抽签。

如果面试官追问"你就不能上向量库吗"：可以，`Retriever` 是 Protocol，
实现 `retrieve()` 就能替换，评测脚本不用改。当前选择是**权衡后的取舍**，不是不会。

---

## 六、环境约束与工程取舍（真实发生过的问题）

本机沙箱里 `pip install` 在解包阶段被权限策略拦截（`WinError 5`），`tempfile.mkdtemp`
建立的目录也无法写入。这些限制直接塑造了架构：

1. **核心零第三方依赖**：LLM HTTP 用 `urllib`、docx 用 `zipfile + XML`、检索用 BM25、
   去重用 `difflib`。只有 fastapi/uvicorn/pydantic/openpyxl 是必需的。
2. **bootstrap.py**：装不上就用"下载 wheel + 解压到 `.deps`"兜底（已用 pypdf 验证可用）。
3. **测试不用 `tmp_path`**：改用工作区自建目录，避免踩 `mkdtemp` 的 0o700 ACL 限制。

代价是少用了一些现成轮子；收益是**换台机器照样跑**，且每个依赖都是自己选的，不是继承来的。

---

## 七、目录结构

```
atp/
  config.py            环境配置（含 20 行 .env 解析器）
  models.py            四阶段输入输出契约（pydantic）
  store.py             运行产物持久化（state/artifacts/events/llm_calls）
  llm/
    base.py            LLMClient 协议、对话累积
    deepseek.py        OpenAI 兼容客户端（urllib + 重试 + 审计）
    mock.py            离线启发式基线（同时是评测下限参照）
    schema.py          JSON 契约：解析→校验→业务校验→回喂重试
  parsers/prd.py       md/txt/docx 解析 + 语义切块（带 chunk_id）
  retrieval/bm25.py    BM25 + 符号加权 + 多查询 RRF 融合
  skills/              四个 Skill（s1~s4）
  coverage/rules.py    确定性质量规则（覆盖/查重/可追溯/空话）
  pipeline/orchestrator.py 状态机 + HITL 闸门 + 人工改动 diff
  export/xlsx.py       Excel 导出（含闸门拦截）
  eval/                指标（含阈值敏感性）、LLM judge、基准集质检、三模式执行器、报告渲染
app/api.py             FastAPI（提测/运行/确认/裁决/导出）
web/index.html         单文件 Vue3 控制台（依赖本地化）
scripts/check_web.js   前端模板编译校验（Node + Vue compiler）
data/prd/              coupon-center-prd.md（营销，2478 字，10 处隐性规则）
                       account-login-prd.md（账号安全，用于跨文档泛化验证）
data/golden/           coupon-golden.json（42 条主基准，20 条隐性）
                       account-login-golden.json（20 条泛化探针，不当模型能力用）
tests/                 102 个测试
reports/               评测报告 + 基准集复核表单
docs/                  架构 / 实验计划 / 面试问答 / 简历叙事 / 复核指南 / **声称-证据对照表** / 交接说明
docs/examples/         真实运行的产物示例（人工改动 diff、评审报告）
```

---

## 八、已知限制

1. **只验了两份中文互联网 PRD**（营销 + 账号安全），跨行业、英文文档、超长（>50 页）文档未验证；
   跨域验证证明的是"管道不依赖领域"，不是"任意 PRD 都能用"。
2. **基准集只有一位复核人**：42 条已由一人逐条复核（39 通过 / 3 修正），但**没有交叉复核**，
   标注者间一致性（Cohen's κ）未测。一人复核能消除"AI 编造 UI 行为"这类错，消不掉个人理解偏差。
3. **Fidelity 双路径**：默认启发式代理（只看关键数字是否带入）；`--judge llm` 走 1–5 分语义评判，
   但 **judge 本身还没做过与人工评分的致率抽检**（`agreement_with_human()` 已实现，缺人工样本）。
4. **阈值敏感性已可跑但未解读**：`--sweep` 会输出 0.40–0.80 的曲线；当前基线在 0.80 处才断崖，
   说明基线匹配本来就脆，真实模型下的曲线才有解释价值。
5. **人工确认在评测里是"录制回放"**：`rag_hitl` 模式用的是落盘的人工修改版测试地图，
   保证可复现，但不是本次实时人审——报告里必须写明。
6. **并发只测了管道开销**：mock 下 2 份并发 0.1s，但真实模型的延迟/限流/重试未测，
   批量跑 100 份的稳定性未知；同步阻塞的客户端在真实 API 下可能不是这个数量级。

---

## 九、接下来三周（详见 `docs/experiment-plan.md`）

| 周 | 目标 | 产出 |
|---|---|---|
| W1 | 接真实模型跑通，替换 README 数字 | 真实三模式报告 + 首次失败分析 |
| W2 | 基准集人工复核 + LLM judge 致率抽检 + 逐条裁决 | 基准集可称"人工标注"、Fidelity 可信、采纳率有数 |
| W3 | 包装：README 数字、3 分钟演示、简历 bullet、面试 FAQ | 投递材料 |
