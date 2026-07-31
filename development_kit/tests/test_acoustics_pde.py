"""Tests for constrained Acoustics and PDE tools without COMSOL."""

from src.tools.acoustics_pde import (
    add_pde_interface,
    add_pressure_acoustics_interface,
    configure_boundaries,
)


class FakeSelection:
    def __init__(self, fail=False):
        self.fail = fail
        self.entities = None
        self.named_tag = None

    def set(self, values):
        if self.fail:
            raise RuntimeError("injected selection failure")
        self.entities = list(values)

    def named(self, tag):
        if self.fail:
            raise RuntimeError("injected named-selection failure")
        self.named_tag = tag


class FakeFeature:
    def __init__(self, tag, feature_type, fail_property=None):
        self._tag = tag
        self.feature_type = feature_type
        self.selection_node = FakeSelection()
        self.properties = {}
        self.fail_property = fail_property
        self.feature_label = None

    def tag(self):
        return self._tag

    def selection(self):
        return self.selection_node

    def set(self, name, value):
        if name == self.fail_property:
            raise RuntimeError("injected property failure")
        self.properties[name] = value

    def label(self, value=None):
        if value is not None:
            self.feature_label = value
        return self.feature_label or self.feature_type


class FakeFeatureList:
    def __init__(self, defaults=None, fail_type=None):
        self.items = dict(defaults or {})
        self.fail_type = fail_type
        self.removed = []

    def tags(self):
        return list(self.items)

    def get(self, tag):
        return self.items[tag]

    def create(self, tag, feature_type, dimension):
        feature = FakeFeature(
            tag,
            feature_type,
            "Zn" if feature_type == self.fail_type else None,
        )
        feature.dimension = dimension
        self.items[tag] = feature
        return feature

    def remove(self, tag):
        self.removed.append(tag)
        del self.items[tag]


class FakePhysics:
    def __init__(self, tag, physics_type, third_argument, fail_boundary_type=None):
        self._tag = tag
        self.physics_type = physics_type
        self.third_argument = third_argument
        self.selection_node = FakeSelection()
        defaults = {}
        if physics_type == "CoefficientFormPDE":
            defaults["cfeq1"] = FakeFeature("cfeq1", "Equation")
        elif physics_type == "GeneralFormPDE":
            defaults["gfeq1"] = FakeFeature("gfeq1", "Equation")
        elif physics_type == "WeakFormPDE":
            defaults["wfeq1"] = FakeFeature("wfeq1", "Equation")
        self.features = FakeFeatureList(defaults, fail_boundary_type)

    def tag(self):
        return self._tag

    def label(self):
        return self.physics_type

    def selection(self):
        return self.selection_node

    def feature(self):
        return self.features


class FakePhysicsList:
    def __init__(self, fail_interface_selection=False):
        self.items = {}
        self.created = []
        self.removed = []
        self.fail_interface_selection = fail_interface_selection

    def tags(self):
        return list(self.items)

    def get(self, tag):
        return self.items[tag]

    def create(self, tag, physics_type, third_argument):
        physics = FakePhysics(tag, physics_type, third_argument)
        physics.selection_node.fail = self.fail_interface_selection
        self.items[tag] = physics
        self.created.append((tag, physics_type, third_argument))
        return physics

    def remove(self, tag):
        self.removed.append(tag)
        del self.items[tag]


class FakeGeometry:
    def getSDim(self):
        return 2


class FakeGeometryList:
    def tags(self):
        return ["geom1"]

    def get(self, tag):
        assert tag == "geom1"
        return FakeGeometry()


class FakeSelectionList:
    def __init__(self, tags=("domain_all", "left", "right")):
        self.selection_tags = list(tags)

    def tags(self):
        return self.selection_tags


class FakeComponent:
    def __init__(self, fail_interface_selection=False):
        self.physics_nodes = FakePhysicsList(fail_interface_selection)
        self.geometries = FakeGeometryList()
        self.selections = FakeSelectionList()

    def tag(self):
        return "comp1"

    def physics(self):
        return self.physics_nodes

    def geom(self, tag=None):
        return self.geometries if tag is None else self.geometries.get(tag)

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
    def __init__(self, component=None):
        self.component = component or FakeComponent()
        self.java = FakeJava(self.component)


def test_pressure_acoustics_uses_geometry_tag_and_named_domain_selection():
    model = FakeModel()

    result = add_pressure_acoustics_interface(
        model,
        selection_name="domain_all",
        component_name="comp1",
    )

    assert result["success"] is True
    assert model.component.physics_nodes.created == [("acpr", "PressureAcoustics", "geom1")]
    assert model.component.physics_nodes.items["acpr"].selection_node.named_tag == ("domain_all")


def test_pressure_acoustics_rolls_back_rejected_selection():
    component = FakeComponent(fail_interface_selection=True)

    result = add_pressure_acoustics_interface(FakeModel(component), domain_selection=[1])

    assert result["success"] is False
    assert result["rolled_back"] is True
    assert component.physics_nodes.items == {}


def test_coefficient_pde_sets_exact_equation_properties_and_variables():
    model = FakeModel()

    result = add_pde_interface(
        model,
        "coefficient",
        dependent_variables=["u", "v"],
        equation_properties={"c": "1", "a": "0", "f": ["fx", "fy"]},
        domain_selection=[1],
    )

    assert result["success"] is True
    physics = model.component.physics_nodes.items["c"]
    assert list(physics.third_argument) == ["u", "v"]
    assert physics.selection_node.entities == [1]
    assert physics.features.get("cfeq1").properties == {
        "c": "1",
        "a": "0",
        "f": ["fx", "fy"],
    }


def test_pde_rejects_unknown_properties_and_duplicate_variables_before_creation():
    model = FakeModel()

    unknown = add_pde_interface(model, "weak", equation_properties={"command": "bad"})
    duplicate = add_pde_interface(model, "general", dependent_variables=["u", "u"])

    assert unknown["success"] is False
    assert duplicate["success"] is False
    assert model.component.physics_nodes.created == []


def _model_with_physics(tag="acpr", physics_type="PressureAcoustics"):
    model = FakeModel()
    physics = FakePhysics(tag, physics_type, "geom1")
    model.component.physics_nodes.items[tag] = physics
    return model, physics


def test_acoustic_boundary_uses_exact_type_properties_and_dimension():
    model, physics = _model_with_physics()

    result = configure_boundaries(
        model,
        "acpr",
        [
            {
                "type": "Pressure",
                "boundaries": [1],
                "properties": {"p0": "1[Pa]"},
                "tag": "p_in",
            }
        ],
        family="acoustic",
    )

    assert result["success"] is True
    feature = physics.features.get("p_in")
    assert feature.dimension == 1
    assert feature.selection_node.entities == [1]
    assert feature.properties == {"p0": "1[Pa]"}


def test_pde_boundary_alias_supports_named_selection():
    model, physics = _model_with_physics("c", "CoefficientFormPDE")

    result = configure_boundaries(
        model,
        "c",
        [{"type": "dirichlet", "selection_name": "left", "properties": {"r": "0"}}],
        family="pde",
    )

    assert result["success"] is True
    feature = next(item for tag, item in physics.features.items.items() if tag != "cfeq1")
    assert feature.feature_type == "DirichletBoundary"
    assert feature.selection_node.named_tag == "left"


def test_boundary_batch_rolls_back_every_created_feature_on_property_failure():
    model, physics = _model_with_physics()
    physics.features.fail_type = "Impedance"

    result = configure_boundaries(
        model,
        "acpr",
        [
            {"type": "SoundHard", "boundaries": [1], "tag": "wall"},
            {
                "type": "Impedance",
                "boundaries": [2],
                "properties": {"Zn": "rho*c"},
                "tag": "imp",
            },
        ],
        family="acoustic",
    )

    assert result["success"] is False
    assert result["rolled_back"] is True
    assert {"wall", "imp"}.isdisjoint(physics.features.items)


def test_custom_boundary_type_and_property_are_rejected_before_mutation():
    model, physics = _model_with_physics()

    custom_type = configure_boundaries(
        model,
        "acpr",
        [{"type": "VersionSpecific", "boundaries": [1]}],
        family="acoustic",
    )
    custom_property = configure_boundaries(
        model,
        "acpr",
        [{"type": "SoundHard", "boundaries": [1], "properties": {"value": "1"}}],
        family="acoustic",
    )
    dimension_specific = configure_boundaries(
        model,
        "acpr",
        [{"type": "SphericalWaveRadiation", "boundaries": [1]}],
        family="acoustic",
    )
    indexed_weak_expression = configure_boundaries(
        model,
        "acpr",
        [{"type": "WeakContribution", "boundaries": [1], "properties": {"weak": "0"}}],
        family="pde",
    )

    assert custom_type["success"] is False
    assert custom_property["success"] is False
    assert dimension_specific["success"] is False
    assert indexed_weak_expression["success"] is False
    assert physics.features.items == {}
