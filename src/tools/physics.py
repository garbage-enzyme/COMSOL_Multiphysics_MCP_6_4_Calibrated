"""Physics tools for COMSOL MCP Server."""

from typing import Optional, Sequence
import jpype
from mcp.server.fastmcp import FastMCP

from .session import session_manager

_tag_counter = {}

PHYSICS_TYPE_ALIASES = {
    "es": ("es", "Electrostatics"),
    "electrostatics": ("es", "Electrostatics"),
    "ec": ("ec", "ConductiveMedia"),
    "electriccurrents": ("ec", "ConductiveMedia"),
    "conductivemedia": ("ec", "ConductiveMedia"),
    "solid": ("solid", "SolidMechanics"),
    "solidmechanics": ("solid", "SolidMechanics"),
    "ht": ("ht", "HeatTransfer"),
    "heattransfer": ("ht", "HeatTransfer"),
    "spf": ("spf", "LaminarFlow"),
    "laminarflow": ("spf", "LaminarFlow"),
}

BOUNDARY_TYPE_ALIASES = {
    "temperature": "TemperatureBoundary",
    "temperatureboundary": "TemperatureBoundary",
    "heatflux": "HeatFluxBoundary",
    "heatfluxboundary": "HeatFluxBoundary",
}


def _first_component(jm):
    """Return the first component's Java object.

    COMSOL's ModelEntityList.get() requires a String tag, not an int index,
    so we iterate via tags() and pick the first one.
    """
    tags = list(jm.component().tags())
    if not tags:
        return None
    return jm.component().get(tags[0])


def _component_sdim(comp):
    """Get spatial dimension from a component's first geometry.

    The COMSOL client API's physics `create` requires the spatial dimension
    passed as a string (e.g. "3"). Component dimension is determined by the
    geometry sequence created on it via `geom().create(tag, sdim)`. If no
    geometry exists yet, default to "3" (3D).
    """
    try:
        geom_tags = list(comp.geom().tags())
        if geom_tags:
            return str(comp.geom(geom_tags[0]).getSDim())
    except Exception:
        pass
    return "3"


def _find_physics_context(jm, physics_name):
    """Return ``(component, physics)`` by label or tag."""
    for comp_tag in jm.component().tags():
        comp = jm.component().get(comp_tag)
        for p_tag in comp.physics().tags():
            p = comp.physics().get(p_tag)
            if p.label() == physics_name or p.tag() == physics_name:
                return comp, p
    return None, None


def _find_physics_java(jm, physics_name):
    """Look up a physics node by label or tag across all components."""
    _, physics = _find_physics_context(jm, physics_name)
    return physics


def add_boundary_condition(
    model,
    physics_name: str,
    boundary_condition: str,
    boundary_selection: Sequence[int],
    *,
    properties: Optional[dict] = None,
    feature_tag: Optional[str] = None,
) -> dict:
    """Create a boundary feature with the required clientapi entity dimension."""
    if not boundary_selection:
        return {"success": False, "error": "boundary_selection must not be empty."}

    comp, physics = _find_physics_context(model.java, physics_name)
    if physics is None:
        return {
            "success": False,
            "error": f"Physics interface not found: {physics_name}",
        }

    normalized = boundary_condition.replace(" ", "").casefold()
    feature_type = BOUNDARY_TYPE_ALIASES.get(normalized, boundary_condition)
    boundary_dim = max(int(_component_sdim(comp)) - 1, 0)
    tag = feature_tag or _make_tag(feature_type.lower())
    feature = physics.feature().create(tag, feature_type, boundary_dim)
    boundaries = [int(boundary) for boundary in boundary_selection]
    feature.selection().set(boundaries)

    property_errors = {}
    for name, value in (properties or {}).items():
        try:
            feature.set(name, value)
        except Exception as exc:
            property_errors[name] = str(exc)
    try:
        feature.label(f"{boundary_condition} (Boundaries {boundaries})")
    except Exception:
        pass

    result = {
        "success": True,
        "physics": physics_name,
        "boundary_condition": {
            "tag": tag,
            "type": feature_type,
            "requested_type": boundary_condition,
            "boundaries": boundaries,
            "properties": dict(properties or {}),
            "entity_dimension": boundary_dim,
        },
    }
    if property_errors:
        result["warning"] = "Boundary created, but some properties could not be set."
        result["property_errors"] = property_errors
    return result


def _make_tag(prefix="bc"):
    """Generate a unique tag using a monotonic counter."""
    _tag_counter[prefix] = _tag_counter.get(prefix, 0) + 1
    return f"{prefix}_{_tag_counter[prefix]}"


def _physics_spec(physics_type: str) -> tuple[str, str]:
    """Return the conventional tag prefix and clientapi interface type."""
    normalized = physics_type.replace(" ", "").replace("_", "").casefold()
    if normalized in PHYSICS_TYPE_ALIASES:
        return PHYSICS_TYPE_ALIASES[normalized]
    tag = physics_type.replace(" ", "_").lower()
    return tag, physics_type


def add_physics_interface(
    model,
    physics_type: str,
    *,
    component_name: Optional[str] = None,
) -> dict:
    """Add a physics interface with alias normalization for clientapi."""
    if not physics_type.strip():
        return {"success": False, "error": "physics_type must not be empty."}

    jm = model.java
    comp = jm.component(component_name) if component_name else _first_component(jm)
    if comp is None:
        return {"success": False, "error": f"Component not found: {component_name}"}

    tag_prefix, interface_type = _physics_spec(physics_type)
    existing = set(comp.physics().tags())
    tag = tag_prefix
    index = 2
    while tag in existing:
        tag = f"{tag_prefix}{index}"
        index += 1

    physics_java = comp.physics().create(tag, interface_type, _component_sdim(comp))
    return {
        "success": True,
        "physics": {
            "name": (
                physics_java.label()
                if hasattr(physics_java, "label")
                else interface_type
            ),
            "type": interface_type,
            "requested_type": physics_type,
            "tag": tag,
            "component": comp.tag(),
        },
    }


def list_physics_features(model, physics_name: str) -> dict:
    """List physics child features through clientapi tags and labels."""
    physics = _find_physics_java(model.java, physics_name)
    if physics is None:
        return {
            "success": False,
            "error": f"Physics interface not found: {physics_name}",
        }

    features = []
    feature_list = physics.feature()
    for tag in list(feature_list.tags()):
        feature = feature_list.get(tag)
        info = {"tag": tag}
        try:
            info["label"] = str(feature.label())
        except Exception:
            info["label"] = tag
        try:
            info["selection"] = list(feature.selection().entities())
        except Exception:
            info["selection"] = None
        features.append(info)

    return {
        "success": True,
        "physics": physics_name,
        "features": features,
        "count": len(features),
    }


def remove_physics_interface(model, physics_name: str) -> dict:
    """Remove a physics interface by tag or label through clientapi."""
    jm = model.java
    available = []
    for component_tag in list(jm.component().tags()):
        component = jm.component().get(component_tag)
        physics_list = component.physics()
        for tag in list(physics_list.tags()):
            physics = physics_list.get(tag)
            try:
                label = str(physics.label())
            except Exception:
                label = tag
            available.append({"component": component_tag, "tag": tag, "label": label})
            if physics_name in {tag, label}:
                physics_list.remove(tag)
                return {
                    "success": True,
                    "removed": tag,
                    "label": label,
                    "component": component_tag,
                }
    return {
        "success": False,
        "error": f"Physics interface not found: {physics_name}",
        "available": available,
    }


PHYSICS_INTERFACES = {
    "AC/DC": {
        "electrostatic": "Electrostatics (es)",
        "electric_currents": "Electric Currents (ec)",
        "magnetic_fields": "Magnetic Fields (mf)",
        "electromagnetic_waves": "Electromagnetic Waves (emw)",
    },
    "Structural": {
        "solid_mechanics": "Solid Mechanics (solid)",
        "shell": "Shell (shell)",
        "beam": "Beam (beam)",
        "membrane": "Membrane (memb)",
    },
    "Heat Transfer": {
        "heat_transfer": "Heat Transfer in Solids (ht)",
        "conjugate_ht": "Conjugate Heat Transfer (cht)",
        "radiation": "Radiation (rad)",
    },
    "Fluid Flow": {
        "laminar_flow": "Laminar Flow (spf)",
        "turbulent_flow": "Turbulent Flow (spf)",
        "creeping_flow": "Creeping Flow (brinkman)",
    },
    "Acoustics": {
        "pressure_acoustics": "Pressure Acoustics (acpr)",
        "thermoacoustics": "Thermoacoustics (ta)",
    },
    "Chemical": {
        "transport_diluted": "Transport of Diluted Species (tds)",
        "reaction_engineering": "Reaction Engineering (re)",
    },
    "Optics": {
        "ray_optics": "Geometrical Optics (gop)",
        "wave_optics": "Wave Optics (ewfd)",
    },
    "Multiphysics": {
        "thermal_stress": "Thermal Stress (ts)",
        "fluid_structure": "Fluid-Structure Interaction (fsi)",
        "electromechanical": "Electromechanical Forces",
        "joule_heating": "Joule Heating (jh)",
    },
}


def register_physics_tools(mcp: FastMCP) -> None:
    """Register physics tools with the MCP server."""
    
    @mcp.tool()
    def physics_list(model_name: Optional[str] = None) -> dict:
        """
        List all physics interfaces defined in a model.
        
        Args:
            model_name: Model name (default: current model)
        
        Returns:
            List of physics interface names
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            physics = model.physics()
            multiphysics = model.multiphysics()
            
            return {
                "success": True,
                "physics": physics,
                "multiphysics": multiphysics,
                "physics_count": len(physics),
                "multiphysics_count": len(multiphysics),
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to list physics: {str(e)}"}
    
    @mcp.tool()
    def physics_get_available() -> dict:
        """
        Get a list of available physics interfaces organized by category.
        
        Returns:
            Dictionary of physics categories and their interfaces
        """
        return {
            "success": True,
            "interfaces": PHYSICS_INTERFACES,
            "note": "Interface identifiers (in parentheses) are used when adding physics.",
        }
    
    @mcp.tool()
    def physics_add(
        physics_type: str,
        component_name: Optional[str] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Add a physics interface to the model.

        Common physics types:
        - "Electrostatics" or "es": Electrostatic field analysis
        - "ElectricCurrents" or "ec": Electric current conduction
        - "SolidMechanics" or "solid": Structural stress analysis
        - "HeatTransfer" or "ht": Heat transfer in solids
        - "LaminarFlow" or "spf": Fluid dynamics

        Args:
            physics_type: Type identifier (e.g., "Electrostatics", "es")
            component_name: Component to add physics to (default: first component)
            model_name: Model name (default: current model)

        Returns:
            Created physics interface info
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            return add_physics_interface(
                model,
                physics_type,
                component_name=component_name,
            )
        except Exception as e:
            return {"success": False, "error": f"Failed to add physics: {str(e)}"}
    
    @mcp.tool()
    def physics_add_electrostatics(
        domain_selection: Optional[str] = None,
        relpermittivity: Optional[float] = None,
        domain_numbers: Optional[Sequence[int]] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Add Electrostatics physics interface for electric field analysis.

        In COMSOL 6.3+/6.4 the Electrostatics interface defaults to a FreeSpace
        domain feature that uses vacuum permittivity and IGNORES material
        relpermittivity. To model a dielectric, pass relpermittivity (e.g. 2.1)
        and this tool will automatically create a ChargeConservation feature
        plus a material node so the value takes effect.

        Args:
            domain_selection: Selection name for domains (default: all domains)
            relpermittivity: Relative permittivity eps_r (e.g. 2.1). If given,
                a ChargeConservation feature + material are created automatically.
            domain_numbers: Domain numbers to assign the dielectric to (default: all)
            model_name: Model name (default: current model)

        Returns:
            Created physics info
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            jm = model.java
            comp = _first_component(jm)
            if comp is None:
                return {"success": False, "error": "No component found in model."}
            physics_java = comp.physics().create("es", "Electrostatics", _component_sdim(comp))

            if domain_selection:
                try:
                    physics_java.selection().set(domain_selection)
                except Exception:
                    pass

            ccn_info = None
            if relpermittivity is not None:
                # Create material node with relpermittivity under propertyGroup('def')
                mat_tag = "mat_es"
                try:
                    mat = comp.material().create(mat_tag, "Common")
                    mat.label(f"dielectric_epsr_{relpermittivity}")
                    mat.propertyGroup("def").set("relpermittivity", str(relpermittivity))
                    if domain_numbers:
                        mat.selection().set([int(d) for d in domain_numbers])
                except Exception as e:
                    return {"success": False, "error": f"Created Electrostatics but failed to add material: {str(e)}"}

                # Create ChargeConservation domain feature (overrides default FreeSpace fsp1)
                try:
                    sdim = _component_sdim(comp)
                    ccn = physics_java.feature().create("ccn1", "ChargeConservation", int(sdim))
                    if domain_numbers:
                        ccn.selection().set([int(d) for d in domain_numbers])
                    ccn.set("materialType", "from_mat")
                    ccn_info = {
                        "tag": "ccn1",
                        "type": "ChargeConservation",
                        "materialType": "from_mat",
                        "relpermittivity": relpermittivity,
                        "material_tag": mat_tag,
                        "domain_numbers": list(domain_numbers) if domain_numbers else "all",
                    }
                except Exception as e:
                    return {"success": False, "error": f"Created Electrostatics+material but failed to add ChargeConservation: {str(e)}"}

            return {
                "success": True,
                "physics": {
                    "name": physics_java.label() if hasattr(physics_java, 'label') else "Electrostatics",
                    "type": "Electrostatics",
                    "tag": "es",
                    "domain_selection": domain_selection,
                    "charge_conservation": ccn_info,
                    "note": ("ChargeConservation+material created (6.3+/6.4 default FreeSpace would ignore eps_r)."
                             if ccn_info else
                             "No relpermittivity given: 6.3+/6.4 default FreeSpace uses vacuum eps0. Pass relpermittivity to model a dielectric."),
                }
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to add Electrostatics: {str(e)}"}
    
    @mcp.tool()
    def physics_add_solid_mechanics(
        domain_selection: Optional[str] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Add Solid Mechanics physics for structural analysis.

        Args:
            domain_selection: Selection name for domains (default: all domains)
            model_name: Model name (default: current model)

        Returns:
            Created physics info
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            jm = model.java
            comp = _first_component(jm)
            if comp is None:
                return {"success": False, "error": "No component found in model."}
            physics_java = comp.physics().create("solid", "SolidMechanics", _component_sdim(comp))

            if domain_selection:
                try:
                    physics_java.selection().set(domain_selection)
                except Exception:
                    pass

            return {
                "success": True,
                "physics": {
                    "name": physics_java.label() if hasattr(physics_java, 'label') else "Solid Mechanics",
                    "type": "SolidMechanics",
                    "tag": "solid",
                    "domain_selection": domain_selection,
                }
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to add Solid Mechanics: {str(e)}"}
    
    @mcp.tool()
    def physics_add_heat_transfer(
        domain_selection: Optional[str] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Add Heat Transfer physics for thermal analysis.

        Args:
            domain_selection: Selection name for domains (default: all domains)
            model_name: Model name (default: current model)

        Returns:
            Created physics info
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            jm = model.java
            comp = _first_component(jm)
            if comp is None:
                return {"success": False, "error": "No component found in model."}
            physics_java = comp.physics().create("ht", "HeatTransfer", _component_sdim(comp))

            if domain_selection:
                try:
                    physics_java.selection().set(domain_selection)
                except Exception:
                    pass

            return {
                "success": True,
                "physics": {
                    "name": physics_java.label() if hasattr(physics_java, 'label') else "Heat Transfer",
                    "type": "HeatTransfer",
                    "tag": "ht",
                    "domain_selection": domain_selection,
                }
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to add Heat Transfer: {str(e)}"}
    
    @mcp.tool()
    def physics_add_laminar_flow(
        domain_selection: Optional[str] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Add Laminar Flow physics for fluid dynamics.

        Args:
            domain_selection: Selection name for domains (default: all domains)
            model_name: Model name (default: current model)

        Returns:
            Created physics info
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            jm = model.java
            comp = _first_component(jm)
            if comp is None:
                return {"success": False, "error": "No component found in model."}
            physics_java = comp.physics().create("spf", "LaminarFlow", _component_sdim(comp))

            if domain_selection:
                try:
                    physics_java.selection().set(domain_selection)
                except Exception:
                    pass

            return {
                "success": True,
                "physics": {
                    "name": physics_java.label() if hasattr(physics_java, 'label') else "Laminar Flow",
                    "type": "LaminarFlow",
                    "tag": "spf",
                    "domain_selection": domain_selection,
                }
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to add Laminar Flow: {str(e)}"}

    @mcp.tool()
    def physics_add_domain_feature(
        physics_name: str,
        feature_type: str,
        domain_selection: Sequence[int],
        properties: Optional[dict] = None,
        feature_tag: Optional[str] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Add a domain feature (e.g. ChargeConservation) to a physics interface.

        In COMSOL 6.3+/6.4 several physics interfaces ship with a default domain
        feature that ignores material properties. For Electrostatics the
        default is FreeSpace (fsp1, vacuum eps0); to use a dielectric you must
        add a ChargeConservation feature:
            feature_type="ChargeConservation", properties={"materialType": "from_mat"}
        or hardcode the relative permittivity:
            properties={"epsilonr": "2.1"}  (requires materialType != from_mat)

        Common domain feature types:
        - Electrostatics: "ChargeConservation"
        - Heat Transfer: "Solid", "Fluid", "ThinLayer"
        - Solid Mechanics: "LinearElasticMaterial", "RigidDomain"

        Args:
            physics_name: Name or label of the physics interface
            feature_type: Domain feature type (e.g. "ChargeConservation")
            domain_selection: Domain numbers to apply the feature to
            properties: Dictionary of property names and values
            feature_tag: Tag for the feature (auto-generated if None)
            model_name: Model name (default: current model)

        Returns:
            Created domain feature info
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {"success": False, "error": f"Model not found: {model_name or 'no current model'}"}

        properties = properties or {}

        try:
            jm = model.java
            physics_java = _find_physics_java(jm, physics_name)
            if physics_java is None:
                return {"success": False, "error": f"Physics interface not found: {physics_name}"}

            # spatial dimension of the physics interface (domain features use int sdim)
            sdim = 3
            try:
                comp = _first_component(jm)
                if comp is not None:
                    gtags = list(comp.geom().tags())
                    if gtags:
                        sdim = int(comp.geom(gtags[0]).getSDim())
            except Exception:
                pass

            tag = feature_tag or _make_tag(feature_type.lower())
            feat = physics_java.feature().create(tag, feature_type, sdim)
            feat.selection().set([int(d) for d in domain_selection])

            set_failures = []
            for prop_name, prop_value in properties.items():
                try:
                    feat.set(prop_name, prop_value)
                except Exception as exc:
                    set_failures.append(f"{prop_name}: {exc}")

            result = {
                "success": True,
                "domain_feature": {
                    "tag": tag,
                    "type": feature_type,
                    "physics": physics_name,
                    "selection": list(domain_selection),
                    "properties": properties,
                    "sdim": sdim,
                }
            }
            if set_failures:
                result["warning"] = (
                    "Some property sets failed (property names may be wrong "
                    f"for this feature type): {set_failures}"
                )
            return result
        except Exception as e:
            return {"success": False, "error": f"Failed to add domain feature: {str(e)}"}

    @mcp.tool()
    def physics_configure_boundary(
        physics_name: str,
        boundary_condition: str,
        boundary_selection: Sequence[int],
        properties: Optional[dict] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Configure a boundary condition for a physics interface.

        Common boundary conditions for Heat Transfer:
        - "Temperature": Fixed temperature
        - "HeatFlux": Heat flux boundary
        - "ConvectiveHeatFlux": Convection cooling
        - "ThermalInsulation": Thermal insulation (adiabatic)

        Common for Solid Mechanics:
        - "Fixed": Fixed constraint
        - "Roller": Roller constraint
        - "Symmetry": Symmetry plane
        - "BoundaryLoad": Applied force/pressure

        Common for Electrostatics:
        - "Ground": Zero potential boundary
        - "ElectricPotential": Specified voltage
        - "SurfaceChargeDensity": Surface charge
        - "ZeroCharge": Zero normal displacement field

        Args:
            physics_name: Name or label of the physics interface
            boundary_condition: Type of boundary condition (e.g. "Temperature", "HeatFlux")
            boundary_selection: Boundary/edge numbers to apply condition to
            properties: Dictionary of property names and values (e.g. {"T0": "293.15[K]"})
            model_name: Model name (default: current model)

        Returns:
            Created boundary condition info
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        properties = properties or {}

        try:
            jm = model.java

            return add_boundary_condition(
                model,
                physics_name,
                boundary_condition,
                boundary_selection,
                properties=properties,
            )
        except Exception as e:
            return {"success": False, "error": f"Failed to configure boundary: {str(e)}"}
    
    @mcp.tool()
    def physics_set_material(
        physics_name: str,
        material_name: str,
        domain_selection: Optional[Sequence[int]] = None,
        properties: Optional[dict] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Assign a material to physics domains.

        This tool tries to add the material from COMSOL's built-in library
        if it's not already in the model. If ``properties`` is provided those
        values are written into the material's Basic property group (tag
        ``def``) so domain features with ``materialType=from_mat`` will use
        them.

        Recognised property names (Heat Transfer / Electrostatics):
        - "thermalconductivity": e.g. "130[W/(m*K)]"
        - "density":              e.g. "2329[kg/m^3]"
        - "heatcapacity":         e.g. "700[J/(kg*K)]"
        - "relpermittivity":      e.g. "2.1"   (Electrostatics)

        Args:
            physics_name: Name of the physics interface
            material_name: Name of the material (e.g. "Silicon", "Steel AISI 4340", "Copper")
            domain_selection: Domain numbers (default: all domains for this physics)
            properties: Optional dict of Basic material properties to write
            model_name: Model name (default: current model)

        Returns:
            Assignment confirmation
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            jm = model.java
            materials = model.materials()
            tag = material_name.replace(" ", "_").replace("-", "_")

            if material_name not in materials:
                comp = _first_component(jm)
                if comp is None:
                    return {"success": False, "error": "No component found in model."}
                try:
                    mat = comp.material().create(tag, "Common")
                    mat.label(material_name)
                except Exception as e:
                    return {"success": False, "error": f"Could not create material node: {str(e)}"}

            physics_java = _find_physics_java(jm, physics_name)

            if physics_java is None:
                return {"success": False, "error": f"Physics interface not found: {physics_name}"}

            mat_node = comp.material(tag)
            if domain_selection:
                mat_node.selection().set([int(d) for d in domain_selection])

            set_warnings = []
            if properties:
                try:
                    grp = mat_node.propertyGroup("def")
                except Exception as exc:
                    return {
                        "success": False,
                        "error": (
                            f"Material '{tag}' has no 'def' property group to "
                            f"write physical properties into: {exc}"
                        ),
                    }
                for prop_name, prop_value in properties.items():
                    try:
                        # vector/scalar form: COMSOL accepts a single-element
                        # string array for scalar-anisotropic prop names.
                        grp.set(prop_name, [prop_value])
                    except Exception:
                        try:
                            grp.set(prop_name, prop_value)
                        except Exception as exc:
                            set_warnings.append(f"{prop_name}: {exc}")

            result = {
                "success": True,
                "material": material_name,
                "physics": physics_name,
                "domain_selection": list(domain_selection) if domain_selection else "all",
                "message": f"Material '{material_name}' assigned to physics '{physics_name}'",
            }
            if set_warnings:
                result["warning"] = (
                    "Some material property sets failed (check property "
                    f"names): {set_warnings}"
                )
            return result
        except Exception as e:
            return {"success": False, "error": f"Failed to set material: {str(e)}"}
    
    @mcp.tool()
    def multiphysics_add(
        coupling_type: str,
        physics_list: Sequence[str],
        model_name: Optional[str] = None
    ) -> dict:
        """
        Add a multiphysics coupling between physics interfaces.
        
        Common coupling types:
        - "ThermalStress": Couples Heat Transfer and Solid Mechanics
        - "FluidStructureInteraction": Couples Fluid Flow and Solid Mechanics
        - "ElectromechanicalForces": Couples Electrostatics and Solid Mechanics
        - "JouleHeating": Couples Electric Currents and Heat Transfer
        
        Args:
            coupling_type: Type of multiphysics coupling
            physics_list: Names of physics interfaces to couple
            model_name: Model name (default: current model)
        
        Returns:
            Created coupling info
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            coupling_node = model.create("multiphysics", coupling_type)
            
            return {
                "success": True,
                "coupling": {
                    "name": coupling_node.name() if hasattr(coupling_node, 'name') else coupling_type,
                    "type": coupling_type,
                    "physics": list(physics_list),
                }
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to add multiphysics: {str(e)}"}
    
    @mcp.tool()
    def physics_list_features(
        physics_name: str,
        model_name: Optional[str] = None
    ) -> dict:
        """
        List all features (boundary conditions, domain settings) in a physics interface.
        
        Args:
            physics_name: Name of the physics interface
            model_name: Model name (default: current model)
        
        Returns:
            List of physics features
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            return list_physics_features(model, physics_name)
        except Exception as e:
            return {"success": False, "error": f"Failed to list features: {str(e)}"}
    
    @mcp.tool()
    def physics_remove(
        physics_name: str,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Remove a physics interface from the model.
        
        Args:
            physics_name: Name of the physics interface to remove
            model_name: Model name (default: current model)
        
        Returns:
            Removal confirmation
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            return remove_physics_interface(model, physics_name)
        except Exception as e:
            return {"success": False, "error": f"Failed to remove physics: {str(e)}"}
    
    @mcp.tool()
    def geometry_get_boundaries(
        geometry_name: Optional[str] = None,
        component_name: str = "comp1",
        model_name: Optional[str] = None
    ) -> dict:
        """
        Get all boundaries from a geometry with their properties.

        Use this to identify which boundary numbers correspond to which faces
        before setting boundary conditions.

        Args:
            geometry_name: Geometry sequence name (default: first geometry)
            component_name: Component name (default: 'comp1')
            model_name: Model name (default: current model)

        Returns:
            List of boundaries with their numbers and areas
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            import jpype as _jpype
            jm = model.java

            comp = jm.component(component_name)
            if comp is None:
                return {"success": False, "error": f"Component '{component_name}' not found."}

            geom_list = comp.geom()
            if geom_list.size() == 0:
                return {"success": False, "error": "No geometries in component."}
            geom_tag = geometry_name
            if not geom_tag:
                # clientapi: list is not subscriptable; use tags().
                tags = list(geom_list.tags())
                geom_tag = tags[0]
            geom = comp.geom(geom_tag)
            geom.run()

            nboundary = geom.getNBoundaries()
            ndomain = geom.getNDomains()
            sdim = int(geom.getSDim())

            # Whole-geometry bounding box [xmin,xmax,ymin,ymax,zmin,zmax].
            try:
                bbox = [float(x) for x in geom.getBoundingBox()]
            except Exception:
                bbox = None

            boundaries = []
            if sdim == 3:
                for i in range(1, nboundary + 1):
                    info = {"boundary_number": i}
                    try:
                        pr = list(geom.faceParamRange(i))
                        u_mid = (float(pr[0]) + float(pr[1])) / 2.0
                        v_mid = (float(pr[2]) + float(pr[3])) / 2.0
                        pp = _jpype.JArray(_jpype.JArray(_jpype.JDouble))(1)
                        pp[0] = _jpype.JArray(_jpype.JDouble)([u_mid, v_mid])
                        normal = list(geom.faceNormal(i, pp)[0])
                        center = list(geom.faceX(i, pp)[0])
                        info["normal"] = [float(n) for n in normal]
                        info["center"] = [float(c) for c in center]
                    except Exception as e:
                        info["error"] = f"Could not get face info: {str(e)[:80]}"
                    boundaries.append(info)
            elif sdim == 2:
                for i in range(1, nboundary + 1):
                    info = {"boundary_number": i}
                    try:
                        pr = list(geom.edgeParamRange(i))
                        u_mid = (float(pr[0]) + float(pr[1])) / 2.0
                        pp = _jpype.JArray(_jpype.JDouble)([u_mid])
                        normal = list(geom.edgeNormal(i, pp)[0])
                        center = list(geom.edgeX(i, pp)[0])
                        info["normal"] = [float(n) for n in normal]
                        info["center"] = [float(c) for c in center]
                    except Exception as e:
                        info["error"] = f"Could not get edge info: {str(e)[:80]}"
                    boundaries.append(info)
            else:
                for i in range(1, nboundary + 1):
                    boundaries.append({"boundary_number": i})

            result = {
                "success": True,
                "geometry": geom_tag,
                "space_dimension": sdim,
                "total_boundaries": nboundary,
                "total_domains": ndomain,
                "boundaries": boundaries,
                "hint": "Use 'normal' to identify faces (e.g. z=0 face has normal [0,0,-1]); "
                        "use 'center' to confirm by coordinate. Then set BCs via physics_configure_boundary.",
            }
            if bbox is not None:
                result["bounding_box"] = bbox
            return result
        except Exception as e:
            return {"success": False, "error": f"Failed to get boundaries: {str(e)}"}
    
    @mcp.tool()
    def physics_interactive_setup_flow(
        physics_name: str = "Laminar Flow",
        model_name: Optional[str] = None
    ) -> dict:
        """
        Interactive setup wizard for Laminar Flow boundary conditions.
        
        This tool helps identify and configure flow boundary conditions:
        1. Lists all available boundaries
        2. Prompts user to select inlet, outlet, and wall boundaries
        3. Configures appropriate boundary conditions
        
        Args:
            physics_name: Name of the Laminar Flow physics interface
            model_name: Model name (default: current model)
        
        Returns:
            Boundary information and setup instructions
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            # Get geometry boundaries
            boundaries_info = geometry_get_boundaries(None, model_name)
            if not boundaries_info.get("success"):
                return boundaries_info
            
            return {
                "success": True,
                "message": "Interactive Flow Setup - Please specify boundaries",
                "available_boundaries": boundaries_info["total_boundaries"],
                "boundaries": boundaries_info["boundaries"],
                "setup_instructions": {
                    "step1": "Identify which boundary numbers are INLETS (flow enters)",
                    "step2": "Identify which boundary numbers are OUTLETS (flow exits)",
                    "step3": "Use physics_configure_boundary to set conditions",
                },
                "boundary_condition_types": {
                    "InletBoundary": "Set inlet velocity (U0 parameter)",
                    "OutletBoundary": "Set outlet pressure (p0 parameter, default 0)",
                    "Wall": "No-slip wall (default for unspecified boundaries)",
                    "Symmetry": "Symmetry plane",
                },
                "example_usage": {
                    "inlet": "physics_configure_boundary(physics_name='Laminar Flow', boundary_condition='InletBoundary', boundary_selection=[1, 2], properties={'U0': '1[mm/s]'})",
                    "outlet": "physics_configure_boundary(physics_name='Laminar Flow', boundary_condition='OutletBoundary', boundary_selection=[3])",
                },
                "next_step": "Please tell me which boundary numbers to use for inlet(s) and outlet(s)",
            }
        except Exception as e:
            return {"success": False, "error": f"Interactive setup failed: {str(e)}"}
    
    @mcp.tool()
    def physics_setup_flow_boundaries(
        physics_name: str,
        inlet_boundaries: Sequence[int],
        outlet_boundaries: Sequence[int],
        inlet_velocity: str = "1[mm/s]",
        outlet_pressure: str = "0",
        model_name: Optional[str] = None
    ) -> dict:
        """
        Setup Laminar Flow boundary conditions with specified boundaries.
        
        This tool configures inlet velocity and outlet pressure boundary conditions
        for a fluid flow simulation.
        
        Args:
            physics_name: Name of the Laminar Flow physics interface
            inlet_boundaries: List of boundary numbers for inlets
            outlet_boundaries: List of boundary numbers for outlets
            inlet_velocity: Inlet velocity expression (default: "1[mm/s]")
            outlet_pressure: Outlet pressure expression (default: "0")
            model_name: Model name (default: current model)
        
        Returns:
            Configuration confirmation
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            jm = model.java
            
            # Find physics in component
            physics_interfaces = model.physics()
            if physics_name not in physics_interfaces:
                return {"success": False, "error": f"Physics '{physics_name}' not found. Available: {physics_interfaces}"}
            
            # Get component and physics
            physics_java = _find_physics_java(jm, physics_name)

            if physics_java is None:
                return {"success": False, "error": f"Could not find physics interface: {physics_name}"}

            results = {"inlets": [], "outlets": []}

            for i, boundary in enumerate(inlet_boundaries):
                inlet_tag = _make_tag("inl")
                inlet = physics_java.create(inlet_tag, 'InletBoundary')
                inlet.selection().set([int(boundary)])
                inlet.set('U0', inlet_velocity)
                inlet.label(f'Inlet {i+1} (Boundary {boundary})')
                results["inlets"].append({
                    "tag": inlet_tag,
                    "boundary": boundary,
                    "velocity": inlet_velocity
                })
            
            for i, boundary in enumerate(outlet_boundaries):
                outlet_tag = _make_tag("out")
                outlet = physics_java.create(outlet_tag, 'OutletBoundary')
                outlet.selection().set([int(boundary)])
                outlet.set('p0', outlet_pressure)
                outlet.label(f'Outlet {i+1} (Boundary {boundary})')
                results["outlets"].append({
                    "tag": outlet_tag,
                    "boundary": boundary,
                    "pressure": outlet_pressure
                })
            
            return {
                "success": True,
                "physics": physics_name,
                "configured_boundaries": results,
                "inlet_velocity": inlet_velocity,
                "outlet_pressure": outlet_pressure,
                "message": f"Configured {len(inlet_boundaries)} inlet(s) and {len(outlet_boundaries)} outlet(s)",
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to setup boundaries: {str(e)}"}

    @mcp.tool()
    def physics_interactive_setup_heat(
        physics_name: str = "Heat Transfer in Solids",
        model_name: Optional[str] = None
    ) -> dict:
        """
        Interactive setup wizard for Heat Transfer boundary conditions.
        
        This tool helps identify and configure thermal boundary conditions:
        1. Lists all available boundaries
        2. Shows typical boundary condition types for thermal analysis
        3. Provides setup instructions
        
        Args:
            physics_name: Name of the Heat Transfer physics interface
            model_name: Model name (default: current model)
        
        Returns:
            Boundary information and setup instructions
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            boundaries_info = geometry_get_boundaries(None, model_name)
            if not boundaries_info.get("success"):
                return boundaries_info
            
            return {
                "success": True,
                "message": "Interactive Heat Transfer Setup",
                "available_boundaries": boundaries_info["total_boundaries"],
                "boundaries": boundaries_info["boundaries"],
                "boundary_condition_types": {
                    "TemperatureBoundary": "Fixed temperature (heat sink/source)",
                    "HeatFluxBoundary": "Prescribed heat flux (heat source)",
                    "ConvectiveHeatFlux": "Convection cooling/heating",
                    "Symmetry": "Symmetry plane (adiabatic)",
                    "ThermalInsulation": "Thermal insulation (default)"
                },
                "typical_setup": {
                    "heat_source": "Use HeatFluxBoundary with q0 parameter (W/m^2)",
                    "heat_sink": "Use TemperatureBoundary with T0 parameter (K or degC)",
                    "convection": "Use ConvectiveHeatFlux with h and Text parameters"
                },
                "example_usage": {
                    "heat_source": "physics_setup_heat_boundaries(physics_name='Heat Transfer in Solids', heat_flux_boundaries=[1, 2], heat_flux_value='1e6[W/m^2]')",
                    "heat_sink": "physics_setup_heat_boundaries(physics_name='Heat Transfer in Solids', temperature_boundaries=[3], temperature_value='293.15[K]')"
                },
                "next_step": "Tell me which boundary numbers to use for heat source and heat sink",
            }
        except Exception as e:
            return {"success": False, "error": f"Interactive setup failed: {str(e)}"}

    @mcp.tool()
    def physics_setup_heat_boundaries(
        physics_name: str,
        heat_flux_boundaries: Optional[Sequence[int]] = None,
        temperature_boundaries: Optional[Sequence[int]] = None,
        convection_boundaries: Optional[Sequence[int]] = None,
        heat_flux_value: str = "1e6[W/m^2]",
        temperature_value: str = "293.15[K]",
        convection_coeff: str = "10[W/(m^2*K)]",
        ambient_temp: str = "293.15[K]",
        model_name: Optional[str] = None
    ) -> dict:
        """
        Setup Heat Transfer boundary conditions with specified boundaries.
        
        This tool configures thermal boundary conditions for heat transfer simulation:
        - Heat flux boundaries (heat sources)
        - Temperature boundaries (heat sinks)
        - Convective cooling/heating boundaries
        
        Args:
            physics_name: Name of the Heat Transfer physics interface
            heat_flux_boundaries: List of boundary numbers for heat flux
            temperature_boundaries: List of boundary numbers for fixed temperature
            convection_boundaries: List of boundary numbers for convection
            heat_flux_value: Heat flux value (default: "1e6[W/m^2]")
            temperature_value: Temperature value (default: "293.15[K]" = 20°C)
            convection_coeff: Convection coefficient (default: "10[W/(m^2*K)]")
            ambient_temp: Ambient temperature for convection (default: "293.15[K]")
            model_name: Model name (default: current model)
        
        Returns:
            Configuration confirmation
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        heat_flux_boundaries = heat_flux_boundaries or []
        temperature_boundaries = temperature_boundaries or []
        convection_boundaries = convection_boundaries or []

        try:
            jm = model.java

            physics_interfaces = model.physics()
            if physics_name not in physics_interfaces:
                return {"success": False, "error": f"Physics '{physics_name}' not found. Available: {physics_interfaces}"}

            physics_java = _find_physics_java(jm, physics_name)

            if physics_java is None:
                return {"success": False, "error": f"Could not find physics interface: {physics_name}"}

            results = {"heat_flux": [], "temperature": [], "convection": []}

            for i, boundary in enumerate(heat_flux_boundaries):
                tag = _make_tag("hf")
                bc = physics_java.create(tag, 'HeatFluxBoundary')
                bc.selection().set([int(boundary)])
                bc.set('q0', heat_flux_value)
                bc.label(f'Heat Flux {i+1} (Boundary {boundary})')
                results["heat_flux"].append({
                    "tag": tag,
                    "boundary": boundary,
                    "heat_flux": heat_flux_value
                })
            
            for i, boundary in enumerate(temperature_boundaries):
                tag = _make_tag("temp")
                bc = physics_java.create(tag, 'TemperatureBoundary')
                bc.selection().set([int(boundary)])
                bc.set('T0', temperature_value)
                bc.label(f'Temperature {i+1} (Boundary {boundary})')
                results["temperature"].append({
                    "tag": tag,
                    "boundary": boundary,
                    "temperature": temperature_value
                })
            
            for i, boundary in enumerate(convection_boundaries):
                tag = _make_tag("conv")
                bc = physics_java.create(tag, 'ConvectiveHeatFlux')
                bc.selection().set([int(boundary)])
                bc.set('h', convection_coeff)
                bc.set('Text', ambient_temp)
                bc.label(f'Convection {i+1} (Boundary {boundary})')
                results["convection"].append({
                    "tag": tag,
                    "boundary": boundary,
                    "h": convection_coeff,
                    "T_amb": ambient_temp
                })
            
            return {
                "success": True,
                "physics": physics_name,
                "configured_boundaries": results,
                "summary": {
                    "heat_flux_boundaries": len(heat_flux_boundaries),
                    "temperature_boundaries": len(temperature_boundaries),
                    "convection_boundaries": len(convection_boundaries)
                },
                "message": f"Configured {len(heat_flux_boundaries)} heat flux, {len(temperature_boundaries)} temperature, and {len(convection_boundaries)} convection boundaries",
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to setup heat boundaries: {str(e)}"}

    @mcp.tool()
    def physics_boundary_selection(
        physics_name: str,
        boundary_condition_type: str,
        boundary_numbers: Sequence[int],
        properties: Optional[dict] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Generic boundary condition setup with boundary selection.

        Use this tool to configure any boundary condition by specifying:
        1. The physics interface name
        2. The boundary condition type
        3. The boundary numbers to apply the condition to
        4. Properties specific to the boundary condition

        Common boundary condition types by physics:

        Heat Transfer (ht):
        - Temperature: Set T0 (temperature)
        - HeatFlux: Set q0 (heat flux)
        - ConvectiveHeatFlux: Set h (coefficient), Text (ambient temp)

        Laminar Flow (spf):
        - InletBoundary: Set U0 (velocity)
        - OutletBoundary: Set p0 (pressure)
        - Wall: No-slip wall

        Solid Mechanics (solid):
        - Fixed: Fixed constraint
        - BoundaryLoad: Set Fx, Fy, Fz or FAx, FAy, FAz

        Args:
            physics_name: Name or label of the physics interface
            boundary_condition_type: Type of boundary condition
            boundary_numbers: List of boundary numbers
            properties: Dictionary of property names and values
            model_name: Model name (default: current model)

        Returns:
            Configuration confirmation
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        properties = properties or {}

        try:
            return add_boundary_condition(
                model,
                physics_name,
                boundary_condition_type,
                boundary_numbers,
                properties=properties,
            )
        except Exception as e:
            return {"success": False, "error": f"Failed to create boundary condition: {str(e)}"}


