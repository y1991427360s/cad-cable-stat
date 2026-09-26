;;; 多氟多电缆长度自动统计 - CAD 分步引导向导（自包含，含导出命令）
;;; 只需 APPLOAD 本文件，然后输入 DDFD_CABLE_WIZARD，按提示一步步做即可。
;;;
;;; 包含以下命令:
;;;   DDFD_CABLE_WIZARD        引导向导：建图层→标柜子→画路径→标竖井→导出并运行统计
;;;   DDFD_CABLE_LAYERS        第1步：按楼层数自动创建 CABLE_* 图层
;;;   DDFD_CABLE_ROOMS         定义房间：给闭合多段线写房间名称（放到 CABLE_ROOM_nF）
;;;   DDFD_CABLE_CABINETS      第2步：逐个点柜子出线点并写柜名文字（柜名已标过会提醒）
;;;   DDFD_CABLE_ROUTES        第3步：沿电缆沟/桥架画走线路径（自动开端点捕捉）
;;;   DDFD_CABLE_SHAFTS        第4步：标跨楼层竖井（自动写 ZJn_楼层 配对文字）
;;;   DDFD_CABLE_EXPORT_RUN    第5步：导出4个CSV到项目文件夹 data\，可直接运行统计
;;;   DDFD_EXPORT_CABLE_ROUTE  仅导出4个CSV（与 cad_export_cable_route.lsp 相同）
;;;   DDFD_ROOM_CHECK          检查房间：列出将导出的房间，定位房间图层上未命名/无效的多段线
;;;   DDFD_CLEAN_ROOM_LAYERS   把房间图层上未命名的多段线移到 0 层（先确认，可 U 撤销）
;;;
;;; 图层约定（n 为楼层号 1~99；地下室写 B1F、B2F，向导只建地上各层，地下室图层请手工新建同名图层）：
;;;   CABLE_CABINET_nF  柜子出线点文字     CABLE_ROUTE_nF  电缆沟/桥架中心线
;;;   CABLE_SHAFT_nF    竖井文字（同一竖井各层命名 ZJ1_1F / ZJ1_2F 配对）
;;;   CABLE_ROOM_nF     房间边界（只有“定义房间”写过名称的多段线才算房间）
;;;
;;; 导出规则：只导出模型空间（布局里的不算），全部读 DXF 数据，不依赖 COM。
;;;   柜子/竖井：TEXT、MTEXT（自动去格式）、块（取第一个非空属性值，没有属性就用块名）；
;;;   路径：LINE、LWPOLYLINE、POLYLINE（含圆弧段，长度按弧长）、ARC；SPLINE、椭圆等不导出，导出后会列出句柄。

(vl-load-com)

;; 改动本文件时同步更新版本号，加载时会打印，便于确认 CAD 里用的是哪一版
(setq *ddfd-cable-version* "2026-09-26")

;;; ================= 通用与导出部分 =================
;; 本段原样复制到 cad_export_cable_route.lsp（python tools\lisp_test\sync_export_lsp.py），两份必须逐字一致。
;; 导出只读 DXF 数据、不调用 COM（accoreconsole 和部分 ZWCAD 没有 COM）；只有动态块取原块名时试一次 COM，出错不影响导出。

;; —— 字符串工具 ——

;; 把 str 里的 old 全部换成 new（vl-string-subst 只换第一处）；从替换后的位置接着找，new 里含 old 也不会死循环
(defun ddfd-string-replace-all (new old str / pos)
  (setq str (if str (vl-princ-to-string str) "") pos 0)
  (if (/= old "")
    (while (setq pos (vl-string-search old str pos))
      (setq str (strcat (substr str 1 pos) new (substr str (+ pos (strlen old) 1)))
            pos (+ pos (strlen new)))))
  str)

;; CSV 单元格：整格加双引号，内容里的每个双引号都写成两个
(defun ddfd-csv-escape (s)
  (strcat "\"" (ddfd-string-replace-all "\"\"" "\"" s) "\""))

;; 去掉首尾的 ASCII 空白
(defun ddfd-string-trim (s)
  (vl-string-trim " \t\r\n" (if s (vl-princ-to-string s) "")))

;; 手输/粘贴的文件夹路径：去掉首尾空白和双引号（资源管理器“复制文件地址”会带引号）
(defun ddfd-clean-path (s)
  (vl-string-trim " \t\r\n\"" (if s s "")))

;; MBCS 版 AutoLISP（AutoCAD 2020 及更早、LISPSYS=0、部分 ZWCAD）的字符串是 GBK 字节：一个汉字两个字节，
;; 尾字节可能落在 ASCII 范围（\ { } 和字母都有可能）；Unicode 版一个汉字就是一个字符。用 "中" 的长度区分两者
(defun ddfd-mbcs-p () (/= (strlen "中") 1))

;; 把字符串拆成单个字符的表；MBCS 下 GBK 双字节字符整体算一个，逐字处理时不会把汉字尾字节当成 ASCII
(defun ddfd-string-chars (s / codes mbcs c res)
  (setq codes (vl-string->list (if s (vl-princ-to-string s) "")) mbcs (ddfd-mbcs-p))
  (while codes
    (setq c (car codes) codes (cdr codes))
    (if (and mbcs (<= 129 c 254) codes)
      (setq res (cons (vl-list->string (list c (car codes))) res) codes (cdr codes))
      (setq res (cons (vl-list->string (list c)) res))))
  (reverse res))

;; 单个字符的 ASCII 码；汉字等非 ASCII 字符返回 0
(defun ddfd-ascii-code (ch / c)
  (if (and ch (= (strlen ch) 1) (< (setq c (ascii ch)) 128)) c 0))

(defun ddfd-digit-p (ch) (<= 48 (ddfd-ascii-code ch) 57))

(defun ddfd-alnum-p (ch / c)
  (setq c (ddfd-ascii-code ch))
  (or (<= 48 c 57) (<= 65 c 90) (<= 97 c 122)))

;; 单行文字（TEXT、属性）的 %% 控制码：%%u %%o %%k（下划线/上划线/删除线开关）去掉，%%% 为 %，%%nnn 为对应 ASCII 字符；
;; %%c %%d %%p 换成图上显示的 Φ ° ±：清册里直径一般写 Φ（CAD 显示的直径符号不在 GBK 里，而 CSV 按 GBK 写出），
;; 这样 CSV 和柜子清单里看到的就是图上的字，清册照着写就能对上
(defun ddfd-text-plain (s / chars out c n d2 d3 code)
  (setq chars (ddfd-string-chars s))
  (while chars
    (setq c (car chars))
    (if (and (= c "%") (= (cadr chars) "%") (setq n (caddr chars)))
      (progn
        (setq d2 (cadddr chars) d3 (car (cddddr chars)))
        (cond
          ((member n '("c" "C")) (setq out (cons "Φ" out) chars (cdddr chars)))
          ((member n '("d" "D")) (setq out (cons "°" out) chars (cdddr chars)))
          ((member n '("p" "P")) (setq out (cons "±" out) chars (cdddr chars)))
          ((member n '("u" "U" "o" "O" "k" "K")) (setq chars (cdddr chars)))
          ((= n "%") (setq out (cons "%" out) chars (cdddr chars)))
          ((and (ddfd-digit-p n) (ddfd-digit-p d2) (ddfd-digit-p d3)
                (<= 32 (setq code (atoi (strcat n d2 d3))) 126))
           (setq out (cons (chr code) out) chars (cdr (cddddr chars))))
          (T (setq out (cons c out) chars (cdr chars)))))
      (setq out (cons c out) chars (cdr chars))))
  (apply 'strcat (reverse out)))

;; 多行文字去格式：\P \N \~ 换成空格；去掉编组花括号；\f \F \H \W \Q \T \A \C \c \p 连同参数（到分号为止）去掉；
;; \L \l \O \o \K \k 开关去掉；堆叠 \Sa^b; \Sa/b; \Sa#b; 写成 a/b；\\ \{ \} 还原成字符；认不出的 \x 原样保留
;; （\U+XXXX：Unicode 版 AutoLISP 的 vl-string->list 会直接换成对应字符；MBCS 版里只有 GBK 以外的字才这样写，保留原样）。
;; 最后按单行文字处理 %% 控制码
(defun ddfd-mtext-plain (s / chars out c n)
  (setq chars (ddfd-string-chars s))
  (while chars
    (setq c (car chars) chars (cdr chars))
    (cond
      ((member c '("{" "}")))
      ((and (= c "\\") chars)
       (setq n (car chars))
       (cond
         ((member n '("P" "N" "~")) (setq out (cons " " out) chars (cdr chars)))
         ((member n '("\\" "{" "}")) (setq out (cons n out) chars (cdr chars)))
         ((member n '("L" "l" "O" "o" "K" "k")) (setq chars (cdr chars)))
         ((member n '("f" "F" "H" "W" "Q" "T" "A" "C" "c" "p"))
          (while (and chars (/= (car chars) ";")) (setq chars (cdr chars)))
          (setq chars (cdr chars)))
         ((= n "S")
          (setq chars (cdr chars))
          (while (and chars (/= (car chars) ";"))
            (setq out (cons (if (member (car chars) '("^" "#")) "/" (car chars)) out)
                  chars (cdr chars)))
          (setq chars (cdr chars)))
         (T (setq out (cons c out)))))
      (T (setq out (cons c out)))))
  (ddfd-text-plain (apply 'strcat (reverse out))))

;; MTEXT 内容超过 250 字时，前面的部分分段放在 3 组码里，按顺序拼上最后的 1 组码
(defun ddfd-mtext-raw (ent / s item)
  (setq s "")
  (foreach item ent (if (= (car item) 3) (setq s (strcat s (cdr item)))))
  (strcat s (cond ((cdr (assoc 1 ent))) (T ""))))

;; 柜名去掉 ASCII 空白（空格、制表、回车换行）。全角空格、不间断空格交给 Python（它会去掉全部 Unicode 空白）：
;; MBCS 版 AutoLISP 里字符串是 GBK 字节，按 160/12288 去字节会把生僻字的尾字节删坏
(defun ddfd-remove-spaces (s)
  (vl-list->string
    (vl-remove-if '(lambda (c) (member c '(9 10 13 32)))
                  (vl-string->list (if s (vl-princ-to-string s) "")))))

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

;; —— 楼层 ——

;; 从图层名解析楼层（不分大小写），规则与 Python 的 infer_floor_from_layer 相同，按顺序：
;;   1. B<n>F 且 B 前面不是字母或数字：地下室，CABLE_ROUTE_B1F -> B1F；
;;   2. 中文 一~九十九 加 层/楼，前面带“地下”或“负”算地下室：电缆沟二层 -> 2F，地下一层 -> B1F；
;;   3. <n>F（取完整的一串数字）：CABLE_ROUTE_12F -> 12F，CABLE_ROUTE_01F -> 1F。
;; 楼层号只认 1~99，都认不出返回 ""
(defun ddfd-layer-floor (layer / chars len i j n res)
  (setq chars (ddfd-string-chars layer) len (length chars) res "" i 0)
  (while (and (= res "") (< i len))
    (if (and (member (nth i chars) '("B" "b"))
             (or (= i 0) (not (ddfd-alnum-p (nth (1- i) chars)))))
      (progn
        (setq j (1+ i))
        (while (ddfd-digit-p (nth j chars)) (setq j (1+ j)))
        (if (and (> j (1+ i)) (member (nth j chars) '("F" "f"))
                 (<= 1 (setq n (ddfd-chars-number chars (1+ i) j)) 99))
          (setq res (strcat "B" (itoa n) "F")))))
    (setq i (1+ i)))
  (if (= res "") (setq res (ddfd-layer-floor-cn chars)))
  (setq i 0)
  (while (and (= res "") (< i len))
    (if (and (ddfd-digit-p (nth i chars))
             (or (= i 0) (not (ddfd-digit-p (nth (1- i) chars)))))
      (progn
        (setq j i)
        (while (ddfd-digit-p (nth j chars)) (setq j (1+ j)))
        (if (and (member (nth j chars) '("F" "f"))
                 (<= 1 (setq n (ddfd-chars-number chars i j)) 99))
          (setq res (strcat (itoa n) "F")))))
    (setq i (1+ i)))
  res)

;; 数字字符 chars[from..to) 的值；超过 99 就不再往下算（只需判断是否在 1~99 内）
(defun ddfd-chars-number (chars from to / n)
  (setq n 0)
  (while (and (< from to) (<= n 99))
    (setq n (+ (* n 10) (- (ascii (nth from chars)) 48)) from (1+ from)))
  n)

;; 中文数字 零~九、两、十 的值，其他字符返回 nil
(defun ddfd-cn-digit (ch)
  (cdr (assoc ch '(("零" . 0) ("〇" . 0) ("一" . 1) ("二" . 2) ("两" . 2) ("三" . 3) ("四" . 4)
                   ("五" . 5) ("六" . 6) ("七" . 7) ("八" . 8) ("九" . 9) ("十" . 10)))))

;; 中文数字串（单字表）的值：一~九、十、十二、二十、二十三 这类写法，其他返回 nil（同 Python 的 chinese_number）
(defun ddfd-cn-number (run / ones)
  (cond
    ((not (member "十" run)) (if (null (cdr run)) (ddfd-cn-digit (car run))))
    ((= (car run) "十")
     (setq ones (cdr run))
     (cond ((null ones) 10)
           ((and (null (cdr ones)) (/= (car ones) "十")) (+ 10 (ddfd-cn-digit (car ones))))))
    ((= (cadr run) "十")
     (setq ones (cddr run))
     (cond ((null ones) (* 10 (ddfd-cn-digit (car run))))
           ((and (null (cdr ones)) (/= (car ones) "十"))
            (+ (* 10 (ddfd-cn-digit (car run))) (ddfd-cn-digit (car ones))))))))

;; 中文楼层：只看第一段紧挨着 层/楼 的中文数字（Python 也只取第一处），前面是“地下”或“负”就是地下室
(defun ddfd-layer-floor-cn (chars / len i j k n run res)
  (setq len (length chars) i 0 res "")
  (while (< i len)
    (if (and (ddfd-cn-digit (nth i chars))
             (or (= i 0) (not (ddfd-cn-digit (nth (1- i) chars)))))
      (progn
        (setq j i)
        (while (and (< j len) (ddfd-cn-digit (nth j chars))) (setq j (1+ j)))
        (if (member (nth j chars) '("层" "楼"))
          (progn
            (setq run nil k (1- j))
            (while (>= k i) (setq run (cons (nth k chars) run) k (1- k)))
            (if (and (setq n (ddfd-cn-number run)) (<= 1 n 99))
              (setq res (strcat (if (and (> i 0)
                                         (or (= (nth (1- i) chars) "负")
                                             (and (> i 1) (= (nth (1- i) chars) "下") (= (nth (- i 2) chars) "地"))))
                                  "B" "")
                                (itoa n) "F")))
            (setq i len)))))
    (setq i (1+ i)))
  res)

;; —— 柜子/竖井的位置和名称 ——

;; 定位点：TEXT/INSERT 的 10 组码是 OCS 坐标，要转到 WCS；MTEXT、POINT 的 10 组码本身就是 WCS。
;; 与旧版 COM 的 InsertionPoint 一致，TEXT 取 10 组码（文字基点），不取 11 组码（对齐点）
(defun ddfd-entity-point (ename / ent pt)
  (setq ent (entget ename) pt (cdr (assoc 10 ent)))
  (if (and pt (member (cdr (assoc 0 ent)) '("TEXT" "INSERT")))
    (trans pt ename 0)
    pt))

;; 块参照的第一个非空属性值：66 组码为 1 时，块参照后面紧跟着 ATTRIB 图元直到 SEQEND；多行属性按多行文字去格式
(defun ddfd-block-first-attr (ename / cur ed res)
  (setq res "")
  (if (= 1 (cdr (assoc 66 (entget ename))))
    (progn
      (setq cur (entnext ename))
      (while (and cur (= res "") (= "ATTRIB" (cdr (assoc 0 (setq ed (entget cur))))))
        (setq res (ddfd-string-trim
                    (if (assoc 101 ed)
                      (ddfd-mtext-plain (cdr (assoc 1 ed)))
                      (ddfd-text-plain (cdr (assoc 1 ed)))))
              cur (entnext cur)))))
  res)

;; 动态块参照的块名是匿名的 *U…：先按 DXF 找原块名（匿名块记录的 AcDbBlockRepBTag 扩展数据指向原块记录），
;; 找不到再试 COM 的 EffectiveName（放在 vl-catch-all-apply 里，控制台等没有 COM 的环境出错就用 *U… 原名）
(defun ddfd-block-effective-name (ename name / rec rep res)
  (if (wcmatch name "`**")
    (progn
      (if (and (setq rec (tblobjname "BLOCK" name))
               (setq rec (cdr (assoc 330 (entget rec))))
               (setq rep (cdadr (assoc -3 (entget rec '("AcDbBlockRepBTag")))))
               (setq rep (cdr (assoc 1005 rep)))
               (setq rep (handent rep)))
        (setq res (cdr (assoc 2 (entget rep)))))
      (if (not res)
        (progn
          (setq rep (vl-catch-all-apply
                      '(lambda () (vla-get-EffectiveName (vlax-ename->vla-object ename)))
                      nil))
          (if (and (= (type rep) 'STR) (/= rep "")) (setq res rep))))
      (if res res name))
    name))

(defun ddfd-insert-name (ename / nm)
  (setq nm (ddfd-block-first-attr ename))
  (if (= nm "")
    (ddfd-block-effective-name ename (cdr (assoc 2 (entget ename))))
    nm))

;; 柜名/竖井编号：TEXT 取 1 组码，MTEXT 取 3 组码各段加 1 组码并去格式，块参照取第一个非空属性值、没有就用块名；
;; 其他图元（点、线等）没有名称，返回 ""
(defun ddfd-entity-name (ename / ent typ)
  (setq ent (entget ename) typ (cdr (assoc 0 ent)))
  (cond
    ((= typ "TEXT") (ddfd-text-plain (cdr (assoc 1 ent))))
    ((= typ "MTEXT") (ddfd-mtext-plain (ddfd-mtext-raw ent)))
    ((= typ "INSERT") (ddfd-insert-name ename))
    (T "")))

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
    (progn (princ (strcat "\n无法写入文件: " filename)) nil)))

;; —— 导出统计 ——
;; ddfd-do-export 把 *ddfd-export-stats* 声明成局部变量，导出过程中往里记 (类别 . 内容)，成功后汇总打印

(defun ddfd-stat-add (key item)
  (setq *ddfd-export-stats* (cons (cons key item) *ddfd-export-stats*)))

(defun ddfd-stat-get (key / res pair)
  (foreach pair *ddfd-export-stats* (if (= (car pair) key) (setq res (cons (cdr pair) res))))
  res)

;; —— 柜子/竖井导出 ——

(defun ddfd-export-point-row (fh pattern en / ent layer floor handle kind pt nm)
  (setq ent (entget en)
        layer (cdr (assoc 8 ent))
        floor (ddfd-layer-floor layer)
        handle (cdr (assoc 5 ent))
        kind (if (wcmatch pattern "CABLE_SHAFT*") "SHAFT" "CAB")
        pt (ddfd-entity-point en)
        nm (ddfd-string-trim (ddfd-entity-name en)))
  (if (= kind "CAB")
    (setq nm (ddfd-remove-spaces nm))
  )
  (cond
    ((= nm "") (ddfd-stat-add (strcat kind "-EMPTY") handle))
    ((and pt (>= (length pt) 2))
     (if (= floor "") (ddfd-stat-add "NOFLOOR" layer))
     (ddfd-stat-add kind (list nm floor handle))
     (if (= kind "SHAFT")
       (ddfd-write-csv-row fh
         (list nm floor (rtos (car pt) 2 6) (rtos (cadr pt) 2 6) "" layer (cdr (assoc 0 ent)) handle))
       (ddfd-write-csv-row fh
         (list nm floor "" (rtos (car pt) 2 6) (rtos (cadr pt) 2 6) layer (cdr (assoc 0 ent)) handle))))))

;; 逐个图元捕获错误：先保证文件被关闭（否则 .pending 被锁、清理不掉），
;; 再报出出错图元的句柄；有任何图元失败就返回 nil，整次导出放弃、保留原数据
(defun ddfd-export-fail-message (en result)
  (princ (strcat "\n无法读取图元 " (cdr (assoc 5 (entget en))) " ("
                 (cdr (assoc 8 (entget en))) "): " (vl-catch-all-error-message result))))

;; ssget "_X" 也会选到布局（图纸空间，67 组码为 1）里的图元，只导出模型空间的
(defun ddfd-export-point-layer (pattern filename header / ss fh i en result ok)
  (if (setq fh (open filename "w"))
    (progn
      (setq ok T)
      (ddfd-write-csv-row fh header)
      (if (setq ss (ssget "_X" (list (cons 8 pattern))))
        (progn
          (setq i 0)
          (while (< i (sslength ss))
            (setq en (ssname ss i))
            (if (= (cdr (assoc 67 (entget en))) 1)
              (ddfd-stat-add "PAPER" (cdr (assoc 5 (entget en))))
              (progn
                (setq result (vl-catch-all-apply 'ddfd-export-point-row (list fh pattern en)))
                (if (vl-catch-all-error-p result) (progn (ddfd-export-fail-message en result) (setq ok nil)))))
            (setq i (1+ i)))))
      (close fh)
      ok)
    (progn (princ (strcat "\n无法写入文件: " filename)) nil)))

;; —— 路径导出 ——

;; 凸度为 b 的圆弧段：圆心角 = 4*atan|b|，弧长 = 弦长*圆心角 / (2*sin(圆心角/2))；b 为 0 就是弦长
(defun ddfd-bulge-length (p1 p2 bulge / chord th)
  (setq chord (distance p1 p2))
  (if (and bulge (> (abs bulge) 1e-12) (> chord 0.0))
    (progn
      (setq th (* 4.0 (atan (abs bulge))))
      (/ (* chord th) (* 2.0 (sin (/ th 2.0)))))
    chord))

;; LWPOLYLINE 顶点：((WCS点 . 凸度) ...)；10 组码是 OCS 平面坐标，高程在 38 组码，凸度 42 组码跟在各自顶点后面
(defun ddfd-lwpoly-vertices (en ent / elev cur pts item)
  (setq elev (cond ((cdr (assoc 38 ent))) (T 0.0)))
  (foreach item ent
    (cond
      ((= (car item) 10)
       (if cur (setq pts (cons cur pts)))
       (setq cur (cons (trans (list (cadr item) (caddr item) elev) en 0) 0.0)))
      ((and cur (= (car item) 42))
       (setq cur (cons (car cur) (cdr item))))))
  (if cur (setq pts (cons cur pts)))
  (reverse pts))

;; 二维 POLYLINE：VERTEX 的 10 组码是 OCS（高程取 POLYLINE 自身 10 组码的 Z），凸度在 42 组码；
;; 三维多段线（70 组码含 8）的顶点就是 WCS、没有凸度；样条拟合的控制点（VERTEX 的 70 组码含 16）不在线上，跳过
(defun ddfd-poly-vertices (en ent / flags elev cur vd pt pts)
  (setq flags (cond ((cdr (assoc 70 ent))) (T 0))
        elev (cond ((caddr (cdr (assoc 10 ent)))) (T 0.0))
        cur (entnext en))
  (while (and cur (= (cdr (assoc 0 (setq vd (entget cur)))) "VERTEX"))
    (if (= 0 (logand (cond ((cdr (assoc 70 vd))) (T 0)) 16))
      (setq pt (cdr (assoc 10 vd))
            pts (cons (if (= 8 (logand flags 8))
                        (cons pt 0.0)
                        (cons (trans (list (car pt) (cadr pt) elev) en 0)
                              (cond ((cdr (assoc 42 vd))) (T 0.0))))
                      pts)))
    (setq cur (entnext cur)))
  (reverse pts))

;; 写一段路径；起点终点重合的段（重复顶点、整圆）跳过，段号仍按原顶点序号，与旧版一致
(defun ddfd-write-route-seg (fh routeid floor layer handle segno p1 p2 len)
  (if (> (distance p1 p2) 1e-9)
    (progn
      (ddfd-write-csv-row fh
        (list routeid floor
              (rtos (car p1) 2 6) (rtos (cadr p1) 2 6)
              (rtos (car p2) 2 6) (rtos (cadr p2) 2 6)
              (rtos (if len len (distance p1 p2)) 2 6) layer handle (itoa segno)))
      (ddfd-stat-add "SEG" handle))))

;; 一个路径图元写成若干段：LINE 一段；ARC 一段（圆心、半径、起止角都在 OCS 里，逆时针从起点角到终点角）；
;; 多段线每段一行、段号 1..n，闭合的最后一段从末点回到首点；网格/多面网格和其他图元不导出，记下来导出后提示
(defun ddfd-export-route-rows (fh en / ent layer handle typ floor routeid flags cen r a0 a1 sweep verts k)
  (setq ent (entget en)
        layer (cdr (assoc 8 ent))
        handle (cdr (assoc 5 ent))
        typ (cdr (assoc 0 ent))
        floor (ddfd-layer-floor layer)
        routeid (strcat "R" handle)
        flags (cond ((cdr (assoc 70 ent))) (T 0)))
  (if (or (member typ '("LINE" "ARC" "LWPOLYLINE"))
          (and (= typ "POLYLINE") (= 0 (logand flags 80))))
    (progn
      (if (= floor "") (ddfd-stat-add "NOFLOOR" layer))
      (cond
        ((= typ "LINE")
         (ddfd-write-route-seg fh routeid floor layer handle 1
           (cdr (assoc 10 ent)) (cdr (assoc 11 ent)) nil))
        ((= typ "ARC")
         (setq cen (cdr (assoc 10 ent)) r (cdr (assoc 40 ent))
               a0 (cdr (assoc 50 ent)) a1 (cdr (assoc 51 ent))
               sweep (- a1 a0))
         (while (<= sweep 0.0) (setq sweep (+ sweep pi pi)))
         (ddfd-write-route-seg fh routeid floor layer handle 1
           (trans (list (+ (car cen) (* r (cos a0))) (+ (cadr cen) (* r (sin a0))) (caddr cen)) en 0)
           (trans (list (+ (car cen) (* r (cos a1))) (+ (cadr cen) (* r (sin a1))) (caddr cen)) en 0)
           (* r sweep)))
        (T
         (setq verts (if (= typ "LWPOLYLINE") (ddfd-lwpoly-vertices en ent) (ddfd-poly-vertices en ent)))
         (if (and (= 1 (logand flags 1)) (cdr verts))
           (setq verts (append verts (list (car verts)))))
         (setq k 1)
         (while (cdr verts)
           (ddfd-write-route-seg fh routeid floor layer handle k (caar verts) (caadr verts)
             (ddfd-bulge-length (caar verts) (caadr verts) (cdar verts)))
           (setq verts (cdr verts) k (1+ k))))))
    (ddfd-stat-add "UNSUPPORTED" (cons (if (= typ "POLYLINE") "POLYLINE网格" typ) handle))))

(defun ddfd-export-routes (filename / ss fh i en result ok)
  (if (setq fh (open filename "w"))
    (progn
      (setq ok T)
      (ddfd-write-csv-row fh (list "路径编号" "楼层" "起点X" "起点Y" "终点X" "终点Y" "长度_CAD单位" "图层" "句柄" "段号"))
      (if (setq ss (ssget "_X" (list (cons 8 "CABLE_ROUTE*"))))
        (progn
          (setq i 0)
          (while (< i (sslength ss))
            (setq en (ssname ss i))
            (if (= (cdr (assoc 67 (entget en))) 1)
              (ddfd-stat-add "PAPER" (cdr (assoc 5 (entget en))))
              (progn
                (setq result (vl-catch-all-apply 'ddfd-export-route-rows (list fh en)))
                (if (vl-catch-all-error-p result) (progn (ddfd-export-fail-message en result) (setq ok nil)))))
            (setq i (1+ i)))))
      (close fh)
      ok)
    (progn (princ (strcat "\n无法写入文件: " filename)) nil)))

;; —— 导出汇总 ——

(defun ddfd-join (items sep / res s)
  (setq res (if items (car items) ""))
  (foreach s (cdr items) (setq res (strcat res sep s)))
  res)

;; 最多列出 limit 个，多了用省略号
(defun ddfd-join-limit (items sep limit / shown n s)
  (setq n 0)
  (foreach s items (if (< n limit) (setq shown (cons s shown))) (setq n (1+ n)))
  (strcat (ddfd-join (reverse shown) sep) (if (> n limit) (strcat sep "…") "")))

;; 计数：返回 ((项 . 个数) ...)，按首次出现的顺序
(defun ddfd-count-items (items / counts pair s)
  (foreach s items
    (if (setq pair (assoc s counts))
      (setq counts (subst (cons s (1+ (cdr pair))) pair counts))
      (setq counts (cons (cons s 1) counts))))
  (reverse counts))

;; 楼层排序键：地下室在前（B2F < B1F < 1F < 2F），楼层未知排最后
(defun ddfd-floor-key (fl)
  (cond ((= fl "") 1000)
        ((= (substr fl 1 1) "B") (- (atoi (substr fl 2))))
        (T (atoi fl))))

;; 导出成功后按文件报数量，再提示柜名重复、空柜名、不支持的路径图元、看不出楼层的图层、布局里的图元
(defun ddfd-export-summary (room-count / cabs floors keyed i prev run dups c empty unsup nofloor paper)
  (setq cabs (ddfd-stat-get "CAB")
        floors (vl-sort (ddfd-count-items (mapcar 'cadr cabs))
                        '(lambda (a b) (< (ddfd-floor-key (car a)) (ddfd-floor-key (car b))))))
  (princ (strcat "\n已导出四个CSV：柜子 " (itoa (length cabs)) " 个"
                 (if floors
                   (strcat "（"
                           (ddfd-join (mapcar '(lambda (p) (strcat (if (= (car p) "") "楼层未知" (car p)) " " (itoa (cdr p))))
                                              floors)
                                      " / ")
                           "）")
                   "")
                 "、路径线段 " (itoa (length (ddfd-stat-get "SEG"))) " 段"
                 "、竖井 " (itoa (length (ddfd-stat-get "SHAFT"))) " 个"
                 "、房间 " (itoa room-count) " 个。"))
  ;; 柜名重复：按 (柜名 CSV行序) 排序后相邻同名的归成一组，组内句柄按 CSV 顺序，第一个就是 Python 统计时采用的
  (setq i 0)
  (foreach c cabs (setq keyed (cons (list (car c) i (caddr c)) keyed) i (1+ i)))
  (foreach c (append (vl-sort keyed '(lambda (a b) (if (= (car a) (car b)) (< (cadr a) (cadr b)) (< (car a) (car b)))))
                     (list nil))
    (if (and prev c (= (car c) (car prev)))
      (setq run (cons (caddr c) run))
      (progn
        (if (cdr run)
          (setq dups (cons (strcat (car prev) "（句柄 " (ddfd-join (reverse run) "、") "）") dups)))
        (setq run (if c (list (caddr c))))))
    (setq prev c))
  (if dups
    (princ (strcat "\n注意：柜名重复 " (itoa (length dups)) " 个，统计时只用每组第一个句柄的坐标，请改名或删掉多余的："
                   (ddfd-join-limit (reverse dups) "；" 10))))
  (if (setq empty (ddfd-stat-get "CAB-EMPTY"))
    (princ (strcat "\n注意：柜子图层上有 " (itoa (length empty))
                   " 个图元没有柜名（空文字、点或其他非文字图元），已跳过。句柄: " (ddfd-join-limit empty " " 20))))
  (if (setq empty (ddfd-stat-get "SHAFT-EMPTY"))
    (princ (strcat "\n注意：竖井图层上有 " (itoa (length empty))
                   " 个图元没有竖井编号，已跳过。句柄: " (ddfd-join-limit empty " " 20))))
  (if (setq unsup (ddfd-stat-get "UNSUPPORTED"))
    (princ (strcat "\n注意：CABLE_ROUTE 图层上有 " (itoa (length unsup)) " 个不支持的图元（"
                   (ddfd-join (mapcar 'car (ddfd-count-items (mapcar 'car unsup))) "、")
                   "），请炸开或改画多段线。句柄: " (ddfd-join-limit (mapcar 'cdr unsup) " " 20))))
  (if (setq nofloor (ddfd-count-items (ddfd-stat-get "NOFLOOR")))
    (princ (strcat "\n注意：这些图层名看不出楼层（应以 _1F、_B1F 这类结尾），导出的楼层列为空："
                   (ddfd-join (mapcar '(lambda (p) (strcat (car p) "（" (itoa (cdr p)) " 个图元）")) nofloor) "、"))))
  (if (setq paper (ddfd-stat-get "PAPER"))
    (princ (strcat "\n提示：布局（图纸空间）里有 " (itoa (length paper))
                   " 个 CABLE_* 图元，只导出模型空间的，已忽略。")))
  (princ))

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

;; 房间可以没有（导出空的 房间范围.csv）；房间列表在导出前打印，便于当场核对。
;; *ddfd-export-stats* 是局部变量（动态作用域），导出函数记下的数量只在本次导出里有效
(defun ddfd-do-export (outdir / info rooms result names fn *ddfd-export-stats*)
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
            ;; 汇总只是提示，出错也不影响已经写好的文件
            (setq result (vl-catch-all-apply 'ddfd-export-summary
                           (list (if (= rooms 'NO-ROOMS) 0 (length rooms)))))
            (if (vl-catch-all-error-p result)
              (princ (strcat "\n已导出四个CSV（汇总提示出错: " (vl-catch-all-error-message result) "）")))
            T)
          (progn
            (princ "\n提交失败，请检查文件占用或 .previous 备份。")
            nil))
        (progn
          (if (vl-catch-all-error-p result)
            (princ (strcat "\n导出阶段发生错误: " (vl-catch-all-error-message result)))
            (princ "\n有图元读取失败或文件无法写入（见上面的提示），本次没有覆盖项目数据。"))
          (setq names '("柜子坐标.csv" "路径线段.csv" "竖井.csv" "房间范围.csv"))
          (foreach fn names
            (if (findfile (strcat outdir fn ".pending"))
              (vl-file-delete (strcat outdir fn ".pending"))))
          nil)))
    (progn (princ "\n已取消导出，未覆盖项目数据。") nil)))

(defun c:DDFD_EXPORT_CABLE_ROUTE (/ prefix outdir)
  (setq prefix (getvar "DWGPREFIX"))
  (setq outdir (ddfd-clean-path (getstring T (strcat "\n导出目录 <" prefix "cable_export>: "))))
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

;; 询问楼层数并记住（写入注册表环境项，下次默认沿用）；图层名 1F~99F 都能解析楼层
(defun ddfd-wiz-ask-floors (/ saved n)
  (setq saved (getenv "DDFD_CABLE_FLOORS"))
  (setq saved (if (and saved (> (atoi saved) 0)) (atoi saved) 2))
  (initget 6)
  (setq n (getint (strcat "\n这张图一共几层楼? <" (itoa saved) ">: ")))
  (if (not n) (setq n saved))
  (if (> n 99) (setq n 99))
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

;; getpoint 返回的是当前 UCS 坐标，TEXT 的 10 组码要的是 WCS（默认拉伸方向下 OCS 就是 WCS），先转换，
;; 否则用了 UCS 的图里文字会跑到别处
(defun ddfd-wiz-make-text (pt h str layer)
  (entmake (list (cons 0 "TEXT")
                 (cons 8 layer)
                 (cons 10 (trans pt 1 0))
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
  (princ "\n按楼层自动创建 4 类图层：柜子出线点(黄)、走线路径(青)、竖井(红)、房间(品红)。已存在的不会重建。")
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
  (princ (strcat "\n图层就绪（" (itoa n) " 层楼 × 4 类）。"))
  (princ)
)



;; 查找某楼层房间图层上已使用该名称的房间（排除 self），返回图元名或 nil
(defun ddfd-wiz-find-room (layer nm self / ss i en found)
  (if (setq ss (ssget "_X" (list '(0 . "*POLYLINE") (cons 8 layer))))
    (progn
      (setq i 0)
      (while (and (not found) (< i (sslength ss)))
        (setq en (ssname ss i) i (1+ i))
        (if (and (not (equal en self)) (= (ddfd-room-name en) nm))
          (setq found en)))))
  found)

;; —— 房间范围定义：给闭合直线多段线写房间名称 ——
;; 普通图层上的多段线复制一份到 CABLE_ROOM_nF 再命名（原图形不动）；
;; 已是房间或本来就画在房间图层上的，直接原地命名/改名，避免叠出重复的房间。
;; 不切换当前图层，之后画的图形不会误落到房间图层上。
(defun ddfd-wiz-step-rooms (/ n f layer pick en ent typ old nm dupe target tobj cnt)
  (princ "\n———— 房间范围定义 ————")
  (princ "\n逐个选择已闭合的直线多段线，输入房间名称。普通图层上的图形会复制一份到房间图层，原图形不动。")
  (setq n (ddfd-wiz-floors))
  (setq f 1)
  (while (<= f n)
    (if (ddfd-wiz-yesno (strcat "\n定义 " (itoa f) "F 房间范围吗?") "Y")
      (progn
        (setq layer (ddfd-wiz-layer-name "ROOM" f))
        (ddfd-wiz-make-layer layer 6)
        (setq cnt 0)
        (setq pick (entsel (strcat "\n[" (itoa f) "F] 选择闭合多段线（回车结束）: ")))
        (while pick
          (setq en (car pick))
          (setq ent (entget en))
          (setq typ (cdr (assoc 0 ent)))
          (cond
            ((not (member typ '("LWPOLYLINE" "POLYLINE")))
             (princ "\n对象不是 LWPOLYLINE/POLYLINE，已跳过。"))
            ((not (ddfd-room-closed-p en))
             (princ "\n多段线既未设置闭合标志，首尾点也未重合，已跳过。"))
            ((ddfd-room-has-arc en)
             (princ "\n房间范围不能包含圆弧段，请改用直线多段线。"))
            (T
             (setq old (ddfd-room-name en))
             (if (/= old "")
               (princ (strcat "\n这已经是房间「" old "」，输入新名称可改名，直接回车保持不变。")))
             (setq nm (getstring T "\n输入房间名称: "))
             (setq dupe (if (/= nm "") (ddfd-wiz-find-room layer nm en)))
             (cond
               ((= nm "")
                (princ (if (/= old "") "\n未改名。" "\n房间名称不能为空，已跳过。")))
               ((and dupe
                     (not (ddfd-wiz-yesno (strcat "\n" (itoa f) "F 已有房间「" nm "」，删掉旧的、用这次选的替换吗?") "N")))
                (princ "\n已跳过，原来的房间保持不变。"))
               (T
                (if dupe (entdel dupe))
                (if (or (/= old "") (wcmatch (strcase (cdr (assoc 8 ent))) "CABLE_ROOM*"))
                  (setq target en)
                  (setq target (vlax-vla-object->ename (vla-Copy (vlax-ename->vla-object en)))))
                (setq tobj (vlax-ename->vla-object target))
                (vla-put-Closed tobj :vlax-true)
                (vla-put-Layer tobj layer)
                (ddfd-room-set-name target nm)
                (setq cnt (1+ cnt))
                (princ (strcat "\n已定义房间：" nm))))))
          (setq pick (entsel (strcat "\n[" (itoa f) "F] 选择下一个闭合多段线（回车结束）: ")))
        )
        (princ (strcat "\n" (itoa f) "F 共定义 " (itoa cnt) " 个房间。"))
      )
    )
    (setq f (1+ f))
  )
  (princ)
)

;; 模型空间里柜子图层上已经用了这个柜名的图元，返回 ((句柄 . 图层) ...)；比较时和导出一样按去掉空格后的柜名
(defun ddfd-wiz-find-cabinet (nm / key ss i en ed res)
  (setq key (ddfd-remove-spaces (ddfd-text-plain nm)))
  (if (and (/= key "") (setq ss (ssget "_X" '((8 . "CABLE_CABINET*")))))
    (progn
      (setq i 0)
      (while (< i (sslength ss))
        (setq en (ssname ss i) ed (entget en) i (1+ i))
        (if (and (/= (cdr (assoc 67 ed)) 1)
                 (= key (ddfd-remove-spaces (ddfd-entity-name en))))
          (setq res (cons (cons (cdr (assoc 5 ed)) (cdr (assoc 8 ed))) res))))))
  (reverse res))

;; —— 第2步：标柜子出线点 ——
(defun ddfd-wiz-step2 (/ n f layer h pt nm dupe cnt)
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
          (cond
            ((= (ddfd-string-trim nm) "")
             (princ "  柜名为空，本点作废。"))
            ((and (setq dupe (ddfd-wiz-find-cabinet nm))
                  (not (ddfd-wiz-yesno
                         (strcat "\n柜名「" nm "」已经标过（" (cdar dupe) " 句柄 " (caar dupe)
                                 (if (cdr dupe) (strcat " 等 " (itoa (length dupe)) " 处") "")
                                 "），再标会重名，统计时只会用其中一个的位置。仍然标注吗?")
                         "N")))
             (princ "  已跳过，本点作废。"))
            (T
             (ddfd-wiz-make-text pt h nm layer)
             (setq cnt (1+ cnt))
             (princ (strcat "  已标注: " nm))))
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
  ;; 粘贴“复制文件地址”得到的路径带双引号，去掉
  (setq input (ddfd-clean-path input))
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
    ((= stepno 6) (ddfd-wiz-step-rooms))
  )
  (ddfd-wiz-restore)
  (princ)
)

;; 带说明的步骤菜单，选步骤前先能看到每一步是干什么的
(defun ddfd-wiz-menu ()
  (princ "\n=============== 电缆长度统计 · 引导向导 ===============")
  (princ "\n  [A] 从头一步步来（新图推荐，依次执行 1→5，含房间定义）")
  (princ "\n  [1] 建图层   - 输入楼层数，自动建 柜子/路径/竖井/房间 四类图层")
  (princ "\n  [R] 定义房间 - 选择闭合多段线并输入房间名称（已定义的再选一次可改名）")
  (princ "\n  [C] 检查房间 - 列出将导出的房间，定位房间图层上未命名/无效的多段线")
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
    (initget "A R C 1 2 3 4 5 Q")
    (setq choice (getkword (strcat "\n选择要做的步骤 [A/R/C/1/2/3/4/5/Q] <" (if first "A" "Q") ">: ")))
    (if (not choice) (setq choice (if first "A" "Q")))
    (setq first nil)
    (cond
      ((= choice "Q") (setq done T))
      ((= choice "A") (ddfd-wiz-run 0) (setq done T))
      ((= choice "R") (ddfd-wiz-run 6))
      ((= choice "C") (c:DDFD_ROOM_CHECK))
      (T (ddfd-wiz-run (atoi choice)))
    )
  )
  (princ "\n向导已退出，单步做完会回到菜单；随时输入 DDFD_CABLE_WIZARD 再进。")
  (princ)
)

(defun c:DDFD_CABLE_LAYERS () (ddfd-wiz-run 1))
(defun c:DDFD_CABLE_ROOMS () (ddfd-wiz-run 6))
(defun c:DDFD_CABLE_CABINETS () (ddfd-wiz-run 2))
(defun c:DDFD_CABLE_ROUTES () (ddfd-wiz-run 3))
(defun c:DDFD_CABLE_SHAFTS () (ddfd-wiz-run 4))
(defun c:DDFD_CABLE_EXPORT_RUN () (ddfd-wiz-run 5))

(princ (strcat "\n已加载电缆统计引导向导（版本 " *ddfd-cable-version* "）。输入 DDFD_CABLE_WIZARD 开始一步步操作。"))
(princ "\n分步命令: DDFD_CABLE_LAYERS 建图层 | DDFD_CABLE_ROOMS 定义房间 | DDFD_CABLE_CABINETS 标柜子 | DDFD_CABLE_ROUTES 画路径 | DDFD_CABLE_SHAFTS 标竖井 | DDFD_CABLE_EXPORT_RUN 导出并运行 | DDFD_EXPORT_CABLE_ROUTE 仅导出")
(princ "\n房间检查: DDFD_ROOM_CHECK 定位房间图层上的问题图形 | DDFD_CLEAN_ROOM_LAYERS 把未命名的移到 0 层")
(princ)
