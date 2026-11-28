# SPDX-License-Identifier: Apache-2.0
"""The GIMP entry point cannot be imported without GIMP, so these tests read gimp-openfill.py
as source: every procedure it names must be listed, created and given a run function."""

import ast
import unittest
from pathlib import Path

ENTRY = Path(__file__).resolve().parent.parent / "gimp-openfill" / "gimp-openfill.py"


class EntryProcedureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = ENTRY.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)

    def _constants(self) -> dict[str, str]:
        found = {}
        for node in self.tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name) and target.id.startswith("PROC_"):
                    found[target.id] = ast.literal_eval(node.value)
        return found

    def _plugin_method(self, name: str) -> ast.FunctionDef:
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ClassDef) and node.name == "OpenFill":
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == name:
                        return item
        raise AssertionError(f"OpenFill.{name} not found")

    def _names_used(self, node: ast.AST) -> set[str]:
        return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}

    def test_every_procedure_is_listed_and_created(self):
        constants = self._constants()
        listed = self._names_used(self._plugin_method("do_query_procedures"))
        created = self._names_used(self._plugin_method("do_create_procedure"))
        for const in constants:
            self.assertIn(const, listed, f"{const} is not in do_query_procedures")
            self.assertIn(const, created, f"{const} is not handled in do_create_procedure")

    def test_procedure_names_are_unique(self):
        values = list(self._constants().values())
        self.assertEqual(len(values), len(set(values)))

    def test_remove_background_asks_where_the_result_goes(self):
        self.assertEqual(self._constants()["PROC_BGREMOVE"], "python-fu-openfill-remove-background")
        runner = next(n for n in self.tree.body if isinstance(n, ast.FunctionDef) and n.name == "run_remove_background")
        calls = [n for n in ast.walk(runner) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
        work = [c for c in calls if c.func.attr == "remove_background"]
        self.assertEqual(len(work), 1)  # App.remove_background does the work
        self.assertEqual([k.arg for k in work[0].keywords], ["output"])  # the dialog's choice is passed on
        self.assertIn("_dialog", self._names_used(runner))  # the dialog asks for it

    def test_remove_background_registers_the_output_choice(self):
        method = self._plugin_method("_remove_background")
        choices = [
            n
            for n in ast.walk(method)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "add_choice_argument"
        ]
        self.assertEqual([ast.literal_eval(c.args[0]) for c in choices], ["output"])
        self.assertEqual(ast.literal_eval(choices[0].args[4]), "layer")  # the default: a new layer

    def test_menu_docstring_lists_remove_background(self):
        self.assertIn("Remove Background...", ast.get_docstring(self.tree))
        self.assertIn("python-fu-openfill-remove-background", ast.get_docstring(self.tree))


if __name__ == "__main__":
    unittest.main()
