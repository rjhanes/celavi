from typing import List, Dict, Deque, Tuple
from collections import deque

from .unique_identifier import UniqueIdentifier
from .states import StateTransition, NextState


class Component:
    """
    This class models a component in the discrete event simulation (DES) model.

    transitions_table: Dict[StateTransition, NextState]
        The transition table for the state machine. E.g., when a component
        begins life, it enters the "use" state. When a component is in the
        "use" state, it can receive the transition "landfilling", which
        transitions the component into the "landfill" state.
    """

    def __init__(
        self,
        context,
        kind: str,
        xlong: float,
        ylat: float,
        year: int,
        lifespan_timesteps: float,
        mass_tonnes: float,
    ):
        """
        This takes parameters named the same as the instance variables. See
        the comments for the class for further information about instance
        attributes.

        It sets the initial state, which is an empty string. This is
        because there is no state until the component begins life, when
        the process defined in method begin_life() is called by SimPy.

        Parameters
        ----------
        context: Context
            The context that contains this component.

        kind: str
            The type of this component. The word "type", however, is also a Python
            keyword, so this attribute is named kind.

        xlong: float
            The longitude of the component.

        ylat: float
            The latitude of the component.

        year: int
            The year in which this component enters the use state for the first
            time.

        intitial_lifespan_timesteps: float
            The lifespan, in timesteps, of the component during its first
            In use phase. The argument can be
            provided as a floating point value, but it is converted into an
            integer before it is assigned to the instance attribute. This allows
            more intuitive integration with random number generators defined
            outside this class which may return floating point values.

        mass_tonnes: float
            The total mass of the component, in tonnes.
        """

        self.phase = ""  # There is no location initially
        self.context = context
        self.id = UniqueIdentifier.unique_identifier()
        self.kind = kind
        self.xlong = xlong
        self.ylat = ylat
        self.year = year
        self.mass_tonnes = mass_tonnes
        self.initial_lifespan_timesteps = int(lifespan_timesteps)  # timesteps
        self.pathway: Deque[Tuple[str, int]] = deque()

    def begin_life(self, env):
        """
        This process should be called exactly once during the discrete event
        simulation. It is what starts the lifetime this component. Since it is
        only called once, it does not have a loop, like most other SimPy
        processes. When the component enters life, this method immediately
        sets the end-of-life (EOL) process for the use state.

        Parameters
        ----------
        env: simpy.Environment
            The SimPy environment running the DES timesteps.
        """
        begin_timestep = (self.year - self.context.min_year) / self.context.years_per_timestep
        yield env.timeout(begin_timestep)
        path_choice = self.context.cost_graph.choose_paths()
        self.pathway = deque()

        # for phase in path_choice[0]['path']:
        #     self.pathway.append(phase)

        for facility, lifespan in path_choice[0]['path']:
            if facility.startswith("in use"):
                self.pathway.append((facility, self.initial_lifespan_timesteps))
            elif facility.startswith("landfill"):
                self.pathway.append((facility, self.context.max_timesteps * 2))
            else:
                self.pathway.append((facility, lifespan))

        env.process(self.eol_process(env))

    def transition(self, transition: str, timestep: int) -> None:
        """
        Transition the component's state machine from the current state based on a
        transition.

        Parameters
        ----------
        transition: str
            The next state to transition into.

        timestep: int
            The discrete timestep.

        Returns
        -------
        None

        Raises
        ------
        KeyError
            Raises a KeyError if the transition is not accessible from the
            current state.
        """
        lookup = StateTransition(state=self.state, transition=transition)
        if lookup not in self.transitions_table:
            raise KeyError(
                f"transition: {transition} not allowed from current state {self.state}"
            )
        next_state = self.transitions_table[lookup]
        self.state = next_state.state
        self.initial_lifespan_timesteps = next_state.lifespan
        # self.transition_list.append(transition)
        if next_state.state_entry_function is not None:
            next_state.state_entry_function(self.context, self, timestep)
        if next_state.state_exit_function is not None:
            next_state.state_exit_function(self.context, self, timestep)

    def eol_process(self, env):
        """
        This process controls the state transitions for the component at
        each end-of-life (EOL) event.

        Parameters
        ----------
        env: simpy.Environment
            The environment in which this process is running.
        """
        while True:
            if len(self.pathway) > 0:
                location, lifespan = self.pathway.popleft()
                count_inventory = self.context.count_facility_inventories[location]
                mass_inventory = self.context.mass_facility_inventories[location]
                count_inventory.increment_quantity(self.kind, 1, env.now)
                mass_inventory.increment_quantity(self.kind, self.mass_tonnes, env.now)
                yield env.timeout(lifespan)
                count_inventory.increment_quantity(self.kind, -1, env.now)
                mass_inventory.increment_quantity(self.kind, -self.mass_tonnes, env.now)
            else:
                break
