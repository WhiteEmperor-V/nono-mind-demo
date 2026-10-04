# wellness 器官 P1/P2 尾巴修复报告

任务书: `docs/FIX_WELLNESS_P1P2.md`(家令"修")
范围: 只动 `organs/wellness.py` 及其尾巴(测试/服务模板/报告), 不碰 `state/server.py`、`weixin/`、`.secrets.json`、`/root/.hermes/config.yaml`、量化。
基线: P0x4 已修(commit d87140f)。

## 改了哪些文件
| 文件 | 改了什么 |
|---|---|
| `organs/wellness.py` | P1-1 / P1-2 / P1-5 三处修根因 |
| `tests/test_wellness.py` | 原 5 项 + 新增 5 项回归用例(共 10 项) |
| `deploy/nono-wellness.service` | P2-8 加重启上限 + 内存上限; 同步装到 `/etc/systemd/system/` 并 `daemon-reload` |
| `docs/FIX_P1P2_REPORT.md` | 本报告 |

## 逐条对应

### P1-1 被4h节流压下的"过时报病"——自愈后撤掉待发
- 新增 kv `wellness/suppressed_issue={msg,at,key}`; `key` 标识病类(`brain`/`organs`)。
- `report_to_master(c, msg, key)`: 被 4h 节流时把待发大事连同病类记进 `suppressed_issue`。
- `_drop_suppressed_if_healed(c, brain_ok, dead_organs)`: 每轮体检拿到本轮病况后, 若该类病已恢复(brain通/器官全活)→ 删掉 `suppressed_issue`, 不当期、也不"到期补发"。
- 4h 窗口到点时, `report_to_master` 只在调用方判定"该类病本轮仍成立"(即本轮确有 big_issue)时才走到发送分支, 发的永远是当下实况, 不是过期话; 发送成功后清掉 `suppressed_issue`。
- `run_one` 记录 `big_issue_key`, 并把 `big_issue` 传给 `report_to_master` 时带上 key。

### P1-2 git_baseline 吞掉别人暂存的脏改动
- 固化基线改为**按路径提交**: `git commit -m ... -- <files>`, 不再 `git commit`(无路径)吞全局暂存区。
- 用 `git status --porcelain -- <files>` 判断"我们关心的文件"是否真脏: 不脏(脏的是别处)就跳过固化, 直接打 baseline。
- 空 baseline 提交加 `--only`(无路径=不提交任何暂存内容), 否则 `git commit --allow-empty` 仍会把别处 staged 的文件卷进 baseline 提交。这是修 P1-2 时实测发现的连带坑, 一并堵上。

### P1-5 毒信号检测只认 loop_error
- `check_poison_signals` 放宽: 强度 > `POISON_STRENGTH(0.5)` 时——
  - `sig_type=="loop_error"` → 清(原逻辑保留);
  - 其它类型(如 `master_message`/coder 任务反复崩堆着的) → `now - created > POISON_STALE_SEC(4h)` 即"滞留且强度未衰减到阈值以下" → 清。
- 只读 `signals` 表现有字段(`strength`/`sig_type`/`created`), **未加任何 schema/列**; 已用 `ponytail:` 注释标注"够用即可 + 升级路径"。
- 清掉前 `print` 留痕(含 sig_id / type / strength / 判据)。

### P2-8 systemd 模板资源/重启上限
- `deploy/nono-wellness.service`: `[Unit]` 加 `StartLimitIntervalSec=60` + `StartLimitBurst=5`(防"改→回滚"死循环疯狂重启刷日志); `[Service]` 加 `MemoryMax=200M`。
- 已 `cp` 到 `/etc/systemd/system/nono-wellness.service` 并 `systemctl daemon-reload`。
- 校验: `systemd-analyze verify` 无输出(无错); `systemctl show` 实测 `MemoryMax=209715200`、`StartLimitIntervalUSec=1min`、`StartLimitBurst=5`。

## 测试
- `tests/test_wellness.py` 全量绿: **10/10 OK**(原 5 + 新 5)。
- 新增用例(对应每条尾巴):
  - `SuppressedIssueTest::test_suppressed_issue_dropped_after_heal_no_stale_report` — 报过病→病好→到期不补发过时话, 且 `suppressed_issue` 被撤。
  - `SuppressedIssueTest::test_suppressed_issue_kept_and_sent_if_still_sick` — 窗口到期但病仍在 → 正常再报并清待发。
  - `PoisonSignalTest::test_poison_clears_loop_error_and_stale_other` — loop_error 照清; 滞留非 loop_error 清; 新鲜信号/低强度信号不清; 有 print 留痕。
  - `GitBaselineTest::test_baseline_does_not_swallow_others_staged_files` — 别人有 staged 脏改动时, 基线 commit 不含别人的文件, 别人的改动仍留在暂存区。
  - `GitBaselineTest::test_baseline_skips_fixup_when_our_files_clean` — 我们关心的文件干净时跳过固化, 不产生多余提交。
- 复跑整套 `tests/` 确认无回归: 只有 `tests/test_p1_fixes.py` 的 2 个 ❌(wechat P1-3 outbox 重试上限, 与 wellness 无关); 已 `git stash` 前后各跑一次确认**修前修后同样 2 个 ❌**, 属既存问题, 未在本任务范围内。

## 边界遵守
- 未碰 `state/server.py`、`weixin/`、`.secrets.json`、`/root/.hermes/config.yaml`、量化。
- 改动全落在 `/root/nono-mind` 仓库; 已 `git commit`(不 push)。
- 无新依赖; P1-5 未加 schema。
