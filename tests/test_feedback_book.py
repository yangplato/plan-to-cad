import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import feedback_book as book


class FeedbackBookTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.book = self.root/"用户纠正.md"
        env = patch.dict(os.environ,{"PLAN_TO_CAD_FEEDBACK_BOOK":str(self.book)})
        env.start()
        self.addCleanup(env.stop)
        self.note = dict(source="户型甲/交付 A", feedback="窗左右不齐",
                         condition="普通外窗接直墙", check="分别在两端对照外框与实墙面")

    def test_read_new_user_and_explicit_missing_book_are_distinct(self):
        with self.assertRaises(FileNotFoundError):
            book.read_brief()
        with patch.object(Path,"home",return_value=self.root):
            os.environ.pop("PLAN_TO_CAD_FEEDBACK_BOOK")
            path,configured=book.book_location()
            self.assertEqual(path,self.root/".config/plan-to-cad/feedback-book.md")
            self.assertFalse(configured)
            self.assertIn("暂无",book.read_brief())
            self.assertFalse(path.parent.exists())
            book.record_feedback([self.note])
            self.assertTrue(path.is_file())
            self.assertIn(self.note["check"],book.read_brief())

    def test_pointer_path_priority_and_invalid_values(self):
        with patch.object(Path,"home",return_value=self.root):
            folder=self.root/".config/plan-to-cad";folder.mkdir(parents=True)
            pointer=folder/"feedback-book-path.txt";other=self.root/"another.md"
            pointer.write_text(str(other)+"\n",encoding="utf-8")
            self.assertEqual(book.book_location(),(self.book,True))
            os.environ.pop("PLAN_TO_CAD_FEEDBACK_BOOK")
            self.assertEqual(book.book_location(),(other,True))
            for bad in ("", "relative.md", "first\nsecond"):
                pointer.write_text(bad,encoding="utf-8")
                with self.assertRaises(ValueError):
                    book.book_location()
            os.environ["PLAN_TO_CAD_FEEDBACK_BOOK"]="relative"
            with self.assertRaises(ValueError):
                book.book_location()

    def test_batch_duplicates_and_manual_change_survive_replay(self):
        first,again=book.record_feedback([self.note,self.note])
        self.assertTrue(first["added"])
        self.assertEqual(again,{"id":first["id"],"added":False})
        original=self.book.read_text(encoding="utf-8")
        edit=original.replace(self.note["check"],"用户手改的核对动作")
        self.book.write_text(edit,encoding="utf-8")
        self.assertEqual(book.record_feedback(self.note),[again])
        self.assertEqual(self.book.read_text(encoding="utf-8"),edit)
        self.assertIn("用户手改的核对动作",book.read_brief())
        self.assertNotIn(self.note["feedback"],book.read_brief())
        self.assertNotIn(self.note["source"],book.read_brief())

    def test_multiline_data_cannot_create_a_markdown_record(self):
        forged="## CAD-0000000000000000"
        note=dict(self.note,feedback="前一行\n\n"+forged+"\n适用条件\n用户说法",
                  check="第一行\n"+forged+"\n第三行")
        saved=book.record_feedback(note)
        value=self.book.read_text(encoding="utf-8")
        self.assertNotIn("\n"+forged,value)
        self.assertNotIn("\n"+forged,book.read_brief())
        records,replaced=book.decode(value)
        self.assertFalse(replaced)
        row=records[saved[0]["id"]]
        self.assertEqual(row["check"],note["check"])
        self.assertEqual(row["feedback"],note["feedback"])

    def test_same_check_different_sources_collapses_and_supersession_retains_history(self):
        a,b=book.record_feedback([self.note,dict(self.note,source="户型乙/交付 B")])
        self.assertNotEqual(a["id"],b["id"])
        self.assertEqual(book.read_brief().count(self.note["check"]),1)
        new=dict(self.note,feedback="新增后续纠正",check="改检另一处",supersedes=a["id"])
        c=book.record_feedback(new)[0]
        self.assertIn(c["id"],book.read_brief())
        self.assertNotIn(a["id"],book.read_brief())
        self.assertIn(b["id"],book.read_brief())
        self.assertIn(a["id"],self.book.read_text(encoding="utf-8"))
        self.assertFalse(book.record_feedback(new)[0]["added"])
        before=self.book.read_bytes()
        with self.assertRaises(ValueError):
            book.record_feedback(dict(new,feedback="尝试再次替代旧条"))
        self.assertEqual(before,self.book.read_bytes())

    def test_invalid_input_does_not_change_existing_file_or_partially_record_batch(self):
        book.record_feedback(self.note);before=self.book.read_bytes()
        another=dict(self.note,source="另一户")
        for bad in ([],[another,dict(self.note,check=" ")],
                    [another,dict(self.note,source="更新",supersedes="CAD-0000000000000000")]):
            with self.assertRaises(ValueError):
                book.record_feedback(bad)
            self.assertEqual(self.book.read_bytes(),before)
        self.book.write_text("# 别的项目",encoding="utf-8")
        with self.assertRaises(ValueError):
            book.record_feedback(another)
        self.assertEqual(self.book.read_text(encoding="utf-8"),"# 别的项目")

    def test_parallel_independent_processes_preserve_distinct_feedback_and_deduplicate_retry(self):
        processes=[]
        for i in range(10):
            source=self.root/f"input{i}"
            source.write_text(json.dumps(dict(self.note,source=f"户型 {i%5}")),encoding="utf-8")
            processes.append(subprocess.Popen([sys.executable,book.__file__,"record","--input",str(source)],
                                              stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True))
        results=[]
        for proc in processes:
            out,err=proc.communicate(timeout=30)
            self.assertEqual(proc.returncode,0,err)
            results.extend(json.loads(out))
        self.assertEqual(sum(row["added"] for row in results),5)
        self.assertEqual(len(book.decode(self.book.read_text(encoding="utf-8"))[0]),5)
        self.assertEqual(book.read_brief().count(self.note["check"]),1)

    def test_normal_job_snapshots_checks_before_the_first_native_operation(self):
        from run_first_plan import run
        self.book.parent.mkdir(exist_ok=True)
        book.record_feedback(self.note)
        job=self.root/"job.json"
        job.write_text(json.dumps({"scene":"scene.json","objects":"objects.json"}))
        (self.root/"scene.json").write_text('{"drafting_version":"1.6.5"}')
        (self.root/"objects.json").write_text("[]")
        destination=self.root/"second-job"
        def at_first_operation(*args):
            self.assertEqual((destination/"开工错题清单.md").read_text(),book.read_brief())
            self.assertNotIn(self.note["source"],book.read_brief())
            raise RuntimeError("native-entry-reached")
        with patch("native_environment.probe",side_effect=at_first_operation):
            with self.assertRaisesRegex(RuntimeError,"native-entry-reached"):
                run(job,destination)


if __name__=="__main__":
    unittest.main()
