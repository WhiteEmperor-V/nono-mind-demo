# 任务书: 票2 — 动态loop生命周期 + 大loop编排派活 + 判进步存经验
## 前置: 票1(78609ee)已验收全绿——manifest/skill分级/进度互看/judgments公共层已就位

## 主人定案(9/27-28, 以 multi-loop-architecture skill 9-27深化版为准)
- 小loop"要就造、用完删", 资源动态调整: 配摄像头→拉起摄像头loop长驻; 小活→临时loop弄完即删; 不用了→连摄像头loop也删
- 大loop(底座)=编排者: 遇到长问题按manifest查能力, 决定造哪个/几个loop、什么顺序入场
- skill/插件本体留在大loop, 用时复制给小loop; 小loop改进后大loop"判进步"存回公共区
- 判进步=大loop拿"本次结果"比"上次同类结果", 进步才存(不让小loop自夸)

## 本票做4件事(全寄生现有State/manifest/skills, 零新协议/新进程/新依赖)
1. **临时loop生命周期**: 大loop按任务现拼一个临时loop(读manifest的provides/requires_skills装配, 复用票1的register_capabilities_from_manifests)
   - 任务做完→大loop撤掉这个loop(grant的授权跟着失效)→把干活的经验经"判进步"存回skills公共区
   - 常驻loop(wechat/视觉/语言/摄像头)不删, 只"激活/熄火"(ticket1的loop_type: continuous长驻 vs 临时)
2. **大loop编排(第一步静态模板)**: core/loop.py加"任务类型→建议顺序"模板(参考不死规则, 强脑子可override)
   - 例: 写码类=coder→test→reflection; 建模类=coder→canvas→reflection
   - 编排时查manifest找能力, 造所需loop, 按顺序派发(走现有task_dispatch/CAS claim, 不新造协议)
3. **判进步存经验(票1留的接口improve_and_store落地)**: 大loop干完同类任务, 对比本次vs上次结果
   - 进步(如这次一次过/上次的超时这次没超时)→调skills.improve_and_store把改进版存回公共区+记progress账
   - 退步→不存, 用老版
4. **授权动态化收尾**: 大loop派活时按manifest的requires_skills现场grant(票1已做grant, 这步接进编排流程)

## 边界铁律(越权=打回)
- 不碰 weixin/凭据, .secrets, /root/.hermes/config.yaml, 量化代码, llm_client通道, state/server.py
- 不新增常驻进程; 临时loop复用现有插件子进程机制, 用完terminate
- 现有常驻器官功能不回归(5器官照跑)
- 模板=建议, 不写死; 强模型override路径要留

## 交付
- core/loop.py 编排+临时loop生命周期; 判进步存经验接进skills.improve_and_store
- tests/test_ticket2.py(临时loop造/删/经验存 + 编排模板 + 判进步) 全绿
- 全量回归(wellness/judgments/ticket1/skills/plugins)保持绿
- commit(不push), 报告 docs/TICKET2_report.md(改的文件+每文件干什么+判进步的对比判据写清)

## 验收标准(诺诺会亲跑)
- test_ticket2绿 + 全量回归绿 + 越权检查空
- 临时loop真实能"造出来干完活再删掉"(不是只写函数没接线)
