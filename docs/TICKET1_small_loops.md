# 任务书: 小loop动态拼装架构 — 票1(基础层)
## 目标: 治好诺诺"人格分裂"——从"一堆固定器官凑出的机构"升级为"一个自我(底座) + 按需拼装能力(插件)"

## 设计定案(主人 9/27-9/28 拍板, 以 multi-loop-architecture skill 9-27 深化版为准)
- **底座loop(大loop) = 唯一的"自我"**, 常驻, 不装能力插件, 只管编排/造loop/验经验
- **小loop = 底座 + 插件(能力) + skill(说明书)**, 按需拼装:
  - 常驻小loop(wechat/视觉/语言): 长期跑
  - 临时小loop: 要时造、用完删
- loop装1个插件=专精, 装N个=复合
- skill/插件本体**保留在大loop处**, 小loop用时就地**复制一份过去**, 小loop自己用+改, 大loop检查有无进步, 有进步存回公共区
- **记忆分公共层+私有层**: 公共层(judgments/经验)所有loop可读, 私有层各loop隔离
- skill/插件**授权走动态**(大loop派活时判, 不写死表)
- 固定模板=参考不死规则(弱模型照走, 强模型可override)
- 判进步=大loop拿本次结果比上次同类结果, 进步才存

## 本票范围: 票1(基础层) — 只做地基, 不做"临时loop用完即删"运行时
1. 每个现有器官在 plugins/ 下出一份 manifest.yaml(声明装哪些插件/能力/需要什么skill)
2. 新增 immune/plugins.py: 启动扫描 plugins/*/manifest.yaml, 把能力注册进capabilities表(替代各器官散写的CAPABILITIES), 支持按manifest动态开多实例
3. 新增 immune/skills.py + skills/general/ + skills/specialized/: skill分两级, 授权动态查(不写死), 小loop用前查权限
4. 各器官改: 读自己manifest的provides(能力跟插件走), 往 states/<自己>/progress 写进度(供小loop互看)
5. 底座公共层: 把judgments(判断账)接成所有loop可读的公共记忆层(已存在, 确认各loop读得到即可)

## 明确不做(本票): 临时loop用完即删的运行时、动态授权引擎、任务编排模板——票2再上

## 边界铁律(越权=打回)
- 不碰 weixin/ 凭据、.secrets、/root/.hermes/config.yaml、量化代码
- 不改 llm_client 通道(主脑cpa那套)
- 不碰 state/server.py(那是9/9审查遗留)
- 插件/skill本体不动核心loop现有行为, 只做"注册/读取"层, 现有5器官功能不回归

## 交付
- plugins/*/manifest.yaml, immune/plugins.py, immune/skills.py, skills/general|specialized/*.yaml
- tests/test_plugins.py + tests/test_skills.py 全绿
- commit(不push), 报告写 docs/TICKET1_report.md
- 改的文件清单+每个文件干什么写进报告
