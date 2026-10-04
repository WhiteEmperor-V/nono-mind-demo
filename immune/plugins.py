#!/usr/bin/env python3
"""插件管理器(免疫系统): 扫描plugins/目录, 每插件独立子进程, 健康检查, 崩溃重启
规格: plugins/<name>/manifest.yaml + main.py
manifest字段: name/version/loop_type(continuous|scheduled|event)/subscribes/publishes/entry

票1新增(小loop拼装基础层): manifest还能声明 organ/plugins/provides/requires_skills/instances,
据此把能力注册进capabilities表(替代各器官散写的CAPABILITIES), 并支持按manifest开多实例。
四件套接口: init(state)/tick(state)/on_event(event)/health()->bool
"""
import os, json, time, subprocess, yaml, importlib, sys, signal, functools

PLUGINS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugins")
MANIFEST = "manifest.yaml"


# --------------------------------------------------------------------------
# 器官插件清单: 读manifest → 能力/所需skill/实例(票1基础层, 纯读取/注册, 不动核心loop行为)
# --------------------------------------------------------------------------
def manifest_path(name):
    return os.path.join(PLUGINS_DIR, name, MANIFEST)

def load_manifest(name):
    """读某个器官/插件的manifest. 不存在或读坏返回None(调用方决定兜底)."""
    p = manifest_path(name)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            mf = yaml.safe_load(f) or {}
    except Exception as e:
        print(f"[plugins] manifest读取失败 {name}: {e}")
        return None
    return mf if isinstance(mf, dict) else None

def discover_manifests():
    """扫plugins/*/manifest.yaml, 返回 {name: manifest}(坏文件跳过)."""
    out = {}
    if not os.path.isdir(PLUGINS_DIR):
        return out
    for name in sorted(os.listdir(PLUGINS_DIR)):
        mf = load_manifest(name)
        if mf:
            out[name] = mf
    return out

def provides_from_manifest(name):
    """manifest里各插件provides的并集(器官靠声明哪些插件就知道会什么)."""
    mf = load_manifest(name) or {}
    out = []
    for pl in mf.get("plugins", []) or []:
        if isinstance(pl, dict):
            for cap in pl.get("provides", []) or []:
                if cap not in out:
                    out.append(cap)
    return out

def requires_skills_from_manifest(name):
    """manifest里各插件requires_skills的并集(工作loop装配时要带上的skill)."""
    mf = load_manifest(name) or {}
    out = []
    for pl in mf.get("plugins", []) or []:
        if isinstance(pl, dict):
            for sk in pl.get("requires_skills", []) or []:
                if sk not in out:
                    out.append(sk)
    return out

def organ_for_provides(cap):
    """按manifest反向查: 哪个器官的插件provides提供了该能力(编排查能力用). 没找到返回None."""
    for name, mf in discover_manifests().items():
        organ = mf.get("organ") or name
        for pl in mf.get("plugins", []) or []:
            if isinstance(pl, dict) and cap in (pl.get("provides") or []):
                return organ
    return None

def capabilities_from_manifest(name):
    """manifest的capabilities段 → 填好organ的能力声明列表(供器官当CAPABILITIES用/注册).
    ponytail: 若manifest没写capabilities段但声明了provides(插件提供的能力), 也自动生成基础能力声明
    (让"她自己造插件只写provides"也能直接注册进能力表, 不要求造插件者手写capabilities全字段)."""
    mf = load_manifest(name) or {}
    organ = mf.get("organ") or name
    out = []
    for cap in mf.get("capabilities", []) or []:
        if isinstance(cap, dict) and cap.get("name"):
            cap = dict(cap)
            cap.setdefault("organ", organ)
            out.append(cap)
    # 没手写capabilities但声明了provides → 给每个provides能力生成一条基础声明
    if not out:
        provides = []
        for pl in mf.get("plugins", []) or []:
            if isinstance(pl, dict):
                provides += pl.get("provides", []) or []
        provides += mf.get("provides", []) or []
        for cap_name in dict.fromkeys(provides):
            # 自动生成的描述必须过validate(≥20字 + 含"何时不用"边界), 拼一句合规的
            out.append({
                "organ": organ, "name": cap_name,
                "description": (f"{mf.get('description', organ + '的能力')}。"
                                 f"何时用：需要{cap_name}相关能力时。"
                                 f"何时不用：与该能力无关的任务，交给对应器官。"),
                "params_schema": {}, "examples": [],
            })
    return out

def instances_from_manifest(name):
    """大loop能开哪些实例id. instances: N(整数) → <name>-1..<name>-N; 列表→原样; 缺省→[name]."""
    mf = load_manifest(name) or {}
    n = mf.get("instances", 1)
    if isinstance(n, list):
        return [str(x) for x in n]
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = 1
    return [name] if n <= 1 else [f"{name}-{i}" for i in range(1, n + 1)]

def register_capabilities_from_manifests(c):
    """把每个manifest声明的能力注册进capabilities表(幂等, 可反复调).
    启动扫一次 + 她自己现造插件后现场重扫一次(动态注册, 不靠重启). 返回(ok, total)."""
    from immune import capabilities as _caps
    ok = total = 0
    for name, mf in discover_manifests().items():
        for cap in capabilities_from_manifest(name):
            total += 1
            try:
                good, err = _caps.register(c, cap)
            except Exception as e:
                good, err = False, str(e)
            if good:
                ok += 1
            elif err:
                print(f"[plugins] 能力注册被拒 {name}/{cap.get('name')}: {err}")
    return ok, total

def rescan_and_register(c):
    """动态注册入口: 她现造了插件后调这个, 重新扫manifest把新能力注册进表(不用重启core).
    返回本次新发现并注册的能力数(0=没变化, >0=有新插件/新能力). ponytail: 全量重扫(目录才8个, 毫秒级),
    不做增量diff; 升级路径=按mtime记录只扫新增目录."""
    from immune import capabilities as _caps
    before = {cap.get("name") for caps in (c.kv_list(_caps.KV_NS) or {}).values()
              if isinstance(caps, list) for cap in caps}
    ok, total = register_capabilities_from_manifests(c)
    after = {cap.get("name") for caps in (c.kv_list(_caps.KV_NS) or {}).values()
             if isinstance(caps, list) for cap in caps}
    return len(after - before), total

def write_progress(c, self_name, task, pct=0, stuck=""):
    """小loop进度写进 states/<自己>/progress(所有loop可读, 零新协议). 返回写入的记录."""
    rec = {"task": str(task)[:200], "pct": int(pct), "stuck": stuck, "at": time.time()}
    try:
        c.kv_set(f"states/{self_name}/progress", rec)
    except Exception as e:
        print(f"[plugins] 写进度失败 {self_name}: {e}")
    return rec

def read_progress(c, self_name):
    """读某个小loop的进度(互看用)."""
    return c.kv_get(f"states/{self_name}/progress")

def read_judgments(c, active_only=True):
    """公共记忆层: 读判断账(judgments/), 所有loop可读. 供小loop开工前对齐大loop的判断."""
    out = []
    for _k, j in (c.kv_list("judgments/") or {}).items():
        if isinstance(j, dict) and (not active_only or j.get("status") == "active"):
            out.append(j)
    return out

def _find_client(args, kwargs):
    for x in list(args) + list(kwargs.values()):
        if hasattr(x, "kv_set"):
            return x
    return None

def track_progress(self_name, task_getter=None):
    """装饰器: 器官入口自动写 states/<自己>/progress(开工pct1, 结束pct100, 异常记stuck).
    task_getter(*args,**kwargs) 可选, 返回任务描述字符串."""
    def deco(fn):
        @functools.wraps(fn)
        def wrap(*a, **k):
            c = _find_client(a, k)
            task = ""
            if task_getter is not None:
                try:
                    task = task_getter(*a, **k) or ""
                except Exception:
                    task = ""
            if c:
                write_progress(c, self_name, task, 1)
            try:
                r = fn(*a, **k)
            except Exception as e:
                if c:
                    write_progress(c, self_name, task, 100, stuck=str(e)[:120])
                raise
            if c:
                write_progress(c, self_name, task, 100)
            return r
        return wrap
    return deco



class PluginHandle:
    """一个已加载插件的运行时句柄"""
    def __init__(self, name, manifest, proc=None, module=None, last_heartbeat=None):
        self.name, self.manifest, self.proc = name, manifest, proc
        self.module, self.last_heartbeat = module, last_heartbeat or time.time()
        self.restarts = 0
    def healthy(self) -> bool:
        if self.proc is not None:
            return self.proc.poll() is None
        if self.module and hasattr(self.module, "health"):
            try: return bool(self.module.health())
            except Exception: return False
        return (time.time() - self.last_heartbeat) < 300

class PluginManager:
    def __init__(self, state_client, plugins_dir=PLUGINS_DIR):
        self.c = state_client
        self.dir = plugins_dir
        self.plugins = {}

    def discover(self):
        """扫描目录, 返回发现的插件名"""
        found = []
        if not os.path.isdir(self.dir): return found
        for name in sorted(os.listdir(self.dir)):
            mpath = os.path.join(self.dir, name, MANIFEST)
            if os.path.isfile(mpath):
                found.append(name)
        return found

    def load(self, name):
        """加载插件(子进程模式: continuous型; 进程内模式: event/scheduled型)"""
        pdir = os.path.join(self.dir, name)
        mf = yaml.safe_load(open(os.path.join(pdir, MANIFEST)))
        handle = PluginHandle(name, mf)
        entry = mf.get("entry", "main.py")
        if mf.get("loop_type") == "continuous":
            # 子进程隔离: 崩溃不传染
            handle.proc = subprocess.Popen(
                [sys.executable, os.path.join(pdir, entry)],
                cwd=pdir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            sys.path.insert(0, pdir)
            handle.module = importlib.import_module(entry[:-3] if entry.endswith(".py") else entry)
            if hasattr(handle.module, "init"):
                handle.module.init(self.c)
        self.plugins[name] = handle
        self.c.emit("task_done", {"organ": "plugin_manager",
                                  "result": f"插件{name}已加载"}, decay=0.2, source="immune")
        return handle

    def unload(self, name):
        h = self.plugins.pop(name, None)
        if h and h.proc:
            h.proc.terminate()
            try: h.proc.wait(timeout=5)
            except: h.proc.kill()

    def health_check(self):
        """白细胞: 不健康插件杀掉重启(最多3次, 防僵尸循环)"""
        report = []
        for name, h in list(self.plugins.items()):
            if not h.healthy():
                if h.restarts < 3:
                    self.unload(name)
                    h2 = self.load(name)
                    h2.restarts = h.restarts + 1
                    self.plugins[name] = h2
                    report.append(f"{name}:重启({h2.restarts})")
                else:
                    report.append(f"{name}:放弃(3次)")
                    self.unload(name)
        return report

    def run_all_once(self):
        """event/scheduled型插件执行一次tick"""
        out = []
        for name, h in self.plugins.items():
            if h.module and hasattr(h.module, "tick"):
                try:
                    out.append((name, h.module.tick(self.c)))
                except Exception as e:
                    out.append((name, f"error: {e}"))
        return out
