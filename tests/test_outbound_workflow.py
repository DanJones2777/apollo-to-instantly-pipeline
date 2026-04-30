import unittest

from outbound_workflow import (
    build_dry_run_plan,
    dedupe_preserve_order,
    filter_bounceban_deliverables,
    filter_reoon_results,
    normalize_email,
    resolve_campaign_name,
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


if __name__ == "__main__":
    unittest.main()
