"""Resolve explicit wavelength-control nodes at the ClientAPI boundary."""

from typing import Any


def evaluate_on_dataset(
    model: Any, expressions: Any, dataset_tag: str, *, unit: str | None = None
) -> Any:
    """Resolve a Java tag to an exact MPh dataset node before evaluation."""
    matches = [node for node in model / "datasets" if str(node.tag()) == dataset_tag]
    if len(matches) != 1:
        raise ValueError("strict wavelength dataset tag must identify exactly one MPh node")
    kwargs: dict[str, Any] = {"dataset": matches[0]}
    if unit is not None:
        kwargs["unit"] = unit
    return model.evaluate(expressions, **kwargs)


def resolve_nodes(
    model: Any, study_tag: str, step_tag: str, dataset_tag: str
) -> tuple[Any, Any, str]:
    java = model.java
    step = java.study(study_tag).feature(step_tag)
    dataset = java.result().dataset(dataset_tag)
    solution = str(dataset.getString("solution"))
    bound_study = str(java.sol(solution).study())
    return step, dataset, bound_study
