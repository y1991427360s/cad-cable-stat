;;; 多氟多电缆长度自动统计 - CAD 分步引导向导（自包含，含导出命令）
;;; 只需 APPLOAD 本文件，然后输入 DDFD_CABLE_WIZARD，按提示一步步做即可。
;;;
;;; 包含以下命令:
;;;   DDFD_CABLE_WIZARD        引导向导：建图层→标柜子→画路径→标竖井→导出并运行统计
;;;   DDFD_CABLE_LAYERS        第1步：按楼层数自动创建 CABLE_* 图层
;;;   DDFD_CABLE_CABINETS      第2步：逐个点柜子出线点并写柜名文字
;;;   DDFD_CABLE_ROUTES        第3步：沿电缆沟/桥架画走线路径（自动开端点捕捉）
;;;   DDFD_CABLE_SHAFTS        第4步：标跨楼层竖井（自动写 ZJn_楼层 配对文字）
;;;   DDFD_CABLE_EXPORT_RUN    第5步：导出3个CSV并复制到项目文件夹 data\，可直接运行统计
;;;   DDFD_EXPORT_CABLE_ROUTE  仅导出3个CSV（与 cad_export_cable_route.lsp 相同）
;;;
;;; 图层约定（n 为楼层号）：
;;;   CABLE_CABINET_nF  柜子出线点文字     CABLE_ROUTE_nF  电缆沟/桥架中心线
;;;   CABLE_SHAFT_nF    竖井文字（同一竖井各层命名 ZJ1_1F / ZJ1_2F 配对）

(vl-load-com)

;;; ================= 通用与导出部分 =================

(defun ddfd-csv-escape (s / txt)
  (setq txt (if s (vl-princ-to-string s) ""))
  (setq txt (vl-string-subst "\"\"" "\"" txt))
  (strcat "\"" txt "\"")
)

(defun ddfd-clean-text (s / txt)
  (setq txt (if s (vl-princ-to-string s) ""))
  (setq txt (vl-string-subst " " "\\P" txt))
  (setq txt (vl-string-subst "" "{" txt))
  (setq txt (vl-string-subst "" "}" txt))
  txt
)

(defun ddfd-remove-spaces (s / txt chars ch)
  (setq txt (ddfd-clean-text s))
  (setq chars (vl-string->list txt))
  (setq txt "")
  (foreach ch chars
    (if (not (member ch '(9 10 13 32 160 12288)))
      (setq txt (strcat txt (chr ch)))
    )
  )
  txt
)

(defun ddfd-write-csv-row (fh cells / first)
  (setq first T)
  (foreach c cells
    (if first
      (setq first nil)
      (princ "," fh)
    )
    (princ (ddfd-csv-escape c) fh)
  )
  (write-line "" fh)
)

;; 从图层名解析楼层，支持 1F~20F（先查大数字避免 12F 被误判成 2F）
(defun ddfd-layer-floor (layer / up n res)
  (setq up (strcase layer))
  (setq res "")
  (setq n 20)
  (while (and (> n 0) (= res ""))
    (if (wcmatch up (strcat "*" (itoa n) "F*"))
      (setq res (strcat (itoa n) "F"))
    )
    (setq n (1- n))
  )
  (if (= res "")
    (cond
      ((or (wcmatch layer "*二层*") (wcmatch layer "*二楼*")) (setq res "2F"))
      ((or (wcmatch layer "*一层*") (wcmatch layer "*一楼*")) (setq res "1F"))
    )
  )
  res
)

(defun ddfd-point->list (variant-or-list)
  (cond
    ((= (type variant-or-list) 'VARIANT)
     (vlax-safearray->list (vlax-variant-value variant-or-list)))
    ((= (type variant-or-list) 'SAFEARRAY)
     (vlax-safearray->list variant-or-list))
    (T variant-or-list)
  )
)

(defun ddfd-entity-point (ename / ent typ obj pt)
  (setq ent (entget ename))
  (setq typ (cdr (assoc 0 ent)))
  (setq obj (vlax-ename->vla-object ename))
  (cond
    ((member typ '("TEXT" "MTEXT" "POINT" "INSERT"))
     (cond
       ((= typ "INSERT") (ddfd-point->list (vla-get-InsertionPoint obj)))
       ((= typ "MTEXT") (ddfd-point->list (vla-get-InsertionPoint obj)))
       ((= typ "TEXT") (ddfd-point->list (vla-get-InsertionPoint obj)))
       (T (cdr (assoc 10 ent)))
     )
    )
    (T (cdr (assoc 10 ent)))
  )
)

(defun ddfd-block-first-attr (obj / attrs attr res)
  (setq res "")
  (if (= :vlax-true (vla-get-HasAttributes obj))
    (progn
      (setq attrs (vl-catch-all-apply 'vlax-invoke (list obj 'GetAttributes)))
      (if (not (vl-catch-all-error-p attrs))
        (foreach attr attrs
          (if (= res "")
            (setq res (vla-get-TextString attr))
          )
        )
      )
    )
  )
  res
)

(defun ddfd-entity-name (ename / ent typ obj nm)
  (setq ent (entget ename))
  (setq typ (cdr (assoc 0 ent)))
  (setq obj (vlax-ename->vla-object ename))
  (cond
    ((= typ "TEXT") (ddfd-clean-text (cdr (assoc 1 ent))))
    ((= typ "MTEXT") (ddfd-clean-text (vla-get-TextString obj)))
    ((= typ "INSERT")
     (setq nm (ddfd-block-first-attr obj))
     (if (= nm "")
       (if (vlax-property-available-p obj 'EffectiveName)
         (setq nm (vla-get-EffectiveName obj))
         (setq nm (vla-get-Name obj))
       )
     )
     (ddfd-clean-text nm)
    )
    (T "")
  )
)

(defun ddfd-ensure-folder (dir)
  (if (not (vl-file-directory-p dir))
    (vl-mkdir dir)
  )
)


(defun ddfd-room-name (ename / xd item value)
  (setq value "")
  (setq xd (assoc -3 (entget ename (list "DDFD_CABLE_ROOM"))))
  (if xd
    (progn
      (setq item (cadr xd))
      (if item (setq value (cdr (assoc 1000 (cdr item)))))
    )
  )
  (if value value "")
)

(defun ddfd-room-set-name (ename name / data)
  (regapp "DDFD_CABLE_ROOM")
  (setq data (entget ename))
  (entmod (append data (list (list -3 (list "DDFD_CABLE_ROOM" (cons 1000 name))))))
  (entupd ename)
)

(defun ddfd-room-has-arc (ename / ent typ cur item found)
  (setq ent (entget ename))
  (setq typ (cdr (assoc 0 ent)))
  (setq found nil)
  (if (= typ "LWPOLYLINE")
    (foreach item ent
      (if (and (= (car item) 42) (> (abs (cdr item)) 1e-9)) (setq found T))
    )
    (if (= typ "POLYLINE")
      (progn
        (setq cur (entnext ename))
        (while (and cur (= (cdr (assoc 0 (entget cur))) "VERTEX"))
          (if (and (assoc 42 (entget cur)) (> (abs (cdr (assoc 42 (entget cur)))) 1e-9)) (setq found T))
          (setq cur (entnext cur))
        )
      )
    )
  )
  found
)


(defun ddfd-room-vertices (en / ed typ cur vd pts pt elev flags item)
  (setq ed (entget en) typ (cdr (assoc 0 ed)) pts nil)
  (cond
    ((= typ "LWPOLYLINE")
     (setq elev (cond ((cdr (assoc 38 ed))) (T 0.0)))
     (foreach item ed
       (if (= (car item) 10)
         (progn (setq pt (cdr item))
           (setq pts (cons (trans (list (car pt) (cadr pt) elev) en 0) pts))))))
    ((= typ "POLYLINE")
     (setq flags (cond ((cdr (assoc 70 ed))) (T 0)))
     (if (= 0 (logand flags 88))
       (progn
         (setq cur (entnext en) elev (caddr (cdr (assoc 10 ed))))
         (if (not elev) (setq elev 0.0))
         (while (and cur (/= (cdr (assoc 0 (entget cur))) "SEQEND"))
           (setq vd (entget cur))
           (if (= (cdr (assoc 0 vd)) "VERTEX")
             (progn (setq pt (cdr (assoc 10 vd)))
               (setq pts (cons (trans (list (car pt) (cadr pt) elev) en 0) pts))))
           (setq cur (entnext cur)))))))
  (reverse pts))

(defun ddfd-room-closed-p (ename / ed pts)
  (setq ed (entget ename) pts (ddfd-room-vertices ename))
  (and (>= (length pts) 3)
       (or (= 1 (logand (cond ((cdr (assoc 70 ed))) (T 0)) 1))
           (equal (car pts) (last pts) 1e-6))))

(defun ddfd-export-one-room (fh en / ent layer floor name handle pts pt count)
  (setq ent (entget en) layer (cdr (assoc 8 ent)) floor (ddfd-layer-floor layer)
        name (ddfd-room-name en) handle (cdr (assoc 5 ent)))
  (if (or (not name) (= name "")) (setq name (strcat "房间_" handle)))
  (if (and (ddfd-room-closed-p en) (not (ddfd-room-has-arc en)))
    (progn
      (setq pts (ddfd-room-vertices en) count 0)
      (if (equal (car pts) (last pts) 1e-6)
        (setq pts (reverse (cdr (reverse pts)))))
      (foreach pt pts
        (setq count (1+ count))
        (ddfd-write-csv-row fh
          (list name floor (itoa count) (rtos (car pt) 2 6) (rtos (cadr pt) 2 6) layer handle)))
      count)))

(defun ddfd-room-has-named-p (/ ss ent ed found)
  (if (setq ss (ssget "_X" '((-3 ("DDFD_CABLE_ROOM")))))
    (> (sslength ss) 0)
    (progn
      (setq ent (entnext) found nil)
      (while (and ent (not found))
        (setq ed (entget ent (list "DDFD_CABLE_ROOM")))
        (if (and ed (assoc -3 ed)) (setq found T))
        (setq ent (entnext ent)))
      found)))

(defun ddfd-room-entity-p (ename / ent layer typ xd)
  (setq ent (entget ename))
  (setq layer (cdr (assoc 8 ent)))
  (setq typ (cdr (assoc 0 ent)))
  (setq xd (assoc -3 (entget ename (list "DDFD_CABLE_ROOM"))))
  (and (member typ '("LWPOLYLINE" "POLYLINE"))
       (or xd
           (and layer (wcmatch (strcase layer) "CABLE_ROOM*")
                (not (ddfd-room-has-named-p))))))

;;; ==========================================================================
;;; 模块: 房间导出范围管理 (ddfd-room-scope)
;;; 包含: ddfd-room-exportable-p, ddfd-room-scope-load, 
;;;       ddfd-room-scope-save, ddfd-room-scope-choose
;;; ==========================================================================

;; 辅助谓词: 校验实体是否为满足导出条件的有效闭合无弧房间
(defun ddfd-room-exportable-p (ent / ed pts area prev pt)
  (and ent (setq ed (entget ent))
    (/= (cdr (assoc 67 ed)) 1)
    (ddfd-room-entity-p ent) (ddfd-room-closed-p ent)
    (not (ddfd-room-has-arc ent))
    (progn
      (setq pts (ddfd-room-vertices ent) area 0.0 prev (last pts))
      (foreach pt pts
        (setq area (+ area (- (* (car prev) (cadr pt)) (* (car pt) (cadr prev)))) prev pt))
      (> (abs area) 1e-9))))

;; 加载持久化范围: 首行校验当前DWG全名，后续行校验图元句柄
;; 全有效才返回实体图元名列表，出现任何失效句柄或非房间图元立即放弃并关闭文件
(defun ddfd-room-scope-load (scopefile / fp curdwg line ent rooms valid)
  (if (and scopefile
           (= (type scopefile) 'STR)
           (findfile scopefile)
           (setq fp (open scopefile "r")))
    (progn
      (setq curdwg (strcat (getvar "DWGPREFIX") (getvar "DWGNAME"))
            valid  T)
      (setq line (read-line fp))
      (if (and line (= (vl-string-right-trim "\r\n " line) curdwg))
        (while (and valid (setq line (read-line fp)))
          (setq line (vl-string-trim " \t\r\n" line))
          (if (/= line "")
            (if (and (setq ent (handent line))
                     (ddfd-room-exportable-p ent))
              (setq rooms (cons ent rooms))
              (setq valid nil))))
        (setq valid nil))
      (close fp)
      (if (and valid rooms)
        (reverse rooms)
        nil))
    nil))

;; 保存房间范围至持久化文本文件
(defun ddfd-room-scope-save (scopefile rooms / fp ed hdl ent)
  (if (and scopefile
           (= (type scopefile) 'STR)
           (listp rooms)
           rooms
           (setq fp (open scopefile "w")))
    (progn
      (write-line (strcat (getvar "DWGPREFIX") (getvar "DWGNAME")) fp)
      (foreach ent rooms
        (if (and ent
                 (setq ed (entget ent))
                 (setq hdl (cdr (assoc 5 ed))))
          (write-line hdl fp)))
      (close fp)
      T)
    nil))

;; 交互提示并确定房间范围
;; 返回: 实体列表 / 符号 'NO-ROOMS / nil (用户取消或无有效房间)
(defun ddfd-room-scope-choose (scopefile / ent ed rooms)
  (setq ent (entnext))
  (while ent
    (setq ed (entget ent))
    (if (and ed
             (or (= (cdr (assoc 410 ed)) "Model")
                 (/= (cdr (assoc 67 ed)) 1))
             (ddfd-room-exportable-p ent)
             (not (member ent rooms)))
      (setq rooms (cons ent rooms)))
    (setq ent (entnext ent)))
  (if rooms (reverse rooms) 'NO-ROOMS))

(defun ddfd-export-rooms (filename rooms / fh result en ok)
  (if (setq fh (open filename "w"))
    (progn
      (ddfd-write-csv-row fh (list "房间名称" "楼层" "顶点序号" "X" "Y" "图层" "句柄"))
      (setq ok T)
      (if (/= rooms 'NO-ROOMS)
        (foreach en rooms
          (setq result (vl-catch-all-apply 'ddfd-export-one-room (list fh en)))
          (if (vl-catch-all-error-p result)
            (progn
              (princ (strcat "\n房间导出错误: " (vl-catch-all-error-message result)))
              (setq ok nil))
            (if (not result) (setq ok nil)))))
      (close fh)
      ok)
    nil))


(defun ddfd-export-point-layer (pattern filename header / ss fh i en ent obj layer typ pt nm handle)
  (setq fh (open filename "w"))
  (ddfd-write-csv-row fh header)
  (setq ss (ssget "_X" (list (cons 8 pattern))))
  (if ss
    (progn
      (setq i 0)
      (while (< i (sslength ss))
        (setq en (ssname ss i))
        (setq ent (entget en))
        (setq obj (vlax-ename->vla-object en))
        (setq layer (cdr (assoc 8 ent)))
        (setq typ (cdr (assoc 0 ent)))
        (setq pt (ddfd-entity-point en))
        (setq nm (ddfd-entity-name en))
        (if (wcmatch pattern "CABLE_CABINET*")
          (setq nm (ddfd-remove-spaces nm))
        )
        (setq handle (cdr (assoc 5 ent)))
        (if (and pt (>= (length pt) 2))
          (if (wcmatch pattern "CABLE_SHAFT*")
            (ddfd-write-csv-row fh
              (list nm (ddfd-layer-floor layer) (rtos (car pt) 2 6) (rtos (cadr pt) 2 6) "" layer typ handle))
            (ddfd-write-csv-row fh
              (list nm (ddfd-layer-floor layer) "" (rtos (car pt) 2 6) (rtos (cadr pt) 2 6) layer typ handle))
          )
        )
        (setq i (1+ i))
      )
    )
  )
  (close fh)
)

(defun ddfd-export-routes (filename / ss fh i en ent obj layer handle typ endp seg p1 p2 d routeid)
  (setq fh (open filename "w"))
  (ddfd-write-csv-row fh (list "路径编号" "楼层" "起点X" "起点Y" "终点X" "终点Y" "长度_CAD单位" "图层" "句柄" "段号"))
  (setq ss (ssget "_X" (list (cons 8 "CABLE_ROUTE*"))))
  (if ss
    (progn
      (setq i 0)
      (while (< i (sslength ss))
        (setq en (ssname ss i))
        (setq ent (entget en))
        (setq obj (vlax-ename->vla-object en))
        (setq layer (cdr (assoc 8 ent)))
        (setq handle (cdr (assoc 5 ent)))
        (setq typ (cdr (assoc 0 ent)))
        (setq routeid (strcat "R" handle))
        (cond
          ((= typ "LINE")
           (setq p1 (cdr (assoc 10 ent)))
           (setq p2 (cdr (assoc 11 ent)))
           (setq d (distance p1 p2))
           (ddfd-write-csv-row fh
             (list routeid (ddfd-layer-floor layer)
                   (rtos (car p1) 2 6) (rtos (cadr p1) 2 6)
                   (rtos (car p2) 2 6) (rtos (cadr p2) 2 6)
                   (rtos d 2 6) layer handle "1"))
          )
          ((member typ '("LWPOLYLINE" "POLYLINE"))
           (setq endp (fix (vlax-curve-getEndParam obj)))
           (setq seg 0)
           (while (< seg endp)
             (setq p1 (vlax-curve-getPointAtParam obj seg))
             (setq p2 (vlax-curve-getPointAtParam obj (1+ seg)))
             (setq d (- (vlax-curve-getDistAtParam obj (1+ seg))
                        (vlax-curve-getDistAtParam obj seg)))
             (ddfd-write-csv-row fh
               (list routeid (ddfd-layer-floor layer)
                     (rtos (car p1) 2 6) (rtos (cadr p1) 2 6)
                     (rtos (car p2) 2 6) (rtos (cadr p2) 2 6)
                     (rtos d 2 6) layer handle (itoa (1+ seg))))
             (setq seg (1+ seg))
           )
          )
        )
        (setq i (1+ i))
      )
    )
  )
  (close fh)
)

;; 导出3个CSV到 outdir（末尾需带反斜杠）
(defun ddfd-export-commit (outdir / names fn dst bak tmp saved installed ok)
  (setq names '("柜子坐标.csv" "路径线段.csv" "竖井.csv" "房间范围.csv") ok T)
  ;; Refuse an unfinished previous transaction instead of overwriting its backup.
  (foreach fn names
    (if (findfile (strcat outdir fn ".previous")) (setq ok nil)))
  (foreach fn names
    (if ok
      (progn
        (setq dst (strcat outdir fn) bak (strcat dst ".previous"))
        (if (findfile dst)
          (if (vl-file-rename dst bak) (setq saved (cons fn saved)) (setq ok nil))))))
  (foreach fn names
    (if ok
      (if (vl-file-rename (strcat outdir fn ".pending") (strcat outdir fn))
        (setq installed (cons fn installed)) (setq ok nil))))
  (if ok
    (foreach fn saved (vl-file-delete (strcat outdir fn ".previous")))
    (progn
      (foreach fn installed (vl-file-delete (strcat outdir fn)))
      (foreach fn saved
        (if (not (vl-file-rename (strcat outdir fn ".previous") (strcat outdir fn)))
          (princ (strcat "\n恢复失败，原文件保留在: " outdir fn ".previous"))))))
  ok)

(defun ddfd-export-stage (outdir rooms)
  (if (ddfd-export-rooms (strcat outdir "房间范围.csv.pending") rooms)
    (progn
      (ddfd-export-point-layer "CABLE_CABINET*" (strcat outdir "柜子坐标.csv.pending")
        (list "柜子名称" "楼层" "房间" "X" "Y" "图层" "对象类型" "句柄"))
      (ddfd-export-routes (strcat outdir "路径线段.csv.pending"))
      (ddfd-export-point-layer "CABLE_SHAFT*" (strcat outdir "竖井.csv.pending")
        (list "竖井编号" "楼层" "X" "Y" "高度" "图层" "对象类型" "句柄"))
      T)
    nil))

(defun ddfd-do-export (outdir / rooms scopefile result names fn)
  (setq scopefile (strcat outdir "房间导出范围.txt"))
  (setq rooms (ddfd-room-scope-choose scopefile))
  (if rooms
    (progn
      (setq result (vl-catch-all-apply 'ddfd-export-stage (list outdir rooms)))
      (if (and (not (vl-catch-all-error-p result)) result)
        (if (ddfd-export-commit outdir)
          (progn
            (if (= rooms 'NO-ROOMS)
              (if (findfile scopefile) (vl-file-delete scopefile))
              (if (not (ddfd-room-scope-save scopefile rooms))
                (princ "\n范围记录保存失败，下次需重新选择。")))
            (princ (strcat "\n已导出四个CSV；房间数量: "
              (itoa (if (= rooms 'NO-ROOMS) 0 (length rooms)))))
            T)
          (progn
            (princ "\n提交失败，请检查文件占用或 .previous 备份。")
            nil))
        (progn
          (if (vl-catch-all-error-p result)
            (princ (strcat "\n导出阶段发生错误: " (vl-catch-all-error-message result)))
            (princ "\n导出阶段未生成有效数据，停止后续运行。"))
          (setq names '("柜子坐标.csv" "路径线段.csv" "竖井.csv" "房间范围.csv"))
          (foreach fn names
            (if (findfile (strcat outdir fn ".pending"))
              (vl-file-delete (strcat outdir fn ".pending"))))
          nil)))
    (progn (princ "\n已取消导出，未覆盖项目数据。") nil)))

(defun c:DDFD_EXPORT_CABLE_ROUTE (/ prefix outdir)
  (setq prefix (getvar "DWGPREFIX"))
  (setq outdir (getstring T (strcat "\n导出目录 <" prefix "cable_export>: ")))
  (if (= outdir "")
    (setq outdir (strcat prefix "cable_export"))
  )
  (ddfd-ensure-folder outdir)
  (if (not (wcmatch outdir "*\\"))
    (setq outdir (strcat outdir "\\"))
  )
  (if (ddfd-do-export outdir)
    (progn
      (princ (strcat "\n已导出到：" outdir))
      (princ "\n请把四个CSV复制到项目 data 文件夹。")))
  (princ)
)

;;; ================= 引导向导部分 =================

;; 楼层号 -> 图层名，kind 取 "CABINET" / "ROUTE" / "SHAFT"
(defun ddfd-wiz-layer-name (kind f)
  (strcat "CABLE_" kind "_" (itoa f) "F")
)

(defun ddfd-wiz-make-layer (name color)
  (if (not (tblsearch "LAYER" name))
    (entmake (list (cons 0 "LAYER")
                   (cons 100 "AcDbSymbolTableRecord")
                   (cons 100 "AcDbLayerTableRecord")
                   (cons 2 name)
                   (cons 70 0)
                   (cons 62 color)
                   (cons 6 "Continuous")))
  )
)

;; 询问楼层数并记住（写入注册表环境项，下次默认沿用）
(defun ddfd-wiz-ask-floors (/ saved n)
  (setq saved (getenv "DDFD_CABLE_FLOORS"))
  (setq saved (if (and saved (> (atoi saved) 0)) (atoi saved) 2))
  (initget 6)
  (setq n (getint (strcat "\n这张图一共几层楼? <" (itoa saved) ">: ")))
  (if (not n) (setq n saved))
  (if (> n 20) (setq n 20))
  (setenv "DDFD_CABLE_FLOORS" (itoa n))
  n
)

;; 取已记住的楼层数；没有则现问
(defun ddfd-wiz-floors (/ saved)
  (setq saved (getenv "DDFD_CABLE_FLOORS"))
  (if (and saved (> (atoi saved) 0))
    (atoi saved)
    (ddfd-wiz-ask-floors)
  )
)

(defun ddfd-wiz-text-height (/ def h)
  (setq def (cond (*ddfd-wiz-texth*) (T (getvar "TEXTSIZE"))))
  (if (<= def 0.0) (setq def 300.0))
  (setq h (getreal (strcat "\n标注文字高度(按图纸单位) <" (rtos def 2 1) ">: ")))
  (if (or (not h) (<= h 0.0)) (setq h def))
  (setq *ddfd-wiz-texth* h)
  h
)

(defun ddfd-wiz-make-text (pt h str layer)
  (entmake (list (cons 0 "TEXT")
                 (cons 8 layer)
                 (cons 10 pt)
                 (cons 40 h)
                 (cons 1 str)
                 (cons 7 (getvar "TEXTSTYLE"))))
)

(defun ddfd-wiz-yesno (msg default / ans)
  (initget "Y N")
  (setq ans (getkword (strcat msg " [Y=是/N=否] <" default ">: ")))
  (if (not ans) (setq ans default))
  (= ans "Y")
)

;; —— 第1步：建图层 ——
(defun ddfd-wiz-step1 (/ n f nm)
  (princ "\n———— 第1步：创建图层 ————")
  (princ "\n按楼层自动创建 3 类图层：柜子出线点(黄)、走线路径(青)、竖井(红)。已存在的不会重建。")
  (setq n (ddfd-wiz-ask-floors))
  (setq f 1)
  (while (<= f n)
    (foreach pair (list (list "CABINET" 2) (list "ROUTE" 4) (list "SHAFT" 1) (list "ROOM" 6))
      (setq nm (ddfd-wiz-layer-name (car pair) f))
      (ddfd-wiz-make-layer nm (cadr pair))
      (princ (strcat "\n  " nm))
    )
    (setq f (1+ f))
  )
  (princ (strcat "\n图层就绪（" (itoa n) " 层楼 × 3 类）。"))
  (princ)
)



;; —— 房间范围定义：复制闭合直线多段线并保存房间名称 ——
(defun ddfd-wiz-step-rooms (/ n f layer pick en ent obj typ nm copy cnt oldlayer)
  (setq oldlayer (getvar "CLAYER"))
  (princ "\n———— 房间范围定义 ————")
  (princ "\n逐个选择已闭合的直线多段线，输入房间名称；原图形不会被修改。")
  (setq n (ddfd-wiz-floors))
  (setq f 1)
  (while (<= f n)
    (if (ddfd-wiz-yesno (strcat "\n定义 " (itoa f) "F 房间范围吗?") "Y")
      (progn
        (setq layer (ddfd-wiz-layer-name "ROOM" f))
        (ddfd-wiz-make-layer layer 6)
        (setvar "CLAYER" layer)
        (setq cnt 0)
        (setq pick (entsel (strcat "\n[" (itoa f) "F] 选择闭合多段线（回车结束）: ")))
        (while pick
          (setq en (car pick))
          (setq ent (entget en))
          (setq typ (cdr (assoc 0 ent)))
          (setq obj (vlax-ename->vla-object en))
          (if (not (member typ '("LWPOLYLINE" "POLYLINE")))
            (princ "\n对象不是 LWPOLYLINE/POLYLINE，已跳过。")
            (if (not (ddfd-room-closed-p en))
              (princ "\n多段线既未设置闭合标志，首尾点也未重合，已跳过。")
              (if (ddfd-room-has-arc en)
                (princ "\n房间范围不能包含圆弧段，请改用直线多段线。")
                (progn
                  (setq nm (getstring T "\n输入房间名称: "))
                  (if (= nm "")
                    (princ "\n房间名称不能为空，已跳过。")
                    (progn
                      (setq copy (vla-Copy obj))
                      (vla-put-Closed copy :vlax-true)
                      (vla-put-Layer copy layer)
                      (setq copy (vlax-vla-object->ename copy))
                      (ddfd-room-set-name copy nm)
                      (setq cnt (1+ cnt))
                      (princ (strcat "\n已定义房间：" nm))
                    )
                  )
                )
              )
            )
          )
          (setq pick (entsel (strcat "\n[" (itoa f) "F] 选择下一个闭合多段线（回车结束）: ")))
        )
        (princ (strcat "\n" (itoa f) "F 共定义 " (itoa cnt) " 个房间。"))
      )
    )
    (setq f (1+ f))
  )
  (if oldlayer (setvar "CLAYER" oldlayer))
  (princ)
)

;; —— 第2步：标柜子出线点 ——
(defun ddfd-wiz-step2 (/ n f layer h pt nm cnt)
  (princ "\n———— 第2步：标柜子出线点 ————")
  (princ "\n在每个柜子的“柜后出线点”位置点一下，再输入柜名。")
  (princ "\n柜名要和 Excel 清册的 起点/终点 写得一样（空格差异不用管，程序自动忽略）。")
  (setq n (ddfd-wiz-floors))
  (setq h (ddfd-wiz-text-height))
  (setq f 1)
  (while (<= f n)
    (if (ddfd-wiz-yesno (strcat "\n标注 " (itoa f) "F 层的柜子吗?") "Y")
      (progn
        (setq layer (ddfd-wiz-layer-name "CABINET" f))
        (ddfd-wiz-make-layer layer 2)
        (setvar "CLAYER" layer)
        (setq cnt 0)
        (setq pt (getpoint (strcat "\n[" (itoa f) "F] 指定柜子出线点(回车结束本层): ")))
        (while pt
          (setq nm (getstring T "柜子名称: "))
          (if (/= nm "")
            (progn
              (ddfd-wiz-make-text pt h nm layer)
              (setq cnt (1+ cnt))
              (princ (strcat "  已标注: " nm))
            )
            (princ "  柜名为空，本点作废。")
          )
          (setq pt (getpoint (strcat "\n[" (itoa f) "F] 指定下一个柜子出线点(回车结束本层): ")))
        )
        (princ (strcat "\n" (itoa f) "F 层共标注 " (itoa cnt) " 个柜子。"))
      )
    )
    (setq f (1+ f))
  )
  (princ "\n标错的直接用 ERASE 删掉那个文字再重标即可。")
  (princ)
)

;; —— 第3步：画走线路径 ——
(defun ddfd-wiz-step3 (/ n f layer more)
  (princ "\n———— 第3步：画走线路径 ————")
  (princ "\n沿电缆沟/桥架的中心线画多段线，电缆能走哪里线就画到哪里。")
  (princ "\n关键：两条线相接时端点必须真正搭上（端点对端点，或端点搭在另一条线上），只是看着交叉不算连通。")
  (princ "\n已自动打开 端点/交点/最近点 捕捉，照着捕捉标记点取点即可。")
  (setq n (ddfd-wiz-floors))
  (setvar "OSMODE" 545)
  (setq f 1)
  (while (<= f n)
    (if (ddfd-wiz-yesno (strcat "\n画 " (itoa f) "F 层的路径吗?") "Y")
      (progn
        (setq layer (ddfd-wiz-layer-name "ROUTE" f))
        (ddfd-wiz-make-layer layer 4)
        (setvar "CLAYER" layer)
        (setq more T)
        (while more
          (princ (strcat "\n[" (itoa f) "F] 开始画一条路径(多段线)，画完回车结束这条线..."))
          (command "_.PLINE")
          (while (> (getvar "CMDACTIVE") 0) (command PAUSE))
          (setq more (ddfd-wiz-yesno (strcat "\n[" (itoa f) "F] 继续画下一条路径?") "Y"))
        )
      )
    )
    (setq f (1+ f))
  )
  (princ)
)

;; —— 第4步：标竖井 ——
(defun ddfd-wiz-step4 (/ n h base f1 f2 f tmp layer pt more)
  (princ "\n———— 第4步：标竖井（有跨楼层电缆才需要）————")
  (setq n (ddfd-wiz-floors))
  (if (< n 2)
    (princ "\n只有一层楼，用不到竖井，本步跳过。")
    (if (ddfd-wiz-yesno "\n有跨楼层的电缆需要标竖井吗?" "Y")
      (progn
        (princ "\n同一个竖井要在它经过的每一层平面图上各点一个位置，程序会自动写 ZJn_楼层 配对文字。")
        (setq h (ddfd-wiz-text-height))
        (if (not *ddfd-wiz-shaftn*) (setq *ddfd-wiz-shaftn* 1))
        (setq more T)
        (while more
          (setq base (getstring (strcat "\n竖井编号 <ZJ" (itoa *ddfd-wiz-shaftn*) ">: ")))
          (if (= base "") (setq base (strcat "ZJ" (itoa *ddfd-wiz-shaftn*))))
          (initget 6)
          (setq f1 (getint "\n这个竖井从几层开始? <1>: "))
          (if (not f1) (setq f1 1))
          (initget 6)
          (setq f2 (getint (strcat "\n到几层结束? <" (itoa n) ">: ")))
          (if (not f2) (setq f2 n))
          (if (> f1 f2) (progn (setq tmp f1) (setq f1 f2) (setq f2 tmp)))
          (setq f f1)
          (while (<= f f2)
            (setq layer (ddfd-wiz-layer-name "SHAFT" f))
            (ddfd-wiz-make-layer layer 1)
            (setvar "CLAYER" layer)
            (setq pt (getpoint (strcat "\n在 " (itoa f) "F 平面图上点竖井 " base " 的位置: ")))
            (if pt
              (progn
                (ddfd-wiz-make-text pt h (strcat base "_" (itoa f) "F") layer)
                (princ (strcat "  已标注: " base "_" (itoa f) "F"))
              )
              (princ (strcat "  跳过 " (itoa f) "F（注意：竖井没配对会在问题清单里提示）。"))
            )
            (setq f (1+ f))
          )
          (setq *ddfd-wiz-shaftn* (1+ *ddfd-wiz-shaftn*))
          (setq more (ddfd-wiz-yesno "\n还要添加下一个竖井吗?" "N"))
        )
      )
    )
  )
  (princ)
)

;; —— 第5步：导出并运行统计 ——
(defun ddfd-wiz-step5 (/ prefix outdir tooldir input datadir fn src dst cmdfile)
  (princ "\n———— 第5步：导出数据并运行统计 ————")
  (setq tooldir (getenv "DDFD_CABLE_PROJDIR"))
  (if (or (not tooldir) (not (vl-file-directory-p tooldir)))
    (setq tooldir "")
  )
  (if (= tooldir "")
    (setq input (getstring T "\n项目文件夹(内含 data 子目录和 一键启动.cmd): "))
    (setq input (getstring T (strcat "\n项目文件夹(内含 data 子目录和 一键启动.cmd) <" tooldir ">: ")))
  )
  (if (/= input "") (setq tooldir input))
  (while (wcmatch tooldir "*\\")
    (setq tooldir (substr tooldir 1 (1- (strlen tooldir))))
  )
  (setq datadir (strcat tooldir "\\data"))
  (if (vl-file-directory-p datadir)
    (progn
      (setq outdir (strcat datadir "\\"))
      (if (ddfd-do-export outdir)
        (progn
      (setenv "DDFD_CABLE_PROJDIR" tooldir)
      (setq cmdfile (strcat tooldir "\\一键启动.cmd"))
      (if (findfile cmdfile)
        (if (ddfd-wiz-yesno "\n现在就运行统计吗?" "Y")
          (progn
            (startapp "cmd.exe" (strcat "/c start \"\" \"" cmdfile "\""))
            (princ "\n已启动统计窗口。跑完后查看 outputs\\自动统计_计算结果.xlsx 和 outputs\\路径可视化.html。")
            (princ "\n若“问题清单”里有柜子对不上，把 outputs\\柜子清单.csv 的建议填进 data\\柜名别名.csv 再重跑。")
          )
          (princ "\n之后双击项目文件夹里的 一键启动.cmd 即可。")
        )
        (princ (strcat "\n未找到 " cmdfile "，之后请手动运行统计。"))
      )
        ))
    )
    (progn
      (princ (strcat "\n未找到 data 子目录: " datadir))
      (princ "\n未导出任何数据，请先选择有效的项目文件夹。")
    )
  )
  (princ)
)

;; 保存并恢复用户环境（捕捉模式、当前图层）
(defun ddfd-wiz-restore ()
  (if *ddfd-wiz-oldos* (setvar "OSMODE" *ddfd-wiz-oldos*))
  (if (and *ddfd-wiz-oldlayer* (tblsearch "LAYER" *ddfd-wiz-oldlayer*))
    (setvar "CLAYER" *ddfd-wiz-oldlayer*)
  )
  (setq *ddfd-wiz-oldos* nil)
  (setq *ddfd-wiz-oldlayer* nil)
)

;; 按需连跑全部步骤，每步之间确认
(defun ddfd-wiz-all (/ go)
  (setq go T)
  (ddfd-wiz-step1)
  (if go (if (ddfd-wiz-yesno "\n第1步完成。继续定义房间?" "Y") (ddfd-wiz-step-rooms) (setq go nil)))
  (if go (if (ddfd-wiz-yesno "\n房间定义完成。进入第2步(标柜子)?" "Y") (ddfd-wiz-step2) (setq go nil)))
  (if go (if (ddfd-wiz-yesno "\n第2步完成。进入第3步(画路径)?" "Y") (ddfd-wiz-step3) (setq go nil)))
  (if go (if (ddfd-wiz-yesno "\n第3步完成。进入第4步(标竖井)?" "Y") (ddfd-wiz-step4) (setq go nil)))
  (if go (if (ddfd-wiz-yesno "\n第4步完成。进入第5步(导出并运行统计)?" "Y") (ddfd-wiz-step5) (setq go nil)))
  (if (not go)
    (princ "\n已停在当前步骤。之后输入 DDFD_CABLE_WIZARD 可从任意步骤继续。")
  )
)

(defun ddfd-wiz-run (stepno / *error*)
  (setq *ddfd-wiz-oldos* (getvar "OSMODE"))
  (setq *ddfd-wiz-oldlayer* (getvar "CLAYER"))
  (defun *error* (msg)
    (ddfd-wiz-restore)
    (princ (strcat "\n已中断: " msg))
    (princ "\n可重新输入 DDFD_CABLE_WIZARD 从任意步骤继续。")
    (princ)
  )
  (cond
    ((= stepno 0) (ddfd-wiz-all))
    ((= stepno 1) (ddfd-wiz-step1))
    ((= stepno 2) (ddfd-wiz-step2))
    ((= stepno 3) (ddfd-wiz-step3))
    ((= stepno 4) (ddfd-wiz-step4))
    ((= stepno 5) (ddfd-wiz-step5))
  )
  (ddfd-wiz-restore)
  (princ)
)

;; 带说明的步骤菜单，选步骤前先能看到每一步是干什么的
(defun ddfd-wiz-menu ()
  (princ "\n=============== 电缆长度统计 · 引导向导 ===============")
  (princ "\n  [A] 从头一步步来（新图推荐，依次执行 1→5，含房间定义）")
  (princ "\n  [1] 建图层   - 输入楼层数，自动建 柜子/路径/竖井/房间 四类图层")
  (princ "\n  [R] 定义房间 - 选择闭合多段线并输入房间名称")
  (princ "\n  [2] 标柜子   - 在柜后出线点点一下+输柜名，自动写文字")
  (princ "\n  [3] 画路径   - 沿电缆沟/桥架中心画多段线，自动开端点捕捉")
  (princ "\n  [4] 标竖井   - 跨楼层电缆用，自动写 ZJn_楼层 配对文字")
  (princ "\n  [5] 导出运行 - 导出CSV并复制进项目data，可直接运行统计")
  (princ "\n  [Q] 退出向导")
  (princ)
)

(defun c:DDFD_CABLE_WIZARD (/ choice first done)
  (setq first T)
  (setq done nil)
  (while (not done)
    (ddfd-wiz-menu)
    (initget "A R 1 2 3 4 5 Q")
    (setq choice (getkword (strcat "\n选择要做的步骤 [A/R/1/2/3/4/5/Q] <" (if first "A" "Q") ">: ")))
    (if (not choice) (setq choice (if first "A" "Q")))
    (setq first nil)
    (cond
      ((= choice "Q") (setq done T))
      ((= choice "A") (ddfd-wiz-run 0) (setq done T))
      ((= choice "R") (setq oldlayer (getvar "CLAYER")) (ddfd-wiz-step-rooms) (if oldlayer (setvar "CLAYER" oldlayer)))
      (T (ddfd-wiz-run (atoi choice)))
    )
  )
  (princ "\n向导已退出，单步做完会回到菜单；随时输入 DDFD_CABLE_WIZARD 再进。")
  (princ)
)

(defun c:DDFD_CABLE_LAYERS () (ddfd-wiz-run 1))
(defun c:DDFD_CABLE_ROOMS (/ oldlayer *error*)
  (defun *error* (msg)
    (if oldlayer (setvar "CLAYER" oldlayer))
    (princ)
  )
  (setq oldlayer (getvar "CLAYER"))
  (ddfd-wiz-step-rooms)
  (if oldlayer (setvar "CLAYER" oldlayer))
  (princ)
)

(defun c:DDFD_CLEAN_ROOM_LAYERS (/ ss i en ed lay xd cnt)
  (princ "\n正在检查房间图层上的多余未命名图元...")
  (setq ss (ssget "_X" '((0 . "*POLYLINE") (8 . "CABLE_ROOM*"))))
  (setq cnt 0)
  (if ss
    (progn
      (setq i 0)
      (while (< i (sslength ss))
        (setq en (ssname ss i))
        (setq ed (entget en (list "DDFD_CABLE_ROOM")))
        (setq xd (assoc -3 ed))
        (if (not xd)
          (progn
            (setq ed (subst (cons 8 "0") (assoc 8 ed) ed))
            (entmod ed)
            (setq cnt (1+ cnt))
          )
        )
        (setq i (1+ i))
      )
    )
  )
  (if (> cnt 0)
    (princ (strcat "\n已将 " (itoa cnt) " 个未命名的多余图元（如图框、表格）移回 0 图层。"))
    (princ "\n房间图层很干净，未发现多余未命名图元。")
  )
  (princ)
)
(defun c:DDFD_CABLE_CABINETS () (ddfd-wiz-run 2))
(defun c:DDFD_CABLE_ROUTES () (ddfd-wiz-run 3))
(defun c:DDFD_CABLE_SHAFTS () (ddfd-wiz-run 4))
(defun c:DDFD_CABLE_EXPORT_RUN () (ddfd-wiz-run 5))

(princ "\n已加载电缆统计引导向导。输入 DDFD_CABLE_WIZARD 开始一步步操作。")
(princ "\n分步命令: DDFD_CABLE_LAYERS 建图层 | DDFD_CABLE_CABINETS 标柜子 | DDFD_CABLE_ROUTES 画路径 | DDFD_CABLE_SHAFTS 标竖井 | DDFD_CABLE_EXPORT_RUN 导出并运行 | DDFD_EXPORT_CABLE_ROUTE 仅导出")
(princ)
