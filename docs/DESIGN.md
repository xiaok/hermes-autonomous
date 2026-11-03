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
[1] 复盘   有到期跟进点且门控放行时被唤醒 → 取 due 的，查证据
[2] 判断   两条独立评分线 → 静默 / 做完再报 / 先问
[3] 学习   记录 outcome；被嫌烦/被忽略的类别自动静音
```

## 门控在代码里，prompt 只管判断与措辞

能不能发、发多少，不靠模型自觉。`followups.py` 里的 `send_gate()` 是唯一的判定，wake、digest、
send 三处都调用它，谁也不自己再算一遍夜间 / 冷却 / 静音：

- **夜间**：本地（`settings.timezone`，缺省系统时区）23:00–08:00
- **冷却**：距 `kv.last_send` 不足 90 分钟
- **静音**：`muted_whys()`——30 天内已发出的跟进点里，同一 `why` 有 1 次 `annoyed` 或 3 次 `ignored`

`followups.py send --ids …` 是私聊的**唯一出口**：先过 `send_gate`，再检查每个 id 存在、active、
真的 due（发过且未重新到期的不算），最后检查 ≤3 个 id、正文非空、≤6 个非空行。任何一条不过就以
rc=2 拒绝（`refused=night|cooling|muted|not_due|too_many_ids|too_many_lines|…`），**不发送、不截断**，
这一轮就不发消息。通过后调用 `hermes send`，拿到 message_id 才算送达，并在同一把锁内立即记账
（`sent_at` / `sends` / `sent_text` / `revisit_at = now+24h` / `kv.last_send`），不等 `apply`。

`prompts/review.md` 只负责：判断哪些 due 的值得说、怎么措辞、记录 outcome，以及用一次 `apply`
把本轮写回。prompt 里写的上限只是提示，真正的上限在 `send` 里。

### [1] 判断：两条线，各自独立

**实用线**（utility / risk / curiosity）：相关 0–2 + 变化 0–2 + 可行动 0–2 + 时效 0–2 + 风险 −2..0 → **≥5 发**

**关心线**（care）：是真事 / 细节具体 / 时机对 / 没有重复 → **四条全过就发**

这条分线是实际使用中补上的：只用“可行动性”打分，会把「喉咙好点没」永远判成 0 分而不发。

### [2] 动作三档（按风险，不按类型）

- **静默做**：只读查询（天气、价格、新闻、日历读）
- **做完再报**：生成文档 / 表格 / 摘要，跑一次检查
- **先问**：改日历、设提醒、给别人发消息、花钱、删除

### [3] 学习

- 每次复盘开始时，agent 看 48 小时内发出的跟进点之后主人的反应，经 `apply` 的 `outcomes` 记成
  `accepted` / `ignored` / `annoyed` / `useless` / `unknown`
- 30 天内已发出的跟进点：同一类别（`why`）被 `ignored` 3 次或 `annoyed` 1 次 → 整个类别静音
  （到期也不唤醒 review，digest 标 `held_reason=muted`，`send` 拒绝）；超过 30 天的反馈不再计入
- 静音只看已记录的 outcome：agent 没写 outcome，就不会静音；`useless` / `unknown` 不计入
- 未实现（只是方向）：把猜错的案例沉淀成可回滚的 playbook 规则

## 运行形态

三步：**每 15 分钟门卫 → monitor 抑制 → send 记账**。

1. **门卫**：一个 Hermes cron job 每 15 分钟（`*/15 * * * *`）先跑 `--monitor-script followup-wake.py`
   （即 `scripts/wake.py`，不调模型），打印**一行**状态：

   | 输出 | 含义 |
   |---|---|
   | `quiet` | 没有到期跟进点，也不需要 capture |
   | `capture` | 没有可发的，但距上次 capture ≥2 小时且主人有 ≥1 条新消息：只记账，不发私聊 |
   | `review ids 12,15` | 这些到期跟进点现在可以发（id 升序）；对应 digest 的 `may_send=true` |
   | `held night\|cooling\|muted` | 有到期的，但全部被门控压住（多种原因时按 night > cooling > muted 取一个）；对应 `may_send=false` |
   | `{"mode": "setup-needed"}` | 还没配置 owner |

   优先级：`review` > `capture` > `held` > `quiet`。
2. **monitor 抑制**：cron 的 monitor 对门卫的输出**按字节做哈希**，与上次相同就不唤醒 agent。所以每个
   状态的输出必须字节稳定：不带时间戳、计数或会变的顺序。`held …` 只写原因、不写 id，压住期间就一直
   是同一行，不会每 15 分钟叫醒一次；状态真的变了（冷却结束、到了早上、新到期一项）输出才变。
3. **send 记账**：被唤醒后，`--script followup-digest.py` 先把 `followups.py digest` 的 JSON 交给 agent
   （`mode` / `may_send` / `held_reason` 与门卫读的是同一个 `send_gate`）。agent 按 `prompts/review.md`
   判断，要发就调 `followups.py send`（见上节：唯一出口，拒绝即不发，通过即记账），最后用一次
   `followups.py apply` 写回新跟进点、decisions、outcomes 和水位。

- **投递**：job 用 `--deliver local`，正常 run 的最终回复不进聊天；只有 job 失败才 `--failure-deliver origin`
- **开发输出**：`setup --dev-log` 打开后，最终回复是一段开发日志；默认 `[SILENT]`
- **夜间**：23:00–08:00 门卫输出 `held night`（或 `capture`），`send` 拒绝；到期的攒到早上

## 数据

`~/.hermes/plugin-data/hermes-autonomous/followups.db`

```
followups(id, key, kind, why, subject, quote, hint, revisit_at,
          status, checks, last_checked, note, created_at,
          sent_at, sent_msg_id, outcome, sends, sent_text)
watermark(surface, last_ts)
kv(k, v)              -- last_send（冷却起点）、last_capture（capture 节奏）
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
3. **隐私**：复盘读主人会话里的全部用户/助手消息（默认只有私聊，`include_groups` 才读群聊、话题/thread、论坛话题/forum 与频道/channel），数据不出本机。
4. **过度解读**：没有原话依据的一律降级为问句。
