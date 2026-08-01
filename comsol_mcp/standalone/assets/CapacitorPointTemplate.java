import com.comsol.model.Model;
import com.comsol.model.util.ModelUtil;

import java.util.Locale;

public class __CLASS_NAME__ {
    private static final double EPSILON_0 = 8.8541878128e-12;
    private static final double PLATE_SIDE_M = 0.01;
    private static final double PLATE_GAP_M = 0.001;
    private static final double EPSILON_R = 2.1;
    private static final double VOLTAGE_V = __VOLTAGE_LITERAL__;

    public static Model run() {
        Model model = ModelUtil.create("Model");
        try {
            model.label("COMSOL MCP standalone capacitor point");
            model.param().set("plate_side", "0.01[m]");
            model.param().set("plate_gap", "0.001[m]");
            model.param().set("epsr", "2.1");
            model.param().set("V0", "__VOLTAGE_LITERAL__[V]");

            model.component().create("comp1", true);
            model.component("comp1").geom().create("geom1", 3);
            model.component("comp1").geom("geom1").feature().create("dielectric", "Block");
            model.component("comp1").geom("geom1").feature("dielectric").set(
                    "size", new String[] {"plate_side", "plate_side", "plate_gap"}
            );
            model.component("comp1").geom("geom1").run();

            int domains = model.component("comp1").geom("geom1").getNDomains();
            int boundaries = model.component("comp1").geom("geom1").getNBoundaries();
            if (domains != 1 || boundaries != 6) {
                throw new IllegalStateException("unexpected capacitor topology");
            }

            createElectrodeSelection(
                    model, "sel_ground", "-1e-9[m]", "1e-9[m]"
            );
            createElectrodeSelection(
                    model, "sel_potential", "plate_gap-1e-9[m]", "plate_gap+1e-9[m]"
            );
            int[] groundEntities = model.component("comp1").selection("sel_ground").entities();
            int[] potentialEntities = model.component("comp1").selection("sel_potential").entities();
            if (groundEntities.length != 1 || potentialEntities.length != 1
                    || groundEntities[0] == potentialEntities[0]) {
                throw new IllegalStateException("electrode selections are not unique");
            }

            model.component("comp1").physics().create("es", "Electrostatics", "geom1");
            model.component("comp1").physics("es").feature().create(
                    "ccn1", "ChargeConservation", 3
            );
            model.component("comp1").physics("es").feature("ccn1").selection().set(
                    new int[] {1}
            );
            model.component("comp1").physics("es").feature("ccn1").set(
                    "materialType", "from_mat"
            );

            model.component("comp1").material().create("mat1", "Common");
            model.component("comp1").material("mat1").propertyGroup("def").set(
                    "relpermittivity", "epsr"
            );
            model.component("comp1").material("mat1").selection().set(new int[] {1});

            model.component("comp1").physics("es").feature().create("gnd1", "Ground", 2);
            model.component("comp1").physics("es").feature("gnd1").selection().named(
                    "sel_ground"
            );
            model.component("comp1").physics("es").feature().create(
                    "ep1", "ElectricPotential", 2
            );
            model.component("comp1").physics("es").feature("ep1").selection().named(
                    "sel_potential"
            );
            model.component("comp1").physics("es").feature("ep1").set("V0", "V0");

            model.component("comp1").mesh().create("mesh1");
            model.component("comp1").mesh("mesh1").feature().create("ftr1", "FreeTet");
            model.component("comp1").mesh("mesh1").run();
            long elements = model.component("comp1").mesh("mesh1").getNumElem();

            model.study().create("std1");
            model.study("std1").create("stat", "Stationary");
            model.study("std1").run();

            model.result().numerical().create("gev1", "EvalGlobal");
            model.result().numerical("gev1").set(
                    "expr", new String[] {"2*es.intWe/(V0)^2", "es.intWe"}
            );
            model.result().numerical("gev1").set("unit", new String[] {"pF", "J"});
            double[][] values = model.result().numerical("gev1").getReal();
            if (values.length != 2 || values[0].length != 1 || values[1].length != 1) {
                throw new IllegalStateException("unexpected result shape");
            }

            double capacitancePf = values[0][0];
            double energyJ = values[1][0];
            double theoryPf = EPSILON_0 * EPSILON_R * PLATE_SIDE_M * PLATE_SIDE_M
                    / PLATE_GAP_M * 1e12;
            double relativeError = Math.abs(capacitancePf - theoryPf) / theoryPf;
            double expectedEnergyJ = theoryPf * 1e-12 * VOLTAGE_V * VOLTAGE_V / 2.0;
            double energyRelativeError = Math.abs(energyJ - expectedEnergyJ) / expectedEnergyJ;
            if (!Double.isFinite(capacitancePf) || !Double.isFinite(energyJ)
                    || relativeError > 1e-6 || energyRelativeError > 1e-6) {
                throw new IllegalStateException("capacitor physical gate failed");
            }

            System.out.println(String.format(
                    Locale.ROOT,
                    "COMSOL_MCP_EVENT {\"schema_name\":\"comsol_mcp.standalone_driver_event\","
                            + "\"schema_version\":\"1.0.0\","
                            + "\"event\":\"point_result\","
                            + "\"point_id\":\"__POINT_ID__\","
                            + "\"voltage_v\":%.17g,"
                            + "\"capacitance_pf\":%.17g,"
                            + "\"theoretical_capacitance_pf\":%.17g,"
                            + "\"relative_error\":%.17g,"
                            + "\"energy_j\":%.17g,"
                            + "\"expected_energy_j\":%.17g,"
                            + "\"energy_relative_error\":%.17g,"
                            + "\"domains\":%d,"
                            + "\"boundaries\":%d,"
                            + "\"ground_boundary\":%d,"
                            + "\"potential_boundary\":%d,"
                            + "\"mesh_elements\":%d,"
                            + "\"solver_started\":true,"
                            + "\"status\":\"passed\"}",
                    VOLTAGE_V,
                    capacitancePf,
                    theoryPf,
                    relativeError,
                    energyJ,
                    expectedEnergyJ,
                    energyRelativeError,
                    domains,
                    boundaries,
                    groundEntities[0],
                    potentialEntities[0],
                    elements
            ));
            return null;
        } finally {
            if (ModelUtil.tags().length > 0) {
                ModelUtil.remove("Model");
            }
        }
    }

    private static void createElectrodeSelection(
            Model model, String tag, String zMinimum, String zMaximum
    ) {
        model.component("comp1").selection().create(tag, "Box");
        model.component("comp1").selection(tag).geom("geom1", 2);
        model.component("comp1").selection(tag).set("xmin", "-1e-9[m]");
        model.component("comp1").selection(tag).set("xmax", "plate_side+1e-9[m]");
        model.component("comp1").selection(tag).set("ymin", "-1e-9[m]");
        model.component("comp1").selection(tag).set("ymax", "plate_side+1e-9[m]");
        model.component("comp1").selection(tag).set("zmin", zMinimum);
        model.component("comp1").selection(tag).set("zmax", zMaximum);
        model.component("comp1").selection(tag).set("condition", "inside");
    }

    public static void main(String[] args) {
        run();
    }
}
