"""Regression coverage for silent misspelling normalization (§1)."""
import unittest

from localcodeagent.context.spelling import (
    normalize_user_text, _skeleton, _dl,
)


class TestNormalizeUserText(unittest.TestCase):
    def norm(self, text, ctx=()):
        return normalize_user_text(text, context_words=ctx)[0]

    def fixes(self, text, ctx=()):
        return normalize_user_text(text, context_words=ctx)[1]

    # --- the backlog examples ---------------------------------------------
    def test_feticcinii_becomes_fettuccine(self):
        out = self.norm("give me a recipe for crawfish feticcinii")
        self.assertIn("fettuccine", out)
        self.assertIn("crawfish", out)

    def test_fettucini_variant(self):
        self.assertIn("fettuccine", self.norm("how do I make fettucini"))

    def test_recipie(self):
        self.assertEqual(
            self.norm("whats a good recipie for bread"),
            "what's a good recipe for bread")

    def test_chiken_alfredo(self):
        out = self.norm("chiken alfredo")
        self.assertIn("chicken", out)
        self.assertIn("alfredo", out)

    def test_whats_the_wether(self):
        self.assertEqual(
            self.norm("whats the wether today"),
            "what's the weather today")

    def test_dose_before_verb(self):
        self.assertEqual(
            self.norm("how much ram dose nexus use"),
            "how much ram does nexus use")

    def test_chek_github(self):
        self.assertEqual(self.norm("chek github"), "check github")

    def test_witch_model(self):
        self.assertEqual(
            self.norm("witch model is loaded"),
            "which model is loaded")

    # --- typo classes -------------------------------------------------------
    def test_swapped_letters(self):
        self.assertIn("the", self.norm("teh answer"))

    def test_omitted_letters(self):
        self.assertIn("probably", self.norm("its probaly fine"))

    def test_repeated_letters(self):
        self.assertIn("really", self.norm("that is reallly good"))

    def test_phonetic(self):
        self.assertIn("psychology", self.norm("tell me about sykology"))

    # --- context resolution -------------------------------------------------
    def test_context_disambiguates(self):
        base = "i want some desrt"
        cooking = self.norm(base, ctx={"dessert", "chocolate", "cake"})
        travel = self.norm(base, ctx={"desert", "sand", "dunes"})
        self.assertIn("dessert", cooking)
        self.assertIn("desert", travel)

    def test_ambiguous_left_alone(self):
        # A token with no confident correction is preserved verbatim.
        out = self.norm("the zqxwv is fine")
        self.assertIn("zqxwv", out)

    # --- protected spans ------------------------------------------------------
    def test_code_untouched(self):
        out = self.norm("run `feticcinii.py --flagnn` please")
        self.assertIn("`feticcinii.py --flagnn`", out)

    def test_url_untouched(self):
        out = self.norm("open https://feticcinii.example.com/recipie")
        self.assertIn("https://feticcinii.example.com/recipie", out)

    def test_path_untouched(self):
        out = self.norm(r"edit C:\proj\recipie.txt")
        self.assertIn(r"C:\proj\recipie.txt", out)

    def test_quoted_string_untouched(self):
        out = self.norm('say "feticcinii recipie" out loud')
        self.assertIn('"feticcinii recipie"', out)

    def test_slash_command_untouched(self):
        out = self.norm("/status")
        self.assertEqual(out, "/status")

    def test_model_id_untouched(self):
        out = self.norm("load qwen3-8b please")
        self.assertIn("qwen3-8b", out)

    def test_proper_noun_mid_sentence(self):
        out = self.norm("tell Orion to check PostgreSQL")
        self.assertIn("Orion", out)
        self.assertIn("PostgreSQL", out)

    def test_domain_words_kept(self):
        out = self.norm("restart invokeai and comfyui")
        self.assertIn("invokeai", out)
        self.assertIn("comfyui", out)

    # --- real words that stay -------------------------------------------------
    def test_dose_of_medicine_kept(self):
        out = self.norm("take a dose twice a day")
        self.assertIn("dose", out)
        self.assertNotIn("does", out)

    def test_the_witch_kept(self):
        out = self.norm("the witch flew away")
        self.assertIn("witch", out)

    def test_clean_text_unchanged(self):
        text = "give me a recipe for crawfish fettuccine"
        self.assertEqual(self.norm(text), text)
        self.assertEqual(self.fixes(text), [])

    def test_morph_shield_yields_to_decisive_candidate(self):
        """'editer' looks like a valid 'edit+er' inflection — but 'editor'
        exists one edit away with the same phonetic skeleton, so the
        derived-word shield must yield to the decisive correction."""
        self.assertEqual(self.norm("witch editer do I prefer"),
                         "witch editor do I prefer")
        # Real inflections stay put — and a real word never gets
        # shortened through the shield ('waiter' is not 'water').
        for sent in ("the runner hopes it works",
                     "she talks slowly",
                     "I like bakers and makers",
                     "the waiter brought our orders quickly"):
            self.assertEqual(self.norm(sent), sent)

    def test_empty_and_none(self):
        self.assertEqual(normalize_user_text(""), ("", []))
        self.assertEqual(normalize_user_text(None), ("", []))


class TestHelpers(unittest.TestCase):
    def test_skeleton_phonetic_pairs(self):
        self.assertEqual(_skeleton("fettuccine"), _skeleton("feticcinii"))
        self.assertEqual(_skeleton("recipe"), _skeleton("recipie"))
        self.assertEqual(_skeleton("weather"), _skeleton("wether"))

    def test_dl_transposition_is_one(self):
        self.assertEqual(_dl("teh", "the"), 1)


if __name__ == "__main__":
    unittest.main()
