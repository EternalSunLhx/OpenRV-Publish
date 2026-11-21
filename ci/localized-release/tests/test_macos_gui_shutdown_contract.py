"""Prevent successful direct Qt shutdown in the three shipped Mac GUI checks.

This conservative source guard supplements the callback tests and native release
gates. It does not establish that a real RV session can close or exit cleanly.
"""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
import sys
import unittest


def mac_success_shutdowns(source: str) -> list[str]:
    """Find reachable exit/quit calls without an explicit nonzero failure code.

    Only literal sys.platform comparisons can exclude a platform branch. Unknown
    conditions are scanned both ways. Within these small generated GUI programs,
    any .exit/.quit receiver is checked, including aliased Qt applications.
    """
    def platform_truth(node):
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            result = platform_truth(node.operand)
            return None if result is None else not result
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            left, right = node.left, node.comparators[0]
            platform = lambda value: (isinstance(value, ast.Attribute) and value.attr == 'platform'
                                      and isinstance(value.value, ast.Name) and value.value.id == 'sys')
            if platform(right):
                left, right = right, left
            if platform(left) and isinstance(right, ast.Constant) and isinstance(right.value, str):
                if isinstance(node.ops[0], ast.Eq):
                    return right.value == 'darwin'
                if isinstance(node.ops[0], ast.NotEq):
                    return right.value != 'darwin'
        return None

    def nonzero_guard(node, branch):
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            left, right = node.left, node.comparators[0]
            if isinstance(left, ast.Constant):
                left, right = right, left
            if (isinstance(left, ast.Name) and isinstance(right, ast.Constant)
                    and type(right.value) is int and right.value == 0
                    and ((branch and isinstance(node.ops[0], ast.NotEq))
                         or (not branch and isinstance(node.ops[0], ast.Eq)))):
                return {left.id}
        return set()

    class Check(ast.NodeVisitor):
        def __init__(self):
            self.nonzero = set()
            self.errors = []

        def record(self, node):
            self.errors.append(f'line {node.lineno}: {ast.unparse(node)}')

        def block(self, statements, facts):
            self.nonzero = set(facts)
            for statement in statements:
                self.visit(statement)
            return set(self.nonzero)

        def visit_If(self, node):
            self.visit(node.test)
            facts = set(self.nonzero)
            known = platform_truth(node.test)
            possible = []
            if known is not False:
                possible.append(self.block(node.body, facts | nonzero_guard(node.test, True)))
            if known is not True:
                possible.append(self.block(node.orelse, facts | nonzero_guard(node.test, False)))
            self.nonzero = set.intersection(*possible)

        def visit_IfExp(self, node):
            self.visit(node.test)
            known = platform_truth(node.test)
            if known is not False:
                self.visit(node.body)
            if known is not True:
                self.visit(node.orelse)

        def visit_Name(self, node):
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                self.nonzero.discard(node.id)

        def visit_FunctionDef(self, node):
            facts = set(self.nonzero)
            # A later callback invocation must not borrow an outer temporary fact.
            self.nonzero = set()
            self.generic_visit(node)
            self.nonzero = facts

        visit_AsyncFunctionDef = visit_FunctionDef
        visit_Lambda = visit_FunctionDef

        def visit_Attribute(self, node):
            if node.attr in ('exit', 'quit'):
                # A bare app.quit passed to singleShot also bypasses session close.
                self.record(node)
            self.generic_visit(node)

        def visit_Call(self, node):
            method = (node.func.attr if isinstance(node.func, ast.Attribute)
                      else node.func.id if isinstance(node.func, ast.Name) else None)
            if method not in ('exit', 'quit'):
                self.generic_visit(node)
                return
            code = node.args[0] if len(node.args) == 1 and not node.keywords else None
            if not node.args and len(node.keywords) == 1 and node.keywords[0].arg in ('returnCode', 'code', 'retcode'):
                code = node.keywords[0].value
            failure = isinstance(code, ast.Name) and code.id in self.nonzero
            if code is not None:
                try:
                    value = ast.literal_eval(code)
                    failure |= type(value) is int and value != 0
                except (ValueError, TypeError):
                    pass
            if method == 'quit' or not failure:
                self.record(node)
            # Do not reclassify this already-checked method as a bare callback.
            if isinstance(node.func, ast.Attribute):
                self.visit(node.func.value)
            for argument in [*node.args, *(keyword.value for keyword in node.keywords)]:
                self.visit(argument)

    check = Check()
    check.visit(ast.parse(source))
    return check.errors


class MacGuiShutdownContractTests(unittest.TestCase):
    def test_rejects_success_default_and_deferred_quit_examples(self):
        for source in ('app.exit(0)', 'app.exit()', 'app.quit()', 'QtCore.QCoreApplication.exit(0)',
                       'QtWidgets.QApplication.instance().quit()', 'application.exit(returnCode=0)',
                       'QtCore.QTimer.singleShot(0, lambda: app.exit())', 'QtCore.QTimer.singleShot(0, app.quit)',
                       'renamed_qt_app.exit(unknown_code)', 'if unknown_platform:\n    app.quit()'):
            with self.subTest(source=source):
                self.assertTrue(mac_success_shutdowns(source), source)

    def test_allows_failure_codes_and_explicit_non_darwin_branches(self):
        for source in ('app.exit(1)', 'QtCore.QCoreApplication.exit(-1)', 'app.exit(returnCode=2)',
                       'if code != 0:\n    app.exit(code)',
                       'if 0 == code:\n    window.close()\nelse:\n    app.exit(code)',
                       'if sys.platform != "darwin":\n    app.exit(0)\nelse:\n    window.close()',
                       'if "darwin" == sys.platform:\n    window.close()\nelse:\n    app.quit()',
                       'app.quit() if sys.platform == "win32" else window.close()'):
            with self.subTest(source=source):
                self.assertEqual(mac_success_shutdowns(source), [], source)

    def test_guards_do_not_hide_success_or_borrow_invalid_failure_proofs(self):
        for source in ('if sys.platform == "darwin":\n    app.exit(0)',
                       'if platform.system() == "Windows":\n    app.quit()',
                       'if code != 0:\n    app.exit(code)\nelse:\n    app.exit(code)',
                       'if code != 0:\n    code = 0\n    app.exit(code)',
                       'if code != 0:\n    if unknown:\n        code = 0\n    app.exit(code)',
                       'if code != 0:\n    callback = lambda: app.exit(code)',
                       'if code != 0:\n    app.quit()'):
            with self.subTest(source=source):
                self.assertTrue(mac_success_shutdowns(source), source)

    def test_formal_mac_gui_programs_have_no_successful_direct_shutdown(self):
        root = Path(__file__).resolve().parents[3]

        def load(name, path):
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module

        smoke = load('shutdown_contract_smoke', root / 'ci/localized-release/runtime_smoke.py')
        scripts = root / 'tests/localization'
        sys.path.insert(0, str(scripts))
        try:
            persistence = load('shutdown_contract_persistence', scripts / 'macos_language_persistence.py')
        finally:
            sys.path.remove(str(scripts))
        for name, source in (('runtime_smoke.GUI_SMOKE', smoke.GUI_SMOKE),
                             ('persistence.PROBE', persistence.PROBE),
                             ('persistence.gui_check_source()', persistence.gui_check_source())):
            with self.subTest(program=name):
                self.assertEqual(mac_success_shutdowns(source), [], name)


if __name__ == '__main__':
    unittest.main()
