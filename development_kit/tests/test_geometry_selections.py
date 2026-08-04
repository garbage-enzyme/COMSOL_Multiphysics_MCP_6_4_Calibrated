"""Tests for bounded named geometry selections without COMSOL."""

from src.tools.geometry_selections import create_box_selection, create_side_selections


class FakeGeometry:
    def __init__(self, dimension=2):
        self.dimension = dimension

    def getSDim(self):
        return self.dimension


class FakeGeometryList:
    def __init__(self, dimension=2, fail_after=None):
        self.geometry = FakeGeometry(dimension)
        self.fail_after = fail_after
        self.get_count = 0

    def tags(self):
        return ["geom1"]

    def get(self, tag):
        assert tag == "geom1"
        self.get_count += 1
        if self.fail_after is not None and self.get_count > self.fail_after:
            raise RuntimeError("injected geometry lookup failure")
        return self.geometry


class FakeSelection:
    def __init__(self, tag, fail_property=None):
        self.tag = tag
        self.fail_property = fail_property
        self.geometry = None
        self.dimension = None
        self.properties = {}

    def geom(self, geometry, dimension):
        self.geometry = geometry
        self.dimension = dimension

    def set(self, name, value):
        if name == self.fail_property:
            raise RuntimeError("injected selection property failure")
        self.properties[name] = value

    def entities(self):
        return [1, 4]


class FakeSelectionList:
    def __init__(self, fail_tag=None, existing=()):
        self.fail_tag = fail_tag
        self.items = {tag: FakeSelection(tag) for tag in existing}
        self.removed = []

    def tags(self):
        return list(self.items)

    def create(self, tag, selection_type):
        assert selection_type == "Box"
        selection = FakeSelection(tag, "condition" if tag == self.fail_tag else None)
        self.items[tag] = selection
        return selection

    def remove(self, tag):
        self.removed.append(tag)
        del self.items[tag]


class FakeComponent:
    def __init__(self, dimension=2, fail_tag=None, existing=(), fail_geometry_after=None):
        self.geometries = FakeGeometryList(dimension, fail_geometry_after)
        self.selections = FakeSelectionList(fail_tag, existing)

    def tag(self):
        return "comp1"

    def geom(self):
        return self.geometries

    def selection(self):
        return self.selections


class FakeComponentList:
    def __init__(self, component):
        self.component = component

    def tags(self):
        return ["comp1"]

    def get(self, tag):
        assert tag == "comp1"
        return self.component


class FakeJava:
    def __init__(self, component):
        self.component_node = component

    def component(self, tag=None):
        if tag is None:
            return FakeComponentList(self.component_node)
        assert tag == "comp1"
        return self.component_node


class FakeModel:
    def __init__(self, component):
        self.java = FakeJava(component)


def test_box_selection_preserves_tag_geometry_bounds_and_entities():
    component = FakeComponent()

    result = create_box_selection(
        FakeModel(component),
        selection_name="duct_left",
        x_min="-tol",
        x_max="tol",
        y_min="0",
        y_max="height",
    )

    assert result["success"] is True
    assert result["selection"]["entities"] == [1, 4]
    assert result["selection"]["entities_evaluated"] is True
    selection = component.selections.items["duct_left"]
    assert (selection.geometry, selection.dimension) == ("geom1", 1)
    assert selection.properties == {
        "xmin": "-tol",
        "xmax": "tol",
        "ymin": "0",
        "ymax": "height",
        "condition": "intersects",
    }


def test_box_selection_rejects_invalid_inputs_before_mutation():
    component = FakeComponent(existing=("taken",))
    model = FakeModel(component)

    results = [
        create_box_selection(
            model,
            selection_name="taken",
            x_min="0",
            x_max="1",
            y_min="0",
            y_max="1",
        ),
        create_box_selection(
            model,
            selection_name="new",
            x_min="0",
            x_max="1",
            y_min="0",
            y_max="1",
            z_min="0",
        ),
        create_box_selection(
            model,
            selection_name="new",
            x_min="0",
            x_max="1",
            y_min="0",
            y_max="1",
            condition="touches",
        ),
    ]

    assert all(result["success"] is False for result in results)
    assert set(component.selections.items) == {"taken"}


def test_box_selection_rolls_back_failed_property_setup():
    component = FakeComponent(fail_tag="bad_box")

    result = create_box_selection(
        FakeModel(component),
        selection_name="bad_box",
        x_min="0",
        x_max="1",
        y_min="0",
        y_max="1",
    )

    assert result == {
        "success": False,
        "error": "Box selection setup failed.",
        "rolled_back": True,
    }
    assert component.selections.items == {}


def test_side_selections_create_one_atomic_rectangular_set():
    component = FakeComponent()

    result = create_side_selections(
        FakeModel(component),
        x_min="0[m]",
        x_max="1[m]",
        y_min="0[m]",
        y_max="0.1[m]",
        prefix="duct",
        tolerance="1e-8[m]",
    )

    assert result["success"] is True
    assert result["count"] == 4
    assert set(component.selections.items) == {
        "duct_left",
        "duct_right",
        "duct_bottom",
        "duct_top",
    }
    assert component.selections.items["duct_top"].properties["ymin"] == ("(0.1[m])-(1e-8[m])")
    assert all(
        selection.properties["condition"] == "inside"
        for selection in component.selections.items.values()
    )


def test_side_selections_roll_back_every_prior_side_on_failure():
    component = FakeComponent(fail_tag="duct_bottom")

    result = create_side_selections(
        FakeModel(component),
        x_min="0",
        x_max="1",
        y_min="0",
        y_max="1",
        prefix="duct",
    )

    assert result["success"] is False
    assert result["failed_side"] == "bottom"
    assert result["rolled_back"] is True
    assert component.selections.items == {}
    assert set(component.selections.removed) == {
        "duct_left",
        "duct_right",
        "duct_bottom",
    }


def test_side_selections_roll_back_after_geometry_lookup_failure():
    component = FakeComponent(fail_geometry_after=2)

    result = create_side_selections(
        FakeModel(component),
        x_min="0",
        x_max="1",
        y_min="0",
        y_max="1",
        prefix="duct",
    )

    assert result["success"] is False
    assert result["failed_side"] == "right"
    assert result["rolled_back"] is True
    assert component.selections.items == {}


def test_side_selections_are_explicitly_two_dimensional():
    result = create_side_selections(
        FakeModel(FakeComponent(dimension=3)),
        x_min="0",
        x_max="1",
        y_min="0",
        y_max="1",
    )

    assert result["success"] is False
    assert "2D" in result["error"]
