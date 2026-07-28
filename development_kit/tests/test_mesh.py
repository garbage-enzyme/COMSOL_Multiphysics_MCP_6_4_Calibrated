"""Unit tests for mesh helpers without a COMSOL client."""

from src.tools.mesh import create_mesh_sequence, get_mesh_info


class FakeFeatureList:
    def tags(self):
        return ["size", "ftet1"]


class FakeMesh:
    def label(self):
        return "Physics-controlled mesh"

    def feature(self):
        return FakeFeatureList()

    def getNumElem(self):
        return 18837

    def getNumVertex(self):
        return 4120


class FakeMeshList:
    def __init__(self, meshes):
        self.meshes = meshes

    def tags(self):
        return list(self.meshes)

    def get(self, tag):
        return self.meshes[tag]


class FakeComponent:
    def __init__(self, meshes):
        self.meshes = meshes

    def tag(self):
        return "comp1"

    def mesh(self):
        return FakeMeshList(self.meshes)


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
    def __init__(self, meshes):
        self.java = FakeJava(FakeComponent(meshes))


class JavaStringLike:
    def __init__(self, value):
        self.value = value

    def __str__(self):
        return self.value


class JavaTagFeatureList(FakeFeatureList):
    def tags(self):
        return [JavaStringLike("size"), JavaStringLike("ftet1")]


class JavaTagMesh(FakeMesh):
    def feature(self):
        return JavaTagFeatureList()


class JavaTagMeshList(FakeMeshList):
    def tags(self):
        return [JavaStringLike(tag) for tag in self.meshes]

    def get(self, tag):
        return self.meshes[str(tag)]


class JavaTagComponent(FakeComponent):
    def mesh(self):
        return JavaTagMeshList(self.meshes)


def test_get_mesh_info_uses_clientapi_counts():
    result = get_mesh_info(FakeModel({"mesh1": FakeMesh()}))

    assert result == {
        "success": True,
        "mesh": {
            "name": "mesh1",
            "component": "comp1",
            "features": ["size", "ftet1"],
            "label": "Physics-controlled mesh",
            "num_elements": 18837,
            "num_vertices": 4120,
        },
    }


def test_get_mesh_info_resolves_label():
    result = get_mesh_info(
        FakeModel({"mesh1": FakeMesh()}),
        mesh_name="Physics-controlled mesh",
    )

    assert result["success"] is True
    assert result["mesh"]["name"] == "mesh1"


def test_get_mesh_info_reports_available_tags():
    result = get_mesh_info(FakeModel({"mesh1": FakeMesh()}), mesh_name="missing")

    assert result["success"] is False
    assert "mesh1" in result["error"]


def test_get_mesh_info_normalizes_java_string_tags():
    model = FakeModel({"mesh1": JavaTagMesh()})
    model.java = FakeJava(JavaTagComponent({"mesh1": JavaTagMesh()}))

    result = get_mesh_info(model)

    assert result["success"] is True
    assert result["mesh"]["name"] == "mesh1"
    assert result["mesh"]["features"] == ["size", "ftet1"]


class MutableMeshFeatureList:
    def __init__(self, fail=False):
        self.created = []
        self.fail = fail

    def create(self, tag, feature_type):
        if self.fail:
            raise RuntimeError("feature failure")
        self.created.append((tag, feature_type))


class MutableMeshSequence:
    def __init__(self, *, fail_feature=False, fail_run=False):
        self.features = MutableMeshFeatureList(fail_feature)
        self.fail_run = fail_run

    def feature(self):
        return self.features

    def run(self):
        if self.fail_run:
            raise RuntimeError("build failure")

    def getNumElem(self):
        return 12

    def getNumVertex(self):
        return 7


class MutableMeshList:
    def __init__(self, existing=(), *, fail_feature=False, fail_run=False):
        self.meshes = {tag: object() for tag in existing}
        self.fail_feature = fail_feature
        self.fail_run = fail_run

    def tags(self):
        return list(self.meshes)

    def create(self, tag):
        sequence = MutableMeshSequence(
            fail_feature=self.fail_feature,
            fail_run=self.fail_run,
        )
        self.meshes[tag] = sequence
        return sequence

    def remove(self, tag):
        del self.meshes[tag]


class MutableMeshComponent(FakeComponent):
    def __init__(self, mesh_list):
        self.mesh_list = mesh_list

    def mesh(self):
        return self.mesh_list


def test_create_mesh_sequence_rolls_back_feature_and_build_failures():
    for failure in ("fail_feature", "fail_run"):
        mesh_list = MutableMeshList(**{failure: True})
        model = FakeModel({})
        model.java = FakeJava(MutableMeshComponent(mesh_list))

        result = create_mesh_sequence(model, mesh_name="mesh2")

        assert result == {
            "success": False,
            "error": "Mesh setup failed.",
            "rolled_back": True,
        }
        assert "mesh2" not in mesh_list.meshes


def test_create_mesh_sequence_validates_before_creation_and_builds_successfully():
    mesh_list = MutableMeshList(existing=("mesh1",))
    model = FakeModel({})
    model.java = FakeJava(MutableMeshComponent(mesh_list))

    invalid = create_mesh_sequence(model, mesh_name=" ")
    created = create_mesh_sequence(model, mesh_name="mesh2")

    assert invalid["success"] is False
    assert list(mesh_list.meshes) == ["mesh1", "mesh2"]
    assert created["success"] is True
    assert created["built"] is True
    assert created["num_elements"] == 12
