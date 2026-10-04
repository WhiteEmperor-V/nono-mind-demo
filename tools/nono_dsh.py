#!/usr/bin/env python3
"""诺诺的dsh调用器 v2: 免费通道+可控thinking+自定义patch(插件挂载)

用法:
  python3 nono_dsh.py "任务"
  python3 nono_dsh.py --patch /root/dsh-home/my-plugins/nas-tool/cordis.patch.yml "查NAS磁盘"
判断: 诺诺自己的工具, 好用留 难用改.
"""
import argparse
from deepseek_harness import DeepSeekHarness


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", default="/root/nono-mind")
    ap.add_argument("--model", default="moonshotai/kimi-k3")
    ap.add_argument("--provider", default="deepseek-official")
    ap.add_argument("--effort", default=None, help="reasoning_effort: off/low/medium/high")
    ap.add_argument("--session-id", default=None)
    ap.add_argument("--dsh-home", default="/root/dsh-home")
    ap.add_argument("--max-tokens", type=int, default=32768)
    ap.add_argument("--base-url", default=None, help="默认用DEEPSEEK_BASE_URL环境变量")
    ap.add_argument("--patch", action="append", default=[], help="cordis.patch.yml路径, 可重复")
    ap.add_argument("task")
    args = ap.parse_args()

    kwargs = {}
    if args.base_url:
        kwargs["base_url"] = args.base_url
    if args.patch:
        kwargs["patches"] = tuple(args.patch)

    with DeepSeekHarness(
        provider=args.provider,
        model=args.model,
        reasoning_effort=args.effort,
        max_tokens=args.max_tokens,
        cwd=args.workspace,
        dsh_home=args.dsh_home,
        profile="sdk-minimal",
        **kwargs,
    ) as harness:
        result = harness.run(args.task, session_id=args.session_id)
        print(result.final_response)


if __name__ == "__main__":
    main()
