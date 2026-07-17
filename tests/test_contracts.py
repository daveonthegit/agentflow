from __future__ import annotations

import unittest

from agentflow.contracts import (
    MAX_DISCOVERIES_PER_OUTPUT,
    ContractError,
    contract_schema,
    validate_builder_report,
    validate_discoveries,
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


if __name__ == "__main__":
    unittest.main()
