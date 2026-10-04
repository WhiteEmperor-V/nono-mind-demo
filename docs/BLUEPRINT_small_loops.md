# 施工图: 小loop拼装 + skill分级 + 可见不派活
## 主人拍板(9/26晚) + 诺诺落地方案

---

## 一、她现在的真实骨架（要改的基础）

```
core/loop.py  ── 5秒一跳的主干
  │
  ├─ dialogue      (实时对话)
  ├─ reflection    (每2h反思)
  ├─ coder         (写码)
  ├─ canvas        (画图)
  ├─ wechat_loop   (微信通道)
  └─ wellness      (体检, 刚上线)
  │
  每个器官有 CAPABILITIES 列表 → 注册到 immune/capabilities.py (State信号池)
  核心loop按 to=="xxx" 分发信号给对应器官 (硬编码, 一个器官一个elif)
```

**现在的限制：**
- 器官是"写死的"，加一个能力 = 改 core/loop.py 加一个 elif
- 没有"一个器官能同时开多个实例"的概念（3个游戏loop = 现在做不到）
- 没有通用/专精的权限区分（所有能力对所有器官开放）
- 没有"小loop互看状态"的机制（只有核心能看全局）

---

## 二、要做的 4 件事（施工图核心）

### 事 ①：把"器官"升级为"可拼装插件"

**现在**：器官是硬编码的模块，`from organs import coder`
**改成**：器官 = 一组"插件"的集合，每个插件声明自己的能力+需要哪些skill

**落地方式（改动最小）**：
不重写 organs/ 目录，而是在 `plugins/` 目录下加一个**插件清单文件**：

```
plugins/
  coder/
    manifest.yaml     ← 新增: 声明这个器官装了哪些插件、需要什么skill
  canvas/
    manifest.yaml
  dialogue/
    manifest.yaml
  ...
```

`manifest.yaml` 长这样（以 coder 为例）：
```yaml
organ: coder
plugins:
  - name: code_sandbox
    type: active        # active=有心跳 / passive=被调用
    provides: [write_code, run_test, git_commit]
    requires_skills: [coding-python]   # 需要授权的专精skill
  - name: git_ops
    type: passive
    provides: [commit, rollback]
    requires_skills: []
skill_authorization:      # 这个器官被授权了哪些skill
  - coding-python
  - git-ops
```

**谁来读这个文件**：`immune/plugins.py`（新增，~100行）
- 启动时扫描所有 `plugins/*/manifest.yaml`
- 把每个器官声明的 `provides` 能力注册到 capabilities 表（替代现在器官自己写 CAPABILITIES 的散乱方式）
- **大loop想开N个coder实例**：`plugins/` 里读同一份 manifest，开 N 个 process/thread，每个有自己的实例ID（`coder-1`, `coder-2`...）

### 事 ②：skill 分通用 / 专精两级

**现在**：没有 skill 系统（`skills/` 目录只有一个 threejs-game-skills 空壳）

**落地方式**：
```
skills/
  general/          # 通用: 所有loop都能用，无需授权
    common-sense.yaml
    state-read.yaml
  specialized/      # 专精: 特定loop授权才能用
    coding-python.yaml
    quant-trading.yaml
    3d-modeling.yaml
    ...
```

每个 skill 文件：
```yaml
name: quant-trading
type: specialized
authorized_organs: [quant]   # 只有 quant loop 授权了才能用
description: 量化交易策略...
when_not: 不适用于非交易场景
```

**谁来管权限**：`immune/skills.py`（新增，~80行）
- 器官 A 想用某个 skill 前，先查 `authorized_organs` 有没有自己
- 没有 → 要么拒绝，要么发信号给大loop申请授权（大loop拍板）

### 事 ③：小loop互相看状态，派活过大loop

**现在**：核心 loop 是唯一的全局视角，器官之间完全隔离

**落地方式**：
每个器官实例在 State 信号池里写自己的状态（`states/<organ-name>/progress`）：
```
states/coder-1/progress = {当前任务, 完成度%, 卡在哪}
states/coder-2/progress = {当前任务, 完成度%, 卡在哪}
states/canvas/progress  = {当前任务, 完成度%, 卡在哪}
```

**可见性规则（铁律落地）**：
- 任何小loop可以随时**读** `states/` 里其它小loop的进度（State信号池天然支持，零新协议）
- 要**给别的loop派活** → 不能直接写信号给那个器官，必须发一个 `dispatch_request` 信号给大loop，大loop审核后转成真正的 `task_dispatch` 信号
- 大loop的审核逻辑：检查目标loop有没有空、这个任务是不是它该干的（看manifest的provides）

**改动位置**：
- `core/loop.py`：加一个 `dispatch_request` 处理分支（收到申请→查目标器官状态→允许则转发，不允许则拒绝+记录原因）
- 各器官：在任务完成/卡住时写 `states/<self>/progress`（~5行代码/器官）

### 事 ④：大loop编排长问题（哪些小loop入场、什么顺序）

**这是最难的一块，分两步**：

**第一步（先做，简单）**：大loop遇到复杂任务时，**按 manifest 查哪些器官有相关能力**，按依赖顺序编排：
```
任务: "写个3D游戏并渲染"
大loop编排:
  1. coder-1 (code_sandbox插件) → 写代码
  2. coder-1 → 跑测试 (git_commit插件)
  3. canvas (threejs插件) → 渲染验证
  4. reflection → 复盘这次哪步慢
```

**第二步（以后再做，复杂）**：大loop自己动态决定开几个实例、什么顺序、要不要并行。这个暂时不做，先用第一步的静态编排。

---

## 三、文件改动清单（施工图执行层）

| 文件 | 动作 | 工作量 |
|---|---|---|
| `plugins/*/manifest.yaml` | 新增（每个现有器官一份） | 小 |
| `immune/plugins.py` | 新增：读manifest、注册能力、管理多实例 | 中 (~150行) |
| `immune/skills.py` | 新增：skill分级权限检查 | 小 (~80行) |
| `skills/general/*.yaml` | 新增 | 小 |
| `skills/specialized/*.yaml` | 新增 | 小 |
| `core/loop.py` | 改：加 dispatch_request 分支 + 多实例支持 | 中 |
| `organs/*.py` | 改：各器官加写 progress 到 states/ | 小 (每器官~5行) |

**总工作量**：中等，Codex 可以一次任务书干完（danger-full-access，测试全绿+commit）

---

## 四、实施顺序（分两票，不硬挤一锅）

**票1（先做，基础层）**：
- 事①：插件manifest + plugins.py（让器官能力从manifest读，不再散在各器官文件里）
- 事②：skill分级 + skills.py（通用/专精 + 授权检查）

**票2（票1绿了再做，通信层）**：
- 事③：states/ 互相可见 + dispatch_request 过大loop
- 事④：大loop编排（第一步静态版）

**两票分开验收**，不攒一锅交付。

---

## 五、给主人的一句话

**施工图核心**：把"硬编码器官"升级成"按manifest拼装的插件集合"，能力跟着插件走（装什么插件会什么），skill 分两级（通用全开/专精要授权），小loop能互看进度但派活必须过大loop，大loop遇到长任务时按manifest查能力、编排入场顺序。分两票做，先基础后通信。

主人你看完说一声，我就出任务书（票1）派给 Codex 开干。
