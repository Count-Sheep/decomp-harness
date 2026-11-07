"""Exercise the actual campaign adapter without a live model, compiler or shared source edits."""

from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import local_campaign as campaign
import local_campaign_investigation as investigation
from test_local_campaign_investigation import ASM


class ContextExchangeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.folder = self.root / "attempt"
        self.folder.mkdir()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(campaign, "INVESTIGATION", True))
        self.stack.enter_context(patch.object(campaign, "CONTEXT_REQUESTS", True))
        self.stack.enter_context(patch.object(campaign, "activity"))
        self.source = "int target(void) { return 0; }"
        asm = self.root / "build/TEST01/asm/test.s"
        asm.parent.mkdir(parents=True)
        asm.write_text(ASM)
        index = {"functions": {"target": {"asm_path": "build/TEST01/asm/test.s", "source": "src/test.c",
                                          "callees": ["helper"], "data": [], "insns": 5}}}
        self.session = investigation.Session(self.root, index, "target", self.source, "int target(void)",
                                             declaration=lambda name: "int helper(void);")
        self.item = {"investigation": {"available": True, "dossier": self.session.dossier()}}
        self.stack.enter_context(patch.object(campaign, "investigation_session", return_value=self.session))

    def run_exchange(self, generate):
        with patch.object(campaign, "ollama", side_effect=generate):
            return campaign.generate_with_context({}, self.item, self.source, "original C contract", self.folder,
                                                  1, "unused", "model", 30, 1024, 0.15, 10)

    def test_request_then_c_preserves_original_contract_and_records_provenance(self):
        prompts, seeds = [], []
        def generate(host, model, prompt, timeout, tokens, *args, **kwargs):
            prompts.append(prompt); seeds.append(kwargs["seed"])
            self.assertLessEqual(timeout, 30)
            campaign.LAST_TIMINGS.update(prompt_n=20, predicted_n=10, prompt_per_second=10, predicted_per_second=5)
            if len(prompts) == 1:
                return json.dumps({"context_requests": [{"kind": "callee", "symbol": "helper"}]})
            self.assertIn("int helper(void);", prompt)
            self.assertIn("original C contract", prompt)
            return "```c\nint target(void) { return helper(); }\n```"
        response, prompt = self.run_exchange(generate)
        self.assertEqual(campaign.extract_function(response, "target").strip(), "int target(void) { return helper(); }")
        self.assertEqual(seeds, [10, 11])
        self.assertEqual(self.item["context_model_calls"], 2)
        self.assertEqual(campaign.LAST_TIMINGS["predicted_n"], 20)
        self.assertEqual(campaign.LAST_TIMINGS["predicted_per_second"], 5)
        recorded = json.loads((self.folder / "attempt-001.investigation.json").read_text())
        self.assertEqual(recorded["lookup_rounds"], 1)
        self.assertEqual(recorded["records"][1]["results"][0]["declaration_sha256"], investigation.digest("int helper(void);"))
        self.assertEqual((self.folder / "attempt-001.prompt.md").read_text(), prompt)

    def test_stale_dossier_never_calls_model(self):
        self.item["investigation"]["dossier"]["fingerprint"] = "stale"
        with patch.object(campaign, "ollama") as generate, self.assertRaises(campaign.StaleSource):
            self.run_exchange(generate)
        generate.assert_not_called()

    def test_opt_out_retains_one_call_without_request_contract(self):
        with patch.object(campaign, "CONTEXT_REQUESTS", False):
            def generate(host, model, prompt, *args, **kwargs):
                self.assertNotIn("context_requests", prompt)
                return "```c\nint target(void) { return 1; }\n```"
            self.run_exchange(generate)
        self.assertEqual(self.item["context_model_calls"], 1)
        self.assertEqual(self.item["context_lookup_rounds"], 0)

    def test_malformed_request_is_retained_but_not_reinterpreted_as_c(self):
        with self.assertRaisesRegex(ValueError, "Malformed"):
            self.run_exchange(lambda *args, **kwargs: "{broken")
        recorded = json.loads((self.folder / "attempt-001.investigation.json").read_text())
        self.assertEqual(recorded["model_calls"], 1)
        self.assertEqual(recorded["records"][0]["response"], "{broken")

    def test_partial_response_keeps_legacy_artifact_on_failure(self):
        def generate(*args, **kwargs):
            kwargs["response_path"].write_text("partial C")
            raise campaign.IncompleteResponse("cut off")
        with self.assertRaises(campaign.IncompleteResponse):
            self.run_exchange(generate)
        self.assertEqual((self.folder / "attempt-001.response.md").read_text(), "partial C")

    def test_context_prompt_compaction_retries_stay_within_same_round(self):
        calls = []
        def generate(host, model, prompt, *args, **kwargs):
            calls.append(prompt)
            if len(calls) == 1:
                raise campaign.PromptTooLarge(2000, 1000)
            return "```c\nint target(void) { return 1; }\n```"
        with patch.object(campaign, "compact_prompt", return_value="original C contract compact"):
            self.run_exchange(generate)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.item["context_model_calls"], 1)


if __name__ == "__main__":
    unittest.main()
