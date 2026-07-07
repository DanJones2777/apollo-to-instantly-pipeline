import base64
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import outbound_workflow
from outbound_workflow import (
    append_master_row,
    build_dry_run_plan,
    clean_company_name,
    dedupe_preserve_order,
    filter_bounceban_deliverables,
    filter_reoon_results,
    normalize_email,
    resolve_campaign_name,
    upsert_log_entry,
)


class WorkflowPureFunctionTests(unittest.TestCase):
    def test_normalize_email(self):
        self.assertEqual(normalize_email("  Alex@Example.COM "), "alex@example.com")
        self.assertEqual(normalize_email(None), "")

    def test_dedupe_preserve_order(self):
        self.assertEqual(dedupe_preserve_order(["a", "b", "a", "", "c"]), ["a", "b", "c"])

    def test_filter_reoon_results_allows_safe_or_catch_all(self):
        payload = {
            "results": {
                "safe@example.com": {"is_safe_to_send": True, "is_catch_all": False},
                "catch@example.com": {"is_safe_to_send": False, "is_catch_all": True},
                "bad@example.com": {"is_safe_to_send": False, "is_catch_all": False},
            }
        }
        self.assertEqual(
            filter_reoon_results(payload, ["safe@example.com", "catch@example.com", "bad@example.com"]),
            ["safe@example.com", "catch@example.com"],
        )

    def test_filter_reoon_results_accepts_list_shape(self):
        payload = {
            "results": [
                {"email": "safe@example.com", "is_safe_to_send": True},
                {"email": "bad@example.com", "is_safe_to_send": False, "is_catch_all": False},
            ]
        }
        self.assertEqual(filter_reoon_results(payload, ["safe@example.com", "bad@example.com"]), ["safe@example.com"])

    def test_filter_bounceban_deliverables(self):
        payload = {
            "items": [
                {"email": "ok@example.com", "result": "deliverable"},
                {"email": "risk@example.com", "result": "risky"},
            ]
        }
        self.assertEqual(
            filter_bounceban_deliverables(payload, ["ok@example.com", "risk@example.com"]),
            ["ok@example.com"],
        )

    def test_resolve_campaign_name_prefers_cli(self):
        settings = {"instantly": {"campaign_name": "Settings Name"}}
        self.assertEqual(resolve_campaign_name("CLI Name", settings), "CLI Name")

    def test_resolve_campaign_name_uses_settings(self):
        settings = {"instantly": {"campaign_name": "Settings Name"}}
        self.assertEqual(resolve_campaign_name(None, settings), "Settings Name")

    def test_build_dry_run_plan_includes_approval_gate(self):
        settings = {
            "campaign_brief": {"segment": "Fintech founders"},
            "instantly": {
                "campaign_name": "Fintech Founders",
                "email_subject": "Quick question",
                "email_body": "<p>Hi {{first_name}}</p>",
            },
        }
        plan = build_dry_run_plan(
            {"type": "csv", "path": "leads.csv", "apollo_enrichment_needed": True},
            "Fintech Founders",
            settings,
        )
        self.assertTrue(plan["dry_run"])
        self.assertTrue(plan["approval_required_before_run"])
        self.assertEqual(plan["campaign"]["email_subject"], "Quick question")
        self.assertIn("Use Apollo bulk people enrichment for rows missing emails", plan["steps"])


class PipelineLogTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp.close()
        self._patcher = patch.object(outbound_workflow, "PIPELINE_LOG_PATH", Path(self._tmp.name))
        self._patcher.start()
        # Start with an empty log
        Path(self._tmp.name).write_text("[]")

    def tearDown(self):
        self._patcher.stop()
        Path(self._tmp.name).unlink(missing_ok=True)

    def test_upsert_creates_new_entry(self):
        entry = upsert_log_entry("S065", list_name="Web3 Leaders", total_exported=100)
        self.assertEqual(entry["sequence_code"], "S065")
        self.assertEqual(entry["list_name"], "Web3 Leaders")
        self.assertEqual(entry["total_exported"], 100)

    def test_upsert_updates_existing_entry(self):
        upsert_log_entry("S065", list_name="Web3 Leaders", total_exported=100)
        upsert_log_entry("S065", deliverable=80, apollo_sheet_url="https://example.com/sheet")
        log = json.loads(Path(self._tmp.name).read_text())
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["deliverable"], 80)
        self.assertEqual(log[0]["total_exported"], 100)  # preserved from first call

    def test_upsert_skips_none_values(self):
        upsert_log_entry("S065", list_name="Web3 Leaders", deliverable=None)
        log = json.loads(Path(self._tmp.name).read_text())
        self.assertNotIn("deliverable", log[0])

    def _make_master_csv_b64(self, rows: list[list[str]]) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerows(rows)
        return base64.b64encode(buf.getvalue().encode()).decode()

    def test_append_master_row_inserts_after_last_s_row(self):
        rows = [
            ["List No.", "Date Created", "List Name", "Source", "Function",
             "Geography", "Sent by what method?", "Results",
             "Contact List (Green = enriched, Orange = n/a)", "Re-cleaned by Shifu", "Notes"],
            ["S063", "Jun-2026", "HRpeople-Web3", "Apollo", "HR & People", "Global", "Instantly", "1000", "", "", ""],
            ["S064", "Jun-2026", "Founders-Web3", "Apollo", "Founders & Execs", "Global", "Instantly", "", "", "", ""],
            [],
            ["List No.", "FOR WHICH CLIENT", "Position"],  # R-table header
            ["R001", "Transferroom", "AI/ML engineer"],
        ]
        b64 = self._make_master_csv_b64(rows)
        updated = append_master_row(
            b64, "S065", "Jun-2026", "Web3 People Leaders",
            "HR & People, not Talent", "Global", "500",
            "https://docs.google.com/s/TEST", "",
        )
        lines = updated.strip().splitlines()
        # New row should appear after S064 (index 3 in lines, 0-based)
        reader = list(csv.reader(lines))
        s_codes = [r[0] for r in reader if r and r[0].startswith("S")]
        self.assertEqual(s_codes, ["S063", "S064", "S065"])
        # R-table must still be present after the new row
        r_codes = [r[0] for r in reader if r and r[0].startswith("R")]
        self.assertEqual(r_codes, ["R001"])

    def test_append_master_row_sets_source_and_method(self):
        rows = [
            ["List No.", "Date Created", "List Name", "Source", "Function",
             "Geography", "Sent by what method?", "Results",
             "Contact List (Green = enriched, Orange = n/a)", "Re-cleaned by Shifu", "Notes"],
            ["S064", "Jun-2026", "Founders", "Apollo", "Founders", "Global", "Instantly", "800", "", "", ""],
        ]
        b64 = self._make_master_csv_b64(rows)
        updated = append_master_row(b64, "S065", "Jun-2026", "My List", "HR", "UK", "300", "https://example.com", "")
        reader = list(csv.reader(updated.strip().splitlines()))
        new = next(r for r in reader if r and r[0] == "S065")
        self.assertEqual(new[3], "Apollo")       # Source always Apollo
        self.assertEqual(new[6], "Instantly")    # Method always Instantly
        self.assertEqual(new[9], "")             # Re-cleaned by Shifu always blank


class CompanyNameCleanerTests(unittest.TestCase):
    def test_strips_legal_suffixes(self):
        self.assertEqual(clean_company_name("Acme Inc"), "Acme")
        self.assertEqual(clean_company_name("Acme LLC"), "Acme")
        self.assertEqual(clean_company_name("Acme GmbH"), "Acme")
        self.assertEqual(clean_company_name("Acme Corp"), "Acme")

    def test_strips_pipe_descriptors(self):
        self.assertEqual(clean_company_name("Acme | Hiring"), "Acme")

    def test_strips_dash_descriptors(self):
        self.assertEqual(clean_company_name("Acme - We're Hiring!"), "Acme")

    def test_strips_parenthetical(self):
        self.assertEqual(clean_company_name("Acme (formerly Foo)"), "Acme")

    def test_drops_generic_tail_word(self):
        self.assertEqual(clean_company_name("Acme Technologies"), "Acme")
        self.assertEqual(clean_company_name("Acme Solutions"), "Acme")
        self.assertEqual(clean_company_name("Acme Group"), "Acme")

    def test_preserves_short_acronym(self):
        self.assertEqual(clean_company_name("IBM"), "IBM")

    def test_ai_preserved_uppercase(self):
        self.assertEqual(clean_company_name("OpenAI"), "OpenAI")
        self.assertEqual(clean_company_name("Scale AI"), "Scale AI")

    def test_allcaps_becomes_title_case(self):
        self.assertEqual(clean_company_name("ANTHROPIC"), "Anthropic")

    def test_camel_case_preserved(self):
        self.assertEqual(clean_company_name("DeepMind"), "DeepMind")

    def test_empty_returns_EMPTY(self):
        self.assertEqual(clean_company_name(""), "EMPTY")
        self.assertEqual(clean_company_name(None), "EMPTY")
        self.assertEqual(clean_company_name("   "), "EMPTY")

    def test_multi_word_with_legal_suffix(self):
        self.assertEqual(clean_company_name("Stripe Inc"), "Stripe")
        self.assertEqual(clean_company_name("Coinbase Global Inc"), "Coinbase Global")

    def test_does_not_drop_solo_tail_word(self):
        # "Group" alone should not be dropped (only when >1 word)
        self.assertEqual(clean_company_name("Group"), "Group")


if __name__ == "__main__":
    unittest.main()
