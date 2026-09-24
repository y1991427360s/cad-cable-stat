;;; 多氟多电缆长度自动统计 - CAD路径导出
;;; 命令：DDFD_EXPORT_CABLE_ROUTE（导出4个CSV）| DDFD_ROOM_CHECK 检查房间 | DDFD_CLEAN_ROOM_LAYERS 清理房间图层
;;; 支持 AutoCAD / ZWCAD 常见的 LINE、LWPOLYLINE、POLYLINE、TEXT、MTEXT、INSERT、POINT。
;;; 建议图层：
;;;   CABLE_CABINET_1F / CABLE_CABINET_2F   柜子出线点或带柜名文字/块
;;;   CABLE_ROUTE_1F   / CABLE_ROUTE_2F     固定电缆沟/桥架中心线
;;;   CABLE_SHAFT_1F   / CABLE_SHAFT_2F     电缆竖井点或带竖井名文字/块
;;;   CABLE_ROOM_1F    / CABLE_ROOM_2F      房间边界（需用向导的“定义房间”写入名称）

(vl-load-com)

;; 改动本文件时同步更新版本号，加载时会打印，便于确认 CAD 里用的是哪一版
(setq *ddfd-cable-version* "2026-09-23")

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

;; 房间只认“定义房间”写过名称(XData)的多段线；图层上未命名的图形（图框、表格等）一律不当房间
(defun ddfd-room-entity-p (ename / ent)
  (setq ent (entget ename (list "DDFD_CABLE_ROOM")))
  (and (member (cdr (assoc 0 ent)) '("LWPOLYLINE" "POLYLINE"))
       (assoc -3 ent)))

;; 可导出的房间：模型空间、已命名、图层名能看出楼层、闭合、无圆弧、面积非零
(defun ddfd-room-exportable-p (ent / ed pts area prev pt)
  (and ent (setq ed (entget ent))
    (/= (cdr (assoc 67 ed)) 1)
    (ddfd-room-entity-p ent)
    (/= (ddfd-layer-floor (cdr (assoc 8 ed))) "")
    (ddfd-room-closed-p ent)
    (not (ddfd-room-has-arc ent))
    (progn
      (setq pts (ddfd-room-vertices ent) area 0.0 prev (last pts))
      (foreach pt pts
        (setq area (+ area (- (* (car prev) (cadr pt)) (* (car pt) (cadr prev)))) prev pt))
      (> (abs area) 1e-9))))

;; 扫描模型空间，返回 (房间图元表 CABLE_ROOM图层上未命名多段线句柄表 已命名但无效的句柄表)
(defun ddfd-room-collect (/ ent ed layer rooms unnamed bad)
  (setq ent (entnext))
  (while ent
    (setq ed (entget ent (list "DDFD_CABLE_ROOM"))
          layer (cdr (assoc 8 ed)))
    (if (and (member (cdr (assoc 0 ed)) '("LWPOLYLINE" "POLYLINE"))
             (/= (cdr (assoc 67 ed)) 1))
      (cond
        ((ddfd-room-exportable-p ent) (setq rooms (cons ent rooms)))
        ((assoc -3 ed) (setq bad (cons (cdr (assoc 5 ed)) bad)))
        ((and layer (wcmatch (strcase layer) "CABLE_ROOM*"))
         (setq unnamed (cons (cdr (assoc 5 ed)) unnamed)))))
    (setq ent (entnext ent)))
  (list (reverse rooms) (reverse unnamed) (reverse bad)))

;; 在命令行按楼层列出要导出的房间，并提示同层重名、被跳过的多段线
(defun ddfd-room-report (info / key seen dup en)
  (princ (strcat "\n房间: " (itoa (length (car info))) " 个"))
  (foreach en (car info)
    (setq key (strcat (ddfd-layer-floor (cdr (assoc 8 (entget en)))) " " (ddfd-room-name en)))
    (princ (strcat "\n  " key))
    (if (member key seen) (setq dup (cons key dup)) (setq seen (cons key seen))))
  (foreach key dup
    (princ (strcat "\n注意：同层房间重名 " key "，统计时会忽略这些范围，请改名或删掉多余的一份。")))
  (if (cadr info)
    (princ (strcat "\n注意：CABLE_ROOM 图层上有 " (itoa (length (cadr info)))
                   " 个未命名多段线（不是用“定义房间”建的），不会导出。输入 DDFD_ROOM_CHECK 可定位查看。")))
  (if (caddr info)
    (princ (strcat "\n注意：有 " (itoa (length (caddr info)))
                   " 个已命名房间不闭合、含圆弧、面积为零或图层名看不出楼层，不会导出。输入 DDFD_ROOM_CHECK 可定位查看。")))
  (princ))

;; 把选择集缩放到可见范围（用 DXF 顶点算包围盒，不依赖 COM；临时关捕捉以免点被吸走）
(defun ddfd-room-zoom (ss / i pt x0 y0 x1 y1 d os)
  (setq i 0)
  (repeat (sslength ss)
    (foreach pt (ddfd-room-vertices (ssname ss i))
      (setq x0 (if x0 (min x0 (car pt)) (car pt))
            y0 (if y0 (min y0 (cadr pt)) (cadr pt))
            x1 (if x1 (max x1 (car pt)) (car pt))
            y1 (if y1 (max y1 (cadr pt)) (cadr pt))))
    (setq i (1+ i)))
  (if x0
    (progn
      (setq d (* 0.05 (max (- x1 x0) (- y1 y0) 1.0))
            os (getvar "OSMODE"))
      (setvar "OSMODE" 0)
      (command "_.ZOOM" "_W"
               (trans (list (- x0 d) (- y0 d) 0.0) 0 1)
               (trans (list (+ x1 d) (+ y1 d) 0.0) 0 1))
      (setvar "OSMODE" os))))

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


(defun ddfd-export-point-row (fh pattern en / ent layer pt nm)
  (setq ent (entget en))
  (setq layer (cdr (assoc 8 ent)))
  (setq pt (ddfd-entity-point en))
  (setq nm (ddfd-entity-name en))
  (if (wcmatch pattern "CABLE_CABINET*")
    (setq nm (ddfd-remove-spaces nm))
  )
  (if (and pt (>= (length pt) 2))
    (if (wcmatch pattern "CABLE_SHAFT*")
      (ddfd-write-csv-row fh
        (list nm (ddfd-layer-floor layer) (rtos (car pt) 2 6) (rtos (cadr pt) 2 6) "" layer (cdr (assoc 0 ent)) (cdr (assoc 5 ent))))
      (ddfd-write-csv-row fh
        (list nm (ddfd-layer-floor layer) "" (rtos (car pt) 2 6) (rtos (cadr pt) 2 6) layer (cdr (assoc 0 ent)) (cdr (assoc 5 ent))))
    )
  )
)

;; 逐个图元捕获错误：先保证文件被关闭（否则 .pending 被锁、清理不掉），
;; 再报出出错图元的句柄；有任何图元失败就返回 nil，整次导出放弃、保留原数据
(defun ddfd-export-fail-message (en result)
  (princ (strcat "\n无法读取图元 " (cdr (assoc 5 (entget en))) " ("
                 (cdr (assoc 8 (entget en))) "): " (vl-catch-all-error-message result))))

(defun ddfd-export-point-layer (pattern filename header / ss fh i en result ok)
  (setq fh (open filename "w") ok T)
  (ddfd-write-csv-row fh header)
  (setq ss (ssget "_X" (list (cons 8 pattern))))
  (if ss
    (progn
      (setq i 0)
      (while (< i (sslength ss))
        (setq en (ssname ss i))
        (setq result (vl-catch-all-apply 'ddfd-export-point-row (list fh pattern en)))
        (if (vl-catch-all-error-p result) (progn (ddfd-export-fail-message en result) (setq ok nil)))
        (setq i (1+ i))
      )
    )
  )
  (close fh)
  ok
)

(defun ddfd-export-route-rows (fh en / ent obj layer handle typ endp seg p1 p2 d routeid)
  (setq ent (entget en))
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
     (setq obj (vlax-ename->vla-object en))
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
)

(defun ddfd-export-routes (filename / ss fh i en result ok)
  (setq fh (open filename "w") ok T)
  (ddfd-write-csv-row fh (list "路径编号" "楼层" "起点X" "起点Y" "终点X" "终点Y" "长度_CAD单位" "图层" "句柄" "段号"))
  (setq ss (ssget "_X" (list (cons 8 "CABLE_ROUTE*"))))
  (if ss
    (progn
      (setq i 0)
      (while (< i (sslength ss))
        (setq en (ssname ss i))
        (setq result (vl-catch-all-apply 'ddfd-export-route-rows (list fh en)))
        (if (vl-catch-all-error-p result) (progn (ddfd-export-fail-message en result) (setq ok nil)))
        (setq i (1+ i))
      )
    )
  )
  (close fh)
  ok
)

;; 把 .pending 换成正式文件（outdir 末尾需带反斜杠）；任一步失败则回滚
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
  (and (ddfd-export-rooms (strcat outdir "房间范围.csv.pending") rooms)
       (ddfd-export-point-layer "CABLE_CABINET*" (strcat outdir "柜子坐标.csv.pending")
         (list "柜子名称" "楼层" "房间" "X" "Y" "图层" "对象类型" "句柄"))
       (ddfd-export-routes (strcat outdir "路径线段.csv.pending"))
       (ddfd-export-point-layer "CABLE_SHAFT*" (strcat outdir "竖井.csv.pending")
         (list "竖井编号" "楼层" "X" "Y" "高度" "图层" "对象类型" "句柄"))))

;; 房间可以没有（导出空的 房间范围.csv）；房间列表在导出前打印，便于当场核对
(defun ddfd-do-export (outdir / info rooms result names fn)
  (setq info (ddfd-room-collect))
  (ddfd-room-report info)
  (setq rooms (if (car info) (car info) 'NO-ROOMS))
  (if rooms
    (progn
      (setq result (vl-catch-all-apply 'ddfd-export-stage (list outdir rooms)))
      (if (and (not (vl-catch-all-error-p result)) result)
        (if (ddfd-export-commit outdir)
          (progn
            ;; 旧版本留下的房间范围记录已不再使用
            (if (findfile (strcat outdir "房间导出范围.txt"))
              (vl-file-delete (strcat outdir "房间导出范围.txt")))
            (princ (strcat "\n已导出四个CSV；房间数量: "
              (itoa (if (= rooms 'NO-ROOMS) 0 (length rooms)))))
            T)
          (progn
            (princ "\n提交失败，请检查文件占用或 .previous 备份。")
            nil))
        (progn
          (if (vl-catch-all-error-p result)
            (princ (strcat "\n导出阶段发生错误: " (vl-catch-all-error-message result)))
            (princ "\n有图元读取失败（见上面的句柄），本次没有覆盖项目数据。"))
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

;; 定位房间问题：选中并缩放到 CABLE_ROOM 图层上未命名、或已命名但无效的多段线（只选中，不修改图形）
(defun c:DDFD_ROOM_CHECK (/ info ss h en)
  (setq info (ddfd-room-collect))
  (ddfd-room-report info)
  (setq ss (ssadd))
  (foreach h (append (cadr info) (caddr info))
    (if (setq en (handent h)) (ssadd en ss)))
  (if (> (sslength ss) 0)
    (progn
      (princ "\n问题句柄:")
      (foreach h (append (cadr info) (caddr info)) (princ (strcat " " h)))
      (ddfd-room-zoom ss)
      (sssetfirst nil ss)
      (princ "\n已选中并缩放到这些多段线。图框/表格之类请在特性里改回原图层或删除；也可输入 DDFD_CLEAN_ROOM_LAYERS 把未命名的统一移到 0 层。"))
    (princ "\n房间图层没有问题图形。"))
  (princ))

;; 把 CABLE_ROOM 图层上未命名的多段线移到 0 层：先列出并确认，U 一步即可撤销
(defun c:DDFD_CLEAN_ROOM_LAYERS (/ info h en ed ans cnt)
  (setq info (ddfd-room-collect))
  (if (null (cadr info))
    (princ "\nCABLE_ROOM 图层上没有未命名的多段线。")
    (progn
      (princ (strcat "\n将把 " (itoa (length (cadr info))) " 个未命名多段线移到 0 层，句柄:"))
      (foreach h (cadr info) (princ (strcat " " h)))
      (initget "Y N")
      (setq ans (getkword "\n确认移动吗? [Y=是/N=否] <N>: "))
      (if (= ans "Y")
        (progn
          (command "_.UNDO" "_BE")
          (setq cnt 0)
          (foreach h (cadr info)
            (if (and (setq en (handent h)) (setq ed (entget en))
                     (entmod (subst (cons 8 "0") (assoc 8 ed) ed)))
              (setq cnt (1+ cnt))))
          (command "_.UNDO" "_E")
          (princ (strcat "\n已移动 " (itoa cnt) " 个到 0 层；移错了输入 U 撤销。")))
        (princ "\n已取消，未做任何修改。"))))
  (princ))

(princ (strcat "\n已加载：DDFD_EXPORT_CABLE_ROUTE / DDFD_ROOM_CHECK / DDFD_CLEAN_ROOM_LAYERS（版本 " *ddfd-cable-version* "）"))
(princ)
