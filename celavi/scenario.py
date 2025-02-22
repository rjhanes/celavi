"""
Created January 27, 2022.

@author: rhanes
"""
from dataclasses import replace
import re
import os
import time
import yaml
import pickle

import numpy as np
import pandas as pd
import warnings
from scipy.stats import weibull_min


from celavi.routing import Router
from celavi.uncertainty_methods import apply_array_uncertainty
from celavi.costgraph import CostGraph
from celavi.compute_locations import ComputeLocations
from celavi.data_filtering import filter_locations, filter_routes
from celavi.pylca_celavi.des_interface import PylcaCelavi
from celavi.reeds_importer import ReedsImporter
from celavi.des import Context
from celavi.diagnostic_viz import DiagnosticViz




class Scenario:
    """
    Set up, validate, and execute a CELAVI scenario.

    Execute multiple runs automatically to quantify uncertainty in results.
    """

    def __init__(self, parser):
        """
        Initialize Scenario and validate inputs.

        Parameter
        ---------
        parser
            Parser object for reading in config files.

        Raises
        ------
        IOError
            Raises IOError if either config file cannot be opened.
        """
        self.args = parser.parse_args()

        # Get the configuration information as two dictionaries
        try:
            with open(
                os.path.join(self.args.data, self.args.casestudy), "r", encoding="utf-8"
            ) as f:
                self.case = yaml.load(f, Loader=yaml.FullLoader)
        except IOError:
            print(
                f"Could not open {os.path.join(self.args.data, self.args.casestudy)} for configuration."
            )
            raise
        try:
            self.scenario_filename = os.path.join(self.args.data, self.args.scenario)
            with open(self.scenario_filename, "r", encoding="utf-8") as f:
                self.scen = yaml.load(f, Loader=yaml.FullLoader)
        except IOError:
            print(
                f"Could not open {os.path.join(self.args.data, self.args.scenario)} for configuration."
            )
            raise

        self.files = {}
        self.routes = pd.DataFrame()

        # Create holders for the three CELAVI components
        self.context = None
        self.netw = None
        self.lca = None

        # Start at model run 0
        self.run = 0
        # Record start time of this scenario
        self.start = self.simtime(0.0)
        # Create a random number generator with a user-defined seed
        self.rng = np.random.default_rng(self.scen["scenario"]["seed"])

        print(f"Preprocessing starting at {self.simtime(self.start)} s", flush=True)

        # Assemble list of all input and output file paths
        self.get_filepaths()
        # Move any existing results files to a separate directory
        self.clear_results()
        # Preprocess and save generated files
        self.preprocess()

        # Instantiate model
        self.setup()

        print(f"Simulations starting at {self.simtime(self.start)} s", flush=True)

        # Execute all model runs
        # Subtract one from the number of runs in the config file because
        # arange includes zero in the list
        for i in np.arange(self.scen["scenario"]["runs"]):
            time0 = time.time()
            self.run = i
            self.execute()

            print(
                f"Creating diagnostic visualizations for run {i} at {self.simtime(self.start)} s",
                flush=True,
            )

            # Postprocess and save the output of a single model run
            self.postprocess()
            print(f'Run {i} took {str(time.time()-time0)} seconds', flush = True)

        # Print run finish message
        print(f"FINISHED SIMULATION at {self.simtime(self.start)} s", flush=True)

    def get_filepaths(self):
        """
        Check that input files exist and assemble paths.

        Raises
        ------
        Exception
            Raises exception if necessary filepaths do not exist.
        """
        for _dir, _fdict in self.case["files"].items():
            # Create the directory if it doesn't exist
            if not os.path.isdir(
                os.path.join(self.args.data, self.case["directories"][_dir])
            ):
                os.makedirs(
                    os.path.join(self.args.data, self.case["directories"][_dir])
                )

            for _n, _f in _fdict.items():
                # Capacity projection file is a scenario parameter and must be
                # processed separately
                if _n == "capacity_projection":
                    _f = self.scen["scenario"].get("capacity_projection")

                _p = os.path.join(self.args.data, self.case["directories"][_dir], _f)
                if not os.path.isfile(_p) and dir in [
                    "inputs_to_preprocessing",
                    "inputs",
                    "quality_checks",
                ]:
                    raise Exception(
                        f"{_f} in {os.path.join(self.args.data, _dir)} "
                        f"does not exist"
                    )
                self.files[_n] = _p

    def preprocess(self):
        """Compute routes, locations, technology units, and step costs."""

        start_year = self.case["model_run"].get("start_year")

        # Note that the step_cost file must be updated (or programmatically generated)
        # to include all facility ids. Otherwise, cost graph can't run with the full
        # computed data set.
        if self.scen["flags"].get("compute_locations", True):
            loc = ComputeLocations(
                start_year=start_year,
                power_plant_locations=self.files["power_plant_locs"],
                landfill_locations=self.files["landfill_locs"],
                other_facility_locations=self.files["other_facility_locs"],
                transportation_graph=self.files["transportation_graph"],
                node_locations=self.files["node_locs"],
                lookup_facility_type=self.files["lookup_facility_type"],
                technology_data_filename=self.files["technology_data"],
                standard_scenarios_filename=self.files["capacity_projection"],
            )
            loc.join_facilities(locations_output_file=self.files["locs"])

        # if the step_costs file is being generated, then all facilities of the same
        # type will have the same cost models.
        if self.scen["flags"].get("generate_step_costs", True):
            pd.read_csv(self.files["lookup_step_costs"]).merge(
                pd.read_csv(self.files["locs"])[["facility_id", "facility_type"]],
                on="facility_type",
                how="outer",
            ).to_csv(self.files["step_costs"], index=False)

        # Data filtering for states
        states_to_filter = self.scen["scenario"].get("states_included", [])

        # if the data is being filtered and a new routes file is NOT being
        # generated, then the existing routes file must also be filtered
        if self.scen["flags"]["use_computed_routes"]:
            _routefile = self.files["routes_computed"]
        else:
            _routefile = self.files["routes_custom"]

        if self.scen["flags"].get("location_filtering", False):
            if not states_to_filter:
                print("Cannot filter data; no state list provided", flush=True)
            else:
                print(f"Filtering locations: {states_to_filter}", flush=True)
                filter_locations(
                    self.files["locs"],
                    self.files["technology_data"],
                    states_to_filter,
                )
            if not self.scen["flags"].get("run_routes", True):
                print(f"Filtering routes: {states_to_filter}", flush=True)
                filter_routes(self.files["locs"], _routefile)

        if self.scen["flags"].get("run_routes", True):
            Router.get_all_routes(
                locations_file=self.files["locs"],
                route_pair_file=self.files["route_pairs"],
                distance_filtering=self.scen["flags"].get("distance_filtering", False),
                transportation_graph=self.files["transportation_graph"],
                node_locations=self.files["node_locs"],
                routes_output_file=_routefile,
                routing_output_folder=os.path.join(
                    self.args.data, self.case["directories"].get("generated")
                ),
            )

        print(f"Run routes completed in {self.simtime(self.start)} s", flush=True)

    def setup(self):
        """Create instances of CostGraph, DES (Context and Components) and PyLCIA."""
        start_year = self.case["model_run"].get("start_year")

        component_material_mass = pd.read_csv(self.files["component_material_mass"])

        component_total_mass = (
            component_material_mass.groupby(by=["year", "technology", "component"])
            .sum("mass_tonnes")
            .reset_index()
        )

        circular_components = self.scen["technology_components"].get(
            "circular_components"
        )

        if self.scen["flags"].get("initialize_costgraph", True):
            # Initialize the CostGraph using these parameter settings
            print(f"CostGraph starts at {self.simtime(self.start)} s", flush=True)
            self.netw = CostGraph(
                step_costs_file=self.files["step_costs"]
                if self.scen["flags"].get("generate_step_costs")
                else self.files["step_costs_custom"],
                fac_edges_file=self.files["fac_edges"],
                transpo_edges_file=self.files["transpo_edges"],
                locations_file=self.files["locs"],
                routes_file=self.files["routes_computed"]
                if self.scen["flags"].get("use_computed_routes")
                else self.files["routes_custom"],
                sc_begin=self.scen["circular_pathways"].get("sc_begin"),
                sc_end=self.scen["circular_pathways"].get("sc_end"),
                sc_in_circ=self.scen["circular_pathways"].get("sc_in_circ", []),
                sc_out_circ=self.scen["circular_pathways"].get("sc_out_circ", []),
                year=start_year,
                start_year=start_year,
                verbose=self.case["model_run"].get("cg_verbose", 1),
                save_copy=self.case["model_run"].get("save_cg_csv", True),
                save_name=self.files["costgraph_csv"],
                pathway_crit_history_filename=self.files["pathway_criterion_history"],
                circular_components=circular_components,
                component_initial_mass=component_total_mass.loc[
                    component_total_mass.year == start_year, "mass_tonnes"
                ].values[0],
                path_dict=self.scen["circular_pathways"],
                random_state=self.rng,
                run=self.run,
            )
            print(f"CostGraph initialized at {self.simtime(self.start)}", flush=True)

            if self.scen["flags"].get("pickle_costgraph", True):
                # Save the CostGraph object using pickle
                pickle.dump(self.netw, open(self.files["costgraph_pickle"], "wb"))

        else:
            self.netw = pickle.load(open(self.files["costgraph_pickle"], "rb"))
            print(f"CostGraph read in at {self.simtime(self.start)}", flush=True)

        # Electricity spatial mix level. Defaults to 'state' when not provided.
        electricity_grid_spatial_level = self.scen["scenario"].get(
            "electricity_mix_level", "state"
        )

        if electricity_grid_spatial_level == "state":
            reeds_importer = ReedsImporter(
                reeds_imported_filename=self.files["state_reeds_grid_mix"],
                reeds_output_filename=self.files["state_electricity_lci"],
            )
            reeds_importer.state_level_reeds_importer()
            dynamic_lci_filename = self.files["state_electricity_lci"]
        else:
            reeds_importer = ReedsImporter(
                reeds_imported_filename=self.files["national_reeds_grid_mix"],
                reeds_output_filename=self.files["national_electricity_lci"],
            )
            reeds_importer.national_level_reeds_importer()
            dynamic_lci_filename = self.files["national_electricity_lci"]

        # Prepare LCIA code
        #verbose = 0 means no print statements. 
        #verbose = 1 prints detailed LCA calculation steps
        if self.case["model_run"].get("warning_verbose") == 0:
            warnings.filterwarnings('ignore')
        self.lca = PylcaCelavi(
            lcia_des_filename=self.files["lcia_to_des"],
            shortcutlca_filename=self.files["lcia_shortcut_db"],
            intermediate_demand_filename=self.files["intermediate_demand"],
            dynamic_lci_filename=dynamic_lci_filename,
            electricity_grid_spatial_level=electricity_grid_spatial_level,
            static_lci_filename=self.files["static_lci"],
            uslci_tech_filename=self.files["uslci_tech"],
            uslci_emission_filename=self.files["uslci_emission"],
            uslci_process_filename=self.files["uslci_process_adder"],
            stock_filename=self.files["stock_filename"],
            emissions_lci_filename=self.files["emissions_lci"],
            traci_lci_filename=self.files["traci_lci"],
            use_shortcut_lca_calculations=self.scen["flags"].get(
                "use_lcia_shortcut", True
            ),
            verbose = self.case["model_run"].get("lcia_verbose"),
            substitution_rate={
                mat: apply_array_uncertainty(rate, self.run)
                for mat, rate in self.scen["technology_components"]
                .get("substitution_rates")
                .items()
            },
            run=self.run,
        )

    def execute(self):
        """Execute one model run within the scenario."""
        start_year = self.case["model_run"].get("start_year")

        component_material_mass = pd.read_csv(self.files["component_material_mass"])

        component_total_mass = (
            component_material_mass.groupby(by=["year", "technology", "component"])
            .sum("mass_tonnes")
            .reset_index()
        )

        # Reset key CostGraph internal parameters to avoid re-initializing
        # from scratch.
        if self.run > 0:
            self.netw.run = self.run
            self.netw.cost_methods.run = self.run
            self.netw.year = start_year
            self.netw.path_dict["year"] = start_year
            self.netw.path_dict["component mass"] = component_total_mass.loc[
                component_total_mass.year == start_year, "mass_tonnes"
            ].values[0]
            self.netw.update_costs(self.netw.path_dict)
            self.netw.pathway_crit_history = list()

            self.lca.run = self.run

        timesteps_per_year = self.case["model_run"].get("timesteps_per_year")
        des_timesteps = int(
            timesteps_per_year * (self.case["model_run"].get("end_year") - start_year)
            + timesteps_per_year
        )

        circular_components = self.scen["technology_components"].get(
            "circular_components"
        )

        possible_components = list(
            self.scen["technology_components"].get("component_list", []).keys()
        )

        # Get list of unique materials involved in the case study
        materials = [
            self.scen["technology_components"].get("component_materials")[c]
            for c in circular_components
        ]
        material_list = [item for sublist in materials for item in sublist]

        # Create the DES context and tie it to the CostGraph
        self.context = Context(
            locations_filename=self.files["locs"],
            step_costs_filename=self.files["step_costs"]
            if self.scen["flags"].get("generate_step_costs")
            else self.files["step_costs_custom"],
            component_material_masses_filename=self.files["component_material_mass"],
            possible_components=possible_components,
            possible_materials=material_list,
            cost_graph=self.netw,
            cost_graph_update_interval_timesteps=self.case["model_run"].get(
                "cg_update"
            ),
            lca=self.lca,
            path_dict=self.scen["circular_pathways"],
            min_year=start_year,
            max_timesteps=des_timesteps,
            timesteps_per_year=timesteps_per_year,
            model_run=self.run,
        )

        print(f"Context initialized at {self.simtime(self.start)} s", flush=True)

        # Create the technology dataframe that will be used to populate
        # the context with components.
        technology_data = pd.read_csv(self.files["technology_data"])
        components = []
        for _, row in technology_data.iterrows():
            year = row["year"]
            in_use_facility_id = int(row["facility_id"])
            manuf_facility_id = self.netw.find_upstream_neighbor(
                int(row["facility_id"])
            )
            n_technology = int(row["n_technology"])

            for _ in range(n_technology):
                for c in circular_components:
                    components.append(
                        {
                            "year": year,
                            "kind": c,
                            "manuf_facility_id": manuf_facility_id,
                            "in_use_facility_id": in_use_facility_id,
                        }
                    )

        components = pd.DataFrame(components)

        # Create the lifespan functions for the components.
        lifespan_fns = {}

        # By default, all components are assigned fixed lifetimes
        for component in (
            self.scen["technology_components"].get("component_list").keys()
        ):
            lifespan_fns[component] = (
                lambda steps=apply_array_uncertainty(
                    self.scen["technology_components"].get("component_fixed_lifetimes")[
                        component
                    ],
                    self.run,
                ), convert=timesteps_per_year: steps
                * convert
            )

        # If fixed lifetimes are not being used, then apply the Weibull parameters
        # to the circular component(s) only. All non-circular components keep their
        # fixed lifetimes.
        if not self.scen["flags"].get("use_fixed_lifetime", True):
            for c in circular_components:
                lifespan_fns[c] = lambda: weibull_min.rvs(
                    self.scen["technology_components"].get("component_weibull_params")[
                        c
                    ]["K"],
                    loc=self.case["model_run"].get("min_lifespan"),
                    scale=self.scen["technology_components"].get(
                        "component_weibull_params"
                    )[c]["L"]
                    - self.case["model_run"].get("min_lifespan"),
                    size=1,
                    random_state=self.rng,
                )[0]

        print(f"Components initialized at {self.simtime(self.start)} s", flush=True)

        # Populate the context with components.
        self.context.populate(components, lifespan_fns)

        print(
            f"Context populated with components at {self.simtime(self.start)} s",
            flush=True,
        )

        # Run the context
        self.context.run()

    def postprocess(self):
        """Post-process, visualize, and save results of one model run."""

        # Create a name for the scenario, based either on a key in the original
        # scneario YAML or, if the key is not found, the filename of the scenario.

        default_scenario_identifier = self.scenario_filename.split("/")[-1].replace(
            ".yaml", ""
        )
        scenario_identifier = self.scen["scenario"].get(
            "name", default_scenario_identifier
        )
        seed = self.scen["scenario"]["seed"]
        run = self.run

        # Plot the cumulative count levels of the count inventories
        possible_component_list = list(
            self.scen["technology_components"].get("component_list", []).keys()
        )
        diagnostic_viz_counts = DiagnosticViz(
            facility_inventories=self.context.count_facility_inventories,
            output_plot_filename=self.files["component_counts_plot"],
            keep_cols=possible_component_list,
            start_year=self.case["model_run"].get("start_year"),
            timesteps_per_year=self.case["model_run"].get("timesteps_per_year"),
            component_count=self.scen["technology_components"].get("component_list"),
            var_name="unit",
            value_name="count",
            run=self.run,
        )
        count_cumulative_histories = (
            diagnostic_viz_counts.gather_and_melt_cumulative_histories()
        )

        with open(self.files["count_cumulative_histories"], "a") as f:
            count_cumulative_histories.to_csv(
                f, mode="a", header=f.tell() == 0, index=False, line_terminator="\n"
            )

        diagnostic_viz_counts.generate_plots()

        # Plot the levels of the mass inventories
        diagnostic_viz_mass = DiagnosticViz(
            facility_inventories=self.context.mass_facility_inventories,
            output_plot_filename=self.files["material_mass_plot"],
            keep_cols=possible_component_list,
            start_year=self.case["model_run"].get("start_year"),
            timesteps_per_year=self.case["model_run"].get("timesteps_per_year"),
            component_count=self.scen["technology_components"].get("component_list"),
            var_name="material",
            value_name="tonnes",
            run=self.run,
        )
        mass_cumulative_histories = (
            diagnostic_viz_mass.gather_and_melt_cumulative_histories()
        )

        diagnostic_viz_mass.generate_plots()

        # Postprocess and save CostGraph outputs
        self.netw.save_costgraph_outputs()

        # Join LCIA and locations computed and write the result to enable creation of
        # maps
        lcia_names = [
            "year",
            "facility_id",
            "material",
            "route_id",
            "stage",
            "state",
            "impact",
            "impact_value",
            "run",
        ]
        lcia_df = pd.read_csv(self.files["lcia_to_des"], names=lcia_names)
        locations_df = pd.read_csv(self.files["locs"])

        locations_columns = [
            "facility_id",
            "facility_type",
            "lat",
            "long",
            "region_id_1",
            "region_id_2",
            "region_id_3",
            "region_id_4",
        ]

        locations_select_df = locations_df.loc[:, locations_columns]
        lcia_process = lcia_df.loc[
            (lcia_df.run == self.run) & (lcia_df.route_id.isna())
        ]
        lcia_locations_df = lcia_process.merge(
            locations_select_df, how="inner", on="facility_id"
        ).drop_duplicates()

        with open(self.files["lcia_facility_results"], "a") as f:
            lcia_locations_df.to_csv(
                f, index=False, mode="a", header=f.tell() == 0, line_terminator="\n"
            )

        # Create and save LCIA results for transportation, by route county
        lcia_transpo = (
            lcia_df.dropna()
            .loc[lcia_df.run == self.run]
            .merge(
                pd.read_csv(
                    self.files["routes_computed"]
                    if self.scen["flags"]["use_computed_routes"]
                    else self.files["routes_custom"],
                    usecols=["route_id", "region_transportation", "vkmt", "total_vkmt"],
                ),
                on="route_id",
                how="outer",
            )
            .dropna(subset=["region_transportation"])
            .rename(
                columns={
                    "region_transportation": "fips",
                    "impact_value": "impact_total",
                    "vkmt": "vkmt_by_region",
                    "total_vkmt": "vkmt_total",
                }
            )
        )

        # Calculate transportation impacts by region (county) using county-level vkmt and route-level vkmt
        lcia_transpo["impact_value"] = (
            lcia_transpo.impact_total
            * lcia_transpo.vkmt_by_region
            / lcia_transpo.vkmt_total
        )

        # Drop unneeded columns
        lcia_transpo.drop(
            axis=1,
            columns=[
                "facility_id",
                "route_id",
                "material",
                "stage",
                "vkmt_by_region",
                "vkmt_total",
                "impact_total",
            ],
            inplace=True,
        )

        # When a route has multiple impact values in the same region, it means multiple road classes
        # were used. Sum these values to get one impact value per impact per region.
        # Groupby year-impact-fips and sum the impact_value over road classes
        # Save the disaggregated transportation impacts to file
        lcia_transpo_agg = (
            lcia_transpo.groupby(["year", "impact", "run", "fips"])
            .agg("sum")
            .reset_index()
            .astype(
                {
                    "year": "int",
                    "impact": "str",
                    "run": "int",
                    "fips": "int",
                    "impact_value": "float",
                }
            )
        )

        # Summarize log files into one place.

        lcia_summary = []
        for _, row in lcia_locations_df.iterrows():
            impact, units = self.impact_and_units(row["impact"])
            summary_row = {
                "units": units,
                "name": impact,
                "value": row["impact_value"],
            }
            lcia_summary.append(summary_row)

        lcia_summary = pd.DataFrame(lcia_summary)
        lcia_summary = lcia_summary.groupby(["units", "name"]).sum().reset_index()
        lcia_summary["seed"] = seed
        lcia_summary["run"] = run
        lcia_summary["scenario"] = scenario_identifier
        lcia_summary["category"] = "environmental impact"

        # Mass flow summary. Filter out zero tonne mass flows.
        mass_summary = mass_cumulative_histories.loc[:, ["facility_type", "tonnes"]].reset_index()
        mass_summary = mass_summary[mass_summary.facility_type.isin(self.scen.get("circular_pathways", {}).get("sc_end") + self.scen.get("circular_pathways", {}).get("sc_out_circ"))]
        mass_summary = mass_summary.groupby("facility_type").sum().reset_index()
        mass_summary = mass_summary.rename(
            columns={"facility_type": "name", "tonnes": "value"}
        )
        mass_summary["seed"] = seed
        mass_summary["run"] = run
        mass_summary["scenario"] = scenario_identifier
        mass_summary["category"] = "mass flow"
        mass_summary["units"] = "tonnes"

        # Store outflow and inflow circularity
        outflow_circularity, inflow_circularity = self.calculate_circularity_metrics(mass_cumulative_histories)

        circularity_rows = [
            {
                "value": outflow_circularity,
                "name": "Outflow Circularity",
                "units": "unitless",
                "scenario": scenario_identifier,
                "run": run,
                "seed": seed,
                "category": "circularity metric",
            },
            {
                "value": inflow_circularity,
                "name": "Inflow Circularity",
                "units": "unitless",
                "scenario": scenario_identifier,
                "run": run,
                "seed": seed,
                "category": "circularity metric",
            }
        ]

        circularity = pd.DataFrame(circularity_rows)

        # Collect all summaries and reorder the columns.
        cols = ["seed", "run", "scenario", "category", "name", "value", "units"]
        central_summary = pd.concat([lcia_summary, mass_summary, circularity])
        central_summary = central_summary.loc[:, cols]

        # Write all postprocessed log files.

        with open(self.files["mass_cumulative_histories"], "a") as f:
            mass_cumulative_histories.to_csv(
                f, mode="a", header=f.tell() == 0, index=False, line_terminator="\n"
            )

        with open(self.files["lcia_transpo_results"], "a") as f:
            lcia_transpo_agg.to_csv(
                f, index=False, mode="a", header=f.tell() == 0, line_terminator="\n"
            )

        with open(self.files["lcia_transpo_results"], "a") as f:
            lcia_transpo_agg.to_csv(
                f, index=False, mode="a", header=f.tell() == 0, line_terminator="\n"
            )

        with open(self.files["central_summary"], "a") as f:
            central_summary.to_csv(
                f, index=False, mode="a", header=f.tell() == 0, line_terminator="\n"
            )

    @staticmethod
    def impact_and_units(line_item):
        """
        Cleans up an impact name to return the units and the name of the impact
        as separate strings. It removes "(", ")", "[", "]" and thw word
        "substance" from units.

        Parameters
        ----------
        line_item: str
            Impact and unit mixed together from the LCIA.

        Returns
        -------
        str, str
            Tuple of impact name and units of that impact.
        """
        p_paren = re.compile("\(.*\)")
        p_square = re.compile("\[.*\]")

        all_paren = p_paren.findall(line_item)
        all_square = p_square.findall(line_item)

        if len(all_paren) > 0:
            units = all_paren[0]
            impact = line_item.replace(units, "")
        elif len(all_square) > 0:
            units = all_square[0]
            impact = line_item.replace(units, "")
        else:
            units = "unitless"
            impact = line_item

        impact = " ".join(impact.split())
        impact = impact.replace(" , ", ", ")

        units = (
            units.replace("(", "")
            .replace(")", "")
            .replace("[", "")
            .replace("]", "")
            .replace("substance", "")
        )

        return impact, units

    def calculate_circularity_metrics(self, mass):
        """
        Calculates the inflow and outflow circularity metrics from the mass flow dataframe
        provided.

        Parameters
        ----------
        mass: pandas.DataFrame
            The mass flow cumulative history from the scenario run

        Returns
        -------
        Tuple[float, float]
            A tuple of the outflow circularity metric and the inflow 
            circularity metric.
        """

        # Pivot the mass flows. Proper behavior results in a dataframe with a
        # single row.
        
        mass["scenario"] = "scenario"  # A dummy value only used for the pivot.
        tonnes_pivot = (
            mass.loc[:, ["scenario", "facility_type", "tonnes"]]
            .groupby(["scenario", "facility_type"])
            .sum()
            .reset_index()
            .pivot(index=["scenario"], columns="facility_type", values="tonnes")
            .reset_index()
        )

        # Get the facility types for circularity metric calculation.
        circular_pathways = self.scen.get("circular_pathways", {})

        # If the circular pathways are not in the config, report this and return 0s
        # for circularity metrics.

        if not circular_pathways:
            print("circular_pathways not found in scenario. Defaulting circularity metrics to 0.")
            outflow_circularity = 0.0
            inflow_circularity = 0.0
            return outflow_circularity, inflow_circularity

        # Get the lists of pathway facility types, defaulting to empty
        # lists so this method does not crash.
        # Place any strings into their own lists.

        sc_begin = circular_pathways.get("sc_begin", [])
        sc_end = circular_pathways.get("sc_end", [])
        sc_in_circ = circular_pathways.get("sc_in_circ", [])
        sc_out_circ = circular_pathways.get("sc_out_circ", [])

        sc_begin = sc_begin if type(sc_begin) == list else [sc_begin]
        sc_end = sc_end if type(sc_end) == list else [sc_end]
        sc_in_circ = sc_in_circ if type(sc_in_circ) == list else [sc_in_circ]
        sc_out_circ = sc_out_circ if type(sc_out_circ) == list else [sc_out_circ]

        # Outflow circularity

        outflow_numerator = 0.0
        for facility_type in sc_out_circ:
            if facility_type in tonnes_pivot:
                outflow_numerator += tonnes_pivot.loc[0, facility_type]
            else:
                print(f'Circular pathway facility type {facility_type} in config not found in pivot, skipping.')

        outflow_denominator = 1.0 if len(sc_end + sc_out_circ) == 0 else 0.0
        for facility_type in sc_out_circ + sc_end:
            if facility_type in tonnes_pivot:
                outflow_denominator += tonnes_pivot.loc[0, facility_type]
            else:
                print(f'Circular pathway facility type {facility_type} in config not found in pivot, skipping.')

        outflow_circularity = outflow_numerator / outflow_denominator

        # Inflow circularity

        inflow_numerator = 0.0
        for facility_type in sc_in_circ:
            if facility_type in tonnes_pivot:
                inflow_numerator += tonnes_pivot.loc[0, facility_type]
            else:
                print(f'Circular pathway facility type {facility_type} in config not found in pivot, skipping.')

        inflow_denominator = 1.0 if len(sc_begin + sc_in_circ) == 0 else 0.0
        for facility_type in sc_in_circ + sc_begin:
            if facility_type in tonnes_pivot:
                inflow_denominator += tonnes_pivot.loc[0, facility_type]
            else:
                print(f'Circular pathway facility type {facility_type} in config not found in pivot, skipping.')

        inflow_circularity = inflow_numerator / inflow_denominator

        return outflow_circularity, inflow_circularity

    def clear_results(self):
        """Move old CSV results files to a timestamped sub-directory."""
        # If the user wants to remove old results from the results directory,
        if self.scen["flags"].get("clear_results", True):
            # Define the new directory name uniquely using a timestamp.
            _dir = "results-" + str(self.simtime(0.0))
            # Create the directory.
            os.makedirs(os.path.join(self.args.data, _dir))
            # For all CSV results files,
            for _n, _f in self.case["files"].get("results").items():
                # If the file exists in the results directory,
                if os.path.isfile(self.files[_n]):
                    # Move it to the newly created, timestamped directory
                    os.rename(self.files[_n], os.path.join(self.args.data, _dir, _f))
            # For all diagnostic plots in the results directory,
            for _f in os.listdir(
                os.path.join(self.args.data, self.case["directories"].get("results"))
            ):
                if _f.endswith(".png"):
                    os.rename(
                        os.path.join(
                            self.args.data, self.case["directories"].get("results"), _f
                        ),
                        os.path.join(self.args.data, _dir, _f),
                    )
            # After processing all results files, if the timestamped directory
            # is empty, delete it (there were no results files to move)
            if not os.listdir(os.path.join(self.args.data, _dir)):
                os.rmdir(os.path.join(self.args.data, _dir))

    @staticmethod
    def simtime(starttime):
        """
        Record the current simulation time to one decimal point.

        The simulation time does not reset for additional model runs.

        Parameters
        ----------
        starttime : float
            Time that the simulation began.

        Return
        ------
        [float]
            Time since simulation began.
        """
        if starttime == 0.0:
            return int(np.round(time.time()))
        else:
            return int(np.round(time.time() - starttime))
