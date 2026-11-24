"""The shapes a records app speaks, as structural protocols a domain package implements.

A viewer built on portail is domain-free: it knows viewports and the protocols below, and a domain package (signals,
images, …) implements them WITHOUT importing the app or portail — the app depends on the domain packages, and the
reverse import would be a cycle — so the match is purely structural (:class:`typing.Protocol`). Each protocol is
runtime-checkable: ``isinstance(source, Recordings)`` tells whether an object offers the members (it does not check
their signatures).

**Viewports are dicts** ``{"x0", "x1", "y0", "y1"}`` (:class:`Extent`) — explicit axis names, no positional convention
to get wrong between a frequency-first store and a time-first canvas.
"""

from typing import Any, Dict, List, Protocol, TypedDict, runtime_checkable


class Extent(TypedDict):
    """An axis-aligned box / viewport on a canvas: ``x`` grows right, ``y`` grows up."""

    x0: float
    x1: float
    y0: float
    y1: float


@runtime_checkable
class Canvas(Protocol):
    """What a viewer draws: tiles of any extent at any pixel size.

    ``tile`` returns an array: either ``(height, width)`` floats — a SCALAR tile the viewer colormaps (dB for a
    spectrogram) — or ``(height, width, 3)`` uint8 RGB (an image); in both, **row 0 is** ``y0`` (the lowest y — a
    viewer draws it at the bottom). Extra keyword params are forwarded from the query. Typed ``Any`` so portail needs
    no array library; a numpy ``ndarray`` is the expected answer.

    A canvas MAY also expose, for the viewer: ``params`` — ``{name: {"choices": [...], "default": v}}`` tile
    parameters a UI offers as controls; and per-axis ``axes[…]["format"]`` (``hz`` / ``s`` / ``px`` / ``number``) for
    value formatting. A canvas serving box lists MAY expose ``annotations`` (the editable list's rectangles),
    ``annotations_key`` (the record entry they came from) and ``layers`` (the record's other box lists,
    ``{entry: rectangles}``, read-only).
    """

    @property
    def extent(self) -> Extent: ...

    @property
    def axes(self) -> Dict[str, Dict[str, Any]]: ...

    def tile(self, extent: Extent, width: int, height: int, **params: Any) -> Any: ...


@runtime_checkable
class Recording(Protocol):
    """One viewable thing: an id, a title, its metadata, and a canvas.

    The canvas MUST be lazy — listing never draws, and building a canvas may read the whole recording. A recording
    MAY expose a canvas-free ``extent`` hint.
    """

    id: str
    title: str

    @property
    def metadata(self) -> Dict[str, Any]: ...

    @property
    def canvas(self) -> Canvas: ...


@runtime_checkable
class Recordings(Protocol):
    """A keyed collection of recordings; ``get`` raises ``KeyError`` for an unknown id."""

    def ids(self) -> List[str]: ...

    def get(self, recording_id: str) -> Recording: ...


@runtime_checkable
class Opener(Protocol):
    """Turns a path into workspace content — how a file or folder dropped on the app becomes a source.

    ``matches(path)`` says whether this opener understands the path (a file OR a directory); the app dispatches to
    the first match, in configured order. ``open(path, params)`` returns an **open result** dict whose keys are all
    optional:

    - ``sources``: new :class:`Recordings` collections to add to the workspace,
    - ``message``: one line for the person.

    Whatever an opener adds that must SURVIVE a workspace save is the app's to keep (a configurable object its save
    writes back).
    """

    name: str

    def matches(self, path: Any) -> bool: ...

    def open(self, path: Any, params: Dict[str, Any]) -> Dict[str, Any]: ...


__all__ = ["Canvas", "Extent", "Opener", "Recording", "Recordings"]
