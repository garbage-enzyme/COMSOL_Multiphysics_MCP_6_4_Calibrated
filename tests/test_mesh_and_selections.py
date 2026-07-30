"""Tests for mesh sequence and geometry selection tools."""

from src.tools import geometry, mesh


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorator(function):
            self.tools[function.__name__] = function
            return function

        return decorator


class FakeNodeList:
    def __init__(self, items=None):
        self.items = list(items or [])

    def size(self):
        return len(self.items)

    def get(self, index):
        return self.items[index]

    def tags(self):
        return [item.tag() for item in self.items]


class FakeGeometry:
    def __init__(self, tag="geom1"):
        self._tag = tag

    def tag(self):
        return self._tag


class FakeMesh:
    def __init__(self, tag, geometry):
        self._tag = tag
        self._geometry = geometry
        self.ran = False
        self._features = FakeNodeList()

    def tag(self):
        return self._tag

    def label(self):
        return f"Mesh {self._tag}"

    def geom(self):
        return self._geometry

    def run(self):
        self.ran = True

    def feature(self):
        return self._features


class FakeMeshList(FakeNodeList):
    def create(self, tag, geometry):
        item = FakeMesh(tag, geometry)
        self.items.append(item)
        return item


class FakeBoxSelection:
    def __init__(self, tag):
        self._tag = tag
        self.geometry = None
        self.dimension = None
        self.properties = {}

    def geom(self, geometry, dimension):
        self.geometry = geometry
        self.dimension = dimension

    def set(self, name, value):
        self.properties[name] = value

    def entities(self):
        return [1, 2]


class FakeSelectionList:
    def __init__(self):
        self.items = {}

    def create(self, tag, selection_type):
        assert selection_type == "Box"
        item = FakeBoxSelection(tag)
        self.items[tag] = item
        return item


class FakeComponent:
    def __init__(self, tag="comp1"):
        self._tag = tag
        self._geometries = FakeNodeList([FakeGeometry()])
        self._meshes = FakeMeshList()
        self._selections = FakeSelectionList()

    def tag(self):
        return self._tag

    def geom(self, tag=None):
        if tag is None:
            return self._geometries
        return next(item for item in self._geometries.items if item.tag() == tag)

    def mesh(self, tag=None):
        if tag is None:
            return self._meshes
        return next(item for item in self._meshes.items if item.tag() == tag)

    def selection(self):
        return self._selections


class FakeComponentList(FakeNodeList):
    def __call__(self, tag=None):
        if tag is None:
            return self
        return next(item for item in self.items if item.tag() == tag)


class FakeJavaModel:
    def __init__(self):
        self.component = FakeComponentList([FakeComponent()])


class FakeModel:
    def __init__(self):
        self.java = FakeJavaModel()


def registered_tools(monkeypatch):
    model = FakeModel()
    monkeypatch.setattr(mesh.session_manager, "get_model", lambda name=None: model)
    monkeypatch.setattr(geometry.session_manager, "get_model", lambda name=None: model)
    mcp = FakeMCP()
    mesh.register_mesh_tools(mcp)
    geometry.register_geometry_tools(mcp)
    return model, mcp.tools


def test_mesh_create_auto_creates_and_runs_sequence(monkeypatch):
    model, tools = registered_tools(monkeypatch)

    result = tools["mesh_create"]()

    created = model.java.component("comp1").mesh("mesh1")
    assert result["success"] is True
    assert result["auto_created"] is True
    assert result["meshes"] == ["mesh1"]
    assert created.geom() == "geom1"
    assert created.ran is True


def test_mesh_list_and_info_use_java_tags(monkeypatch):
    model, tools = registered_tools(monkeypatch)
    model.java.component("comp1").mesh().create("mesh_custom", "geom1")

    listed = tools["mesh_list"]()
    info = tools["mesh_info"](mesh_name="mesh_custom")

    assert listed["meshes"][0]["tag"] == "mesh_custom"
    assert info["success"] is True
    assert info["mesh"]["tag"] == "mesh_custom"
    assert info["mesh"]["geometry"] == "geom1"


def test_geometry_create_box_selection(monkeypatch):
    model, tools = registered_tools(monkeypatch)

    result = tools["geometry_create_box_selection"](
        selection_name="wave_left",
        x_min="-1e-9[m]",
        x_max="1e-9[m]",
        y_min="0",
        y_max="1",
    )

    created = model.java.component("comp1").selection().items["wave_left"]
    assert result["success"] is True
    assert result["selection"]["entities"] == [1, 2]
    assert created.geometry == "geom1"
    assert created.dimension == 1
    assert created.properties["condition"] == "intersects"


def test_geometry_create_side_selections(monkeypatch):
    model, tools = registered_tools(monkeypatch)

    result = tools["geometry_create_side_selections"](
        x_min="0",
        x_max="2[m]",
        y_min="0",
        y_max="1[m]",
        prefix="wave",
    )

    selections = model.java.component("comp1").selection().items
    assert result["success"] is True
    assert set(selections) == {
        "wave_left",
        "wave_right",
        "wave_bottom",
        "wave_top",
    }
