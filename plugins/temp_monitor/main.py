import subprocess
import json

def init(state):
    """注册能力"""
    state["capabilities"] = ["load_check"]
    state["last_result"] = None
    return True

def tick(state):
    """采集 uptime 和 df -h"""
    try:
        # 采集负载
        uptime_result = subprocess.run(["uptime"], capture_output=True, text=True, timeout=5)
        uptime_output = uptime_result.stdout.strip()
        
        # 采集磁盘
        df_result = subprocess.run(["df", "-h"], capture_output=True, text=True, timeout=5)
        df_output = df_result.stdout.strip()
        
        result = {
            "uptime": uptime_output,
            "df": df_output
        }
        state["last_result"] = result
        
        # 发射信号
        signal = {
            "type": "temp_monitor_status",
            "payload": result
        }
        print(json.dumps(signal, ensure_ascii=False))
        return True
    except Exception as e:
        print(json.dumps({"type": "temp_monitor_status", "error": str(e)}, ensure_ascii=False))
        return False

def health():
    return True

if __name__ == "__main__":
    # 测试运行
    state = {}
    init(state)
    print("--- tick() output ---")
    tick(state)
    print("--- health() ---")
    print(health())
