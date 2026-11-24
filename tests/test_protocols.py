"""The structural protocols a domain package implements without importing portail (``portail.protocols``).

Each is runtime-checkable: an object offering the members passes ``isinstance``; one missing a member does not.
"""

from typing import Any, Dict, List

import pytest

from portail.protocols import Canvas, Extent, Opener, Recording, Recordings

WHOLE: Extent = {"x0": 0.0, "x1": 1.0, "y0": 0.0, "y1": 2.0}


class Grid:
    @property
    def extent(self) -> Extent:
        return WHOLE

    @property
    def axes(self) -> Dict[str, Dict[str, Any]]:
        return {"x": {"label": "x", "format": "number"}, "y": {"label": "y", "format": "s"}}

    def tile(self, extent: Extent, width: int, height: int, **params: Any) -> Any:
        return [[0.0] * width for _ in range(height)]


class Item:
    def __init__(self, recording_id: str) -> None:
        self.id = recording_id
        self.title = f"item {recording_id}"

    @property
    def metadata(self) -> Dict[str, Any]:
        return {"size": 1}

    @property
    def canvas(self) -> Canvas:
        return Grid()


class Items:
    def ids(self) -> List[str]:
        return ["a", "b"]

    def get(self, recording_id: str) -> Recording:
        if recording_id not in self.ids():
            raise KeyError(recording_id)
        return Item(recording_id)


class FolderOpener:
    name = "folder"

    def matches(self, path: Any) -> bool:
        return str(path).endswith("/")

    def open(self, path: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        return {"sources": [Items()], "message": f"opened {path}"}


class NoTile:
    extent = WHOLE
    axes: Dict[str, Dict[str, Any]] = {}


class NoCanvas:
    id = "a"
    title = "a"
    metadata: Dict[str, Any] = {}


class NoGet:
    def ids(self) -> List[str]:
        return []


class NoName:
    def matches(self, path: Any) -> bool:
        return False

    def open(self, path: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        return {}


@pytest.mark.parametrize(
    "protocol,conforming,missing",
    [
        (Canvas, Grid(), NoTile()),
        (Recording, Item("a"), NoCanvas()),
        (Recordings, Items(), NoGet()),
        (Opener, FolderOpener(), NoName()),
    ],
    ids=["canvas", "recording", "recordings", "opener"],
)
def test_an_object_with_every_member_conforms_and_one_missing_a_member_does_not(
    protocol: type, conforming: Any, missing: Any
) -> None:
    assert isinstance(conforming, protocol)
    assert not isinstance(missing, protocol)


def test_the_conforming_objects_work_together_as_the_protocols_describe() -> None:
    source = FolderOpener().open("/data/", {})["sources"][0]
    assert isinstance(source, Recordings)
    recording = source.get(source.ids()[0])
    assert isinstance(recording, Recording) and isinstance(recording.canvas, Canvas)
    assert len(recording.canvas.tile(recording.canvas.extent, 4, 3)) == 3
    with pytest.raises(KeyError):
        source.get("zzz")
