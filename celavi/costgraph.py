import networkx as nx
from csv import DictReader
import pandas as pd
import numpy as np

# the locations dataset will be the largest; try to read that one in line by
# line. All other datasets will be relatively small, so storing and
# manipulating the entire dataset within the Python environment shouldn't
# slow execution down noticeably
mockdata = "C:/Users/rhanes/Box Sync/Circular Economy LDRD/data/input-data-mockup.xlsx"
steps_df = pd.read_excel(mockdata, sheet_name='edges')
costs_df = pd.read_excel(mockdata, sheet_name='costs')
interconnect_df = pd.read_excel(mockdata, sheet_name='interconnections')
loc_df = "C:/Users/rhanes/Box Sync/Circular Economy LDRD/data/loc-mock.csv"

# @todo move cost methods here

# @note cost, next state, relocation destination for the component

class CostGraph:
    """
        Contains methods for reading in graph data, creating network of
        facilities in a supply chain, and finding preferred pathways for
        implementation.
    """

    def __init__(self,
                 input_name : str,
                 locations_file : str,
                 routes_file : str):
        """
        Reads in small datasets to DataFrames and stores the path to the large
        locations dataset for later use.

        Parameters
        ----------
        input_name
            File name or other identifier where input datasets are stored
        locations_file
            path to dataset of facility locations
        routes_file
            path to dataset of routes between facilities
        """
        # @todo update file IO method to match actual input data format
        self.steps_df=pd.read_excel(input_name, sheet_name='edges')
        self.costs_df=pd.read_excel(input_name, sheet_name='costs')
        self.interconnect_df=pd.read_excel(input_name, sheet_name='interconnections')

        # the location dataset is read in and processed line by line
        self.loc_df=locations_file

        # @todo read in the routes dataset line by line?
        self.routes_df=routes_file

        # create empty instance variable for supply chain DiGraph
        self.supply_chain = nx.DiGraph()


    @staticmethod
    def get_node_names(facilityID : [int, str],
                       subgraph_steps: list):
        """
        Generates a list of unique node names from a list of processing steps
        and a unique facility ID

        Parameters
        ----------
        facilityID: [int, str]
            Unique facility identifier.

        subgraph_steps: list of strings
            List of processing steps at this facility

        Returns
        -------
        list of strings
            List of unique node IDs created from processing step and facility ID
        """
        return ["{}{}".format(i, str(facilityID)) for i in subgraph_steps]

    @staticmethod
    def node_filter(graph : nx.DiGraph,
                    attr_key : str,
                    get_val):
        """
        Finds node names in graph that have 'attr_key': get_val in
             their attribute dictionary

        Parameters
        ----------
        graph
            a networkx DiGraph containing at least one node with a node
            attribute dictionary
        attr_key
            key in the attribute dictionary on which to filter nodes in graph
        get_val
            value of attribute key on which to filter nodes in graph

        Returns
        -------
            list of names of nodes (str) in graph
        """
        _out = [x for x, y in graph.nodes(data=True) if y[attr_key] == get_val]

        return _out

    @staticmethod
    def list_of_tuples(list1 : list,
                       list2: list):
        """
        Converts two lists into a list of tuples where each tuple contains
        one element list1 and one element from list2:
        [(list1[0], list2[0]), (list1[1], list2[1]), ...]

        Parameters
        ----------
        list1
            list of any data type
        list2
            list of any data type

        Returns
        -------
            list of tuples
        """
        return list(map(lambda x, y: (x, y), list1, list2))

    def get_edges(self,
                  facility_df : pd.DataFrame,
                  u_edge='step',
                  v_edge='next_step'):
        """
        Converts two columns of node names into a list of string tuples
        for edge definition with networkx

        Parameters
        ----------
        facility_df
            DataFrame listing processing steps (u_edge) and the next
            processing step (v_edge) by facility type
        u_edge
            unique processing steps within a facility type
        v_edge
            steps to which the processing steps in u_edge connect

        Returns
        -------
            list of string tuples that define edges within a facility type
        """
        _type = facility_df['facility_type'].values[0]

        _out = self.steps_df[[u_edge, v_edge]].loc[self.steps_df.facility_type == _type].dropna().to_records(index=False).tolist()

        return _out


    def get_nodes(self,
                  facility_df : pd.DataFrame):
        """
        Generates a data structure that defines all nodes and node attributes
        for a single facility.

        Parameters
        ----------
        facility_df : pd.DataFrame
            DataFrame containing unique facility IDs, processing steps, and
            the name of the method (if any) used to calculate processing costs
            Column names in facility_df must be:
                ['facility_id', 'step', 'cost_method']

        Returns
        -------
            List of (str, dict) tuples used to define a networkx DiGraph
            Attributes are: processing step, cost method, facility ID, and
            region identifiers

        """

        _id = facility_df['facility_id'].values[0]

        # list of nodes (processing steps) within a facility
        _node_names = self.costs_df['step'].loc[self.costs_df.facility_id == _id].tolist()

        # data frame matching facility processing steps with methods for cost
        # calculation over time
        _step_cost = self.costs_df[['step','cost_method','facility_id']].loc[self.costs_df.facility_id == _id]

        # create list of dictionaries from data frame with processing steps,
        # cost calculation method, and facility-specific region identifiers
        _attr_data = _step_cost.merge(facility_df,how='outer',on='facility_id').to_dict(orient='records')

        # reformat data into a list of tuples as (str, dict)
        _nodes = self.list_of_tuples(_node_names, _attr_data)

        return _nodes


    def build_facility_graph(self,
                             facility_df : pd.DataFrame):
        """
        Creates networkx DiGraph object containing nodes, intra-facility edges,
        and all relevant attributes for a single facility.

        Parameters
        ----------
        facility_df
            DataFrame with a single row that defines a supply chain facility.
            facility_df must contain the columns:
            ['facility_id', 'facility_type', 'lat', 'long', 'region_id_1',
            'region_id_2', 'region_id_3', 'region_id_4']

        Returns
        -------
            networkx DiGraph
        """
        # Create empty directed graph object
        _facility = nx.DiGraph()

        # Generates list of (str, dict) tuples for node definition
        _facility_nodes = self.get_nodes(facility_df)

        # Populate the directed graph with node names and attribute
        # dictionaries
        _facility.add_nodes_from(_facility_nodes)

        # Populate the directed graph with edges
        # Edges within facilities don't have transportation costs or distances
        # associated with them.
        _facility.add_edges_from(self.get_edges(facility_df),
                                 cost=0,
                                 dist=0)

        # Use the facility ID and list of processing steps in this facility to
        # create a list of unique node names
        # Unique meaning over the entire supply chain (graph of subgraphs)
        _node_names = list(_facility.nodes)
        _id = facility_df['facility_id'].values[0]
        _node_names_unique = self.get_node_names(_id,_node_names)

        # Construct a dict of {'old node name': 'new node name'}
        _labels = {}
        for i in np.arange(0, len(_node_names_unique)):
            _labels.update({_node_names[i]: _node_names_unique[i]})

        # Relabel nodes to unique names (step + facility ID)
        nx.relabel_nodes(_facility, _labels, copy=False)

        return _facility


    def build_supplychain_graph(self):
        """
        Reads in the locations data set line by line. Each line becomes a
        DiGraph representing a single facility. Facility DiGraphs are
        added onto a supply chain DiGraph and connected with inter-facility
        edges. Edges within facilities have no cost or distance. Edges
        between facilities have costs defined in the interconnections
        dataset and distances defined in the routes dataset.

        # @todo connect to regionalizations: every sub-graph needs a location
        and a distance to every connected sub-graph

        Returns
        -------
        @todo after debugging, remove the return value
        self.supply_chain
            networkx DiGraph
        """

        # add all facilities and intra-facility edges to supply chain
        with open(self.loc_df, 'r') as _loc_file:

            _reader = pd.read_csv(_loc_file, chunksize=1)

            for _line in _reader:

                # Build the subgraph representation and add it to the list of facility
                # subgraphs
                _fac_graph = self.build_facility_graph(facility_df = _line)

                # add onto the supply supply chain graph
                self.supply_chain.add_nodes_from(_fac_graph.nodes(data=True))
                self.supply_chain.add_edges_from(_fac_graph.edges(data=True))

        # add all inter-facility edges, with costs but without distances
        # this is a relatively short loop
        for index, row in interconnect_df.iterrows():
            _u = row['u_step']
            _v = row['v_step']
            _edge_cost = row['cost_method']

            # get two lists of nodes to connect based on df row
            _u_nodes = self.node_filter(self.supply_chain, 'step', _u)
            _v_nodes = self.node_filter(self.supply_chain, 'step', _v)

            _edge_list = self.list_of_tuples(_u_nodes, _v_nodes)

            self.supply_chain.add_edges_from(_edge_list,
                                             cost=_edge_cost,
                                             dist=None)

        # read in and process routes line by line
        with open(self.routes_df, 'r') as _route_file:

            _reader = pd.read_csv(_route_file, chunksize=1)

            for _line in _reader:
                # get all nodes that this route connects
                _u = self.node_filter(self.supply_chain,
                                      'facility_id',
                                      _line['source_facility_id'])
                _v = self.node_filter(self.supply_chain,
                                      'facility_id',
                                      _line['destination_facility_id'])

                # look for edges in supply_chain that connect _u and _v


                # for every tuple in the edge list, look for a row in routes
                # where source_facility_id matches the facility ID of u and
                # destination_facility_id matches the facility ID of V, and pull
                # the total_vmt from that row

        return self.supply_chain


    def enumerate_paths(self):
        """

        Returns
        -------

        """
        # Calculate total pathway costs (sum of all node and edge costs) over all
        # possible pathways

        # @note simple paths have no repeated nodes; this might not work for a cyclic
        #  graph
        # The target parameter can be replaced by a list

        path_edge_list = list([])

        # Get list of nodes and edges by pathway
        for path in map(nx.utils.pairwise,
                        nx.all_simple_paths(self.supply_chain, source='in use',
                                            target='landfill')):
            path_edge_list.append(list(path))

        path_node_list = list(nx.all_simple_paths(self.supply_chain, source='in use', target='landfill'))

        # dictionary defining all possible pathways by nodes and edges
        # nodes = calculate total processing costs
        # edges = calculate total transportation costs and distances
        paths_dict = {'nodes': path_node_list, 'edges': path_edge_list}

        for edges in paths_dict['edges']:
            for u, v in edges:
                print(u, v, self.supply_chain.get_edge_data(u, v))

        for nodes, edges in zip(path_node_list, path_edge_list):
            costs = [self.supply_chain.get_edge_data(u, v)['cost'] for u, v in edges]
            distances = [self.supply_chain.get_edge_data(u, v)['distance'] for u, v in
                         edges]
            graph_path = ",".join(nodes)
            print(
                f"Path: {graph_path}. Total cost={sum(costs)}, total distance={sum(distances)}")

    def update_paths(self):
        pass
        # @todo dynamically update node costs based on cost-over-time and learning-by-
        #  doing models

        # The edges between sub-graphs will have different transportation costs depending
        # on WHAT's being moved: blade segments or ground/shredded blade material.
        # @note Is there a way to also track component-material "status" or general
        #  characteristics as it traverses the graph? Maybe connecting this graph to
        #  the state machine

test = CostGraph(input_name=mockdata, locations_file=loc_df)

sc_graph = test.build_supplychain_graph()


