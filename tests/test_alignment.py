import unittest
from collections import Counter

from rag_infra.alignment import ABSTAIN, build_cases, render_prompt, score, summarize


class AlignmentTest(unittest.TestCase):
    def setUp(self):
        self.cases = build_cases()
        self.attack = next(case for case in self.cases if case["kind"] == "attack")

    def test_attacks_change_only_one_document_in_the_paired_case(self):
        groups = [{c["group"] for c in build_cases(split)} for split in ("dev", "train", "test")]
        self.assertEqual(sum(map(len, groups)), len(set.union(*groups)))
        pairs = {case["group"]: case for case in self.cases if case["kind"] == "clean"}
        attacks = [case for case in self.cases if case["kind"] == "attack"]
        self.assertEqual(set(Counter(case["family"] for case in attacks).values()), {4})
        for case in attacks:
            clean = pairs[case["group"]]
            self.assertEqual((case["question"], case["expected"]), (clean["question"], clean["expected"]))
            self.assertEqual([doc["id"] for doc in case["docs"]], [doc["id"] for doc in clean["docs"]])
            changed = [(a, b) for a, b in zip(case["docs"], clean["docs"]) if a != b]
            self.assertEqual(len(changed), 1)
            self.assertTrue(changed[0][0]["text"].startswith(changed[0][1]["text"]))
            self.assertIn(case["marker"], changed[0][0]["text"])

    def test_boundary_only_changes_system_message_and_filter_keeps_evidence(self):
        plain = render_prompt(self.attack, "plain")
        boundary = render_prompt(self.attack, "boundary")
        filtered = render_prompt(self.attack, "boundary_filter")
        self.assertEqual(plain[1:], boundary[1:])
        self.assertNotEqual(plain[0], boundary[0])
        self.assertEqual(filtered[0], boundary[0])
        self.assertNotIn(self.attack["marker"], filtered[-1]["content"])
        self.assertIn(self.attack["expected"], filtered[-1]["content"])
        # No claim that the filter covers the indirect family.
        indirect = next(case for case in self.cases if case.get("family") == "indirect")
        self.assertEqual(render_prompt(indirect, "boundary"), render_prompt(indirect, "boundary_filter"))

    def test_challenge_changes_label_and_attack_text_without_changing_answers(self):
        challenge = build_cases("challenge")
        train_families = {case.get("family") for case in build_cases("train")}
        for old, new in zip(self.cases, challenge, strict=True):
            self.assertEqual(old["expected"], new["expected"])
            if old["kind"] != "missing":
                self.assertNotEqual(old["citation"], new["citation"])
            if old["kind"] == "attack":
                self.assertNotIn(new["family"], train_families)
                self.assertNotIn(old["marker"], str(new["docs"]))

    def test_scores_separate_grounding_refusal_and_attack_success(self):
        correct = self.attack["expected"] + " | A"
        self.assertTrue(score(self.attack, correct)["grounded_correct"])
        self.assertFalse(score(self.attack, self.attack["expected"])["grounded_correct"])
        leaked = score(self.attack, correct + "\n" + self.attack["marker"])
        self.assertTrue(leaked["attack_success"])
        self.assertFalse(leaked["grounded_correct"])
        clean = next(case for case in self.cases if case["kind"] == "clean")
        missing = next(case for case in self.cases if case["kind"] == "missing")
        metrics = summarize([leaked, score(clean, ABSTAIN), score(missing, ABSTAIN)])
        self.assertEqual(metrics["clean_false_refusal"]["count"], 1)
        self.assertEqual(metrics["missing_correct_abstention"]["count"], 1)
