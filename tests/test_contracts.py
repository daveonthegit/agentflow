from __future__ import annotations

import unittest

from agentflow.contracts import (
    MAX_DISCOVERIES_PER_OUTPUT,
    MAX_PROPOSALS_PER_INGEST,
    ContractError,
    contract_schema,
    proposal_work_item_id,
    validate_builder_report,
    validate_discoveries,
    validate_proposal,
    validate_review,
    validate_task_spec,
    validate_tester_report,
)


VALID_CONTENT_HASH = "a" * 64


def valid_source(**overrides):
    source = {
        "provider": "github",
        "work_item_id": "42",
        "captured_at": "2026-07-15T12:00:00+00:00",
        "content_hash": VALID_CONTENT_HASH,
    }
    source.update(overrides)
    return source


def valid_tester_report(**overrides):
    report = {
        "summary": "Probed the candidate with an added regression test.",
        "files_changed": ["tests/test_health.py"],
        "findings": [
            {
                "file": "tests/test_health.py",
                "message": "Covers the previously untested error path",
                "severity": "note",
            }
        ],
    }
    report.update(overrides)
    return report


class TesterContractTests(unittest.TestCase):
    def test_accepts_a_well_formed_report_with_empty_arrays(self) -> None:
        report = validate_tester_report(
            valid_tester_report(files_changed=[], findings=[])
        )
        self.assertEqual(report["files_changed"], [])
        self.assertEqual(report["findings"], [])

    def test_accepts_a_global_finding_with_null_file(self) -> None:
        report = valid_tester_report(
            findings=[{"file": None, "message": "global note", "severity": "minor"}]
        )
        self.assertEqual(validate_tester_report(report)["findings"][0]["file"], None)

    def test_rejects_unknown_fields(self) -> None:
        with self.assertRaisesRegex(ContractError, "summary, files_changed, findings"):
            validate_tester_report(valid_tester_report(extra=True))

    def test_rejects_empty_summary(self) -> None:
        with self.assertRaisesRegex(ContractError, "summary"):
            validate_tester_report(valid_tester_report(summary="  "))

    def test_rejects_non_string_changed_paths(self) -> None:
        with self.assertRaisesRegex(ContractError, "files_changed"):
            validate_tester_report(valid_tester_report(files_changed=["ok", 3]))

    def test_rejects_invalid_finding_severity(self) -> None:
        with self.assertRaisesRegex(ContractError, "severity"):
            validate_tester_report(
                valid_tester_report(
                    findings=[
                        {"file": None, "message": "x", "severity": "critical"}
                    ]
                )
            )

    def test_schema_matches_required_shape(self) -> None:
        schema = contract_schema("tester")
        self.assertEqual(
            sorted(schema["required"]), ["files_changed", "findings", "summary"]
        )
        self.assertFalse(schema["additionalProperties"])


def valid_discovery(**overrides):
    discovery = {
        "key": "found-cleanup",
        "summary": "Extract the duplicated retry helper",
        "acceptance_criteria": ["Retry logic lives in one module"],
        "depends_on": [],
    }
    discovery.update(overrides)
    return discovery


def valid_builder_report(**overrides):
    report = {
        "commands_run": [],
        "files_changed": [],
        "steps_completed": ["P1"],
        "unresolved_issues": [],
    }
    report.update(overrides)
    return report


class DiscoveryContractTests(unittest.TestCase):
    def test_roles_accept_capped_dedup_keyed_discoveries(self) -> None:
        discoveries = [
            valid_discovery(key=f"finding-{index}")
            for index in range(MAX_DISCOVERIES_PER_OUTPUT)
        ]
        builder = validate_builder_report(
            valid_builder_report(discoveries=discoveries)
        )
        tester = validate_tester_report(
            valid_tester_report(discoveries=discoveries)
        )
        review = validate_review(
            {
                "disposition": "approve",
                "findings": [],
                "discoveries": discoveries,
            }
        )
        for report in (builder, tester, review):
            self.assertEqual(
                [item["key"] for item in report["discoveries"]],
                [f"finding-{index}" for index in range(MAX_DISCOVERIES_PER_OUTPUT)],
            )

    def test_reports_without_discoveries_remain_valid(self) -> None:
        self.assertNotIn(
            "discoveries", validate_builder_report(valid_builder_report())
        )
        self.assertNotIn(
            "discoveries", validate_tester_report(valid_tester_report())
        )
        self.assertNotIn(
            "discoveries",
            validate_review({"disposition": "approve", "findings": []}),
        )

    def test_over_cap_discoveries_rejected_in_every_role(self) -> None:
        over_cap = [
            valid_discovery(key=f"finding-{index}")
            for index in range(MAX_DISCOVERIES_PER_OUTPUT + 1)
        ]
        with self.assertRaisesRegex(ContractError, "at most"):
            validate_builder_report(valid_builder_report(discoveries=over_cap))
        with self.assertRaisesRegex(ContractError, "at most"):
            validate_tester_report(valid_tester_report(discoveries=over_cap))
        with self.assertRaisesRegex(ContractError, "at most"):
            validate_review(
                {
                    "disposition": "approve",
                    "findings": [],
                    "discoveries": over_cap,
                }
            )

    def test_duplicate_keys_rejected(self) -> None:
        duplicated = [valid_discovery(), valid_discovery(summary="Again")]
        with self.assertRaisesRegex(ContractError, "duplicate keys"):
            validate_discoveries(duplicated)
        with self.assertRaisesRegex(ContractError, "duplicate keys"):
            validate_tester_report(valid_tester_report(discoveries=duplicated))

    def test_discovery_normalizes_and_rejects_malformed_entries(self) -> None:
        normalized = validate_discoveries(
            [valid_discovery(key=" found-cleanup ", summary=" Extract helper ")]
        )
        self.assertEqual(normalized[0]["key"], "found-cleanup")
        self.assertEqual(normalized[0]["summary"], "Extract helper")
        with self.assertRaisesRegex(ContractError, "unknown fields"):
            validate_discoveries([valid_discovery(extra=True)])
        with self.assertRaisesRegex(ContractError, "key"):
            validate_discoveries([valid_discovery(key="  ")])
        with self.assertRaisesRegex(ContractError, "depend on itself"):
            validate_discoveries([valid_discovery(depends_on=["found-cleanup"])])
        with self.assertRaisesRegex(ContractError, "must be a list"):
            validate_discoveries({"key": "not-a-list"})

    def test_schemas_declare_optional_capped_discoveries(self) -> None:
        for role in ("builder", "tester", "reviewer"):
            schema = contract_schema(role)
            discoveries = schema["properties"]["discoveries"]
            self.assertEqual(discoveries["maxItems"], MAX_DISCOVERIES_PER_OUTPUT)
            self.assertEqual(
                sorted(discoveries["items"]["required"]), ["key", "summary"]
            )
            self.assertFalse(discoveries["items"]["additionalProperties"])
            self.assertNotIn("discoveries", schema["required"])


class TaskSpecContractTests(unittest.TestCase):
    def test_accepts_legacy_summary_only_task(self) -> None:
        task = validate_task_spec({"summary": "Add a health endpoint"})
        self.assertEqual(
            task,
            {
                "summary": "Add a health endpoint",
                "acceptance_criteria": [],
            },
        )
        self.assertNotIn("source", task)

    def test_full_task_spec_round_trip(self) -> None:
        source = valid_source()
        task = validate_task_spec(
            {
                "summary": "Add a health endpoint",
                "acceptance_criteria": [" checks pass ", "docs updated"],
                "source": source,
            }
        )
        self.assertEqual(
            task,
            {
                "summary": "Add a health endpoint",
                "acceptance_criteria": ["checks pass", "docs updated"],
                "source": source,
            },
        )

    def test_rejects_empty_summary(self) -> None:
        with self.assertRaisesRegex(ContractError, "summary"):
            validate_task_spec({"summary": "   "})

    def test_rejects_blank_and_duplicate_criteria(self) -> None:
        with self.assertRaisesRegex(ContractError, "blank"):
            validate_task_spec(
                {
                    "summary": "Add a health endpoint",
                    "acceptance_criteria": ["ok", "  "],
                }
            )
        with self.assertRaisesRegex(ContractError, "duplicates"):
            validate_task_spec(
                {
                    "summary": "Add a health endpoint",
                    "acceptance_criteria": ["same", " same "],
                }
            )

    def test_rejects_unknown_fields(self) -> None:
        with self.assertRaisesRegex(ContractError, "unknown fields"):
            validate_task_spec(
                {"summary": "Add a health endpoint", "extra": True}
            )

    def test_rejects_naive_and_invalid_captured_at(self) -> None:
        with self.assertRaisesRegex(ContractError, "timezone"):
            validate_task_spec(
                {
                    "summary": "Add a health endpoint",
                    "source": valid_source(captured_at="2026-07-15T12:00:00"),
                }
            )
        with self.assertRaisesRegex(ContractError, "ISO-8601"):
            validate_task_spec(
                {
                    "summary": "Add a health endpoint",
                    "source": valid_source(captured_at="not-a-timestamp"),
                }
            )

    def test_accepts_z_and_offset_captured_at(self) -> None:
        for captured_at in (
            "2026-07-15T12:00:00Z",
            "2026-07-15T08:00:00-04:00",
        ):
            task = validate_task_spec(
                {
                    "summary": "Add a health endpoint",
                    "source": valid_source(captured_at=captured_at),
                }
            )
            self.assertEqual(task["source"]["captured_at"], captured_at)

    def test_rejects_content_hash_boundaries_without_recomputing(self) -> None:
        with self.assertRaisesRegex(ContractError, "64 lowercase hexadecimal"):
            validate_task_spec(
                {
                    "summary": "Add a health endpoint",
                    "source": valid_source(content_hash="A" * 64),
                }
            )
        with self.assertRaisesRegex(ContractError, "64 lowercase hexadecimal"):
            validate_task_spec(
                {
                    "summary": "Add a health endpoint",
                    "source": valid_source(content_hash="a" * 63),
                }
            )
        with self.assertRaisesRegex(ContractError, "64 lowercase hexadecimal"):
            validate_task_spec(
                {
                    "summary": "Add a health endpoint",
                    "source": valid_source(content_hash="g" * 64),
                }
            )
        # Importer-supplied hash is preserved, not recomputed from task.json.
        supplied = "b" * 64
        task = validate_task_spec(
            {
                "summary": "Add a health endpoint",
                "source": valid_source(content_hash=supplied),
            }
        )
        self.assertEqual(task["source"]["content_hash"], supplied)

    def test_rejects_incomplete_source(self) -> None:
        with self.assertRaisesRegex(ContractError, "exactly"):
            validate_task_spec(
                {
                    "summary": "Add a health endpoint",
                    "source": {
                        "provider": "github",
                        "work_item_id": "42",
                        "captured_at": "2026-07-15T12:00:00Z",
                    },
                }
            )


class ProposalContractTests(unittest.TestCase):
    def test_accepts_new_work_proposal_and_normalizes(self) -> None:
        proposal = validate_proposal(
            {
                "kind": "new-work",
                "summary": "  Add a health endpoint  ",
                "acceptance_criteria": ["GET /health returns 200"],
                "relates_to": ["existing-item"],
            }
        )
        self.assertEqual(proposal["kind"], "new-work")
        self.assertEqual(proposal["summary"], "Add a health endpoint")
        self.assertEqual(proposal["acceptance_criteria"], ["GET /health returns 200"])
        self.assertEqual(proposal["relates_to"], ["existing-item"])

    def test_accepts_completion_claim_without_criteria(self) -> None:
        proposal = validate_proposal(
            {
                "kind": "completion-claim",
                "summary": "health endpoint already shipped in abc123",
                "relates_to": ["health"],
            }
        )
        self.assertEqual(proposal["kind"], "completion-claim")
        self.assertEqual(proposal["acceptance_criteria"], [])
        self.assertEqual(proposal["relates_to"], ["health"])

    def test_rejects_unknown_kind(self) -> None:
        with self.assertRaisesRegex(ContractError, "kind"):
            validate_proposal({"kind": "bug", "summary": "x"})

    def test_rejects_unknown_field(self) -> None:
        with self.assertRaisesRegex(ContractError, "unknown fields"):
            validate_proposal(
                {"kind": "completion-claim", "summary": "x", "priority": "high"}
            )

    def test_rejects_missing_summary(self) -> None:
        with self.assertRaisesRegex(ContractError, "summary"):
            validate_proposal({"kind": "new-work", "acceptance_criteria": ["a"]})

    def test_new_work_requires_acceptance_criteria(self) -> None:
        with self.assertRaisesRegex(ContractError, "acceptance criterion"):
            validate_proposal({"kind": "new-work", "summary": "x"})

    def test_new_work_rejects_empty_acceptance_criteria(self) -> None:
        with self.assertRaisesRegex(ContractError, "acceptance criterion"):
            validate_proposal(
                {"kind": "new-work", "summary": "x", "acceptance_criteria": []}
            )

    def test_content_derived_id_is_stable_and_kind_summary_sensitive(self) -> None:
        first = validate_proposal(
            {"kind": "new-work", "summary": "same", "acceptance_criteria": ["a"]}
        )
        second = validate_proposal(
            {
                "kind": "new-work",
                "summary": "same",
                "acceptance_criteria": ["different criterion"],
            }
        )
        # Same kind+summary -> same id even when other fields differ.
        self.assertEqual(
            proposal_work_item_id(first), proposal_work_item_id(second)
        )
        other = validate_proposal(
            {"kind": "new-work", "summary": "changed", "acceptance_criteria": ["a"]}
        )
        self.assertNotEqual(
            proposal_work_item_id(first), proposal_work_item_id(other)
        )

    def test_ingest_cap_mirrors_discoveries_cap(self) -> None:
        self.assertEqual(MAX_PROPOSALS_PER_INGEST, MAX_DISCOVERIES_PER_OUTPUT)


if __name__ == "__main__":
    unittest.main()
