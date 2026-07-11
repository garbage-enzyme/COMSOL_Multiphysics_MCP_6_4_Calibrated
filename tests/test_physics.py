"""Unit tests for physics helpers without a COMSOL client."""

from src.tools.physics import (
    add_boundary_condition,
    add_physics_interface,
    list_physics_features,
    remove_physics_interface,
    setup_flow_boundaries,
    setup_heat_boundaries,
)


class FakePhysics:
    def __init__(self, label):
        self._label = label

    def label(self):
        return self._label


class FakePhysicsList:
    def __init__(self, existing=()):
        self.existing = list(existing)
        self.created = []

    def tags(self):
        return list(self.existing)

    def create(self, tag, interface_type, dimension):
        self.created.append((tag, interface_type, dimension))
        self.existing.append(tag)
        return FakePhysics(interface_type)


class FakeGeometry:
    def getSDim(self):
        return 3


class FakeGeometryList:
    def tags(self):
        return ["geom1"]


class FakeComponent:
    def __init__(self, existing=()):
        self.physics_list = FakePhysicsList(existing)

    def tag(self):
        return "comp1"

    def physics(self):
        return self.physics_list

    def geom(self, tag=None):
        return FakeGeometryList() if tag is None else FakeGeometry()


class FakeComponentList:
    def __init__(self, component):
        self.component = component

    def tags(self):
        return ["comp1"]

    def get(self, tag):
        return self.component


class FakeJava:
    def __init__(self, component):
        self.component_node = component

    def component(self, tag=None):
        if tag is None:
            return FakeComponentList(self.component_node)
        return self.component_node


class FakeModel:
    def __init__(self, component):
        self.java = FakeJava(component)


def test_add_physics_interface_normalizes_electrostatics_alias():
    component = FakeComponent()

    result = add_physics_interface(FakeModel(component), "es")

    assert result["success"] is True
    assert component.physics_list.created == [("es", "Electrostatics", "3")]
    assert result["physics"]["requested_type"] == "es"
    assert result["physics"]["type"] == "Electrostatics"


def test_add_physics_interface_avoids_existing_tag():
    component = FakeComponent(existing=["ht", "ht2"])

    result = add_physics_interface(FakeModel(component), "Heat Transfer")

    assert result["physics"]["tag"] == "ht3"
    assert component.physics_list.created == [("ht3", "HeatTransfer", "3")]


def test_add_physics_interface_preserves_unknown_full_type():
    component = FakeComponent()

    result = add_physics_interface(FakeModel(component), "CustomPhysics")

    assert result["physics"]["type"] == "CustomPhysics"
    assert component.physics_list.created == [
        ("customphysics", "CustomPhysics", "3")
    ]


def test_add_physics_interface_validates_type():
    result = add_physics_interface(FakeModel(FakeComponent()), "  ")

    assert result["success"] is False


class FakeSelection:
    def __init__(self, entities):
        self._entities = entities

    def entities(self):
        return self._entities


class FakePhysicsFeature:
    def __init__(self, label, entities):
        self._label = label
        self._selection = FakeSelection(entities)

    def label(self):
        return self._label

    def selection(self):
        return self._selection


class FakePhysicsNode:
    def __init__(self):
        self.features = {
            "wee1": FakePhysicsFeature("Wave Equation", [1, 2]),
            "pec1": FakePhysicsFeature("Perfect Electric Conductor", [3]),
        }

    def label(self):
        return "Electromagnetic Waves, Frequency Domain"

    def tag(self):
        return "ewfd"

    def feature(self):
        return FakePhysicsFeatureList(self.features)


class FakePhysicsFeatureList:
    def __init__(self, features):
        self.features = features

    def tags(self):
        return list(self.features)

    def get(self, tag):
        return self.features[tag]


class ListingPhysicsList:
    def __init__(self, physics):
        self.physics = physics

    def tags(self):
        return list(self.physics)

    def get(self, tag):
        return self.physics[tag]

    def remove(self, tag):
        del self.physics[tag]


class ListingComponent:
    def __init__(self, physics):
        self.physics_nodes = physics

    def physics(self):
        return ListingPhysicsList(self.physics_nodes)


class ListingComponentList:
    def __init__(self, component):
        self.component = component

    def tags(self):
        return ["comp1"]

    def get(self, tag):
        return self.component


class ListingJava:
    def __init__(self, physics):
        self.component_list = ListingComponentList(ListingComponent(physics))

    def component(self):
        return self.component_list


class ListingModel:
    def __init__(self, physics):
        self.java = ListingJava(physics)


def test_list_physics_features_uses_tags_labels_and_selections():
    result = list_physics_features(ListingModel({"ewfd": FakePhysicsNode()}), "ewfd")

    assert result == {
        "success": True,
        "physics": "ewfd",
        "features": [
            {"tag": "wee1", "label": "Wave Equation", "selection": [1, 2]},
            {
                "tag": "pec1",
                "label": "Perfect Electric Conductor",
                "selection": [3],
            },
        ],
        "count": 2,
    }


def test_list_physics_features_accepts_physics_label():
    result = list_physics_features(
        ListingModel({"ewfd": FakePhysicsNode()}),
        "Electromagnetic Waves, Frequency Domain",
    )

    assert result["success"] is True
    assert result["count"] == 2


def test_remove_physics_interface_accepts_label():
    physics = {"ewfd": FakePhysicsNode()}
    model = ListingModel(physics)

    result = remove_physics_interface(
        model,
        "Electromagnetic Waves, Frequency Domain",
    )

    assert result == {
        "success": True,
        "removed": "ewfd",
        "label": "Electromagnetic Waves, Frequency Domain",
        "component": "comp1",
    }
    assert physics == {}


def test_remove_physics_interface_reports_available_nodes():
    result = remove_physics_interface(
        ListingModel({"ewfd": FakePhysicsNode()}),
        "missing",
    )

    assert result["success"] is False
    assert result["available"][0]["tag"] == "ewfd"


class BoundarySelection:
    def __init__(self):
        self.entities = None

    def set(self, entities):
        self.entities = entities


class BoundaryFeature:
    def __init__(self):
        self.selection_node = BoundarySelection()
        self.properties = {}
        self.feature_label = None

    def selection(self):
        return self.selection_node

    def set(self, name, value):
        self.properties[name] = value

    def label(self, value):
        self.feature_label = value


class BoundaryFeatureList:
    def __init__(self):
        self.created = []

    def create(self, tag, feature_type, entity_dimension):
        feature = BoundaryFeature()
        self.created.append((tag, feature_type, entity_dimension, feature))
        return feature


class BoundaryPhysics:
    def __init__(self):
        self.features = BoundaryFeatureList()

    def tag(self):
        return "ht"

    def label(self):
        return "Heat Transfer in Solids"

    def feature(self):
        return self.features


class BoundaryPhysicsList:
    def __init__(self, physics):
        self.physics = physics

    def tags(self):
        return ["ht"]

    def get(self, tag):
        return self.physics


class BoundaryComponent(FakeComponent):
    def __init__(self, physics):
        super().__init__()
        self.physics_node = physics

    def physics(self):
        return BoundaryPhysicsList(self.physics_node)


def test_add_boundary_condition_uses_feature_create_and_boundary_dimension():
    physics = BoundaryPhysics()
    component = BoundaryComponent(physics)

    result = add_boundary_condition(
        FakeModel(component),
        "ht",
        "Temperature",
        [3, 4],
        properties={"T0": "293.15[K]"},
        feature_tag="temp1",
    )

    tag, feature_type, dimension, feature = physics.features.created[0]
    assert (tag, feature_type, dimension) == ("temp1", "TemperatureBoundary", 2)
    assert feature.selection_node.entities == [3, 4]
    assert feature.properties == {"T0": "293.15[K]"}
    assert result["boundary_condition"]["entity_dimension"] == 2


def test_add_boundary_condition_resolves_canonical_name_with_localized_label():
    physics = BoundaryPhysics()
    physics.label = lambda: "静电"
    physics.tag = lambda: "es"
    component = BoundaryComponent(physics)

    result = add_boundary_condition(
        FakeModel(component),
        "Electrostatics",
        "Ground",
        [3],
        feature_tag="gnd1",
    )

    assert result["success"] is True
    assert physics.features.created[0][0:3] == ("gnd1", "Ground", 2)


def test_add_boundary_condition_validates_selection():
    result = add_boundary_condition(
        FakeModel(BoundaryComponent(BoundaryPhysics())),
        "ht",
        "Temperature",
        [],
    )

    assert result["success"] is False


def test_setup_flow_boundaries_uses_clientapi_features():
    physics = BoundaryPhysics()
    model = FakeModel(BoundaryComponent(physics))

    result = setup_flow_boundaries(
        model,
        "ht",
        [1],
        [2],
        inlet_velocity="2[mm/s]",
        outlet_pressure="1[Pa]",
    )

    assert result["success"] is True
    assert [item[1] for item in physics.features.created] == [
        "InletBoundary",
        "OutletBoundary",
    ]
    assert physics.features.created[0][3].properties == {"U0": "2[mm/s]"}
    assert physics.features.created[1][3].properties == {"p0": "1[Pa]"}


def test_setup_heat_boundaries_creates_all_requested_types():
    physics = BoundaryPhysics()
    model = FakeModel(BoundaryComponent(physics))

    result = setup_heat_boundaries(
        model,
        "ht",
        heat_flux_boundaries=[1],
        temperature_boundaries=[2],
        convection_boundaries=[3],
    )

    assert result["summary"] == {
        "heat_flux_boundaries": 1,
        "temperature_boundaries": 1,
        "convection_boundaries": 1,
    }
    assert [item[1] for item in physics.features.created] == [
        "HeatFluxBoundary",
        "TemperatureBoundary",
        "ConvectiveHeatFlux",
    ]
