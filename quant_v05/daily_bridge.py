#!/usr/bin/env python3
"""TradingAgents-CN 报告 -> 模拟盘 每日桥接 v0.5（信号硬校验层）

基于原版 daily_bridge 核心逻辑（必须保留）：
  - parse_signal(report): 从 MongoDB 报告解析 symbol/action/target_price/confidence
  - main(): pymongo 连接 tradingagentscn.analysis_reports -> 最近 20 条 -> 逐条解析
            -> BUY/SELL 入队 add_signal -> processed_reports 去重
  - state 文件 /opt/quant-pipeline/paper/state.json; SELL 无持仓忽略
  - cron 兼容: 17:00 兜底(默认 open 口径); 15:30 收盘设 BRIDGE_PRICE=close 用 close 口径

v0.5 新增（硬校验层）:
  1. parse_signal 扩展: 聚合 recommendation/summary/final_report 等所有文本字段,
     正则解析 持有期(日内|波段|中线) 与 止损位(止损[位]?[：:]?数字)
  2. 硬校验: BUY 必须同时有【持有期】+【止损位】, SELL 至少【持有期】; 否则打回
  3. 打回机制: 本地 API 重新分析(POST /api/auth/login -> /api/analysis/single,
     research_depth=深度 + 强化 prompt), 每报告最多重析 1 次
     (MongoDB remediation_done 字段标记), 重析后仍不合格 -> 放弃 + 日志
  4. 全部处理结果 append 到 /opt/quant-pipeline/paper/signal_validation.log
"""
import sys, os, re, json, time, urllib.request
from datetime import datetime

sys.path.insert(0, "/opt/quant-pipeline/paper")

# ---------------- 配置（环境变量可覆盖） ----------------
DB_URI = os.environ.get(
    "QUANT_DB_URI",
    os.environ.get("MONGO_URI", "mongodb://admin:tradingagents123@127.0.0.1:27017/?authSource=admin"),
)
STATE_FILE = os.environ.get("QUANT_STATE_FILE", "/opt/quant-pipeline/paper/state.json")
VALIDATION_LOG = os.environ.get("QUANT_VALIDATION_LOG", "/opt/quant-pipeline/paper/signal_validation.log")
API = os.environ.get("QUANT_API", "http://localhost:8000")
USER, PASS = "admin", "admin123"
DB_NAME = "tradingagentscn"
COLL = "analysis_reports"
MAX_BACKDAYS = 5
BRIDGE_PRICE = os.environ.get("BRIDGE_PRICE", "open")  # 15:30=close / 17:00=open
LOCK_FILE = "/tmp/daily_bridge.lock"
REMEDIATION_FIELD = "remediation_done"

REINFORCE_PROMPT = (
    "【上次输出不合格】缺少持有期/止损位，本次必须遵守硬校验格式：\n"
    "1. 结论第一行必须输出：信号:买入|卖出 | 持有期:日内|波段|中线 | 止损:XX.XX元 | 依据:XX\n"
    "2. 买入信号必须给出止损位（明确数字价格，单位元）；卖出信号至少给出持有期\n"
    "3. 其余分析内容按正常报告组织，不得与结论格式矛盾。"
)


def _load_db_uri():
    """MongoDB 连接串：环境变量 -> /tmp 切换脚本（同原版） -> 默认值"""
    if os.environ.get("QUANT_DB_URI") or os.environ.get("MONGO_URI"):
        return DB_URI
    for p in ("/tmp/switch_newapi.py", "/tmp/switch_bifrost.py"):
        if os.path.exists(p):
            m = re.search(r'uri = "([^"]+)"', open(p).read())
            if m:
                return m.group(1)
    return DB_URI


def _log(msg):
    """打印到 stdout，并 append 到 signal_validation.log"""
    try:
        print(msg)
        with open(VALIDATION_LOG, "a") as f:
            f.write("%s %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


# ---------------- 解析 ----------------
def _all_text(report):
    """聚合报告所有文本字段（recommendation/summary/final_report/嵌套报告等）"""
    keys = ("recommendation", "summary", "final_report", "report", "content",
            "conclusion", "final_decision", "consensus", "decision", "analysis",
            "text", "analyst_reports")
    parts = []

    def _walk(v):
        if isinstance(v, str):
            if v.strip():
                parts.append(v.strip())
        elif isinstance(v, dict):
            for val in v.values():
                _walk(val)
        elif isinstance(v, (list, tuple)):
            for it in v:
                _walk(it)

    for k in keys:
        _walk(report.get(k))
    return "\n".join(parts)


def _holding_period(full_text):
    m = re.search(r"(日内|波段|中线)", full_text or "")
    return m.group(1) if m else None


def _stop_loss(full_text):
    # 止损[位/价]? [冒号/空格] 价格数字（排除 10% 这类百分比写法与截断匹配）
    m = re.search(r"止损[位价]?\s*[：:]?\s*(\d+(?:\.\d+)?)(?!\s*%|\d)", full_text or "")
    return float(m.group(1)) if m else None


def parse_signal(report):
    """(symbol, action, target_price, confidence)。
    保持原版核心：rec/summary 关键词判 action，目标价/confidence_score 读取。"""
    rec = str(report.get("recommendation") or "")
    summary = str(report.get("summary") or "")

    # symbol：结构化字段优先，其次文本 6 位代码
    symbol = report.get("stock_symbol") or report.get("symbol") or report.get("code")
    if symbol is not None:
        symbol = str(symbol).strip()
        m = re.search(r"(\d{6})", symbol)
        symbol = m.group(1) if m else (symbol if re.fullmatch(r"\d{6}", symbol) else None)
    if not symbol:
        for field in (rec, summary):
            m = re.search(r"[（(]?(\d{6})[）)]?", field)
            if m:
                symbol = m.group(1)
                break

    # action：文本优先（原版行为），结构化字段兜底
    action = "HOLD"
    if re.search(r"卖出|减仓|回避|清仓|sell", rec, re.I):
        action = "SELL"
    elif re.search(r"买入|建仓|加仓|买进|buy", rec, re.I):
        action = "BUY"
    else:
        a = str(report.get("action") or report.get("signal") or "").strip().upper()
        if a in ("BUY", "SELL"):
            action = a
        elif "卖" in a:
            action = "SELL"
        elif "买" in a:
            action = "BUY"

    # 目标价：区间取中间值，单值直取（原版兼容）
    full = _all_text(report)
    tp = None
    m = re.search(r"目标价[格]?[：: ]?\s*([\d.]+)\s*[-~至到]\s*([\d.]+)", full)
    if m:
        tp = (float(m.group(1)) + float(m.group(2))) / 2.0
    else:
        m = re.search(r"目标价[格]?[：: ]?\s*([\d.]+)", full)
        if m:
            tp = float(m.group(1))

    conf = report.get("confidence_score")
    return symbol, action, tp, conf


def validate_signal(symbol, action, full_text):
    """硬校验：(ok, holding, stop_loss, reason)。
    BUY 必须同时有持有期+止损位；SELL 至少持有期。"""
    holding = _holding_period(full_text)
    sl = _stop_loss(full_text)
    if action == "BUY":
        if holding and sl:
            ok, reason = True, "通过"
        elif not holding and not sl:
            ok, reason = False, "缺少持有期与止损位"
        elif holding:
            ok, reason = False, "缺少止损位"
        else:
            ok, reason = False, "缺少持有期"
    else:
        ok, reason = (True, "通过") if holding else (False, "缺少持有期")
    return ok, holding, sl, reason


def _mark_processed(state, aid):
    pro = set(state.get("processed_reports", []))
    pro.add(aid)
    state["processed_reports"] = list(pro)


def _save_state(pt, state):
    try:
        pt.s["processed_reports"] = state.get("processed_reports", [])
        pt.s["remediation"] = state.get("remediation", {})
        pt.save()
    except Exception as e:
        _log("[bridge] state 保存失败: %s" % e)


def _remediation_key(symbol, action, rday):
    """同一 symbol+action+报告日期只打回一次（防同日多报告重复触发）"""
    return "%s|%s|%s" % (symbol, action, rday)


def _remediation_record(state, symbol, action, rday, task_id):
    key = _remediation_key(symbol, action, rday)
    state.setdefault("remediation", {})[key] = {
        "task_id": task_id,
        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _remediation_entry(state, symbol, action, rday):
    key = _remediation_key(symbol, action, rday)
    return state.setdefault("remediation", {}).get(key)


# ---------------- 打回重析（本地 API） ----------------
def _api(method, path, data=None, token=None, retries=2):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    body = json.dumps(data).encode() if data is not None else None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(API + path, data=body, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            if i >= retries:
                print("WARN %s %s 失败: %s" % (method, path, e))
                return None
            time.sleep(2)


def _login_token():
    r = _api("POST", "/api/auth/login", {"username": USER, "password": PASS})
    if not r:
        return None
    d = r.get("data") or {}
    return d.get("access_token") or d.get("token") or r.get("access_token") or r.get("token")


def _mark_remediation_done(client, rpt):
    """MongoDB remediation_done=True：重析后的新报告不得再次重析"""
    try:
        coll = client[DB_NAME][COLL]
        rid = rpt.get("_id")
        if rid is not None:
            coll.update_one({"_id": rid}, {"$set": {REMEDIATION_FIELD: True}}, upsert=False)
    except Exception as e:
        print("WARN remediation_done 标记失败: %s" % e)


def _report_time(rpt):
    """报告入库时间 -> datetime（naive；仅用于“是否新报告”粗判）"""
    t = rpt.get("created_at") or rpt.get("timestamp") or rpt.get("created")
    if isinstance(t, datetime):
        return t
    if isinstance(t, str) and t.strip():
        s = t.strip().replace("Z", "+00:00").replace("T", " ")
        try:
            return datetime.fromisoformat(s[:19])
        except Exception:
            pass
    return None


def _report_day(rpt):
    """报告所属自然日（yyyy-mm-dd）：created_at 优先，_id 兜底（本地时区）"""
    t = _report_time(rpt)
    if t is not None:
        return t.strftime("%Y-%m-%d")
    try:
        return datetime.fromtimestamp(float(rpt.get("_id").generation_time.timestamp())).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")


def _build_reinforce_prompt(symbol, rpt):
    """原侦查 prompt（取报告自带 custom_prompt，缺则通用模板）+ 强化要求"""
    base = rpt.get("custom_prompt") or rpt.get("prompt") or ""
    if not base:
        base = (
            "【个股分析】请对股票 %s 出具完整投资报告：先综合评估大盘/板块/个股"
            "的战场态势（资金、消息、技术面），再给出明确信号、预期持有期、止损位与依据。" % symbol
        )
    return base + "\n" + REINFORCE_PROMPT


def _submit_remediation(client, token, symbol, rpt):
    """打回：POST /api/analysis/single 重新提交分析。提交成功后标记 remediation_done 并返回 task_id"""
    if rpt.get(REMEDIATION_FIELD):
        return None
    payload = {"symbol": symbol, "parameters": {
        "research_depth": "深度",
        "custom_prompt": _build_reinforce_prompt(symbol, rpt),
        "selected_analysts": ["market", "fundamentals", "news", "social"],
    }}
    resp = _api("POST", "/api/analysis/single", payload, token=token)
    if not resp or not resp.get("success"):
        _log("[打回] %s 重析提交失败: %s" % (symbol, str(resp)[:80]))
        return None
    task_id = (resp.get("data") or {}).get("task_id") or resp.get("task_id")
    _mark_remediation_done(client, rpt)
    _log("[打回] %s 硬校验不合格，已重析提交 task_id=%s（每报告仅 1 次）" % (symbol, task_id))
    return task_id


def _fetch_report_by_symbol(client, symbol):
    """查该 symbol 最新一份报告（新报告_id 最大，即重析产物）"""
    try:
        cur = client[DB_NAME][COLL].find(
            {"$or": [{"symbol": symbol}, {"stock_symbol": symbol}]}
        ).sort("_id", -1).limit(1)
        docs = list(cur)
        return docs[0] if docs else None
    except Exception:
        return None


def _poll_remediation_done(client, token, symbol, task_id, since_dt, old_id):
    """同步等待重析完成（最多约 6 分钟）并取最新报告。
    返回 (fresh_report|None, status) status in completed/failed/timeout/api_error"""
    last_status = ""
    for _ in range(24):
        time.sleep(15)
        st = _api("GET", "/api/analysis/tasks/%s/status" % task_id, token=token)
        if not st:
            continue
        d = st.get("data") or {}
        status = d.get("status") or st.get("status") or ""
        last_status = status
        if status in ("completed", "success", "succeeded"):
            time.sleep(2)  # 等报告落库
            fresh = _fetch_report_by_symbol(client, symbol)
            if fresh is not None:
                if old_id is None or str(fresh.get("_id")) != str(old_id):
                    # 新报告（新 _id）或原地更新后的报告（时间比提交晚）
                    try:
                        rt = _report_time(fresh)
                        if rt is not None and since_dt is not None and rt < since_dt:
                            return None, "completed_stale"  # 仍是旧报告，下次 cron 再兜
                    except TypeError:
                        pass
                    return fresh, "completed"
            return None, "completed_stale"  # 任务完成但报告尚未落库，下次 cron 兜
        if status in ("failed", "error"):
            _log("[放弃] %s 重析任务失败: %s" % (symbol, str(d.get("error_message"))[:80]))
            return None, "failed"
    _log("[放弃] %s 重析等待超时（status=%s），报告留待下次 cron 校验" % (symbol, last_status or "?"))
    return None, "timeout"


# ---------------- 信号入队 ----------------
def _queue_signal(pt, state, today, symbol, action, tp, conf, reason):
    """原版入队：SELL 无持仓忽略"""
    if action == "SELL" and symbol not in state.get("positions", {}):
        _log("SKIP %s SELL 但无持仓，忽略" % symbol)
        return False
    pt.add_signal(today, symbol, action, target_price=tp, confidence=conf, reason=reason)
    _log("OK 信号入队: %s %s（硬校验通过）" % (symbol, action))
    return True


# ---------------- 主流程 ----------------
def main():
    _log("[bridge] 启动 mode=BRIDGE_PRICE=%s time=%s" % (BRIDGE_PRICE, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

    # 单实例锁（重析可能跑较久，避免与下一次 cron 重叠）
    try:
        pid = int(open(LOCK_FILE).read().strip() or "0")
        os.kill(pid, 0)
        _log("[bridge] 已有实例运行中，本次跳过")
        return
    except FileNotFoundError:
        pass
    except (ProcessLookupError, ValueError):
        pass
    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))
    try:
        _run()
    finally:
        try:
            os.unlink(LOCK_FILE)
        except OSError:
            pass


def _run():
    try:
        import pymongo
        from paper_trading import PaperTrading
        from data_source import get_daily_ohlc
    except Exception as e:
        _log("[bridge] 依赖导入失败: %s（需在 Server3 /opt/quant-pipeline/paper 运行）" % e)
        return

    token = _login_token()
    if not token:
        _log("[bridge] API 登录失败，终止")
        return

    client = pymongo.MongoClient(_load_db_uri(), serverSelectionTimeoutMS=8000)
    db = client[DB_NAME]
    reports = list(db[COLL].find().sort("_id", -1).limit(20))  # 原版：最近 20 条
    pt = PaperTrading(state_file=STATE_FILE)
    state = pt.s  # 复用同一份 state：processed_reports / positions / pending_signals
    if "processed_reports" not in state:
        state["processed_reports"] = []
    today0 = datetime.now().strftime("%Y-%m-%d")

    new_count = 0
    queued_ids = set()  # 本轮打回已入队的重析产物，防止主扫描重复处理
    for rpt in reports:
        aid = str(rpt.get("analysis_id") or rpt.get("_id"))
        if aid in queued_ids or aid in set(state.get("processed_reports", [])):
            continue
        # 若重析原地覆盖了本报告(_id 相同)，也按已处理跳过
        if str(rpt.get("_id")) in queued_ids or str(rpt.get("_id")) in set(state.get("processed_reports", [])):
            continue
        new_count += 1
        symbol, action, tp, conf = parse_signal(rpt)
        today = today0
        rday = _report_day(rpt)

        if symbol and action in ("BUY", "SELL"):
            ok, hold, sl, reason = validate_signal(symbol, action, _all_text(rpt))
            if ok:
                _log("[通过] %s %s 持有期=%s 止损位=%s，信号入队" % (symbol, action, hold, sl))
                _queue_signal(pt, state, today, symbol, action, tp, conf, reason)
            elif rpt.get(REMEDIATION_FIELD):
                # 该报告已打回重析过一次，二次校验仍不合格 → 放弃
                _log("[放弃] %s %s 已重析过仍不合格，放弃: %s" % (symbol, action, reason))
            elif _remediation_entry(state, symbol, action, rday):
                # 同日同标的已打回过一次（本报告若为重析产物）→ 不再打回，直接放弃
                _log("[放弃] %s %s 重析产物仍不合格，放弃: %s" % (symbol, action, reason))
            else:
                # 硬校验不通过 → 打回重析（该报告仅 1 次，remediation_done 落库防重）
                _log("[打回] %s %s 硬校验不通过: %s，提交重析（每报告仅 1 次）" % (symbol, action, reason))
                since_dt = datetime.now()
                task_id = _submit_remediation(client, token, symbol, rpt)
                if task_id:
                    _remediation_record(state, symbol, action, rday, task_id)
                    fresh, st = _poll_remediation_done(
                        client, token, symbol, task_id, since_dt, str(rpt.get("_id")))
                    if fresh is not None:
                        s2, a2, tp2, c2 = parse_signal(fresh)
                        ok2, hold2, sl2, reason2 = validate_signal(s2, a2, _all_text(fresh))
                        if ok2 and s2 and a2 in ("BUY", "SELL"):
                            _log("[通过] %s %s 重析产物校验通过（持有期=%s 止损位=%s），信号入队"
                                 % (s2, a2, hold2, sl2))
                            _queue_signal(pt, state, today, s2, a2, tp2, c2,
                                          "重析报告 %s" % str(fresh.get("analysis_id") or fresh.get("_id")))
                            # 重析产物本次已入队 → 直接标记 processed，防止下次 cron 重复
                            queued_ids.add(str(fresh.get("analysis_id") or fresh.get("_id")))
                            _mark_processed(state, str(fresh.get("analysis_id") or fresh.get("_id")))
                        else:
                            _log("[放弃] %s %s 重析产物仍不合格，放弃: %s"
                                 % (symbol, action, reason2 or "无有效信号"))
        else:
            _log("[跳过] %s 无 BUY/SELL 信号或代码缺失: symbol=%s action=%s" % (aid, symbol, action))
        _mark_processed(state, aid)
        _save_state(pt, state)

    if new_count == 0:
        print("无新报告（已全部处理）")

    # ---------- 原版行情估值逻辑（tick 执行 pending 信号，cron 兼容） ----------
    symbols = set(state.get("positions", {}).keys())
    for sig in state.get("pending_signals", []):
        symbols.add(sig["symbol"])
    rows = {}
    for sym in symbols:
        rows[sym] = get_daily_ohlc(sym, days=MAX_BACKDAYS + 5)

    if symbols and rows:
        date_sets = [set(v.keys()) for v in rows.values() if v]
        if date_sets:
            common = set.intersection(*date_sets)
            if common:
                latest_date = max(common)
                ohlc = {sym: rows[sym][latest_date] for sym in symbols}
                import inspect
                try:
                    if "price_field" in inspect.signature(pt.tick).parameters:
                        eq = pt.tick(latest_date, ohlc, price_field=BRIDGE_PRICE)
                    else:
                        eq = pt.tick(latest_date, ohlc)  # 兼容未打补丁的 paper_trading
                except TypeError:
                    eq = pt.tick(latest_date, ohlc)
                st = pt.status()
                print("MARK %s 净值=%.2f 持仓=%s 连亏=%s 暂停=%s" % (
                    latest_date, eq, list(st["positions"].keys()),
                    st["consecutive_losses"], st["pause_until"]))
            else:
                print("WARN 无共同交易日数据")
        else:
            print("WARN 无行情数据")
    else:
        print("WARN 无行情数据")


if __name__ == "__main__":
    main()
