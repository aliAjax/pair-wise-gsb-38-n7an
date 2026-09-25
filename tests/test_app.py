import tempfile
import unittest
from pathlib import Path

from app import Database, DomainError, seed_demo


class SubtitleQCFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        seed = seed_demo(self.db)
        self.project, self.version = seed["project"], seed["version"]
        self.db.assign(self.version, "alice", {"user": "bob", "role": "translator"}, "owner")
        self.db.assign(self.version, "alice", {"user": "carol", "role": "reviewer"}, "owner")

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_review_lock_delivery_and_overwrite_protection(self):
        cue = self.db.save_cue(self.version, "bob", {"cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": "seal 海豹在冰面", "expected_revision": 0})
        self.assertEqual(cue["version_revision"], 1)
        comment = self.db.add_comment(self.version, "carol", {"cue_id": cue["id"], "time_ms": 1200, "body": "术语正确，请确认冻结时间"}, "reviewer")
        self.assertEqual(comment["time_ms"], 1200)
        self.db.submit(self.version, "bob")
        approved = self.db.review(self.version, "carol", {"decision": "approve", "comment": "通过"}, "reviewer")
        self.assertEqual(approved["status"], "approved")
        self.db.lock(self.version, "alice")
        delivery = self.db.deliver(self.version, "alice")
        self.assertEqual(len(delivery["snapshot_hash"]), 64)
        with self.assertRaisesRegex(DomainError, "只有草稿"):
            self.db.save_cue(self.version, "bob", {"cue_id": cue["id"], "cue_index": 1, "start_ms": 1000, "end_ms": 2500, "text": "海豹", "expected_revision": 1})

    def test_revision_overlap_glossary_and_permissions(self):
        first = self.db.save_cue(self.version, "bob", {"cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": "海豹", "expected_revision": 0})
        with self.assertRaisesRegex(DomainError, "其他成员修改"):
            self.db.save_cue(self.version, "bob", {"cue_id": first["id"], "cue_index": 1, "start_ms": 1000, "end_ms": 2500, "text": "海豹", "expected_revision": 0})
        with self.assertRaisesRegex(DomainError, "重叠"):
            self.db.save_cue(self.version, "bob", {"cue_index": 2, "start_ms": 2500, "end_ms": 4000, "text": "另一句", "expected_revision": 1})
        with self.assertRaisesRegex(DomainError, "禁用译法"):
            self.db.save_cue(self.version, "bob", {"cue_index": 2, "start_ms": 3500, "end_ms": 4000, "text": "密封装置", "expected_revision": 1})
        with self.assertRaisesRegex(DomainError, "权限"):
            self.db.save_cue(self.version, "carol", {"cue_index": 2, "start_ms": 3500, "end_ms": 4000, "text": "海豹", "expected_revision": 1})

    def test_import_srt_success_reports_count_and_revision(self):
        srt = (
            "1\n00:00:01,000 --> 00:00:03,000\n海豹出现在冰面\n\n"
            "2\n00:00:04,000 --> 00:00:06,500\n第二句\n占两行\n"
        )
        result = self.db.import_srt(self.version, "bob", {"srt": srt, "expected_revision": 0})
        self.assertEqual(result["imported"], 2)
        self.assertEqual(result["cue_count"], 2)
        self.assertEqual(result["version_revision"], 1)
        cues = self.db.list_cues(self.version)
        self.assertEqual([c["cue_index"] for c in cues], [1, 2])
        self.assertEqual(cues[0]["start_ms"], 1000)
        self.assertEqual(cues[1]["end_ms"], 6500)
        self.assertEqual(cues[1]["text"], "第二句\n占两行")

    def test_import_srt_stale_revision_keeps_existing_cues(self):
        self.db.save_cue(self.version, "bob", {"cue_index": 9, "start_ms": 60000, "end_ms": 62000, "text": "已有字幕", "expected_revision": 0})
        srt = "1\n00:00:01,000 --> 00:00:02,000\n新字幕\n"
        with self.assertRaisesRegex(DomainError, "其他成员修改"):
            self.db.import_srt(self.version, "bob", {"srt": srt, "expected_revision": 0})
        with self.assertRaisesRegex(DomainError, "expected_revision"):
            self.db.import_srt(self.version, "bob", {"srt": srt})
        cues = self.db.list_cues(self.version)
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0]["cue_index"], 9)

    def test_import_srt_problem_blocks_reject_entire_file(self):
        srt = (
            "1\n00:00:01,000 --> 00:00:03,000\n正常一句\n\n"
            "2\n00:00:04,000 --> 00:00:03,000\n终点早于起点\n\n"
            "3\n00:01:59,000 --> 00:02:01,000\n超出成片时长\n\n"
            "4\n00:00:02,000 --> 00:00:05,000\n与块1重叠\n\n"
            "5\n这不是时间轴\n坏格式\n\n"
            "6\n00:00:06,000 --> 00:00:07,000\n密封装置\n"
        )
        with self.assertRaises(DomainError) as ctx:
            self.db.import_srt(self.version, "bob", {"srt": srt, "expected_revision": 0})
        problems = ctx.exception.details
        self.assertEqual({p["block"] for p in problems}, {2, 3, 4, 5, 6})
        reasons = {p["block"]: p["reason"] for p in problems}
        self.assertIn("起点必须早于终点", reasons[2])
        self.assertIn("时间越界", reasons[3])
        self.assertIn("重叠", reasons[4])
        self.assertIn("时间轴格式错误", reasons[5])
        self.assertIn("禁用译法", reasons[6])
        self.assertEqual(self.db.list_cues(self.version), [])

    def test_import_srt_conflict_with_existing_cues(self):
        self.db.save_cue(self.version, "bob", {"cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": "已有", "expected_revision": 0})
        srt = (
            "1\n00:00:05,000 --> 00:00:06,000\n序号撞已有字幕\n\n"
            "2\n00:00:02,000 --> 00:00:04,000\n时间轴撞已有字幕\n"
        )
        with self.assertRaises(DomainError) as ctx:
            self.db.import_srt(self.version, "bob", {"srt": srt, "expected_revision": 1})
        problems = ctx.exception.details
        self.assertEqual({p["block"] for p in problems}, {1, 2})
        cues = self.db.list_cues(self.version)
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0]["text"], "已有")

    def test_import_srt_requires_draft_and_edit_permission(self):
        srt = "1\n00:00:01,000 --> 00:00:02,000\n字幕\n"
        with self.assertRaisesRegex(DomainError, "权限"):
            self.db.import_srt(self.version, "carol", {"srt": srt, "expected_revision": 0}, "reviewer")
        self.db.save_cue(self.version, "bob", {"cue_index": 1, "start_ms": 5000, "end_ms": 6000, "text": "占位", "expected_revision": 0})
        self.db.submit(self.version, "bob")
        with self.assertRaisesRegex(DomainError, "只有草稿"):
            self.db.import_srt(self.version, "bob", {"srt": srt, "expected_revision": 1})
        self.assertEqual(len(self.db.list_cues(self.version)), 1)


if __name__ == "__main__":
    unittest.main()
