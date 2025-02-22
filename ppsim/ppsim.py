"""
A module for simulating population protocols.

The main class :any:`Simulation` is created with a description of the protocol and the initial condition,
and is responsible for running the simulation.

The general syntax is

.. code-block:: python

    a, b, u = 'A', 'B', 'U'
    approx_majority = {
        (a,b): (u,u),
        (a,u): (a,a),
        (b,u): (b,b),
    }
    n = 10 ** 5
    init_config = {a: 0.51 * n, b: 0.49 * n}
    sim = Simulation(init_config=init_config, rule=approx_majority)
    sim.run()
    sim.history.plot()
    plt.title('approximate majority protocol')
    plt.xlim(0, sim.times[-1])
    plt.ylim(0, n)

More examples given in https://github.com/UC-Davis-molecular-computing/population-protocols-python-package/tree/main/examples

:any:`Snapshot` is a base class for snapshot objects that get are updated by Simulation,
used to visualize the protocol during or after the simulation has run.

:any:`StatePlotter` is a subclass of Snapshot that gives a barplot visualizing the
counts of all states and how they change over time.

:py:meth:`time_trials` is a convenience function used for gathering data about the
convergence time of a protocol.
"""

import dataclasses
from dataclasses import dataclass
from datetime import timedelta
import math
from time import perf_counter
from typing import Union, Hashable, Dict, Tuple, Callable, Optional, List, Iterable, Set

import ipywidgets as widgets
import matplotlib.pyplot as plt
from natsort import natsorted
import numpy as np
import pandas as pd
import seaborn as sns
from tqdm.auto import tqdm

from . import simulator
from ppsim.crn import Reaction, reactions_to_dict

# TODO: these names are not showing up in the mouseover information
State = Hashable
Output = Union[Tuple[State, State], Dict[Tuple[State, State], float]]
TransitionFunction = Callable[[State, State], Output]
Rule = Union[TransitionFunction, Dict[Tuple[State, State], Output], Iterable[Reaction]]
ConvergenceDetector = Callable[[Dict[State, int]], bool]


# TODO: give other option for when the number of reachable states is large or unbounded
def state_enumeration(init_dist: Dict[State, int], rule: Callable[[State, State], Output]) -> Set[State]:
    """Finds all reachable states by breadth-first search.

    Args:
        init_dist: dictionary mapping states to counts
            (states are any hashable type, commonly NamedTuple or String)
        rule: function mapping a pair of states to either a pair of states
            or to a dictionary mapping pairs of states to probabilities

    Returns:
        a set of all reachable states
    """
    checked_states = set()
    unchecked_states = set(init_dist.keys())
    while len(unchecked_states) > 0:
        unchecked_state = unchecked_states.pop()
        if unchecked_state not in checked_states:
            checked_states.add(unchecked_state)
        for checked_state in checked_states:
            for new_states in [rule(checked_state, unchecked_state),
                               rule(unchecked_state, checked_state)]:
                if new_states is not None:
                    if isinstance(new_states, dict):
                        # if the output is a distribution
                        new_states = sum(new_states.keys(), ())
                    for new_state in new_states:
                        if new_state not in checked_states and new_state not in unchecked_states:
                            unchecked_states.add(new_state)
    return checked_states


class Snapshot:
    """"Base class for Snapshot objects.

    Attributes:
        simulation: The Simulation object that initialized and will update the Snapshot.
            This attribute gets set when the Simulation object calls add_snapshot.
        update_time: How many seconds will elapse between calls to update while
            running sim.
        time: The parallel time at the current snapshot. Changes when self.update is
            called.
        config: The configuration array at the current snapshot. Changes when
            self.update is called.
    """

    def __init__(self) -> None:
        """Init construction for the base class.

        Parameters can be passed in here, and any attributes that can be defined
        without the parent Simulation object can be instantiated here, such as
        self.update_interval.
        """
        self.simulation = None
        self.update_time = 0.1
        self.time = None
        self.config = None

    def initialize(self):
        """Method which is called once during add_snapshot.

        Any initialization that requires accessing the data in self.simulation
        should go here.
        """
        pass

    def update(self, index: Optional[int] = None):
        """Method which is called while the Simulation is running.

        Args:
            index: An optional integer index. If present, the snapshot will use the
                data from the configuration at self.sim.configs[index] and time
                self.sim.times[index]. Otherwise, the snapshot will use the current
                configuration self.sim.config_array and current time self.sim.time.
        """
        if type(index) is int:
            self.time = self.simulation.times[index]
            self.config = self.simulation.configs[index]
        else:
            self.time = self.simulation.time
            self.config = self.simulation.config_array


class TimeUpdate(Snapshot):
    """Simple Snapshot that prints the current time in the Simulator.

    When calling Simulator.run, if there are no current Snapshots present, then
    this object will get added to provide a basic progress update.
    """

    def update(self, index: Optional[int] = None):
        super().update(index)
        print(f'\r Time: {self.time:.3f}', end='\r')


# TODO: add seed
@dataclass
class Simulation:
    """Class to simulate a population protocol."""

    state_list: List[State]
    """A sorted list of all reachable states."""
    state_dict: Dict[State, int]
    """Maps states to their integer index to be used
            in array representations."""
    simulator: simulator.Simulator
    """An internal Simulator object, whose methods actually
            perform the steps of the simulation."""
    configs: List[np.ndarray]
    """A list of all configurations that have been
            recorded during the simulation, as integer arrays."""
    time: float
    """The current time of the Simulation."""
    times: List[Union[float, timedelta]]
    """A list of all the corresponding times for configs,
            in units of parallel time."""
    steps_per_time_unit: float
    """Number of simulated interactions per time unit."""
    time_units: Optional[str]
    """The units that time is in."""
    continuous_time: bool
    """Whether continuous time is used. The regular discrete
            time model considers a steps_per_time_unit steps to be 1 unit of time.
            The continuous time model is a poisson process, with expected
            steps_per_time_unit number of steps per 1 unit of time."""
    column_names: Union[pd.MultiIndex, List[str]]
    """Columns representing all states for pandas dataframe.
            If the State is a tuple, NamedTuple, or dataclass, this will be a
            pandas MultiIndex based on the various fields.
            Otherwise it is list of str(State) for each State."""
    snapshots: List[Snapshot]
    """A list of Snapshot objects, which get
            periodically called during the running of the simulation to give live
            updates."""
    rng: np.random.Generator
    """A numpy random generator used to sample random variables outside the
            cython code."""
    seed: Optional[int]
    """The optional integer seed used for rng and inside cython code."""

    def __init__(self, init_config: Dict[State, int], rule: Rule, simulator_method: str = "MultiBatch",
                 transition_order: str = "asymmetric", seed: Optional[int] = None,
                 volume: Optional[float] = None, continuous_time: bool = False, time_units: Optional[str] = None,
                 **kwargs):
        """Initialize a Simulation.

        Args:
            init_config (Dict[State, int]): The starting configuration, as a
                dictionary mapping states to counts.
            rule: A representation of the transition rule. The first two options are
                a dictionary, whose keys are tuples of 2 states and values are their
                outputs, or a function which takes pairs of states as input. For a
                deterministic transition function, the output is a tuple of 2 states.
                For a probabilistic transition function, the output is a dictionary
                mapping tuples of states to probabilities. Inputs that are not present
                in the dictionary, or return None from the function, are interpreted as
                null transitions that return the same pair of states as the output.
                The third option is a list of Reactions describing a CRN, which will
                be parsed into an equivalent population protocol.
            simulator_method (str): Which Simulator method to use, either 'MultiBatch'
                or 'Sequential'.
                The MultiBatch simulator does O(sqrt(n)) interaction steps in parallel
                using batching, and is much faster for large population sizes and
                relatively small state sets.
                The Sequential represents the population as an array of agents, and
                simulates each interaction step by choosing a pair of agents to update.
                Defaults to 'MultiBatch'.
            transition_order (str): Should the rule be interpreted as being symmetric,
                either 'asymmetric', 'symmetric', or 'symmetric_enforced'.
                Defaults to 'asymmetric'.
                'asymmetric': Ordering of the inputs matters, and all inputs not
                    explicitly given as assumed to be null interactions.
                'symmetric': The input pairs are interpreted as unordered. If rule(a, b)
                    returns None, while rule(b, a) has a non-null output, then the
                    output of rule(a, b) is assumed to be the same as rule(b, a).
                    If rule(a, b) and rule(b, a) are each given, there is no effect.
                    Asymmetric interactions can be explicitly included this way.
                'symmetric_enforced': The same as symmetric, except that if rule(a, b)
                    and rule(b, a) are non-null and do not give the same set of outputs,
                    a ValueError is raised.
            seed: An optional integer used as the seed for all pseudorandom number
                generation. Defaults to None.
            volume: If a list of Reactions is given for a CRN, then the parameter volume
                can be passed in here. Defaults to None. If None, the volume will be
                assumed to be the population size n.
            continuous_time: Whether continuous time is used. Defaults to False.
                If a CRN as a list of reactions is passed in, this will be set to True.
            time_units: An optional string given the units that time is in. Defaults to None.
                This must be a valid string to pass as unit to pandas.to_timedelta.
            **kwargs: If rule is a function, other keyword function parameters are
                passed in here.
        """
        self.seed = seed
        self.rng = np.random.default_rng(seed)
        self.n = sum(init_config.values())
        self.steps_per_time_unit = self.n
        self.time_units = time_units
        self.continuous_time = continuous_time
        # if rule is iterable of Reactions from the crn module, then convert to dict
        rule_is_reaction_iterable = True
        try:
            for reaction in rule:
                if not isinstance(reaction, Reaction):
                    rule_is_reaction_iterable = False
                    break
        except:
            # might end up here if rule is not even iterable, e.g., is a function
            rule_is_reaction_iterable = False
        if rule_is_reaction_iterable:
            if volume is None:
                volume = self.n
            rule, rate_max = reactions_to_dict(rule, self.n, volume)
            self.steps_per_time_unit *= rate_max
            # Default to continuous time for lists of reactions
            self.continuous_time = True

        self._rule = rule
        self._rule_kwargs = kwargs

        # Get a list of all reachable states, use the natsort library to put in a nice order.
        self.state_list = natsorted(list(state_enumeration(init_config, self.rule)),
                                    key=lambda x: repr(x))
        # TODO: process a dictionary in a more straightforward way, and check that state_list only includes
        #         states in the dictionary
        self.state_dict = {state: i for i, state in enumerate(self.state_list)}

        if simulator_method.lower() == 'multibatch':
            self._method = simulator.SimulatorMultiBatch
        elif simulator_method.lower() == 'sequential':
            self._method = simulator.SimulatorSequentialArray
        else:
            raise ValueError('simulator_method must be multibatch or sequential')
        self._transition_order = transition_order
        self.initialize_simulator(self.array_from_dict(init_config))

        # Check an arbitrary state to see if it has fields.
        # This will be true for either a tuple, NamedTuple, or dataclass.
        state = next(iter(init_config.keys()))
        # Check for dataclass.
        if dataclasses.is_dataclass(state):
            field_names = [field.name for field in dataclasses.fields(state)]
            tuples = [dataclasses.astuple(state) for state in self.state_list]
        else:
            # Check for NamedTuple.
            field_names = getattr(state, '_fields', None)
            tuples = self.state_list
        # Check also for tuple.
        if (field_names and len(field_names) > 1)\
                or (isinstance(state, tuple) and len(state) > 1):
            # Make a MultiIndex only if there are multiple fields
            self.column_names = pd.MultiIndex.from_tuples(tuples, names=field_names)
        else:
            self.column_names = [str(i) for i in self.state_list]
        self.configs = []
        self.times = []
        self.time = 0
        self.add_config()
        # private history dataframe is initially empty, updated by the getter of property self.history
        self._history = pd.DataFrame(data=self.configs, index=pd.Index(self.times_in_units(self.times)),
                                     columns=self.column_names)
        self.snapshots = []

    def rule(self, a, b):
        """The rule, as a function of two input states."""
        # If the input rule was a dict
        if type(self._rule) == dict:
            if (a, b) in self._rule:
                return self._rule[(a, b)]
        # If the input rule was a function, with possible kwargs
        elif callable(self._rule):
            # Make a fresh copy in the case of a dataclass in case the function mutates a, b
            if dataclasses.is_dataclass(a):
                a, b = dataclasses.replace(a), dataclasses.replace(b)
            output = self._rule(a, b, **self._rule_kwargs)
            # If function just mutates a, b but doesn't return, then return new a, b values
            return (a, b) if output is None else output
        else:
            raise TypeError("rule must be either a dict or a callable.")

    def initialize_simulator(self, config):
        """Build the data structures necessary to instantiate the Simulator class.

        Args:
            config: The config array to instantiate the Simulator.
        """
        q = len(self.state_list)
        delta = np.zeros((q, q, 2), dtype=np.intp)
        null_transitions = np.zeros((q, q), dtype=bool)
        random_transitions = np.zeros((q, q, 2), dtype=np.intp)
        random_outputs = []
        transition_probabilities = []
        for i, a in enumerate(self.state_list):
            for j, b in enumerate(self.state_list):
                output = self.rule(a, b)
                # when output is a distribution
                if type(output) == dict:
                    s = sum(output.values())
                    assert s <= 1, "The sum of output probabilities must be <= 1."
                    # ensure probabilities sum to 1
                    if 1 - s:
                        if (a, b) in output.keys():
                            output[(a, b)] += 1 - s
                        else:
                            output[(a, b)] = 1 - s
                    if len(output) == 1:
                        # distribution only had 1 output, not actually random
                        output = next(iter(output.keys()))
                    else:
                        # add (number of outputs, index to outputs)
                        random_transitions[i, j] = (len(output), len(random_outputs))
                        for (x, y) in output.keys():
                            random_outputs.append((self.state_dict[x], self.state_dict[y]))
                        transition_probabilities.extend(list(output.values()))
                if output is None or set(output) == {a, b}:
                    null_transitions[i, j] = True
                    delta[i, j] = (i, j)
                elif type(output) == tuple:
                    delta[i, j] = (self.state_dict[output[0]], self.state_dict[output[1]])
        random_outputs = np.array(random_outputs, dtype=np.intp)
        transition_probabilities = np.array(transition_probabilities, dtype=float)

        if self._transition_order.lower() in ['symmetric', 'symmetric_enforced']:
            for i in range(q):
                for j in range(q):
                    # Set the output for i, j to be equal to j, i if null
                    if null_transitions[i, j]:
                        null_transitions[i, j] = null_transitions[j, i]
                        delta[i, j] = delta[j, i]
                        random_transitions[i, j] = random_transitions[j, i]
                    # If i, j and j, i are both non-null, with symmetric_enforced, check outputs are equal
                    elif self._transition_order.lower() == 'symmetric_enforced' \
                            and not null_transitions[j, i]:
                        if sorted(delta[i, j]) != sorted(delta[j, i]) or \
                                random_transitions[i, j, 0] != random_transitions[j, i, 0]:
                            a, b = self.state_list[i], self.state_list[j]
                            raise ValueError(f'''Asymmetric interaction:
                                            {a, b} -> {self.rule(a, b)}
                                            {b, a} -> {self.rule(b, a)}''')

        self.simulator = self._method(config, delta, null_transitions,
                                      random_transitions, random_outputs, transition_probabilities, self.seed)

    def array_from_dict(self, d: Dict) -> np.ndarray:
        """Convert a configuration dictionary to an array.

        Args:
            d: A dictionary mapping states to counts.

        Returns: An array giving counts of all states, in the order of
            self.state_list.
        """

        a = np.zeros(len(self.state_list), dtype=np.int64)
        for k in d.keys():
            a[self.state_dict[k]] += d[k]
        return a

    def run(self, run_until: Union[float, ConvergenceDetector] = None, history_interval: float = 1.,
            stopping_interval: float = 1., timer: bool = True) -> None:
        """Runs the simulation.

        Can give a fixed amount of time to run the simulation, or a function that checks
        the configuration for convergence.

        Args:
            run_until: The stop condition. To run for a fixed amount of parallel time, give
                a numerical value. To run until a convergence criterion, give a function
                mapping a configuration (as a dictionary mapping states to counts) to a
                boolean. The run will stop when the function returns True.
                Defaults to None. If None, the simulation will run until the configuration
                is silent (all transitions are null). This only works with the multibatch
                simulator method, if another simulator method is given, then using None will
                raise a ValueError.
            history_interval: The length to run the simulator before recording data,
                in units of parallel time (n steps). Defaults to 1.
                This can either be a float, or a function that takes the current time and
                and returns a float.
            stopping_interval: The length to run the simulator before checking for the stop
                condition.
            timer: If True, and there are no other snapshot objects, a default TimeUpdate
                Snapshot will be created to print the current parallel time.
        """
        if len(self.snapshots) == 0 and timer is True:
            self.add_snapshot(TimeUpdate())

        end_time = None
        # stop_condition() returns True when it is time to stop
        if run_until is None:
            if type(self.simulator) != simulator.SimulatorMultiBatch:
                raise ValueError('Running until silence only works with multibatch simulator.')

            def stop_condition():
                return self.simulator.silent
        elif type(run_until) is float or type(run_until) is int:
            end_time = self.time + run_until

            def stop_condition():
                return self.time >= end_time
        elif callable(run_until):

            def stop_condition():
                return run_until(self.config_dict)
        else:
            raise TypeError('run_until must be a float, int, function, or None.')

        # Stop if stop_condition is already met
        if stop_condition():
            return

        def get_next_history_time():
            # Get the next time that will be recorded to self.times and self.history
            if callable(history_interval):
                length = history_interval(self.time)
            else:
                length = history_interval
            if length <= 0:
                raise ValueError('history_interval must always be strictly positive.')
            return self.time + length

        if stopping_interval <= 0:
            raise ValueError('stopping_interval must always be strictly positive.')

        next_history_time = get_next_history_time()

        def get_next_time():
            # Get the next time simulator will run until
            t = min(next_history_time, self.time + stopping_interval)
            if end_time is not None:
                t = min(t, end_time)
            return t

        next_time = get_next_time()
        # The next step that the simulator will be run until, which corresponds to parallel time next_time
        next_step = self.time_to_steps(next_time)

        for snapshot in self.snapshots:
            snapshot.next_snapshot_time = perf_counter() + snapshot.update_time

        # add max_wall_clock to be the minimum snapshot update time, to put a time bound on calls to simulator.run
        max_wallclock_time = [min([s.update_time for s in self.snapshots])] if len(self.snapshots) > 0 else []
        while stop_condition() is False:
            if self.time >= next_time:
                next_step += self.time_to_steps(get_next_time() - next_time)
                next_time = get_next_time()
            current_step = self.simulator.t
            self.simulator.run(next_step, *max_wallclock_time)
            if self.simulator.t == next_step:
                self.time = next_time
            elif self.simulator.t < next_step:
                # simulator exited early from hitting max_wallclock_time
                # add a fraction of the time until next_time equal to the fractional progress made by simulator
                self.time += (next_time - self.time) * (self.simulator.t - current_step) / (next_step - current_step)
            else:
                raise RuntimeError('The simulator ran to step {self.simulator.t} past the next step {next_step}.')
            if self.time >= next_history_time:
                assert self.time == next_history_time, \
                    f'self.time = {self.time} overshot next_history_time = {next_history_time}'
                self.add_config()
                next_history_time = get_next_history_time()
            for snapshot in self.snapshots:
                if perf_counter() >= snapshot.next_snapshot_time:
                    snapshot.update()
                    snapshot.next_snapshot_time = perf_counter() + snapshot.update_time
        # add the final configuration if it wasn't already recorded
        if self.time > self.times[-1]:
            self.add_config()
        # final updates for all snapshots
        for snapshot in self.snapshots:
            snapshot.update()

        if len(self.snapshots) == 1 and type(self.snapshots[0]) is TimeUpdate:
            self.snapshots.pop()
            print()

    @property
    def reactions(self) -> str:
        """
        A string showing all non-null transitions in reaction notation.

        Each reaction is separated by newlines, so that ``print(self.reactions)`` will display all reactions.
        Only works with simulator method multibatch, otherwise will raise a ValueError.
        """
        if type(self.simulator) != simulator.SimulatorMultiBatch:
            raise ValueError('reactions must be defined by multibatch simulator.')
        w = max([len(str(state)) for state in self.state_list])
        reactions = [self._reaction_string(r, p, w) for (r, p) in
                     zip(self.simulator.reactions, self.simulator.reaction_probabilities)]
        return '\n'.join(reactions)

    @property
    def enabled_reactions(self) -> str:
        """
        A string showing all non-null transitions that are currently enabled.

        This can only check the current configuration in self.simulator.
        Each reaction is separated by newlines, so that ``print(self.enabled_reactions)``
        will display all enabled reactions.
        """
        if type(self.simulator) != simulator.SimulatorMultiBatch:
            raise ValueError('reactions must be defined by multibatch simulator.')
        w = max([len(str(state)) for state in self.state_list])
        self.simulator.get_enabled_reactions()

        reactions = []
        for i in range(self.simulator.num_enabled_reactions):
            r = self.simulator.reactions[self.simulator.enabled_reactions[i]]
            p = self.simulator.reaction_probabilities[self.simulator.enabled_reactions[i]]
            reactions.append(self._reaction_string(r, p, w))
        return '\n'.join(reactions)

    def _reaction_string(self, reaction, p: float = 1, w: int = 1) -> str:
        """A string representation of a reaction."""

        reactants = [self.state_list[i] for i in sorted(reaction[0:2])]
        products = [self.state_list[i] for i in sorted(reaction[2:])]
        s = '{0}, {1}  -->  {2}, {3}'.format(*[str(x).rjust(w) for x in reactants + products])
        if p < 1:
            s += f'      with probability {p}'
        return s

    # TODO: If this changes n, then the timescale must change
    def reset(self, init_config: Optional[Dict[State, int]] = None) -> None:
        """Reset the Simulation.

        Args:
            init_config: The configuration to reset to. Defaults to None.
                If None, will use the old initial configuration.
        """
        if init_config is None:
            config = self.configs[0]
        else:
            config = np.zeros(len(self.state_list), dtype=np.int64)
            for k in init_config.keys():
                config[self.state_dict[k]] += init_config[k]
        self.configs = [config]
        self.times = [0]
        self._history = pd.DataFrame(data=self.configs, index=pd.Index(self.times, name='time'),
                                     columns=self._history.columns)
        self.simulator.reset(config)

    # TODO: If this changes n, then the timescale must change
    def set_config(self, config: Union[Dict[State, int], np.ndarray]) -> None:
        """Change the current configuration.

        Args:
            config: The configuration to change to. This can be a dictionary,
                mapping states to counts, or an array giving counts in the order
                of state_list.
        """
        if type(config) is dict:
            config_array = self.array_from_dict(config)
        else:
            config_array = np.array(config, dtype=np.int64)
        self.simulator.reset(config_array, self.simulator.t)
        self.add_config()

    def time_to_steps(self, time: float) -> int:
        """Convert length of time into number of steps.

        Args:
            time: The amount of time to convert.
        """
        expected_steps = time * self.steps_per_time_unit
        if self.continuous_time:
            # In continuous time the number of steps is a poisson random variable
            # TODO: handle the case when expected_steps is larger than an int32 and numpy reports a ValueError
            return self.rng.poisson(expected_steps)
        else:
            # In discrete time we round up to the next step
            return math.ceil(expected_steps)

    @property
    def config_dict(self) -> Dict[State, int]:
        """The current configuration, as a dictionary mapping states to counts."""
        return {self.state_list[i]: self.simulator.config[i] for i in np.nonzero(self.simulator.config)[0]}

    @property
    def config_array(self) -> np.ndarray:
        """The current configuration in the simulator, as an array of counts.

        The array is given in the same order as self.state_list. The index of state s
        is self.state_dict[s].
        """
        return np.asarray(self.simulator.config)

    @property
    def history(self) -> pd.DataFrame:
        """A pandas dataframe containing the history of all recorded configurations."""
        h = len(self._history)
        if h < len(self.configs):
            new_history = pd.DataFrame(data=self.configs[h:], index=pd.Index(self.times_in_units(self.times[h:])),
                                       columns=self._history.columns)
            self._history = pd.concat([self._history, new_history])
            if self.time_units is None:
                if self.continuous_time:
                    self._history.index.name = 'time (continuous units)'
                else:
                    self._history.index.name = f'time ({self.steps_per_time_unit} interaction steps)'
        return self._history

    def times_in_units(self, times):
        """If self.time_units is defined, convert time list to appropriate units."""
        if self.time_units:
            return pd.to_timedelta(times, unit=self.time_units)
        else:
            return times

    def add_config(self) -> None:
        """Appends the current simulator configuration and time."""
        self.configs.append(np.array(self.simulator.config))
        self.times.append(self.time)

    def set_snapshot_time(self, time: float) -> None:
        """Updates all snapshots to the nearest recorded configuration to a specified time.

        Args:
            time (float): The parallel time to update the snapshots to.
        """
        index = np.searchsorted(self.times, time)
        for snapshot in self.snapshots:
            snapshot.update(index=index)

    def set_snapshot_index(self, index: int) -> None:
        """Updates all snapshots to the configuration self.configs[index].

        Args:
            index (int): The index of the configuration.
        """
        for snapshot in self.snapshots:
            snapshot.update(index=index)

    def add_snapshot(self, snap: "Snapshot") -> None:
        """Add a new Snapshot to self.snapshots.

        Args:
            snap (Snapshot): The Snapshot object to be added.
        """
        snap.simulation = self
        snap.initialize()
        snap.update()
        self.snapshots.append(snap)

    def snapshot_slider(self, var: str = 'index') -> widgets.interactive:
        """Returns a slider that updates all Snapshot objects.

        Returns a slider from the ipywidgets library.

        Args:
            var: What variable the slider uses, either 'index' or 'time'.
        """
        if var.lower() == 'index':
            return widgets.interactive(self.set_snapshot_index,
                                       index=widgets.IntSlider(min=0,
                                                               max=len(self.times) - 1,
                                                               layout=widgets.Layout(width='100%'),
                                                               step=1))
        elif var.lower() == 'time':
            return widgets.interactive(self.set_snapshot_time,
                                       time=widgets.FloatSlider(min=self.times[0],
                                                                max=self.times[-1],
                                                                layout=widgets.Layout(width='100%'),
                                                                step=0.01))
        else:
            raise ValueError("var must be either 'index' or 'time'.")

    def sample_silence_time(self) -> float:
        """Starts a new trial from the initial distribution and return time until silence."""
        if type(self.simulator) != simulator.SimulatorMultiBatch:
            raise ValueError('silence time can only be found by multibatch simulator.')
        self.simulator.run_until_silent(np.array(self.configs[0]))
        return self.time

    def sample_future_configuration(self, time: float, num_samples: int = 100) -> pd.DataFrame:
        """Repeatedly samples the configuration at a fixed future time.

        Args:
            time: The amount of time ahead to sample the configuration.
            num_samples: The number of samples to get.

        Returns:
            A dataframe whose rows are the sampled configuration.
        """
        samples = []
        t = self.simulator.t
        for _ in tqdm(range(num_samples)):
            self.simulator.reset(np.array(self.configs[-1]), t)
            end_step = t + self.time_to_steps(time)
            self.simulator.run(end_step)
            samples.append(np.array(self.simulator.config))
        return pd.DataFrame(data=samples, index=pd.Index(range(num_samples), name='trial #'),
                            columns=self._history.columns)

    def __getstate__(self):
        """Returns information to be pickled."""
        # Clear _history such that it can be regenerated by self.history
        d = dict(self.__dict__)
        d['_history'] = pd.DataFrame(data=self.configs[0:1], index=pd.Index(self.times_in_units(self.times[0:1])),
                                     columns=self._history.columns)
        del d['simulator']
        return d

    def __setstate__(self, state) -> None:
        """Instantiates from the pickled state information."""
        self.__dict__ = state
        self.initialize_simulator(self.configs[-1])

class Plotter(Snapshot):
    """Base class for a Snapshot which will make a plot.

    Gives the option to map states to categories, for an easy way to visualize
    relevant subsets of the states rather than the whole state set.
    These require an interactive matplotlib backend to work.

    Attributes:
        fig: The matplotlib figure that is created which holds the barplot.
        ax: The matplotlib axis object that holds the barplot. Modifying properties
            of this object is the most direct way to modify the plot.
        state_map: A function mapping states to categories, which acts as a filter
            to view a subset of the states or just one field of the states.
        categories: A list which holds the set {state_map(state)} for all states
            in state_list. This gives the set of labels for the bars in the barplot.
        _matrix: A (# states)x(# categories) matrix such that for the configuration
            array self.config (indexed by states), matrix * config gives an array
            of counts of categories. Used internally for the update function.
    """
    def __init__(self, state_map=None, update_time=0.5) -> None:
        """Initializes the StatePlotter.

        Args:
            state_map: An optional function mapping states to categories.
        """
        self._matrix = None
        self.state_map = state_map
        self.update_time = update_time

    def _add_state_map(self, state_map):
        """An internal function called to update self.categories and self.matrix."""
        self.categories = []
        # categories will be ordered in the same order as state_list
        for state in self.simulation.state_list:
            if state_map(state) is not None and state_map(state) not in self.categories:
                self.categories.append(state_map(state))

        categories_dict = {j: i for i, j in enumerate(self.categories)}
        self._matrix = np.zeros((len(self.simulation.state_list), len(self.categories)), dtype=np.int64)
        for i, state in enumerate(self.simulation.state_list):
            m = state_map(state)
            if m is not None:
                self._matrix[i, categories_dict[m]] += 1

    def initialize(self) -> None:
        """Initializes the plotter by creating a fig and ax."""
        self.fig, self.ax = plt.subplots()
        if self.state_map is not None:
            self._add_state_map(self.state_map)
        else:
            self.categories = self.simulation.state_list


class StatePlotter(Plotter):
    """Plotter which produces a barplot of counts."""

    def initialize(self) -> None:
        """Initializes the barplot.

        If self.state_map gets changed, call initialize to update the barplot to
            show the new set self.categories.
        """
        super().initialize()
        self.ax = sns.barplot(x=[str(c) for c in self.categories], y=np.zeros(len(self.categories)))
        # rotate the x-axis labels if any of the label strings have more than 2 characters
        if max([len(str(c)) for c in self.categories]) > 2:
            for tick in self.ax.get_xticklabels():
                tick.set_rotation(45)
        self.ax.set_ylim(0, self.simulation.simulator.n)

    def update(self, index: Optional[int] = None) -> None:
        """Update the heights of all bars in the plot."""
        super().update(index)
        if self._matrix is not None:
            heights = np.matmul(self.config, self._matrix)
        else:
            heights = self.config
        for i, rect in enumerate(self.ax.patches):
            rect.set_height(heights[i])

        self.ax.set_title(f'Time {self.time}')
        self.fig.tight_layout()
        self.fig.canvas.draw()


class HistoryPlotter(Plotter):

    def update(self, index: Optional[int] = None) -> None:
        super().update(index)
        self.ax.clear()
        if self._matrix is not None:
            df = pd.DataFrame(data=np.matmul(self.simulation.history.to_numpy(), self._matrix), columns=self.categories,
                              index=self.simulation.history.index)
        else:
            df = self.simulation.history
        df.plot(ax=self.ax)
        # rotate the x labels if they are time units
        if self.simulation.time_units:
            for tick in self.ax.get_xticklabels():
                tick.set_rotation(45)
        self.fig.canvas.draw()


def time_trials(rule: Rule, ns: List[int], initial_conditions: Union[Callable, List],
                convergence_condition: Optional[Callable] = None, convergence_check_interval: float = 0.1,
                num_trials: int = 100, max_wallclock_time: float = 60 * 60 * 24,

                **kwargs) -> pd.DataFrame:
    """Gathers data about the convergence time of a rule.

    Args:
        rule: The rule that is used to generate the Simulation.
        ns: A list of population sizes n to sample from.
            This should be in increasing order to make the time_bound work.
        initial_conditions: An initial condition is a dict mapping states to counts.
            This can either be a list of initial conditions, or a function mapping
            population size n to an initial condition of n agents.
        convergence_condition: A boolean function that takes a configuration dict
            as input and returns True if that configuration has converged.
            Defaults to None. If None, the simulation will run until silent
            (all transitions are null), and the data will be for silence time.
        convergence_check_interval: How often (in parallel time) the simulation will
            run between convergence checks. Defaults to 0.1.
            Smaller values give better resolution, but spend more time checking for
            convergence.
        num_trials: The maximum number of trials that will be done for each
            population size n, if there is sufficient time. Defaults to 100.
            If you want to ensure that you get the full num_trials samples for
            each value n, use a large value for time_bound.
        max_wallclock_time: A bound (in seconds) for how long this function will run.
            Each value n is given a time budget based on the time remaining, and
            will stop before doing num_trials runs when this time budget runs out.
            Defaults to 60 * 60 * 24 (one day).
        **kwargs: Other keyword arguments to pass into Simulation.

    Returns:
        df: A pandas dataframe giving the data from each trial, with two columns
            'n' and 'time'. A good way to visualize this dataframe is using the
            seaborn library, calling sns.lineplot(x='n', y='time', data=df).
    """
    d = {'n': [], 'time': []}
    end_time = perf_counter() + max_wallclock_time
    if callable(initial_conditions):
        initial_conditions = [initial_conditions(n) for n in ns]
    for i in tqdm(range(len(ns))):
        sim = Simulation(initial_conditions[i], rule, **kwargs)
        t = perf_counter()
        time_limit = t + (end_time - t) / (len(ns) - i)
        j = 0
        while j < num_trials and perf_counter() < time_limit:
            j += 1
            sim.reset(initial_conditions[i])
            sim.run(convergence_condition, stopping_interval=convergence_check_interval, timer=False)
            d['n'].append(ns[i])
            d['time'].append(sim.time)

    return pd.DataFrame(data=d)
