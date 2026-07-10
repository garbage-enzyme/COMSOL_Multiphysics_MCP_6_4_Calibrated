"""Unit tests for physics helpers without a COMSOL client."""

from src.tools.physics import add_physics_interface


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
