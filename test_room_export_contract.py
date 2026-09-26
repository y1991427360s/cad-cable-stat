"""CAD 插件（两份 LISP）的源码契约检查；不连接 CAD。

实跑验证用 accoreconsole：python tools\\lisp_test\\run_lisp_test.py（合成图导出、逐项核对 CSV、端到端计算）。
"""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parent
SHARED_BANNER = ';;; ================= 通用与导出部分 ================='
WIZARD_BANNER = ';;; ================= 引导向导部分 ================='
# 导出流程的入口：从这里能调用到的函数都算“导出路径”
EXPORT_ROOTS = ('ddfd-do-export', 'c:DDFD_EXPORT_CABLE_ROUTE')
# 导出路径里唯一允许的 COM：动态块取原块名，必须包在 vl-catch-all-apply 里
COM_FALLBACK_FUNC = 'ddfd-block-effective-name'
COM_FALLBACK_SYMBOLS = {'vla-get-effectivename', 'vlax-ename->vla-object'}


def forms(text):
    tokens = re.findall(r';[^\n]*|"(?:\\.|[^"\\])*"|[()]|\x27|[^\s()\x27]+', text)
    tokens = iter(t for t in tokens if not t.startswith(';'))

    def read(token):
        if token == '(':
            result = []
            for t in tokens:
                if t == ')':
                    return result
                result.append(read(t))
            raise AssertionError('未闭合括号')
        if token == ')':
            raise AssertionError('多余右括号')
        if token == "'":
            return ['quote', read(next(tokens))]
        return token

    return [read(t) for t in tokens]


def calls(tree, name):
    if isinstance(tree, list):
        if tree and tree[0] == name:
            yield tree
        for value in tree:
            yield from calls(value, name)


def symbols(tree):
    """树里出现的全部符号（不含字符串字面量）。"""
    if isinstance(tree, list):
        for value in tree:
            yield from symbols(value)
    elif isinstance(tree, str) and not tree.startswith('"'):
        yield tree


def reachable(funcs, roots):
    """从 roots 出发能调用到的已定义函数（含 'ddfd-xxx 这种传给 vl-catch-all-apply 的引用），不分大小写。"""
    by_lower = {name.lower(): name for name in funcs}
    seen, stack = set(), [r.lower() for r in roots]
    while stack:
        key = stack.pop()
        if key in seen or key not in by_lower:
            continue
        seen.add(key)
        stack.extend(s.lower() for s in symbols(funcs[by_lower[key]][2:]) if s.lower() in by_lower)
    return {by_lower[key] for key in seen}


def com_symbols(tree, guarded=False):
    """vla-/vlax- 符号及其是否处在 vl-catch-all-apply 调用之内。"""
    if isinstance(tree, list):
        inner = guarded or (bool(tree) and tree[0] == 'vl-catch-all-apply')
        for value in tree:
            yield from com_symbols(value, inner)
    elif isinstance(tree, str) and re.match(r'(?i)vlax?-', tree):
        yield tree, guarded


class RoomExportContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.code = {}
        cls.funcs = {}
        for name in ('cad_cable_wizard.lsp', 'cad_export_cable_route.lsp'):
            raw = (ROOT / name).read_bytes()
            assert b'\n' not in raw.replace(b'\r\n', b'')
            assert b'\r' not in raw.replace(b'\r\n', b'')
            text = raw.decode('gbk', errors='strict')
            assert '\ufffd' not in text
            cls.code[name] = text
            definitions = [f for f in forms(text) if isinstance(f, list) and f and f[0] == 'defun']
            cls.funcs[name] = {f[1]: f for f in definitions}
            assert len(cls.funcs[name]) == len(definitions), '重复函数定义'
        cls.f = cls.funcs['cad_cable_wizard.lsp']
        cls.e = cls.funcs['cad_export_cable_route.lsp']

    def test_shared_export_functions_identical(self):
        shared = ('ddfd-room-', 'ddfd-export-', 'ddfd-do-export', 'c:DDFD_EXPORT_CABLE_ROUTE',
                  'c:DDFD_ROOM_CHECK', 'c:DDFD_CLEAN_ROOM_LAYERS')
        for name in self.f:
            if name.startswith(shared):
                self.assertEqual(self.f[name], self.e[name], name)
        # 精简版里不能有向导版没有的房间/导出函数（防止残留旧实现）
        for name in self.e:
            if name.startswith(shared):
                self.assertIn(name, self.f, name)

    def test_every_export_file_function_identical_in_wizard(self):
        # (a) 精简版的每个 defun 都必须在向导里、且逐项相同
        for name, form in self.e.items():
            self.assertIn(name, self.f, name)
            self.assertEqual(form, self.f[name], name)
        # 反过来，向导里除了向导专用函数，其余都要在精简版里（精简版能单独加载）
        for name in self.f:
            if not name.startswith(('ddfd-wiz-', 'c:DDFD_CABLE_')):
                self.assertIn(name, self.e, name)

    def test_shared_section_copied_verbatim(self):
        wizard = self.code['cad_cable_wizard.lsp']
        export = self.code['cad_export_cable_route.lsp']
        start, end = wizard.index(SHARED_BANNER), wizard.index(WIZARD_BANNER)
        self.assertLess(start, end)
        self.assertIn(wizard[start:end], export)
        self.assertEqual(export.count(SHARED_BANNER), 1)
        self.assertNotIn(WIZARD_BANNER, export)

    def test_version_banner_identical(self):
        versions = {re.search(r'\(setq \*ddfd-cable-version\* "([^"]+)"\)', code).group(1) for code in self.code.values()}
        self.assertEqual(len(versions), 1)

    def test_export_path_has_no_com_except_guarded_effective_name(self):
        # (b) 导出路径只读 DXF；accoreconsole 和部分 ZWCAD 没有 COM
        for file_name, funcs in self.funcs.items():
            path = reachable(funcs, EXPORT_ROOTS)
            for must in ('ddfd-export-stage', 'ddfd-export-route-rows', 'ddfd-entity-point', 'ddfd-entity-name',
                         'ddfd-block-first-attr', COM_FALLBACK_FUNC, 'ddfd-export-summary', 'ddfd-room-collect'):
                self.assertIn(must, path, f'{file_name}: {must} 不在导出路径上')
            for name in path:
                for sym, guarded in com_symbols(funcs[name]):
                    self.assertEqual(name, COM_FALLBACK_FUNC, f'{file_name}: {name} 调用了 {sym}')
                    self.assertTrue(guarded, f'{file_name}: {sym} 没有包在 vl-catch-all-apply 里')
                    self.assertIn(sym.lower(), COM_FALLBACK_SYMBOLS, sym)
            self.assertNotIn('vlax-curve', self.code[file_name])
            self.assertNotIn('ddfd-point->list', funcs)

    def test_point_and_route_exporters_skip_paper_space(self):
        # (c) ssget "_X" 也会选到布局里的图元（67 组码为 1），每个图元先过这道闸
        for exporter, row_fn in (('ddfd-export-point-layer', 'ddfd-export-point-row'),
                                 ('ddfd-export-routes', 'ddfd-export-route-rows')):
            gates = [form for form in calls(self.f[exporter], 'if')
                     if any(c[1] == '67' for c in calls(form[1], 'assoc'))]
            self.assertEqual(len(gates), 1, exporter)
            gate = gates[0]
            self.assertEqual(gate[1], ['=', ['cdr', ['assoc', '67', ['entget', 'en']]], '1'], exporter)
            self.assertNotIn(row_fn, str(gate[2]), exporter)
            self.assertEqual(len(gate), 4, exporter)
            self.assertIn(row_fn, str(gate[3]), exporter)

    def test_csv_escape_doubles_every_quote(self):
        # (d) vl-string-subst 只换第一处；转义必须用全部替换的辅助函数
        escape = self.f['ddfd-csv-escape']
        self.assertTrue(list(calls(escape, 'ddfd-string-replace-all')))
        helper = self.f['ddfd-string-replace-all']
        loops = list(calls(helper, 'while'))
        self.assertTrue(loops and list(calls(loops[0], 'vl-string-search')))
        for file_name, funcs in self.funcs.items():
            for name, form in funcs.items():
                self.assertNotIn('vl-string-subst', list(symbols(form)), f'{file_name}: {name}')

    def test_remove_spaces_only_strips_ascii_whitespace(self):
        # MBCS 版 AutoLISP 里 160 可能是 GBK 尾字节，全角/不间断空格交给 Python
        form = self.f['ddfd-remove-spaces']
        self.assertIn(['quote', ['9', '10', '13', '32']], list(calls(form, 'quote')))
        self.assertNotIn('160', list(symbols(form)))
        self.assertNotIn('12288', list(symbols(form)))

    def test_text_scanning_is_mbcs_safe(self):
        # 逐字处理一律先拆成字符（MBCS 下 GBK 双字节算一个），不直接按字节找 \ { } B F
        for name in ('ddfd-mtext-plain', 'ddfd-text-plain', 'ddfd-layer-floor'):
            self.assertTrue(list(calls(self.f[name], 'ddfd-string-chars')), name)
        chars = self.f['ddfd-string-chars']
        self.assertTrue(list(calls(chars, 'ddfd-mbcs-p')))

    def test_route_export_reads_dxf_and_reports_unsupported(self):
        rows = self.f['ddfd-export-route-rows']
        text = str(rows)
        for typ in ('"LINE"', '"ARC"', '"LWPOLYLINE"', '"POLYLINE"'):
            self.assertIn(typ, text)
        self.assertTrue([c for c in calls(rows, 'ddfd-stat-add') if c[1] == '"UNSUPPORTED"'])
        self.assertTrue(list(calls(rows, 'ddfd-bulge-length')))
        self.assertTrue(list(calls(self.f['ddfd-export-route-rows'], 'trans')))
        self.assertTrue(list(calls(self.f['ddfd-lwpoly-vertices'], 'trans')))
        self.assertTrue(list(calls(self.f['ddfd-poly-vertices'], 'trans')))
        self.assertTrue(list(calls(self.f['ddfd-entity-point'], 'trans')))

    def test_summary_printed_only_after_commit(self):
        fn = self.f['ddfd-do-export']
        commits = [form for form in calls(fn, 'if') if form[1] == ['ddfd-export-commit', 'outdir']]
        self.assertEqual(len(commits), 1)
        self.assertIn('ddfd-export-summary', str(commits[0][2]))
        self.assertNotIn('ddfd-export-summary', str(commits[0][3]))
        self.assertEqual(str(fn).count('ddfd-export-summary'), 1)
        # 统计变量是 ddfd-do-export 的局部变量，不会带到下一次导出
        self.assertIn('*ddfd-export-stats*', fn[2])

    def test_direct_export_without_interactive_prompts(self):
        collect = self.f['ddfd-room-collect']
        for name in ('getkword', 'initget', 'getstring', 'open', 'vl-file-delete', 'entmod', 'entdel'):
            self.assertFalse(list(calls(collect, name)), name)

    def test_room_requires_name_xdata(self):
        # 房间图层上未命名的多段线（图框、表格等）不能当房间导出
        entity_p = self.f['ddfd-room-entity-p']
        self.assertIn('"DDFD_CABLE_ROOM"', str(entity_p))
        self.assertIn(['assoc', '-3', 'ent'], list(calls(entity_p, 'assoc')))
        self.assertNotIn('CABLE_ROOM*', str(entity_p))
        self.assertNotIn('ddfd-room-has-named-p', self.f)

    def test_step_rooms_keeps_current_layer(self):
        # 定义房间不能把当前图层留在 CABLE_ROOM_nF，否则之后画的图形会落到房间图层
        step = self.f['ddfd-wiz-step-rooms']
        self.assertFalse([c for c in calls(step, 'setvar') if c[1] == '"CLAYER"'])
        self.assertIn(['ddfd-wiz-run', '6'], list(calls(self.f['c:DDFD_CABLE_ROOMS'], 'ddfd-wiz-run')))
        self.assertIn(['ddfd-wiz-run', '6'], list(calls(self.f['c:DDFD_CABLE_WIZARD'], 'ddfd-wiz-run')))
        self.assertTrue(list(calls(step, 'ddfd-wiz-find-room')))

    def test_step2_confirms_duplicate_cabinet_name(self):
        step = self.f['ddfd-wiz-step2']
        found = list(calls(step, 'ddfd-wiz-find-cabinet'))
        self.assertTrue(found)
        # 重名时要问一句，默认 N（不标）
        confirm = [c for c in calls(step, 'ddfd-wiz-yesno') if c[-1] == '"N"']
        self.assertTrue(confirm)
        finder = self.f['ddfd-wiz-find-cabinet']
        self.assertIn('CABLE_CABINET*', str(finder))
        self.assertTrue(any(c[1] == '67' for c in calls(finder, 'assoc')))
        self.assertTrue(list(calls(finder, 'ddfd-remove-spaces')))

    def test_wizard_text_uses_wcs_point(self):
        # getpoint 给的是 UCS 坐标，TEXT 的 10 组码要 WCS
        self.assertIn(['trans', 'pt', '1', '0'], list(calls(self.f['ddfd-wiz-make-text'], 'trans')))

    def test_floor_count_matches_layer_parser(self):
        # 图层名 1F~99F 都能解析出楼层，向导的楼层数上限与之一致
        ask = self.f['ddfd-wiz-ask-floors']
        self.assertIn(['>', 'n', '99'], list(calls(ask, '>')))
        self.assertIn('99', list(symbols(self.f['ddfd-layer-floor'])))

    def test_clean_room_layers_confirms_and_is_undoable(self):
        clean = self.f['c:DDFD_CLEAN_ROOM_LAYERS']
        self.assertTrue(list(calls(clean, 'getkword')))
        self.assertIn('"_BE"', str(clean))
        self.assertIn('"_E"', str(clean))
        self.assertFalse(list(calls(self.f['c:DDFD_ROOM_CHECK'], 'entmod')))

    def test_cancellation_gates_all_writes(self):
        fn = self.f['ddfd-do-export']
        gate = fn[-1]
        self.assertEqual(gate[:2], ['if', 'rooms'])
        self.assertFalse(list(calls(gate[3], 'open')))
        self.assertFalse(list(calls(gate[3], 'ddfd-export-stage')))
        self.assertTrue(list(calls(gate[2], 'ddfd-export-commit')))

    def test_wizard_never_copies_stale_export_and_gates_launch(self):
        wizard = self.f['ddfd-wiz-step5']
        self.assertFalse(list(calls(wizard, 'vl-file-copy')))
        gates = [x for x in calls(wizard, 'if') if x[1] == ['ddfd-do-export', 'outdir']]
        self.assertEqual(len(gates), 1)
        self.assertEqual(len(list(calls(gates[0][2], 'startapp'))), 1)
        self.assertEqual(len(list(calls(wizard, 'startapp'))), 1)

    def test_room_geometry_does_not_call_com(self):
        for name in ('ddfd-room-vertices', 'ddfd-room-closed-p', 'ddfd-export-one-room',
                     'ddfd-room-collect', 'ddfd-room-zoom'):
            self.assertNotRegex(str(self.f[name]), r'vlax-curve|vla-get|vlax-ename')

    def test_transaction_stages_all_four_files_before_commit(self):
        stage = str(self.f['ddfd-export-stage'])
        for name in ('柜子坐标.csv', '路径线段.csv', '竖井.csv', '房间范围.csv'):
            self.assertIn(name + '.pending', stage)
        commit = self.f['ddfd-export-commit']
        self.assertTrue(list(calls(commit, 'vl-file-rename')))
        self.assertIn('installed', str(commit))
        self.assertIn('saved', str(commit))

    def test_room_last_point_not_nested_car(self):
        for name in ('cad_cable_wizard.lsp', 'cad_export_cable_route.lsp'):
            code = self.code[name]
            self.assertNotIn('(car (last pts))', code)
            self.assertIn('(last pts)', code)

    def test_room_xdata_reads_assoc_on_cdr_item(self):
        for name in ('cad_cable_wizard.lsp', 'cad_export_cable_route.lsp'):
            code = self.code[name]
            self.assertNotIn('(assoc 1000 item)', code)
            self.assertIn('(assoc 1000 (cdr item))', code)


if __name__ == '__main__':
    unittest.main()
