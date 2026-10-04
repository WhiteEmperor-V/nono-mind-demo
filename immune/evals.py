#!/usr/bin/env python3
"""地基⑥: 评价系统轻量版——任务完成时的自评分(步骤成功率) + 主人反馈记录
理论出处: Foundation Agents综述(评价/奖励是完整个体的核心模块)
用途: 反思器官读evals画能力画像(哪类任务成功率高), 长期积累后可指导待学清单排序
"""
import time

def self_eval(c, task_id, organ: str, steps: list, demand: str = "", stuck_abort: bool = False):
    """任务完成时自评: 步骤成功率 + 首步即成奖励
    stuck_abort=True(卡死中止)不计入能力分母——卡死≠能力差(AgentGym: 奖励要归因正确)"""
    if not steps:
        return
    ok_steps = sum(1 for s in steps if s.get("ok"))
    success_rate = ok_steps / max(len(steps), 1)
    # 评分: 成功率为基, 首步即成+0.2奖励(计划质量), 步数少+0.1(效率)
    score = success_rate
    if len(steps) == 1 and ok_steps:
        score += 0.2
    elif len(steps) <= 2 and ok_steps == len(steps):
        score += 0.1
    c.kv_set(f"evals/{task_id or str(int(time.time()))}", {
        "at": time.time(), "organ": organ, "demand": demand[:120],
        "score": round(min(score, 1.0), 2), "steps_total": len(steps),
        "stuck_abort": stuck_abort,
        "steps_ok": ok_steps, "source": "self"})

def master_feedback(c, task_id: str, good: bool):
    """主人反馈(最高权重, 覆盖自评)"""
    key = f"evals/{task_id}"
    old = c.kv_get(key)
    if isinstance(old, dict):
        old["master_feedback"] = "good" if good else "bad"
        c.kv_set(key, old)

def ability_profile(c, organ: str = None) -> dict:
    """能力画像: 各器官/任务类型的平均得分(反思器官读它排待学清单优先级)
    stuck_abort的卡死任务不计入能力分(归因正确: 卡死≠能力差)"""
    store = c.kv_list("evals/")
    profile = {}
    for k, v in store.items():
        if not isinstance(v, dict):
            continue
        if organ and v.get("organ") != organ:
            continue
        if v.get("stuck_abort"):
            continue  # 卡死中止的不算
        org = v.get("organ", "?")
        p = profile.setdefault(org, {"count": 0, "total": 0.0, "good": 0, "bad": 0})
        p["count"] += 1
        p["total"] += float(v.get("score", 0))
        if v.get("master_feedback") == "good": p["good"] += 1
        if v.get("master_feedback") == "bad": p["bad"] += 1
    for org, p in profile.items():
        p["avg"] = round(p["total"] / p["count"], 2)
    return profile

# ---- ScalingInter-RL: 渐进步数预算(AgentGym的RoundScheduler思想) ----
def steps_budget(c, organ: str = "coder", difficulty: str = "medium") -> int:
    """根据能力画像动态算步数预算: 练得越熟→链条越长; 从短horizon练起(无数据=4)
    difficulty: simple(×0.6)/medium(×1.0)/hard(×1.4), 上限10下限2"""
    p = ability_profile(c, organ).get(organ)
    if not p or p.get("count", 0) < 3:
        base = 4  # AgentGym: 从小horizon练起, 数据不足3条视为新手
    else:
        avg = p["avg"]
        base = 8 if avg >= 0.9 else 6 if avg >= 0.7 else 4 if avg >= 0.5 else 3
    coef = {"simple": 0.6, "medium": 1.0, "hard": 1.4}.get(difficulty, 1.0)
    return max(2, min(10, round(base * coef)))
