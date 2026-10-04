#!/usr/bin/env python3
"""StuckDetector(OpenHands五种死循环滑窗检测的简化版): agent循环卡死检测
检测的循环病:
1. 重复动作: 连续N步代码几乎相同(LLM原地打转)
2. 重复报错: 连续N步同一种错误(stderr核心相同)
3. 空观察: 连续N步无有效输出
4. 无进展: 连续N步观察完全相同
返回建议: continue/换策略/中止
"""
from collections import deque

REPEAT_ACTION_N = 3      # 连续3步代码相同=打转
REPEAT_ERROR_N = 3       # 连续3步同类错误
EMPTY_OBS_N = 4          # 连续4步空输出
NO_PROGRESS_N = 4        # 连续4步观察不变

def _code_sig(code: str) -> str:
    """代码指纹: 去空白取核心(命名/字面量变化不算新动作)"""
    import re
    return re.sub(r"\s+", " ", code.strip())[:200]

def _err_sig(stderr: str) -> str:
    """错误指纹: 取首行关键信息(行号变化不算新错误)"""
    for line in stderr.splitlines():
        line = line.strip()
        if line and not line.startswith("File "):
            return line[:120]
    return ""

class StuckDetector:
    def __init__(self):
        self.code_sigs = deque(maxlen=REPEAT_ACTION_N)
        self.err_sigs = deque(maxlen=REPEAT_ERROR_N)
        self.empty_streak = 0
        self.obs_sigs = deque(maxlen=NO_PROGRESS_N)

    def observe(self, code: str, obs: dict) -> str:
        """每步喂一次(code+观察), 返回判定: ok/repeat_action/repeat_error/empty/no_progress"""
        advice = "ok"
        # 1. 重复动作
        self.code_sigs.append(_code_sig(code))
        if len(self.code_sigs) == REPEAT_ACTION_N and len(set(self.code_sigs)) == 1:
            advice = "repeat_action"
        # 2. 重复报错
        esig = _err_sig(obs.get("stderr", "")) if not obs.get("ok") else ""
        if esig:
            self.err_sigs.append(esig)
            if len(self.err_sigs) == REPEAT_ERROR_N and len(set(self.err_sigs)) == 1:
                advice = "repeat_error"
        else:
            self.err_sigs.clear()
        # 3. 空观察
        if not obs.get("stdout", "").strip() and not obs.get("stderr", "").strip():
            self.empty_streak += 1
            if self.empty_streak >= EMPTY_OBS_N:
                advice = "empty"
        else:
            self.empty_streak = 0
        # 4. 无进展(优先级最低——empty/repeat_error更具体时让位)
        osig = (obs.get("stdout", "")[:100], obs.get("stderr", "")[:100], obs.get("returncode"))
        self.obs_sigs.append(osig)
        if advice == "ok" and len(self.obs_sigs) == NO_PROGRESS_N and len(set(self.obs_sigs)) == 1:
            advice = "no_progress"
        return advice

    def nudge(self, advice: str) -> str:
        """给LLM的纠偏指令(注入下一步prompt, OpenHands的nudge模式)"""
        return {
            "repeat_action": "⚠️你连续多步输出相同代码, 在原地打转。必须换一种方法或换一个方向。",
            "repeat_error": "⚠️你连续多步犯同一个错误。停下分析错误根因, 换思路, 不要再提交同样的代码。",
            "empty": "⚠️你连续多步没有任何输出。检查脚本是否真的执行了/是否print了结果。",
            "no_progress": "⚠️你连续多步观察毫无变化, 任务没有进展。重新评估任务是否需要拆解或换路径。",
        }.get(advice, "")
