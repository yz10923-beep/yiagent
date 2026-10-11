from __future__ import annotations

import pytest

from yi_agent.data.rcaeval import case_id, labels_from_names, parse_case_name


def test_names_parse_into_labels() -> None:
    c = parse_case_name("re2tt_ts-order-service_cpu_1")
    assert (c.suite, c.system, c.service, c.fault, c.rep) == (
        "re2",
        "tt",
        "ts-order-service",
        "cpu",
        1,
    )
    c = parse_case_name("re3ss_front-end_f2_3")
    assert (c.suite, c.system, c.service, c.fault, c.rep) == ("re3", "ss", "front-end", "f2", 3)
    assert c.group == ("re3", "ss", "front-end", "f2")


@pytest.mark.parametrize(
    "bad",
    ["re2tt_ts-order-service_f1_1", "re3ob_adservice_cpu_1", "re2xx_a_cpu_1", "re2tt_a_cpu", "x"],
)
def test_bad_names_are_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_case_name(bad)


def test_ids_are_stable_and_carry_nothing_of_the_name() -> None:
    name = "re2tt_ts-order-service_cpu_1"
    assert case_id(name) == case_id(name)
    i = case_id(name)
    assert len(i) == 11 and i[0] == "c" and int(i[1:], 16) >= 0
    for part in ("re2", "tt", "order", "cpu"):
        assert part not in i


def test_only_re2_and_re3_are_kept_sorted() -> None:
    labels = labels_from_names(
        ["re3ob_adservice_f3_1", "re1ob_adservice_cpu_1", "re2ss_carts_cpu_2"]
    )
    assert [c.name for c in labels] == ["re2ss_carts_cpu_2", "re3ob_adservice_f3_1"]
