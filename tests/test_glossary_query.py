import json
import tempfile
import unittest
from pathlib import Path

from nlp_curriculum_term_index.glossary import (
    GlossaryIndex,
    GlossaryRepository,
    GlossaryVersion,
)
from nlp_curriculum_term_index.query import QueryService, replay_snapshot
from nlp_curriculum_term_index.repository import RecordRepository
from nlp_curriculum_term_index.snapshot import SnapshotArchive, SnapshotArchiveError
from nlp_curriculum_term_index.workflow import TextWorkflow


GLOSSARY = {
    "concepts": [
        {"concept_id": "math.fraction", "subject": "数学", "canonical_name": "分数",
         "definition": "表示整体的一部分的数"},
        {"concept_id": "chinese.fraction", "subject": "语文", "canonical_name": "分数",
         "definition": "考试或评价的得分"},
        {"concept_id": "math.ratio", "subject": "数学", "canonical_name": "比例"},
    ],
    "terms": [
        {"concept_id": "math.fraction", "surface": "分数", "term_type": "canonical"},
        {"concept_id": "chinese.fraction", "surface": "分数", "term_type": "canonical"},
        {"concept_id": "math.fraction", "surface": "分式", "term_type": "legacy",
         "textbook_versions": ["2013"]},
        {"concept_id": "math.fraction", "surface": "分数式", "term_type": "abbreviation"},
        {"concept_id": "math.ratio", "surface": "百分比", "term_type": "abbreviation"},
    ],
}


def record(record_id, text, *, subject="", textbook_version="", context="", source="synthetic"):
    return {
        "record_id": record_id,
        "source": source,
        "revision": 1,
        "observed_at": "2026-11-18T09:00:00Z",
        "text": text,
        "context": context,
        "subject": subject,
        "textbook_version": textbook_version,
    }


class GlossaryTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def repository(self):
        return RecordRepository(self.directory / "records.json")

    def glossary(self, clock="2026-01-01T00:00:00Z"):
        return GlossaryRepository(self.directory / "glossary.json", clock=clock)

    def service(self, records, glossary, clock="2026-02-01T00:00:00Z"):
        return QueryService(records, glossary, clock=clock)


class GlossaryVersionTests(GlossaryTestCase):
    def test_version_identity_is_content_addressed_and_immutable(self):
        glossary = self.glossary()
        first = glossary.publish(GLOSSARY, version_id="v1")
        original = first.to_dict()
        second = glossary.update(
            base_version="v1",
            version_id="v2",
            add_terms=[{"concept_id": "math.ratio", "surface": "比率", "term_type": "abbreviation"}],
        )
        self.assertNotEqual(first.digest, second.digest)
        self.assertEqual(glossary.get("v1").to_dict(), original)
        self.assertEqual(glossary.get("v1").digest, first.digest)

    def test_tampered_version_is_rejected(self):
        glossary = self.glossary()
        glossary.publish(GLOSSARY, version_id="v1")
        stored = json.loads((self.directory / "glossary.json").read_text(encoding="utf-8"))
        stored["versions"][0]["terms"][0]["surface"] = "被篡改"
        (self.directory / "glossary.json").write_text(
            json.dumps(stored, ensure_ascii=False), encoding="utf-8"
        )
        with self.assertRaises(ValueError):
            glossary.versions()

    def test_term_cannot_declare_a_conflicting_subject(self):
        glossary = self.glossary()
        payload = {
            "concepts": [{"concept_id": "math.fraction", "subject": "数学", "canonical_name": "分数"}],
            "terms": [{"concept_id": "math.fraction", "surface": "分数", "subject": "语文"}],
        }
        with self.assertRaises(ValueError):
            glossary.publish(payload)

    def test_future_version_does_not_change_current_queries(self):
        clock = ["2026-03-01T00:00:00Z"]
        glossary = GlossaryRepository(self.directory / "glossary.json", clock=lambda: clock[0])
        first = glossary.publish(GLOSSARY, version_id="v1")
        glossary.update(
            base_version="v1",
            version_id="v2",
            effective_at="2026-04-01T00:00:00Z",
            add_terms=[{"concept_id": "math.ratio", "surface": "比率"}],
        )
        self.assertEqual(glossary.at("2026-03-15T00:00:00Z").version_id, first.version_id)
        self.assertEqual(glossary.at("2026-05-01T00:00:00Z").version_id, "v2")
        self.assertEqual(glossary.effective().version_id, "v1")
        clock[0] = "2026-05-01T00:00:00Z"
        self.assertEqual(glossary.effective().version_id, "v2")

    def test_backdated_effective_time_cannot_rewrite_history(self):
        clock = ["2026-01-01T00:00:00Z"]
        glossary = GlossaryRepository(self.directory / "glossary.json", clock=lambda: clock[0])
        first = glossary.publish(GLOSSARY, version_id="v1", effective_at="2026-01-01T00:00:00Z")
        clock[0] = "2026-03-01T00:00:00Z"
        glossary.publish(
            {**GLOSSARY, "terms": [*GLOSSARY["terms"],
                                   {"concept_id": "math.ratio", "surface": "比率"}]},
            version_id="v2",
            effective_at="2026-02-01T00:00:00Z",
        )
        # v2 虽然声明在 2 月生效，但 3 月才发布，不能改写 3 月之前查询的含义。
        self.assertEqual(glossary.at("2026-02-15T00:00:00Z").version_id, first.version_id)
        self.assertEqual(glossary.at("2026-03-01T00:00:00Z").version_id, "v2")


class ResolutionTests(GlossaryTestCase):
    def setUp(self):
        super().setUp()
        glossary = self.glossary()
        version = glossary.publish(GLOSSARY, version_id="v1")
        self.index = GlossaryIndex(version)

    def test_cross_subject_surface_stays_ambiguous(self):
        result = self.index.resolve("分数")
        self.assertEqual(result.status, "ambiguous")
        self.assertIsNone(result.concept_id)
        self.assertEqual({concept["subject"] for concept in result.concepts}, {"数学", "语文"})
        self.assertIn("ambiguous_cross_subject", [item["code"] for item in result.uncertainties])

    def test_subject_selects_concept(self):
        self.assertEqual(self.index.resolve("分数", subject="数学").concept_id, "math.fraction")
        self.assertEqual(self.index.resolve("分数", subject="语文").concept_id, "chinese.fraction")
        self.assertEqual(self.index.resolve("分数", subject="英语").status, "undefined")

    def test_legacy_term_only_valid_for_its_textbook_version(self):
        current = self.index.resolve("分式", subject="数学", textbook_version="2024")
        self.assertEqual(current.status, "undefined")
        self.assertEqual(current.reason, "textbook_version_mismatch")
        historical = self.index.resolve("分式", subject="数学", textbook_version="2013")
        self.assertEqual(historical.concept_id, "math.fraction")
        self.assertIn("legacy_term", [item["code"] for item in historical.uncertainties])

    def test_unknown_surface_reports_no_definition(self):
        result = self.index.resolve("不存在的术语")
        self.assertEqual(result.status, "undefined")
        self.assertEqual(result.uncertainties[0]["code"], "no_valid_definition")


class SearchTests(GlossaryTestCase):
    def setUp(self):
        super().setUp()
        self.glossary_repo = self.glossary()
        self.glossary_repo.publish(GLOSSARY, version_id="v1")
        self.records = self.repository()
        self.records.replace([
            record("math-num", "学习分数加法", subject="数学", textbook_version="2024"),
            record("math-legacy", "旧教材讲分式", subject="数学", textbook_version="2013"),
            record("chinese-num", "分数公布与讲评", subject="语文", textbook_version="2024"),
            record("no-subject", "分数练习", textbook_version="2024"),
        ])

    def search(self, query, **kwargs):
        return self.service(self.records, self.glossary_repo).search(query, **kwargs)

    def test_same_surface_in_different_subjects_is_not_merged(self):
        snapshot = self.search("分数", subject="数学")
        by_id = {result["record_id"]: result for result in snapshot["results"]}
        self.assertEqual(by_id["math-num"]["concepts"][0]["concept_id"], "math.fraction")
        self.assertNotIn("chinese-num", by_id)

    def test_without_subject_results_follow_each_lessons_own_subject(self):
        snapshot = self.search("分数")
        by_id = {result["record_id"]: result for result in snapshot["results"]}
        self.assertEqual(by_id["math-num"]["concepts"][0]["concept_id"], "math.fraction")
        self.assertEqual(by_id["chinese-num"]["concepts"][0]["concept_id"], "chinese.fraction")
        codes = [item["code"] for item in snapshot["uncertainties"]]
        self.assertIn("ambiguous_cross_subject", codes)

    def test_legacy_term_matches_by_concept_and_keeps_source_version(self):
        snapshot = self.search("分数", subject="数学")
        by_id = {result["record_id"]: result for result in snapshot["results"]}
        legacy = by_id["math-legacy"]
        self.assertEqual(legacy["matched_terms"], ["分式"])
        match = legacy["matches"][0]
        self.assertEqual(match["matched_text"], "分式")
        self.assertEqual(match["record"]["textbook_version"], "2013")
        self.assertEqual(match["record"]["revision"], 1)
        self.assertEqual(match["terms"][0]["term_type"], "legacy")
        self.assertEqual(match["quote"]["text"], "分式")
        self.assertIn("legacy_term", [item["code"] for item in match["uncertainties"]])

    def test_textbook_version_filters_the_same_concept(self):
        snapshot = self.search("分式", subject="数学", textbook_version="2024")
        self.assertEqual(snapshot["results"], [])
        self.assertIn("no_valid_definition", [item["code"] for item in snapshot["uncertainties"]])

    def test_abbreviation_is_linked_to_its_concept(self):
        self.records.replace([record("abbr", "计算分数式", subject="数学", textbook_version="2024")])
        snapshot = self.search("分数", subject="数学")
        self.assertEqual(snapshot["results"][0]["matched_terms"], ["分数式"])

    def test_query_for_unknown_term_reports_uncertainty(self):
        snapshot = self.search("课程术语")
        self.assertEqual(snapshot["results"], [])
        self.assertEqual(snapshot["term_resolutions"][0]["status"], "undefined")
        self.assertIn("no_valid_definition", [item["code"] for item in snapshot["uncertainties"]])


class SnapshotTests(GlossaryTestCase):
    def setUp(self):
        super().setUp()
        self.glossary_repo = self.glossary(clock="2026-01-01T00:00:00Z")
        self.glossary_repo.publish(GLOSSARY, version_id="v1")
        self.records = self.repository()
        self.records.replace([
            record("math-num", "学习分数加法", subject="数学", textbook_version="2024"),
            record("math-legacy", "旧教材讲分式", subject="数学", textbook_version="2013"),
        ])
        self.archive = SnapshotArchive(self.directory / "snapshots.json")

    def test_delivered_snapshot_survives_glossary_update(self):
        service = self.service(self.records, self.glossary_repo, clock="2026-02-01T00:00:00Z")
        first = service.search("分数", subject="数学")
        self.archive.save(first)
        delivered = json.loads(json.dumps(first, ensure_ascii=False))

        self.glossary_repo.update(
            base_version="v1",
            version_id="v2",
            add_terms=[{"concept_id": "math.ratio", "surface": "比率"}],
        )
        self.assertEqual(self.archive.get(first["snapshot_id"]), delivered)
        replay = replay_snapshot(first)
        self.assertTrue(replay["recomputable"])
        self.assertTrue(replay["glossary_intact"])
        self.assertEqual(replay["glossary_version_id"], "v1")
        self.assertEqual(replay["results"][0]["evidence"][0]["textbook_version"], "2024")

        requery = service.search("分数", subject="数学")
        self.assertEqual(requery["glossary"]["version_id"], "v2")
        self.assertNotEqual(requery["snapshot_id"], first["snapshot_id"])

    def test_as_of_uses_the_version_effective_then(self):
        self.glossary_repo.update(
            base_version="v1",
            version_id="v2",
            effective_at="2026-04-01T00:00:00Z",
            add_terms=[{"concept_id": "math.ratio", "surface": "比率"}],
        )
        service = self.service(self.records, self.glossary_repo, clock="2026-05-01T00:00:00Z")
        historical = service.search("分数", subject="数学", as_of="2026-02-01T00:00:00Z")
        self.assertEqual(historical["glossary"]["version_id"], "v1")
        self.assertEqual(historical["glossary"]["selection"]["mode"], "as_of")
        current = service.search("分数", subject="数学")
        self.assertEqual(current["glossary"]["version_id"], "v2")

    def test_snapshot_archive_is_append_only(self):
        service = self.service(self.records, self.glossary_repo)
        snapshot = service.search("分数", subject="数学")
        self.archive.save(snapshot)
        altered = json.loads(json.dumps(snapshot, ensure_ascii=False))
        altered["results"] = []
        with self.assertRaises(SnapshotArchiveError):
            self.archive.save(altered)
        self.assertEqual(len(self.archive.all()), 1)

    def test_snapshot_archive_rejects_content_that_does_not_match_its_id(self):
        service = self.service(self.records, self.glossary_repo)
        snapshot = service.search("分数", subject="数学")
        tampered = json.loads(json.dumps(snapshot, ensure_ascii=False))
        tampered["results"][0]["matched_terms"] = ["伪造"]
        with self.assertRaises(SnapshotArchiveError):
            self.archive.save(tampered)

    def test_ingest_annotates_terms_with_concept_identity(self):
        workflow = TextWorkflow(self.records, glossary=self.glossary_repo)
        workflow.ingest([record("annotated", "旧教材讲分式", subject="数学", textbook_version="2013")])
        stored = {row["record_id"]: row for row in workflow.list_records()}["annotated"]
        mention = stored["term_mentions"][0]
        self.assertEqual(mention["concept_id"], "math.fraction")
        self.assertEqual(mention["matched_text"], "分式")
        self.assertEqual(mention["textbook_version"], "2013")
        self.assertEqual(stored["term_analysis_glossary"]["version_id"], "v1")


if __name__ == "__main__":
    unittest.main()
