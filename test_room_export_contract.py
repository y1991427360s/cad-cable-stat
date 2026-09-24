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
        shared = ('ddfd-room-', 'ddfd-export-', 'ddfd-do-export', 'c:DDFD_EXPORT_CABLE_ROUTE',
                  'c:DDFD_ROOM_CHECK', 'c:DDFD_CLEAN_ROOM_LAYERS')
        export_funcs = self.funcs['cad_export_cable_route.lsp']
        for name in self.f:
            if name.startswith(shared):
                self.assertEqual(self.f[name], export_funcs[name], name)
        # 精简版里不能有向导版没有的房间/导出函数（防止残留旧实现）
        for name in export_funcs:
            if name.startswith(shared):
                self.assertIn(name, self.f, name)

    def test_version_banner_identical(self):
        versions = {re.search(r'\(setq \*ddfd-cable-version\* "([^"]+)"\)', code).group(1) for code in self.code.values()}
        self.assertEqual(len(versions), 1)

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
