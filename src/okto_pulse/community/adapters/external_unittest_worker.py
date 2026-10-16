"""Child-process unittest observer. No Pulse credentials or receipt authority."""

import importlib.util
import json
from pathlib import Path
import sys
import unittest


def main():
    root, result_path, *selectors = sys.argv[1:]
    sys.path.insert(0, root)
    suite = unittest.TestSuite()
    for index, selector in enumerate(selectors):
        filename, name = selector.split("::", 1)
        spec = importlib.util.spec_from_file_location(f"pulse_test_{index}", Path(root) / filename)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        loader = unittest.TestLoader()
        selected = loader.loadTestsFromName(name, module)
        if loader.errors or selected.countTestCases() != 1:
            raise ValueError("exactly one existing test method required per selector")
        suite.addTests(selected)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    observed = {
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "expected_failures": len(result.expectedFailures),
        "unexpected_successes": len(result.unexpectedSuccesses),
    }
    Path(result_path).write_text(json.dumps(observed), encoding="utf-8")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    try:
        exit_code = main()
    except Exception as exc:
        # Expose the category, not arbitrary exception text that may contain
        # project secrets. Setup failure is not an executed failing test.
        Path(sys.argv[2]).write_text(json.dumps({"runner_error": type(exc).__name__}), encoding="utf-8")
        exit_code = 2
    raise SystemExit(exit_code)
