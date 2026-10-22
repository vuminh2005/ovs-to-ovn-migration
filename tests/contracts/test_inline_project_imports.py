"""Catch project imports in Python heredocs that syntax checks alone accept."""
import ast
import importlib.util
import unittest

from tests.support import ROOT

spec = importlib.util.spec_from_file_location('project_check', ROOT/'tools/check_project.py')
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


class InlineImportTests(unittest.TestCase):
    def test_original_argv_sys_path_pattern_is_rejected(self):
        tree = ast.parse('import sys\nsys.path.insert(0, sys.argv[1])\n'
                         'from extended_validation import Extended\n')
        self.assertEqual(len(checker.inline_project_import_errors(tree)), 1)

    def test_resolver_must_run_before_project_import(self):
        valid = 'from _paths import configure\nconfigure()\nfrom run_layout import run_path\n'
        self.assertEqual(checker.inline_project_import_errors(ast.parse(valid)), [])
        late = 'from _paths import configure\nfrom run_layout import run_path\nconfigure()\n'
        self.assertEqual(len(checker.inline_project_import_errors(ast.parse(late))), 1)
