# 题1「素材审核与发布排期 Agent」执行方案

> 定位：3 小时限时考核的执行蓝图。读完这一份就能开工，不需要再回看原题。
> 核心判断：这道题真正考的是**状态诚实性**与**模型决策真实性**，不是功能完整度。

---

## 0. 一句话战略

先把 6 个验收点全部打通（保底），再用「幂等防重复」拿高分项（加分），
全程保证：**任何输出都不出现"已发布"**，**任何失败都如实返回 failed**。

题目把这题的关键缺陷写得很明确：越权发布、虚报保存成功。
这两条踩中任一条，功能做得再全也是 0 分。所以整份方案的每个设计决策，
都优先服务"不虚报"这一条。

---

## 1. 验收点对照表（这就是你的 roadmap，也直接放进 README 第一屏）

| # | 验收点 | 计划实现 | 验证方式 | 产出文件 |
|---|---|---|---|---|
| 1 | 正常排期 | M02 无风险，用户要求 18:00，时段可用 → 直接排期并保存 | `python -m src.cli --case normal` | `examples/run_normal.{jsonl,txt}` |
| 2 | 素材信息缺失 | M03 元数据缺封面/正文 → 补查一次 → 仍缺 → 阻断并列出缺口 | `--case missing_info` | `examples/run_missing_info.*` |
| 3 | 规则风险 | M04 命中规则 R2（未核实效果数据）→ 阻断，不排期 | `--case rule_risk` | `examples/run_rule_risk.*` |
| 4 | 时段冲突 | M01 无风险，要求 18:00 但已占用，19:00 可用 → 改期 19:00 + 标记待确认 | `--case conflict` | `examples/run_conflict.*` |
| 5 | 重复提交 | 再次提交与 #4 相同请求 → 返回同一 draft_id，`idempotent_replay=true` | `--case conflict --repeat` | `examples/run_repeat.*` |
| 6 | 保存失败 | M05 保存时注入存储故障 → 明确返回 `failed`，不写成功 | `--case save_failure` | `examples/run_save_failure.*` |

> 6 个验收点用 5 条执行路径覆盖（#4 和 #5 共用一条路径的两次运行）。
> 满足题目"至少 3 组输入"的要求，且每组都同时覆盖多题共通的多条验收点。

**额外要求（题目硬性）**：至少两条路径的运行记录要能看出"模型因不同的工具返回而改变动作"。
本方案中 #4（冲突后改期）和 #3（规则阻断后停止）是最清晰的一对：
前者观察到"时段已占用"后换时段，后者观察到"规则 blocked"后直接终止。

---

## 2. 一票否决清单（写代码前贴在显示器上）

- [ ] 任何输出/文档/注释不出现 `已发布`、`published`、`发布成功`、`上线`
- [ ] 状态词只用三个：`draft_saved` / `pending_confirmation` / `failed`（外加 `blocked`、`needs_info`、`incomplete`）
- [ ] 保存失败不得被 `try/except` 吞掉，必须原样上报 `failed` + `error_code`
- [ ] 未获用户确认的改期，必须带 `pending_confirmation: true`，不得当作已定稿
- [ ] 不得擅自修改用户给的硬性发布时间要求（只可"建议 + 待确认"，不可"改为"）

---

## 3. 业务数据设计（data/）

### 3.1 素材（data/assets.json）

```json
{
  "asset_id": "M01",
  "title": "秋季新品开箱短视频",
  "channel": "douyin",
  "requested_publish_at": "2026-11-05T18:00",
  "hard_requirement": true,
  "metadata": {
    "has_cover": true,
    "has_body": true,
    "has_authorization": true,
    "duration_sec": 45
  },
  "content_flags": ["contains_product_claim"]
}
```

- `hard_requirement` 决定"是否可改期"：为 `true` 时改期必须走"待确认"，为 `false` 时可直接建议新时段。
  **但无论哪种，都不能未经确认就当成最终排期。**
- M03 故意把 `has_cover` / `has_body` 置为 `false`，用来造"信息缺失"。

### 3.2 规则（data/rules.json）

```json
{
  "version": "2026-10-01",
  "rules": [
    {"rule_id": "R1", "desc": "素材必须四要素齐全", "level": "block"},
    {"rule_id": "R2", "desc": "不得包含未核实的效果数据（如提升80%）", "level": "block"},
    {"rule_id": "R3", "desc": "同一渠道 30 分钟内不得连发两条", "level": "warn"},
    {"rule_id": "R4", "desc": "发布时间须落在 09:00-22:00", "level": "block"}
  ]
}
```

`level` 决定 Agent 的动作：`block` → 终止该素材；`warn` → 继续但写入待确认项。
**规则版本号要进幂等键**，规则变了草稿应当失效重算。

### 3.3 排期（data/schedule.json）

```json
{
  "douyin": {
    "2026-11-05": {
      "18:00": {"asset_id": "M00", "status": "occupied"},
      "19:00": {"asset_id": null, "status": "free"},
      "20:00": {"asset_id": null, "status": "free"}
    }
  }
}
```

### 3.4 故障注入（data/failure_config.json）

```json
{"save_draft_fail_for": ["M05"]}
```

用配置文件而不是环境变量，评审复现更简单。**这个文件的存在本身就要在 README 里写明**，
否则评审不知道"保存失败"怎么演示。

---

## 4. 工具设计（src/tools.py）

五个工具，全部是本地函数，签名固定（评审会看 trace 里的参数）：

| 工具 | 入参 | 出参 | 关键行为 |
|---|---|---|---|
| `list_pending_assets` | `channel?` | `[{asset_id,title,requested_publish_at}]` | 只返回待处理素材 |
| `get_asset_detail` | `asset_id` | `{metadata, content_flags}` 或 `{status:"not_found"}` | 缺字段时如实标 `missing_fields` |
| `check_rules` | `asset_id` | `{risk_level, hits:[{rule_id,level,reason}]}` | 返回命中的全部规则，不只第一条 |
| `query_slots` | `channel, date` | `{occupied:[...], available:[...]}` | 按时间升序返回可用时段 |
| `save_draft` | `asset_id, slot, notes, idempotency_key` | `{status, draft_id, error_code?}` | 幂等 + 可注入失败 |

**工具返回必须带 `status` 字段**，让模型能观察到失败（这是 ReAct 的前提）。
如果失败时抛异常，模型就只能"看到崩溃"而不是"观察到结果并调整"。

---

## 5. 状态机（每个素材独立推进）

```
PENDING
  ├─ get_asset_detail → missing_fields 非空 → (补查 1 次) → 仍缺 → NEEDS_INFO (阻断)
  ├─ check_rules → 命中 block → BLOCKED (终止该素材)
  ├─ check_rules → 命中 warn → 继续，warn 进待确认项
  └─ 时段检查
        ├─ 要求时段可用 → save_draft → DRAFT_SAVED
        └─ 要求时段已占用 → query_slots 找替代 → 建议 + 标记 PENDING_CONFIRMATION → save_draft
                                                                              ├─ 成功 → DRAFT_SAVED_WITH_CONFIRM
                                                                              └─ 失败 → SAVE_FAILED

全局：step > MAX_STEPS 或 tool_calls > MAX_TOOL_CALLS → INCOMPLETE（列出剩余待办）
```

**上限策略（题目硬性）**：全局 `MAX_STEPS = 80`、`MAX_TOOL_CALLS = 60`，
外加单素材 `MAX_STEPS_PER_ASSET = 8`。实测全量 7 个素材约 32 步 / 25 次工具调用，
单一 12 步的全局上限会在第 3 个素材就截断。
到达上限时**不是抛异常**，而是返回结构化未完成：

```json
{
  "status": "incomplete",
  "reason": "max_steps_reached",
  "completed": ["M01 draft_saved", "M02 draft_saved"],
  "remaining": ["M03: 尚未检查规则", "M04: 尚未检查规则"]
}
```

---

## 6. 幂等设计（高分项，也是防重复验收点的唯一正解）

**不要用请求 UUID 做幂等键**——那样重复提交还是两份草稿，直接踩中扣分点。

推荐方案：**以 `asset_id` 为主键，一个素材永远只有一份草稿**。

```
idempotency_key = sha256(f"{asset_id}|{rules_version}")[:16]
```

保存逻辑：

```
if key exists in drafts table:
    if slot 与已有草稿一致:
        return {status:"draft_saved", draft_id: existing, idempotent_replay: true}
    else:
        update slot, 返回同一 draft_id, idempotent_replay: true, updated: true
else:
    create new draft, draft_id = f"D-{counter:03d}"
```

这样做的两个好处：一是"相同请求重复保存只产生一份草稿"必然成立；
二是评审"更换占用时段并重复提交"时，也只得到同一份草稿（更新而非新增），
不会出现两条记录导致看起来很乱。

**取舍说明**（写进 docs/02）：若业务要求"同一素材可有多版本草稿"，
则应改用 `asset_id + slot` 复合键。本题选择单键，因为题意强调的是"一份草稿"。

---

## 7. ReAct 循环（决策真实性的证明）

```
while step < MAX_STEPS:
    observation = 最近一次工具结果（首轮为初始上下文）
    decision = model.decide(observation, state)      # 真模型；离线时用 mock 决策器
    trace.log(decision)                               # 记录 决策依据，不含思维链
    if decision.action == "finish": break
    result = dispatch(decision.action, decision.args) # 执行工具
    trace.log(result)                                 # 记录 返回摘要
```

Trace 每行（JSONL）字段固定：

```json
{
  "run_id": "run-20261029-001",
  "step": 3,
  "event": "model_decision",
  "asset_id": "M01",
  "action": "query_slots",
  "tool_args": {"channel": "douyin", "date": "2026-11-05"},
  "reason": "check_rules 返回 no_risk，但用户要求 18:00；需先确认该时段是否可用"
}
```

```json
{
  "run_id": "run-20261029-001",
  "step": 4,
  "event": "tool_result",
  "asset_id": "M01",
  "tool": "query_slots",
  "result_summary": "occupied=[18:00], available=[19:00, 20:00]",
  "next_action": "save_draft",
  "reason": "18:00 被 M00 占用，19:00 可用；因原定 18:00 为硬性要求，改选 19:00 后需标记待确认"
}
```

**`reason` 的写法是关键**：写"因某工具返回 X，所以选 Y"，不写"我认为…"。
题目明确说不须提交内部思维过程，写了反而可能被判为无效决策日志。

**决策真实性的自证方法**：让 `model.decide` 的入参只包含 observation 和 state，
不预埋"下一步该干什么"。如果评审把 `data/schedule.json` 里 18:00 改成 free，
Agent 应当自动不去调用 `query_slots`——这就是决策随工具结果变化的证据。

---

## 8. 目录结构（照抄即可）

```
.
├── README.md                  # 第一屏放第 1 节的验收对照表 + 运行命令
├── requirements.txt
├── .env.example               # OPENAI_API_KEY=  ; 并注明"不配置也能离线跑"
├── src/
│   ├── __init__.py
│   ├── cli.py                 # 唯一入口，argparse，--case / --offline / --repeat
│   ├── agent.py               # ReAct 循环 + 步数上限
│   ├── tools.py               # 五个工具 + 故障注入
│   ├── store.py               # 草稿表 + 幂等逻辑（可换 JSON 文件持久化）
│   ├── trace.py               # JSONL 落盘
│   └── llm.py                 # 真模型 adapter + OfflineMockLLM
├── data/
│   ├── assets.json
│   ├── rules.json
│   ├── schedule.json
│   └── failure_config.json
├── tests/
│   ├── test_idempotency.py
│   ├── test_save_failure.py
│   ├── test_max_steps.py
│   └── test_no_publish_word.py   # 断言所有输出不含"已发布"
├── examples/
│   ├── run_normal.jsonl / .txt
│   ├── run_missing_info.jsonl / .txt
│   ├── run_rule_risk.jsonl / .txt
│   ├── run_conflict.jsonl / .txt
│   ├── run_repeat.jsonl / .txt
│   ├── run_save_failure.jsonl / .txt
│   └── unittest_output.txt
└── docs/
    ├── 01-requirements.md
    ├── 02-solution.md
    ├── 03-evaluation.md        # 必须逐条引用 examples/ 下的文件名
    ├── 04-ai-usage.md
    └── 05-retrospective.md
```

---

## 9. 三小时时间表（每 30 分钟提交一次）

| 时间 | 任务 | 完成标志 |
|---|---|---|
| 0–10 | 定题、把第 1 节表格抄成 checklist 放进 README 骨架 | README 有 6 行待勾选项 |
| 10–25 | 设计定死：工具签名、状态机、trace schema、上限、幂等键 | 第 3–7 节内容落到 docs/02 草稿 |
| 25–60 | **无模型骨架先跑通**：OfflineMockLLM 返回硬编码动作，CLI 跑完 normal 用例，trace 已落盘 | `--case normal --offline` 有输出 |
| 60–105 | 接真实模型，按 checklist 逐条打通 6 个验收点 | 6 条 `--case` 全部跑通 |
| 105–130 | 5 条路径 trace 自动落盘、修 bug、补 `--repeat` | examples/ 下 6 组文件齐全 |
| 130–150 | 写测试并跑通（标准库 unittest，零依赖） | `python -m unittest` 全绿，输出存 examples/ |
| 150–170 | 写 docs/01–05，03 逐条引用 examples 文件名 | 五份文档齐全 |
| 170–180 | README 收尾、脱敏、最终 commit、填 commit SHA | 仓库可公开访问 |

**关键顺序**：25–60 一定要先跑无模型骨架。先接模型的话，
一旦 API 报错或模型行为不稳定，你会在 90 分钟时还没跑通一条路径。

---

## 10. 风险预案（时间超了怎么办）

| 风险 | 预案 |
|---|---|
| 90 分钟时还没跑通 6 条 | 砍掉"规则 warn → 待确认项"分支，只保留 block；砍界面/多余日志 |
| 模型 API 不可用/超时 | 全程用 `--offline`，docs 里如实标注"Mock 只验证流程，不证明真实模型决策效果" |
| 时间不够写 5 份 docs | 01/02/03 必写（评审看的），04/05 各写半页 |
| 幂等实现出 bug | 退化为内存字典 + 进程内幂等，仍能通过单次运行的重复提交测试 |
| 仍做不完 | 在 docs/05 如实写"未完成：X，原因：Y"，**不要虚报**，这是题目鼓励的做法 |

---

## 11. 提交前 10 项自检

- [ ] `python -m src.cli --case normal --offline` 在干净环境能跑通（不需要 key）
- [ ] README 第一屏是 6 行验收对照表，每行能指到命令和产出文件
- [ ] examples/ 下 6 组 trace 是程序产出的，步数和时间戳连续可核对
- [ ] 抽样两条路径，决策 `reason` 能看出因不同工具结果而改动作
- [ ] `save_draft` 失败时返回 `failed`，不是异常、不是 ok
- [ ] 重复提交返回同一 `draft_id` 且有 `idempotent_replay: true`
- [ ] 步数上限 12，到顶返回结构化 incomplete
- [ ] 全仓 `grep -r "已发布\|published\|发布成功"` 结果为空
- [ ] .env.example 存在且注明"无配置也可离线运行"
- [ ] 无真实密钥、无隐私数据；私有仓库已加评审账号

---

## 12. 两个必须写进 docs/02 的技术取舍说明

评审不看你用了什么框架，只看你**能不能解释为什么这么选**。

第一，**为什么不用 LangGraph/ADK**。本题只有 5 个工具、单 Agent、无并行，
原生实现 80 行就能覆盖 ReAct 循环 + 上限 + trace，引入框架反而增加
"trace 在框架内部、评审看不到决策依据"的风险。若你有现成熟悉的框架，
用也可以，但必须把框架的调用事件导出成自己的 JSONL 格式。

第二，**为什么幂等键用 asset_id 而不是请求 hash**。同上文第 6 节，
因为题意要的是"一份草稿"，且评审会主动更换时段再提交来试探。

---

## 13. 下一步

按第 9 节时间表推进。25–60 分钟这一段（无模型骨架）如果需要，
可以直接让我把 `src/` 骨架、`data/` 合成数据、trace schema 和 README 对照表一次性搭出来，
你只需要接模型和填规则判断。
