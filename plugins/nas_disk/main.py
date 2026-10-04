import subprocess
import json

def init(state):
    """注册能力"""
    state["capabilities"] = ["disk_check"]
    state["last_result"] = None
    return True

def tick(state):
    """对NAS挂载点跑 df -h，把结果 emit 成 nas_disk_status 信号"""
    try:
        # 执行 df -h 获取磁盘使用情况
        result = subprocess.run(["df", "-h"], capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            output = result.stdout.strip()
            # 解析 df -h 输出
            lines = output.split('\n')
            disks = []
            for line in lines[1:]:  # 跳过表头
                parts = line.split()
                if len(parts) >= 6:
                    disks.append({
                        "filesystem": parts[0],
                        "size": parts[1],
                        "used": parts[2],
                        "available": parts[3],
                        "use_percent": parts[4],
                        "mounted_on": parts[5]
                    })
            
            state["last_result"] = {
                "timestamp": __import__("time").time(),
                "disks": disks,
                "raw_output": output
            }
            # 发射信号
            if "emit" in state:
                state["emit"]("nas_disk_status", state["last_result"])
            return True
        else:
            state["last_result"] = {"error": result.stderr}
            return False
    except Exception as e:
        state["last_result"] = {"error": str(e)}
        return False

def health():
    """健康检查，返回 True"""
    return True
