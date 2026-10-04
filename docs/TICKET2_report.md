# 票2 交付报告: 动态loop生命周期 + 大loop编排派活 + 判进步存经验

## 一句话
大loop(底座)现在能按 manifest 现拼一个**临时loop**干活: 现场动态授权(requires_skills)→复制skill本体→
按模板顺序派发(走现有 `task_create/task_claim` CAS + `task_dispatch`)→干完删loop(授权跟着失效)→
拿"本次 vs 上次同类结果"**判进步**, 进步才把改进版经 `skills.improve_and_store` 存回公共区。
**全寄生现有 State/manifest/skills, 零新协议/新进程/新依赖, 未碰任何铁律禁区。**

## 一、改的文件清单 + 每个文件干什么

| 文件 | 动作 | 作用 |
|---|---|---|
| `core/loop.py` | 改 | 票2核心: 任务类型→建议顺序模板 + 临时loop装配/拆解 + 判进步 + 编排 + 真实接线 |
| `immune/skills.py` | 改 | 新增 `revoke(c, loop, skill=None)`(撤loop→授权失效); `grant` 补懒加载(首次授权前自动扫skill) |
| `immune/plugins.py` | 改 | 新增 `organ_for_provides(cap)`(编排查manifest反查"谁提供该能力") |
| `tests/test_ticket2.py` | 新增 | 票2验收用例(编排模板/临时loop造删/判进步/编排端到端/接线检查)全绿 |
| `docs/TICKET2_report.md` | 新增 | 本报告 |

### core/loop.py 新增(约200行, 全部落在一处, 可单测)
- `TASK_TEMPLATES`: 任务类型→**建议顺序**(能力名). `code`=写码→跑测→复盘; `modeling`=coder→canvas→reflection; `canvas`=canvas→reflection。
  **是建议不是写死**: `orchestrate(order=...)` 留强脑子 override 路径, `params.no_orchestrate` 可关掉自动编排。
- `_detect_task_type(text)`: 原话→任务类型(关键词规则, 不调LLM; 认不出返回 None 走原有单发路径)。
- `plan_stages(c, task_type)`: **查 manifest 找能力**——把模板里的能力名用 `organ_for_provides` 反查成器官顺序, 返回 `[{organ,cap}]`。
- `assemble_temp_loop(c, organ, task_type)`: 读 `provides_from_manifest/requires_skills_from_manifest` 装配;
  `skills.grant`(现场动态授权)+`skills.copy_to`(skill本体复制给小loop); 记 `loops/temp/<id>`; emit `loop_spawned`。
- `teardown_temp_loop(c, loop_id)`: `skills.revoke`(删该loop全部grant→**授权跟着loop失效**)+删复制的skill+标 `terminated`; emit `loop_terminated`。
- `judge_progress(prev, cur)`: **纯函数判进步**。`improve`/`regress`/`flat`/`baseline`。
- `record_result(c, task_type, organ, metrics, skill)`: 对比本次 vs `progress/<task_type>`(上次同类账目);
  进步→组改进版 body→`skills.improve_and_store(c, "big_loop", skill, body)` 存回公共区+emit `progress_stored`; 无论进退都更新账目。
- `_metrics_from_task` / `_run_stage` / `orchestrate`: 提取度量 / 复用器官入口执行 / 按序编排派发。
- `main()`: ①启动 `skills.load_skills(c)`; ②coder/canvas 分支真实 `assemble→run→teardown→record_result`(不是空函数);
  ③`master_message` 命中跨器官长问题(如建模)→ `orchestrate` 按建议顺序起临时loop跑完再回执。

## 二、判进步的对比判据(写清)
账目键 `progress/<task_type>`, 每次记本次度量 `{ok, steps, over, at, note}`(ok=是否成功, steps=步数, over=是否超时/卡死)。
下次干同类活时 `judge_progress(上次, 本次)`:
- **首次(无上次)** → `baseline`: 只记基线,**不存**经验。
- **进步=improve**(任一): 上次失败→这次成功; 上次超时/卡死(`over`)→这次没有; 都成功但这次 `steps` 更少。
- **退步=regress**: 这次失败而上次成功, 或都成功但这次步数更多。→ **不存**, 继续用老版。
- **持平=flat**: 其余。
只有 `improve` 且配了 `skill` 时才调 `skills.improve_and_store`(由**大loop** `"big_loop"` 落 `by` 字段), 即"不让小loop自夸"。
改进版 = 原skill body + 追加一条 `practice`(含 step数/任务类型/结论), 版本落 `skills/<skill>/versions`。

## 三、临时loop"真实造/删"(验收硬要求)
- 装配: `assemble_temp_loop` → 断言 `loops/temp/<id>.status=running` + `skills/grants/<id>/coding-python` 存在 + `skills/loops/<id>/coding-python` 已复制。
- 拆解: `teardown_temp_loop` → 断言该loop的 grant 全空、复制的skill被删、记录 `terminated`。
- 常驻loop(wechat_loop等 continuous)**不进临时表**, 只"激活/熄火", 本票不改其生命周期。
- 接线证据: `test_wiring_real` 用 `inspect.getsource(core.loop.main)` 断言真实 dispatch 分支调用了 `assemble_temp_loop/teardown_temp_loop/orchestrate/record_result`
  (防止"只写函数没接线")。

## 四、测试结果
- 票2: `python3 tests/test_ticket2.py` → 5项全过 ✅
- 核心必绿回归(逐文件独立新进程):
  `test_wellness` 10/10 ✅ · `test_judgments` 9/9 ✅ · `test_ticket1` 全过 ✅ · `test_skills` 全过 ✅ · `test_plugins` 全过 ✅
- 更广回归: `test_layered` 11/11 ✅ · `test_p2` 14/14 ✅ · `test_state` 10/10 ✅ · `test_stuck` ✅ ·
  `test_tasks` 5/5 ✅ · `test_memory_graph` ✅ · `test_fixes` 9/9 ✅ · `test_idle` 11项 ✅
- 既有(与票2无关): `test_p1_fixes` 24/26 —— 2项为**改动前就存在**的 P1-3 微信outbox重试上限失败(与票1报告一致);
  `test_wechat_loop` 需真网络/登录, 本环境超时(亦为既有)。

## 五、边界遵守(已核)
- `git diff --name-only` 过越权黑名单(weixin/凭据/.secrets/hermes config/量化/llm_client/state server) → **空**。
- 未新增常驻进程: 临时loop复用现有信号池+任务CAS, 不 `Popen`/不起服务; 用完删记录+撤授权。
- 常驻5器官功能不回归: `test_wellness/test_judgments/test_layered/test_idle` 等照绿。

## 六、本票未做 / 留口
- 编排仍是**静态模板第一步**(主人定案"第二步以后再做"): 动态决定开几个实例/并行/依赖图未做, 留 `orchestrate(order=...)` 给强脑子 override。
- 自动编排仅稳定触发"跨器官长问题"(当前=modeling, 如"写个3D游戏并渲染"→coder→canvas→reflection);
  其余任务走单发分支(单发分支已带临时loop生命周期+判进步)。code 类模板(coder→跑测→复盘)已就位, 供强脑子显式调用。
