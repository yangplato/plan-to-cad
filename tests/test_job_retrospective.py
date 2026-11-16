import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("job_retrospective", Path(__file__).resolve().parents[1] / "scripts/job_retrospective.py")
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class RetrospectiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.job = Path(self.temp.name)
        (self.job / "final.dwg").write_bytes(b"actual frozen final")
        (self.job / "before.json").write_text('{"thickness":430}')
        (self.job / "after.json").write_text('{"thickness":200}')
        (self.job / "review.json").write_text('{"status":"pending"}')
        self.data = {
            "job_id": "test-plan", "new_experience": True,
            "final_file": {"path": "final.dwg", "sha256": hashlib.sha256((self.job / "final.dwg").read_bytes()).hexdigest()},
            "checks": [{"id": "wall", "status": "pass", "evidence": ["after.json"]}],
            "findings": [{"id": "overlap", "problem": "parallel walls overlap", "method": "measure union at the original section",
                          "before": {"measurement": "430 mm", "evidence": ["before.json"]},
                          "after": {"measurement": "200 mm", "evidence": ["after.json"]},
                          "recheck": {"status": "pass", "evidence": ["after.json"]}}],
            "unverified": ["all door clearances"], "timing": {"active_minutes": 0, "background_minutes": 2.5},
        }

    def write(self, *, overwrite=False):
        input_path = self.job / "input.json"
        input_path.write_text(json.dumps(self.data), encoding="utf-8")
        return module.write_report(input_path, self.job, overwrite=overwrite)

    def test_records_measured_change_and_actual_hash_without_global_verdict(self):
        output = self.write()
        text = output.read_text()
        self.assertIn("430 mm", text)
        self.assertIn("200 mm", text)
        self.assertIn(self.data["final_file"]["sha256"], text)
        self.assertIn("all door clearances", text)
        self.assertIn("实际操作0分钟", text)
        self.assertIn("不是脚本作出的整图验收", text)
        self.assertEqual((self.job / "final.dwg").read_bytes(), b"actual frozen final")

    def test_zero_error_prose_does_not_create_pass(self):
        self.data["checks"] = []
        self.data["summary"] = "0 errors, all passed"
        with self.assertRaises(module.ReportError):
            self.write()
        self.assertFalse((self.job / "作业复盘.md").exists())

    def test_rejects_pass_without_evidence(self):
        self.data["checks"][0]["evidence"] = []
        with self.assertRaises(module.ReportError):
            self.write()

    def test_known_failure_and_pending_precede_new_success(self):
        self.data["checks"] = [
            {"id": "native-reopen", "status": "pending", "reason": "not run"},
            {"id": "wrong-fixture", "status": "fail", "evidence": ["before.json"]},
        ]
        text = self.write().read_text()
        self.assertLess(text.index("## 已知失败"), text.index("## 待核检查"))
        self.assertLess(text.index("## 待核检查"), text.index("## 本次新增"))

    def test_finding_recheck_failure_is_not_hidden_by_summary_checks(self):
        self.data["findings"][0]["recheck"] = {"status": "fail", "evidence": ["after.json"]}
        text = self.write().read_text()
        self.assertLess(text.index("## 已知失败"), text.index("## 本次新增"))
        self.assertIn("本次事项复验：overlap", text)

    def test_no_new_experience_is_one_line_with_reference_and_known_limits(self):
        self.data.update(new_experience=False, reference="review.json", findings=[])
        self.data["checks"] = [{"id": "native-reopen", "status": "pending"}]
        text = self.write().read_text()
        self.assertEqual(len(text.splitlines()), 1)
        self.assertIn("无新增经验", text)
        self.assertIn("review.json", text)
        self.assertIn("native-reopen", text)
        self.assertIn("all door clearances", text)

    def test_changed_final_file_invalidates_report_before_replacing_old_record(self):
        output = self.write()
        old = output.read_bytes()
        (self.job / "final.dwg").write_bytes(b"revised without refreshed checks")
        with self.assertRaises(module.ReportError):
            self.write(overwrite=True)
        self.assertEqual(output.read_bytes(), old)

    def test_evidence_outside_job_is_rejected(self):
        self.data["checks"][0]["evidence"] = [str(Path(__file__).resolve())]
        with self.assertRaises(module.ReportError):
            self.write()

    def test_finite_real_timing_required(self):
        for invalid in [float("nan"), float("inf"), -1, True]:
            with self.subTest(invalid=invalid):
                self.data["timing"]["active_minutes"] = invalid
                with self.assertRaises(module.ReportError):
                    self.write()

    def test_failed_atomic_replace_keeps_old_record_and_removes_temporary(self):
        output = self.write()
        old = output.read_bytes()
        with patch.object(module.os, "replace", side_effect=OSError("simulated disk failure")):
            with self.assertRaises(OSError):
                self.write(overwrite=True)
        self.assertEqual(output.read_bytes(), old)
        self.assertFalse(list(self.job.glob(".retrospective-*.tmp")))

    def test_requires_explicit_boolean_and_unique_check_ids(self):
        self.data["new_experience"] = "false"
        with self.assertRaises(module.ReportError):
            self.write()
        self.data["new_experience"] = True
        self.data["checks"] *= 2
        with self.assertRaises(module.ReportError):
            self.write()

    def test_missing_final_hash_and_malformed_status_are_rejected(self):
        del self.data["final_file"]["sha256"]
        with self.assertRaises(module.ReportError):
            self.write()
        self.data["final_file"]["sha256"] = hashlib.sha256((self.job / "final.dwg").read_bytes()).hexdigest()
        self.data["checks"][0]["status"] = ["pass"]
        with self.assertRaises(module.ReportError):
            self.write()


if __name__ == "__main__":
    unittest.main()
