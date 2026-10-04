# 任务书：wellness 器官（自我体检 + 静默自愈 + git 回滚）

## 背景（主人 9/25 拍板）
nono-mind 现在是"系统靠五根 systemd 管子托着的活体植物人"——她不知道自己活着、不知道自己病了。
三个真实暗病全是主人肉眼发现报给诺诺的：
1. 9/19 `_CPKEY` NameError：llm_client 崩 → 对话能力木了18h，只会复读兜底句
2. 9/25 scnet 通道 431 挂死：主脑挂 → 她降级到 nemotron 慢走，自己不知道
3. 9/25 reflection 静默 failed 18h：没人知道

目标：给她装一个 **wellness 器官**（专职自我体检），活着时每隔 5 分钟自己体检一遍，
有病**静默自愈**（不刷屏主人），只有"大事"才报主人；主人不回她，她继续自己解决。

## 主人拍板的三条规矩（硬约束，违反即打回）

### 规矩1：报病 = 只在大事上叫主人，平时静默自愈
- 小问题（scnet挂、毒信号、某器官心跳停了、llm通道慢）→ 她**自己修，不告诉主人**，修完不汇报
- 只有"大事"才发主人微信：① 脑子彻底没救（所有LLM通道全挂）② 自己修了N次没修好 ③ 要动到钱/生产配置
- 报病后**主人不回 → 她不等卡死**，继续自己想办法（重试自愈/降级兜底），把"等主人"和"自救"解耦

### 规矩2：她能自己动手的边界（治"自己把自己修坏"）
她允许做的自愈操作（白名单，只这些，越权一律禁止）：
- 重启挂掉/心跳停了的器官（systemctl restart nono-state/core/reflection/canvas/wechat）
- 清掉信号池里的毒信号（连续失败≥3次的，走已有 POISON drop）
- 调 llm_client 降级链顺序（哪个通道活优先用哪个）——走仓内 llm_client.py，git 管

她**绝不允许**做的：
- 改 /root/.hermes/config.yaml（Hermes 全局配置，出了她身体范围）
- 改任何 systemd 之外的系统服务（nginx/docker/quant）
- 删数据、改凭据、动 weixin/account.json、.secrets.json
- 任何 rm/写库/外发数据类高危操作

### 规矩3：改坏了能自己改回来 = git 回滚（主人指定，不用 .bak 文件）
她**每次改代码类文件前**（organs/*.py, core/*.py, llm_client.py, deploy/*.service）：
1. **先 commit 一份"改动前基线"**：`git add <要改的文件> && git commit -m "wellness: baseline before <改动描述> [auto]"`
   （若工作区已有脏改动，先 commit 那些脏改动再开基线，保证基线是干净的已知状态）
2. **改完立刻验证**（每个改动配一个最小自检，见下方"自愈操作表"）
3. **验证不过 → `git checkout -- <文件>` 回滚到基线 + 自动 commit "wellness: rollback <改动> [验证不过]"**，
   并且这件事**升级成大事报主人**（她没把握，交给人）
4. 验证通过 → commit "wellness: <改动描述> [verified]" 固化

**注意 git 边界**：`weixin/`、`.secrets.json` 在 .gitignore 外（凭据不入仓，回滚不会碰它们，符合安全原则）。
仓内代码/服务模板归 git 管；/root/.hermes/config.yaml 不归本仓，她根本不许碰，所以"git回滚"只管仓内文件。

## 器官实现要求（照此造）

### 文件：organs/wellness.py（主动器官，有自己的心跳）
- 声明 CAPABILITIES，注册进能力注册表（照 canvas_organ/coder 的 CAPABILITIES 写法）
- 体检循环：每 5 分钟（IDLE 可调，环境变量 WELLNESS_INTERVAL 默认 300s）跑一次
- 体检项（四条，都对应9/25真实暗病）：
  | 体检项 | 怎么检 | 判病标准 |
  |---|---|---|
  | LLM脑子通不通 | llm_client.chat 发"回复OK" min_tokens=5 | 10s无回 = 脑子病了 |
  | 各器官心跳 | systemctl is-active 五个服务 + journald 最近30min有无该服务崩溃记录 | 任一 inactive = 该器官死了 |
  | 信号池毒信号 | 读 signals 表，strength高+处理反复失败的 | 复用已有 POISON_CNT drop 逻辑 |
  | 器官日志报错 | journald 最近5min grep "循环级异常/NameError/No module/timeout" | 有命中 = 有暗病 |

### 自愈操作表（规矩2白名单 + 规矩3 git回滚，逐条配验证）
| 病 | 自愈动作 | 改前基线 | 验证 | 不过则 |
|---|---|---|---|---|
| 某器官 inactive | `systemctl restart <svc>` | 无需(不改代码) | restart后5s内 `is-active` 变 active | 重试2次仍败→报主人 |
| LLM某通道死(如scnet 431) | 改 llm_client.py 降级链顺序，把活通道提到前 | git commit baseline | 改完 `chat("回复OK")` 能拿到非兜底回复 | git checkout 回滚 + 报主人 |
| 毒信号死循环 | 走已有 POISON drop | 无需 | 信号池该 sig_id 消失 | 重试 |

### 报病（规矩1）
- 大事才发：往 State `wechat_outbox` append 一条人话（走已有 outbox 链路，wechat_loop 会发）
- 文案口语，例："主人我scnet那条主脑路不通了，我现在靠备用通道走，能正常说话就是慢一点。要不要你帮我看看scnet那边？（你忙的话我自己再试试修）"
- 主人不回：她记 `last_master_noresponse`，下轮体检若病还在 → 自己再试一次自愈，不重复刷屏（4h节流，照 reflection 主动分享那套）

## 交付与验收
- 代码由 Codex 写（诺诺出任务书+验收）；organs/wellness.py + core/loop.py 注册 + 测试
- 测试（宿主机跑，socket集成诺诺跑）：
  1. 注入一个 inactive 的模拟服务 → wellness 自愈 restart 成功
  2. 故意把 llm_client 某通道改错 → wellness 改回 + git 回滚验证（diff 看得到 baseline/rollback 两次commit）
  3. 脑子彻底全挂（模拟）→ 发 outbox 报病，主人不回下轮重试不刷屏
  4. 全程 `git log` 能追溯：baseline → 改动 → (rollback 或 verified)
- 全绿后 commit，不 push（诺诺审）

## 边界（明确不做，防 Codex 越权）
- 不碰 /root/.hermes/config.yaml、weixin/、.secrets.json
- 不碰量化（铁律：量化永不接入新身体）
- 自愈只动白名单操作，任何"我觉得需要改X"但X不在白名单 → 只报主人不动手
