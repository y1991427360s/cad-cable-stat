"""accoreconsole 实跑验证 CAD 插件的导出（不开 AutoCAD 界面，不碰任何工程 DWG）。

用法（仓库根目录）：
    set PYTHONUTF8=1
    python tools\\lisp_test\\run_lisp_test.py
    python tools\\lisp_test\\run_lisp_test.py --console "E:\\AutoCAD2027\\AutoCAD 2027\\accoreconsole.exe"
    python tools\\lisp_test\\run_lisp_test.py --mbcs   # 另用 LISPSYS=0（MBCS 版 AutoLISP，同 AutoCAD 2020 及更早）再跑一遍

--mbcs 会临时把 AutoCAD 2027 的 LISPSYS 设成 0（写在用户配置里，重启控制台才生效），跑完无论成败都改回原值并核实。
每次运行重建 %TEMP%\\ddfd_lisp_test\\，里面留着 .scr、夹具 LISP、日志、控制台输出、导出的 CSV 和端到端项目，便于排查。

流程：
  1. 向导版：accoreconsole 不带 /i 打开空白图（acadiso.dwt）→ 加载 cad_cable_wizard.lsp 和夹具 → entmake 造合成图
     （LINE、带圆弧段和重复顶点的开口 LWPOLYLINE、反向拉伸+高程的闭合 LWPOLYLINE、二维/三维 POLYLINE、ARC、
      SPLINE/ELLIPSE/网格（应跳过并提示）、TEXT/MTEXT/块属性/动态块/无 COM 时的匿名块、引号柜名、布局里的图元、房间）
     → 纯函数用例 → (ddfd-do-export) 导出四个 CSV；
  2. 精简版 cad_export_cable_route.lsp 单独加载做同样的事，证明自包含，四个 CSV 须与向导版逐字节一致；
  3. 事务：注入读图失败 → 不覆盖已有 CSV、不留 .pending；残留 .previous → 拒绝提交；清掉后重导成功且结果不变；
  4. 向导第 2 步用脚本应答实跑：UCS 平移旋转后标注位置换算成 WCS，重名柜名要确认；
  5. 核对 CSV（坐标、弧长、闭合段、段号、楼层 B1F/1F/2F、引号转义）、命令行汇总提示、纯函数结果
     （楼层解析与 Python 的 infer_floor_from_layer 对照）；
  6. 用导出的 CSV + 生成的小清册跑 calculate_cable_lengths.py，检查每条电缆状态 OK、基础路径长度符合几何计算。
注意：控制台 stdout 是 UTF-16LE；子进程必须 stdin=DEVNULL，否则 accoreconsole 会等输入卡住。
"""

from __future__ import annotations

import argparse
import csv
import io
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONSOLE = Path(r"E:\AutoCAD2027\AutoCAD 2027\accoreconsole.exe")
WORK = Path(os.environ.get("TEMP", str(ROOT))) / "ddfd_lisp_test"
WIZARD = ROOT / "cad_cable_wizard.lsp"
EXPORT = ROOT / "cad_export_cable_route.lsp"
FILES = ("柜子坐标.csv", "路径线段.csv", "竖井.csv", "房间范围.csv")

sys.path.insert(0, str(ROOT))
from cable_stat.text import infer_floor_from_layer, normalize_floor  # noqa: E402

# ---------------------------------------------------------------- 夹具（GBK 写出，只在临时空白图里用）

FIXTURE = r'''
;;; 测试夹具：由 tools\lisp_test\run_lisp_test.py 生成，只在临时空白图里用
(defun ddfdt-log (fh tag a b)
  (princ tag fh) (princ "|" fh) (princ a fh) (princ "|" fh) (princ b fh) (princ "\n" fh))

(defun ddfdt-layer (name color)
  (if (not (tblsearch "LAYER" name))
    (entmake (list '(0 . "LAYER") '(100 . "AcDbSymbolTableRecord") '(100 . "AcDbLayerTableRecord")
                   (cons 2 name) '(70 . 0) (cons 62 color) '(6 . "Continuous")))))

;; 清单：标签 -> 句柄（复杂图元做完 SEQEND 后 entlast 仍是主图元）
(defun ddfdt-note (fh label ok)
  (ddfdt-log fh "M" label (if (and ok (entlast)) (cdr (assoc 5 (entget (entlast)))) "FAILED")))

(defun ddfdt-make (fh label data) (ddfdt-note fh label (entmake data)))

(defun ddfdt-make-complex (fh label items / ok d)
  (setq ok T)
  (foreach d items (if (not (entmake d)) (setq ok nil)))
  (ddfdt-note fh label ok))

(defun ddfdt-text (fh label layer x y str)
  (ddfdt-make fh label (list '(0 . "TEXT") (cons 8 layer) (list 10 x y 0.0) '(40 . 300.0) (cons 1 str))))

(defun ddfdt-mtext (fh label layer x y str)
  (ddfdt-make fh label (list '(0 . "MTEXT") '(100 . "AcDbEntity") (cons 8 layer) '(100 . "AcDbMText")
                             (list 10 x y 0.0) '(40 . 300.0) (cons 1 str))))

(defun ddfdt-v2d (pt bulge)
  (list '(0 . "VERTEX") '(100 . "AcDbEntity") '(8 . "CABLE_ROUTE_1F") '(100 . "AcDbVertex") '(100 . "AcDb2dVertex")
        (cons 10 pt) (cons 42 bulge) '(70 . 0)))

(defun ddfdt-v3d (pt flag sub)
  (list '(0 . "VERTEX") '(100 . "AcDbEntity") '(8 . "CABLE_ROUTE_1F") '(100 . "AcDbVertex") (cons 100 sub)
        (cons 10 pt) (cons 70 flag)))

(defun ddfdt-build (fh / l s anon ok)
  (foreach l '(("CABLE_CABINET_1F" 2) ("CABLE_CABINET_2F" 2) ("CABLE_CABINET_B1F" 2) ("CABLE_CABINET" 2)
               ("CABLE_ROUTE_1F" 4) ("CABLE_ROUTE_2F" 4) ("CABLE_ROUTE_B1F" 4)
               ("CABLE_SHAFT_1F" 1) ("CABLE_SHAFT_2F" 1) ("CABLE_SHAFT_B1F" 1) ("CABLE_ROOM_1F" 6))
    (ddfdt-layer (car l) (cadr l)))
  ;; ---- 1F 路径 ----
  (ddfdt-make fh "L1" '((0 . "LINE") (8 . "CABLE_ROUTE_1F") (10 0.0 0.0 0.0) (11 10000.0 0.0 0.0)))
  ;; 开口 LWPOLYLINE：第 2 段凸度 0.5，第 3 段是重复顶点（零长度，跳过但段号保留）
  (ddfdt-make fh "P1" '((0 . "LWPOLYLINE") (100 . "AcDbEntity") (8 . "CABLE_ROUTE_1F") (100 . "AcDbPolyline")
                        (90 . 5) (70 . 0)
                        (10 10000.0 0.0) (42 . 0.0) (10 14000.0 0.0) (42 . 0.5) (10 18000.0 0.0) (42 . 0.0)
                        (10 18000.0 0.0) (42 . 0.0) (10 18000.0 6000.0) (42 . 0.0)))
  ;; 闭合 LWPOLYLINE，拉伸方向 (0 0 -1)（镜像出来的），高程 100：OCS 的 x 取反才是 WCS
  (ddfdt-make fh "P2" '((0 . "LWPOLYLINE") (100 . "AcDbEntity") (8 . "CABLE_ROUTE_1F") (100 . "AcDbPolyline")
                        (90 . 4) (70 . 1) (38 . 100.0)
                        (10 -18000.0 6000.0) (10 -22000.0 6000.0) (10 -22000.0 10000.0) (10 -18000.0 10000.0)
                        (210 0.0 0.0 -1.0)))
  ;; 二维 POLYLINE：第 2 段凸度 1（半圆）
  (ddfdt-make-complex fh "PL3"
    (list '((0 . "POLYLINE") (100 . "AcDbEntity") (8 . "CABLE_ROUTE_1F") (100 . "AcDb2dPolyline")
            (66 . 1) (10 0.0 0.0 0.0) (70 . 0))
          (ddfdt-v2d '(0.0 0.0 0.0) 0.0)
          (ddfdt-v2d '(0.0 -5000.0 0.0) 1.0)
          (ddfdt-v2d '(0.0 -9000.0 0.0) 0.0)
          '((0 . "SEQEND") (100 . "AcDbEntity") (8 . "CABLE_ROUTE_1F"))))
  ;; ARC：圆心 (10000,-5000) 半径 5000，90 度到 180 度：起点 (10000,0) 终点 (5000,-5000)
  (ddfdt-make fh "A1" (list '(0 . "ARC") '(8 . "CABLE_ROUTE_1F") '(10 10000.0 -5000.0 0.0) '(40 . 5000.0)
                            (cons 50 (/ pi 2.0)) (cons 51 pi)))
  ;; 三维多段线：(22000,10000,0) -> (22000,13000,4000)，长度 5000
  (ddfdt-make-complex fh "P3D"
    (list '((0 . "POLYLINE") (100 . "AcDbEntity") (8 . "CABLE_ROUTE_1F") (100 . "AcDb3dPolyline")
            (66 . 1) (10 0.0 0.0 0.0) (70 . 8))
          (ddfdt-v3d '(22000.0 10000.0 0.0) 32 "AcDb3dPolylineVertex")
          (ddfdt-v3d '(22000.0 13000.0 4000.0) 32 "AcDb3dPolylineVertex")
          '((0 . "SEQEND") (100 . "AcDbEntity") (8 . "CABLE_ROUTE_1F"))))
  ;; 零长度 LINE：不导出
  (ddfdt-make fh "Z1" '((0 . "LINE") (8 . "CABLE_ROUTE_1F") (10 30000.0 -3000.0 0.0) (11 30000.0 -3000.0 0.0)))
  ;; 不支持的图元：SPLINE、ELLIPSE、多边形网格
  (ddfdt-make fh "S1" '((0 . "SPLINE") (100 . "AcDbEntity") (8 . "CABLE_ROUTE_1F") (100 . "AcDbSpline")
                        (210 0.0 0.0 1.0) (70 . 8) (71 . 3) (72 . 8) (73 . 4) (74 . 0) (42 . 1.0e-10) (43 . 1.0e-10)
                        (40 . 0.0) (40 . 0.0) (40 . 0.0) (40 . 0.0) (40 . 1.0) (40 . 1.0) (40 . 1.0) (40 . 1.0)
                        (10 30000.0 20000.0 0.0) (10 31000.0 21000.0 0.0) (10 32000.0 20000.0 0.0) (10 33000.0 21000.0 0.0)))
  (ddfdt-make fh "E1" (list '(0 . "ELLIPSE") '(100 . "AcDbEntity") '(8 . "CABLE_ROUTE_1F") '(100 . "AcDbEllipse")
                            '(10 40000.0 20000.0 0.0) '(11 2000.0 0.0 0.0) '(210 0.0 0.0 1.0) '(40 . 0.5)
                            '(41 . 0.0) (cons 42 (* 2.0 pi))))
  (ddfdt-make-complex fh "M1"
    (list '((0 . "POLYLINE") (100 . "AcDbEntity") (8 . "CABLE_ROUTE_1F") (100 . "AcDbPolygonMesh")
            (66 . 1) (10 0.0 0.0 0.0) (70 . 16) (71 . 2) (72 . 2))
          (ddfdt-v3d '(50000.0 0.0 0.0) 64 "AcDbPolygonMeshVertex")
          (ddfdt-v3d '(50000.0 1000.0 0.0) 64 "AcDbPolygonMeshVertex")
          (ddfdt-v3d '(51000.0 0.0 0.0) 64 "AcDbPolygonMeshVertex")
          (ddfdt-v3d '(51000.0 1000.0 0.0) 64 "AcDbPolygonMeshVertex")
          '((0 . "SEQEND") (100 . "AcDbEntity") (8 . "CABLE_ROUTE_1F"))))
  ;; 布局（图纸空间）里的路径线：不导出
  (ddfdt-make fh "PS1" '((0 . "LINE") (67 . 1) (8 . "CABLE_ROUTE_1F") (10 0.0 0.0 0.0) (11 99999.0 0.0 0.0)))
  ;; ---- 2F、B1F 路径 ----
  (ddfdt-make fh "L2" '((0 . "LINE") (8 . "CABLE_ROUTE_2F") (10 0.0 0.0 0.0) (11 20000.0 0.0 0.0)))
  ;; 反向拉伸的 ARC：OCS 圆心 (-20000,5000)，180 度到 270 度：WCS 起点 (25000,5000) 终点 (20000,0)
  (ddfdt-make fh "A2" (list '(0 . "ARC") '(8 . "CABLE_ROUTE_2F") '(10 -20000.0 5000.0 0.0) '(40 . 5000.0)
                            (cons 50 pi) (cons 51 (* 1.5 pi)) '(210 0.0 0.0 -1.0)))
  (ddfdt-make fh "L3" '((0 . "LINE") (8 . "CABLE_ROUTE_B1F") (10 0.0 0.0 0.0) (11 8000.0 0.0 0.0)))
  ;; ---- 柜子 ----
  (ddfdt-text fh "C_1AL" "CABLE_CABINET_1F" 1000.0 500.0 "1AL")
  (ddfdt-text fh "C_QUOTE" "CABLE_CABINET_1F" 9000.0 500.0 "柜\"A\"1")
  (ddfdt-mtext fh "C_MTEXT" "CABLE_CABINET_1F" 15000.0 1500.0
               "{\\fSimSun|b0|i0|c134|p2;\\C1;\\L2号\\l\\P\\H0.7x;低压柜\\~A\\S1^2;\\{X\\}}")
  ;; 超过 250 字的 MTEXT：前 250 字放在 3 组码
  (setq s "{")
  (repeat 60 (setq s (strcat s "\\H1x;")))
  (setq s (strcat s "3AL}"))
  (ddfdt-make fh "C_LONG" (list '(0 . "MTEXT") '(100 . "AcDbEntity") '(8 . "CABLE_CABINET_1F") '(100 . "AcDbMText")
                                '(10 5000.0 -500.0 0.0) '(40 . 300.0) (cons 3 (substr s 1 250)) (cons 1 (substr s 251))))
  (ddfdt-log fh "M" "C_LONG_HAS3" (if (assoc 3 (entget (entlast))) "T" "NIL"))
  ;; 带属性的块：第一个属性为空，第二个是柜名；块参照反向拉伸（OCS 的 x 取反）。
  ;; ATTRIB 不写 100 子类标记：带标记时 210 组码必须放在 AcDbText 段里，放错位置 entmake 会失败
  (entmake '((0 . "BLOCK") (2 . "DDFD_TEST_CAB") (70 . 2) (10 0.0 0.0 0.0)))
  (entmake '((0 . "CIRCLE") (8 . "0") (10 0.0 0.0 0.0) (40 . 100.0)))
  (entmake '((0 . "ATTDEF") (8 . "0") (10 0.0 0.0 0.0) (40 . 100.0) (1 . "") (3 . "NOTE") (2 . "NOTE") (70 . 0)))
  (entmake '((0 . "ATTDEF") (8 . "0") (10 0.0 -200.0 0.0) (40 . 100.0) (1 . "") (3 . "NAME") (2 . "NAME") (70 . 0)))
  (entmake '((0 . "ENDBLK")))
  (ddfdt-make-complex fh "C_4AP"
    (list '((0 . "INSERT") (8 . "CABLE_CABINET_1F") (66 . 1) (2 . "DDFD_TEST_CAB") (10 -12500.0 800.0 0.0) (210 0.0 0.0 -1.0))
          '((0 . "ATTRIB") (8 . "CABLE_CABINET_1F") (10 -12500.0 800.0 0.0) (40 . 100.0) (1 . "") (2 . "NOTE") (70 . 0)
            (210 0.0 0.0 -1.0))
          '((0 . "ATTRIB") (8 . "CABLE_CABINET_1F") (10 -12500.0 600.0 0.0) (40 . 100.0) (1 . "4AP") (2 . "NAME") (70 . 0)
            (210 0.0 0.0 -1.0))
          '((0 . "SEQEND") (8 . "CABLE_CABINET_1F"))))
  ;; 没有属性的块：用块名
  (entmake '((0 . "BLOCK") (2 . "KG5") (70 . 0) (10 0.0 0.0 0.0)))
  (entmake '((0 . "CIRCLE") (8 . "0") (10 0.0 0.0 0.0) (40 . 100.0)))
  (entmake '((0 . "ENDBLK")))
  (ddfdt-make fh "C_KG5" '((0 . "INSERT") (8 . "CABLE_CABINET_1F") (2 . "KG5") (10 16000.0 -1000.0 0.0)))
  ;; 模拟动态块：匿名块 *U 的块记录带 AcDbBlockRepBTag 扩展数据，指向原块 DDFD_DYN_SRC
  (entmake '((0 . "BLOCK") (2 . "DDFD_DYN_SRC") (70 . 0) (10 0.0 0.0 0.0)))
  (entmake '((0 . "CIRCLE") (8 . "0") (10 0.0 0.0 0.0) (40 . 50.0)))
  (entmake '((0 . "ENDBLK")))
  (entmake '((0 . "BLOCK") (2 . "*U") (70 . 1) (10 0.0 0.0 0.0)))
  (entmake '((0 . "CIRCLE") (8 . "0") (10 0.0 0.0 0.0) (40 . 50.0)))
  (setq anon (entmake '((0 . "ENDBLK"))))
  (ddfdt-log fh "M" "C_DYN_ANON" (vl-princ-to-string anon))
  (regapp "AcDbBlockRepBTag")
  (setq ok (vl-catch-all-apply
             '(lambda ( / rec src)
                (setq rec (cdr (assoc 330 (entget (tblobjname "BLOCK" anon))))
                      src (cdr (assoc 330 (entget (tblobjname "BLOCK" "DDFD_DYN_SRC")))))
                (entmod (append (entget rec)
                                (list (list -3 (list "AcDbBlockRepBTag" '(1070 . 1)
                                                     (cons 1005 (cdr (assoc 5 (entget src)))))))))
                (assoc -3 (entget rec '("AcDbBlockRepBTag"))))
             nil))
  (ddfdt-log fh "M" "C_DYN_XDATA" (if (and ok (not (vl-catch-all-error-p ok))) "T" "NIL"))
  (ddfdt-make fh "C_DYN" (list '(0 . "INSERT") '(8 . "CABLE_CABINET_1F") (cons 2 anon) '(10 8000.0 -700.0 0.0)))
  ;; 没有 AcDbBlockRepBTag 的匿名块：DXF 查不到原块名，会走到 COM 的 EffectiveName；
  ;; 控制台没有 COM，必须被 vl-catch-all-apply 接住并退回 *U… 原名
  (entmake '((0 . "BLOCK") (2 . "*U") (70 . 1) (10 0.0 0.0 0.0)))
  (entmake '((0 . "CIRCLE") (8 . "0") (10 0.0 0.0 0.0) (40 . 50.0)))
  (setq anon (entmake '((0 . "ENDBLK"))))
  (ddfdt-log fh "M" "C_ANON_NAME" (vl-princ-to-string anon))
  (ddfdt-make fh "C_ANON" (list '(0 . "INSERT") '(8 . "CABLE_CABINET_1F") (cons 2 anon) '(10 9500.0 -1500.0 0.0)))
  ;; %% 控制码、空格、空柜名、点、反向拉伸的文字、重名、布局里的文字、中文与生僻字
  (ddfdt-text fh "C_PCT" "CABLE_CABINET_1F" 2000.0 -600.0 "%%u6AL%%u")
  (ddfdt-text fh "C_SYM" "CABLE_CABINET_1F" 3000.0 600.0 "%%c%%d%%p%%%%%0657AL")
  (ddfdt-text fh "C_SP" "CABLE_CABINET_1F" 4000.0 400.0 "  8 AL ")
  (ddfdt-text fh "C_BLANK" "CABLE_CABINET_1F" 4500.0 400.0 " ")
  (ddfdt-make fh "C_POINT" '((0 . "POINT") (8 . "CABLE_CABINET_1F") (10 4600.0 400.0 0.0)))
  (ddfdt-make fh "C_MIR" '((0 . "TEXT") (8 . "CABLE_CABINET_1F") (10 -6000.0 700.0 0.0) (40 . 300.0) (1 . "9AL")
                           (210 0.0 0.0 -1.0)))
  (ddfdt-text fh "C_DUP1" "CABLE_CABINET_1F" 7000.0 300.0 "DUP1")
  (ddfdt-text fh "C_DUP2" "CABLE_CABINET_2F" 7000.0 300.0 "DUP1")
  (ddfdt-make fh "C_PAPER" '((0 . "TEXT") (67 . 1) (8 . "CABLE_CABINET_1F") (10 100.0 100.0 0.0) (40 . 3.0)
                             (1 . "PAPERCAB")))
  (ddfdt-text fh "C_CN" "CABLE_CABINET_1F" 500.0 -800.0 "1号进线柜")
  (ddfdt-text fh "C_ARC" "CABLE_CABINET_1F" 7000.0 -3000.0 "ARCAL")
  (ddfdt-text fh "C_RARE_T" "CABLE_CABINET_1F" 11000.0 -500.0 "@RARE_TEXT@")
  (ddfdt-mtext fh "C_RARE_MT" "CABLE_CABINET_1F" 11500.0 1000.0 "@RARE_MTEXT@")
  (ddfdt-text fh "C_2AL" "CABLE_CABINET_2F" 1000.0 500.0 "2AL")
  (ddfdt-text fh "C_B1AL" "CABLE_CABINET_B1F" 1000.0 500.0 "B1AL")
  (ddfdt-text fh "C_NOFLOOR" "CABLE_CABINET" 500.0 500.0 "NOFLOOR1")
  ;; ---- 竖井 ----
  (ddfdt-text fh "S_ZJ1_1F" "CABLE_SHAFT_1F" 18000.0 3000.0 "ZJ1_1F")
  (ddfdt-text fh "S_ZJ1_2F" "CABLE_SHAFT_2F" 18000.0 500.0 "ZJ1_2F")
  (ddfdt-text fh "S_ZJ2_B1F" "CABLE_SHAFT_B1F" 7000.0 500.0 "ZJ2_B1F")
  (ddfdt-mtext fh "S_ZJ2_1F" "CABLE_SHAFT_1F" 7500.0 200.0 "{\\fArial|b1;ZJ2_1F}")
  ;; ---- 房间：一个用 ddfd-room-set-name 命名，一个未命名（不导出，只提示） ----
  (ddfdt-make fh "R_ROOM" '((0 . "LWPOLYLINE") (100 . "AcDbEntity") (8 . "CABLE_ROOM_1F") (100 . "AcDbPolyline")
                            (90 . 4) (70 . 1) (10 0.0 -2000.0) (10 12000.0 -2000.0) (10 12000.0 2000.0) (10 0.0 2000.0)))
  (ddfd-room-set-name (entlast) "配电室")
  (ddfdt-make fh "R_UNNAMED" '((0 . "LWPOLYLINE") (100 . "AcDbEntity") (8 . "CABLE_ROOM_1F") (100 . "AcDbPolyline")
                               (90 . 4) (70 . 1) (10 -1000.0 -20000.0) (10 60000.0 -20000.0) (10 60000.0 30000.0)
                               (10 -1000.0 30000.0))))

(defun ddfdt-case (fh id fn / r)
  (setq r (vl-catch-all-apply fn nil))
  (ddfdt-log fh "U" id (cond ((vl-catch-all-error-p r) (strcat "ERROR: " (vl-catch-all-error-message r)))
                              ((numberp r) (rtos r 2 6))
                              ((null r) "NIL")
                              (T r))))

(defun ddfdt-run-body (fh outdir mode / r)
  (ddfdt-log fh "INFO" "version" *ddfd-cable-version*)
  (ddfdt-log fh "INFO" "mbcs" (if (ddfd-mbcs-p) "T" "NIL"))
  (ddfdt-log fh "INFO" "lispsys" (vl-princ-to-string (getvar "LISPSYS")))
  (ddfdt-build fh)
  (if (= mode "full") (ddfdt-units fh))
  ;; 注入读图失败：导出过程中调用不存在的函数
  (if (= mode "fail")
    (defun ddfd-bulge-length (p1 p2 bulge) (ddfdt-no-such-function p1)))
  (setq r (vl-catch-all-apply 'ddfd-do-export (list outdir)))
  (ddfdt-log fh "EXPORT" mode (cond ((vl-catch-all-error-p r) (strcat "ERROR " (vl-catch-all-error-message r)))
                                     (r "T")
                                     (T "NIL"))))

(defun ddfdt-run (logpath outdir mode / fh r)
  (setq fh (open logpath "w"))
  (setq r (vl-catch-all-apply 'ddfdt-run-body (list fh outdir mode)))
  (if (vl-catch-all-error-p r) (ddfdt-log fh "ERROR" mode (vl-catch-all-error-message r)))
  (ddfdt-log fh "DONE" mode "")
  (close fh)
  (princ))
'''

# GBK 尾字节落在 ASCII 范围的生僻字：MBCS 版 AutoLISP 里逐字节处理会把它们拆坏
RARE_A0 = "仩"   # 81 A0：旧版按 160 去空白会删掉尾字节
RARE_5C = "乗"   # 81 5C：尾字节是反斜杠，后面跟 P 会被当成 \P
RARE_7D = "倉"   # 82 7D：尾字节是 }
RARE_7B = "亄"   # 81 7B：尾字节是 {
RARE_42 = "丅"   # 81 42：尾字节是 B
RARE_62 = "乥"   # 81 62：尾字节是 b（字母）
RARE_TEXT = f"{RARE_A0} {RARE_5C}柜"
RARE_MTEXT = f"{{\\fSimSun;{RARE_5C}P{RARE_7D}{RARE_7B}1}}"


def lisp_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def arc_len(chord: float, bulge: float) -> float:
    theta = 4 * math.atan(abs(bulge))
    return chord * theta / (2 * math.sin(theta / 2))


# ---------------------------------------------------------------- 纯函数用例

FLOOR_CASES = [
    ("CABLE_ROUTE_1F", "1F"), ("CABLE_ROUTE_2F", "2F"), ("CABLE_ROUTE_12F", "12F"), ("CABLE_ROUTE_21F", "21F"),
    ("CABLE_ROUTE_99F", "99F"), ("CABLE_ROUTE_B1F", "B1F"), ("CABLE_ROUTE_B2F", "B2F"), ("cable_route_b1f", "B1F"),
    ("CABLE_ROUTE_01F", "1F"), ("CABLE_ROUTE_B01F", "B1F"), ("CABLE_ROUTE_SUB1F", "1F"), ("ABCB1F", "1F"),
    ("X_B12F", "B12F"), ("CABLE_ROUTE", ""), ("CABLE_ROUTE_F1", ""), ("CABLE_ROUTE_B1", ""),
    ("CABLE_ROUTE_1F_B", "1F"), ("CABLE_ROUTE_100F", ""), ("CABLE_ROUTE_0F", ""),
    ("电缆沟二层", "2F"), ("电缆沟一楼", "1F"), ("电缆沟十层", "10F"), ("电缆沟十二层", "12F"), ("电缆沟二十层", "20F"),
    ("电缆沟二十三楼", "23F"), ("地下一层", "B1F"), ("负二层", "B2F"), ("两层", "2F"), ("电缆沟", ""), ("一二层", ""),
    ("CABLE_ROUTE_一层_2F", "1F"), (f"CABLE_ROUTE_{RARE_42}1F", "1F"), (f"CABLE_ROUTE_{RARE_62}B1F", "B1F"),
    (f"CABLE_ROUTE_{RARE_A0}1F", "1F"), ("CABLE_ROOM_1F", "1F"),
]
# LISP 只认 1~99 楼，Python 的 infer_floor_from_layer 对这些会给出 100F / 0F
PY_FLOOR_DIFFERENCES = {"CABLE_ROUTE_100F", "CABLE_ROUTE_0F"}

MTEXT_CASES = [
    (r"{\fSimSun|b0|i0|c134|p2;\C1;\L2号\l\P\H0.7x;低压柜\~A\S1^2;\{X\}}", "2号 低压柜 A1/2{X}"),
    (r"\pxqc;{\Q15;AB}\PCD", "AB CD"),
    (r"A\\B", "A\\B"),
    (r"\S3#4;", "3/4"),
    (r"\S5/8;", "5/8"),
    (r"\T1.1;X", "X"),
    (r"\A1;50%%d", "50°"),
    (r"\W0.8;\OOver\o\KStrike\k", "OverStrike"),
    (r"{\fArial|b1;ZJ2_1F}", "ZJ2_1F"),
    ("A\\", "A\\"),
    (r"1\N2", "1 2"),
    (RARE_MTEXT, f"{RARE_5C}P{RARE_7D}{RARE_7B}1"),
    (r"\H2.5x;\c255;\Fromans|c0;ABC", "ABC"),
]

TEXT_CASES = [
    ("%%u6AL%%u", "6AL"), ("%%c%%d%%p%%%%%0657AL", "Φ°±%A7AL"), ("100%", "100%"), ("%%", "%%"), ("%%z", "%%z"),
    ("%%1", "%%1"), ("%%0317AL", "%%0317AL"), ("%%O上划%%o", "上划"), ("A%%", "A%%"),
]

LISP_EXPR_CASES = [
    # (编号, LISP 表达式, 期望值；float 按数值比较)
    ("esc:1", '(ddfd-csv-escape "a\\"b\\"c")', '"a""b""c"'),
    ("esc:2", '(ddfd-csv-escape "\\"\\"")', '""""""'),
    ("esc:3", '(ddfd-csv-escape "")', '""'),
    ("esc:4", "(ddfd-csv-escape nil)", '""'),
    ("rep:1", '(ddfd-string-replace-all "xx" "x" "axbx")', "axxbxx"),
    ("rep:2", '(ddfd-string-replace-all "" "ab" "abcab")', "c"),
    ("rep:3", '(ddfd-string-replace-all "b" "" "abc")', "abc"),
    ("sp:1", '(ddfd-remove-spaces " 1 A\\tL\\n")', "1AL"),
    ("sp:2", f"(ddfd-remove-spaces {lisp_str(RARE_TEXT)})", f"{RARE_A0}{RARE_5C}柜"),
    ("sp:3", '(ddfd-remove-spaces "　全角")', "　全角"),
    ("path:1", '(ddfd-clean-path " \\"E:\\\\proj\\\\x\\" ")', "E:\\proj\\x"),
    ("bulge:1", "(ddfd-bulge-length '(0.0 0.0 0.0) '(4000.0 0.0 0.0) 0.5)", arc_len(4000, 0.5)),
    ("bulge:2", "(ddfd-bulge-length '(0.0 0.0 0.0) '(4000.0 0.0 0.0) 1.0)", arc_len(4000, 1.0)),
    ("bulge:3", "(ddfd-bulge-length '(0.0 0.0 0.0) '(4000.0 0.0 0.0) -1.0)", arc_len(4000, 1.0)),
    ("bulge:4", "(ddfd-bulge-length '(0.0 0.0 0.0) '(4000.0 0.0 0.0) 0.0)", 4000.0),
    ("bulge:5", "(ddfd-bulge-length '(0.0 0.0 0.0) '(3000.0 4000.0 0.0) nil)", 5000.0),
    # 认不出的 \x 原样保留；但 Unicode 版 AutoLISP 的 vl-string->list 会把 \U+XXXX 直接换成对应字符（长度 1），
    # MBCS 版保留 7 个字符。期望值在 check_units 里按会话的 MBCS 标志决定
    ("mtext:u", '(strlen (ddfd-mtext-plain "\\\\U+4E2D"))', None),
    ("mbcs", "(if (ddfd-mbcs-p) \"T\" \"NIL\")", None),
]


def unit_cases() -> list[tuple[str, str, object]]:
    cases: list[tuple[str, str, object]] = []
    for i, (layer, want) in enumerate(FLOOR_CASES):
        cases.append((f"floor:{i}", f"(ddfd-layer-floor {lisp_str(layer)})", want))
    for i, (raw, want) in enumerate(MTEXT_CASES):
        cases.append((f"mtext:{i}", f"(ddfd-mtext-plain {lisp_str(raw)})", want))
    for i, (raw, want) in enumerate(TEXT_CASES):
        cases.append((f"text:{i}", f"(ddfd-text-plain {lisp_str(raw)})", want))
    cases.extend(LISP_EXPR_CASES)
    return cases


def fixture_source() -> str:
    body = FIXTURE.replace("@RARE_TEXT@", lisp_str(RARE_TEXT)[1:-1]).replace("@RARE_MTEXT@", lisp_str(RARE_MTEXT)[1:-1])
    lines = ["(defun ddfdt-units (fh)"]
    for case_id, expr, _ in unit_cases():
        lines.append(f"  (ddfdt-case fh {lisp_str(case_id)} '(lambda () {expr}))")
    lines.append("  (princ))")
    return body + "\n" + "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 期望的导出结果

ARC_P1 = arc_len(4000, 0.5)
ARC_PL3 = arc_len(4000, 1.0)
QUARTER = 5000 * math.pi / 2

ROUTES = {  # 标签: (楼层, 图层, [(段号, 起点X, 起点Y, 终点X, 终点Y, 长度)])
    "L1": ("1F", "CABLE_ROUTE_1F", [(1, 0, 0, 10000, 0, 10000)]),
    "P1": ("1F", "CABLE_ROUTE_1F", [(1, 10000, 0, 14000, 0, 4000), (2, 14000, 0, 18000, 0, ARC_P1),
                                    (4, 18000, 0, 18000, 6000, 6000)]),
    "P2": ("1F", "CABLE_ROUTE_1F", [(1, 18000, 6000, 22000, 6000, 4000), (2, 22000, 6000, 22000, 10000, 4000),
                                    (3, 22000, 10000, 18000, 10000, 4000), (4, 18000, 10000, 18000, 6000, 4000)]),
    "PL3": ("1F", "CABLE_ROUTE_1F", [(1, 0, 0, 0, -5000, 5000), (2, 0, -5000, 0, -9000, ARC_PL3)]),
    "A1": ("1F", "CABLE_ROUTE_1F", [(1, 10000, 0, 5000, -5000, QUARTER)]),
    "P3D": ("1F", "CABLE_ROUTE_1F", [(1, 22000, 10000, 22000, 13000, 5000)]),
    "L2": ("2F", "CABLE_ROUTE_2F", [(1, 0, 0, 20000, 0, 20000)]),
    "A2": ("2F", "CABLE_ROUTE_2F", [(1, 25000, 5000, 20000, 0, QUARTER)]),
    "L3": ("B1F", "CABLE_ROUTE_B1F", [(1, 0, 0, 8000, 0, 8000)]),
}
NO_ROUTE_ROWS = ("Z1", "S1", "E1", "M1", "PS1")
UNSUPPORTED = {"S1": "SPLINE", "E1": "ELLIPSE", "M1": "POLYLINE网格"}

CAB1 = "CABLE_CABINET_1F"
CABINETS = {  # 标签: (柜名, 楼层, X, Y, 图层, 对象类型)；柜名 None 的运行时按清单决定（动态块原名 / 匿名块名）
    "C_1AL": ("1AL", "1F", 1000, 500, CAB1, "TEXT"),
    "C_QUOTE": ('柜"A"1', "1F", 9000, 500, CAB1, "TEXT"),
    "C_MTEXT": ("2号低压柜A1/2{X}", "1F", 15000, 1500, CAB1, "MTEXT"),
    "C_LONG": ("3AL", "1F", 5000, -500, CAB1, "MTEXT"),
    "C_4AP": ("4AP", "1F", 12500, 800, CAB1, "INSERT"),
    "C_KG5": ("KG5", "1F", 16000, -1000, CAB1, "INSERT"),
    "C_DYN": (None, "1F", 8000, -700, CAB1, "INSERT"),
    "C_ANON": (None, "1F", 9500, -1500, CAB1, "INSERT"),
    "C_PCT": ("6AL", "1F", 2000, -600, CAB1, "TEXT"),
    "C_SYM": ("Φ°±%A7AL", "1F", 3000, 600, CAB1, "TEXT"),
    "C_SP": ("8AL", "1F", 4000, 400, CAB1, "TEXT"),
    "C_MIR": ("9AL", "1F", 6000, 700, CAB1, "TEXT"),
    "C_DUP1": ("DUP1", "1F", 7000, 300, CAB1, "TEXT"),
    "C_DUP2": ("DUP1", "2F", 7000, 300, "CABLE_CABINET_2F", "TEXT"),
    "C_CN": ("1号进线柜", "1F", 500, -800, CAB1, "TEXT"),
    "C_ARC": ("ARCAL", "1F", 7000, -3000, CAB1, "TEXT"),
    "C_RARE_T": (f"{RARE_A0}{RARE_5C}柜", "1F", 11000, -500, CAB1, "TEXT"),
    "C_RARE_MT": (f"{RARE_5C}P{RARE_7D}{RARE_7B}1", "1F", 11500, 1000, CAB1, "MTEXT"),
    "C_2AL": ("2AL", "2F", 1000, 500, "CABLE_CABINET_2F", "TEXT"),
    "C_B1AL": ("B1AL", "B1F", 1000, 500, "CABLE_CABINET_B1F", "TEXT"),
    "C_NOFLOOR": ("NOFLOOR1", "", 500, 500, "CABLE_CABINET", "TEXT"),
}
EMPTY_CABINETS = ("C_BLANK", "C_POINT")
SHAFTS = {
    "S_ZJ1_1F": ("ZJ1_1F", "1F", 18000, 3000, "CABLE_SHAFT_1F", "TEXT"),
    "S_ZJ1_2F": ("ZJ1_2F", "2F", 18000, 500, "CABLE_SHAFT_2F", "TEXT"),
    "S_ZJ2_B1F": ("ZJ2_B1F", "B1F", 7000, 500, "CABLE_SHAFT_B1F", "TEXT"),
    "S_ZJ2_1F": ("ZJ2_1F", "1F", 7500, 200, "CABLE_SHAFT_1F", "MTEXT"),
}
ROOM_VERTICES = [(0, -2000), (12000, -2000), (12000, 2000), (0, 2000)]

HEADERS = {
    "柜子坐标.csv": ["柜子名称", "楼层", "房间", "X", "Y", "图层", "对象类型", "句柄"],
    "路径线段.csv": ["路径编号", "楼层", "起点X", "起点Y", "终点X", "终点Y", "长度_CAD单位", "图层", "句柄", "段号"],
    "竖井.csv": ["竖井编号", "楼层", "X", "Y", "高度", "图层", "对象类型", "句柄"],
    "房间范围.csv": ["房间名称", "楼层", "顶点序号", "X", "Y", "图层", "句柄"],
}

# 端到端：(电缆编号, 起点, 终点, 期望基础路径长度[m], 是否拐弯)
SHAFT_H = 4.5
CABLES = [
    ("E1", "1AL", "9AL", 0.5 + 5.0 + 0.7, False),
    ("E2", "1AL", "2AL", 0.5 + 9.0 + 4.0 + ARC_P1 / 1000 + 3.0 + SHAFT_H + 0.5 + 17.0 + 0.5, True),
    ("E3", "B1AL", "2AL", 0.5 + 6.0 + 0.5 + SHAFT_H + 0.2 + 2.5 + 4.0 + ARC_P1 / 1000 + 3.0 + SHAFT_H + 18.0, True),
    ("E4", '柜"A"1', "1号进线柜", 0.5 + 9.0 + 0.8 + 0.5, True),
    ("E5", "2号低压柜A1/2{X}", "KG5", 1.5 + 0.25 * ARC_P1 / 1000 + 1.0, False),
    ("E6", "Φ°±%A7AL", "6AL", 0.6 + 1.0 + 0.6, False),
    ("E7", "3AL", "B1AL", 0.5 + 2.5 + 0.2 + SHAFT_H + 0.5 + 6.0 + 0.5, True),
    ("E8", "4AP", "2号低压柜A1/2{X}", 0.8 + 1.5 + 0.25 * ARC_P1 / 1000 + 1.5, False),
    ("E9", "@DYN@", "1AL", 0.7 + 7.0 + 0.5, False),
    ("E10", "ARCAL", "1AL", 0.6 * QUARTER / 1000 + 9.0 + 0.5, True),
    ("E11", f"{RARE_A0}{RARE_5C}柜", f"{RARE_5C}P{RARE_7D}{RARE_7B}1", 0.5 + 0.5 + 1.0, False),
    ("E12", "8AL", "9AL", 0.4 + 2.0 + 0.7, False),
]


# ---------------------------------------------------------------- 工具


class Checker:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passed = 0

    def check(self, ok: bool, message: str) -> bool:
        if ok:
            self.passed += 1
        else:
            self.failures.append(message)
            print("  失败:", message)
        return ok

    def near(self, got: str | float, want: float, message: str, tol: float = 2e-5) -> bool:
        try:
            value = float(got)
        except (TypeError, ValueError):
            return self.check(False, f"{message}: 不是数字 {got!r}")
        return self.check(abs(value - want) <= tol, f"{message}: 得到 {value!r}，期望 {want!r}")


def write_gbk(path: Path, text: str) -> None:
    path.write_bytes(text.replace("\r\n", "\n").replace("\n", "\r\n").encode("gbk"))


def run_console(console: Path, scr: Path, cwd: Path, timeout: int = 300) -> str:
    proc = subprocess.run(
        [str(console), "/s", str(scr)], cwd=cwd, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout,
    )
    out = proc.stdout.decode("utf-16le", errors="replace")
    (cwd / "console.txt").write_text(out, encoding="utf-8")
    return out


def parse_log(path: Path) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    if not path.exists():
        return result
    for line in path.read_bytes().decode("gbk").splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3:
            result.setdefault(parts[0], {})[parts[1]] = parts[2]
    return result


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    text = path.read_bytes().decode("gbk")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    header = rows[0] if rows else []
    return header, [dict(zip(header, row)) for row in rows[1:]]


def run_session(console: Path, name: str, lsp: Path, mode: str, outdir: Path, fixture: Path) -> tuple[dict, str]:
    folder = WORK / name
    folder.mkdir(parents=True, exist_ok=True)
    log = folder / "run.log"
    scr = folder / "run.scr"
    script = [
        '(setvar "SECURELOAD" 0)',
        f"(load {lisp_str(lsp.as_posix())})",
        f"(load {lisp_str(fixture.as_posix())})",
        f"(ddfdt-run {lisp_str(log.as_posix())} {lisp_str(str(outdir) + os.sep)} {lisp_str(mode)})",
        '(command "_QUIT" "_Y")',
        "",
    ]
    write_gbk(scr, "\n".join(script))
    out = run_console(console, scr, folder)
    return parse_log(log), out


# 向导第 2 步（标柜子）实跑：UCS 原点移到 (1000,1000) 并绕 Z 转 90 度，点 (100,200) 标 1AL；
# 在 (300,200) 再标 1AL 回答 N（不标）；在 (500,200) 标 "1 A L"（去空格后重名）回答 Y（照标）。
# ddfd-wiz-floors 换成固定 1 层，不读写注册表里的楼层数
STEP2_DRIVER = r'''
(defun ddfd-wiz-floors () 1)
(defun ddfdt-step2-prep ()
  (setvar "OSMODE" 0)
  (command "_.UCS" "_O" "1000,1000,0")
  (command "_.UCS" "_Z" "90")
  (princ))
(defun ddfdt-step2-report (logpath / fh ss i ed)
  (setq fh (open logpath "w"))
  (if (setq ss (ssget "_X" '((0 . "TEXT") (8 . "CABLE_CABINET_1F"))))
    (progn
      (setq i 0)
      (while (< i (sslength ss))
        (setq ed (entget (ssname ss i)) i (1+ i))
        (ddfdt-log fh "T" (cdr (assoc 1 ed))
                   (strcat (rtos (cadr (assoc 10 ed)) 2 6) "," (rtos (caddr (assoc 10 ed)) 2 6))))))
  (ddfdt-log fh "INFO" "clayer" (getvar "CLAYER"))
  (ddfdt-log fh "DONE" "step2" "")
  (close fh)
  (princ))
'''
STEP2_ANSWERS = ["", "Y", "100,200", "1AL", "300,200", "1AL", "N", "500,200", "1 A L", "Y", ""]


def run_step2_session(console: Path, fixture: Path) -> tuple[dict, str, list[tuple[str, str]]]:
    folder = WORK / "wizard_step2"
    folder.mkdir(parents=True, exist_ok=True)
    driver = folder / "step2_driver.lsp"
    write_gbk(driver, STEP2_DRIVER)
    log = folder / "run.log"
    scr = folder / "run.scr"
    script = [
        '(setvar "SECURELOAD" 0)',
        f"(load {lisp_str(WIZARD.as_posix())})",
        f"(load {lisp_str(fixture.as_posix())})",
        f"(load {lisp_str(driver.as_posix())})",
        "(ddfdt-step2-prep)",
        "(c:DDFD_CABLE_CABINETS)",
        *STEP2_ANSWERS,
        f"(ddfdt-step2-report {lisp_str(log.as_posix())})",
        '(command "_QUIT" "_Y")',
        "",
    ]
    write_gbk(scr, "\n".join(script))
    out = run_console(console, scr, folder)
    texts = []
    if log.exists():
        for line in log.read_bytes().decode("gbk").splitlines():
            parts = line.split("|", 2)
            if parts[0] == "T" and len(parts) == 3:
                texts.append((parts[1], parts[2]))
    return parse_log(log), out, texts


def check_step2(ck: Checker, log: dict, out: str, texts: list[tuple[str, str]]) -> None:
    ck.check("DONE" in log, f"[wizard_step2] 没有跑完，控制台输出见 {WORK / 'wizard_step2' / 'console.txt'}")
    # UCS (x,y) -> WCS (1000 - y, 1000 + x)
    want = sorted([("1AL", (800.0, 1100.0)), ("1 A L", (800.0, 1500.0))])
    got = sorted((name, tuple(float(v) for v in xy.split(","))) for name, xy in texts)
    ck.check(len(got) == 2 and [g[0] for g in got] == [w[0] for w in want]
             and all(abs(a - b) < 1e-6 for g, w in zip(got, want) for a, b in zip(g[1], w[1])),
             f"[wizard_step2] 柜名文字 {got}，期望 {want}（第二个 1AL 应被拒绝，坐标应是 UCS 点换算的 WCS）")
    ck.check(out.count("柜名「1AL」已经标过") == 1 and out.count("柜名「1 A L」已经标过") == 1,
             "[wizard_step2] 重名柜名没有提示确认")
    ck.check("已跳过，本点作废" in out, "[wizard_step2] 回答 N 后没有跳过")
    ck.check(log.get("INFO", {}).get("clayer") == "0", f"[wizard_step2] 当前图层没有恢复: {log.get('INFO')}")


def set_lispsys(console: Path, value: int) -> None:
    folder = WORK / f"lispsys_{value}"
    folder.mkdir(parents=True, exist_ok=True)
    scr = folder / "run.scr"
    write_gbk(scr, f'(setvar "LISPSYS" {value})\n(command "_QUIT" "_Y")\n')
    run_console(console, scr, folder)


def probe_lispsys(console: Path) -> tuple[str, str]:
    folder = WORK / "lispsys_probe"
    folder.mkdir(parents=True, exist_ok=True)
    log = folder / "probe.log"
    scr = folder / "run.scr"
    write_gbk(scr, "\n".join([
        f'(setq fh (open {lisp_str(log.as_posix())} "w"))',
        '(princ (getvar "LISPSYS") fh) (princ "|" fh) (princ (strlen "中") fh)',
        "(close fh)",
        '(command "_QUIT" "_Y")',
        "",
    ]))
    run_console(console, scr, folder)
    text = log.read_bytes().decode("gbk") if log.exists() else "?|?"
    lispsys, _, strlen = text.partition("|")
    return lispsys.strip(), strlen.strip()


# ---------------------------------------------------------------- 核对


def check_session_basics(ck: Checker, name: str, log: dict, out: str, mode: str, export_ok: bool) -> None:
    ck.check("DONE" in log, f"[{name}] 日志没有 DONE（LISP 中途出错？控制台输出见 {WORK / name / 'console.txt'}）")
    ck.check("ERROR" not in log, f"[{name}] 夹具出错: {log.get('ERROR')}")
    failed = [k for k, v in log.get("M", {}).items() if v == "FAILED"]
    ck.check(not failed, f"[{name}] entmake 失败的夹具图元: {failed}")
    want = "T" if export_ok else "NIL"
    ck.check(log.get("EXPORT", {}).get(mode) == want, f"[{name}] ddfd-do-export 返回 {log.get('EXPORT')}，期望 {want}")
    ck.check("2026-09-26" in log.get("INFO", {}).get("version", ""), f"[{name}] 版本号 {log.get('INFO')}")


def check_units(ck: Checker, name: str, log: dict) -> None:
    got = log.get("U", {})
    mbcs = log.get("INFO", {}).get("mbcs") == "T"
    ck.check(got.get("mbcs") == ("T" if mbcs else "NIL"), f"[{name}] ddfd-mbcs-p 与会话不符: {got.get('mbcs')}")
    ck.near(got.get("mtext:u", "?"), 7.0 if mbcs else 1.0, f"[{name}] \\U+4E2D 去格式后的长度")
    for case_id, expr, want in unit_cases():
        if case_id not in got:
            ck.check(False, f"[{name}] 用例 {case_id} 没有结果: {expr}")
            continue
        value = got[case_id]
        if want is None:
            continue
        if isinstance(want, float):
            ck.near(value, want, f"[{name}] {expr}", tol=2e-6)
        else:
            ck.check(value == want, f"[{name}] {expr} 得到 {value!r}，期望 {want!r}")
    # 楼层解析与 Python 的 infer_floor_from_layer 对照
    for i, (layer, _) in enumerate(FLOOR_CASES):
        if layer in PY_FLOOR_DIFFERENCES:
            continue
        py = infer_floor_from_layer(layer)
        py = normalize_floor(py) if py else ""
        ck.check(got.get(f"floor:{i}") == py, f"[{name}] 楼层 {layer!r}: LISP {got.get(f'floor:{i}')!r} Python {py!r}")


def check_csvs(ck: Checker, name: str, outdir: Path, manifest: dict[str, str], dyn_name: str) -> None:
    for fn in FILES:
        path = outdir / fn
        if not ck.check(path.exists(), f"[{name}] 缺少 {fn}"):
            return
        raw = path.read_bytes()
        ck.check(b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b""), f"[{name}] {fn} 换行不是 CRLF")
        header, _ = read_csv(path)
        ck.check(header == HEADERS[fn], f"[{name}] {fn} 表头 {header}")
        for line in raw.decode("gbk").splitlines():
            ck.check(line.startswith('"') and line.endswith('"'), f"[{name}] {fn} 单元格没有加引号: {line}")
    leftovers = [p.name for p in outdir.iterdir() if p.suffix in (".pending", ".previous")]
    ck.check(not leftovers, f"[{name}] 导出目录残留 {leftovers}")

    # 路径线段
    _, rows = read_csv(outdir / "路径线段.csv")
    by_handle: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        by_handle.setdefault(row["句柄"], []).append(row)
    expected_count = sum(len(segs) for _, _, segs in ROUTES.values())
    ck.check(len(rows) == expected_count, f"[{name}] 路径线段 {len(rows)} 行，期望 {expected_count}")
    for label, (floor, layer, segs) in ROUTES.items():
        handle = manifest.get(label)
        got = by_handle.get(handle, [])
        ck.check([r["段号"] for r in got] == [str(s[0]) for s in segs],
                 f"[{name}] {label} 段号 {[r['段号'] for r in got]}，期望 {[s[0] for s in segs]}")
        for row, (segno, x1, y1, x2, y2, length) in zip(got, segs):
            where = f"[{name}] {label} 段{segno}"
            ck.check(row["路径编号"] == f"R{handle}", f"{where} 路径编号 {row['路径编号']}")
            ck.check(row["楼层"] == floor, f"{where} 楼层 {row['楼层']!r}，期望 {floor}")
            ck.check(row["图层"] == layer, f"{where} 图层 {row['图层']}")
            for key, want in (("起点X", x1), ("起点Y", y1), ("终点X", x2), ("终点Y", y2), ("长度_CAD单位", length)):
                ck.near(row[key], want, f"{where} {key}")
    for label in NO_ROUTE_ROWS:
        ck.check(manifest.get(label) not in by_handle, f"[{name}] {label} 不应导出")

    # 柜子
    _, rows = read_csv(outdir / "柜子坐标.csv")
    by_handle1 = {row["句柄"]: row for row in rows}
    ck.check(len(rows) == len(CABINETS), f"[{name}] 柜子 {len(rows)} 行，期望 {len(CABINETS)}")
    for label, (cab, floor, x, y, layer, typ) in CABINETS.items():
        row = by_handle1.get(manifest.get(label, "?"))
        if not ck.check(row is not None, f"[{name}] 柜子 {label} 没有导出"):
            continue
        cab = (dyn_name if label == "C_DYN" else manifest.get("C_ANON_NAME")) if cab is None else cab
        ck.check(row["柜子名称"] == cab, f"[{name}] {label} 柜名 {row['柜子名称']!r}，期望 {cab!r}")
        ck.check(row["楼层"] == floor, f"[{name}] {label} 楼层 {row['楼层']!r}，期望 {floor!r}")
        ck.check(row["图层"] == layer and row["对象类型"] == typ and row["房间"] == "",
                 f"[{name}] {label} 图层/类型/房间 {row}")
        ck.near(row["X"], x, f"[{name}] {label} X")
        ck.near(row["Y"], y, f"[{name}] {label} Y")
    for label in EMPTY_CABINETS + ("C_PAPER",):
        ck.check(manifest.get(label) not in by_handle1, f"[{name}] {label} 不应导出")
    ck.check('"柜""A""1"' in (outdir / "柜子坐标.csv").read_bytes().decode("gbk"), f"[{name}] 柜名里的双引号没有全部加倍")

    # 竖井
    _, rows = read_csv(outdir / "竖井.csv")
    by_handle2 = {row["句柄"]: row for row in rows}
    ck.check(len(rows) == len(SHAFTS), f"[{name}] 竖井 {len(rows)} 行，期望 {len(SHAFTS)}")
    for label, (sid, floor, x, y, layer, typ) in SHAFTS.items():
        row = by_handle2.get(manifest.get(label, "?"))
        if not ck.check(row is not None, f"[{name}] 竖井 {label} 没有导出"):
            continue
        ck.check((row["竖井编号"], row["楼层"], row["高度"], row["图层"], row["对象类型"]) == (sid, floor, "", layer, typ),
                 f"[{name}] {label} {row}")
        ck.near(row["X"], x, f"[{name}] {label} X")
        ck.near(row["Y"], y, f"[{name}] {label} Y")

    # 房间
    _, rows = read_csv(outdir / "房间范围.csv")
    ck.check(len(rows) == len(ROOM_VERTICES), f"[{name}] 房间顶点 {len(rows)} 行，期望 {len(ROOM_VERTICES)}（未命名多段线不应导出）")
    for i, (row, (x, y)) in enumerate(zip(rows, ROOM_VERTICES), start=1):
        ck.check((row["房间名称"], row["楼层"], row["顶点序号"], row["图层"], row["句柄"])
                 == ("配电室", "1F", str(i), "CABLE_ROOM_1F", manifest.get("R_ROOM")), f"[{name}] 房间顶点 {row}")
        ck.near(row["X"], x, f"[{name}] 房间顶点{i} X")
        ck.near(row["Y"], y, f"[{name}] 房间顶点{i} Y")


def check_summary(ck: Checker, name: str, out: str, manifest: dict[str, str], outdir: Path) -> None:
    want = "已导出四个CSV：柜子 21 个（B1F 1 / 1F 17 / 2F 2 / 楼层未知 1）、路径线段 15 段、竖井 4 个、房间 1 个。"
    ck.check(want in out, f"[{name}] 缺少汇总行: {want}")
    ck.check("房间: 1 个" in out and "1F 配电室" in out, f"[{name}] 房间列表没有打印")
    ck.check("CABLE_ROOM 图层上有 1 个未命名多段线" in out, f"[{name}] 没有提示未命名房间多段线")
    # 重名组内句柄按 CSV 顺序列出，第一个就是 Python 采用的那条
    _, rows = read_csv(outdir / "柜子坐标.csv")
    dup_handles = [row["句柄"] for row in rows if row["柜子名称"] == "DUP1"]
    want_dup = f"柜名重复 1 个，统计时只用每组第一个句柄的坐标，请改名或删掉多余的：DUP1（句柄 {'、'.join(dup_handles)}）"
    ck.check(len(dup_handles) == 2 and want_dup in out, f"[{name}] 柜名重复提示不对，期望 {want_dup}")
    empty_lines = [line for line in out.splitlines() if "个图元没有柜名" in line]
    ck.check(bool(empty_lines) and "有 2 个图元没有柜名" in empty_lines[0]
             and all(manifest[k] in empty_lines[0] for k in EMPTY_CABINETS), f"[{name}] 空柜名提示不对: {empty_lines}")
    unsup = [line for line in out.splitlines() if "不支持的图元" in line]
    ok = bool(unsup) and "CABLE_ROUTE 图层上有 3 个不支持的图元（" in unsup[0] and "请炸开或改画多段线" in unsup[0]
    ok = ok and all(t in unsup[0] and manifest[k] in unsup[0] for k, t in UNSUPPORTED.items())
    ck.check(ok, f"[{name}] 不支持图元提示不对: {unsup}")
    ck.check("看不出楼层" in out and "CABLE_CABINET（1 个图元）" in out, f"[{name}] 没有提示看不出楼层的图层")
    ck.check("布局（图纸空间）里有 2 个 CABLE_* 图元" in out, f"[{name}] 没有提示布局里的图元")


def run_e2e(ck: Checker, data_src: Path, dyn_name: str) -> None:
    import openpyxl

    project = WORK / "e2e"
    data = project / "data"
    outputs = project / "outputs"
    data.mkdir(parents=True)
    outputs.mkdir()
    for fn in FILES:
        shutil.copy(data_src / fn, data / fn)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["电缆编号", "起点", "终点", "电缆长度"])
    cables = [(cid, dyn_name if a == "@DYN@" else a, b, base, bent) for cid, a, b, base, bent in CABLES]
    for cid, a, b, _, _ in cables:
        ws.append([cid, a, b, None])
    wb.save(project / "自动统计.xlsx")
    proc = subprocess.run(
        [sys.executable, str(ROOT / "calculate_cable_lengths.py"), "--workbook", str(project / "自动统计.xlsx"),
         "--data-dir", str(data), "--output", str(outputs / "自动统计_计算结果.xlsx")],
        cwd=ROOT, env={**os.environ, "PYTHONUTF8": "1"}, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    (project / "calculator_output.txt").write_text(proc.stdout + "\n" + proc.stderr, encoding="utf-8")
    if not ck.check(proc.returncode == 0, f"[e2e] calculate_cable_lengths.py 退出码 {proc.returncode}: {proc.stderr[-800:]}"):
        return
    detail = outputs / "统计明细.csv"
    if not ck.check(detail.exists(), "[e2e] 没有生成 统计明细.csv"):
        return
    with detail.open(encoding="utf-8-sig", newline="") as f:
        rows = {row["电缆编号"]: row for row in csv.DictReader(f)}
    for cid, a, b, base, bent in cables:
        row = rows.get(cid)
        if not ck.check(row is not None, f"[e2e] 统计明细缺少 {cid}"):
            continue
        ck.check(row["状态"] == "OK", f"[e2e] {cid} {a}->{b} 状态 {row['状态']!r}")
        ck.near(row["基础路径长度"], base, f"[e2e] {cid} {a}->{b} 基础路径长度", tol=0.0015)
        rule = row["规则"]
        ck.check(("不拐弯" not in rule) == bent, f"[e2e] {cid} 规则 {rule!r}，期望{'拐弯' if bent else '不拐弯'}")


def main() -> int:
    parser = argparse.ArgumentParser(description="accoreconsole 实跑验证 CAD 插件导出")
    parser.add_argument("--console", type=Path, default=DEFAULT_CONSOLE, help="accoreconsole.exe 路径")
    parser.add_argument("--mbcs", action="store_true", help="另用 LISPSYS=0（MBCS 版 AutoLISP）再跑一遍，跑完恢复")
    args = parser.parse_args()
    if not args.console.exists():
        print(f"找不到 accoreconsole: {args.console}")
        return 2
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)
    fixture = WORK / "ddfd_test_fixture.lsp"
    write_gbk(fixture, fixture_source())
    ck = Checker()

    def full_pass(tag: str) -> Path:
        """向导版完整导出 + 精简版单独加载导出，返回向导版导出目录。"""
        results = {}
        for name, lsp in (("wizard", WIZARD), ("export_only", EXPORT)):
            session = f"{tag}{name}"
            outdir = WORK / session / "out"
            outdir.mkdir(parents=True)
            print(f"== {session}: 加载 {lsp.name}，造图并导出")
            log, out = run_session(args.console, session, lsp, "full", outdir, fixture)
            check_session_basics(ck, session, log, out, "full", True)
            manifest = log.get("M", {})
            dyn = "DDFD_DYN_SRC" if manifest.get("C_DYN_XDATA") == "T" else manifest.get("C_DYN_ANON", "?")
            ck.check(manifest.get("C_LONG_HAS3") == "T", f"[{session}] 长 MTEXT 没有 3 组码，未测到分段拼接")
            ck.check(manifest.get("C_ANON_NAME", "").startswith("*U"), f"[{session}] 匿名块名 {manifest.get('C_ANON_NAME')}")
            print(f"   LISPSYS={log.get('INFO', {}).get('lispsys')} MBCS={log.get('INFO', {}).get('mbcs')} "
                  f"动态块XData={'有' if dyn == 'DDFD_DYN_SRC' else '无'}")
            check_units(ck, session, log)
            check_csvs(ck, session, outdir, manifest, dyn)
            check_summary(ck, session, out, manifest, outdir)
            results[name] = (outdir, manifest, dyn)
        (w_dir, w_man, _), (e_dir, e_man, _) = results["wizard"], results["export_only"]
        ck.check(w_man == e_man, f"[{tag}] 两次会话的图元句柄不同，无法逐字节对比")
        for fn in FILES:
            ck.check((w_dir / fn).read_bytes() == (e_dir / fn).read_bytes(), f"[{tag}] 精简版与向导版的 {fn} 不一致")
        return w_dir

    base_dir = full_pass("")
    dyn_name = "DDFD_DYN_SRC"
    wizard_log = parse_log(WORK / "wizard" / "run.log")
    if wizard_log.get("M", {}).get("C_DYN_XDATA") != "T":
        dyn_name = wizard_log.get("M", {}).get("C_DYN_ANON", "?")
    ck.check(wizard_log.get("INFO", {}).get("mbcs") == "NIL", "默认会话应是 Unicode 版 AutoLISP")

    # ---- 事务：失败不覆盖、残留 .previous 拒绝提交、清掉后重导 ----
    tx_dir = WORK / "tx_data"
    shutil.copytree(base_dir, tx_dir)
    snapshot = {fn: (tx_dir / fn).read_bytes() for fn in FILES}
    print("== tx_fail: 导出中途读图失败")
    log, out = run_session(args.console, "tx_fail", WIZARD, "fail", tx_dir, fixture)
    check_session_basics(ck, "tx_fail", log, out, "fail", False)
    ck.check("无法读取图元" in out and "本次没有覆盖项目数据" in out, "[tx_fail] 没有提示读图失败")
    ck.check(all((tx_dir / fn).read_bytes() == data for fn, data in snapshot.items()), "[tx_fail] 失败后原 CSV 被改动")
    ck.check(not list(tx_dir.glob("*.pending")) and not list(tx_dir.glob("*.previous")), "[tx_fail] 残留 .pending/.previous")

    print("== tx_previous: 残留上次未完成的 .previous")
    stale = tx_dir / "竖井.csv.previous"
    stale.write_bytes(b"stale")
    log, out = run_session(args.console, "tx_previous", WIZARD, "plain", tx_dir, fixture)
    check_session_basics(ck, "tx_previous", log, out, "plain", False)
    ck.check("提交失败" in out, "[tx_previous] 没有提示提交失败")
    ck.check(all((tx_dir / fn).read_bytes() == data for fn, data in snapshot.items()), "[tx_previous] 原 CSV 被改动")
    ck.check(stale.read_bytes() == b"stale", "[tx_previous] .previous 被覆盖")
    for p in list(tx_dir.glob("*.pending")) + [stale]:
        p.unlink()

    print("== tx_again: 清掉后重新导出")
    (tx_dir / "房间导出范围.txt").write_text("旧版本残留", encoding="utf-8")
    log, out = run_session(args.console, "tx_again", WIZARD, "plain", tx_dir, fixture)
    check_session_basics(ck, "tx_again", log, out, "plain", True)
    ck.check(all((tx_dir / fn).read_bytes() == data for fn, data in snapshot.items()), "[tx_again] 重导结果与第一次不同")
    ck.check(not [p for p in tx_dir.iterdir() if p.suffix in (".pending", ".previous")], "[tx_again] 残留 .pending/.previous")
    ck.check(not (tx_dir / "房间导出范围.txt").exists(), "[tx_again] 旧版 房间导出范围.txt 没有删掉")

    # ---- 向导第 2 步：重名确认、UCS 下标注位置 ----
    print("== wizard_step2: 标柜子（UCS 已旋转平移，含重名确认）")
    step2_log, step2_out, step2_texts = run_step2_session(args.console, fixture)
    check_step2(ck, step2_log, step2_out, step2_texts)

    # ---- 端到端 ----
    print("== e2e: 用导出的 CSV 跑 calculate_cable_lengths.py")
    run_e2e(ck, base_dir, dyn_name)

    # ---- MBCS 版 AutoLISP ----
    if args.mbcs:
        original = wizard_log.get("INFO", {}).get("lispsys", "1") or "1"
        print(f"== MBCS: 临时设 LISPSYS=0（原值 {original}），跑完恢复")
        try:
            set_lispsys(args.console, 0)
            mbcs_dir = full_pass("mbcs_")
            mbcs_log = parse_log(WORK / "mbcs_wizard" / "run.log")
            ck.check(mbcs_log.get("INFO", {}).get("mbcs") == "T", f"[mbcs] 没有切到 MBCS 版 AutoLISP: {mbcs_log.get('INFO')}")
            for fn in FILES:
                ck.check((mbcs_dir / fn).read_bytes() == (base_dir / fn).read_bytes(), f"[mbcs] {fn} 与 Unicode 版导出不一致")
        finally:
            set_lispsys(args.console, int(original))
            lispsys, strlen = probe_lispsys(args.console)
            print(f"   已恢复 LISPSYS={lispsys}（strlen \"中\" = {strlen}）")
            ck.check(lispsys == original, f"LISPSYS 没有恢复成 {original}，当前 {lispsys}，请在 AutoCAD 里手动 (setvar \"LISPSYS\" {original})")

    print()
    print(f"通过 {ck.passed} 项检查，失败 {len(ck.failures)} 项；工作目录 {WORK}")
    for message in ck.failures:
        print("  -", message)
    return 1 if ck.failures else 0


if __name__ == "__main__":
    sys.exit(main())
