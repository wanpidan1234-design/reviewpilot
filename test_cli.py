"""Public CLI contracts, with workflow imports replaced by inert stubs."""
import contextlib
import io
import sys
import types
import unittest
from unittest.mock import patch

import reviewpilot


class PublicCLI(unittest.TestCase):
    def test_help_and_version_do_not_import_a_workflow(self):
        for arguments in (["--help"], ["--version"], ["run", "--help"]):
            with self.subTest(arguments=arguments):
                with patch.object(reviewpilot.importlib, "import_module") as importer:
                    with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as stopped:
                        reviewpilot.main(arguments)
                    self.assertEqual(stopped.exception.code, 0)
                    importer.assert_not_called()

    def test_run_targets_only_one_synchronous_worker_and_restores_argv(self):
        observed = []
        previous = sys.argv
        engine = types.SimpleNamespace(
            __file__="engine.py",
            main=lambda: observed.append(list(sys.argv)) or 7,
        )
        with patch.object(reviewpilot.importlib, "import_module", return_value=engine) as importer:
            result = reviewpilot.main(["run", "--repo", "demo", "--pr", "42"])
        self.assertEqual(result, 7)
        importer.assert_called_once_with("engine")
        self.assertEqual(observed, [["engine.py", "--worker", "--repo", "demo", "--pr", "42"]])
        self.assertIs(sys.argv, previous)

    def test_missing_or_invalid_scope_never_imports_a_workflow(self):
        arguments = [[], ["run"], ["run", "--repo", "demo"],
                     ["run", "--repo", "demo", "--pr", "0"],
                     ["run", "--repo", "demo", "--pr", "-3"]]
        with patch.object(reviewpilot.importlib, "import_module") as importer:
            for argv in arguments:
                with self.subTest(argv=argv):
                    with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stopped:
                        reviewpilot.main(argv)
                    self.assertEqual(stopped.exception.code, 2)
            importer.assert_not_called()

    def test_optional_components_are_dispatched_only_when_selected(self):
        for name in ("labeler", "observer"):
            observed = []
            module = types.SimpleNamespace(__file__=name+".py", main=lambda: observed.append(list(sys.argv)) or 0)
            with patch.object(reviewpilot.importlib, "import_module", return_value=module) as importer:
                self.assertEqual(reviewpilot.main([name]), 0)
            importer.assert_called_once_with(name)
            self.assertEqual(observed, [[name+".py"]])


if __name__ == "__main__":
    unittest.main()
