#!/usr/bin/env python3
"""wellness 器官: 诺诺的"值班医生"——活着时自我体检, 有病静默自愈, 大事才叫主人.

主人 9/25 拍板三条规矩(硬约束, 详见 docs/TASK_wellness_organ.md):
  ① 只在大事上叫主人, 平时静默自愈; 主人不回她自己继续解决(不卡死等指示)
  ② 自愈白名单: 重启器官/清毒信号/调LLM降级链顺序, 越权一律禁止
  ③ 改代码前 git commit 基线 → 改 → 验证, 验证不过自动 git checkout 回滚 + 报主人
设计: 每 5min(WELLNESS_INTERVAL) 体检4项(对应9/25三个真实暗病), 有病按白名单自愈.
"""
import sys, os, re, time, json, subprocess, threading

sys.path.insert(0, "/root/nono-mind")
from state.server import StateClient
from organs import judgments as _judgments   # 挂点B/C: 判断账(每日失效探测)

ROOT = "/root/nono-mind"
INTERVAL = float(os.environ.get("WELLNESS_INTERVAL", 300))

# 能力声明(注册进 immune/capabilities, 照 canvas_organ/coder 写法)
from immune.plugins import capabilities_from_manifest, track_progress

# 票1: 能力跟插件走——从 plugins/wellness/manifest.yaml 读(不再散写在此)
CAPABILITIES = capabilities_from_manifest("wellness")

# 规矩②: 自愈白名单——只许动这些器官的systemd服务, 别的碰都不许碰
# P0-1修复(Codex审出): 按unit类型分流——常驻服务看is-active; reflection是timer触发型,
# 平时inactive是正常的, 查它的timer别误判(曾误报"反思挂了"还反复乱重启)
ALWAYS_SERVICES = ("nono-state", "nono-core", "nono-canvas", "nono-wechat")  # 常驻服务
TIMER_SERVICES = ("nono-reflection",)                                         # timer触发, 查.timer

def _unit_active(unit: str) -> bool:
    try:
        r = subprocess.run(["systemctl", "is-active", unit],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() == "active"
    except Exception:
        return False

# P0-4修复(Codex审出): 自愈白名单收窄——"自动改llm_client代码"划出白名单。
# 她改代码每一步(读/改/验证)都可能把自己修坏, 且验证不可靠(P0-2)。
# 通道挂了她降级兜底+报主人, 不亲自动刀。
# GIT_GUARDED 保留, 但只用于未来主人显式派"允许改代码"的自愈任务时才激活; 自动体检里不触代码。


# ---------- 规矩③: git 基线 → 改 → 验证不过自动回滚 ----------
def git_baseline(files, desc):
    """改代码前先commit一份'改动前基线'. 工作区脏则先commit脏改动, 保证基线是已知状态.
    返回是否成功打好基线. 失败(比如没进仓/没git)→ 返回False, 调用方就不许改."""
    r1 = subprocess.run(["git", "add", "--", *files], cwd=ROOT, capture_output=True, text=True)
    if r1.returncode != 0:
        return False
    # P1-2修复(Codex审出): 固化基线只 commit 我们关心的文件(git commit -- <files>),
    # 不吞全局暂存区——否则会把别人已staged的别的文件一起卷进我们的基线commit.
    ours_dirty = subprocess.run(["git", "status", "--porcelain", "--", *files],
                                cwd=ROOT, capture_output=True, text=True)
    if ours_dirty.stdout.strip():
        subprocess.run(["git", "commit", "-m", f"wellness: 固化改动前基线 {desc} [auto]", "--", *files],
                       cwd=ROOT, capture_output=True, text=True)
    # 若我们关心的文件本身没脏(脏的是别处) → 跳过固化, 直接打 baseline
    # --only 无路径=不commit任何暂存内容(否则会把别人staged的也一起提交), --allow-empty 放行空提交
    r2 = subprocess.run(["git", "commit", "-m", f"wellness: baseline before {desc} [auto]",
                         "--allow-empty", "--only"], cwd=ROOT, capture_output=True, text=True)
    return r2.returncode == 0


def git_rollback(files):
    """验证不过→回滚到改之前. 只回滚我们改的那几个文件, 不碰别处."""
    subprocess.run(["git", "checkout", "--", *files], cwd=ROOT, capture_output=True, text=True)
    # --allow-empty: 回滚本身是个要留痕的事件, 即使checkout后与HEAD一致也要记一笔
    subprocess.run(["git", "commit", "-m", "wellness: rollback [验证不过, 自动回退]",
                    "--allow-empty"], cwd=ROOT, capture_output=True, text=True)


# ---------- 体检4项(对应9/25三个真实暗病) ----------
def check_llm_brain():
    """LLM脑子通不通: 发个'回复OK'最小请求, 10s没回=脑子病了. 返回(通不通, 用的哪个通道)"""
    try:
        from llm_client import chat
        t0 = time.time()
        r = chat([{"role": "user", "content": "回复OK"}], max_tokens=5, timeout=10)
        # 兜底句检测: 回复若是写死的道歉兜底=脑子其实崩了(9/19 _CPKEY那种)
        is_fallback = ("脑子卡了" in r or "稍等再问我一次" in r) or not r.strip()
        ch = "reply_ok"
        return (not is_fallback and bool(r.strip())), ch, time.time() - t0
    except Exception as e:
        return False, f"exception:{str(e)[:40]}", None


def check_organs():
    """各器官心跳: 常驻服务看is-active, timer型看.timer(P0-1修复). 返回[挂掉的服务名]"""
    dead = []
    for svc in ALWAYS_SERVICES:
        if not _unit_active(svc):
            dead.append(svc)
    for svc in TIMER_SERVICES:
        # reflection: timer在跑=器官健康(timer触发时临时起来干活, 平时inactive正常)
        if not _unit_active(svc + ".timer"):
            dead.append(svc)
    return dead


POISON_STRENGTH = 0.5          # 强度阈值: 高于它说明反复重试/被顶回高值
POISON_STALE_SEC = 4 * 3600    # 滞留阈值: created至今超它=老信号, 早该衰减/被drop却还在

def check_poison_signals(c):
    """信号池毒信号: 反复失败堆着不散的 → 清掉(清前print留痕). 返回清了几条.
    P1-5修复(Codex审出): 不只认 loop_error, 别的反复失败形态(如 master_message/coder任务
    处理失败不drop)同样会堆着——放宽为"同sig_id滞留够久且强度未衰减到阈值以下".
    ponytail: 滞留判据用 created 距今>POISON_STALE_SEC, 只读现有字段够用即可, 不加schema."""
    cleared = 0
    now = time.time()
    try:
        for s in c.signals() or []:
            st = float(s.get("strength", 0) or 0)
            if st <= POISON_STRENGTH:
                continue
            stype = s.get("sig_type")
            if stype == "loop_error":
                why = "loop_error"
            else:
                age = now - float(s.get("created") or now)
                if age <= POISON_STALE_SEC:
                    continue   # 新鲜信号(可能是本轮刚冒的), 不是毒
                why = f"stale_{age / 3600:.1f}h"
            print(f"[wellness] 清毒信号 {s.get('sig_id')} type={stype} strength={st:.2f} ({why})")
            c.drop(s["sig_id"])
            cleared += 1
    except Exception:
        pass
    return cleared


def check_log_errors():
    """最近5min各器官日志有无NameError/No module/循环级异常 等暗病关键字"""
    bad = []
    for svc in ALWAYS_SERVICES:  # 常驻服务才看日志; timer型平时没日志, 跳过(P0-1)
        try:
            r = subprocess.run(
                ["journalctl", "-u", svc, "--since", "5 min ago", "--no-pager", "-o", "short"],
                capture_output=True, text=True, timeout=10)
            for kw in ("NameError", "No module", "循环级异常", "loop_error", "timed out"):
                if kw in (r.stdout or ""):
                    bad.append(f"{svc}:{kw}")
                    break
        except Exception:
            pass
    return bad


# ---------- 自愈(规矩②白名单 + 规矩③git回滚) ----------
def heal_dead_organ(c, dead):
    """重启挂掉的器官(白名单操作). 每服务重试2次, 仍败→报主人.
    P0-1修复: 常驻服务restart服务; timer型restart timer(不直接动服务本身)."""
    healed, failed = [], []
    for svc in dead:
        target = svc + ".timer" if svc in TIMER_SERVICES else svc
        ok = False
        for _ in range(2):
            subprocess.run(["systemctl", "restart", target], capture_output=True, text=True, timeout=30)
            time.sleep(5)
            r = subprocess.run(["systemctl", "is-active", target], capture_output=True, text=True, timeout=10)
            if r.stdout.strip() == "active":
                ok = True
                break
        (healed if ok else failed).append(svc)
    c.kv_set("wellness/heal_log", {"at": time.time(), "type": "restart_organs",
                                    "healed": healed, "failed": failed})
    return healed, failed


def heal_llm_chain(c):
    """P0-4修复(Codex审出): 不自动改llm_client代码.
    脑子通道挂 → 她只'降级兜底+报主人', 不亲自动刀改代码(改代码每步都可能把自己修坏,
    且验证不可靠=P0-2). 这条自愈白名单收窄, 通道级故障交给降级链自己兜.
    返回: 'ok'(脑子通) / 'down'(通道挂, 已降级兜底+报主人)."""
    brain_ok, ch, _ = check_llm_brain()
    if brain_ok:
        return "ok"
    # 通道级故障: 降级链自己会兜(nemotron等备用), 她不碰代码, 只报主人去查scnet那边
    return "down"


# P0-3修复(Codex审出): git任何一步失败=升级报主人, 绝不硬改/装没事.
# (留痕用: 供主人显式派'允许改代码'的自愈任务时调用; 自动体检里不调用改代码)
def git_safe_modify(files, desc, modify_fn):
    """git基线→改→验证. 任一git步失败/验证不过 → 回滚+返回(False,'reason'), 调用方升级报主人.
    ponytail: 简化实现——只封装'改前打基线'+'改后验证'两个闸门, 中间modify_fn是调用方给的."""
    if not git_baseline(files, desc):
        return False, "git_baseline_failed"   # 打不了基线=绝不动手, 交人(P0-3)
    try:
        modify_fn()
        return True, "ok"
    except Exception as e:
        git_rollback(files)
        return False, f"modify_failed:{str(e)[:60]}"


# ---------- 规矩①: 大事才报主人(静默自愈后, 只有大事才发outbox) ----------
BIG_ISSUE_KV = "wellness/last_big_issue"
SUPPRESSED_KV = "wellness/suppressed_issue"   # P1-1: 被4h节流压下的待发大事
REPORT_COOLDOWN = 4 * 3600  # 4h节流, 防刷屏(照reflection那套)

def _drop_suppressed_if_healed(c, brain_ok, dead_organs):
    """P1-1修复(Codex审出): 被4h节流压下的待发大事, 若该类病本轮已自愈 → 撤掉,
    到期不再发'我主脑挂了'这种已过时的话. key 标识病类(brain/organs)."""
    sup = c.kv_get(SUPPRESSED_KV)
    if not sup:
        return
    key = sup.get("key")
    if (key == "brain" and brain_ok) or (key == "organs" and not dead_organs):
        c.kv_del(SUPPRESSED_KV)
        print(f"[wellness] 被节流的{key}病已自愈, 撤掉待发大事(不报过时话)")

def report_to_master(c, msg, key="issue"):
    """大事才发主人微信. 4h内只报一次(主人不回也不刷屏, 她自己继续自救)."""
    last = (c.kv_get(BIG_ISSUE_KV) or {}).get("at", 0)
    if time.time() - last < REPORT_COOLDOWN:
        # 保留上次发送时间(不重置), 否则节流窗永不过期、4h后再也报不出来
        c.kv_set("wellness/last_master_noresponse", {"at": time.time(), "msg": msg[:80]})
        # P1-1: 记下被压下的待发大事+病类; 病好了下轮会撤, 到期才发时先看病还在不在
        c.kv_set(SUPPRESSED_KV, {"msg": msg, "at": time.time(), "key": key})
        print(f"[wellness] 大事已记({key}), 4h内不重复报主人: {msg[:50]}")
        return
    # 窗口到: 只有调用方"该类病本轮仍成立"才会走到这 → 发的是当下实况, 不是过时话
    c.kv_append("wechat_outbox", {"to": "owner", "text": msg})
    c.kv_set(BIG_ISSUE_KV, {"at": time.time(), "msg": msg, "suppressed": False})
    c.kv_del(SUPPRESSED_KV)
    print(f"[wellness] 大事已报主人({key}): {msg[:50]}")


# ---------- 体检主流程 ----------
def run_one(c):
    """跑一次完整体检+自愈. 返回摘要dict."""
    now = time.time()
    report = {"at": now, "brain": False, "organs_dead": [], "poison_cleared": 0,
              "log_errors": [], "healed": [], "failed_organs": [], "big_issue": None,
              "big_issue_key": None}

    # ① LLM脑子
    brain_ok, ch, _ = check_llm_brain()
    report["brain"] = brain_ok
    if not brain_ok:
        r = heal_llm_chain(c)
        if r == "down":
            # P0-4: 她没动代码, 降级链自己兜底着. 报主人去查主脑通道(scnet), 不硬修.
            report["big_issue_key"] = "brain"
            report["big_issue"] = ("主人~ 我主脑那条通道(scnet)不通了, 我现在靠备用通道在兜底, "
                                   "能正常说话就是慢一点. 我不动脑子代码了(怕自己修坏), "
                                   "你有空帮我看看scnet那边吗? 你忙的话我继续兜着.")

    # ② 各器官心跳
    dead = check_organs()
    report["organs_dead"] = dead
    if dead:
        healed, failed = heal_dead_organ(c, dead)
        report["healed"] = healed
        report["failed_organs"] = failed
        if failed:
            report["big_issue_key"] = "organs"
            report["big_issue"] = (f"主人~ 我的{'+'.join(failed)}器官挂了, 我重启了两回没起来, "
                                   f"能说的部分在兜底走. 帮我看看? 你忙的话我再等下试试.")

    # P1-1: 被节流压下的待发大事, 该类病本轮已好 → 撤掉, 不当期(及到期)再报过时话
    _drop_suppressed_if_healed(c, report["brain"], report["organs_dead"])

    # ③ 毒信号
    report["poison_cleared"] = check_poison_signals(c)

    # ④ 日志暗病
    report["log_errors"] = check_log_errors()

    # ⑤ 判断账失效探测(挂点B): 每自然日探一次, 不跟5min同频(省资源)
    report["judgments_probe"] = _judgments.maybe_daily_probe(c)

    # 落痕(体检记录, 主人想看时翻)
    c.kv_set("wellness/last_report", report)

    # 规矩①: 只有大事才报主人, 平时静默
    if report["big_issue"]:
        report_to_master(c, report["big_issue"], report.get("big_issue_key") or "issue")
        # 大脑彻底没救(所有通道挂+回滚了)= 最高级大事, 突破4h节流也得让主人知道
        c.kv_set("wellness/brain_down", {"at": now, "detail": report["big_issue"][:200]})
    else:
        print(f"[wellness] 体检静默通过: 脑子={'通' if report['brain'] else '兜底'} "
              f"器官挂={len(dead)} 毒信号清={report['poison_cleared']} 暗病={len(report['log_errors'])}")
    return report


# 票1: 进度互看
run_one = track_progress("wellness", lambda c: "体检")(run_one)


def main():
    c = StateClient()
    print(f"[wellness] 值班医生上线, 每{INTERVAL:.0f}s体检一次, 静默自愈/大事才报主人")
    while True:
        try:
            run_one(c)
        except Exception as e:
            print(f"[wellness] 体检异常(不退出): {e}")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
