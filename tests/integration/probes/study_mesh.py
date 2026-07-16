"""Standalone clientapi study and mesh integration probe for COMSOL 6.4."""

import jpype
import mph


def main() -> None:
    """Verify mesh creation and the full Stationary study type."""
    client = None
    try:
        client = mph.Client(version="6.4")
        model = client.create("StudyMeshProbe")
        jm = model.java

        comp = jm.component().create("comp1", True)
        geom = comp.geom().create("geom1", 3)
        block = geom.feature().create("blk1", "Block")
        block.set("size", jpype.JArray(jpype.JDouble)([0.01, 0.01, 0.001]))
        block.set("pos", jpype.JArray(jpype.JDouble)([0, 0, 0]))
        geom.run()

        sdim = str(geom.getSDim())
        comp.physics().create("es", "Electrostatics", sdim)

        mesh = comp.mesh().create("mesh1")
        mesh.feature().create("ftr1", "FreeTet")
        mesh.run()
        if int(mesh.getNumElem()) <= 0:
            raise AssertionError("Mesh contains no elements.")

        study = jm.study().create("std1")
        study.create("step1", "Stationary")
        print(
            "study+mesh OK:",
            "ndom=", geom.getNDomains(),
            "nbnd=", geom.getNBoundaries(),
            "nelem=", mesh.getNumElem(),
        )
    finally:
        if client is not None:
            try:
                client.clear()
            except Exception:
                pass
            try:
                client.disconnect()
            except Exception:
                pass


if __name__ == "__main__":
    main()
