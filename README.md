# 素材审核与发布排期 Agent

小影校招 AI Agent 开发岗 · 题1

给定一批待发布素材与已有排期表，输出每条素材的**审核状态、风险原因、建议时段、待确认项、草稿保存状态**。
系统**只保存草稿，不接真实发布**——代码里不存在把草稿变成"已发布"的路径。

---

## 快速开始

无需任何依赖与环境变量（只用 Python 标准库）：

```bash
python -m src.cli --case conflict --offline      # 时段冲突：18:00 → 19:00，标记待确认
python -m src.cli --case conflict --offline --repeat   # 同一请求重复提交，验证只产生一份草稿
```

运行测试与重新生成证据：

```bash
python -m unittest discover -s tests -t . -v     # 324 项测试
python scripts/run_examples.py                   # 重新生成 examples/ 并自动核验
```

---

## 验收点对照

| # | 验收点 | 命令 | 产出证据 |
|---|---|---|---|
| 1 | 正常排期 | `--case normal` | `examples/run_normal.txt` |
| 2 | 素材信息缺失 | `--case missing_info` | `examples/run_missing_info.txt` |
| 2b | 补查有来源（对照组） | `--case missing_info_resolved` | `examples/run_missing_info_resolved.txt` |
| 3 | 规则风险 | `--case rule_risk` | `examples/run_rule_risk.txt` |
| 4 | 时段冲突 | `--case conflict` | `examples/run_conflict.txt` |
| 5 | 重复提交 | `--case conflict --repeat` | 同上「重复提交校验」段 |
| 6 | 保存失败 | `--case save_failure` | `examples/run_save_failure.txt` |
| + | Prompt 注入 | `--case injection` | `examples/run_injection.txt` |
| + | 全量批处理 | `--case full` | `examples/run_full.txt` |

逐条自动核验结果见 `examples/verification.txt`（29 项，0 失败）。

---

## 一次运行会看到什么

```
run_id       : run-conflict
case         : conflict
model        : offline (degraded=false)
status       : completed
budget       : steps 6/80, tools 5/60

asset review      status      draft_id  requested           suggested           confirm
--------------------------------------------------------------------------------------------
M01   passed      草稿已保存       D-001     2026-11-05T18:00    2026-11-05T19:00    待确认

  待确认   M01: 2026-11-05T18:00 已被占用，建议调整为 2026-11-05T19:00

summary      : total=1 已保存草稿=1 阻断=0 待补资料=1 保存失败=0 待确认=1
```

要点：`requested`（用户原始要求）与 `suggested`（建议时段）是两个字段，永不合并；
改期必须出现在「待确认」里；状态词只有「草稿已保存 / 保存失败 / 已阻断 / 待补资料」。

常用参数：

| 参数 | 作用 |
|---|---|
| `--case <name>` | 选择用例，见 `--list-cases` |
| `--offline` | 强制使用离线 Mock 规划器 |
| `--repeat` | 重复提交同一请求，验证幂等 |
| `--trace <path>` | 把决策轨迹写成 JSONL（每次运行覆盖） |
| `--audit <path>` | 把工具调用审计追加为 JSONL（跨运行累积） |
| `--json` | 输出机器可读结果 |
| `--data-dir <path>` | 指定数据目录 |

退出码：`0` 正常、`1` 用例/数据错误、`2` 诚实层拦截、`3` 幂等校验不一致。

---

## 项目结构

```
src/
  cli.py            命令行入口（唯一入口）
  runtime.py        ReAct 闭环 + 四重预算 + 逐素材结算
  guardrail.py      动作白名单矩阵 + 硬约束断言
  tools.py          五个工具与统一返回信封
  planner.py        离线 Mock 规划器 + 动作结构校验
  llm.py            真实模型适配器（严格 JSON、失败降级）
  honesty.py        输出诚实层（禁词即拒绝输出）
  observability.py  TraceWriter（覆盖写）与 AuditLog（追加写）
  repository.py     JSON 原子写 + schema 校验
  services/         素材 / 规则 / 时段 / 草稿四个领域服务
data/               合成素材、规则、排期、故障注入配置
tests/              18 个文件、324 个用例
examples/           运行记录、决策轨迹、审计、自动核验结果
scripts/            一站式证据生成脚本
docs/               需求、架构、评估、AI 使用、回顾、任务拆分、复查记录
```

---

## 设计要点

**模型只有提议权，代码握有执行权。** `BLOCKED` 状态的允许动作集合里没有 `save_draft`，
所以越权在结构上执行不了，而不是"提示词不允许"。

**一个素材永远只有一份草稿。** 以 `asset_id` 为唯一索引（不是请求哈希——那样改期后会多出一份），
写入前刷新签名并用跨平台文件锁包住读-改-写。

**失败必须看得见。** 任何保存失败都记 `failed`，不因错误码不同而显示成"未处理"；
输出层命中"已发布"等禁词直接报错退出，不做静默替换。

**有界执行。** 全局 80 步 / 单素材 8 步 / 60 次工具调用 / 120 秒，任一触达返回
`status=incomplete` 并列出剩余待办。

详细设计见 `docs/02-solution.md`。

---

## 已知边界（重要）

**`examples/` 下的全部记录来自离线 Mock 规划器，不代表真实模型的决策质量。**

Mock 的每一步都读取上一步的工具返回再决策（例如看到 18:00 被占用才改选 19:00），
因此轨迹结构与在线一致；但它内部是规则分支，**不构成"模型会调整动作"的证明**。

真实模型路径已实现（`src/llm.py`），配置后即可生成真实轨迹：

```bash
pip install openai
export OPENAI_API_KEY=sk-...
python -m src.cli --case conflict --trace examples/run_conflict.jsonl
```

本环境无外网、无 API Key，**未能取得真实模型轨迹**，这一点在
`docs/03-evaluation.md` 第 4 节与 `docs/04-ai-usage.md` 第 5 节同样标注。

其它边界：跨素材的时段唯一性不在草稿层校验（属调度层职责）；单进程串行，
不为并发吞吐优化。

---

## 文档索引

| 文档 | 内容 |
|---|---|
| `docs/01-requirements.md` | 用户、目标、范围、成功标准 |
| `docs/02-solution.md` | 分层架构、状态机矩阵、错误码、降级与并发策略 |
| `docs/03-evaluation.md` | 六个验收点的预期 vs 实际、失败与限制 |
| `docs/04-ai-usage.md` | AI 使用范围、关键取舍、验证方式 |
| `docs/05-retrospective.md` | 最难的问题、取舍复盘、未完成与下一步 |
| `docs/06-task-plan.md` | T0–T17 任务拆分、依赖与时间预算 |
| `docs/07-review.md` | 一轮对照题意的复查与修正记录 |
| `examples/README.md` | 证据文件说明与观察点 |

---

## 提交信息

- 题号：**1（素材审核与发布排期 Agent）**
- 仓库：https://github.com/dachengjinresearch-crypto/xiaoying-agent-exam
- commit SHA：068347f7e7cf4b6ce7e8be8452fa4fa29d217af4

> 私有仓库请将评审账号加入可读名单。
