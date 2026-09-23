"""房间导出源码契约检查；不连接 CAD，不代表原生运行验收。"""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parent


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

    def test_shared_export_functions_identical(self):
        for name in self.f:
            if name.startswith(('ddfd-room-', 'ddfd-export-', 'ddfd-do-export')):
                self.assertEqual(self.f[name], self.funcs['cad_export_cable_route.lsp'][name], name)

    def test_direct_export_without_interactive_prompts(self):
        choose = self.f['ddfd-room-scope-choose']
        self.assertFalse(list(calls(choose, 'getkword')))
        self.assertFalse(list(calls(choose, 'initget')))
        self.assertFalse(list(calls(choose, 'getstring')))
        self.assertFalse(list(calls(choose, 'open')))
        self.assertFalse(list(calls(choose, 'vl-file-delete')))

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
        for name in ('ddfd-room-vertices', 'ddfd-room-closed-p', 'ddfd-export-one-room'):
            self.assertNotRegex(str(self.f[name]), r'vlax-curve|vla-get|vlax-ename')

    def test_scope_file_is_bound_to_drawing_and_all_handles(self):
        load = self.f['ddfd-room-scope-load']
        self.assertIn(['getvar', '"DWGNAME"'], list(calls(load, 'getvar')))
        self.assertIn(['setq', 'valid', 'nil'], list(calls(load, 'setq')))
        self.assertIn(['and', 'valid', 'rooms'], list(calls(load, 'and')))

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
