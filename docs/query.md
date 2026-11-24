# The filter language (`portail.query`)

One predicate language for searching a records list by what each record CARRIES. A record is a dict of entries; every
term is a test against an entry, so a class, a filename and a box label all go through the same parse and the same
comparison. `portail.query` imports nothing beyond the standard library.

## Grammar

Terms are joined by `and`; matching is case-insensitive. An `and` inside quotes is part of the value:
`dataset = "cats and dogs"` is one term.

| term | matches when |
| --- | --- |
| `class = 7` / `class != 7` | the entry equals (does not equal) the value |
| `class has 7` | ANY value of a multi-valued entry equals it |
| `class has 3, 7` | …equals ANY of the listed values (a quoted value keeps its comma) |
| `snr 5..20` | ONE value lies between both ends, inclusive (`5..` / `..20` leave an end open) |
| `file ~ train/0` / `file !~ test` | the entry contains (does not contain) the text |
| `score > 0.8` | numeric comparison (`>`, `>=`, `<`, `<=`) — numbers compare as numbers, `10 > 9` |
| `class != @pred` | compare two ENTRIES — `@name` is "the value of that entry" |
| `gt != echo = vit` | a chain: adjacent pairs; an all-`!=` chain means every pair differs |
| `1935` | a text with no operator: the record's NAME holds it (quote `"a = b"` to search for an operator) |
| `edited` | a FLAG word the app declared (below) |

A negation over a multi-valued entry means NO value matches: `class != 7` is false for boxes labelled 3 and 7.
A record missing an entry never satisfies a test about it. A query that cannot be read raises `QueryError`, whose
message says what to write.

## Using it

```python
from portail.query import entry_values, flag_pairs, parse

FLAGS = flag_pairs("edited", "changed")  # edited, unedited, changed, unchanged

query = parse("class has cat, dog and score > 0.5 and unedited", aliases={"gt": "class"}, flags=FLAGS)

for record_id, record in records.items():
    state = {"name": titles[record_id], **edit_state(record_id)}  # what the record cannot know
    if query(record, state):
        ...
```

- **Entries are duck-typed.** `entry_values(entry)` unwraps `entry.values` / `entry.labels` (several values), else
  `entry.value`, else the entry itself; a list or tuple unwraps into its items. Use the same function wherever the app
  lists the values a term will be compared against (a class menu), so the two never drift apart.
- **`numeric(text)`** is the number test every comparison uses (`None` when not a number) — public so a range slider
  decides "is this entry numeric?" by the same rule.
- **What to read.** `query.keys` names the entries the query mentions (read only these), `query.state_keys` the state
  keys its flags read, `query.names` the texts a name must hold; `query.names_only` is true when the query tests names
  and nothing else, answered without reading a record. An empty query is falsy and matches everything.
- **Aliases** map a word to the entry it stands for: with `{"gt": "class", "echo": "echo.class"}`, `gt != echo`
  compares two entries. A quoted value is always a literal, and a value inside a `has` list is always a value.

## Flags

A record cannot know what the app keeps beside it — whether the person edited it, starred it, reviewed it. The app
hands that state in as `query(record, state)` and declares the bare words that read it:

```python
parse("starred and unseen", flags={"starred": "fav", "unseen": "!seen"})
```

`flags` maps each WORD to a STATE KEY; `!` in front of the key means "the key is false". `flag_pairs(*keys,
negation="un")` writes both words of each key. With no flags declared (the default) every bare word is a name term.
Keeping state beside the record rather than inside it is what stops the app's own state leaking into the data a record
is written back as.
