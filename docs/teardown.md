# 主动式 AI 助手产品拆解（2025–2026）

> 口径说明：下文把"厂商自己说的话"和"第三方实测"分开标注。厂商博客/帮助中心只证明**设计意图与可配置项**，不证明体验好坏；体验结论一律引第三方实测或用户原帖。

---

## 1. ChatGPT Pulse（OpenAI）——2025-09-25 上线，2026-06-17 下线

**(a) 机制**
夜间异步研究（OpenAI 原话 "asynchronous research"）：每晚读取 memory + 聊天历史 + 用户反馈 + 可选连接的 Gmail/Google Calendar，自行生成检索式并产出"主题卡片"，第二天早上推送。属于**定时摘要型**，不是事件触发。卡片只活 24 小时，除非保存为 chat 或继续追问。
- https://openai.com/index/introducing-chatgpt-pulse/
- https://arstechnica.com/ai/2025/09/chatgpt-pulse-delivers-morning-updates-based-on-your-chat-history/

**(b) 频率与用户控制**
每天一次；首版 5–10 张卡片；Pro only（$200/月）起步。
控制手段三个：① "curate" 按钮用自然语言指定明天想看什么；② 单卡 thumbs up/down，可查看/删除反馈历史；③ Gmail/Calendar 集成默认关闭、可随时关。
关键缺陷：**没有一键关闭整个 Pulse 的开关**。上线次日就有 Pro 用户在官方论坛要求移除，OpenAI 客服（AI 客服）回复"目前无法 opt out"。
- https://techcrunch.com/2025/09/25/openai-launches-chatgpt-pulse-to-proactively-write-you-morning-briefs/
- https://community.openai.com/t/pulse-please-remove-disable-this-feature-on-mobile-pro-user/1359967
- 反例细节：后来用户发现"可以自己定义每天收到 Pulse 的时间"（社区帖），说明控制项是逐步补上的，不是设计时就有的 —— https://community.openai.com/t/what-has-chatgpt-pulse-done-to-help-you/1371024

**(c) 实际内容**
官方 demo：当晚有饭局 → 给出"跑步 45–50 分钟路线，终点在 KAIYO Rooftop 附近，含 buffer 和备选方案"；乳糖不耐 → "KAIYO 点单策略"；另有核心/Pilates 训练计划、生日徒步的加油点规划、出差后的恢复训练。
- https://www.theverge.com/ai-artificial-intelligence/785881/openai-really-really-wants-you-to-start-your-day-with-chatgpt-pulse

**(d) 失败模式（有据可查）**
1. **任务已完成还继续推**——OpenAI 自己在 limitations 里承认："you may get tips for a project you already completed"。这是"没有完成状态回写"的典型 bug，不是偶发。https://openai.com/index/introducing-chatgpt-pulse/
2. **价值需要用户先做功课**——OpenAI 自己的 ChatGPT Lab 结论："many started to feel its utility once they started telling ChatGPT what they wanted to see"；重度用户原话是"it does take some effort, like prompt engineering"。即：**主动式产品把 curation 成本转嫁给了用户**。https://openai.com/index/introducing-chatgpt-pulse/ 、 https://community.openai.com/t/what-has-chatgpt-pulse-done-to-help-you/1371024
3. **被判定为 feed/engagement 把戏**——NYMag 直接把它类比 Google Now 与 TikTok feed，指出真实动机是把"工具"变成"可消费的内容"以承接广告位。https://nymag.com/intelligencer/article/sam-altman-openai-pulse-chatgpt-feed.html
4. **反讽点**：OpenAI 明明做了反无限滚动的设计（"This experience ends"，官方技术负责人原话），仍被普遍读成 feed。说明**形式（卡片流）比意图更能决定用户感知**。https://www.theverge.com/ai-artificial-intelligence/785881/openai-really-really-wants-you-to-start-your-day-with-chatgpt-pulse
5. **价格错配**——Tom's Guide 评测把"$200/月买一个更花哨的通知"列为首要罪状，并质疑"你日历本来就会提醒你航班"。https://www.tomsguide.com/ai/i-test-ai-for-a-living-and-this-is-hands-down-the-worst-new-ai-tool-of-2025
6. **结局**：2026-06-17 宣布 14 天内下线，导流到 Scheduled tasks（"faster, more reliable, easier to manage"）。存活约 9 个月。https://www.theverge.com/ai-artificial-intelligence/951840/openai-is-sunsetting-pulse-which-showed-you-custom-daily-digests-in-chatgpt

**替代品的硬约束（对设计直接有用）**：Scheduled tasks 免费版"每天最多一次"，付费版可到每小时；event-triggered 任务上限 30 次/小时、720 次/天；活跃任务数 3/5/10/15 按档位。这是 OpenAI 用产品结构给出的"消息预算"答案：**默认一天一次，事件型才允许高频，且一定有硬上限**。https://help.openai.com/en/articles/10291617-scheduled-tasks-in-chatgpt

---

## 2. Meta Muse（2026-09-08）

**(a) 机制**
三层，注意区分：
1. **后台任务 + 事件跟进**（真主动）：用户设定 goal/task 后，Muse 在自己的云 VM 里按计划、也按"相关事件"持续推进；完成后**先自评"这个结果值不值得打扰你"**，只在"meaningfully new 或需要你输入"时才通知。设计原文："When it completes its background work, it evaluates whether the result is worth surfacing, and notifies you only when something is meaningfully new or needs your input." https://introducing.muse.ai/
2. **Feed tab**（半主动）：按一段文本 prompt 生成资讯流，默认 prompt 是"Make me a feed about my interests…avoid clickbait"。是"你给主题、它每天组装"，本质是 prompt 驱动的简报，不是自主发现。https://www.theverge.com/tech/993391/meta-muse-ai-hands-on
3. **Ideas / Goals tab**（拉式建议）：Ideas 是"你可以让我做这些"的建议清单；Goals 追踪长期目标与计划。https://introducing.muse.ai/

**(b) 频率与控制**
官方只说"默认值适合大多数人"，控制方式是**对话式**："you can always tell your Muse to turn it off, dial it down, or dial it up"。没有文档化的滑块/频率档位设置页。不可逆动作走 human-in-the-loop 审批卡，默认"普通浏览放行、难以撤销的动作才停"。
- https://introducing.muse.ai/
- 注意这条设计自觉："we also wanted to avoid banner blindness — where people approve everything to make it go away"，所以故意不把审批做成高频弹窗。

**(c) 实际内容**
- 官方最佳案例：学校邮件里埋着"儿子明天体育选拔截止"，Muse 在作者登机前推送，救回报名。https://introducing.muse.ai/
- 实测：帮清掉数千封推广邮件；发现购物车里有其他商品、下单前主动问是否移除；逛 Facebook Marketplace 找二手沙发，**第二天主动追问"你昨天说最喜欢那张沙发，要不要联系卖家"**。https://www.wired.com/story/metas-muse-is-better-at-surveilling-than-helping-me/
- 反面：Feed 把"我当前所在州"的本地新闻推给我，理由是**从 Amazon 订单的收货地址推断**的——跨源推断，用户没预期。https://www.theverge.com/tech/993391/meta-muse-ai-hands-on

**(d) 失败模式**
1. **通知退化为增长提示**：WIRED 记者卸载 App 前收到的最后一条推送是——"Connect your apps so I can do more"。他把整个 Ideas tab 描述为"每一条建议都是让我再交一份数据的伪装"（连护照/驾照到期日都要）。**这是最典型的"主动消息被商业化污染"**。https://www.wired.com/story/metas-muse-is-better-at-surveilling-than-helping-me/
2. **公共讨论直接点名 spam**：早期用户公开抱怨"头几天还不错，现在感觉像 spam"。https://xitter.cf/SullyOmarr/status/2109544339659751708 （原帖镜像，可信度弱于媒体实测，仅作方向性证据）
3. **越界推断 + 无法自解释**：Verge 记者问"你怎么知道我 IG 兴趣的"，Muse 答"我读了账号 API 数据，比 UI 展示的多"；Meta 发言人确认它确实"pull and infer interests based on your IG activity"。同一位记者第二次测试时，Muse 声称读了 Mac 通知预览，被问到"具体怎么拿到的"时回答"Honest answer: I can't give you the exact plumbing"，Meta 工程 VP 事后承认"它对自己的内部机制解释错了，这是我们的问题"。
   - https://www.theverge.com/tech/993391/meta-muse-ai-hands-on
   - https://www.theverge.com/ai-artificial-intelligence/997833/meta-muse-creepy
4. **不可关闭的记忆**：Memory 是可读可编辑的文件，但"Meta doesn't currently offer a toggle to turn off the memory feature altogether"。对一个"靠记忆做主动"的产品，这是透明度上的硬伤。https://www.wired.com/story/metas-muse-is-better-at-surveilling-than-helping-me/
5. **规模事实**：首周下载 90 万+（Sensor Tower，经 WIRED 转引）——说明"主动式"确实有需求，问题不在需求侧。同上。

---

## 3. Google Gemini Daily Brief（2026-05-19 发布，2026-09 起美国免费）

**(a) 机制**
定时摘要型，且是"编排器"模式：后台扫描 Gmail / Calendar / Gemini 历史聊天 / Connected Apps，每早生成固定结构的三段——`Top of mind`（当天的时效性待办）、`FYI`（有日期的信息）、`Looking ahead`（长期目标的下一步建议）。**每天凌晨后生成一次，之后是静态的**：2026 年 8 月的 APK 拆包显示 Google 正在做"Refresh daily brief"手动刷新，反证当时不能刷新。
- https://blog.google/innovation-and-ai/products/gemini-app/next-evolution-gemini-app/
- https://support.google.com/gemini/answer/17077455
- https://www.androidauthority.com/gemini-daily-brief-manual-refresh-apk-teardown-3711310/

**(b) 频率与控制**
每早一条通知（9to5Google 明确写了"Google also sending a notification every morning"，可在 account menu → Notifications 关掉）。免费化的确切节点：9to5Google 2026-09-08 报道称"上周五起"不再需要 Google AI 订阅，即 2026-09-04。前置条件很重：必须开 Personal Intelligence + Memory + Workspace 连接；不支持 Workspace/学校/受监管账号；US、English、18+。
条目级控制：Mark complete / Dismiss / Helpful / Not helpful / 就某条继续聊。整体开关在 Settings → Personal Intelligence → Daily brief。
- https://9to5google.com/2026/09/08/gemini-daily-brief-free/
- https://support.google.com/gemini/answer/17077455

**(c) 实际内容**
官方给的人群化例子：学生（课表、申请进度）、创业者（可执行的客户邮件）、家长（学校通知、家庭里程碑）、求职者（猎头邮件、面试准备）。每条可点 "More" 看数据来源。
- https://gemini.google/overview/daily-brief/
- https://support.google.com/gemini/answer/17077455

**(d) 失败模式**
1. **"知道我的数据，不知道我的优先级"**——德国记者数周实测：简报主体全是 newsletter / mailing list / 边缘通知，真正重要的没识别出来；每周固定时间来的 Synology 通知被当成要事（"Google 应该能自动识别这种周期性"）。结论句很狠："Es sieht meine Daten, nicht meine Prioritäten." https://www.inside-digital.de/news/ausprobiert-googles-daily-brief-kennt-meine-mails-aber-nicht-meine-prioritaeten
2. **反馈信号被无视**——同一位记者：把"继续之前的对话"连续几天标记完成，第二天照样再出现。**这是"用户已处理"状态没有回写进生成器的典型故障**，和 Pulse 的"已完成项目还给建议"是同一类 bug。同上。
3. **"设置"是幻觉**——他在聊天里让 Gemini 改简报格式，Gemini 给了几个方案，选完"什么都没变，今天还是第一天的样子"。同上。
4. **静态快照问题**——一天只生成一次，白天日程变了不会更新，这正是 Google 后来要加"Refresh"的原因。https://www.androidauthority.com/gemini-daily-brief-manual-refresh-apk-teardown-3711310/
5. **前车之鉴 Daily Hub**：Pixel 10（2025-08）首发的 Daily Hub 是同一定位的产品，上线几天就被 Google 撤下，再没回来。罪状包括"只显示当前时刻一个温度"、"推荐过一期 Honey Boo Boo 的 AI 视频"、"推荐'Perfect belly dance finger cymbals'"、"需要用时偏偏不推机票邮件"、"和 Tasks/Keep/Fitbit 零整合"。这是整份材料里**最完整的"主动信息面板失败"清单**。https://www.androidauthority.com/google-gemini-daily-brief-already-ruined-3668798/

---

## 4. Claude Duet（Anthropic，2026-10-21，waitlist research preview）——当前最接近"正确答案"的样本

**(a) 机制**：两段式。① 每早一份"short brief"（跨已连接 app 的最重要事项）；② 白天**轮询**已连接账号，"只在有事需要你时才发消息"。原文给了坦白的工程理由："Claude Duet checks your connected accounts every few hours, because checking nonstop would use up your plan's limits much faster." 即**频率是被成本约束出来的，不是被设计美学约束出来的**。
**(b) 频率与控制**："briefs you every morning, drafts next steps, then goes quiet until something needs you"；用户可告知"想听什么、多久听一次"；发送/付款/签约/删除前必须展示即将执行的动作并等待批准；证券交易、账户间转账即便批准也不做。
**(c) 内容**：产品页给的样例 brief 是"5 things in progress and 2 that need you"，每条带状态与下一步（"Hiring: Jenn's other offer expires at 6pm. All 3 finalists are booked today."）。
**(d) 自认缺陷**："it still might misread a thread or miscalibrate what's urgent"，以及"it can miss something that arrives between checking in with you"——**轮询必然漏事件，这是主动式系统的结构性缺口，值得写进设计文档**。
- https://claude.com/resources/articles/claude-duet
- https://claude.com/product/claude-duet

---

## 5. 小产品与"失败的主动式"

### Dot（New Computer）——2024-06 上线，2025-09 宣布关停
机制：人格化伴侣 + 主动性推送，典型是**早上一条基于记忆的问候/话题**。控制几乎为零。
失败样本（设计师 Dave Klein 的逐屏拆解，最具体的一手记录）：第二天收到的推送是——"Good morning Dave! I hope you're enjoying the fresh air and beautiful views on your morning hike. I was thinking about your incredible vintage Apple finds and wondering…"。他的反应："I didn't go on a hike this morning so that's odd" + "Dot is desperate to talk about my Apple collection and nothing else at this point"。
**两个失败同时发生：事实错误（我没去徒步）+ 话题固着（只记得一件旧事）——这是"记忆稀薄时做主动"的必然结果。**
- https://diklein.com/writing/on-the-design-of-dot
- https://new.computer/ （关停公告，运营至 2025-10-05）
- https://techcrunch.com/2025/09/05/personalized-ai-companion-app-dot-is-shutting-down/

### Limitless / Rewind——被 Meta 收购，硬件线终止
机制：Pendant 24/7 连续录音 + 转写 + 会议准备/摘要，主动面主要是**会前 briefing 和会后总结**（拉式为主）。不是"无缘无故来找你"型。
结局：2025-12-05 宣布被 Meta 收购；停止向新用户售卖 Pendant；Rewind 于 2025-12-19 起禁用全部屏幕与音频采集；非 Pendant 功能（含 Rewind）下线。
- https://www.limitless.ai/
- https://9to5mac.com/2025/12/05/rewind-limitless-meta-acquisition/
- https://help.limitless.ai/en/articles/10761340-pendant-storage （"designed to record conversations continuously 24/7"）

### Rabbit r1（rabbitOS 2.3 / OS3）——最诚实的取舍：把"主动"做成手动
"Proactive Rabbit Briefing"：**三击**主屏兔子，它自己开口说"此刻最有用的一件事"，带人格和时段语气。规则很具体，可直接抄：提醒优先级最高（24h 内的几乎必定打头）；星标笔记永久候选；最近 48h 笔记 / 7 天录音自动候选；**一小时内不重复同一件事**；没料了就转成闲聊/笑话；离线降级为纯文字。
关键：**它默认不主动打扰你，主动性由一次手势触发**。目前是 easter egg，"no menu for it (yet)"。
- https://forum.rabbitcommunity.tech/t/proactive-rabbit-a-quick-how-to-guide/15852
- https://www.rabbit.tech/updates

### Apple（Siri AI，2026-09-14 beta）——基本不做"无端推送"
Apple 的主动面全部是**事件/位置/用户预设触发**，且都发生在系统 UI 里，不进聊天流：`Call Context`（给商家打电话时把验证码/预订号递到眼前）、`Safari Notify Me`（用户指定监控某网页的补货/降价）、`Siri Recap`（可选、按对话结束事件生成摘要，非转写，排除金融与证件信息）、Health `Insights`（数据驱动建议）。`Siri Recap`/`Live Rewind` 还明确写了"受每日服务端用量限制"。
结论：**Apple 用"零未读"换取零反感**——代价是它在"主动帮你想到"这件事上几乎缺席。
- https://www.apple.com/newsroom/2026/09/siri-ai-a-profoundly-more-capable-and-personal-assistant-is-here/

### OpenAI Dots（2026-10）——"高价值主动"的正面例子
Fast Company 实测：接入日历与邮箱后，Dots 主动指出"我日历上的演讲日期与确认邮件里的日期不一致"。这是**跨源一致性校验**——主动式系统里性价比最高的一类触发：不需要预测用户想要什么，只需要发现两个可信来源互相矛盾。
- https://www.fastcompany.com/91620082/openai-dots-agent-review
- https://www.axios.com/2026/10/20/battle-of-bots-openai-meta-ai-agents-test

---

## 6. 横向对照

| | 触发方式 | 默认频率 | 一条消息的"门槛"由谁定 | 关闭粒度 | 主要翻车点 |
|---|---|---|---|---|---|
| ChatGPT Pulse | 夜间定时研究 | 1 次/天，5–10 卡 | 模型自评相关性 | 仅逐条反馈，**无总开关**（首版） | 已完成项目重推；价值依赖用户 curation；被读成 feed |
| Meta Muse | 任务/目标后台推进 + 事件 | 事件驱动，无公开上限 | 模型自评"值不值得打断" | 仅对话式（关小/关大/关掉） | 通知变增长钩子；跨源推断越界；无法自解释 |
| Gemini Daily Brief | 凌晨定时 | 1 次/天 + 1 条通知 | 模型排优先级 | 条目级 + 总结开关 | 把 newsletter 当要事；完成状态不回写；白天不更新 |
| Claude Duet | 早报 + 数小时轮询 | 1 次/天 + 有事才发 | 模型判断 + 审批门 | 早报/频率可对话调整 | 轮询漏事件；紧急度误判 |
| Dot | 记忆驱动的问候 | ~1 次/天 | 几乎无门槛 | 无 | 事实错误 + 话题固着 |
| Rabbit | **用户手势触发** | 0（默认不推） | 用户 | 无（本来就不推） | 需要用户记得去问 |
| Apple | 事件/预设监控 | 0 | 系统规则 | 逐功能 opt-in | 几乎不主动 |

---

## 7. 十条结论（成功者做对了什么，噪音制造者做错了什么）

1. 成功者把"主动"绑定在**用户已经承诺的目标/任务**上（Muse 的 Goals、Duet 的 handoff），噪音制造者绑定在"用户可能感兴趣"上（Pulse 的兴趣推断、Daily Brief 的收件箱）。
2. 成功者发消息前有一次**显式的"值不值得打断"判定**并写进设计文档（Muse: "notifies you only when something is meaningfully new or needs your input"），失败者只有"生成成功"这一个条件。
3. 消息预算必须是硬上限而不是"模型觉得合适"：OpenAI 最后落到"免费 1 次/天、事件型 30 次/小时封顶"，Duet 落到"每几小时轮询一次"，都是被成本逼出来的诚实约束。
4. 一天一次不够，因为**白天发生的事等不到明早**（Daily Brief 静态快照、Pulse 2pm 的邮件明早才知道）——正确形态是"低频摘要 + 高频但极窄的事件触发"，而不是二选一。
5. 触发质量最高的不是"预测你想要什么"，而是**跨源矛盾检测**：日历 vs 确认邮件（Dots）、学校邮件里的截止日期 vs 家庭日历（Muse）、会议 vs 航班时间冲突（Duet）。这类消息几乎不会被嫌吵。
6. 所有失败案例共享同一个 bug：**"已完成/已处理"状态没有回写进生成器**（Pulse 推已完成项目、Daily Brief 重推已 mark complete 的对话）。这是主动式系统的头号工程债。
7. 用户反馈信号极其昂贵且经常无效：Pulse 需要用户先"像写 prompt 一样"curate 才有用，Daily Brief 的 completed 标记被无视——**把 curation 成本转嫁给用户，等于宣布产品失败**。
8. 关闭粒度决定信任下限：Pulse 首版"无法 opt out"直接引发 Pro 用户公开要求退款/转投竞品；Muse 连 memory 都没有总开关。**没有 kill switch 的主动功能，用户会先关掉整个产品**。
9. 形式即语义：Pulse 明明设计了"体验会结束"，仍被普遍读成 TikTok 式 feed——**卡片流/每日摘要这种形态天然承袭信息流的怀疑**，而"一条消息 + 一个待办"不会。
10. 最后一个反直觉的：Apple 和 Rabbit 用"默认不主动/手势触发"换来了零反感，说明**主动性的价值不在于频率，而在于"每次都对"**——在准确率没到之前，把主动权交回用户（三击、问一句）比提高推送量更接近产品-market fit。
