#!/usr/bin/env python3
"""隔离模式测试入口：python3 -I run_tests.py [-v]。

-I 会忽略 PYTHONPATH 且不自动加入当前目录，因此这里显式把仓库根放入 sys.path，
保证在存在同名占位模块（如恶意 json.py）的环境中也只加载本仓库代码。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=os.path.join(os.path.dirname(__file__), "tests"))
    runner = unittest.TextTestRunner(verbosity=2 if "-v" in sys.argv else 1)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
