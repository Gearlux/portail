"""Filtering records by what they CARRY — one predicate language over record entries.

A record is a dict of typed items, so every filter a records list needs is the same shape: a
test against an entry. ``class = 7`` is a label; ``class has 7`` is one of several labels on a
detection record; ``file ~ train/0`` is a filename; a FLAG word (``edited``) is state the app keeps
beside the record. One grammar covers them, which is why there is no per-attribute special case
anywhere below.

Grammar (terms joined by ``and`` — never one inside quotes; matching is case-insensitive):

    class = 7                 an entry equals a value
    class != 7                …does not
    class has 7               ANY value of a multi-valued entry equals it (a box list carries several)
    class has 3, 7            …equals ANY of the listed values (a quoted value keeps its comma)
    snr 5..20                 ONE value lies between both ends, inclusive (`5..` / `..20` leave an end open)
    file ~ train/0            the entry CONTAINS the text — all of it to the end of the term (`~ KEY=20`)
    1935                      a text with NO operator: the record's NAME holds it ("a = b" quoted for an operator)
    score > 0.8               numeric comparison (>, >=, <, <=)
    class != @pred_resnet     compare two ENTRIES — `@name` is "the value of that entry"
    edited                    a FLAG word the app declared: a state key is true (``unedited``: it is not)

``@name`` is the one piece of syntax that is not obvious, and it exists because comparing two
entries is the question a review pass is actually about ("where does the model disagree with
the label?"); without it that comparison cannot be written at all.

**Flags are the app's.** A record cannot know what the app keeps beside it (whether the person
edited it, whether that edit differs from the source), so the caller hands that state in beside
the record, and ``parse(..., flags=...)`` names the bare words that read it: a mapping of WORD to
STATE KEY, ``!`` in front of the key for "it is false" — ``{"edited": "edited", "unedited":
"!edited"}``. :func:`flag_pairs` writes both words of each key at once. With no flags declared
(the default) every bare word is a NAME term.

A bare text is the one term about no entry: the NAME is the one its list row shows, which the
caller works out and hands in ``state["name"]`` beside the flags' state, so a query of names alone
reads no record (:attr:`Filter.names_only`).
"""

import re
import shlex
from itertools import combinations
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

#: A state key written with this in front means "the key is false".
NEGATED = "!"

#: Longest first — `!=` and `>=` must not be read as `=` or `>`.
OPERATORS = ("!~", "!=", ">=", "<=", "has", "~", "=", ">", "<")

_TERM = re.compile(r"^\s*(?P<key>[\w.]+)\s*(?P<op>" + "|".join(re.escape(o) for o in OPERATORS) + r")\s*(?P<value>.*)$")


class QueryError(ValueError):
    """A filter that cannot be read — carries what to fix, not just that it failed."""


def entry_values(entry: Any) -> List[Any]:
    """Every value an entry holds — one for a scalar, several for a multi-valued item.

    A label has one, a box list has one per box, so ``has`` works on both without knowing which
    it is. Unwrapping follows the item's own shape (duck-typed): ``values``/``labels``, else
    ``value``, else the entry itself; a list or tuple unwraps into its items.

    Public because an app needs the same rule: a class menu must offer exactly the values a
    term will be compared against, and a second unwrapping rule beside this one is how the two
    silently drift apart.
    """
    for attribute in ("values", "labels"):
        held = getattr(entry, attribute, None)
        if held is not None:
            return list(held)
    single = getattr(entry, "value", entry)
    return list(single) if isinstance(single, (list, tuple)) else [single]


def numeric(text: Any) -> Optional[float]:
    """``text`` as a number, ``None`` when it is not one — the test every comparison uses to decide between a numeric
    and a text comparison. Public so an app decides "is this entry numeric?" (a range slider) by the same rule."""
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _matches(held: Any, op: str, wanted: Any) -> bool:
    """One value against one wanted value, numerically when BOTH sides are numbers."""
    left, right = numeric(str(held)), numeric(str(wanted))
    if left is not None and right is not None:
        return {
            "=": left == right,
            "!=": left != right,
            ">": left > right,
            ">=": left >= right,
            "<": left < right,
            "<=": left <= right,
            "has": left == right,
            "~": str(wanted).lower() in str(held).lower(),
            "!~": str(wanted).lower() not in str(held).lower(),
        }[op]
    a, b = str(held).lower(), str(wanted).lower()
    if op in (">", ">=", "<", "<="):
        return {">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b}[op]
    return {"=": a == b, "!=": a != b, "has": a == b, "~": b in a, "!~": b not in a}[op]


class Filter:
    """A parsed query, applied to ``(record, state)`` pairs.

    ``state`` carries what the RECORD cannot know — the app's flags (whether the person edited
    it, …) and the record's ``name``. Keeping it beside the record rather than inside it is what
    stops the app's own state leaking into the data a record is written back as.
    """

    def __init__(
        self,
        terms: Sequence[Callable[[Dict[str, Any], Dict[str, Any]], bool]],
        keys: Sequence[str],
        flags: Sequence[str] = (),
        names: Sequence[str] = (),
        state_keys: Sequence[str] = (),
    ) -> None:
        self.terms = list(terms)
        #: Flag words the query uses, as written (lower case).
        self.flags = list(flags)
        #: The state keys those flags read — a caller works out only the state they read.
        self.state_keys = list(dict.fromkeys(state_keys))
        #: Entry names the query mentions — a caller reads only these.
        self.keys = list(keys)
        #: The texts a record's NAME must hold (its bare clauses) — a caller works the names out only when asked,
        #: and hands each record's in ``state["name"]``.
        self.names = list(names)

    def __bool__(self) -> bool:
        return bool(self.terms)

    @property
    def names_only(self) -> bool:
        """Whether the query tests the records' names and nothing else — answered without reading a record."""
        return bool(self.names) and len(self.names) == len(self.terms)

    def __call__(self, record: Dict[str, Any], state: Optional[Dict[str, Any]] = None) -> bool:
        return all(term(record, state or {}) for term in self.terms)


#: A RANGE term — `<entry> <lo>..<hi>`, either end left out for open. Matched before the operator
#: grammar, which has no `..`: an entry name, white space, then something holding `..`.
_RANGE = re.compile(r"^\s*(?P<key>[\w.]+)\s+(?P<ends>[^\s=<>~!\"']*\.\.[^\s=<>~!\"']*)\s*$")


def _range_ends(ends: str, clause: str) -> Tuple[Optional[float], Optional[float]]:
    """The two ends of ``lo..hi`` — ``None`` for an end left out; refused unless both are numbers
    (or absent, not both) and ``lo <= hi``."""
    parts = ends.split("..")
    hint = f"{clause!r}: write a range as `entry lo..hi` — `lo..` or `..hi` leave an end open"
    if len(parts) != 2 or not any(parts):
        raise QueryError(hint)
    values = [None if not part else numeric(part) for part in parts]
    if any(part and value is None for part, value in zip(parts, values)):
        raise QueryError(hint)
    lo, hi = values
    if lo is not None and hi is not None and lo > hi:
        raise QueryError(f"{hint} (its low end {lo:g} lies above its high end {hi:g})")
    return lo, hi


def _range_term(key: str, lo: Optional[float], hi: Optional[float]) -> Callable[[Dict[str, Any], Dict[str, Any]], bool]:
    """``key lo..hi`` — ONE value of the entry inside both ends. Two `>=`/`<=` terms cannot say
    this of a multi-valued entry: a record holding 2 and 30 satisfies `>= 5` with one and `<= 20`
    with the other. Its reason to exist is the range sliders, which write it."""

    def term(record: Dict[str, Any], _state: Dict[str, Any]) -> bool:
        if key not in record:
            return False
        for held in entry_values(record[key]):
            value = None if isinstance(held, bool) else numeric(str(held))
            if value is not None and (lo is None or value >= lo) and (hi is None or value <= hi):
                return True
        return False

    return term


#: One clause token: a quoted string, an operator, or an operand word.
_CHAIN_TOKEN = re.compile(r"\"[^\"]*\"|'[^']*'|!=|>=|<=|!~|(?<![\w.@])has(?![\w.@])|[=~<>]|@?[\w./-]+")


def _chain_operators(clause: str) -> int:
    """How many comparison operators the clause carries OUTSIDE quotes — 1 after a text test (``~``, ``!~``).

    A text test's value is TEXT to the end of the clause, never an entry to chain on: ``file ~ KEY=20`` asks for a
    file name holding ``KEY=20``. Read as a chain it named an entry ``KEY`` no record has, and a list's walk then read
    every record in full to look for it.
    """
    operators = [token for token in _CHAIN_TOKEN.findall(clause) if token in OPERATORS]
    if operators and operators[0] in ("~", "!~"):
        return 1
    return len(operators)


def _resolve_operand(token: str, aliases: Mapping[str, str], is_last: bool, clause: str) -> "Tuple[str, str]":
    """``("entry", name)`` or ``("literal", text)`` for one chain operand.

    A QUOTED token is always a literal (a dataset may have a class literally named after a
    model); ``@x`` and an aliased name are entries; a bare word is an entry everywhere but the
    LAST position, where it is a literal — so ``echo = vit = 3`` compares two models and pins
    the value, exactly like a single comparison's right-hand side.
    """
    if token and token[0] in "\"'":
        return ("literal", token[1:-1])
    if token.startswith("@"):
        return ("entry", token[1:])
    if token in aliases:
        return ("entry", aliases[token])
    if not is_last:
        if numeric(token) is not None:
            raise QueryError(
                f"{clause!r}: {token!r} is a plain value — a chain compares from an entry, "
                "so only the LAST operand may be a literal"
            )
        return ("entry", token)
    return ("literal", token)


def _chain_terms(
    clause: str, aliases: Mapping[str, str]
) -> "Tuple[List[Callable[[Dict[str, Any], Dict[str, Any]], bool]], List[str]]":
    """Expand ``a = b = c`` into terms — adjacent pairs; an all-``!=`` chain into ALL pairs.

    Equality is transitive, so adjacent pairs carry a mixed chain (``gt != echo = vit`` is
    ``echo = vit and gt != echo``); inequality is NOT, so "all different" must test every
    pair — adjacent-only would let the first and third operand agree unnoticed.
    """
    tokens = _CHAIN_TOKEN.findall(clause)
    if not tokens or tokens[0] in OPERATORS or tokens[-1] in OPERATORS:
        raise QueryError(f"cannot read {clause!r} — a chain is `entry op entry op …`")
    operands: List[Tuple[str, str]] = []
    operators: List[str] = []
    for position, token in enumerate(tokens):
        if position % 2 == 0:
            if token in OPERATORS:
                raise QueryError(f"cannot read {clause!r} — two operators in a row")
            operands.append(_resolve_operand(token, aliases, is_last=position == len(tokens) - 1, clause=clause))
        else:
            if token not in OPERATORS:
                raise QueryError(f"cannot read {clause!r} — expected an operator, got {token!r}")
            operators.append(token)
    pairs: List[Tuple[int, int, str]]
    if len(operators) >= 2 and all(op == "!=" for op in operators):
        pairs = [(i, j, "!=") for i, j in combinations(range(len(operands)), 2)]
    else:
        pairs = [(i, i + 1, operators[i]) for i in range(len(operators))]
    terms: List[Callable[[Dict[str, Any], Dict[str, Any]], bool]] = []
    keys: List[str] = []
    for left, right, op in pairs:
        left_kind, left_name = operands[left]
        if left_kind != "entry":
            raise QueryError(f"{clause!r}: {left_name!r} is not an entry to compare from")
        right_kind, right_name = operands[right]
        keys.append(left_name)
        if right_kind == "entry":
            keys.append(right_name)
        terms.append(_entry_term(left_name, op, f"@{right_name}" if right_kind == "entry" else right_name))
    return terms, keys


def flag_pairs(*keys: str, negation: str = "un") -> Dict[str, str]:
    """Both flag words of each state key: the key itself (true) and ``negation`` + key (false).

    ``flag_pairs("edited", "changed")`` → ``{"edited": "edited", "unedited": "!edited", "changed": "changed",
    "unchanged": "!changed"}`` — the ``flags`` mapping :func:`parse` takes.
    """
    words: Dict[str, str] = {}
    for key in keys:
        words[key] = key
        words[f"{negation}{key}"] = f"{NEGATED}{key}"
    return words


def parse(text: str, aliases: Optional[Mapping[str, str]] = None, flags: Optional[Mapping[str, str]] = None) -> Filter:
    """Read a query. An empty one matches everything; a broken one raises :class:`QueryError`.

    ``aliases`` maps a WORD to the entry it stands for — an app passes ``gt`` (its label entry)
    and every model's name (that model's label entry), which is what makes
    ``gt != echo = vit`` readable sugar over ordinary entry comparisons.

    ``flags`` maps each bare FLAG word (matched case-insensitively) to the ``state`` key it reads,
    ``!key`` for "the key is false" (:func:`flag_pairs` writes both words of a key). A flag word
    is read before anything else, so a declared word is never a name term. None declared: no flags.
    """
    named = dict(aliases or {})
    declared = {word.lower(): key for word, key in (flags or {}).items()}
    for word, key in declared.items():
        if not key.removeprefix(NEGATED) or not word.strip() or word != word.strip():
            raise QueryError(f"the flag {word!r} → {key!r} is not a word naming a state key")
    terms: List[Callable[[Dict[str, Any], Dict[str, Any]], bool]] = []
    keys: List[str] = []
    used: List[str] = []
    state_keys: List[str] = []
    names: List[str] = []
    for raw in _clauses((text or "").strip()):
        clause = raw.strip()
        if not clause:
            continue
        flag = clause.lower()
        if flag in declared:
            term, state_key = _flag_term(declared[flag])
            terms.append(term)
            used.append(flag)
            state_keys.append(state_key)
            continue
        ranged = _RANGE.match(clause)
        if ranged:
            key = named.get(ranged["key"], ranged["key"])
            keys.append(key)
            terms.append(_range_term(key, *_range_ends(ranged["ends"], clause)))
            continue
        operators = _chain_operators(clause)
        if operators >= 2:
            chained, chain_keys = _chain_terms(clause, named)
            terms.extend(chained)
            keys.extend(chain_keys)
            continue
        if operators == 0:
            # No operator outside quotes: a NAME — before `_TERM`, which reads `phase_noise` as `p has e_noise`.
            whole = re.fullmatch(r"\"([^\"]*)\"|'([^']*)'", clause)
            name = next(part for part in whole.groups() if part is not None) if whole else clause
            terms.append(_name_term(name))
            names.append(name)
            continue
        found = _TERM.match(clause)
        if not found:
            either = f" (or one of: {', '.join(declared)})" if declared else ""
            raise QueryError(
                f"cannot read {clause!r} — write `entry = value`{either}. Operators: {', '.join(OPERATORS)}"
            )
        key, op, value = found["key"], found["op"], found["value"].strip()
        if not value:
            raise QueryError(f"{clause!r} has no value to compare against")
        listed = _listed_values(value, clause) if op == "has" else None
        if listed is not None:
            # A LIST is values only: a bare model name in it is a class, never an entry —
            # `@`/alias comparison against several entries at once is a question nobody asked.
            key = named.get(key, key)
            keys.append(key)
            terms.append(_any_of_term(key, listed))
            continue
        quoted = value[0] in "\"'"
        try:
            value = shlex.split(value)[0] if quoted else value
        except ValueError as exc:
            raise QueryError(f"{clause!r}: {exc}") from None
        key = named.get(key, key)
        if not quoted and value in named:  # a bare model / gt name compares ENTRIES, like @
            value = f"@{named[value]}"
        keys.append(key)
        if value.startswith("@"):  # compare against ANOTHER entry
            keys.append(value[1:])
        terms.append(_entry_term(key, op, value))
    return Filter(terms, keys, used, names, state_keys)


_AND = re.compile(r"\s+and\s+", re.IGNORECASE)


def _clauses(text: str) -> List[str]:
    """The terms of ``text``, split on each ``and`` OUTSIDE quotes — so ``label = "cats and dogs"`` stays one term.

    An ``and`` after a quote left open stays in the term, which is then refused whole.
    """
    inside = []
    quote = ""
    for char in text:
        if quote:
            quote = "" if char == quote else quote
        elif char in "\"'":
            quote = char
        inside.append(bool(quote))
    clauses: List[str] = []
    start = 0
    for joint in _AND.finditer(text):
        if not inside[joint.start()]:
            clauses.append(text[start : joint.start()])
            start = joint.end()
    clauses.append(text[start:])
    return clauses


def _listed_values(value: str, clause: str) -> Optional[List[str]]:
    """The values of ``has A, B, "C,D"`` — or None when the value holds no comma outside quotes.

    The list exists for the record filter's class menu (``+`` on several classes lists the
    records holding ANY of them); splitting only on a comma OUTSIDE quotes is what lets a class
    literally named ``A,B`` still be written, quoted. Each item is unquoted the way a single
    value is.
    """
    parts: List[str] = []
    current = ""
    quote = ""
    for char in value:
        if quote:
            quote = "" if char == quote else quote
        elif char in "\"'":
            quote = char
        elif char == ",":
            parts.append(current)
            current = ""
            continue
        current += char
    if not parts:
        return None
    parts.append(current)
    values: List[str] = []
    for part in (p.strip() for p in parts):
        if not part:
            raise QueryError(f"{clause!r}: an empty value in the list — write `has A, B`")
        if part[0] in "\"'":
            try:
                tokens = shlex.split(part)
            except ValueError as exc:
                raise QueryError(f"{clause!r}: {exc}") from None
            if len(tokens) != 1:
                raise QueryError(f"{clause!r}: {part!r} is not one quoted value")
            part = tokens[0]
        values.append(part)
    return values


def _any_of_term(key: str, values: List[str]) -> Callable[[Dict[str, Any], Dict[str, Any]], bool]:
    """``key has v1, v2, …`` — any value of the entry equals any listed value."""

    def term(record: Dict[str, Any], _state: Dict[str, Any]) -> bool:
        if key not in record:
            return False
        return any(_matches(h, "has", w) for h in entry_values(record[key]) for w in values)

    return term


def _name_term(text: str) -> Callable[[Dict[str, Any], Dict[str, Any]], bool]:
    """A bare text — the record's NAME holds it, whatever the case. The name is no entry: the caller hands it in."""
    wanted = text.lower()

    def term(_record: Dict[str, Any], state: Dict[str, Any]) -> bool:
        return wanted in str(state.get("name") or "").lower()

    return term


def _flag_term(target: str) -> Tuple[Callable[[Dict[str, Any], Dict[str, Any]], bool], str]:
    """A flag word's test — ``state[key]`` true, or false for ``!key`` — and the state key it reads."""
    negated = target.startswith(NEGATED)
    key = target[len(NEGATED) :] if negated else target

    def term(_record: Dict[str, Any], state: Dict[str, Any]) -> bool:
        return bool(state.get(key)) is not negated

    return term, key


def _entry_term(key: str, op: str, value: str) -> Callable[[Dict[str, Any], Dict[str, Any]], bool]:
    def term(record: Dict[str, Any], _state: Dict[str, Any]) -> bool:
        if key not in record:
            return False  # a record missing the entry cannot satisfy a test about it
        wanted: List[Any] = [value]
        if value.startswith("@"):
            other = value[1:]
            if other not in record:
                return False
            wanted = entry_values(record[other])
        held = entry_values(record[key])
        if op in ("!=", "!~"):
            # Negation over a multi-valued entry means NO value matches — otherwise a record
            # whose boxes hold both 3 and 7 would satisfy `class != 7`, which reads as a lie.
            positive = "=" if op == "!=" else "~"
            return not any(_matches(h, positive, w) for h in held for w in wanted)
        return any(_matches(h, op, w) for h in held for w in wanted)

    return term


__all__ = ["NEGATED", "OPERATORS", "Filter", "QueryError", "entry_values", "flag_pairs", "numeric", "parse"]
