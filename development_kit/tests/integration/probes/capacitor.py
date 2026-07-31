"""Standalone ParallelPlateCapacitor integration probe for COMSOL 6.4."""

import math

import jpype
import mph


PLATE_LENGTH_M = 0.01
PLATE_SEPARATION_M = 0.001
RELATIVE_PERMITTIVITY = 2.1
VACUUM_PERMITTIVITY_F_PER_M = 8.8541878128e-12


def _geometry_size_expressions() -> tuple[str, str, str]:
    return ("L", "L", "d")


def _theoretical_capacitance_pf() -> float:
    return (
        VACUUM_PERMITTIVITY_F_PER_M
        * RELATIVE_PERMITTIVITY
        * math.pow(PLATE_LENGTH_M, 2)
        / PLATE_SEPARATION_M
        * 1e12
    )


def main() -> None:
    """Build, solve, and validate the capacitor in a dedicated process."""
    client = None
    try:
        client = mph.Client(version="6.4")
        model = client.create("ParallelPlateCap")
        jm = model.java

        for name, value in (
            ("L", f"{PLATE_LENGTH_M:.17g}[m]"),
            ("d", f"{PLATE_SEPARATION_M:.17g}[m]"),
            ("epsr", f"{RELATIVE_PERMITTIVITY:.17g}"),
            ("V0", "1[V]"),
        ):
            jm.param().set(name, value)

        comp = jm.component().create("comp1", True)
        geom = comp.geom().create("geom1", 3)
        block = geom.feature().create("blk1", "Block")
        block.set("size", jpype.JArray(jpype.JString)(_geometry_size_expressions()))
        block.set("pos", jpype.JArray(jpype.JDouble)([0, 0, 0]))
        geom.run()
        print("geom: ndom=", geom.getNDomains(), "nbnd=", geom.getNBoundaries())

        sdim = str(geom.getSDim())
        electrostatics = comp.physics().create("es", "Electrostatics", sdim)
        conservation = electrostatics.feature().create("ccn1", "ChargeConservation", int(sdim))
        conservation.selection().set([1])
        conservation.set("materialType", "from_mat")

        material = comp.material().create("mat1", "Common")
        material.label("dielectric")
        material.propertyGroup("def").set("relpermittivity", "epsr")
        material.selection().set([1])

        ground = electrostatics.feature().create("gnd1", "Ground", 2)
        ground.selection().set([3])
        potential = electrostatics.feature().create("ep1", "ElectricPotential", 2)
        potential.selection().set([4])
        potential.set("V0", "V0")

        mesh = comp.mesh().create("mesh1")
        mesh.feature().create("ftr1", "FreeTet")
        mesh.run()
        print("mesh built, nelem=", mesh.getNumElem())

        study = jm.study().create("std1")
        study.create("step1", "Stationary")
        jm.study("std1").run()

        evaluated = model.evaluate("2*es.intWe/(1[V])^2", "pF")
        capacitance = float(evaluated.reshape(-1)[0])
        theory = _theoretical_capacitance_pf()
        print("C [pF] =", capacitance)
        print("C_theory [pF] =", theory)
        if not math.isclose(capacitance, theory, rel_tol=1e-8, abs_tol=1e-9):
            raise AssertionError(f"Capacitance mismatch: measured={capacitance}, theory={theory}")
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
