#!/usr/bin/env python3
"""LLM客户端三级降级(2026-09-19修复): 全部使用免费通道, 禁止付费key
P0修复: 原链含智谱GLM(计费)→DeepSeek(计费),已全部替换为免费替代方案
"""
import json, re, time, urllib.request, yaml, os

_cfg = yaml.safe_load(open('/root/.hermes/config.yaml'))['providers']

# scnet已删(主人拍板9/27: 供应商拉, 模型老, 价格≈官方)
# _SC = _cfg['scnet']
# _SCBASE, _SCKEY = _SC['api'].rstrip('/'), _SC['api_key']

# 主脑: 全走cliproxyapi的CPA
_CPA = _cfg.get('cliproxyapi', {})
_CPBASE = _CPA.get('base_url', 'http://<YOUR-CPA-HOST>/v1').rstrip('/')
_CPKEY = _CPA.get('api_key', '')
# agnes通道(主人9/28给的key, 细活用灵脑子): 造插件/写码等深度任务第一档
_AGK = "sk-REDACTED-agnes-key"
_AGBASE = "https://apihub.agnes-ai.com/v1"
# (apikey.fun供应商已删, 2026-09-30主人拍板: 全走agnes/nemotron, 不碰apikey_fun_key)
_AK = ""

# 通道冷却: 402/429后120秒内不再撞(省时间)
_cooldown = {}  # channel_name -> until_ts
_fail_cnt = {}  # channel_name -> 连续失败计数(成功即清零)
_COOLDOWN_S = 120

# ---------- 判断账 · 挂点C: 脑子名变化检测 ----------
_brain_seen = None   # 本进程上次用于响应的脑子名(模型名)

def _track_brain(used):
    """挂点C: 每次响应后记录"当前脑子名"; 变了(手动切/降级切)→触发一次换脑重验.
    best-effort: 无State服务时静默跳过, 不阻断对话. 脑子名不变时零额外IO(省资源).
    返回本次是否发生了换脑."""
    global _brain_seen
    if used == _brain_seen:
        return False            # 脑子没变 → 不碰State
    _brain_seen = used
    try:
        from state.server import StateClient
        from organs.judgments import note_brain
        c = StateClient()
        try:
            note_brain(c, used)
        finally:
            c.close()
    except Exception as e:
        print(f"[llm] 换脑重验触发失败(不阻断): {e}")
    return True

def _channel_ok(name):
    """通道是否可用(冷却检查)"""
    if name in _cooldown and time.time() < _cooldown[name]:
        return False
    return True

def _try_chain(messages, max_tokens, chain, timeout=120):
    """按chain顺序尝试各通道"""
    for name, base_url, key, model in chain:
        if not key:
            continue
        if not _channel_ok(name):
            continue
        try:
            url = f"{base_url}/chat/completions"
            req_data = {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": 0.7
            }
            data = json.dumps(req_data).encode('utf-8')
            headers = {
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json"
            }
            req = urllib.request.Request(url, data=data, headers=headers, method='POST')
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read())
                reply = body["choices"][0]["message"]["content"]
                # 清除失败计数
                _fail_cnt[name] = 0
                return reply, name
        except Exception as e:
            err_str = str(e).lower()
            code = 0
            # 提取错误码
            try:
                if hasattr(e, 'read'):
                    err_body = json.loads(e.read().decode())
                    code = int(err_body.get('code', 0)) or int(err_body.get('status', 0))
                    msg = err_body.get('message', '') + err_body.get('msg', '')
                elif hasattr(e, 'code'):
                    code = e.code
                    msg = ''
                else:
                    msg = err_str
            
            except:
                msg = err_str

            # 欠费/余额不足/请求错误 → 跳过此通道但不重试(不是暂时的)
            if code in (400, 401, 402, 403, 422) or 'insufficient' in err_str or 'not exist' in err_str or '余额' in msg or 'balance' in err_str:
                print(f"[llm] ⚠️ {name}不可用(code={code}), 自动跳过")
                _fail_cnt[name] = 999  # 永久标记失效
                continue
            
            # 限流 → 进入冷却窗口
            retry_after = None
            m = re.search(r"[Rr]etry[- ][Aa]fter[:\s]*(\d+)", msg)
            if m:
                retry_after = int(m.group(1))
            
            if 'rate' in err_str or '429' in err_str or 'limit' in err_str or retry_after:
                wait = retry_after or min(_COOLDOWN_S, int(err_str.split('second')[1].split(' ')[0]) if 'second' in err_str else _COOLDOWN_S)
                _cooldown[name] = time.time() + wait
                print(f"[llm] {name}限流, 冷却{wait}s后重试")
                continue
            
            # 网络超时/连接失败 → 短暂重试一次
            if 'timed out' in err_str or 'connection' in err_str or 'refused' in err_str:
                if _fail_cnt.get(name, 0) < 1:
                    _fail_cnt[name] = _fail_cnt.get(name, 0) + 1
                    print(f"[llm] {name}连接异常(第{_fail_cnt[name]}次)")
                    continue
            
            # 其他错误 → 标记冷却
            _cooldown[name] = time.time() + _COOLDOWN_S
            print(f"[llm] {name}异常: {msg[:100]}")
    
    raise RuntimeError("All channels failed")

def _extract_retry_after(msg):
    m = re.search(r"[Rr]etry[- ][Aa]fter[:\s]*(\d+)")
    return int(m.group(1)) if m else None

def _chat_chain():
    """日常对话降级链(2026-09-28主人拍板): 全用agnes(免费), 限流时冷却降回nemotron兜底
    顺序: agnes-3.0-flash(主脑) → nemotron-550b(cpa兜底) → nemotron-120b(cpa兜底)
    (scnet已删; agnes免费但有每分钟调用限制, _try_chain对429/限流会自动冷却不硬撞)"""
    chain = [
        ("agnes-flash",    _AGBASE, _AGK, "agnes-3.0-flash") if _AGK else None,
        ("nemotron-550b",  _CPBASE, _CPKEY, "nvidia/nemotron-3-ultra-550b-a55b") if _CPKEY else None,
        ("nemotron-120b",  _CPBASE, _CPKEY, "nvidia/nemotron-3-super-120b-a12b") if _CPKEY else None,
    ]
    return [c for c in chain if c]


def chat(messages, max_tokens=1000, _retry=0, timeout=120):
    """日常对话降级链(2026-09-28改): 全用agnes(免费), 限流冷却降回nemotron兜底"""
    chain = _chat_chain()
    text, used = _try_chain(messages, max_tokens, chain, timeout=timeout)
    _track_brain(used)
    if used != "agnes-flash":
        print(f"[llm] 本次由{used}通道响应(agnes限流降级)")
    return text

def deep_chat(messages, max_tokens=1000):
    """反思/长任务/造插件写码等细活: agnes-3.0-flash(灵脑子)第一档 → nemotron-550b旗舰 → nemotron-120b兜底
    (2026-09-28主人拍板: 造插件/写码这种活常有, 细活用更灵的脑子; 日常对话仍走nemotron省钱)"""
    chain = [
        ("agnes-flash", _AGBASE, _AGK, "agnes-3.0-flash") if _AGK else None,
        ("nemotron-550b", _CPBASE, _CPKEY, "nvidia/nemotron-3-ultra-550b-a55b") if _CPKEY else None,
        ("nemotron-120b", _CPBASE, _CPKEY, "nvidia/nemotron-3-super-120b-a12b") if _CPKEY else None,
    ]
    chain = [c for c in chain if c]

    text, used = _try_chain(messages, max_tokens, chain)
    _track_brain(used)
    if used not in ("agnes-flash", "nemotron-550b", "nemotron-120b"):
        print(f"[llm] 深度任务由{used}通道响应(降级)")
    return text
