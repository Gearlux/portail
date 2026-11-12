"""The record filter (``portaide.query``) — one predicate language over record entries.

Every filter a records list needs is the same shape: a test against an entry. That is why there is no per-attribute
special case anywhere — a class, a filename and a box label all go through the same parse and the same comparison.
The entries here are duck-typed stand-ins: a :class:`Label` carries one ``value``, :class:`Boxes` carry ``labels``.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from portaide.query import NEGATED, Filter, QueryError, entry_values, flag_pairs, numeric, parse


@dataclass
class Label:
    """One value — the shape of a classification label."""

    value: Any = None
    classes: Optional[List[str]] = None


@dataclass
class Boxes:
    """Several values — the shape of a detection record's box list."""

    boxes: Sequence[Tuple[float, float, float, float]] = ()
    labels: List[Any] = field(default_factory=list)


#: The flags an annotation app declares: its three state keys, each true and negated.
FLAGS = flag_pairs("annotated", "edited", "changed")


def _classified(value: int = 7) -> Dict[str, Any]:
    return {
        "class": Label(value=value, classes=["0", "1", "2", "3", "4", "5", "6", "7"]),
        "hf_file": Label("/data/mnist_png/train/0/1.png"),
        "score": Label(0.42),
    }


def _detected(labels: List[Any]) -> Dict[str, Any]:
    return {"class": Boxes(boxes=[(0, 0, 1, 1)] * len(labels), labels=labels)}


class TestScalarEntries:
    @pytest.mark.parametrize(
        "query,expected",
        [
            ("class = 7", True),
            ("class = 3", False),
            ("class != 3", True),
            ("class != 7", False),
            ("hf_file ~ train/0", True),
            ("hf_file ~ test/9", False),
            ("hf_file !~ test/9", True),
            ("score > 0.4", True),
            ("score > 0.5", False),
            ("score <= 0.42", True),
        ],
    )
    def test_the_operators(self, query: str, expected: bool) -> None:
        assert parse(query)(_classified()) is expected

    def test_numbers_compare_as_NUMBERS_not_text(self) -> None:
        """`10 > 9` must not be decided alphabetically."""
        assert parse("class > 9")({"class": Label(value=10)}) is True

    def test_texts_compare_as_text(self) -> None:
        assert parse("name > b")({"name": Label("cat")}) is True
        assert parse("name < b")({"name": Label("cat")}) is False

    def test_an_absent_entry_never_satisfies_a_test_about_it(self) -> None:
        assert parse("missing = 1")(_classified()) is False
        assert parse("missing != 1")(_classified()) is False

    def test_a_plain_value_is_its_own_entry(self) -> None:
        """No ``value`` attribute: the entry itself is compared."""
        assert parse("count = 3")({"count": 3}) is True


class TestMultiValuedEntries:
    """A detection record carries several labels — the reason `has` exists."""

    def test_has_matches_ANY_value(self) -> None:
        assert parse("class has 7")(_detected([3, 7])) is True
        assert parse("class has 5")(_detected([3, 7])) is False

    def test_negation_means_NO_value_matches(self) -> None:
        """`class != 7` on boxes holding 3 AND 7 must be False — otherwise it reads as a lie."""
        assert parse("class != 7")(_detected([3, 7])) is False
        assert parse("class != 5")(_detected([3, 7])) is True

    def test_entry_values_unwraps_each_shape(self) -> None:
        assert entry_values(Label(7)) == [7]
        assert entry_values(Boxes(labels=[3, 7])) == [3, 7]
        assert entry_values(Label([1, 2])) == [1, 2]
        assert entry_values("text") == ["text"]


class TestHasOneOfAList:
    """`gt has A, B` — a record holding ANY of the listed values (a class menu's several picks)."""

    def test_a_comma_list_matches_a_record_holding_any_of_its_values(self) -> None:
        assert parse("class has 5, 7")(_detected([3, 7])) is True
        assert parse("class has 7, 5")(_detected([3, 7])) is True
        assert parse("class has 5, 6")(_detected([3, 7])) is False

    def test_the_space_after_the_comma_is_optional(self) -> None:
        assert parse("class has 5,7")(_detected([3, 7])) is True

    def test_a_quoted_value_with_a_comma_stays_one_value(self) -> None:
        """A class may be literally named `A,B` — quoting it keeps it whole."""
        record = {"class": Boxes(boxes=[(0, 0, 1, 1)], labels=["A,B"])}
        assert parse('class has "A,B"')(record) is True
        assert parse('class has "A,B", C')(record) is True
        assert parse("class has A")(record) is False

    def test_a_list_may_mix_quoted_and_bare_values(self) -> None:
        record = {"class": Boxes(boxes=[(0, 0, 1, 1)], labels=["two words"])}
        assert parse('class has LTE, "two words"')(record) is True

    def test_the_comma_counts_only_after_has(self) -> None:
        """`~` keeps looking for the text as written, comma included."""
        record = {"hf_file": Label("/data/train,0/1.png")}
        assert parse("hf_file ~ train,0")(record) is True
        assert parse("hf_file ~ train, 0")(record) is False

    def test_an_empty_value_in_the_list_is_refused(self) -> None:
        with pytest.raises(QueryError, match="empty value"):
            parse("class has 5,")
        with pytest.raises(QueryError, match="empty value"):
            parse("class has 5, , 7")

    def test_a_quoted_list_item_must_be_one_value(self) -> None:
        with pytest.raises(QueryError, match="not one quoted value"):
            parse('class has "a" b, c')
        with pytest.raises(QueryError, match="quotation"):
            parse('class has a, "b')

    def test_a_list_about_an_absent_entry_does_not_match(self) -> None:
        assert parse("class has 5, 7")({}) is False

    def test_a_listed_word_is_always_a_value_never_an_aliased_entry(self) -> None:
        """A bare alias alone compares ENTRIES (`gt has echo`); inside a list it is a class."""
        record = {"class": Boxes(boxes=[(0, 0, 1, 1)], labels=["echo"]), "pred": Label("LTE")}
        assert parse("class has LTE, echo", aliases={"echo": "pred"})(record) is True
        assert parse("class has LTE, echo", aliases={"echo": "pred"}).keys == ["class"]


class TestRanges:
    """`snr 5..20` — ONE value inside both ends, which two `>=`/`<=` terms cannot say of a multi-valued entry."""

    def test_one_value_must_lie_inside_both_ends(self) -> None:
        assert parse("snr 5..20")({"snr": Label(12)}) is True
        assert parse("snr 5..20")({"snr": Boxes(labels=[2, 30])}) is False
        assert parse("snr >= 5 and snr <= 20")({"snr": Boxes(labels=[2, 30])}) is True

    def test_an_end_may_be_left_open(self) -> None:
        assert parse("snr 5..")({"snr": Label(500)}) is True
        assert parse("snr ..20")({"snr": Label(-3)}) is True
        assert parse("snr ..20")({"snr": Label(21)}) is False

    def test_a_missing_entry_or_a_text_value_is_outside(self) -> None:
        assert parse("snr 5..20")({}) is False
        assert parse("snr 5..20")({"snr": Label("loud")}) is False
        assert parse("snr 0..1")({"snr": Label(True)}) is False

    @pytest.mark.parametrize("query", ["snr ..", "snr a..b", "snr 20..5", "snr 1..2..3"])
    def test_a_broken_range_is_refused(self, query: str) -> None:
        with pytest.raises(QueryError, match="lo..hi"):
            parse(query)

    def test_numeric_is_the_rule_every_comparison_uses(self) -> None:
        assert numeric("2.5") == 2.5 and numeric(3) == 3.0
        assert numeric("loud") is None and numeric(None) is None


class TestComparingTwoEntries:
    """`@name` is the value of another entry — the question a review pass is about."""

    def test_entries_that_differ(self) -> None:
        record = {"class": Label(value=7), "pred": Label(value=3)}
        assert parse("class != @pred")(record) is True
        assert parse("class = @pred")(record) is False

    def test_entries_that_agree(self) -> None:
        assert parse("class = @pred")({"class": Label(value=7), "pred": Label(value=7)}) is True

    def test_both_entries_are_reported_as_keys_to_read(self) -> None:
        """A caller reads only the entries the query mentions — both sides count."""
        assert set(parse("class != @pred").keys) == {"class", "pred"}

    def test_a_missing_other_entry_does_not_match(self) -> None:
        assert parse("class != @pred")({"class": Label(value=7)}) is False


class TestNames:
    """A bare text tests the record's NAME, which the caller hands in ``state["name"]``."""

    def test_a_bare_text_is_held_by_the_name(self) -> None:
        query = parse("1935")
        assert query({}, {"name": "capture at 1935 MHz"}) is True
        assert query({}, {"name": "capture at 2450 MHz"}) is False
        assert query({}) is False

    def test_a_query_of_names_alone_reads_no_record(self) -> None:
        assert parse("1935 and Capture").names_only is True
        assert parse("1935 and class = 7").names_only is False
        assert parse('"a = b"').names == ["a = b"]


class TestFlags:
    """Flags are the APP's: declared as words mapped to state keys, kept beside the record so they never reach it."""

    @pytest.mark.parametrize("flag", list(FLAGS))
    def test_every_declared_flag_parses(self, flag: str) -> None:
        assert parse(flag, flags=FLAGS)({}, {"annotated": True, "changed": True}) in (True, False)

    def test_annotated_and_changed_are_different_questions(self) -> None:
        """Assigning the class it already had is annotated but NOT changed."""
        state = {"annotated": True, "changed": False}
        assert parse("annotated", flags=FLAGS)({}, state) is True
        assert parse("changed", flags=FLAGS)({}, state) is False
        assert parse("unchanged", flags=FLAGS)({}, state) is True

    def test_flag_pairs_writes_both_words_of_each_key(self) -> None:
        assert flag_pairs("edited") == {"edited": "edited", "unedited": f"{NEGATED}edited"}
        assert flag_pairs("open", negation="not_") == {"open": "open", "not_open": "!open"}

    def test_no_flags_by_default_a_flag_word_is_a_name(self) -> None:
        query = parse("edited")
        assert query.flags == [] and query.names == ["edited"]
        assert query({}, {"edited": True, "name": "r1"}) is False

    def test_an_app_maps_its_own_words_to_its_own_keys(self) -> None:
        flags = {"starred": "fav", "plain": "!fav", "Reviewed": "seen"}
        assert parse("starred", flags=flags)({}, {"fav": 1}) is True
        assert parse("plain", flags=flags)({}, {"fav": 1}) is False
        assert parse("plain", flags=flags)({}, {}) is True
        assert parse("REVIEWED and starred", flags=flags)({}, {"seen": True, "fav": True}) is True

    def test_the_query_reports_the_words_and_the_state_keys_it_reads(self) -> None:
        query = parse("unedited and edited and class = 7", flags=FLAGS)
        assert query.flags == ["unedited", "edited"]
        assert query.state_keys == ["edited"]
        assert query.keys == ["class"]

    def test_a_flag_reads_no_entries(self) -> None:
        assert parse("annotated", flags=FLAGS).keys == []

    @pytest.mark.parametrize("flags", [{"x": "!"}, {"x": ""}, {"": "y"}, {" x": "y"}])
    def test_a_flag_naming_no_state_key_is_refused(self, flags: Dict[str, str]) -> None:
        with pytest.raises(QueryError, match="state key"):
            parse("", flags=flags)

    def test_the_broken_query_message_lists_the_declared_flags(self) -> None:
        with pytest.raises(QueryError, match=r"\(or one of: edited, unedited\)"):
            parse("= 7", flags=flag_pairs("edited"))


class TestCombiningAndRefusing:
    def test_terms_are_joined_by_and(self) -> None:
        assert parse("class = 7 and hf_file ~ train")(_classified()) is True
        assert parse("class = 7 and hf_file ~ test")(_classified()) is False

    def test_an_empty_query_matches_everything(self) -> None:
        assert bool(parse("")) is False and parse("")(_classified()) is True
        assert isinstance(parse(""), Filter)

    def test_a_broken_query_says_what_to_write(self) -> None:
        """A bare word is a NAME; an operator with no entry is broken."""
        with pytest.raises(QueryError, match="entry = value"):
            parse("= 7")

    def test_a_term_with_no_value_is_refused(self) -> None:
        with pytest.raises(QueryError, match="no value"):
            parse("class =")

    def test_an_unclosed_quote_is_refused(self) -> None:
        with pytest.raises(QueryError, match="quotation"):
            parse('class = "abc')

    def test_quoted_values_keep_their_spaces(self) -> None:
        assert parse('hf_file ~ "mnist_png/train"')(_classified()) is True

    def test_the_query_reports_which_entries_it_reads(self) -> None:
        """The caller restricts its walk to these — that is what keeps a filter affordable."""
        assert set(parse("class = 7 and hf_file ~ x").keys) == {"class", "hf_file"}


class TestChainedComparisons:
    """``a = b = c`` — one term per ADJACENT pair; an all-``!=`` chain means ALL pairs differ.

    With aliases mapping ``gt`` to the label entry and each model name to its label entry, ``gt = echo = vit`` reads
    exactly as it acts. A chain of n names is n-1 terms (equality is transitive), and only the all-different chain
    pays all n(n-1)/2 pairs, because adjacent inequality would let the first and third agree unnoticed.
    """

    ALIASES = {"gt": "class", "echo": "echo.class", "vit": "vit.class"}

    def _record(self, gt: int, echo: int, vit: int) -> Dict[str, Any]:
        return {"class": Label(value=gt), "echo.class": Label(value=echo), "vit.class": Label(value=vit)}

    def test_all_agree(self) -> None:
        query = parse("gt = echo = vit", aliases=self.ALIASES)
        assert query(self._record(7, 7, 7)) is True
        assert query(self._record(7, 7, 3)) is False

    def test_models_agree_but_gt_differs(self) -> None:
        query = parse("gt != echo = vit", aliases=self.ALIASES)
        assert query(self._record(7, 3, 3)) is True
        assert query(self._record(7, 7, 3)) is False, "the models must agree with EACH OTHER"
        assert query(self._record(7, 7, 7)) is False

    def test_an_all_different_chain_means_pairWISE_distinct(self) -> None:
        query = parse("gt != echo != vit", aliases=self.ALIASES)
        assert query(self._record(1, 2, 3)) is True
        assert query(self._record(1, 2, 1)) is False, "adjacent-only would let gt and vit agree"

    def test_a_pair_of_models_without_gt(self) -> None:
        query = parse("echo != vit", aliases=self.ALIASES)
        assert query(self._record(0, 1, 2)) is True and query(self._record(0, 5, 5)) is False

    def test_a_chain_composes_with_ordinary_terms(self) -> None:
        record = self._record(7, 3, 3)
        record["echo.score"] = Label(0.9)
        query = parse("gt != echo = vit and echo.score > 0.5", aliases=self.ALIASES)
        assert query(record) is True

    def test_a_chain_ending_in_a_literal(self) -> None:
        """The last operand may be a plain value: everyone predicted 3."""
        query = parse("echo = vit = 3", aliases=self.ALIASES)
        assert query(self._record(7, 3, 3)) is True and query(self._record(7, 4, 4)) is False

    def test_a_chain_of_explicit_entries_and_a_quoted_literal(self) -> None:
        query = parse('@echo.class = vit.class = "3"')
        assert query(self._record(7, 3, 3)) is True

    def test_aliases_never_shadow_a_quoted_literal(self) -> None:
        """A dataset whose class is literally named "echo" stays reachable."""
        record = {"class": Label(value="echo")}
        assert parse('class = "echo"', aliases=self.ALIASES)(record) is True

    def test_an_unaliased_bare_word_in_value_position_stays_a_literal(self) -> None:
        assert parse("class = cat", aliases=self.ALIASES)({"class": Label(value="cat")}) is True

    def test_chain_keys_reach_the_projection_list(self) -> None:
        query = parse("gt != echo = vit", aliases=self.ALIASES)
        assert set(query.keys) >= {"class", "echo.class", "vit.class"}

    def test_without_aliases_nothing_changes(self) -> None:
        assert parse("class = 7")({"class": Label(value=7)}) is True

    def test_a_literal_leading_a_chain_is_refused(self) -> None:
        with pytest.raises(QueryError, match="entry"):
            parse("7 = gt = echo", aliases=self.ALIASES)

    @pytest.mark.parametrize(
        "query,match",
        [
            ("= a = b", "a chain is"),
            ("a = = b = c", "two operators in a row"),
            ("a = b c = d", "expected an operator"),
            ('"x" = a = b', "not an entry to compare from"),
        ],
    )
    def test_a_broken_chain_says_what_is_wrong(self, query: str, match: str) -> None:
        with pytest.raises(QueryError, match=match):
            parse(query)
