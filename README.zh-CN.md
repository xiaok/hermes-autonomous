# hermes-autonomous

**你的 agent 应该记得回来看你。**

大多数助手只会等：你问，它答，对话结束——于是你周二提过的事（你病了、你明天要见人、你刚买了个东西）再也不会被想起来。

`hermes-autonomous` 是给 [Hermes Agent](https://hermes-agent.nousresearch.com) 的主动性层：它会安静地读你们的对话，找出「过一阵子回来看一眼会让人有用、或者觉得被记得」的事，然后在合适的时候回来——有时带一条实用信息（天气、价格、截止日），有时只是问一句「喉咙好点没」。

它不是日报，不是任务清单，也不需要你配置「它该盯什么」。

[English README](README.md)

---

## 安装（约两分钟，不用写代码）

它就是一个普通 Hermes 插件，安装只要一行：

```bash
hermes plugins install xiaok/hermes-autonomous --enable
```

再把下面四行填好，把它接到你的聊天并挂上定时任务：

```bash
PLUGIN=~/.hermes/plugins/hermes-autonomous
python3 $PLUGIN/scripts/followups.py setup --owner-id <你的 Telegram user id> --dm-target telegram:<chat_id>
cp $PLUGIN/cron/*.py ~/.hermes/scripts/
hermes cron create "*/15 * * * *" --name "主动跟进" \
  --monitor-script followup-wake.py --script followup-digest.py \
  --prompt "读 $PLUGIN/prompts/review.md 并执行" --deliver origin
```

不知道自己的 id？找 [@userinfobot](https://t.me/userinfobot)，或者直接问 Hermes——它知道当前会话的 id。

也可以直接跟你的 agent 说：

> 帮我装插件 `xiaok/hermes-autonomous` 并启用，配好当前这个会话，然后建好复盘的定时任务。

### 从源码安装（开发用）

```bash
git clone https://github.com/xiaok/hermes-autonomous.git ~/opt/hermes-autonomous
ln -s ~/opt/hermes-autonomous ~/.hermes/plugins/hermes-autonomous
hermes plugins enable hermes-autonomous          # 工具下个会话生效
python3 ~/opt/hermes-autonomous/scripts/followups.py setup --owner-id … --dm-target …
```

---

## 它到底在做什么

```
每 15 分钟   一条纯脚本问「现在值得叫醒吗」                    ~0 token
值得         读最近几小时对话、更新跟进表
             查天气 / 价格 / 页面 / 日历
             判断：静默 · 做完再报 · 先问
             最多一条消息，最多三项
```

开口之前必须同时满足四条：

| 闸门 | 规则 |
|---|---|
| 到期 | 这件事**现在**值得回看，不是固定早上九点的摘要 |
| 变化 | 跟上次讨论相比有新信息；重复不算跟进 |
| 有据 | 引得出你当时的原话；没原话不发 |
| 配额 | 每次唤醒 ≤1 条、≤3 项、90 分钟冷却、23:00–08:00 静默 |

它不会变成噪音的核心原因：**沉默是一个被计分的结果**。被忽略的跟进点会自己降级；同一类你摆手三次，它就不再开口。

## 两种价值（大多数助手漏掉的那一半）

- **实用** —— 明天要见人，这里是天气和出门时间。
- **关心** —— 你说过喉咙疼，两天后问一句「好点没」。零行动价值，却可能是最重要的一条消息。

所以判断跑两条独立的评分线：只按「可不可行动」打分，会把所有关心类消息悄悄删掉。

## 为什么是这个形状

这些产品的尸检报告我们已经替你读完了：ChatGPT Pulse 上线九个月后下线，它自己的说明里就承认「你可能收到一个已经完成项目的建议」，而且第一版没有总开关。死掉的主动式助手都死于同一组原因：固定日程、不回写完成状态、把配置成本丢给用户。

这个实现绑事件、回写每一次结果、只向你要一个「是/否」。完整拆解和研究数据（最好的模型判断「该不该开口」也只有约 66% F1、35–48% precision）见 [`docs/RESEARCH.md`](docs/RESEARCH.md) 与 [`docs/teardown.md`](docs/teardown.md)。

## 判断是怎么做的

- [`prompts/decide.md`](prompts/decide.md) —— 规则唯一来源：什么值得回访、两条评分线、三档动作。
- [`scripts/decide.py`](scripts/decide.py) —— 离线跑任意场景，看它到底怎么判：
  `python3 scripts/decide.py --scenario scenarios/01-throat.json`
- [`docs/DECIDER.md`](docs/DECIDER.md) —— 输入、闸门、输出结构。

## 你的数据

- 只读**你自己**发的消息（你的私聊、你自己建的话题）。别人的文字不会进入模型。
- 状态存在 `~/.hermes/plugin-data/hermes-autonomous/`（SQLite + 配置，权限 0600），不进仓库。
- 仓库里**没有**任何 id、名字、聊天记录或个人场景——都在本地配置。

## 它碰了什么（披露）

- **读**：Hermes 的会话库 `~/.hermes/state.db`，只读，且只读**你自己**在私聊和本人话题里发的消息；不读其他文件。
- **写**：只有自己的数据目录 `~/.hermes/plugin-data/hermes-autonomous/`（SQLite 跟进表 + `settings.json`，权限 0600）。
- **网络**：插件代码本身不发任何请求。复盘时 agent 可能用 Hermes 自带工具查天气/价格/新闻——那是 Hermes 的流量，不是这个插件的。
- **Shell**：`/followups` 只用 `sys.executable` 跑一个本地 Python 脚本；cron 那侧也是纯 Python。不 spawn `hermes`、不注册 hook、没有后台线程、不绕过审批。
- **凭据**：没有。Telegram id 存在插件自己的 `settings.json`（0600），不读 `.env`、`auth.json` 或钥匙串。
- 无遥测、无统计上报。

## 文档

[English](README.md) · [设计](docs/DESIGN.md) · [调研](docs/RESEARCH.md) · [判断规范](docs/DECIDER.md)

MIT。
