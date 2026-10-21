from types import SimpleNamespace

import pytest

from patchmanager.accounts import conditions, permissions


@pytest.fixture(autouse=True)
def _object_type():
    permissions.register(
        "test.thing", "Thing", condition_attrs=frozenset({"status", "tags", "created_by"})
    )


CASES = [
    ({"attr": "status", "op": "eq", "value": "draft"}, True),
    ({"attr": "status", "op": "ne", "value": "draft"}, False),
    ({"attr": "status", "op": "in", "value": ["draft", "x"]}, True),
    ({"attr": "status", "op": "not_in", "value": ["approved"]}, True),
    ({"attr": "tags", "op": "contains", "value": "core"}, True),
    ({"attr": "created_by", "op": "is_actor"}, True),
]


@pytest.mark.parametrize(("clause", "expected"), CASES)
def test_matches(clause, expected) -> None:
    obj = SimpleNamespace(status="draft", tags=["core"], created_by_id=7)
    actor = SimpleNamespace(pk=7)
    conditions.validate([clause], "test.thing")
    assert conditions.matches([clause], obj, actor) is expected
    assert conditions.to_q([clause], actor)


@pytest.mark.parametrize(
    "bad",
    [
        "nope",
        [1],
        [{"attr": "status", "op": "regex", "value": "."}],
        [{"attr": "other", "op": "eq", "value": 1}],
        [{"attr": "status", "op": "in", "value": "x"}],
        [{"attr": "status", "op": "eq"}],
        [{"attr": "status", "op": "eq", "value": 1, "extra": 1}],
    ],
)
def test_validate_rejects(bad) -> None:
    with pytest.raises(conditions.InvalidConditionError):
        conditions.validate(bad, "test.thing")
