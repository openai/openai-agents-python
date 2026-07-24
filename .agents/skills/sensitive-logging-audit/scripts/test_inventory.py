from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from inventory_logging import collect_source_files, inventory_source, summarize


class InventoryTests(unittest.TestCase):
    def test_collects_direct_logging_calls_without_certifying_receivers(self) -> None:
        candidates = inventory_source(
            """
from logging import error

logger.debug("ready")
logger.error("failed: %s", secret)
error(secret)
task.exception()
"""
        )

        self.assertEqual(
            [(item.kind, item.method) for item in candidates],
            [
                ("logging-call-candidate", "debug"),
                ("logging-call-candidate", "error"),
                ("logging-call-candidate", "error"),
                ("logging-call-candidate", "exception"),
            ],
        )

    def test_collects_policy_helpers_without_claiming_their_callers_are_safe(self) -> None:
        candidates = inventory_source(
            """
from agents.logger import log_model_action_error

log_model_action_error(logger, "failed", error)
agents.logger.log_model_and_tool_action_warning(logger, "failed", error)
"""
        )

        self.assertEqual(
            [(item.kind, item.method) for item in candidates],
            [
                ("policy-helper-call", "log_model_action_error"),
                ("policy-helper-call", "log_model_and_tool_action_warning"),
            ],
        )

    def test_collects_raw_output_method_names(self) -> None:
        candidates = inventory_source(
            """
import os
import pprint
import sys
import traceback
import warnings

print(secret)
pprint.pp(secret)
warnings.warn(secret)
sys.stderr.buffer.write(secret_bytes)
sys.stdout.writelines([secret])
traceback.print_exception(error)
os.write(2, secret_bytes)
"""
        )

        self.assertEqual(
            [(item.kind, item.method) for item in candidates],
            [
                ("raw-output-call-candidate", "print"),
                ("raw-output-call-candidate", "pp"),
                ("raw-output-call-candidate", "warn"),
                ("raw-output-call-candidate", "write"),
                ("raw-output-call-candidate", "writelines"),
                ("raw-output-call-candidate", "print_exception"),
                ("raw-output-call-candidate", "write"),
            ],
        )

    def test_collects_constant_getattr_sink_selections(self) -> None:
        candidates = inventory_source(
            """
emit = getattr(logger, "error")
writer = builtins.getattr(stream, "write")
ignored = getattr(logger, method_name)
"""
        )

        self.assertEqual(
            [(item.kind, item.method) for item in candidates],
            [
                ("getattr-sink-candidate", "error"),
                ("getattr-sink-candidate", "write"),
            ],
        )

    def test_collects_obvious_logging_callbacks(self) -> None:
        candidates = inventory_source(
            """
register(log.warning)
register(on_error=service.error)
register(result=request.error)
"""
        )

        self.assertEqual(
            [(item.kind, item.method, item.call) for item in candidates],
            [
                ("logging-callback-candidate", "warning", "log.warning"),
                ("logging-callback-candidate", "error", "service.error"),
            ],
        )

    def test_keeps_the_output_schema_free_of_security_certification(self) -> None:
        candidate = inventory_source('logger.error("failed", secret)')[0].to_dict()

        self.assertEqual(
            set(candidate),
            {
                "fingerprint",
                "file",
                "line",
                "column",
                "kind",
                "method",
                "context",
                "call",
                "reason",
            },
        )
        self.assertNotIn("policy", candidate)
        self.assertNotIn("safe", candidate)

    def test_reports_enclosing_scope_as_review_context(self) -> None:
        candidate = inventory_source(
            """
class Worker:
    def report(self):
        logger.error(secret)
"""
        )[0]

        self.assertEqual(candidate.context, "class:Worker>function:report")

    def test_does_not_claim_to_follow_assignment_aliases(self) -> None:
        candidates = inventory_source(
            """
emit = logger.error
emit(secret)
"""
        )

        self.assertEqual(candidates, [])

    def test_summary_counts_only_syntactic_candidate_categories(self) -> None:
        candidates = inventory_source(
            """
logger.error(secret)
print(secret)
log_tool_action_error(logger, "failed", error)
register(on_error=service.error)
getattr(logger, "warning")
"""
        )

        self.assertEqual(
            summarize(candidates),
            {
                "totalCandidates": 5,
                "loggingCalls": 1,
                "rawOutputCalls": 1,
                "policyHelperCalls": 1,
                "getattrSelections": 1,
                "callbackReferences": 1,
            },
        )

    def test_collect_source_files_filters_hidden_children_relative_to_root(self) -> None:
        with TemporaryDirectory(prefix=".hidden-parent-") as directory:
            root = Path(directory) / "scan"
            root.mkdir()
            visible = root / "visible.py"
            visible.write_text("print('visible')\n")
            hidden = root / ".cache"
            hidden.mkdir()
            (hidden / "hidden.py").write_text("print('hidden')\n")

            self.assertEqual(collect_source_files([root]), [visible.resolve()])


if __name__ == "__main__":
    unittest.main()
