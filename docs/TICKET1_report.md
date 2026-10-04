# 票1 交付报告: 小loop动态拼装 — 基础层

## 一句话
把"各器官散写能力"改成"能力跟插件(manifest)走", skill分两级+动态授权可用, 各器官往
`states/<自己>/progress` 写进度互看, judgments判断账确认为所有loop可读的公共层。**只做地基, 不碰核心loop现有行为、不碰任何铁律禁区。**

## 一、修的bug: load_skills 扫不到 skill(路径问题)
- 症状: `immune/skills.py::load_skills` 从别的目录跑就"扫不到skill"。
- 根因: 扫描路径依赖运行时CWD, 不是锚定仓库根。
- 修法(`immune/skills.py`):
  - `SKILLS_DIR` 锚定"仓库根/skills"(本文件往上两级), 与CWD无关; 可用 `NONO_SKILLS_DIR` 覆盖。
  - yaml用 `with open(..., encoding="utf-8")`, 非法/非映射文件给出明确告警(不再静默吞掉→"扫不到")。
  - 加 `_index()` 懒加载: 小loop第一次查权限(`can_use`/`visible_skills`)时若还没load过, 自动扫一次,
    保证"用前一定查得到"(以前没人显式load就永远查不到)。
- 回归用例: `tests/test_skills.py::test_load_is_cwd_independent` / `test_lazy_bootstrap_on_first_use`。

## 二、改的文件清单 + 每个文件干什么

| 文件 | 动作 | 作用 |
|---|---|---|
| `plugins/dialogue/manifest.yaml` | 新增 | 对话器官: 装chat插件, provides=[chat], requires_skills=[state-read,self-report] |
| `plugins/reflection/manifest.yaml` | 新增 | 反思器官: 装self_check插件, provides=[self_check] |
| `plugins/coder/manifest.yaml` | 新增 | 编程器官: 装code_sandbox, provides=[code_task], requires_skills=[coding-python], instances=2 |
| `plugins/canvas/manifest.yaml` | 新增 | 画布器官: 装html_create, provides=[canvas_create] |
| `plugins/wellness/manifest.yaml` | 新增 | 体检器官: 装health_check, provides=[health_check] |
| `plugins/wechat_loop/manifest.yaml` | 改 | 微信通道器官补 organ/plugins/instances 字段(保留原插件管理字段, 无对外能力) |
| `immune/plugins.py` | 改 | 新增manifest层: `load_manifest/discover_manifests/provides_from_manifest/requires_skills_from_manifest/capabilities_from_manifest/instances_from_manifest/register_capabilities_from_manifests/write_progress/read_progress/read_judgments/track_progress`。旧的PluginManager原样保留 |
| `immune/skills.py` | 改 | 修路径bug + 懒加载(见上); 分级general/specialized, 动态授权(grant/can_use/copy_to/improve_and_store/visible_skills) |
| `skills/general/*.yaml`, `skills/specialized/*.yaml` | 新增 | 2通用(state-read/self-report) + 3专精(coding-python/quant-trading/3d-modeling) |
| `organs/dialogue.py` | 改 | CAPABILITIES改为读自己manifest; handle套`track_progress`写states/dialogue/progress |
| `organs/reflection.py` | 改 | 同上; 新增CAPABILITIES(原内联在core/loop) |
| `organs/coder.py` | 改 | CAPABILITIES读manifest; run套track_progress |
| `organs/canvas_organ.py` | 改 | CAPABILITIES读manifest; create套track_progress |
| `organs/wellness.py` | 改 | CAPABILITIES读manifest; run_one套track_progress |
| `organs/wechat_loop.py` | 改 | 收消息入State后写states/wechat_loop/progress(try/except包裹, 不影响通道) |
| `core/loop.py` | 改 | 启动注册能力: `BUILTIN_CAPS` 改为从各器官(即各manifest)读取(替代原先dialogue/reflection内联+各器官散写), 注册条数不变(5/5) |
| `tests/test_skills.py` | 新增 | skill分级/动态授权/复制/改进存回 + 路径回归(换CWD仍扫得到/懒加载) |
| `tests/test_plugins.py` | 新增 | manifest扫描/能力合法+注册(5/5)/多实例/所需skill/进度互看/track_progress/judgments公共层 |
| `tests/test_ticket1.py` | 保留(诺诺) | 票1原始验收用例, 保持绿 |

## 三、"公共层接judgments"
判断账(`organs/judgments.py`, 存 `judgments/<id>`)本就在State公共区。新增
`immune/plugins.py::read_judgments(c)` 作为所有loop读公共判断的统一入口, 由
`tests/test_plugins.py::test_judgments_public_layer` 证明"写得进、任何loop读得到"。

## 四、测试结果(逐文件独立跑, 各自新进程)
- 核心必绿: `test_wellness` OK(10) · `test_judgments` OK(9) ✅
- 票1: `test_ticket1` 全通过 · `test_skills` 全通过 · `test_plugins` 全通过 ✅
- 既有回归: `test_layered` OK · `test_p2` 14/14 · `test_state` 10/10 · `test_stuck` OK ·
  `test_tasks` OK · `test_memory_graph` OK · `test_fixes` 9/9 · `test_idle` 11项全过 ✅
- 说明: `test_p1_fixes` 报 24/26(2项): 经 `git stash` 验证为**改动前就存在的**既有失败
  (P1-3微信outbox重试上限, 与票1无关); `test_wechat_loop` 需真网络/登录, 本环境超时,
  也在改动前即如此。

## 五、边界遵守(铁律)
- 未碰 weixin/凭据、.secrets、`/root/.hermes/config.yaml`、量化代码、`llm_client`通道、
  `state/server.py`。
- 各器官功能不回归: 只是能力来源从"散写"变"读manifest", 注册条数不变; 进度写入用
  try/except或装饰器包裹, 不干扰原流程。

## 六、本票未做(票2)
临时loop用完即删运行时、动态授权引擎、任务编排模板、`dispatch_request`过大loop等——按任务书留到票2。
