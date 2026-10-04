138:            "UPDATE signals SET strength = MAX(strength, ?) WHERE sig_type IN ('task_done','loop_progress','heartbeat','loop_error') AND strength < ?",

exec
/bin/bash -lc "ls -la /root/nono-mind/docs/ && find /root/nono-mind -name AGENTS.md -o -name '*.md' | xargs ls -la 2>/dev/null | head && head -80 /root/nono-mind/docs/CODE_REVIEW_20260909.md 2>/dev/null" in /root/nono-mind
 succeeded in 0ms:
total 756
drwxr-xr-x  2 root root   4096 Sep  8 23:23 .
drwxr-xr-x 22 root root   4096 Sep  9 00:00 ..
-rw-r--r--  1 root root  27928 Sep  8 23:23 CODE_REVIEW_20260909.md
-rw-r--r--  1 root root 726587 Sep  6 19:22 architecture-diagram.html
-rw-r--r--  1 root root   5204 Sep  6 19:22 architecture-diagram.json
-rw-r--r-- 1 root root 16396 Sep  6 15:55 /root/nono-mind/.agents/skills/archify/SKILL.md
-rw-r--r-- 1 root root  4543 Sep  6 15:55 /root/nono-mind/.agents/skills/archify/THIRD_PARTY_NOTICES.md
-rw-r--r-- 1 root root  1449 Sep  6 15:55 /root/nono-mind/.agents/skills/archify/brand-marks/README.md
-rw-r--r-- 1 root root 11309 Sep  6 15:55 /root/nono-mind/.agents/skills/archify/references/authoring-contract.md
-rw-r--r-- 1 root root  2179 Sep  6 15:55 /root/nono-mind/.agents/skills/archify/references/brand-marks.md
-rw-r--r-- 1 root root  8604 Sep  6 15:55 /root/nono-mind/.agents/skills/archify/references/delivery-contract.md
-rw-r--r-- 1 root root  4243 Sep  6 15:55 /root/nono-mind/.agents/skills/archify/references/viewer-runtime.md
-rw-r--r-- 1 root root  4202 Sep  6 15:55 /root/nono-mind/.agents/skills/archify/renderers/dataflow/README.md
-rw-r--r-- 1 root root  5197 Sep  6 15:55 /root/nono-mind/.agents/skills/archify/renderers/lifecycle/README.md
-rw-r--r-- 1 root root  4828 Sep  6 15:55 /root/nono-mind/.agents/skills/archify/renderers/sequence/README.md
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

exec
/bin/bash -lc "cd /root/nono-mind && grep -n \"自己的消息回显\\|from_user_id == self.account_id\\|def _handle_inbound\\|content_key\\|def _send_text\\|def _drain_outbox\\|def _default_target\\|def _outbox_loop\" organs/wechat_loop.py" in /root/nono-mind
 succeeded in 0ms:
643:    def _handle_inbound(self, msg: dict) -> dict | None:
648:        if from_user_id == self.account_id:
649:            return None  # 自己的消息回显
658:            content_key = f"content:{from_user_id}:{hashlib.md5(text.encode('utf-8')).hexdigest()}"
659:            if self._dedup.is_duplicate(content_key):
710:    async def _send_text(self, to: str, text: str, context_token=None) -> list:
748:    def _default_target(self) -> str:
760:    async def _drain_outbox(self) -> dict:
871:    async def _outbox_loop(self) -> None:

codex
复审完成（全程只读，未修改任何文件）。审查基于 `dfaf6f7` 全量 diff + 运行链路上的 `core/loop.py`、`state/server.py`、`organs/wechat_loop.py` 交叉核对，并实测了自我认知文本长度与异常注入（构造脏 State 值复现崩溃路径）。

**总体结论**
- 未发现 P0（无安全/数据损坏/必崩路径）。
- 核心链路闭环正确：dialogue→`task_forward`→router→reflection 的数据包装逐层对称，**不存在 dialogue↔reflection 直接互转死循环**（reflection 只写 outbox；微信自回声已被 `organs/wechat_loop.py:648` 过滤）。
- 主要风险集中在：转发执行失败时**静默丢请求/丢结果**（P1）、outbox 消费的**丢失窗口**（P1）、状态快照**类型转换不健壮 + 每轮成本膨胀**（P2）、画布 LLM 输出清理**不健壮**（P2）。

**P1（严重：功能承诺断裂 / 静默丢数据）**

**P1-1 自检/转达任务执行失败时被静默丢弃，主人只收到“有结果了告诉你”的空头承诺**
`organs/reflection.py:249-253`（handle 无自兜底）、`organs/reflection.py:160-167`、`organs/dialogue.py:184-185`（ack 文案承诺结果）、`core/loop.py:83-91`
- 问题：reflection 处理 `task_forward` 的整段代码没有任何 try/except。一旦 `self_check()`/`_absorb_task()` 中任何未防护异常抛出（如 `organs/reflection.py:103` 的 `float(last.get("at"))` 遇到脏值、未知 op），core loop 的每信号隔离（`core/loop.py:86-90`）只负责把它转成 `loop_error` 回注 reflection——而 reflection 对 `loop_error` 走 `organs/reflection.py:257-258` 的 else 分支“吸收打印”，**原 task_forward 在第 91 行被 drop**。结果：主人已经收到“收到~…有结果了告诉你”，实际既没结果也没失败通知，对话历史里也只留了 ack。
- 修复：`handle()` 对 `task_forward` 分支整体 try/except，异常时 `_push_outbox` 发一条“自检执行失败+原因”的兜底消息并写失败态 `reflection/last_self_check`；`self_check` 里所有 `time.localtime(float(...))` 加数字类型防护（见 P2-1）。

**P1-2 outbox 消费是“读快照→发送→再读尾→整体 kv_set”，新增的 reflection 写入存在丢失窗口**
`organs/wechat_loop.py:802-808`、写入方 `organs/reflection.py:25-30`（及既有的 `core/loop.py:77`）
- 问题：`kv_append` 本身原子（`state/server.py:65-75` 单写锁串行），所以“追加 vs 追加”安全；危险在消费侧：`_drain_outbox` 在第 802 行重读后、第 808 行 `kv_set(failed+tail)` 之前，若 `_push_outbox` 的 append 恰好落盘，会被整体回写**覆盖丢失**，且无任何日志。自检报告正是主人显式请求的高价值消息，丢一次=永久丢（此前 review 文档 P1-3 已对该消费端提过 CAS 修复，本次新增写入方后该窗口仍是开放的）。
- 修复：消费改为按 revision CAS 循环（`state/server.py:76-82` 已支持 `expected_revision`，但 `kv_get` 未回传 revision，需补一个带 revision 的读或服务端 `consume` 原语），冲突即重读重试；或每条 outbox 独立 key + 消费后删 key，避免整体数组读改写。

**P2（一般健壮性 / 成本）**

**P2-1 状态快照类型转换不受保护，且整段建 prompt 不在 LLM 降级 try 内——脏 State 值会让一条主人消息“无回复”**
`organs/dialogue.py:209`、`organs/dialogue.py:235`、`organs/dialogue.py:243`、`organs/dialogue.py:300-308`、`organs/reflection.py:103`
- 问题：`_status_snapshot` 里 `float(last.get("at"))`、`float(x.get("created"))` 都在各自的 try/except **之外**（实测注入 `{"at":"not-a-number"}` 或 `created:"yesterday"` 即抛 `ValueError`，见对话文件 209/243 行）。这些值由本仓 kv/信号写入时是数值，但 State 是跨版本共享的，任何一次脏写入/旧格式都会让 `_build_self_awareness` 在 `organs/dialogue.py:300` 处抛错——而该行在 `chat(...)` 的 try（303-308）之外，降级文案不会触发。
- 对“意识核会崩吗”的直接回答：**不会崩核**（`core/loop.py:86-90` 每信号隔离捕获），但该条 master_message 被 `core/loop.py:91` drop，主人收不到任何回复，且产生一条只会被 reflection 吞掉的 `loop_error`。
- 修复：给数值字段加 `isinstance(x,(int,float))` 防护或抽 `_fmt_time()` 帮助函数；把 `organs/dialogue.py:300-302` 的 system 构建与历史读取也挪进 303 的 try/except 里（让降级文案真正覆盖新代码）。

**P2-2 画布 HTML 清理只处理“行首一个 ```”，且不做 `</html>`/DOCTYPE 校验——可能把废文本当成功画作**
`organs/dialogue.py:96-100`
- 问题：`startswith("```") → split("```")[1]` 只对“正文以单组 fence 开头”的情形有效：① LLM 若先输出“好的，我来画…”再包 fence（指令违规常见漂移），整段 Markdown 前缀被当 HTML 落盘并推画布，回复却声称“画好了”（`organs/dialogue.py:128-129` 只按 push 成功判断）；② 内容里出现第二组 ```（如 `<pre>` 示例）会被 `split` 截断；③ `html` 语言标记大小写敏感（`html.startswith("html")`），模型输出 ```HTML 时残留 “HTML\n” 头；④ 没有校验 `<!DOCTYPE html>`/`</html>`、无长度上限。
- 修复：改为“去首尾 fence 行”的剥壳（`re.sub(r"^```[a-zA-Z]*\s*\n|\n```\s*$", ...)`），剥壳后必须校验 `html.lstrip().lower().startswith("<!doctype html") and "</html>" in html`，校验不过按失败走 `record`/降级文案（复用 103-107 的失败路径），不要标 `ok=True`。

**P2-3 每普通消息 6 次读 + 2 次写串行 RTT；State 卡顿时每条消息可停顿数十秒到分钟级（无连接泄漏）**
`organs/dialogue.py:278`、`organs/dialogue.py:204/217/231/238`、`organs/dialogue.py:301`、`organs/dialogue.py:109`（`_remember` 两次 append）
- 问题：一次普通对话 = 健康上报 1 写 + 快照 4 读 + 历史 1 读 + 记忆 2 写 ≈ **8 次串行 socket 往返**（旧版约 3 次）。`StateClient` 复用单连接、断线才重连，**没有连接泄漏**；但每 op 超时 10s+重连一次重试（`state/server.py:280-297`），State 服务抖动/重启时最坏可达每 op ~30s，6 个读全失败也要 ~1.5–3 分钟才轮到 LLM 回复。画布生成还会同步阻塞整个意识核（540s 超时，`organs/dialogue.py:90`），期间的 task_forward 会同步衰减（见 P3-1）。
- 修复：把 4 个快照读合并为一次服务端 `status_snapshot` op（或并行连接），并给 handle 的“建 prompt 前状态段”加总超时；确认 canvas 生成是否应移到独立进程/线程，避免阻塞信号分派。

**P2-4 每轮普通对话注入 ~700–1200 token 的动态 SYSTEM，且每轮内容都变、无法命中缓存**
`organs/dialogue.py:300`、`organs/dialogue.py:252-267`、`organs/dialogue.py:241-246`
- 问题：实测 SYSTEM 文本长度：PERSONA 79 字符 + 自我认知/快照空状态约 692 字符、有状态（画布记录+健康+上次自检+10 条信号）约 965 字符，即每普通轮约 **700–1200 token**，是旧 SYSTEM（~80 字符）的 ~10 倍；按一天 200 轮估算增量 15–25 万 token/天。且快照里的信号时间戳逐秒刷新（243-246），整段 SYSTEM 每轮字节级变化，长上下文缓存基本失效。另：快照里 heartbeat/loop_progress 类的内部噪音也在稀释注意力。
- 修复：静态“身份/兄弟/能力/边界”文本进程内缓存、只在变更时重建；动态快照只在用户问状态类问题时注入，或降频到 ≥N 秒/条；信号行按类型聚合计数（如 `heartbeat×6`）而不是逐条列时间戳。

**P2-5 自检/吸收结果硬编码投 wechat_outbox，丢失来源渠道与 room 上下文**
`organs/reflection.py:139`、`organs/reflection.py:156`、`organs/dialogue.py:172-173`
- 问题：`_forward_task` 的 payload 只带 `origin_text`，没有来源渠道/room/message_id；reflection 结果一律 `kv_append("wechat_outbox", {"to":"owner"})`。① 通过终端/nono_dsh 等非微信通道发起自检时，主人永远只在“微信 owner 的私聊”收到结果（若微信未登录则积压在 outbox）；② 群聊里发起自检，报告走 owner 私聊而非原群。
- 修复：转发 payload 透传来源（`from_wechat`/`room`/`sender`/`channel`），reflection 投递时回填原目标；至少把“微信 owner”作为缺省而非唯一出口。

**P3（观察项/维护风险，建议顺手修）**

- **P3-1 跨文件一致性隐患（对第 5 问的补充）**：发送端/router/接收端三段的 data 包装与 `target/self_check/origin_text` 字段名目前完全对称、优先级一致（`immune/router.py:18` vs `state/model.py:12`），**主链路无问题**。但三处边缘不一致：① `state/server.py:138` 的“防饿死保底”列表不含 `task_forward`——优先级同为 1 的 `loop_error` 被保底而它没有，等待超 ~14s 就低于 `signals` 可见线（`state/server.py:202-204`），约 20s 静默删除，意识核阻塞时（如画布 540s 生成）转发的自检会蒸发；② `organs/reflection.py:180-186` `sweep_suspended` 的类型重投映射不认识 `task_forward`，一旦未来某 target 落入挂起区会被投成 `state_only` 死信；③ `immune/router.py:22-27` `TASK_TARGET_ROUTE` 目前三个 target 全指向 reflection，动态路由实际是 no-op，注释描述的“按 target 路由到对应器官”与实现不符，未来新增 target 时容易漏改上述两处。
- **P3-2 正则误报转发**：`SELF_CHECK_RE`（`organs/dialogue.py:32-34`）把日常用语“帮我修复/调试一下这段 XX”也当系统自检转发；自检在 `_classify_forward`（`organs/dialogue.py:161-162`）之前无上下文/意图判断，建议加“针对系统/器官”的限定并给 3 类意图示例。
- **P3-3 细节卫生**：`organs/dialogue.py:86` `json.load(open(...))` 未 close 且硬编码绝对路径；写盘中途失败（磁盘满）会留下无记录的无主 `canvas/pages/creation_<ts>.html`（`organs/dialogue.py:111-119`），建议失败时清理半成品；`organs/reflection.py:135` 用 ping 结果覆盖器官健康“ok”，单次瞬时抖动会把 reflection 标成异常。
- **P3-4 自检结果不留明细**：`LAST_SELF_CHECK_KV`（`organs/reflection.py:133-134`）只存 ok/计数，主人追问“具体发现了什么问题”时快照（`organs/dialogue.py:234-236`）无内容可答，只能泛泛而谈，建议把 `problems[:3]` 摘要一并落 State。

**六个重点检查点速答**
1. 自我认知每消息多次 State：无连接泄漏（单 socket 复用）；但 8 次串行 RTT + 快照内 3 处裸 `float()` 转换未受保护，脏值会让该条消息无回复——意识核不会崩（每信号隔离兜底）。→ P2-1、P2-3
2. task_forward：reflection 崩溃被 core 兜底转 `loop_error` 后吸收，原请求被 drop、主人无结果无失败通知；无 dialogue↔reflection 互转死循环（reflection 只写 outbox，微信自回声有过滤 `wechat_loop.py:648-649`）。→ P1-1
3. `_canvas_create`：落盘/推送失败路径有状态留痕+降级文案（做得好）；薄弱点在 LLM 输出剥壳与 `</html>` 校验，脏文本会被当成功画作。→ P2-2
4. outbox 追加：append 原子、追加间安全；风险在 wechat 消费端整体回写的丢失窗口。→ P1-2
5. task_forward 跨文件：主链路（`data` 包装、字段名、优先级）一致闭环；不一致都在保底/重投/动态路由三处边缘。→ P3-1
6. token 开销：实测每轮 SYSTEM ~700–1200 token（旧版 ~10 倍），且信号时间戳使其每轮变化、缓存不命中。→ P2-4

如需，我可以按 P1 优先给出一份最小修复 patch（不改行为只补兜底），或先把这份结论落到 `docs/` 作为复审记录。
tokens used
90,389
复审完成（全程只读，未修改任何文件）。审查基于 `dfaf6f7` 全量 diff + 运行链路上的 `core/loop.py`、`state/server.py`、`organs/wechat_loop.py` 交叉核对，并实测了自我认知文本长度与异常注入（构造脏 State 值复现崩溃路径）。

**总体结论**
- 未发现 P0（无安全/数据损坏/必崩路径）。
- 核心链路闭环正确：dialogue→`task_forward`→router→reflection 的数据包装逐层对称，**不存在 dialogue↔reflection 直接互转死循环**（reflection 只写 outbox；微信自回声已被 `organs/wechat_loop.py:648` 过滤）。
- 主要风险集中在：转发执行失败时**静默丢请求/丢结果**（P1）、outbox 消费的**丢失窗口**（P1）、状态快照**类型转换不健壮 + 每轮成本膨胀**（P2）、画布 LLM 输出清理**不健壮**（P2）。

**P1（严重：功能承诺断裂 / 静默丢数据）**

**P1-1 自检/转达任务执行失败时被静默丢弃，主人只收到“有结果了告诉你”的空头承诺**
`organs/reflection.py:249-253`（handle 无自兜底）、`organs/reflection.py:160-167`、`organs/dialogue.py:184-185`（ack 文案承诺结果）、`core/loop.py:83-91`
- 问题：reflection 处理 `task_forward` 的整段代码没有任何 try/except。一旦 `self_check()`/`_absorb_task()` 中任何未防护异常抛出（如 `organs/reflection.py:103` 的 `float(last.get("at"))` 遇到脏值、未知 op），core loop 的每信号隔离（`core/loop.py:86-90`）只负责把它转成 `loop_error` 回注 reflection——而 reflection 对 `loop_error` 走 `organs/reflection.py:257-258` 的 else 分支“吸收打印”，**原 task_forward 在第 91 行被 drop**。结果：主人已经收到“收到~…有结果了告诉你”，实际既没结果也没失败通知，对话历史里也只留了 ack。
- 修复：`handle()` 对 `task_forward` 分支整体 try/except，异常时 `_push_outbox` 发一条“自检执行失败+原因”的兜底消息并写失败态 `reflection/last_self_check`；`self_check` 里所有 `time.localtime(float(...))` 加数字类型防护（见 P2-1）。

**P1-2 outbox 消费是“读快照→发送→再读尾→整体 kv_set”，新增的 reflection 写入存在丢失窗口**
`organs/wechat_loop.py:802-808`、写入方 `organs/reflection.py:25-30`（及既有的 `core/loop.py:77`）
- 问题：`kv_append` 本身原子（`state/server.py:65-75` 单写锁串行），所以“追加 vs 追加”安全；危险在消费侧：`_drain_outbox` 在第 802 行重读后、第 808 行 `kv_set(failed+tail)` 之前，若 `_push_outbox` 的 append 恰好落盘，会被整体回写**覆盖丢失**，且无任何日志。自检报告正是主人显式请求的高价值消息，丢一次=永久丢（此前 review 文档 P1-3 已对该消费端提过 CAS 修复，本次新增写入方后该窗口仍是开放的）。
- 修复：消费改为按 revision CAS 循环（`state/server.py:76-82` 已支持 `expected_revision`，但 `kv_get` 未回传 revision，需补一个带 revision 的读或服务端 `consume` 原语），冲突即重读重试；或每条 outbox 独立 key + 消费后删 key，避免整体数组读改写。

**P2（一般健壮性 / 成本）**

**P2-1 状态快照类型转换不受保护，且整段建 prompt 不在 LLM 降级 try 内——脏 State 值会让一条主人消息“无回复”**
`organs/dialogue.py:209`、`organs/dialogue.py:235`、`organs/dialogue.py:243`、`organs/dialogue.py:300-308`、`organs/reflection.py:103`
- 问题：`_status_snapshot` 里 `float(last.get("at"))`、`float(x.get("created"))` 都在各自的 try/except **之外**（实测注入 `{"at":"not-a-number"}` 或 `created:"yesterday"` 即抛 `ValueError`，见对话文件 209/243 行）。这些值由本仓 kv/信号写入时是数值，但 State 是跨版本共享的，任何一次脏写入/旧格式都会让 `_build_self_awareness` 在 `organs/dialogue.py:300` 处抛错——而该行在 `chat(...)` 的 try（303-308）之外，降级文案不会触发。
- 对“意识核会崩吗”的直接回答：**不会崩核**（`core/loop.py:86-90` 每信号隔离捕获），但该条 master_message 被 `core/loop.py:91` drop，主人收不到任何回复，且产生一条只会被 reflection 吞掉的 `loop_error`。
- 修复：给数值字段加 `isinstance(x,(int,float))` 防护或抽 `_fmt_time()` 帮助函数；把 `organs/dialogue.py:300-302` 的 system 构建与历史读取也挪进 303 的 try/except 里（让降级文案真正覆盖新代码）。

**P2-2 画布 HTML 清理只处理“行首一个 ```”，且不做 `</html>`/DOCTYPE 校验——可能把废文本当成功画作**
`organs/dialogue.py:96-100`
- 问题：`startswith("```") → split("```")[1]` 只对“正文以单组 fence 开头”的情形有效：① LLM 若先输出“好的，我来画…”再包 fence（指令违规常见漂移），整段 Markdown 前缀被当 HTML 落盘并推画布，回复却声称“画好了”（`organs/dialogue.py:128-129` 只按 push 成功判断）；② 内容里出现第二组 ```（如 `<pre>` 示例）会被 `split` 截断；③ `html` 语言标记大小写敏感（`html.startswith("html")`），模型输出 ```HTML 时残留 “HTML\n” 头；④ 没有校验 `<!DOCTYPE html>`/`</html>`、无长度上限。
- 修复：改为“去首尾 fence 行”的剥壳（`re.sub(r"^```[a-zA-Z]*\s*\n|\n```\s*$", ...)`），剥壳后必须校验 `html.lstrip().lower().startswith("<!doctype html") and "</html>" in html`，校验不过按失败走 `record`/降级文案（复用 103-107 的失败路径），不要标 `ok=True`。

**P2-3 每普通消息 6 次读 + 2 次写串行 RTT；State 卡顿时每条消息可停顿数十秒到分钟级（无连接泄漏）**
`organs/dialogue.py:278`、`organs/dialogue.py:204/217/231/238`、`organs/dialogue.py:301`、`organs/dialogue.py:109`（`_remember` 两次 append）
- 问题：一次普通对话 = 健康上报 1 写 + 快照 4 读 + 历史 1 读 + 记忆 2 写 ≈ **8 次串行 socket 往返**（旧版约 3 次）。`StateClient` 复用单连接、断线才重连，**没有连接泄漏**；但每 op 超时 10s+重连一次重试（`state/server.py:280-297`），State 服务抖动/重启时最坏可达每 op ~30s，6 个读全失败也要 ~1.5–3 分钟才轮到 LLM 回复。画布生成还会同步阻塞整个意识核（540s 超时，`organs/dialogue.py:90`），期间的 task_forward 会同步衰减（见 P3-1）。
- 修复：把 4 个快照读合并为一次服务端 `status_snapshot` op（或并行连接），并给 handle 的“建 prompt 前状态段”加总超时；确认 canvas 生成是否应移到独立进程/线程，避免阻塞信号分派。

**P2-4 每轮普通对话注入 ~700–1200 token 的动态 SYSTEM，且每轮内容都变、无法命中缓存**
`organs/dialogue.py:300`、`organs/dialogue.py:252-267`、`organs/dialogue.py:241-246`
- 问题：实测 SYSTEM 文本长度：PERSONA 79 字符 + 自我认知/快照空状态约 692 字符、有状态（画布记录+健康+上次自检+10 条信号）约 965 字符，即每普通轮约 **700–1200 token**，是旧 SYSTEM（~80 字符）的 ~10 倍；按一天 200 轮估算增量 15–25 万 token/天。且快照里的信号时间戳逐秒刷新（243-246），整段 SYSTEM 每轮字节级变化，长上下文缓存基本失效。另：快照里 heartbeat/loop_progress 类的内部噪音也在稀释注意力。
- 修复：静态“身份/兄弟/能力/边界”文本进程内缓存、只在变更时重建；动态快照只在用户问状态类问题时注入，或降频到 ≥N 秒/条；信号行按类型聚合计数（如 `heartbeat×6`）而不是逐条列时间戳。

**P2-5 自检/吸收结果硬编码投 wechat_outbox，丢失来源渠道与 room 上下文**
`organs/reflection.py:139`、`organs/reflection.py:156`、`organs/dialogue.py:172-173`
- 问题：`_forward_task` 的 payload 只带 `origin_text`，没有来源渠道/room/message_id；reflection 结果一律 `kv_append("wechat_outbox", {"to":"owner"})`。① 通过终端/nono_dsh 等非微信通道发起自检时，主人永远只在“微信 owner 的私聊”收到结果（若微信未登录则积压在 outbox）；② 群聊里发起自检，报告走 owner 私聊而非原群。
- 修复：转发 payload 透传来源（`from_wechat`/`room`/`sender`/`channel`），reflection 投递时回填原目标；至少把“微信 owner”作为缺省而非唯一出口。

**P3（观察项/维护风险，建议顺手修）**

- **P3-1 跨文件一致性隐患（对第 5 问的补充）**：发送端/router/接收端三段的 data 包装与 `target/self_check/origin_text` 字段名目前完全对称、优先级一致（`immune/router.py:18` vs `state/model.py:12`），**主链路无问题**。但三处边缘不一致：① `state/server.py:138` 的“防饿死保底”列表不含 `task_forward`——优先级同为 1 的 `loop_error` 被保底而它没有，等待超 ~14s 就低于 `signals` 可见线（`state/server.py:202-204`），约 20s 静默删除，意识核阻塞时（如画布 540s 生成）转发的自检会蒸发；② `organs/reflection.py:180-186` `sweep_suspended` 的类型重投映射不认识 `task_forward`，一旦未来某 target 落入挂起区会被投成 `state_only` 死信；③ `immune/router.py:22-27` `TASK_TARGET_ROUTE` 目前三个 target 全指向 reflection，动态路由实际是 no-op，注释描述的“按 target 路由到对应器官”与实现不符，未来新增 target 时容易漏改上述两处。
- **P3-2 正则误报转发**：`SELF_CHECK_RE`（`organs/dialogue.py:32-34`）把日常用语“帮我修复/调试一下这段 XX”也当系统自检转发；自检在 `_classify_forward`（`organs/dialogue.py:161-162`）之前无上下文/意图判断，建议加“针对系统/器官”的限定并给 3 类意图示例。
- **P3-3 细节卫生**：`organs/dialogue.py:86` `json.load(open(...))` 未 close 且硬编码绝对路径；写盘中途失败（磁盘满）会留下无记录的无主 `canvas/pages/creation_<ts>.html`（`organs/dialogue.py:111-119`），建议失败时清理半成品；`organs/reflection.py:135` 用 ping 结果覆盖器官健康“ok”，单次瞬时抖动会把 reflection 标成异常。
- **P3-4 自检结果不留明细**：`LAST_SELF_CHECK_KV`（`organs/reflection.py:133-134`）只存 ok/计数，主人追问“具体发现了什么问题”时快照（`organs/dialogue.py:234-236`）无内容可答，只能泛泛而谈，建议把 `problems[:3]` 摘要一并落 State。

**六个重点检查点速答**
1. 自我认知每消息多次 State：无连接泄漏（单 socket 复用）；但 8 次串行 RTT + 快照内 3 处裸 `float()` 转换未受保护，脏值会让该条消息无回复——意识核不会崩（每信号隔离兜底）。→ P2-1、P2-3
2. task_forward：reflection 崩溃被 core 兜底转 `loop_error` 后吸收，原请求被 drop、主人无结果无失败通知；无 dialogue↔reflection 互转死循环（reflection 只写 outbox，微信自回声有过滤 `wechat_loop.py:648-649`）。→ P1-1
3. `_canvas_create`：落盘/推送失败路径有状态留痕+降级文案（做得好）；薄弱点在 LLM 输出剥壳与 `</html>` 校验，脏文本会被当成功画作。→ P2-2
4. outbox 追加：append 原子、追加间安全；风险在 wechat 消费端整体回写的丢失窗口。→ P1-2
5. task_forward 跨文件：主链路（`data` 包装、字段名、优先级）一致闭环；不一致都在保底/重投/动态路由三处边缘。→ P3-1
6. token 开销：实测每轮 SYSTEM ~700–1200 token（旧版 ~10 倍），且信号时间戳使其每轮变化、缓存不命中。→ P2-4

如需，我可以按 P1 优先给出一份最小修复 patch（不改行为只补兜底），或先把这份结论落到 `docs/` 作为复审记录。
