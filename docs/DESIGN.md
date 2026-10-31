# DESIGN：主动性层（proactivity layer）

> 不是“可自主执行的动作清单”，也不是“未完成任务列表”。是**自己发现哪些事过一阵子回来看一眼会让他觉得有用、或者觉得被记得**。

## 单位：followup（跟进点），不是 task

一条 followup 回答四个问题：

- `why` —— `care` 情绪 / `utility` 实用 / `risk` 风险 / `curiosity` 好奇
- `subject` —— 一句话说清是什么事
- `quote` —— 当时的原话（可引用，防臆想）
- `revisit_at` —— **什么时候回来看才有意义**（不是截止时间）

用三个例子说明区别：

- 「明天要出门」→ 值得跟进，不是因为没做完，而是明早的天气能改变决定（`utility`）
- 「mint 完 NFT」→ 事情**已经闭合**，但 24h 后的地板价有意义（`curiosity`）
- 「喉咙痛」→ 没有任何行动价值，第二天问一句仍然值得（`care`）

所以状态不是 open/closed，而是 `active / closed`；`closed` 只表示“再提就是啰嗦”。

## 四层

```
[0] 记账   读自上次水位以来的对话 → 更新 followups 表（不出声）
[1] 复盘   09:00 / 19:00 → 取 due 的，查证据
[2] 判断   两条独立评分线 → 静默 / 做完再报 / 先问
[3] 学习   记录结果；每周把猜错的案例写成 playbook 规则
```

### [1] 判断：两条线，各自独立

**实用线**（utility / risk / curiosity）：相关 0–2 + 变化 0–2 + 可行动 0–2 + 时效 0–2 + 风险 −2..0 → **≥5 发**

**关心线**（care）：是真事 / 细节具体 / 时机对 / 没有重复 → **四条全过就发**

这条分线是实际使用中补上的：只用“可行动性”打分，会把「喉咙好点没」永远判成 0 分而不发。

### [2] 动作三档（按风险，不按类型）

- **静默做**：只读查询（天气、价格、新闻、日历读）
- **做完再报**：生成文档 / 表格 / 摘要，跑一次检查
- **先问**：改日历、设提醒、给别人发消息、花钱、删除

### [3] 学习

- 每条发出的跟进点记 outcome：采纳 / 忽略 / 嫌烦 / 事后无用
- 同类被忽略 3 次 → 该类降为静默；被明确嫌烦 1 次 → 该类以后先问
- 每周把**猜错的案例**（发了被无视、该发没发）写成规则，进 `playbook.md`，带版本号可回滚
- 规则只有在重复命中后才晋升 —— 和 skill 的演进方式一致

## 运行形态

- **复盘**：事件驱动。一个 Hermes cron job 每 15 分钟（`*/15 * * * *`）先跑门卫 `scripts/wake.py`
  （不调模型）：没事就输出常量 `quiet`，输出与上次相同时 cron 的 monitor 直接跳过 agent；
  有到期跟进点（且不在 90 分钟冷却 / 夜间）→ review，距上次 capture ≥2 小时且有新消息 → capture
- **输入**：`scripts/followups.py digest`（自上次水位的消息 + 现行跟进点）
- **提示词**：`prompts/review.md`
- **产品输出**：`hermes send --to "$(followups.py dm-target)"`（私聊，≤1 条/次，≤3 项）
- **开发输出**：cron 的最终回复 → 本话题（测试阶段额外要求：想了什么、做了什么、没发什么、存疑什么）
- **预算**：每条最多 3 次只读查询；整个 run 5 分钟内
- **静默**：23:00–08:00 不发私聊（复盘照跑，攒到早上）

## 数据

`~/.hermes/plugin-data/hermes-autonomous/followups.db`

```
followups(id, key, kind, why, subject, quote, hint, revisit_at,
          status, checks, last_checked, note, created_at)
watermark(surface, last_ts)
log(id, run_at, kind, payload)
```

## 不做的事

v0.1 的那套任务队列（`autonomy_*` 工具 + 无人值守 tick）已经删掉：它会 spawn 一个全工具的
headless 会话来执行没人当场批准的任务，既用不上，也过不了插件目录的审批规则（rule 12）。
现在插件只有 `scripts/` 里几个本地脚本 + 一个 `/followups` 命令，自己不注册任何后台执行器；
唯一的定时运行是用户自己用 `hermes cron create` 建的那个 job（`hermes cron list` 里看得到、可删）。

## 已知风险

1. **误报**：ProactiveBench 最好的模型 precision 只有 35–48%，所以“不及格就不发”必须严格执行，且要有日志能回头看。
2. **重复**：同一件事不能每天提，`checks` 计数 + `revisit_at` 后移。
3. **隐私**：复盘读主人会话里的全部用户/助手消息（默认只有私聊，`include_groups` 才读群），数据不出本机。
4. **过度解读**：没有原话依据的一律降级为问句。
