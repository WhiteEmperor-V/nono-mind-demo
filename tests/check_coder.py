import sys, subprocess
sys.path.insert(0, "/root/nono-mind")
from state.server import StateClient
c = StateClient()
ts = c.kv_list("tasks/")
real = {k: v for k, v in ts.items() if not k.startswith("tasks/test_")}
for k, v in sorted(real.items())[-2:]:
    print(f"{k}: status={v.get('status')} steps={len(v.get('steps', []))}")
    print(f"  result: {str(v.get('result'))[:150]}")
procs = c.kv_list("procedures/")
print(f"procedures沉淀: {len(procs)}条")
for k, v in procs.items():
    print(f"  {k}: verified={v.get('verified')}, demand={str(v.get('demand'))[:60]}")
c.close()
r = subprocess.run(["tail", "-2", "/root/nono-mind/weixin/loop.log"], capture_output=True, text=True)
print(r.stdout.strip()[:200])
