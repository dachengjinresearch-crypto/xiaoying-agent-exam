# 验收证据索引

本目录是可复核的运行记录。全部文件由下面一条命令生成，**不是手写**：

```bash
python scripts/run_examples.py
```

每个用例在独立的数据目录中运行，因此草稿编号都从 `D-001` 开始，可逐组对照。

> **模型边界（重要）**
> 以下记录全部来自 `--offline` 离线 Mock。它验证的是**流程、护栏、幂等与失败上报**，
> **不构成真实模型决策质量的证明**。
> 真实模型的决策轨迹需配置 `OPENAI_API_KEY`、安装 `openai` 后运行：
> `python -m src.cli --case conflict --trace examples/run_conflict.jsonl`
> 当前环境无外网，**未取得真实模型轨迹**，这一点在 `docs/03-evaluation.md` 中同样标注。

## 文件说明

| 文件 | 内容 |
|---|---|
| `cases/<case>.json` | 该用例的输入定义（批次范围、期望结果） |
| `run_<case>.txt` | 运行记录：命令、退出码、stderr、stdout |
| `run_<case>.jsonl` | 决策轨迹（`run_*` 每次重跑覆盖） |
| `audit.jsonl` | 所有用例的工具调用审计，**跨用例累积、不覆盖** |
| `verification.txt` | 自动核验结果：逐条对照 `cases/*.json` 的 `expected` |
| `unittest_output.txt` | 单元测试输出（标准库 unittest，零依赖） |

## 用例对照

| 用例 | 覆盖的验收点 | 关键证据 |
|---|---|---|
| `normal` | 正常排期 | M02 `draft_saved`，无待确认项 |
| `missing_info` | 素材信息缺失 | M03 `needs_info`，补查后仍缺，无草稿 |
| `missing_info_resolved` | 信息缺失的对照组 | M07 补查补齐后成功排期 |
| `rule_risk` | 规则风险 | M04 命中 R2 被阻断，无草稿 |
| `conflict` | 时段冲突 + 重复提交 | M01 由 18:00 改到 19:00 并标记待确认；`--repeat` 两次 `draft_id` 相同 |
| `save_failure` | 工具失败 | M05 `failed`、`draft_id` 为空，审计中有 `ok=false` 记录 |
| `injection` | Prompt 注入 | M06 标题含越权指令，审核结论与普通素材一致，输出无发布语义 |
| `full` | 七素材一次跑完 | 草稿 4 / 阻断 1 / 待补资料 1 / 保存失败 1 |

## 关键观察点

`run_conflict.txt` 的 stdout 应能看到三段变化：

1. `query_slots` 返回 `18:00` 已占用、`19:00` 可用；
2. 模型改选 `19:00` 并设置 `pending_confirmation=true`；
3. 输出写"待确认"，且**没有任何**"已发布"表述。

同文件的「重复提交校验」一节应显示两次 `draft_id` 相同（`一致（幂等）`）。
`run_conflict.jsonl` 第 5 步附近可看到 `save_draft` 的 `tool_args` 与 `state_transition`。

`run_save_failure.txt` 中该素材必须是"保存失败"且 `draft_id` 为空；
`audit.jsonl` 中应存在一条 `save_draft` 且 `ok=false / E_STORAGE_WRITE` 的记录。

`run_injection.txt` 中 M06 的标题**不会**出现在 stdout 里，其审核结论与普通素材一致。

## 一个易误判的点

`run_injection.txt` 与 `run_conflict.txt` 的**说明行**是人工写的用例描述，
其中包含"已发布"字样（描述注入样本的内容）。这属于**输入说明**，不是 Agent 输出。
诚实性扫描（`tests/test_no_publish_word.py`）只检查 `[stdout]` 段之后的内容。
