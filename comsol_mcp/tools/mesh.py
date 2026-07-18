"""Mesh tools for COMSOL MCP Server."""

from typing import Optional
from mcp.server.fastmcp import FastMCP

from .session import session_manager


def get_mesh_info(
    model,
    *,
    mesh_name: Optional[str] = None,
    component_name: Optional[str] = None,
) -> dict:
    """Return mesh metadata through the COMSOL 6.4 clientapi."""
    from .physics import _first_component

    jm = model.java
    comp = jm.component(component_name) if component_name else _first_component(jm)
    if comp is None:
        return {"success": False, "error": "No component found in model."}

    mesh_list = comp.mesh()
    tags = [str(tag) for tag in mesh_list.tags()]
    if not tags:
        return {"success": False, "error": "No meshes defined in model."}

    target_tag = None
    if mesh_name is None:
        target_tag = tags[0]
    elif mesh_name in tags:
        target_tag = mesh_name
    else:
        for tag in tags:
            try:
                if str(mesh_list.get(tag).label()) == mesh_name:
                    target_tag = tag
                    break
            except Exception:
                pass
    if target_tag is None:
        return {
            "success": False,
            "error": f"Mesh not found: {mesh_name}. Available tags: {tags}",
        }

    mesh = mesh_list.get(target_tag)
    info = {
        "name": target_tag,
        "component": str(comp.tag()),
        "features": [str(tag) for tag in mesh.feature().tags()],
    }
    try:
        info["label"] = str(mesh.label())
    except Exception:
        pass
    try:
        info["num_elements"] = int(mesh.getNumElem())
    except Exception:
        info["num_elements"] = None
    try:
        info["num_vertices"] = int(mesh.getNumVertex())
    except Exception:
        info["num_vertices"] = None
    return {"success": True, "mesh": info}


def register_mesh_tools(mcp: FastMCP) -> None:
    """Register mesh tools with the MCP server."""
    
    @mcp.tool()
    def mesh_list(model_name: Optional[str] = None) -> dict:
        """
        List all mesh sequences in a model.
        
        Args:
            model_name: Model name (default: current model)
        
        Returns:
            List of mesh sequence names
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            meshes = model.meshes()
            return {
                "success": True,
                "meshes": meshes,
                "count": len(meshes),
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to list meshes: {str(e)}"}
    
    @mcp.tool()
    def mesh_create(
        mesh_name: Optional[str] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Run a mesh sequence to generate the mesh.
        
        This executes the meshing operations defined in the mesh sequence.
        
        Args:
            mesh_name: Mesh sequence name (default: run all mesh sequences)
            model_name: Model name (default: current model)
        
        Returns:
            Mesh generation status
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            model.mesh(mesh_name)
            return {
                "success": True,
                "mesh": mesh_name,
                "message": f"Mesh created: {mesh_name or 'all meshes'}",
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to create mesh: {str(e)}"}

    @mcp.tool()
    def mesh_sequence_create(
        mesh_name: str = "mesh1",
        element_type: str = "FreeTet",
        component_name: Optional[str] = None,
        build: bool = True,
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Create a mesh sequence with a single meshing feature and optionally build it.
        
        COMSOL does NOT auto-create a mesh sequence; one must be created before
        solving. This tool creates a mesh sequence on the component, adds one
        meshing feature (default FreeTet = free tetrahedral), and runs it.
        
        Args:
            mesh_name: Tag/name for the mesh sequence (default 'mesh1')
            element_type: Meshing operation type, e.g. 'FreeTet' (free tetrahedral,
                default), 'FreeSweep' (sweep), 'FreeTri' (free triangular),
                'Map' (mapped), 'BoundaryLayer'.
            component_name: Component name (default: first component)
            build: If True (default), immediately build the mesh after creation.
            model_name: Model name (default: current model)
        
        Returns:
            Created mesh info including element counts if built.
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            from .physics import _first_component
            jm = model.java
            comp = jm.component(component_name) if component_name else _first_component(jm)
            if comp is None:
                return {"success": False, "error": "No component found in model."}
            
            # create mesh sequence
            mesh_seq = comp.mesh().create(mesh_name)
            # add a meshing feature
            feat_tag = "ftr1"
            feat = mesh_seq.feature().create(feat_tag, element_type)
            
            result = {
                "success": True,
                "mesh_name": mesh_name,
                "feature_tag": feat_tag,
                "element_type": element_type,
                "built": False,
            }
            
            if build:
                mesh_seq.run()
                result["built"] = True
                try:
                    # clientapi MeshSequenceClient uses getNumElem/getNumVertex (return int)
                    if hasattr(mesh_seq, "getNumElem"):
                        result["num_elements"] = int(mesh_seq.getNumElem())
                    elif hasattr(mesh_seq, "getElement") and hasattr(mesh_seq.getElement(), "size"):
                        result["num_elements"] = int(mesh_seq.getElement().size())
                    if hasattr(mesh_seq, "getNumVertex"):
                        result["num_vertices"] = int(mesh_seq.getNumVertex())
                    elif hasattr(mesh_seq, "getVertex") and hasattr(mesh_seq.getVertex(), "size"):
                        result["num_vertices"] = int(mesh_seq.getVertex().size())
                except Exception:
                    pass
            
            return result
        except Exception as e:
            return {"success": False, "error": f"Failed to create mesh sequence: {str(e)}"}
    
    @mcp.tool()
    def mesh_info(
        mesh_name: Optional[str] = None,
        component_name: Optional[str] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Get information about a mesh.
        
        Args:
            mesh_name: Mesh sequence name (default: first mesh)
            component_name: Component containing the mesh (default: first)
            model_name: Model name (default: current model)
        
        Returns:
            Mesh statistics including element counts
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            return get_mesh_info(
                model,
                mesh_name=mesh_name,
                component_name=component_name,
            )
        except Exception as e:
            return {"success": False, "error": f"Failed to get mesh info: {str(e)}"}
