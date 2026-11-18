# 示例产物（来自真实运行，非手工编造）

这两个文件是从一次真实运行的 `data/runs/<run_id>/artifacts/` 里复制出来的，用来让人不用跑代码
也能看到输出长什么样。

| 文件 | 来源 | 说明 |
|---|---|---|
| `hitl-diff-example.json` | 运行 `20261118-a829f1`（`rag_hitl` 模式，mock 基线） | 人工确认时改了测试地图：新增 1 个功能（F-99 券预算控制）、在 F-01 里补了 1 条规则。这就是"人工确认"留下的可审计痕迹 |
| `review-example.json` | 同一运行的 `05_review.json` | 评审报告：分数、闸门、逐条 issue |

注意：`data/runs/` 在 `.gitignore` 里（体积大、含完整 PRD 内容），所以真实运行记录不进仓库，
只在这里保留两个脱敏示例。想自己生成同样的产物：

```bash
python run.py run --prd data/prd/coupon-center-prd.md      # 停在人工确认闸门
# 在控制台改测试地图后提交，产物落在 data/runs/<run_id>/artifacts/
```

`hitl_diff` 的字段含义：`added` 人工新增的功能 id，`removed` 删掉的，`edited` 改了哪些字段
（`rules_added` / `rules_removed` 给出具体文本）。`summary` 是 `+新增 / -删除 / ~修改`。
