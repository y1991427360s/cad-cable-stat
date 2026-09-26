"""电缆长度自动统计 · 核心计算包。

模块分工（数据流 CSV/清册 → 路径网 → 长度 → Excel/HTML）：
    text      名称、楼层、数值的归一化规则
    params    计算参数登记表（默认值/说明/取值范围）与 参数.csv 读写
    csvio     CSV 读写（兼容多种编码，写出 utf-8-sig）
    loaders   data\\ 各 CSV 与电缆清册工作簿的读取
    geometry  平面几何与网格空间索引
    graph     固定路径网（吸附/T型/十字连通、柜子接入、最短路）
    rooms     房间判定与跨房间分析
    calc      拐弯判定与长度规则
    report    结果工作簿（汇总/明细/柜子清单/参数/问题清单）
    visual    路径可视化页面数据与 HTML 生成（模板 visualizer.html）
    pipeline  一次完整计算，返回结构化结果
    project   项目文件夹：找清册、盘点状态、写别名表
"""

__version__ = "2026.09.26"
