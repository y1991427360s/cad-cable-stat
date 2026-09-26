;;; 多氟多电缆长度自动统计 - CAD路径导出（开发用精简版；用户侧只发 cad_cable_wizard.lsp）
;;; 命令：DDFD_EXPORT_CABLE_ROUTE（导出4个CSV）| DDFD_ROOM_CHECK 检查房间 | DDFD_CLEAN_ROOM_LAYERS 清理房间图层
;;; 本文件 = 本文件头 + cad_cable_wizard.lsp 的“通用与导出部分”原样复制 + 加载提示。
;;; 改导出逻辑请改向导文件，再运行 python tools\lisp_test\sync_export_lsp.py 同步过来。
;;; 柜子/竖井支持 TEXT、MTEXT（自动去格式）、INSERT（取第一个非空属性值，没有属性用块名）；
;;; 路径支持 LINE、LWPOLYLINE、POLYLINE（含圆弧段，长度按弧长）、ARC，SPLINE/椭圆等不导出、导出后列出句柄。
;;; 只导出模型空间，全部读 DXF 数据，不依赖 COM。
;;; 建议图层（n 为楼层号 1~99，地下室写 B1F、B2F）：
;;;   CABLE_CABINET_nF   柜子出线点文字或块
;;;   CABLE_ROUTE_nF     固定电缆沟/桥架中心线
;;;   CABLE_SHAFT_nF     电缆竖井文字或块（同一竖井各层命名 ZJ1_1F / ZJ1_2F 配对）
;;;   CABLE_ROOM_nF      房间边界（需用向导的“定义房间”写入名称）

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

;;; ================= 加载提示 =================

(princ (strcat "\n已加载：DDFD_EXPORT_CABLE_ROUTE / DDFD_ROOM_CHECK / DDFD_CLEAN_ROOM_LAYERS（版本 " *ddfd-cable-version* "）"))
(princ)
