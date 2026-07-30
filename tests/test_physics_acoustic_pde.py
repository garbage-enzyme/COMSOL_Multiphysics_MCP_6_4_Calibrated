"""Tests for the specialized Acoustics and PDE physics tools."""

from src.tools import physics


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorator(function):
            self.tools[function.__name__] = function
            return function

        return decorator


class FakeSelection:
    def __init__(self):
        self.values = None
        self.named_selection = None

    def set(self, values):
        self.values = values

    def named(self, selection_name):
        self.named_selection = selection_name


class FakeFeature:
    def __init__(self, tag, feature_type, rejected_properties=None):
        self._tag = tag
        self.feature_type = feature_type
        self._label = feature_type
        self._selection = FakeSelection()
        self.properties = {}
        self.rejected_properties = set(rejected_properties or [])

    def tag(self):
        return self._tag

    def label(self, value=None):
        if value is not None:
            self._label = value
        return self._label

    def selection(self):
        return self._selection

    def set(self, name, value):
        if name in self.rejected_properties:
            raise ValueError(f"Unsupported property: {name}")
        self.properties[name] = value


class FakePhysics(FakeFeature):
    def __init__(self, tag, physics_type, dependent_variables=None):
        super().__init__(tag, physics_type)
        self.dependent_variables = dependent_variables
        self.features = {}

    def create(self, tag, feature_type, dimension=None):
        rejected = ["invalid"] if feature_type == "Impedance" else []
        feature = FakeFeature(tag, feature_type, rejected)
        feature.dimension = dimension
        self.features[tag] = feature
        return feature

    def feature(self, tag):
        if tag not in self.features:
            self.features[tag] = FakeFeature(tag, "Equation")
        return self.features[tag]


class FakePhysicsList:
    def __init__(self):
        self.items = []

    def create(self, tag, physics_type, dependent_variables=None):
        item = FakePhysics(tag, physics_type, dependent_variables)
        self.items.append(item)
        return item

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

    def getSDim(self):
        return 2


class FakeGeometryList:
    def __init__(self):
        self.items = [FakeGeometry()]

    def size(self):
        return len(self.items)

    def get(self, index):
        return self.items[index]

    def tags(self):
        return [item.tag() for item in self.items]


class FakeComponent:
    def __init__(self, tag="comp1"):
        self._tag = tag
        self._physics = FakePhysicsList()
        self._geometry = FakeGeometryList()

    def tag(self):
        return self._tag

    def physics(self, tag=None):
        if tag is None:
            return self._physics
        return next(item for item in self._physics.items if item.tag() == tag)

    def geom(self, tag=None):
        if tag is None:
            return self._geometry
        return next(item for item in self._geometry.items if item.tag() == tag)


class FakeComponentList:
    def __init__(self):
        self.items = [FakeComponent()]

    def __call__(self, tag=None):
        if tag is None:
            return self
        return next(item for item in self.items if item.tag() == tag)

    def size(self):
        return len(self.items)

    def get(self, index):
        return self.items[index]

    def tags(self):
        return [item.tag() for item in self.items]


class FakeJavaModel:
    def __init__(self):
        self.component = FakeComponentList()


class FakeModel:
    def __init__(self):
        self.java = FakeJavaModel()


def registered_tools(monkeypatch):
    model = FakeModel()
    monkeypatch.setattr(physics.session_manager, "get_model", lambda name=None: model)
    mcp = FakeMCP()
    physics.register_physics_tools(mcp)
    return model, mcp.tools


def test_available_interfaces_include_pde(monkeypatch):
    _, tools = registered_tools(monkeypatch)

    result = tools["physics_get_available"]()

    assert result["success"] is True
    assert "Mathematics" in result["interfaces"]
    assert "coefficient_form_pde" in result["interfaces"]["Mathematics"]


def test_add_pressure_acoustics(monkeypatch):
    model, tools = registered_tools(monkeypatch)

    result = tools["physics_add_pressure_acoustics"](
        domain_selection=[1, 3],
        component_name="comp1",
    )

    created = model.java.component("comp1").physics().items[0]
    assert result["success"] is True
    assert result["physics"]["type"] == "PressureAcoustics"
    assert result["physics"]["geometry"] == "geom1"
    assert created.tag() == "acpr"
    assert created.selection().values == [1, 3]


def test_add_generic_acoustic_interface(monkeypatch):
    model, tools = registered_tools(monkeypatch)

    result = tools["physics_add_acoustics"](
        physics_type="ThermoacousticsSinglePhysics",
        physics_tag="ta",
        component_name="comp1",
    )

    created = model.java.component("comp1").physics().items[0]
    assert result["success"] is True
    assert result["physics"]["type"] == "ThermoacousticsSinglePhysics"
    assert result["physics"]["geometry"] == "geom1"
    assert created.tag() == "ta"


def test_add_coefficient_form_pde_with_equation_properties(monkeypatch):
    model, tools = registered_tools(monkeypatch)

    result = tools["physics_add_coefficient_form_pde"](
        dependent_variables=["u", "v"],
        equation_properties={"c": "1", "a": "0", "f": "source"},
    )

    created = model.java.component("comp1").physics().items[0]
    assert result["success"] is True
    assert created.feature("cfeq1").properties == {
        "c": "1",
        "a": "0",
        "f": "source",
    }
    assert created.dependent_variables == ["u", "v"]


def test_acoustic_boundary_property_failure_is_reported(monkeypatch):
    model, tools = registered_tools(monkeypatch)
    acoustic = model.java.component("comp1").physics().create(
        "acpr",
        "PressureAcoustics",
    )
    acoustic.label("Pressure Acoustics")

    result = tools["physics_configure_acoustic_boundary"](
        physics_name="Pressure Acoustics",
        boundary_condition="Impedance",
        boundary_selection=[2],
        properties={"Zn": "rho0*c0", "invalid": "value"},
    )

    assert result["success"] is False
    assert result["configured_count"] == 0
    assert result["failed_count"] == 1
    assert result["failed_boundaries"][0]["dimension"] == 1
    assert "invalid" in result["failed_boundaries"][0]["property_errors"]


def test_pde_boundary_batch_accepts_custom_feature_types(monkeypatch):
    model, tools = registered_tools(monkeypatch)
    pde = model.java.component("comp1").physics().create(
        "c",
        "CoefficientFormPDE",
        ["u"],
    )
    pde.label("Coefficient Form PDE")

    result = tools["physics_setup_pde_boundaries"](
        physics_name="Coefficient Form PDE",
        boundary_conditions=[
            {
                "type": "DirichletBoundary",
                "boundaries": [1],
                "properties": {"r": "1"},
            },
            {
                "type": "VersionSpecificBoundary",
                "boundaries": [2],
                "properties": {"value": "2"},
            },
        ],
    )

    assert result["success"] is True
    assert result["configured_count"] == 2
    assert all(item["dimension"] == 1 for item in result["configured_boundaries"])
    assert result["custom_condition_types"] == ["VersionSpecificBoundary"]


def test_pde_boundary_supports_named_selection_and_alias(monkeypatch):
    model, tools = registered_tools(monkeypatch)
    pde = model.java.component("comp1").physics().create(
        "c",
        "CoefficientFormPDE",
        ["eta"],
    )
    pde.label("Coefficient Form PDE")

    result = tools["physics_configure_pde_boundary"](
        physics_name="Coefficient Form PDE",
        boundary_condition="zero_flux",
        selection_name="wave_top",
    )

    created = next(iter(pde.features.values()))
    assert result["success"] is True
    assert result["configured_boundaries"][0]["type"] == "ZeroFluxBoundary"
    assert result["configured_boundaries"][0]["selection_name"] == "wave_top"
    assert created.selection().named_selection == "wave_top"


def test_pde_boundary_reference_matches_comsol_63(monkeypatch):
    _, tools = registered_tools(monkeypatch)

    result = tools["physics_get_pde_boundary_conditions"]()

    conditions = result["boundary_conditions"]
    assert "ZeroFluxBoundary" in conditions
    assert "WeakContribution" in conditions
    assert "GeneralFluxBoundary" not in conditions
