"""Bounded Electrochemistry Module tools for the isolated ``electro_chemistry`` profile.

This surface is intentionally minimal: discovery, interface creation,
electrode-reaction configuration, electrolyte setup, and inspection. It never
starts a solver, never selects mesh/cores/OOC, and never acquires a license.
Missing COMSOL Electrochemistry Module products return explicit errors instead
of silent fallbacks.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, Optional

from mcp.server.mcpserver import MCPServer

from comsol_mcp.utils.validation import strict_json_integer

from .physics import _component_sdim, _first_component
from .session import session_manager

_TAG = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_MAX_SELECTION_ITEMS = 256
_MAX_LABEL_BYTES = 512
_MAX_REACTIONS = 8

# Declared COMSOL products/modules. Availability is checked only through an
# explicit live create/inspect path; offline catalog never fabricates a license.
REQUIRED_PRODUCTS = (
    "COMSOL Multiphysics",
    "Electrochemistry Module",
)

# Minimal verifiable interface set. Tags are the documented clientapi defaults.
# Live COMSOL 6.4.0.293 probe (2026-09-22): SecondaryCurrentDistribution creates;
# the other listed type names are rejected as unknown interfaces on that build.
ELECTROCHEMISTRY_INTERFACES: dict[str, dict[str, Any]] = {
    "secondary_current": {
        "interface_type": "SecondaryCurrentDistribution",
        "default_tag": "sec",
        "title": "Secondary Current Distribution",
        "required_products": REQUIRED_PRODUCTS,
        "evidence_status": "live_comsol_6_4_created",
    },
    "tertiary_current": {
        "interface_type": "TertiaryCurrentDistribution",
        "default_tag": "te",
        "title": "Tertiary Current Distribution",
        "required_products": REQUIRED_PRODUCTS,
        "evidence_status": "type_name_rejected_on_comsol_6_4_0_293",
    },
    "electroanalysis": {
        "interface_type": "Electroanalysis",
        "default_tag": "el",
        "title": "Electroanalysis",
        "required_products": REQUIRED_PRODUCTS,
        "evidence_status": "type_name_rejected_on_comsol_6_4_0_293",
    },
}

ELECTRODE_BOUNDARY_CONDITIONS: dict[str, dict[str, Any]] = {
    "electrode_surface": {
        "feature_type": "ElectrodeSurface",
        "default_tag": "es",
        "title": "Electrode Surface",
        "properties": ("rhos",),
        "evidence_status": "live_comsol_6_4_created",
    },
    "electrolyte_potential": {
        "feature_type": "ElectrolytePotential",
        "default_tag": "ep",
        "title": "Electrolyte Potential",
        "properties": ("phis0",),
        "evidence_status": "live_comsol_6_4_created",
    },
    "electrode_potential": {
        "feature_type": "ElectrodePotential",
        "default_tag": "epd",
        "title": "Electrode Potential",
        "properties": ("phil0",),
        "evidence_status": "live_comsol_6_4_created",
    },
    "insulation": {
        "feature_type": "Insulation",
        "default_tag": "ins",
        "title": "Insulation / Zero Flux",
        "properties": (),
        "evidence_status": "default_feature_on_secondary_current",
    },
}

# SecondaryCurrentDistribution ElectrodeSurface is a surface-resistance boundary.
# Butler-Volmer/Tafel kinetics are not exposed on that feature on COMSOL 6.4.0.293.
ELECTRODE_REACTION_MODELS = (
    "surface_resistance",
    "butler_volmer",
    "tafel",
    "linearized",
    "custom_expression",
)
_SECONDARY_SUPPORTED_REACTION_MODELS = frozenset({"surface_resistance"})


def _bounded_tag(value: str, name: str) -> str:
    if not isinstance(value, str) or not _TAG.fullmatch(value):
        raise ValueError(f"{name} must be one exact clientapi tag")
    return value


def _bounded_label(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("label must be nonempty text")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError("label must be valid UTF-8") from exc
    if size > _MAX_LABEL_BYTES:
        raise ValueError(f"label exceeds {_MAX_LABEL_BYTES} bytes")
    return value


def _positive_finite(value: Optional[float], name: str) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a positive finite number")
    number = float(value)
    if not (number > 0.0) or number == float("inf") or number != number:
        raise ValueError(f"{name} must be a positive finite number")
    return number


def _nonnegative_finite(value: Optional[float], name: str) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a nonnegative finite number")
    number = float(value)
    if number < 0.0 or number == float("inf") or number != number:
        raise ValueError(f"{name} must be a nonnegative finite number")
    return number


def _normalized_entities(
    values: Optional[Sequence[int]],
    *,
    required: bool,
    label: str,
) -> list[int]:
    if values is not None and isinstance(values, (str, bytes)):
        raise TypeError(f"{label} must be an integer array")
    entities = (
        [strict_json_integer(value, label, minimum=1) for value in values]
        if values is not None
        else []
    )
    if len(entities) > _MAX_SELECTION_ITEMS:
        raise ValueError(f"{label} may contain at most {_MAX_SELECTION_ITEMS} items")
    if len(entities) != len(set(entities)):
        raise ValueError(f"{label} must not contain duplicate entity numbers")
    if required and not entities:
        raise ValueError(f"{label} is required")
    return entities


def _require_model(model_name: Optional[str] = None) -> Any:
    model = session_manager.get_model(model_name)
    if model is None:
        raise ValueError(f"Model not found: {model_name or 'no current model'}")
    return model


def build_electro_chemistry_catalog() -> dict[str, Any]:
    """Return the offline electrochemistry capability catalog without Java."""
    return {
        "success": True,
        "profile": "electro_chemistry",
        "required_products": list(REQUIRED_PRODUCTS),
        "interfaces": {
            key: {
                "interface_type": spec["interface_type"],
                "default_tag": spec["default_tag"],
                "title": spec["title"],
                "required_products": list(spec["required_products"]),
                "evidence_status": spec["evidence_status"],
            }
            for key, spec in ELECTROCHEMISTRY_INTERFACES.items()
        },
        "electrode_boundary_conditions": {
            key: {
                "feature_type": spec["feature_type"],
                "default_tag": spec["default_tag"],
                "title": spec["title"],
                "properties": list(spec["properties"]),
                "evidence_status": spec.get("evidence_status", "declared_not_licensed_verified"),
            }
            for key, spec in ELECTRODE_BOUNDARY_CONDITIONS.items()
        },
        "electrode_reaction_models": list(ELECTRODE_REACTION_MODELS),
        "secondary_current_supported_reaction_models": sorted(_SECONDARY_SUPPORTED_REACTION_MODELS),
        "resource_policy": {
            "starts_solver": False,
            "selects_mesh": False,
            "selects_cores": False,
            "selects_ooc": False,
            "acquires_license": False,
            "source_models_immutable": True,
        },
        "notes": (
            "Catalog data is declared design surface only. Live module availability "
            "and Java tags require an authorized COMSOL 6.4 Electrochemistry Module "
            "probe; COMSOL 6.3 upstream records are not accepted as evidence."
        ),
    }


def _physics_by_tag(model: Any, physics_tag: str) -> Any:
    component = _first_component(model.java)
    if component is None:
        raise ValueError("No component found in model.")
    for raw_tag in list(component.physics().tags()):
        if str(raw_tag) == physics_tag:
            return component, component.physics().get(physics_tag)
    return component, None


def inspect_electrochemistry(
    *,
    model_name: Optional[str] = None,
    physics_tag: str = "sec",
) -> dict[str, Any]:
    """Inspect one electrochemistry interface tree without mutation."""
    try:
        tag = _bounded_tag(physics_tag, "physics_tag")
        model = _require_model(model_name)
        component, physics = _physics_by_tag(model, tag)
    except (TypeError, ValueError) as exc:
        return {"success": False, "error": str(exc)}

    if physics is None:
        return {
            "success": False,
            "error": f"Electrochemistry interface not found: {tag}",
            "required_products": list(REQUIRED_PRODUCTS),
        }

    features: list[dict[str, Any]] = []
    feature_list = physics.feature()
    for raw_feature_tag in list(feature_list.tags()):
        feature_tag = str(raw_feature_tag)
        feature = feature_list.get(feature_tag)
        info: dict[str, Any] = {"tag": feature_tag}
        try:
            info["label"] = str(feature.label())
        except Exception:
            info["label"] = feature_tag
        try:
            info["type"] = str(feature.getType()) if hasattr(feature, "getType") else None
        except Exception:
            info["type"] = None
        try:
            info["selection"] = [int(value) for value in list(feature.selection().entities())]
        except Exception:
            info["selection"] = None
        features.append(info)

    try:
        interface_type = str(physics.getType()) if hasattr(physics, "getType") else None
    except Exception:
        interface_type = None

    return {
        "success": True,
        "profile": "electro_chemistry",
        "model_name": model_name,
        "component": str(component.tag()),
        "physics": {
            "tag": tag,
            "label": str(physics.label()) if hasattr(physics, "label") else tag,
            "interface_type": interface_type,
        },
        "features": features,
        "count": len(features),
        "evidence_status": "label_and_tag_only",
    }


def add_electrochemistry_interface(
    model: Any,
    *,
    interface_key: str = "secondary_current",
    physics_tag: Optional[str] = None,
    domain_numbers: Optional[Sequence[int]] = None,
    label: Optional[str] = None,
) -> dict[str, Any]:
    """Create one electrochemistry physics interface as a rollback-safe transaction."""
    try:
        if interface_key not in ELECTROCHEMISTRY_INTERFACES:
            raise ValueError(
                f"interface_key must be one of: {', '.join(sorted(ELECTROCHEMISTRY_INTERFACES))}"
            )
        tag = (
            _bounded_tag(physics_tag, "physics_tag")
            if physics_tag is not None
            else str(ELECTROCHEMISTRY_INTERFACES[interface_key]["default_tag"])
        )
        safe_label = _bounded_label(label)
        domains = _normalized_entities(domain_numbers, required=False, label="domain_numbers")
    except (TypeError, ValueError) as exc:
        return {"success": False, "error": str(exc)}

    component = _first_component(model.java)
    if component is None:
        return {"success": False, "error": "No component found in model."}
    existing = {str(value) for value in list(component.physics().tags())}
    if tag in existing:
        return {"success": False, "error": f"Physics tag already exists: {tag}"}

    spec = ELECTROCHEMISTRY_INTERFACES[interface_key]
    physics_created = False
    try:
        physics_java = component.physics().create(
            tag, str(spec["interface_type"]), _component_sdim(component)
        )
        physics_created = True
        if safe_label:
            physics_java.label(safe_label)
        if domains:
            physics_java.selection().set(domains)
        return {
            "success": True,
            "profile": "electro_chemistry",
            "physics": {
                "tag": tag,
                "interface_type": str(spec["interface_type"]),
                "interface_key": interface_key,
                "label": safe_label or tag,
                "domain_numbers": domains or "all",
                "required_products": list(spec["required_products"]),
            },
            "evidence_status": "created_pending_licensed_probe",
        }
    except Exception as exc:
        rolled_back = True
        if physics_created:
            try:
                component.physics().remove(tag)
            except Exception:
                rolled_back = False
        message = str(exc).strip() or "Electrochemistry interface creation failed."
        return {
            "success": False,
            "error": (
                "Electrochemistry interface creation failed. Confirm the COMSOL "
                f"Electrochemistry Module is installed and licensed. Detail: {message}"
            ),
            "required_products": list(REQUIRED_PRODUCTS),
            "rolled_back": rolled_back,
        }


def configure_electrode_reaction(
    model: Any,
    *,
    physics_tag: str = "sec",
    boundary_numbers: Sequence[int],
    reaction_model: str = "surface_resistance",
    feature_tag: Optional[str] = None,
    surface_resistivity: Optional[float] = None,
    exchange_current_density: Optional[float] = None,
    anodic_charge_transfer_coefficient: Optional[float] = None,
    cathodic_charge_transfer_coefficient: Optional[float] = None,
    label: Optional[str] = None,
) -> dict[str, Any]:
    """Create and configure one ElectrodeSurface boundary as one transaction.

    On SecondaryCurrentDistribution (COMSOL 6.4.0.293) this feature is a
    surface-resistance boundary. Kinetics parameters are rejected explicitly
    rather than written to unknown properties.
    """
    try:
        tag = _bounded_tag(physics_tag, "physics_tag")
        boundaries = _normalized_entities(boundary_numbers, required=True, label="boundary_numbers")
        if reaction_model not in ELECTRODE_REACTION_MODELS:
            raise ValueError(
                f"reaction_model must be one of: {', '.join(ELECTRODE_REACTION_MODELS)}"
            )
        feature = _bounded_tag(feature_tag, "feature_tag") if feature_tag is not None else "es1"
        safe_label = _bounded_label(label)
        rhos = _positive_finite(surface_resistivity, "surface_resistivity")
        i0 = _positive_finite(exchange_current_density, "exchange_current_density")
        alpha_a = _nonnegative_finite(
            anodic_charge_transfer_coefficient, "anodic_charge_transfer_coefficient"
        )
        alpha_c = _nonnegative_finite(
            cathodic_charge_transfer_coefficient, "cathodic_charge_transfer_coefficient"
        )
        if alpha_a is not None and alpha_a > 2.0:
            raise ValueError("anodic_charge_transfer_coefficient must be <= 2")
        if alpha_c is not None and alpha_c > 2.0:
            raise ValueError("cathodic_charge_transfer_coefficient must be <= 2")
        if reaction_model not in _SECONDARY_SUPPORTED_REACTION_MODELS or any(
            value is not None for value in (i0, alpha_a, alpha_c)
        ):
            return {
                "success": False,
                "error": (
                    "SecondaryCurrentDistribution ElectrodeSurface exposes surface "
                    "resistivity (rhos) only on COMSOL 6.4.0.293. Use "
                    "reaction_model='surface_resistance' and surface_resistivity; "
                    "Butler-Volmer/Tafel kinetics require another interface that is "
                    "not available on this host."
                ),
                "required_products": list(REQUIRED_PRODUCTS),
            }
    except (TypeError, ValueError) as exc:
        return {"success": False, "error": str(exc)}

    component, physics = _physics_by_tag(model, tag)
    if physics is None:
        return {
            "success": False,
            "error": f"Electrochemistry interface not found: {tag}",
            "required_products": list(REQUIRED_PRODUCTS),
        }
    existing = {str(value) for value in list(physics.feature().tags())}
    if feature in existing:
        return {"success": False, "error": f"Feature tag already exists: {feature}"}

    created = False
    try:
        node = physics.feature().create(feature, "ElectrodeSurface", 2)
        created = True
        if safe_label:
            node.label(safe_label)
        node.selection().set(boundaries)
        if rhos is not None:
            node.set("rhos", str(rhos))
        return {
            "success": True,
            "profile": "electro_chemistry",
            "feature": {
                "tag": feature,
                "type": "ElectrodeSurface",
                "physics_tag": tag,
                "boundary_numbers": boundaries,
                "reaction_model": reaction_model,
                "surface_resistivity": rhos,
                "label": safe_label or feature,
            },
            "evidence_status": "live_comsol_6_4_configured",
        }
    except Exception as exc:
        rolled_back = True
        if created:
            try:
                physics.feature().remove(feature)
            except Exception:
                rolled_back = False
        message = str(exc).strip() or "Electrode surface configuration failed."
        return {
            "success": False,
            "error": (
                "Electrode surface configuration failed. Confirm ElectrodeSurface "
                f"is available. Detail: {message}"
            ),
            "required_products": list(REQUIRED_PRODUCTS),
            "rolled_back": rolled_back,
        }


def set_electrolyte(
    model: Any,
    *,
    physics_tag: str = "sec",
    domain_numbers: Sequence[int],
    ionic_conductivity: Optional[float] = None,
    feature_tag: Optional[str] = None,
    label: Optional[str] = None,
) -> dict[str, Any]:
    """Configure electrolyte conductivity on selected domains with rollback."""
    try:
        tag = _bounded_tag(physics_tag, "physics_tag")
        domains = _normalized_entities(domain_numbers, required=True, label="domain_numbers")
        conductivity = _positive_finite(ionic_conductivity, "ionic_conductivity")
        feature = _bounded_tag(feature_tag, "feature_tag") if feature_tag is not None else "eip1"
        safe_label = _bounded_label(label)
    except (TypeError, ValueError) as exc:
        return {"success": False, "error": str(exc)}

    component, physics = _physics_by_tag(model, tag)
    if physics is None:
        return {
            "success": False,
            "error": f"Electrochemistry interface not found: {tag}",
            "required_products": list(REQUIRED_PRODUCTS),
        }
    existing = {str(value) for value in list(physics.feature().tags())}
    if feature in existing:
        return {"success": False, "error": f"Feature tag already exists: {feature}"}

    created = False
    try:
        node = physics.feature().create(feature, "Electrolyte", int(_component_sdim(component)))
        created = True
        if safe_label:
            node.label(safe_label)
        node.selection().set(domains)
        if conductivity is not None:
            node.set("sigmal", str(conductivity))
        return {
            "success": True,
            "profile": "electro_chemistry",
            "feature": {
                "tag": feature,
                "type": "Electrolyte",
                "physics_tag": tag,
                "domain_numbers": domains,
                "ionic_conductivity": conductivity,
                "label": safe_label or feature,
            },
            "evidence_status": "configured_pending_licensed_probe",
        }
    except Exception as exc:
        rolled_back = True
        if created:
            try:
                physics.feature().remove(feature)
            except Exception:
                rolled_back = False
        message = str(exc).strip() or "Electrolyte configuration failed."
        return {
            "success": False,
            "error": (
                "Electrolyte configuration failed. Confirm the electrolyte domain "
                f"feature is available. Detail: {message}"
            ),
            "required_products": list(REQUIRED_PRODUCTS),
            "rolled_back": rolled_back,
        }


def register_electrochemistry_tools(mcp: MCPServer) -> None:
    """Register the isolated electrochemistry tool surface."""

    @mcp.tool()  # type: ignore[untyped-decorator]
    def electro_chemistry_catalog() -> dict[str, Any]:
        """Discover electrochemistry interfaces, boundary conditions, and module requirements.

        Offline-safe: no COMSOL, Java, license, or solver is started. Data is a
        declared catalog and is not a live availability or acceptance result.
        """
        return build_electro_chemistry_catalog()

    @mcp.tool()  # type: ignore[untyped-decorator]
    def electro_chemistry_inspect(
        model_name: Optional[str] = None,
        physics_tag: str = "sec",
    ) -> dict[str, Any]:
        """Inspect one electrochemistry interface and its child features.

        Read-only tag/label/selection inspection. Does not mutate the model or
        classify scientific validity.
        """
        return inspect_electrochemistry(model_name=model_name, physics_tag=physics_tag)

    @mcp.tool()  # type: ignore[untyped-decorator]
    def physics_add_electrochemistry(
        interface_key: str = "secondary_current",
        physics_tag: Optional[str] = None,
        domain_numbers: Optional[list[int]] = None,
        label: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> dict[str, Any]:
        """Create one electrochemistry physics interface with rollback on failure.

        Does not start a solver, select mesh/cores/OOC, or acquire a license.
        Missing Electrochemistry Module products return an explicit error.
        """
        try:
            model = _require_model(model_name)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        return add_electrochemistry_interface(
            model,
            interface_key=interface_key,
            physics_tag=physics_tag,
            domain_numbers=domain_numbers,
            label=label,
        )

    @mcp.tool()  # type: ignore[untyped-decorator]
    def physics_configure_electrode_reaction(
        boundary_numbers: list[int],
        physics_tag: str = "sec",
        reaction_model: str = "surface_resistance",
        feature_tag: Optional[str] = None,
        surface_resistivity: Optional[float] = None,
        exchange_current_density: Optional[float] = None,
        anodic_charge_transfer_coefficient: Optional[float] = None,
        cathodic_charge_transfer_coefficient: Optional[float] = None,
        label: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> dict[str, Any]:
        """Configure one ElectrodeSurface boundary with rollback on failure.

        On SecondaryCurrentDistribution this is a surface-resistance boundary
        (`surface_resistivity` / rhos). Kinetics parameters are rejected
        explicitly on COMSOL 6.4.0.293.
        """
        try:
            model = _require_model(model_name)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        return configure_electrode_reaction(
            model,
            physics_tag=physics_tag,
            boundary_numbers=boundary_numbers,
            reaction_model=reaction_model,
            feature_tag=feature_tag,
            surface_resistivity=surface_resistivity,
            exchange_current_density=exchange_current_density,
            anodic_charge_transfer_coefficient=anodic_charge_transfer_coefficient,
            cathodic_charge_transfer_coefficient=cathodic_charge_transfer_coefficient,
            label=label,
        )

    @mcp.tool()  # type: ignore[untyped-decorator]
    def physics_set_electrolyte(
        domain_numbers: list[int],
        physics_tag: str = "sec",
        ionic_conductivity: Optional[float] = None,
        feature_tag: Optional[str] = None,
        label: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> dict[str, Any]:
        """Configure electrolyte domains and optional ionic conductivity."""
        try:
            model = _require_model(model_name)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        return set_electrolyte(
            model,
            physics_tag=physics_tag,
            domain_numbers=domain_numbers,
            ionic_conductivity=ionic_conductivity,
            feature_tag=feature_tag,
            label=label,
        )


# Keep the public catalog callable name stable for unit tests while the MCP
# registration above also uses that name as its tool name.
electro_chemistry_catalog = build_electro_chemistry_catalog
electro_chemistry_inspect = inspect_electrochemistry


# Public function names used by unit tests and non-MCP callers.
__all__ = [
    "ELECTRODE_BOUNDARY_CONDITIONS",
    "ELECTRODE_REACTION_MODELS",
    "ELECTROCHEMISTRY_INTERFACES",
    "REQUIRED_PRODUCTS",
    "add_electrochemistry_interface",
    "build_electro_chemistry_catalog",
    "configure_electrode_reaction",
    "electro_chemistry_catalog",
    "electro_chemistry_inspect",
    "inspect_electrochemistry",
    "register_electrochemistry_tools",
    "set_electrolyte",
]
