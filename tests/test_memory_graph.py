import sys, re
sys.path.insert(0, "/root/nono-mind")

class MemState:
    def __init__(self): self.d = {}
    def kv_set(self, k, v): self.d[k] = v
    def kv_get(self, k): return self.d.get(k)
    def kv_list(self, p=""):
        return {k: v for k, v in self.d.items() if k.startswith(p)} if p else dict(self.d)

from immune import memory_graph as mg
from immune import retrieval as rt
c = MemState()

# 1. memory_write(带毫秒防撞key)
m1 = mg.memory_write(c, "主人喜欢简短回复, 讨厌长篇大论", importance=8)
m2 = mg.memory_write(c, "主人做火箭推力室换热器弯管工作", importance=7)
assert m1 != m2, f"key撞了: {m1}"
# 2. 关系边(双向)
mg.add_relation(c, m1, "relates_to", m2, "都来自日常对话")
assert len(c.d[m1]["relations"]) == 1 and len(c.d[m2]["relations"]) == 1
# 3. 非法关系拒绝+自指拒绝
try:
    mg.add_relation(c, m1, "hates", m2); assert False
except ValueError: pass
try:
    mg.add_relation(c, m1, "relates_to", m1); assert False
except ValueError: pass
# 4. neighbors顺藤摸瓜
nbs = mg.neighbors(c, m1)
assert len(nbs) == 1 and nbs[0]["key"] == m2 and nbs[0]["rel"] == "relates_to"
# 5. 检索图谱扩散
hits = rt.search(c, "主人喜欢 简短回复")
keys = [h["key"] for h in hits]
assert m1 in keys, f"m1应命中: {keys}"
assert m2 in keys, f"m2应作为邻居带出: {keys}"
# 6. MEMO提取正则
reply = "好的主人, 诺诺记住了[MEMO:主人偏好深蓝渐变|7]"
m = re.search(r"\[MEMO:\s*(.+?)\s*\|\s*(\d+)\s*\]\s*$", reply, re.S)
assert m and m.group(1) == "主人偏好深蓝渐变" and m.group(2) == "7"
clean = re.sub(r"\s*\[MEMO:.*$", "", reply, flags=re.S).strip()
assert clean == "好的主人, 诺诺记住了"

print("memory_graph 6项自检全过")
