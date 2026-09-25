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


class SrtImportTest(unittest.TestCase):
    GOOD_SRT = (
        "1\n00:00:01,000 --> 00:00:03,000\n海豹出现在冰面上\n\n"
        "2\n00:00:04,000 --> 00:00:06,500\nseal 海豹换气\n第二行\n\n"
        "3\n00:00:08.000 --> 00:00:10.000\n远处是冰川\n"
    )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        seed = seed_demo(self.db)
        self.project, self.version = seed["project"], seed["version"]
        self.db.assign(self.version, "alice", {"user": "bob", "role": "translator"}, "owner")

    def tearDown(self):
        self.tmp.cleanup()

    def import_srt(self, srt, revision=0, actor="bob"):
        return self.db.import_srt(self.version, actor, {"srt": srt, "expected_revision": revision})

    def test_import_srt_success_returns_count_and_new_revision(self):
        result = self.import_srt(self.GOOD_SRT)
        self.assertEqual(result["imported"], 3)
        self.assertEqual(result["cue_count"], 3)
        self.assertEqual(result["version_revision"], 1)
        cues = self.db.list_cues(self.version)
        self.assertEqual([c["cue_index"] for c in cues], [1, 2, 3])
        self.assertEqual((cues[0]["start_ms"], cues[0]["end_ms"]), (1000, 3000))
        self.assertEqual(cues[1]["text"], "seal 海豹换气\n第二行")
        self.assertEqual(cues[2]["start_ms"], 8000)

    def test_import_srt_stale_revision_rejected_and_existing_cues_untouched(self):
        self.db.save_cue(self.version, "bob", {"cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": "海豹", "expected_revision": 0})
        with self.assertRaisesRegex(DomainError, "其他成员修改"):
            self.import_srt(self.GOOD_SRT, revision=0)
        cues = self.db.list_cues(self.version)
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0]["text"], "海豹")
        with self.assertRaisesRegex(DomainError, "expected_revision"):
            self.db.import_srt(self.version, "bob", {"srt": self.GOOD_SRT})

    def test_import_srt_out_of_range_block_identified_and_nothing_saved(self):
        bad = "1\n00:00:01,000 --> 00:00:03,000\n海豹\n\n2\n00:01:59,000 --> 00:02:30,000\n越界\n"
        with self.assertRaisesRegex(DomainError, "序号 2.*超出成片时长"):
            self.import_srt(bad)
        self.assertEqual(self.db.list_cues(self.version), [])

    def test_import_srt_malformed_block_identified_and_nothing_saved(self):
        bad = "1\n00:00:01,000 --> 00:00:03,000\n海豹\n\n2\n这不是时间轴\n文本\n"
        with self.assertRaisesRegex(DomainError, "第 2 块（序号 2）：时间轴格式不合法"):
            self.import_srt(bad)
        self.assertEqual(self.db.list_cues(self.version), [])
        with self.assertRaisesRegex(DomainError, "第 1 块：序号缺失"):
            self.import_srt("00:00:01,000 --> 00:00:03,000\n没有序号\n")
        with self.assertRaisesRegex(DomainError, "SRT 内容为空"):
            self.import_srt("   \n\n")

    def test_import_srt_overlap_rejected_atomically(self):
        overlapping = "1\n00:00:01,000 --> 00:00:03,500\n海豹\n\n2\n00:00:03,000 --> 00:00:05,000\n撞上\n"
        with self.assertRaisesRegex(DomainError, "序号 2.*重叠"):
            self.import_srt(overlapping)
        self.assertEqual(self.db.list_cues(self.version), [])
        self.db.save_cue(self.version, "bob", {"cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": "海豹", "expected_revision": 0})
        with self.assertRaisesRegex(DomainError, "序号 2.*重叠"):
            self.import_srt("2\n00:00:02,000 --> 00:00:04,000\n撞上已有\n", revision=1)
        self.assertEqual(len(self.db.list_cues(self.version)), 1)

    def test_import_srt_glossary_index_conflict_and_permissions(self):
        with self.assertRaisesRegex(DomainError, "序号 1.*禁用译法"):
            self.import_srt("1\n00:00:01,000 --> 00:00:02,000\n密封装置\n")
        self.assertEqual(self.db.list_cues(self.version), [])
        self.db.save_cue(self.version, "bob", {"cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": "海豹", "expected_revision": 0})
        with self.assertRaisesRegex(DomainError, "序号 1.*冲突"):
            self.import_srt("1\n00:00:05,000 --> 00:00:06,000\n序号撞车\n", revision=1)
        with self.assertRaisesRegex(DomainError, "权限"):
            self.import_srt(self.GOOD_SRT, revision=1, actor="carol")
        self.db.submit(self.version, "bob")
        with self.assertRaisesRegex(DomainError, "只有草稿版本"):
            self.import_srt(self.GOOD_SRT, revision=1)


if __name__ == "__main__":
    unittest.main()
