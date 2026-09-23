;;; 多氟多电缆长度自动统计 - CAD路径导出
;;; 命令：DDFD_EXPORT_CABLE_ROUTE
;;; 支持 AutoCAD / ZWCAD 常见的 LINE、LWPOLYLINE、POLYLINE、TEXT、MTEXT、INSERT、POINT。
;;; 建议图层：
;;;   CABLE_CABINET_1F / CABLE_CABINET_2F   柜子出线点或带柜名文字/块
;;;   CABLE_ROUTE_1F   / CABLE_ROUTE_2F     固定电缆沟/桥架中心线
;;;   CABLE_SHAFT_1F   / CABLE_SHAFT_2F     电缆竖井点或带竖井名文字/块

(vl-load-com)

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

(defun ddfd-room-vertex-points (ename / ent typ cur pts item)
  (setq ent (entget ename))
  (setq typ (cdr (assoc 0 ent)))
  (setq pts nil)
  (if (= typ "LWPOLYLINE")
    (foreach item ent
      (if (= (car item) 10) (setq pts (append pts (list (cdr item)))))
    )
    (if (= typ "POLYLINE")
      (progn
        (setq cur (entnext ename))
        (while (and cur (= (cdr (assoc 0 (entget cur))) "VERTEX"))
          (setq item (cdr (assoc 10 (entget cur))))
          (if item (setq pts (append pts (list item))))
          (setq cur (entnext cur))
        )
      )
    )
  )
  pts
)

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

(princ "\n已加载：DDFD_EXPORT_CABLE_ROUTE")
(princ)
