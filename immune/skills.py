"""skills 分级 + 动态授权(完整版, 对齐9/27-28主人定案)

核心设计(主人拍板):
- skill本体存State信号池 skills/(大loop持有), 分 general(所有loop可用) / specialized(需动态授权)
- 小loop用完就删, 但改进出的 skill 由大loop检查有进步后存回公共区 → 经验不随loop消失
- 授权动态: 不写死给某loop, 大loop派活时临时授权(谁都用), loop删了授权自然失效(不靠TTL)

四件事:
1. load_skills(c): 扫 skills/general|specialized/*.yaml 注册进State
2. grant(c,loop,skill): 大loop派活时临时授权
3. copy_to(c,loop,skill): 把skill本体复制一份给小loop用(用完loop删, 本文件也删)
4. improve_and_store(c,loop,skill,version): 小loop改进出的新版本, 大loop检查有进步才存回公共区(票1先存+记账, "判进步"细则票2做)
"""
import os, time, yaml

# 修bug(9/28): skill扫描路径锚定"仓库根/skills"(immune/skills.py往上两级), 与CWD无关;
# 原实现用CWD/相对路径, 从别的目录跑就"扫不到skill". 可用 NONO_SKILLS_DIR 覆盖.
SKILLS_DIR = os.environ.get("NONO_SKILLS_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills")
NS = "skills"
TIERS = ("general", "specialized")

def load_skills(c):
    """扫 skills/general|specialized/*.yaml 注册进State. 返回(n_general, n_specialized). 幂等."""
    reg = {"general": [], "specialized": []}
    if not os.path.isdir(SKILLS_DIR):
        print(f"[skills] 警告: skill目录不存在, 扫不到skill: {SKILLS_DIR}")
    for tier in TIERS:
        d = os.path.join(SKILLS_DIR, tier)
        if not os.path.isdir(d):
            print(f"[skills] 警告: 缺{SKILLS_DIR}/{tier}目录")
            continue
        for fn in sorted(os.listdir(d)):
            p = os.path.join(d, fn)
            if os.path.isdir(p) or not fn.endswith((".yaml", ".yml")):
                continue
            try:
                with open(p, encoding="utf-8") as f:
                    sk = yaml.safe_load(f)
            except Exception as e:
                print(f"[skills] 解析失败跳过 {p}: {e}")
                continue
            if not isinstance(sk, dict):
                print(f"[skills] 跳过非法skill(不是映射) {p}")
                continue
            sk["tier"] = tier
            sk.setdefault("name", fn.rsplit(".", 1)[0])
            reg[tier].append(sk["name"])
            c.kv_set(f"{NS}/{tier}/{sk['name']}", sk)   # 本体存公共区(大loop持有)
    c.kv_set(f"{NS}/_index", reg)
    return len(reg["general"]), len(reg["specialized"])

def _index(c):
    """skill索引; 未被显式load过则懒加载一次(小loop用前查权限时才扫, 保证"查得到")."""
    idx = c.kv_get(f"{NS}/_index")
    if idx is None:
        load_skills(c)
        idx = c.kv_get(f"{NS}/_index") or {}
    return idx

def can_use(c, loop, skill):
    """某loop能不能用某skill: general恒可用; specialized要已授权(动态, 跟loop走, 无TTL)."""
    idx = _index(c)
    if skill in idx.get("general", []):
        return True
    if skill not in idx.get("specialized", []):
        return False
    return bool(c.kv_get(f"{NS}/grants/{loop}/{skill}"))

def grant(c, loop, skill, reason=""):
    """大loop派活时, 给某loop临时授权某specialized skill. general不授权(本就开放)."""
    sk = c.kv_get(f"{NS}/specialized/{skill}")
    if sk is None:
        _index(c)                 # 懒加载: 首次授权前若还没load过, 自动扫一次
        sk = c.kv_get(f"{NS}/specialized/{skill}")
    if sk is None:
        return False
    c.kv_set(f"{NS}/grants/{loop}/{skill}", {"at": time.time(), "reason": reason})
    c.emit("skill_granted", {"loop": loop, "skill": skill, "reason": reason}, decay=0.1, source="immune")
    return True

def copy_to(c, loop, skill):
    """复制skill本体给小loop用. 写到 skills/loops/<loop>/<skill>(随loop删除)."""
    sk = c.kv_get(f"{NS}/{('general' if can_use(c, loop, skill) and not c.kv_get(f'{NS}/specialized/{skill}') else 'specialized')}/{skill}")
    src = c.kv_get(f"{NS}/general/{skill}") or c.kv_get(f"{NS}/specialized/{skill}")
    if not src:
        return False
    c.kv_set(f"{NS}/loops/{loop}/{skill}", {"body": src, "at": time.time()})
    return True

def revoke(c, loop, skill=None):
    """撤loop时授权跟着失效: 删该loop的grant(票2"授权动态化"收尾). skill=None删该loop全部.
    返回删掉的授权条数。"""
    if skill is not None:
        c.kv_del(f"{NS}/grants/{loop}/{skill}")
        return 1
    n = 0
    for k in list((c.kv_list(f"{NS}/grants/{loop}/") or {}).keys()):
        c.kv_del(k)
        n += 1
    return n

def improve_and_store(c, loop, skill, new_body, note=""):
    """小loop改进了skill, 大loop检查有进步才存回公共区. 票1: 存新版本+记进步账(proof留给票2细化).
    返回是否存了."""
    c.kv_set(f"{NS}/{skill}/versions", (c.kv_get(f"{NS}/{skill}/versions") or []) + [
        {"at": time.time(), "by": loop, "note": note, "body": new_body}])
    c.kv_set(f"{NS}/{('general' if (c.kv_get(f'{NS}/_index') or {}).get('general',[]).count(skill) else 'specialized')}/{skill}",
             new_body)
    c.emit("skill_improved", {"loop": loop, "skill": skill, "note": note}, decay=0.2, source="immune")
    return True

def visible_skills(c, loop):
    """某loop当前能用的全部skill(通用全 + 已授权专精). 供loop自查能力."""
    idx = _index(c)
    out = [g for g in idx.get("general", [])]
    out += [s for s in idx.get("specialized", []) if can_use(c, loop, s)]
    return out
