"""MIM (Metal-Insulator-Metal) patch metasurface tools for COMSOL MCP Server.

These tools support the Au-Al2O3-Au MIM thermal emitter workflow
(Chen et al. Int. J. Thermal Sciences 185, 2023, 108069):
  1. geometry_probe_domains  -- enhanced boundary probing with up/down domains
  2. mim_patch_build          -- build patch geometry + BCs + mesh from a baseline
  3. mim_evaluate_spectral    -- evaluate Rtotal/Ttotal/Atotal vs wavelength

Key discovery: the Floquet periodic condition in ewfd (curl elements) REQUIRES
compatible (identical) meshes on source/destination side faces. The "CopyFace"
mesh feature copies the 2-D mesh from source to destination, ensuring exact
conformity. Combined with FreeTet for the volume, this avoids the Sweep-mesh
topology-mismatch issue that occurs when a patch domain changes the cross-section.
"""

from typing import Optional, Sequence
from mcp.server.fastmcp import FastMCP

from .session import session_manager
from .physics import _first_component


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _get_geom_node(model, geometry_name: Optional[str], component_name: str = "comp1"):
    """Return (geom_node, error)."""
    jm = model.java
    try:
        comp = jm.component(component_name)
        if comp is None:
            return None, f"Component '{component_name}' not found."
        if geometry_name:
            geom = comp.geom(geometry_name)
            if geom is None:
                return None, f"Geometry '{geometry_name}' not found."
        else:
            geoms = comp.geom()
            if geoms.size() == 0:
                return None, "No geometry sequences found."
            tags = list(geoms.tags())
            geom = geoms.get(tags[0])
        return geom, None
    except Exception as e:
        return None, f"Failed to get geometry: {e}"


def _probe_boundaries(geom):
    """Probe all boundaries: returns (list_of_dicts, n_domains, n_boundaries)."""
    import jpype as _jp
    n_bnd = geom.getNBoundaries()
    n_dom = geom.getNDomains()
    sdim = int(geom.getSDim())

    ud = geom.getUpDown()
    ups = list(ud[0]) if n_bnd > 0 else []
    downs = list(ud[1]) if n_bnd > 0 else []

    PP = _jp.JArray(_jp.JArray(_jp.JDouble))(1)
    boundaries = []
    for i in range(1, n_bnd + 1):
        info = {"boundary_number": i, "up_domain": ups[i - 1], "down_domain": downs[i - 1]}
        try:
            pr = list(geom.faceParamRange(i))
            u_mid = (float(pr[0]) + float(pr[1])) / 2.0
            v_mid = (float(pr[2]) + float(pr[3])) / 2.0
            PP[0] = _jp.JArray(_jp.JDouble)([u_mid, v_mid])
            normal = list(geom.faceNormal(i, PP)[0])
            center = list(geom.faceX(i, PP)[0])
            info["normal"] = [float(n) for n in normal]
            info["center"] = [float(c) for c in center]
            info["interior"] = ups[i - 1] != 0 and downs[i - 1] != 0
        except Exception as e:
            info["error"] = f"probe failed: {str(e)[:60]}"
        boundaries.append(info)
    return boundaries, n_dom, n_bnd, sdim


def _identify_side_pairs(boundaries, P_val=None, bbox=None, tol=1e-12):
    """Identify periodic CELL side face pairs from boundary normals + centers.

    Returns dict: {x_src, x_dst, y_src, y_dst, bottom, top} lists of bnd numbers.

    IMPORTANT: This filters by BOTH normal AND center coordinate so that interior
    patch/air-interface side faces (which share the same ±x/±y normal but are at
    interior positions, e.g. cx=L/2 inside the cell) are NOT misclassified as the
    Floquet periodic cell side. Without coordinate filtering, CopyFace source
    would include patch side faces and break Floquet mesh compatibility.

    Args:
        boundaries: list of boundary dicts (each must have 'normal' and 'center').
        P_val: cell period (used as x_max/y_max if bbox not given). Backward compat.
        bbox: optional (xmin, xmax, ymin, ymax, zmin, zmax) for tighter filtering.
        tol: absolute tolerance for matching cell-edge coordinate (metres).
    """
    if bbox is None and P_val is not None:
        bbox = (0.0, P_val, 0.0, P_val, 0.0, P_val)
    if bbox is None:
        # Fall back to pure-normal classification (legacy behaviour, no filtering)
        bbox = None
    xmin, xmax, ymin, ymax, zmin, zmax = (list(bbox) if bbox is not None else [None]*6)

    def _on_edge(coord, edge, tol):
        if edge is None:
            return True  # no bbox filtering
        return abs(coord - edge) <= tol

    x_src, x_dst, y_src, y_dst = [], [], [], []
    bottom, top = [], []
    for b in boundaries:
        if "normal" not in b or "center" not in b:
            continue
        nx, ny, nz = b["normal"]
        cx, cy, cz = b["center"]
        if nz < -0.5 and _on_edge(cz, zmin, tol):  # bottom face (normal -z, z=zmin)
            bottom.append(b["boundary_number"])
        elif nz > 0.5 and _on_edge(cz, zmax, tol) and zmax is not None:  # top, z=zmax
            top.append(b["boundary_number"])
        elif nz > 0.5 and zmax is None:  # top without bbox (legacy)
            top.append(b["boundary_number"])
        elif nx < -0.5 and _on_edge(cx, xmin, tol):  # x=xmin cell side
            x_src.append(b["boundary_number"])
        elif nx > 0.5 and _on_edge(cx, xmax, tol):  # x=xmax cell side
            x_dst.append(b["boundary_number"])
        elif ny < -0.5 and _on_edge(cy, ymin, tol):  # y=ymin cell side
            y_src.append(b["boundary_number"])
        elif ny > 0.5 and _on_edge(cy, ymax, tol):  # y=ymax cell side
            y_dst.append(b["boundary_number"])
    return {
        "x_src": x_src, "x_dst": x_dst,
        "y_src": y_src, "y_dst": y_dst,
        "bottom": bottom, "top": top,
    }


def _list_pair_metadata(comp) -> list[dict[str, str]]:
    """Return pair tags and labels with clientapi strings normalized for JSON."""
    try:
        raw_tags = list(comp.pair().tags())
    except Exception:
        return []

    pairs = []
    for raw_tag in raw_tags:
        tag = str(raw_tag)
        try:
            pair = comp.pair().get(raw_tag)
            pairs.append({"tag": tag, "label": str(pair.label())})
        except Exception:
            pairs.append({"tag": tag})
    return pairs


def _find_air_block_tag(geom) -> Optional[str]:
    """Find the first tall Block-like feature and return its Python tag."""
    for raw_tag in list(geom.feature().tags()):
        tag = str(raw_tag)
        if tag == "fin":
            continue
        feature = geom.feature().get(raw_tag)
        try:
            size_text = str(feature.getString("size"))
            size = [float(value) for value in size_text.replace(",", " ").split()]
            if size[2] > 1e-7:
                return tag
        except Exception:
            pass
    return None


# ---------------------------------------------------------------------------
# tool registration
# ---------------------------------------------------------------------------

def register_mim_patch_tools(mcp: FastMCP) -> None:
    """Register MIM patch metasurface tools."""

    @mcp.tool()
    def geometry_probe_domains(
        geometry_name: Optional[str] = None,
        component_name: str = "comp1",
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Probe geometry boundaries with up/down domain information.

        Enhanced version of geometry_get_boundaries that also returns:
        - up_domain / down_domain for each boundary (identifies interior vs exterior)
        - interior flag (True if both up and down domains are non-zero)
        - Pair info (identity/assembly pairs) if any
        - Auto-identified periodic side face pairs (x_src/dst, y_src/dst, bottom, top)

        Use this to identify which boundaries are interior interfaces, which are
        periodic side pairs, and which is the bottom/top before setting BCs.

        Args:
            geometry_name: Geometry sequence name (default: first)
            component_name: Component name (default: 'comp1')
            model_name: Model name (default: current)

        Returns:
            Boundary info with up/down domains, pair info, side pair identification.
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {"success": False, "error": f"Model not found: {model_name or 'no current model'}"}

        try:
            jm = model.java
            geom, error = _get_geom_node(model, geometry_name, component_name)
            if error:
                return {"success": False, "error": error}

            geom.run()
            boundaries, n_dom, n_bnd, sdim = _probe_boundaries(geom)

            # bbox
            try:
                bbox = [float(x) for x in geom.getBoundingBox()]
            except Exception:
                bbox = None

            # pairs
            comp = jm.component(component_name)
            pairs_info = _list_pair_metadata(comp)

            # identify side pairs (filter by coordinate so interior faces with
            # ±x/±y normals — e.g. patch side faces at x=L/2 — are NOT misread as
            # the Floquet periodic cell sides).
            bbox6 = (tuple(bbox) if bbox is not None else None)
            side_pairs = _identify_side_pairs(boundaries, bbox=bbox6)

            # identify interior boundaries (up!=0 and down!=0 → FormUnion interior)
            interior_bnds = [b["boundary_number"] for b in boundaries if b.get("interior")]

            result = {
                "success": True,
                "geometry": geometry_name or "first",
                "space_dimension": sdim,
                "total_domains": n_dom,
                "total_boundaries": n_bnd,
                "boundaries": boundaries,
                "interior_boundaries": interior_bnds,
                "pairs": pairs_info,
                "side_pairs": side_pairs,
            }
            if bbox is not None:
                result["bounding_box"] = bbox
            return result
        except Exception as e:
            return {"success": False, "error": f"Failed to probe: {e}"}

    @mcp.tool()
    def mim_patch_build(
        patch_size: Sequence[float],
        patch_pos: Sequence[float],
        air_block_tag: Optional[str] = None,
        patch_tag: str = "b_pat",
        diff_tag: str = "dif1",
        layered_transition_tag: str = "ltr1",
        layered_impedance_tag: str = "lib1",
        air_material_tag: str = "mat_air",
        ewfd_tag: str = "ewfd",
        geometry_name: Optional[str] = None,
        component_name: str = "comp1",
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Build a MIM patch metasurface model from a 2-domain baseline.

        Adds a patch Block + Difference (keepsubtract=True) to the geometry,
        rebuilds with FormUnion (automatic continuity), then:
        - Updates LayeredTransition BC to the patch-footprint interior boundary
        - Updates LayeredImpedance BC to the bottom face
        - Assigns air material to the new patch domain
        - Creates a periodic-compatible mesh (FreeTri + CopyFace + FreeTet)

        The patch domain is AIR (not metal). The Au thin film is modeled by the
        LayeredTransition BC on the Al2O3/patch interior boundary (patch footprint).

        Args:
            patch_size: [width, depth, height] of the Au patch in meters.
            patch_pos: [x, y, z] base position of the patch in meters.
            air_block_tag: Tag of the existing air block to subtract from
                (auto-detected if None — picks the block with largest z-size).
            patch_tag: Tag for the new patch block feature (default 'b_pat').
            diff_tag: Tag for the Difference operation (default 'dif1').
            layered_transition_tag: Tag of existing LayeredTransition BC (default 'ltr1').
            layered_impedance_tag: Tag of existing LayeredImpedance BC (default 'lib1').
            air_material_tag: Tag of existing air material (default 'mat_air').
            ewfd_tag: Tag of the ewfd physics interface (default 'ewfd').
            geometry_name: Geometry sequence name (default: first).
            component_name: Component name (default: 'comp1').
            model_name: Model name (default: current).

        Returns:
            New domain/boundary counts, key boundary numbers, mesh stats.
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {"success": False, "error": f"Model not found: {model_name or 'no current model'}"}

        try:
            import jpype as _jp
            jm = model.java
            comp = jm.component(component_name)
            if comp is None:
                return {"success": False, "error": f"Component '{component_name}' not found."}

            geom, error = _get_geom_node(model, geometry_name, component_name)
            if error:
                return {"success": False, "error": error}

            report = {"success": True, "steps": []}

            # ---- Step 1: identify air block (auto-detect if not given) ----
            if not air_block_tag:
                air_block_tag = _find_air_block_tag(geom)
            if not air_block_tag:
                return {"success": False, "error": "Could not auto-detect air block tag. Please specify air_block_tag."}
            report["air_block_tag"] = air_block_tag

            # ---- Step 2: add patch block ----
            b_pat = geom.feature().create(patch_tag, "Block")
            b_pat.set("size", [str(s) for s in patch_size])
            b_pat.set("pos", [str(p) for p in patch_pos])
            report["steps"].append(f"Added patch block {patch_tag}: size={list(patch_size)}, pos={list(patch_pos)}")

            # ---- Step 3: add Difference (keepsubtract=True) ----
            dif = geom.feature().create(diff_tag, "Difference")
            dif.selection("input").set([air_block_tag])
            dif.selection("input2").set([patch_tag])
            try:
                dif.set("keepsubtract", True)
            except Exception:
                dif.set("keep", True)
            report["steps"].append(f"Added Difference {diff_tag}: input={air_block_tag}, subtract={patch_tag}, keep=True")

            # ---- Step 4: build geometry (FormUnion = default) ----
            try:
                fin = geom.feature().get("fin")
                action = fin.getString("action")
                if action and action != "union":
                    fin.set("action", "union")
            except Exception:
                pass
            geom.run()

            boundaries, n_dom, n_bnd, sdim = _probe_boundaries(geom)
            report["n_domains"] = n_dom
            report["n_boundaries"] = n_bnd

            # ---- Step 5: identify key boundaries ----
            # patch footprint interface: interior boundary where up=patch_dom, down=al2_dom.
            # Patch domain is the highest-numbered domain (dom 3 typically) and the
            # Al2O3/air baseline becomes dom 1+2 (al2o3 keeps its domain tag).
            patch_dom = n_dom  # last domain added
            al2_dom = 1
            patch_footprint = [b["boundary_number"] for b in boundaries
                               if b.get("up_domain") == patch_dom and b.get("down_domain") == al2_dom]

            # Filter side/top/bottom by BOTH normal AND coordinate. Without the
            # coordinate filter, the patch side/top faces (interior interfaces with
            # ±x/±y/+z normals) would be misclassified as the cell exterior and
            # break Floquet CopyFace mesh compatibility.
            try:
                bbox_vals = [float(x) for x in geom.getBoundingBox()]
                bbox6 = (bbox_vals[0], bbox_vals[1], bbox_vals[2],
                         bbox_vals[3], bbox_vals[4], bbox_vals[5])
            except Exception:
                bbox6 = None
            side_pairs = _identify_side_pairs(boundaries, bbox=bbox6)
            bottom = side_pairs.get("bottom", [])
            top = side_pairs.get("top", [])

            report["patch_footprint_interface"] = patch_footprint
            report["bottom"] = bottom
            report["top"] = top
            report["side_pairs"] = side_pairs

            # ---- Step 6: update BCs ----
            phys = comp.physics()
            ewfd = phys.get(ewfd_tag)
            if ewfd is None:
                report["steps"].append(f"WARNING: ewfd physics '{ewfd_tag}' not found, skipping BC update")
            else:
                # LayeredTransition → patch footprint
                if patch_footprint:
                    try:
                        ltr = ewfd.feature().get(layered_transition_tag)
                        ltr.selection().set(patch_footprint)
                        report["steps"].append(f"LayeredTransition {layered_transition_tag} → boundaries {patch_footprint}")
                    except Exception as e:
                        report["steps"].append(f"WARNING: Could not update LayeredTransition: {e}")

                # LayeredImpedance → bottom
                if bottom:
                    try:
                        lib = ewfd.feature().get(layered_impedance_tag)
                        lib.selection().set(bottom)
                        report["steps"].append(f"LayeredImpedance {layered_impedance_tag} → boundaries {bottom}")
                    except Exception as e:
                        report["steps"].append(f"WARNING: Could not update LayeredImpedance: {e}")

                # PeriodicStructure excitedPort → top
                if top:
                    try:
                        ps = ewfd.feature().get("ps1")
                        ps.selection("excitedPortSelection").set(top)
                        report["steps"].append(f"PeriodicStructure excitedPort → boundaries {top}")
                    except Exception as e:
                        report["steps"].append(f"WARNING: Could not update PeriodicStructure port: {e}")

            # ---- Step 7: assign air material to patch domain ----
            mat_list = comp.material()
            try:
                air_mat = mat_list.get(air_material_tag)
                cur = list(air_mat.selection().entities())
                if patch_dom not in cur:
                    air_mat.selection().set(cur + [patch_dom])
                    report["steps"].append(f"Air material {air_material_tag} → domains {cur + [patch_dom]}")
            except Exception as e:
                report["steps"].append(f"WARNING: Could not assign air material to dom {patch_dom}: {e}")

            # ---- Step 8: create periodic-compatible mesh ----
            # Delete old mesh sequences
            for mt in list(comp.mesh().tags()):
                comp.mesh().remove(mt)

            mesh = comp.mesh().create("mesh1")

            # FreeTri on source side faces
            x_src = side_pairs.get("x_src", [])
            y_src = side_pairs.get("y_src", [])
            x_dst = side_pairs.get("x_dst", [])
            y_dst = side_pairs.get("y_dst", [])

            if x_src:
                ftx = mesh.feature().create("ftri_x", "FreeTri")
                ftx.selection().set(x_src)
            if y_src:
                fty = mesh.feature().create("ftri_y", "FreeTri")
                fty.selection().set(y_src)

            # CopyFace: source → destination (ensures identical periodic meshes)
            if x_src and x_dst:
                cpx = mesh.feature().create("cp_x", "CopyFace")
                try:
                    cpx.selection("source").set(x_src)
                    cpx.selection("destination").set(x_dst)
                except Exception:
                    try:
                        cpx.selection("src").set(x_src)
                        cpx.selection("dst").set(x_dst)
                    except Exception:
                        cpx.selection().set(x_src + x_dst)
            if y_src and y_dst:
                cpy = mesh.feature().create("cp_y", "CopyFace")
                try:
                    cpy.selection("source").set(y_src)
                    cpy.selection("destination").set(y_dst)
                except Exception:
                    try:
                        cpy.selection("src").set(y_src)
                        cpy.selection("dst").set(y_dst)
                    except Exception:
                        cpy.selection().set(y_src + y_dst)

            # FreeTet for volume
            ft = mesh.feature().create("ft1", "FreeTet")

            mesh.run()
            try:
                report["mesh_elements"] = int(mesh.getNumElem())
                report["mesh_vertices"] = int(mesh.getNumVertex())
            except Exception:
                pass
            report["steps"].append(f"Mesh: FreeTri+CopyFace+FreeTet → {report.get('mesh_elements', '?')} elements")

            return report

        except Exception as e:
            return {"success": False, "error": f"mim_patch_build failed: {e}"}

    @mcp.tool()
    def mim_evaluate_spectral(
        expressions: Optional[Sequence[str]] = None,
        wl_parameter: str = "wl",
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Evaluate spectral quantities (Rtotal, Ttotal, Atotal) vs wavelength.

        Convenience wrapper around results_evaluate that returns a clean
        wavelength-indexed table of reflection / transmission / absorption.

        Args:
            expressions: List of expressions to evaluate (default:
                ['ewfd.Rtotal', 'ewfd.Ttotal', 'ewfd.Atotal', 'wl']).
            wl_parameter: Name of the wavelength parameter (default 'wl').
            model_name: Model name (default: current).

        Returns:
            List of {wl, R, T, A} dicts (wl in µm).
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {"success": False, "error": f"Model not found: {model_name or 'no current model'}"}

        if expressions is None:
            expressions = ["ewfd.Rtotal", "ewfd.Ttotal", "ewfd.Atotal", wl_parameter]

        try:
            # Ensure wl is included
            expr_list = list(expressions)
            if wl_parameter not in expr_list:
                expr_list.append(wl_parameter)

            results = model.evaluate(expr_list)

            # Build clean output
            spectral = []
            for row in results:
                vals = [float(v) for v in row]
                entry = {}
                for i, expr in enumerate(expr_list):
                    if expr == wl_parameter:
                        entry["wl_um"] = vals[i] * 1e6
                    else:
                        entry[expr] = vals[i]
                # Compute emissivity = 1 - R if R present
                if "ewfd.Rtotal" in entry:
                    entry["emissivity"] = 1.0 - entry["ewfd.Rtotal"]
                spectral.append(entry)

            return {
                "success": True,
                "n_wavelengths": len(spectral),
                "expressions": expr_list,
                "spectral_data": spectral,
            }
        except Exception as e:
            return {"success": False, "error": f"Evaluation failed: {e}"}
