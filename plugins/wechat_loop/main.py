#!/usr/bin/env python3
"""wechat_loop 插件入口(免疫层子进程托管模式)
器官本体在 organs/wechat_loop.py(单文件, 可被插件/系统d/意识核三方复用).
"""
import os
import sys

_REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from organs.wechat_loop import main

if __name__ == "__main__":
    sys.exit(main())
