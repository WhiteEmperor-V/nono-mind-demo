审查范围:README/wikis架构(v7三明治) + state/model.py、state/server.py、core/loop.py、organs/(dialogue、reflection、wechat_loop)、immune/router.py、immune/plugins.py、llm_client.py、tools/(gen_image、canvas_push、nono_dsh、nono_cliproxy_proxy)、canvas/canvas_server.py、plugins/heartbeat、deploy/*。全程只读, 未修改文件。运行态取证: 当前 `state.db` signals 表中 20 行全部 `strength=0.15`(见 P1-1 证据)。

---

## P0(崩溃/数据损坏/安全)

**P0-1 画布服务裸奔在 0.0.0.0, 可未授权读取任意本地文件 + 伪造主人消息**
`canvas/canvas_server.py:38` `/push`、`canvas/canvas_server.py:104` `/action`、`canvas/canvas_server.py:266` `uvicorn.run(host="0.0.0.0")`
- `/push {"type":"page","path":"/root/nono-mind/weixin/account.json","name":"x"}`(或 `type=image`, 代码 `:61-67`、`:48-54`)会把**服务端进程可读的任意文件**(bot_token、state.db 全量对话、wiki)复制进 `pages/images` 目录, 然后经 `/pages/x.html`、`/events`(`:136` 回显 URL)读走——纯未授权任意文件披露。
- `/action`(`:118-119`)把 `[画布] {action}: {value}` 以 `master_message` 直投 State, 无任何鉴权/来源绑定 → 任意网络用户可冒充主人给意识核喂话(污染对话历史、消耗 LLM 额度)。
- 模块文档明确该服务经 `<YOUR-TAILSCALE-IP>:8765` 供手机访问, 即**按设计暴露在网络上**。
- 修复: `/push`/`/action`/`/clear` 加共享密钥(与画布页 cookie)校验; 限制 `path` 只能落在 `canvas/` 白名单目录(拒绝绝对路径/`..`); `name` 做白名单字符校验; 若需局域网访问至少绑定内网 IP + 前置鉴权, 不要把服务端任意文件复制逻辑暴露给客户端指定路径。

**P0-2 付费 API Key 明文硬编码并已进 git 历史(远端为 GitHub)**
`organs/dialogue.py:36`、`tools/gen_image.py:7`(同一 `sk-65745f...`, git 已确认自 commit `3a1e7cc` 起存在于 `organs/dialogue.py`), 仓库 remote 为 `git@github.com:WhiteEmperor-V/nono-mind.git`。
- 若仓库公开, 该 key = 直接泄露, 可被他人盗用刷额度/记账; 即便私有也存在历史泄露与误推风险。
- 修复: 立即到 apikey.fun 吊销该 key 并轮换; key 移入 `/root/.hermes/config.yaml`(llm_client 已采用该模式)或环境变量, 并补 `.gitignore`。

---

## P1(严重逻辑/并发/数据丢失)

**P1-1 信号生命周期自相矛盾: P0 主人消息 ~70s 后不可见、~103s 被物理删除; P1/P2/P3 信号被保底机制永久卡在 0.15 永不回收**
`state/server.py:130` `_decay()`、`state/server.py:136-140`、`state/model.py:33-36` `DECAY_MIN=0.01`
- P0 信号 `decay` 被钳到 0.01/0.5s tick: 强度 1.0 → <0.25(查询阈值)约 69s, →<0.125(删除阈值)约 103s。而注释声称“P0 有 24h TTL”(`:132`)。意识核只要阻塞或重启 >2 分钟, 期间到达的主人消息被 `_decay` 直接删除, **永远丢失**——与 `P0_TTL=24h` 的设计契约完全矛盾。
- `state/server.py:136-140` 对 `task_done/loop_progress/heartbeat/loop_error` 每 tick 复活到 `threshold*0.6=0.15`, 而 `signals` 查询阈值是 `>0.25`(`:202-204`)→ 复活线低于可见线, 这些信号既不会被意识核拾取, 又永远高于删除线 0.125, **只增不减**。DB 取证: signals 表现在 20 行全部恰好 `0.15`, 零可见行——已确认“幽灵行”机制在真实运行。
- 附带:`loop_error`(P1)被同样卡死 → “reflection 重试 3 次”机制实际收不到任何衰减后的错误信号。
- 修复: 删除“复活”UPDATE(或把复活线提到 `> threshold`); P0 改用真正“只受 TTL/主动 drop 约束、豁免衰减删除”的处理; 给 `_decay` 增加过期与清理策略(如信号数上限 + 按 `created` 兜底删除)。

**P1-2 意识核单线程阻塞式 LLM 调用, 无抢占、无并行; 最坏单次卡死 ~9 分钟并连带 P1-1 丢消息**
`core/loop.py:63-91`、`organs/dialogue.py:40` `timeout=540`、`llm_client.py:13` `timeout=120`、`llm_client.py:51-53`(降级再 120s)
- 画布创作一次最长阻塞 540s, 期间新 master_message/risk 全部无法处理, 队列里超过 ~103s 的 P0 又被 P1-1 删除。对话 LLM 超时+降级最坏 ~241s。设计文档里的“P0 抢占”在单线程+同步网络 IO 下并不成立。
- 修复: LLM 调用移出主循环(子进程/线程池 + 超时), 或至少给信号等待队列做“P0 插队”与不丢策略; canvas 请求应异步化, 主循环只做分派。

**P1-3 微信 outbox 消费是“读-发-整表覆写”, 无 CAS; 并发追加可被覆盖丢失, 失败项每秒无限重试**
`organs/wechat_loop.py:760-812`
- `:802-808` 先再读一次列表取 tail 再 `kv_set` 整体覆写——第二次读与覆写之间若有写入者 `kv_append`, 该条被静默丢弃(State 明明提供了 `kv_set(expected_revision=...)` CAS 却没用)。
- `:808` 覆写失败时整条队列(含已发出的)原样保留 → 下一轮 1s 后**全部重发**, 主人收到重复消息。
- 失败项(`:796-800` failed 列表)每次 drain 都重试, drain 每 1s 一次; 对失效会话/无效接收者会以 ~1-2 次/s 的节奏永久锤 iLink(无失败计数、无退避、无上限), 与 `_send_text` 的多分片重试叠加还会产生“前几片已发、后片失败→下轮整条重发”的部分重复。
- 修复: 消费改为 CAS 循环(读 revision → 发送 → `kv_set(remaining, expected_revision=旧rev)`, 冲突则重读); failed 项记重试次数/时间戳, 退避并设置丢弃上限; 会话失效(-14)时暂停整个 outbox 而非每秒重试。

**P1-4 getupdates 消息先持久化 sync_buf、后处理消息 → 崩溃/路由失败即永久丢消息**
`organs/wechat_loop.py:836-838`、`organs/wechat_loop.py:696-700`
- `_save_sync_buf()` 在 `_process_msgs()` **之前**执行: 进程在处理中途崩溃, 重启后用已推进的 buf 拉不到这批消息 → 永久丢失。
- 即使不崩溃, `:699` 路由失败(如 State 瞬时不可用)也只是 log 后丢弃, 消息已 ack 无法重取。
- 修复: 先完整处理并落“处理游标”, 再持久化新 buf; 路由失败的消息要进本地待重投队列/挂起区, 而不是直接丢弃。

**P1-5 State 服务单条 sqlite 连接被多线程共享: 读不持锁, checkpoint backup 与并发读同连接交叠**
`state/server.py:27`、`state/server.py:195-206`(kv_get/kv_list/signals 无 `_wlock`)、`state/server.py:108-121`(`self.conn.backup()`)、`state/server.py:234-241`(worker 写)
- 设计上“唯一写者”只串行了写, 读与写/backup 仍在同一 `sqlite3.Connection` 上并发(仅 `check_same_thread=False` 放行)。SQLite 连接对象不支持多线程同时使用, 会出现偶发 `OperationalError: database is locked` / “cannot commit transaction - SQL statements in progress”; backup 期间同连接上有其他语句执行属于文档明示的 misuse, 快照/读取会随机失败。
- 修复: 每个 `_handle` 线程(及 worker)各自打开独立只读连接(WAL 天然多读); 写连接与 backup 源分离(如 `VACUUM INTO` 或独立连接做 `sqlite3 backup`)。

**P1-6 dialogue_history 单 key 无上限增长, 越过 1MB 响应上限后对话器官整体失效**
`organs/dialogue.py:68-78`、`state/server.py:198-200`(`kv_list` 无上限)、`state/server.py:13`(`MAX_MSG=1MB`)
- 每天历史是一个无限增长的 JSON 数组, 每轮对话 `kv_append` 都要整表读改写(O(n)); 当单日数组序列化超过 ~1MB(约数千条), `kv_get`/`kv_list` 响应触发客户端 `响应超过1MB上限`(`state/server.py:304`), dialogue/reflection 每轮必炸。当前 09-08 分片已 24 条/4.6KB, 增速约 200B/条。
- 修复: 对话历史按消息条数/字节数滚动裁剪(如每日只留最近 200 条), 或改为多行存多条记录 + 分页读取; `kv_list` 服务端设字节/条数上限。

**P1-7 日反思 run() 用 “w” 覆写当天日记, 抹掉白天的闲时随笔; 与 idle_reflect 无文件锁并发写**
`organs/reflection.py:122-125` vs `organs/reflection.py:87-93`
- `idle_reflect` 白天向 `journal/{day}.md` append 多条“闲时随笔”; 22:00 定时器跑 `run()` 时 `open(path,"w")` 把当天随笔**全部覆盖删除**; 两个进程同时写同一文件还会交错损坏。
- 修复: `run()` 改为 append/合并(先读旧内容再写), 或换文件名(`{day}-summary.md`); 引入文件锁(如 `fcntl.flock`)。

---

## P2(一般逻辑/健壮性)

**P2-1 `_strip_thinking` 仍有思考泄漏回退路径 + 英文正则可能误伤正常回复**(重点复核项)
`llm_client.py:31-41`
- `:41` `out.strip() or text.strip()`: 若正文整段都是英文思考(如 `Let me analyze...\nThe setup:...` 无正式内容), 剥离后为空则**原样返回原文**——正是本次“刚修复”的 P0 泄漏路径的残留缺口。
- 三个 pattern 都是只剥“行首英文叙述”: 对中文回复基本不触发, 但遇到正常英文回答(如以 `The user wants...`/`Okay, ...\n` 开头的真实内容)会被误伤删掉真实首句; 建议锚定更强的思考特征(如匹配到推理句式且后面紧跟明显正文才剥), 空结果时返回占位错误而非原文。
- 结论: 对中文人格语境误伤概率低, 但“剥空回退原文”应视为必须修。

**P2-2 反思摘要读错字段: 历史条目存 `content`, 代码读 `reply` → 日记缺失“诺诺说了什么”**
`organs/reflection.py:47-48`(dialogue 写入端 `organs/dialogue.py:69-78` 用 `role/content`)— 改为 `h.get('content')` 并区分 role。

**P2-3 kv_append 客户端重试非幂等 → State 抖动时对话/outbox 重复**
`state/server.py:295-315`(`_req` 失败重连重发一次)— `emit` 有 sig_id 幂等, `kv_append` 没有。修复: 为写请求带幂等 key 或 append 支持 `client_msg_id` 去重。

**P2-4 `_decay` worker 无异常兜底: 一旦 `commit` 抛错(见 P1-5), 衰减线程静默死亡, 信号清理永久停摆**
`state/server.py:234-241` — 加 try/except 与连续失败计数。

**P2-5 心跳插件启动即连 State, 连不上直接退出(无初始重试), 且无 manifest 免疫托管时无守护**
`plugins/heartbeat/main.py:16`; 且 State 恢复后该子进程不会自动复活。修复: 启动期也走重连循环。

**P2-6 意识核“每小时GC清理空kv”是死代码**
`core/loop.py:99-101` 只更新 `last_gc`, 从未执行清理 → kv 无清理机制, 与 P1-6 叠加膨胀。

**P2-7 画布 SSE 订阅者泄漏: 断连后队列永不回收, 每次推送都往死队列积压**
`canvas/canvas_server.py:88-94`、`:144`(清理只发生在 `put_nowait` 抛异常时, 而无界 Queue 永不抛) → 用 `finally` 在 generator 结束移除队列。

**P2-8 画布 `/pages`/`/images` 路径未做白名单归一化, 前端 text/html 内容未转义直插 innerHTML**
`canvas/canvas_server.py:126-134`、模板 `:211-221` — `name` 参数与 `item.text` 均未校验/转义; 虽 P0-1 已覆盖文件读取, 仍建议统一收紧(路径 resolve 后必须位于 pages/images 下)。

**P2-9 会话失效后微信器官只暂停 600s 重试旧凭据, 永不重新扫码**
`organs/wechat_loop.py:854-861` — 若官方不自动恢复, 每 10 分钟空转一次直到人工干预; 建议进入“待重新登录”状态并告警/重启时自动 `_login_async`。

**P2-10 wechat_loop 用同步阻塞 StateClient 于 asyncio 事件循环内**
`organs/wechat_loop.py:664-677`、`:764`、`:802-808` — State 抖动时(重连指数退避最坏 ~8s×5+10s 超时) inbox/outbox 双双冻结, 长轮询暂停; 建议换线程池或 asyncio 版 socket 客户端。

**P2-11 “自动认主=第一个私聊联系人”的隐私风险**
`organs/wechat_loop.py:669-677` — 若首个 DM 是陌生人(垃圾/误触), 该陌生人成为 `wechat/owner`, 之后所有 “to=owner” 的回复默认发给此人。至少应要求 `--owner` 显式配置, 或首条消息需在 State 记录待确认。

**P2-12 其他小项**
- `immune/router.py:22-24` 关键词 `"快"` 会命中“尽快/快乐/快递”等普通词并升优先级(priority=1、decay≈0), 建议改词边界/整词匹配。
- `organs/dialogue.py:27-29` 画布分支只要含“骑/车/鹈鹕”就强行改题“鹈鹕骑车”, “画一辆车”会被劫持。
- `tools/gen_image.py:13-23` 参考图先整读再 base64 编解码绕一圈、无 context manager、无异常处理; 大图内存峰值翻倍。
- `tools/nono_cliproxy_proxy.py:14-16` 按 `Content-Length` 无上限整读入内存(仅本机 18901, 低危); 建议限长。
- `organs/wechat_loop.py:808` 两实例并存(免疫托管 + 手工)会双发同一 outbox —— 与 P1-3 的 CAS 修复一并解决。
- `core/loop.py:86-91` 若 except 分支里 `emit(loop_error)` 再失败, 原信号未被 drop, 下轮会重复执行器官副作用(重复 LLM 调用); 建议 drop 放最前或 try/finally。
- `state/server.py:37` 启动即读 `MAX(revision)`, 与 `_migrate_history` 的 `_rev += 1` 顺序无碍但无锁, 低危。
- `immune/plugins.py:70-84` 连续重启 3 次“放弃”后插件已卸载, 但不会向主人/State 告警, 白细胞失效静默。

---

## 重点复核结论

- **llm_client `_strip_thinking`**: 主路径剥英文行首思考可用, 但 `:41` 的空结果回退仍保留思考泄漏路径(P2-1, 建议优先修); 对中文正常回复误伤概率低。
- **dialogue 画布分支**: 主流程(生成 HTML→存文件→推 canvas)闭环正确; 主要问题是 540s 同步阻塞意识核(P1-2)、主题劫持启发式(P2-12)、以及画布服务器端无鉴权使其“创作输出”与任意文件读取混在同一端口(P0-1)。
- **wechat context_token 管理**: 按 target(room/私聊 wxid)存取、会话失效时摘除并免 token 重试一次, 逻辑基本自洽; 真正的并发缺口在 outbox 覆写(P1-3)与双实例双发(P2-12), token 本身无竞态(均在单线程事件循环内变更)。

建议修复顺序: P0-1/P0-2 → P1-1/P1-2 → P1-3/P1-4 → P1-5 → 其余。
tokens used
126,892
# nono-mind 代码审查报告

审查范围:README/wikis架构(v7三明治) + state/model.py、state/server.py、core/loop.py、organs/(dialogue、reflection、wechat_loop)、immune/router.py、immune/plugins.py、llm_client.py、tools/(gen_image、canvas_push、nono_dsh、nono_cliproxy_proxy)、canvas/canvas_server.py、plugins/heartbeat、deploy/*。全程只读, 未修改文件。运行态取证: 当前 `state.db` signals 表中 20 行全部 `strength=0.15`(见 P1-1 证据)。

---

## P0(崩溃/数据损坏/安全)

**P0-1 画布服务裸奔在 0.0.0.0, 可未授权读取任意本地文件 + 伪造主人消息**
`canvas/canvas_server.py:38` `/push`、`canvas/canvas_server.py:104` `/action`、`canvas/canvas_server.py:266` `uvicorn.run(host="0.0.0.0")`
- `/push {"type":"page","path":"/root/nono-mind/weixin/account.json","name":"x"}`(或 `type=image`, 代码 `:61-67`、`:48-54`)会把**服务端进程可读的任意文件**(bot_token、state.db 全量对话、wiki)复制进 `pages/images` 目录, 然后经 `/pages/x.html`、`/events`(`:136` 回显 URL)读走——纯未授权任意文件披露。
- `/action`(`:118-119`)把 `[画布] {action}: {value}` 以 `master_message` 直投 State, 无任何鉴权/来源绑定 → 任意网络用户可冒充主人给意识核喂话(污染对话历史、消耗 LLM 额度)。
- 模块文档明确该服务经 `<YOUR-TAILSCALE-IP>:8765` 供手机访问, 即**按设计暴露在网络上**。
- 修复: `/push`/`/action`/`/clear` 加共享密钥(与画布页 cookie)校验; 限制 `path` 只能落在 `canvas/` 白名单目录(拒绝绝对路径/`..`); `name` 做白名单字符校验; 若需局域网访问至少绑定内网 IP + 前置鉴权, 不要把服务端任意文件复制逻辑暴露给客户端指定路径。

**P0-2 付费 API Key 明文硬编码并已进 git 历史(远端为 GitHub)**
`organs/dialogue.py:36`、`tools/gen_image.py:7`(同一 `sk-65745f...`, git 已确认自 commit `3a1e7cc` 起存在于 `organs/dialogue.py`), 仓库 remote 为 `git@github.com:WhiteEmperor-V/nono-mind.git`。
- 若仓库公开, 该 key = 直接泄露, 可被他人盗用刷额度/记账; 即便私有也存在历史泄露与误推风险。
- 修复: 立即到 apikey.fun 吊销该 key 并轮换; key 移入 `/root/.hermes/config.yaml`(llm_client 已采用该模式)或环境变量, 并补 `.gitignore`。

---

## P1(严重逻辑/并发/数据丢失)

**P1-1 信号生命周期自相矛盾: P0 主人消息 ~70s 后不可见、~103s 被物理删除; P1/P2/P3 信号被保底机制永久卡在 0.15 永不回收**
`state/server.py:130` `_decay()`、`state/server.py:136-140`、`state/model.py:33-36` `DECAY_MIN=0.01`
- P0 信号 `decay` 被钳到 0.01/0.5s tick: 强度 1.0 → <0.25(查询阈值)约 69s, →<0.125(删除阈值)约 103s。而注释声称“P0 有 24h TTL”(`:132`)。意识核只要阻塞或重启 >2 分钟, 期间到达的主人消息被 `_decay` 直接删除, **永远丢失**——与 `P0_TTL=24h` 的设计契约完全矛盾。
- `state/server.py:136-140` 对 `task_done/loop_progress/heartbeat/loop_error` 每 tick 复活到 `threshold*0.6=0.15`, 而 `signals` 查询阈值是 `>0.25`(`:202-204`)→ 复活线低于可见线, 这些信号既不会被意识核拾取, 又永远高于删除线 0.125, **只增不减**。DB 取证: signals 表现在 20 行全部恰好 `0.15`, 零可见行——已确认“幽灵行”机制在真实运行。
- 附带:`loop_error`(P1)被同样卡死 → “reflection 重试 3 次”机制实际收不到任何衰减后的错误信号。
- 修复: 删除“复活”UPDATE(或把复活线提到 `> threshold`); P0 改用真正“只受 TTL/主动 drop 约束、豁免衰减删除”的处理; 给 `_decay` 增加过期与清理策略(如信号数上限 + 按 `created` 兜底删除)。

**P1-2 意识核单线程阻塞式 LLM 调用, 无抢占、无并行; 最坏单次卡死 ~9 分钟并连带 P1-1 丢消息**
`core/loop.py:63-91`、`organs/dialogue.py:40` `timeout=540`、`llm_client.py:13` `timeout=120`、`llm_client.py:51-53`(降级再 120s)
- 画布创作一次最长阻塞 540s, 期间新 master_message/risk 全部无法处理, 队列里超过 ~103s 的 P0 又被 P1-1 删除。对话 LLM 超时+降级最坏 ~241s。设计文档里的“P0 抢占”在单线程+同步网络 IO 下并不成立。
- 修复: LLM 调用移出主循环(子进程/线程池 + 超时), 或至少给信号等待队列做“P0 插队”与不丢策略; canvas 请求应异步化, 主循环只做分派。

**P1-3 微信 outbox 消费是“读-发-整表覆写”, 无 CAS; 并发追加可被覆盖丢失, 失败项每秒无限重试**
`organs/wechat_loop.py:760-812`
- `:802-808` 先再读一次列表取 tail 再 `kv_set` 整体覆写——第二次读与覆写之间若有写入者 `kv_append`, 该条被静默丢弃(State 明明提供了 `kv_set(expected_revision=...)` CAS 却没用)。
- `:808` 覆写失败时整条队列(含已发出的)原样保留 → 下一轮 1s 后**全部重发**, 主人收到重复消息。
- 失败项(`:796-800` failed 列表)每次 drain 都重试, drain 每 1s 一次; 对失效会话/无效接收者会以 ~1-2 次/s 的节奏永久锤 iLink(无失败计数、无退避、无上限), 与 `_send_text` 的多分片重试叠加还会产生“前几片已发、后片失败→下轮整条重发”的部分重复。
- 修复: 消费改为 CAS 循环(读 revision → 发送 → `kv_set(remaining, expected_revision=旧rev)`, 冲突则重读); failed 项记重试次数/时间戳, 退避并设置丢弃上限; 会话失效(-14)时暂停整个 outbox 而非每秒重试。

**P1-4 getupdates 消息先持久化 sync_buf、后处理消息 → 崩溃/路由失败即永久丢消息**
`organs/wechat_loop.py:836-838`、`organs/wechat_loop.py:696-700`
- `_save_sync_buf()` 在 `_process_msgs()` **之前**执行: 进程在处理中途崩溃, 重启后用已推进的 buf 拉不到这批消息 → 永久丢失。
- 即使不崩溃, `:699` 路由失败(如 State 瞬时不可用)也只是 log 后丢弃, 消息已 ack 无法重取。
- 修复: 先完整处理并落“处理游标”, 再持久化新 buf; 路由失败的消息要进本地待重投队列/挂起区, 而不是直接丢弃。

**P1-5 State 服务单条 sqlite 连接被多线程共享: 读不持锁, checkpoint backup 与并发读同连接交叠**
`state/server.py:27`、`state/server.py:195-206`(kv_get/kv_list/signals 无 `_wlock`)、`state/server.py:108-121`(`self.conn.backup()`)、`state/server.py:234-241`(worker 写)
- 设计上“唯一写者”只串行了写, 读与写/backup 仍在同一 `sqlite3.Connection` 上并发(仅 `check_same_thread=False` 放行)。SQLite 连接对象不支持多线程同时使用, 会出现偶发 `OperationalError: database is locked` / “cannot commit transaction - SQL statements in progress”; backup 期间同连接上有其他语句执行属于文档明示的 misuse, 快照/读取会随机失败。
- 修复: 每个 `_handle` 线程(及 worker)各自打开独立只读连接(WAL 天然多读); 写连接与 backup 源分离(如 `VACUUM INTO` 或独立连接做 `sqlite3 backup`)。

**P1-6 dialogue_history 单 key 无上限增长, 越过 1MB 响应上限后对话器官整体失效**
`organs/dialogue.py:68-78`、`state/server.py:198-200`(`kv_list` 无上限)、`state/server.py:13`(`MAX_MSG=1MB`)
- 每天历史是一个无限增长的 JSON 数组, 每轮对话 `kv_append` 都要整表读改写(O(n)); 当单日数组序列化超过 ~1MB(约数千条), `kv_get`/`kv_list` 响应触发客户端 `响应超过1MB上限`(`state/server.py:304`), dialogue/reflection 每轮必炸。当前 09-08 分片已 24 条/4.6KB, 增速约 200B/条。
- 修复: 对话历史按消息条数/字节数滚动裁剪(如每日只留最近 200 条), 或改为多行存多条记录 + 分页读取; `kv_list` 服务端设字节/条数上限。

**P1-7 日反思 run() 用 “w” 覆写当天日记, 抹掉白天的闲时随笔; 与 idle_reflect 无文件锁并发写**
`organs/reflection.py:122-125` vs `organs/reflection.py:87-93`
- `idle_reflect` 白天向 `journal/{day}.md` append 多条“闲时随笔”; 22:00 定时器跑 `run()` 时 `open(path,"w")` 把当天随笔**全部覆盖删除**; 两个进程同时写同一文件还会交错损坏。
- 修复: `run()` 改为 append/合并(先读旧内容再写), 或换文件名(`{day}-summary.md`); 引入文件锁(如 `fcntl.flock`)。

---

## P2(一般逻辑/健壮性)

**P2-1 `_strip_thinking` 仍有思考泄漏回退路径 + 英文正则可能误伤正常回复**(重点复核项)
`llm_client.py:31-41`
- `:41` `out.strip() or text.strip()`: 若正文整段都是英文思考(如 `Let me analyze...\nThe setup:...` 无正式内容), 剥离后为空则**原样返回原文**——正是本次“刚修复”的 P0 泄漏路径的残留缺口。
- 三个 pattern 都是只剥“行首英文叙述”: 对中文回复基本不触发, 但遇到正常英文回答(如以 `The user wants...`/`Okay, ...\n` 开头的真实内容)会被误伤删掉真实首句; 建议锚定更强的思考特征(如匹配到推理句式且后面紧跟明显正文才剥), 空结果时返回占位错误而非原文。
- 结论: 对中文人格语境误伤概率低, 但“剥空回退原文”应视为必须修。

**P2-2 反思摘要读错字段: 历史条目存 `content`, 代码读 `reply` → 日记缺失“诺诺说了什么”**
`organs/reflection.py:47-48`(dialogue 写入端 `organs/dialogue.py:69-78` 用 `role/content`)— 改为 `h.get('content')` 并区分 role。

**P2-3 kv_append 客户端重试非幂等 → State 抖动时对话/outbox 重复**
`state/server.py:295-315`(`_req` 失败重连重发一次)— `emit` 有 sig_id 幂等, `kv_append` 没有。修复: 为写请求带幂等 key 或 append 支持 `client_msg_id` 去重。

**P2-4 `_decay` worker 无异常兜底: 一旦 `commit` 抛错(见 P1-5), 衰减线程静默死亡, 信号清理永久停摆**
`state/server.py:234-241` — 加 try/except 与连续失败计数。

**P2-5 心跳插件启动即连 State, 连不上直接退出(无初始重试), 且无 manifest 免疫托管时无守护**
`plugins/heartbeat/main.py:16`; 且 State 恢复后该子进程不会自动复活。修复: 启动期也走重连循环。

**P2-6 意识核“每小时GC清理空kv”是死代码**
`core/loop.py:99-101` 只更新 `last_gc`, 从未执行清理 → kv 无清理机制, 与 P1-6 叠加膨胀。

**P2-7 画布 SSE 订阅者泄漏: 断连后队列永不回收, 每次推送都往死队列积压**
`canvas/canvas_server.py:88-94`、`:144`(清理只发生在 `put_nowait` 抛异常时, 而无界 Queue 永不抛) → 用 `finally` 在 generator 结束移除队列。

**P2-8 画布 `/pages`/`/images` 路径未做白名单归一化, 前端 text/html 内容未转义直插 innerHTML**
`canvas/canvas_server.py:126-134`、模板 `:211-221` — `name` 参数与 `item.text` 均未校验/转义; 虽 P0-1 已覆盖文件读取, 仍建议统一收紧(路径 resolve 后必须位于 pages/images 下)。

**P2-9 会话失效后微信器官只暂停 600s 重试旧凭据, 永不重新扫码**
`organs/wechat_loop.py:854-861` — 若官方不自动恢复, 每 10 分钟空转一次直到人工干预; 建议进入“待重新登录”状态并告警/重启时自动 `_login_async`。

**P2-10 wechat_loop 用同步阻塞 StateClient 于 asyncio 事件循环内**
`organs/wechat_loop.py:664-677`、`:764`、`:802-808` — State 抖动时(重连指数退避最坏 ~8s×5+10s 超时) inbox/outbox 双双冻结, 长轮询暂停; 建议换线程池或 asyncio 版 socket 客户端。

**P2-11 “自动认主=第一个私聊联系人”的隐私风险**
`organs/wechat_loop.py:669-677` — 若首个 DM 是陌生人(垃圾/误触), 该陌生人成为 `wechat/owner`, 之后所有 “to=owner” 的回复默认发给此人。至少应要求 `--owner` 显式配置, 或首条消息需在 State 记录待确认。

**P2-12 其他小项**
- `immune/router.py:22-24` 关键词 `"快"` 会命中“尽快/快乐/快递”等普通词并升优先级(priority=1、decay≈0), 建议改词边界/整词匹配。
- `organs/dialogue.py:27-29` 画布分支只要含“骑/车/鹈鹕”就强行改题“鹈鹕骑车”, “画一辆车”会被劫持。
- `tools/gen_image.py:13-23` 参考图先整读再 base64 编解码绕一圈、无 context manager、无异常处理; 大图内存峰值翻倍。
- `tools/nono_cliproxy_proxy.py:14-16` 按 `Content-Length` 无上限整读入内存(仅本机 18901, 低危); 建议限长。
- `organs/wechat_loop.py:808` 两实例并存(免疫托管 + 手工)会双发同一 outbox —— 与 P1-3 的 CAS 修复一并解决。
- `core/loop.py:86-91` 若 except 分支里 `emit(loop_error)` 再失败, 原信号未被 drop, 下轮会重复执行器官副作用(重复 LLM 调用); 建议 drop 放最前或 try/finally。
- `state/server.py:37` 启动即读 `MAX(revision)`, 与 `_migrate_history` 的 `_rev += 1` 顺序无碍但无锁, 低危。
- `immune/plugins.py:70-84` 连续重启 3 次“放弃”后插件已卸载, 但不会向主人/State 告警, 白细胞失效静默。

---

## 重点复核结论

- **llm_client `_strip_thinking`**: 主路径剥英文行首思考可用, 但 `:41` 的空结果回退仍保留思考泄漏路径(P2-1, 建议优先修); 对中文正常回复误伤概率低。
- **dialogue 画布分支**: 主流程(生成 HTML→存文件→推 canvas)闭环正确; 主要问题是 540s 同步阻塞意识核(P1-2)、主题劫持启发式(P2-12)、以及画布服务器端无鉴权使其“创作输出”与任意文件读取混在同一端口(P0-1)。
- **wechat context_token 管理**: 按 target(room/私聊 wxid)存取、会话失效时摘除并免 token 重试一次, 逻辑基本自洽; 真正的并发缺口在 outbox 覆写(P1-3)与双实例双发(P2-12), token 本身无竞态(均在单线程事件循环内变更)。

建议修复顺序: P0-1/P0-2 → P1-1/P1-2 → P1-3/P1-4 → P1-5 → 其余。
