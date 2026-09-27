"""电气二次设计工作台统一入口；保留原入口的单实例和自检机制。"""

from cable_stat_app import main


if __name__ == "__main__":
    raise SystemExit(main())
