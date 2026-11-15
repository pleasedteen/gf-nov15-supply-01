try:  # 作为包执行：python -m qrelease
    from .main import main
except ImportError:  # 直接执行脚本（含 python -I）：把父目录加入路径
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from qrelease.main import main

if __name__ == "__main__":
    raise SystemExit(main())
