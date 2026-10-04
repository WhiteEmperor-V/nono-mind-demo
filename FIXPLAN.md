# 修复分支 fix/arch-review-p0 施工任务书

> 依据: 双智能体交叉审查(DeepSeek 12条 + Codex 14条, 共识7条)
> 归档: wikis/nono/arch-review-v1.md

## P0 致命(4项, 本分支完成)

1. 统一Signal模型 → state/model.py 单点定义, router/server/loop全改引用
   修复: 路由断链+双源分裂
2. decay全域clamp: 服务端拒绝emit(strength>10或decay<0.01且非P0) + P0信号改TTL制(24h强制过期)
   修复: decay=0永不清除导致的表膨胀
3. 意识核per-signal try/except + 异常转loop_error信号回注
   修复: 一个LLM异常杀死整个意识核
4. checkpoint降频: 每30s或每50信号一次(二选一先到), 快照滚动保留5份
   修复: 磁盘耗尽+备份阻塞主循环

## P1 严重(5项, 本分支完成)

5. StateClient重连+指数退避(0.5s→1→2→4→8s max) + emit幂等key(客户端生成sig_id)
6. 优先级防饿死: 意识核按P0:P1:P2:P3=4:3:2:1加权轮询 + signals查询LIMIT 20
7. 挂起区单写(只进kv不双写signals) + sweep_suspended实现 + TTL 7天
8. kv_set CAS(expected_revision) + kv_append原子操作

## P2 一般(4项, 视进度)

9. 协议v2: request_id回显 + 4字节长度前缀 + 行长上限1MB
10. 路由key小写规范化 + payload必需字段检查
11. socket chmod 600 + 移到/run/nono/
12. dialogue_history按天分片

## 完成标准
- 全部修复有对应测试
- tests/test_state.py 10/10 + 新增测试全过
- 修复分支merge回main, push
