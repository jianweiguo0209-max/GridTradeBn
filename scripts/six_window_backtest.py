"""推荐入口；实现复用 four_window_backtest 以保持旧命令兼容。"""
import sys

from scripts.four_window_backtest import main


if __name__ == '__main__':
    from gridtrade.backtest.envfile import load_env_file
    load_env_file()
    sys.exit(main())
