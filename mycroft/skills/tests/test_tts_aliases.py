import unittest
from mycroft.client.realtime.tts_aliases import (
    apply_tts_aliases,
    register_tts_transform,
    _transform_number_commas,
    _TTS_TRANSFORMS,
)


class TestApplyTtsAliases(unittest.TestCase):

    def test_noop_when_empty(self):
        self.assertEqual(apply_tts_aliases("hello world"), "hello world")

    def test_noop_when_no_matches(self):
        self.assertEqual(
            apply_tts_aliases("hello", global_aliases={"°": " degrees"}),
            "hello",
        )

    def test_global_alias_applied(self):
        self.assertEqual(
            apply_tts_aliases("90°", global_aliases={"°": " degrees"}),
            "90 degrees",
        )

    def test_skill_alias_applied(self):
        self.assertEqual(
            apply_tts_aliases("a, b, c", skill_aliases={", ": " and "}),
            "a and b and c",
        )

    def test_skill_wins_over_global_on_same_key(self):
        # global says "," → " point", skill says "," → " and" — skill wins
        result = apply_tts_aliases(
            "a, b",
            global_aliases={", ": " point "},
            skill_aliases={", ": " and "},
        )
        self.assertEqual(result, "a and b")

    def test_global_applies_when_skill_does_not_override(self):
        result = apply_tts_aliases(
            "90° and 3.14",
            global_aliases={"°": " degrees"},
            skill_aliases={", ": " and "},
        )
        self.assertEqual(result, "90 degrees and 3.14")

    def test_multiple_global_aliases(self):
        result = apply_tts_aliases(
            "90° at 50%",
            global_aliases={"°": " degrees", "%": " percent"},
        )
        self.assertEqual(result, "90 degrees at 50 percent")

    def test_aliases_applied_sequentially(self):
        # First alias produces something the second alias also matches
        result = apply_tts_aliases(
            "A",
            global_aliases={"A": "B", "B": "C"},
        )
        # "A" → "B" (first pass), then "B" → "C" (second pass)
        self.assertEqual(result, "C")

    def test_named_transform_applied(self):
        result = apply_tts_aliases(
            "total is 11264",
            global_transforms=["number_commas"],
        )
        self.assertEqual(result, "total is 11,264")

    def test_global_transform_then_skill_transform(self):
        def upper_transform(text):
            return text.upper()
        register_tts_transform("_test_upper", upper_transform)
        result = apply_tts_aliases(
            "hello 11264",
            global_transforms=["number_commas"],
            skill_transforms=["_test_upper"],
        )
        # number_commas first: "hello 11,264", then upper: "HELLO 11,264"
        self.assertEqual(result, "HELLO 11,264")

    def test_unknown_transform_name_ignored(self):
        result = apply_tts_aliases("hello", global_transforms=["nonexistent"])
        self.assertEqual(result, "hello")


class TestNumberCommasTransform(unittest.TestCase):

    def test_four_digit_integer(self):
        self.assertEqual(_transform_number_commas("1000"), "1,000")

    def test_five_digit_integer(self):
        self.assertEqual(_transform_number_commas("11264"), "11,264")

    def test_seven_digit_integer(self):
        self.assertEqual(_transform_number_commas("1234567"), "1,234,567")

    def test_three_digit_not_changed(self):
        self.assertEqual(_transform_number_commas("999"), "999")

    def test_decimal_not_changed(self):
        self.assertEqual(_transform_number_commas("11264.5"), "11264.5")

    def test_integer_in_sentence(self):
        self.assertEqual(
            _transform_number_commas("the answer is 11264 items"),
            "the answer is 11,264 items",
        )

    def test_multiple_integers_in_sentence(self):
        self.assertEqual(
            _transform_number_commas("1000 plus 2000 equals 3000"),
            "1,000 plus 2,000 equals 3,000",
        )

    def test_decimal_adjacent_digits_not_touched(self):
        # "3.14159" — digits after decimal should not get commas
        self.assertEqual(_transform_number_commas("3.14159"), "3.14159")

    def test_negative_integer(self):
        self.assertEqual(_transform_number_commas("-11264"), "-11,264")


class TestRegisterTtsTransform(unittest.TestCase):

    def test_custom_transform_registered_and_called(self):
        def my_fn(text):
            return text.replace("cat", "dog")
        register_tts_transform("_test_cat_dog", my_fn)
        result = apply_tts_aliases("I have a cat", skill_transforms=["_test_cat_dog"])
        self.assertEqual(result, "I have a dog")

    def test_overwrite_existing_transform(self):
        register_tts_transform("_test_overwrite", lambda t: t + "A")
        register_tts_transform("_test_overwrite", lambda t: t + "B")
        result = apply_tts_aliases("x", skill_transforms=["_test_overwrite"])
        self.assertEqual(result, "xB")


if __name__ == "__main__":
    unittest.main()
