# circuit.py
#
# This file is part of scqubits.
#
#    Copyright (c) 2019 and later, Jens Koch and Peter Groszkowski
#    All rights reserved.
#
#    This source code is licensed under the BSD-style license found in the
#    LICENSE file in the root directory of this source tree.
############################################################################


import re

from symtable import Symbol
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Tuple,
    Union,
)

import numpy as np
import qutip as qt
import scipy as sp
import sympy as sm

from matplotlib.text import OffsetFrom
from matplotlib import pyplot as plt
from numpy import ndarray
from scipy import sparse
from scipy.sparse.csc import csc_matrix

import scqubits.core.discretization as discretization
import scqubits.core.oscillator as osc
import scqubits.core.qubit_base as base
import scqubits.io_utils.fileio_serializers as serializers

from scqubits import HilbertSpace, settings
from scqubits.core import operators as op
from scqubits.core.storage import DataStore
from scqubits.core.symboliccircuit import Branch, SymbolicCircuit
from scqubits.utils.misc import flatten_list, flatten_list_recursive, list_intersection
from scqubits.utils.spectrum_utils import (
    convert_matrix_to_qobj,
    get_matrixelement_table,
    identity_wrap,
    order_eigensystem,
)


def truncation_template(
    system_hierarchy: list, individual_trunc_dim: int = 6, combined_trunc_dim: int = 50
) -> list:
    """
    Function to generate a template for defining the truncated dimensions for subsystems
    when hierarchical diagonalization is used.

    Parameter
    ---------
    system_hierarchy:
        list showing the system hierarchy
    individual_trunc_dim:
        The default used to set truncation dimension for the subsystem which does not
        use hierarchical diagonalization.
    combined_trunc_dim:
    """
    """
    Function to generate a template for defining the truncated dimensions for subsystems
    when hierarchical diagonalization is used.

    Parameters
    ----------
    system_hierarchy:
        list which sets the system hierarchy
    individual_trunc_dim:
        The default used to set truncation dimension for subsystems which do not
        use hierarchical diagonalization, by default 6
    combined_trunc_dim:
        The default used to set the truncated dim for subsystems which use hierarchical diagonalization, by default 50

    Returns
    -------
        The template for setting the truncated dims for the Circuit instance when
        hierarchical diagonalization is used.
    """
    trunc_dims: List[Union[int, list]] = []
    for subsystem_hierarchy in system_hierarchy:
        if subsystem_hierarchy == flatten_list_recursive(subsystem_hierarchy):
            trunc_dims.append(individual_trunc_dim)
        else:
            trunc_dims.append([combined_trunc_dim, truncation_template(
                subsystem_hierarchy)])
    return trunc_dims


def get_trailing_number(input_str: str) -> Union[int, None]:
    """
    Returns the number trailing a string given as input. Example:
        $ get_trailing_number("a23")
        $ 23

    Parameters
    ----------
    input_str :
        String which trails any number

    Returns
    -------
        returns the trailing integer as int, else returns None
    """
    match = re.search(r"\d+$", input_str)
    return int(match.group()) if match else None


class Subsystem(base.QubitBaseClass, serializers.Serializable):
    """
    Defines a subsystem for a circuit, which can further be used recursively to define
    subsystems within subsystem.

    Parameters
    ----------
    parent: SubSystem
        the instance under which the new subsystem is defined.
    hamiltonian_symbolic: sm.Expr
        The symbolic expression which defines the Hamiltonian for the new subsystem
    system_hierarchy: Optional[List], optional
        Defines the hierarchy of the new subsystem, is set to None when hierarchical diagonalization is not required. by default None
    subsystem_trunc_dims: Optional[List], optional
        Defines the truncated dimensions for the subsystems inside the current
        subsystem, is set to None when hierarchical diagonalization is not required.  , by default None
    truncated_dim: Optional[int], optional
        sets the truncated dimension for the current subsystem, by default 10

    """

    def __init__(
        self,
        parent: "Subsystem",
        hamiltonian_symbolic: sm.Expr,
        system_hierarchy: Optional[List] = None,
        subsystem_trunc_dims: Optional[List] = None,
        truncated_dim: Optional[int] = 10,
    ):
        base.QuantumSystem.__init__(self, id_str=None)

        self.system_hierarchy: list = system_hierarchy
        self.truncated_dim: int = truncated_dim
        self.subsystem_trunc_dims: list = subsystem_trunc_dims

        self.is_child: bool = True
        self.parent: "Subsystem" = parent
        self.hamiltonian_symbolic: sm.Expr = hamiltonian_symbolic
        self._hamiltonian_sym_for_numerics: sm.core.expr.Expr = hamiltonian_symbolic

        self.ext_basis: str = self.parent.ext_basis
        self.external_fluxes: List[Symbol] = [
            var
            for var in self.parent.external_fluxes
            if var in self.hamiltonian_symbolic.free_symbols
        ]
        self.offset_charges: List[Symbol] = [
            var
            for var in self.parent.offset_charges
            if var in self.hamiltonian_symbolic.free_symbols
        ]
        self.param_vars: List[Symbol] = [
            var
            for var in self.parent.param_vars
            if var in self.hamiltonian_symbolic.free_symbols
        ]

        self.var_categories_list: List[int] = []
        cutoffs = []
        for var in list(self.hamiltonian_symbolic.free_symbols):
            if "I" not in str(var):
                filtered_var = re.findall(
                    r"\d+", re.sub(r"ng_\d+|Φ\d+", "", str(var))
                )  # filtering offset charges and external flux
                if not filtered_var:
                    continue
                else:
                    var_index = int(filtered_var[0])
                if var_index not in self.var_categories_list:
                    for cutoff_name in self.parent.cutoff_names:
                        if str(var_index) in cutoff_name:
                            cutoffs.append(getattr(self.parent, cutoff_name))
                    self.var_categories_list.append(var_index)
        # setting some class attributes
        self.var_categories_list.sort()
        self.var_categories: Dict[str, List[Symbol]] = {}
        for var_type in self.parent.var_categories:
            self.var_categories[var_type] = [
                var_index
                for var_index in self.parent.var_categories[var_type]
                if var_index in self.var_categories_list
            ]

        self.cutoff_names: List[str] = []
        for var_type in self.var_categories.keys():
            if var_type == "periodic":
                for x, var_index in enumerate(self.var_categories["periodic"]):
                    self.cutoff_names.append("cutoff_n_" + str(var_index))
            if var_type == "extended":
                for x, var_index in enumerate(self.var_categories["extended"]):
                    self.cutoff_names.append("cutoff_ext_" + str(var_index))

        self.cutoffs_dict: Dict[int, int] = {}
        for var_index in self.var_categories_list:
            for cutoff_name in self.parent.cutoff_names:
                if str(var_index) in cutoff_name:
                    self.cutoffs_dict[var_index] = getattr(self.parent, cutoff_name)
        self.cutoffs_dict = self.cutoffs_dict
        self.discretized_phi_range: Dict[int, Tuple[float]] = {
            i: self.parent.discretized_phi_range[i]
            for i in self.parent.discretized_phi_range
            if i in self.var_categories_list
        }

        self.hierarchical_diagonalization: bool = (
            system_hierarchy != []
            and system_hierarchy != flatten_list_recursive(system_hierarchy)
        )

        if len(self.var_categories_list) == 1 and self.ext_basis == "harmonic":
            self.type_of_matrices: str = "dense"
        else:
            self.type_of_matrices: str = "sparse"

        self._sys_type: str = type(self).__name__
        self._id_str: str = self._autogenerate_id_str()

        self._initiate_subsystem()

    def __repr__(self) -> str:
        return self._id_str

    def _regenerate_sym_hamiltonian(self) -> None:
        """
        Regenerates the system Hamiltonian from the symbolic circuit when needed (for
        example when the circuit is large and circuit parameters are changed).
        """
        if not self.is_child and len(self.symbolic_circuit.nodes) > 3:
            self.hamiltonian_symbolic = (
                self.symbolic_circuit.generate_symbolic_hamiltonian(
                    substitute_params=True
                )
            )
            self.generate_hamiltonian_sym_for_numerics()

    def _set_property_and_update_param_vars(
        self, param_name: str, value: float
    ) -> None:
        """
        Setter method to set parameter variables which are instance properties.

        Parameters
        ----------
        param_name:
            Name of the symbol which is updated
        value:
            The value to which the instance property is updated.
        """

        # update the attribute for the current instance
        setattr(self, "_" + param_name, value)
        # update the attribute for the instance in symboliccircuit
        if not self.is_child and len(self.symbolic_circuit.nodes) > 3:
            self.symbolic_circuit.update_param_init_val(param_name, value)
            self._regenerate_sym_hamiltonian()

        # update Circuit instance
        # generate _hamiltonian_sym_for_numerics if not already generated, delayed for
        # large circuits
        if self.hierarchical_diagonalization:
            self.generate_subsystems()
            self.build_hilbertspace()
        else:
            self.set_operators()

    def _set_property_and_update_ext_flux_or_charge(
        self, param_name: str, value: float
    ) -> None:
        """
        Setter method to set external flux or offset charge variables which are instance properties.

        Parameters
        ----------
        param_name:
            Name of the symbol which is updated
        value:
            The value to which the instance property is updated.
        """

        # update the attribute for the current instance
        setattr(self, "_" + param_name, value)

        # update all subsystem instances
        if self.hierarchical_diagonalization:
            for subsys in self.subsystems.values():
                if hasattr(subsys, param_name):
                    setattr(subsys, param_name, value)

    def _set_property_and_update_cutoffs(self, param_name: str, value: int) -> None:
        """
        Setter method to set cutoffs which are instance properties.

        Parameters
        ----------
        param_name:
            Name of the symbol which is updated
        value:
            The value to which the instance property is updated.
        """
        setattr(self, "_" + param_name, value)

        # set operators and rebuild the HilbertSpace object
        if self.hierarchical_diagonalization:
            for subsys in self.subsystems.values():
                if hasattr(subsys, param_name):
                    setattr(subsys, param_name, value)
            self.set_operators()
            self.build_hilbertspace()
        else:
            self.set_operators()

    def _make_property(
        self, attrib_name: str, init_val: Union[int, float], property_update_type: str
    ) -> None:
        """
        Creates a class instance property with the name attrib_name which is initialized
        to init_val. The setter is set depending on the string in the property_update_type

        Parameters
        ----------
        attrib_name:
            Name of the property that needs to be created.
        init_val:
            The value to which the property is initialized.
        property_update_type:
            The string which sets the kind of setter used for this instance property.
        """
        setattr(self, "_" + attrib_name, init_val)

        def getter(self, name=attrib_name):
            return getattr(self, "_" + name)

        if property_update_type == "update_param_vars":

            def setter(self, value, name=attrib_name):
                return self._set_property_and_update_param_vars(name, value)

        elif property_update_type == "update_external_flux_or_charge":

            def setter(self, value, name=attrib_name):
                return self._set_property_and_update_ext_flux_or_charge(name, value)

        elif property_update_type == "update_cutoffs":

            def setter(self, value, name=attrib_name):
                return self._set_property_and_update_cutoffs(name, value)

        setattr(self.__class__, attrib_name, property(fget=getter, fset=setter))

    def _initiate_subsystem(self) -> None:
        """
        Function which is used to initiate the subsystem instance.
        """

        for x, param in enumerate(self.param_vars):
            # if harmonic oscillator basis is used, param vars become class properties.
            self._make_property(
                param.name, getattr(self.parent, param.name), "update_param_vars"
            )

        # getting attributes from parent
        for flux in self.external_fluxes:
            self._make_property(
                flux.name,
                getattr(self.parent, flux.name),
                "update_external_flux_or_charge",
            )
        for offset_charge in self.offset_charges:
            self._make_property(
                offset_charge.name,
                getattr(self.parent, offset_charge.name),
                "update_external_flux_or_charge",
            )

        for cutoff_str in self.cutoff_names:
            self._make_property(
                cutoff_str, getattr(self.parent, cutoff_str), "update_cutoffs"
            )

        self._init_params: List[str] = (
            [param.name for param in self.param_vars]
            + [flux.name for flux in self.external_fluxes]
            + [offset_charge.name for offset_charge in self.offset_charges]
            + self.cutoff_names
        )

        self._set_vars()
        if self.hierarchical_diagonalization:
            self.generate_subsystems()
            self.set_operators()
            self.build_hilbertspace()
        else:
            self.set_operators()

    def set_system_hierarchy(
        self, system_hierarchy: list, subsystem_trunc_dims: list
    ) -> None:
        """
        Updates the subsystem hierarchy for the Circuit instance.

        Parameters
        ----------
        system_hierarchy : list, optional
            A list of lists which is provided by the user to define subsystems
        subsystem_trunc_dims : list, optional
            dict object which can be generated for a specific system_hierarchy using the method generate_default_trunc_dims
        """
        if len(self.symbolic_circuit.nodes) > 3:
            self.hamiltonian_symbolic = (
                self.symbolic_circuit.generate_symbolic_hamiltonian(
                    substitute_params=True
                )
            )

        self.hierarchical_diagonalization = True
        self.system_hierarchy = system_hierarchy
        self.subsystem_trunc_dims = subsystem_trunc_dims
        self.generate_hamiltonian_sym_for_numerics()
        self.generate_subsystems()
        self.build_hilbertspace()

    # *****************************************************************
    # **** Functions to construct the function for the Hamiltonian ****
    # *****************************************************************

    @staticmethod
    def _exp_dia(x):
        """
        This is a special function to calculate the expm of sparse diagonal matrices
        """
        return sparse.diags(np.exp((x.todia()).diagonal())).tocsc()

    @staticmethod
    def _cos_dia(x):
        """
        This is a special function to calculate the expm of sparse diagonal matrices
        """
        return sparse.diags(np.cos(x.diagonal())).tocsc()

    @staticmethod
    def _sin_dia(x):
        """
        This is a special function to calculate the expm of sparse diagonal matrices
        """
        return sparse.diags(np.sin(x.diagonal())).tocsc()

    def generate_subsystems(self):
        """
        Generates the subsystems (child instances of Circuit) depending on the attribute self.system_hierarchy
        """
        hamiltonian = self._hamiltonian_sym_for_numerics

        systems_sym = []
        interaction_sym = []

        for subsys_index_list in self.system_hierarchy:
            subsys_index_list = flatten_list_recursive(subsys_index_list)
            expr_dict = hamiltonian.as_coefficients_dict()
            terms_list = list(expr_dict.keys())

            H_sys = 0 * sm.symbols("x")
            H_int = 0 * sm.symbols("x")
            for term in terms_list:
                term_var_categories = []
                for var in term.free_symbols:
                    # remove any branch parameters or flux and offset charge symbols
                    if (
                        "Φ" not in str(var)
                        and "ng" not in str(var)
                        and len(list_intersection(self.param_vars, [var])) == 0
                    ):
                        index = get_trailing_number(str(var))
                        if index not in term_var_categories and index is not None:
                            term_var_categories.append(index)

                if len(set(term_var_categories) - set(subsys_index_list)) == 0:
                    H_sys += expr_dict[term] * term

                if (
                    len(set(term_var_categories) - set(subsys_index_list)) > 0
                    and len(set(term_var_categories) & set(subsys_index_list)) > 0
                ):
                    H_int += expr_dict[term] * term
            systems_sym.append(H_sys)
            interaction_sym.append(H_int)
            hamiltonian -= H_sys - H_int  # removing the terms added to a subsystem

        # storing data in class attributes
        self.subsystem_hamiltonians: Dict[int, sm.Expr] = dict(
            zip(
                range(len(self.system_hierarchy)),
                [systems_sym[index] for index in range(len(self.system_hierarchy))],
            )
        )

        self.subsystem_interactions: Dict[int, sm.Expr] = dict(
            zip(
                range(len(self.system_hierarchy)),
                [interaction_sym[index] for index in range(len(self.system_hierarchy))],
            )
        )

        self.subsystems: Dict[int, "Subsystem"] = dict(
            zip(
                range(len(self.system_hierarchy)),
                [
                    Subsystem(
                        self,
                        systems_sym[index],
                        system_hierarchy=self.system_hierarchy[index],
                        truncated_dim=self.subsystem_trunc_dims[index][0]
                        if type(self.subsystem_trunc_dims[index]) == list
                        else self.subsystem_trunc_dims[index],
                        subsystem_trunc_dims=self.subsystem_trunc_dims[index][1]
                        if type(self.subsystem_trunc_dims[index]) == list
                        else None,
                    )
                    for index in range(len(self.system_hierarchy))
                ],
            )
        )

    def get_subsystem_index(self, var_index: int) -> int:
        """
        Returns the subsystem index for the subsystem to which the given var_index
        belongs.

        Parameters
        ----------
        var_index:
            variable index in integer starting from 1.

        Returns
        -------
            subsystem index which can be used to identify the subsystem index in the
            list self.subsystems.
        """
        for index, system_hierarchy in enumerate(self.system_hierarchy):
            if var_index in flatten_list_recursive(system_hierarchy):
                return index

    def build_hilbertspace(self):
        """
        Builds the HilbertSpace object for the `Circuit` instance if
        `hierarchical_diagonalization` is set to true.
        """
        hilbert_space = HilbertSpace(
            [self.subsystems[i] for i in range(len(self.system_hierarchy))]
        )

        # Adding interactions using the symbolic interaction term
        for sys_index in range(len(self.system_hierarchy)):
            interaction = self.subsystem_interactions[sys_index].expand()
            if interaction == 0:  # if the interaction term is zero
                continue
            # modifying interaction terms
            #   - substituting all the external flux, offset charge and branch
            #   parameters.
            interaction = interaction.subs(
                [
                    (param, getattr(self, str(param)))
                    for param in self.param_vars
                    + self.external_fluxes
                    + self.offset_charges
                ]
            )
            #   - substituting Identity with 1
            interaction = interaction.subs("I", 1)
            #   - substituting cos and sin operators with their own symbols
            for i in self.var_categories["extended"]:
                interaction = interaction.replace(
                    sm.cos(1.0 * sm.symbols("θ" + str(i))), sm.symbols("θc" + str(i))
                ).replace(
                    sm.sin(1.0 * sm.symbols("θ" + str(i))), sm.symbols("θs" + str(i))
                )

            expr_dict = interaction.as_coefficients_dict()
            terms_str = list(expr_dict.keys())

            for i, term in enumerate(terms_str):
                coefficient_sympy = expr_dict[term]

                # adding external flux, offset charge and branch parameters to
                # coefficient
                for var in term.free_symbols:
                    if "Φ" in str(var) or "ng" in str(var) or var in self.param_vars:
                        coefficient_sympy = coefficient_sympy * getattr(self, str(var))

                operator_symbols = [
                    var
                    for var in term.free_symbols
                    if (("Φ" not in str(var)) and ("ng" not in str(var)))
                    and (var not in self.param_vars)
                ]

                sys_op_dict = {index: [] for index in range(len(self.system_hierarchy))}
                for var in operator_symbols:
                    var_index = get_trailing_number(str(var))
                    subsystem_index = self.get_subsystem_index(var_index)
                    if "I" not in str(var):
                        operator = self.subsystems[subsystem_index].get_operator(
                            var.name
                        )
                        if isinstance(operator, qt.Qobj):
                            operator = operator.full()

                        sys_op_dict[subsystem_index].append(operator)
                    else:
                        sys_op_dict[0].append(self.subsystems[0]._identity())

                operator_dict = {}

                for index in range(len(self.system_hierarchy)):
                    for op_index, operator in enumerate(sys_op_dict[index]):
                        operator_dict["op" + str(len(operator_dict) + 1)] = (
                            operator,
                            self.subsystems[index],
                        )
                hilbert_space.add_interaction(
                    g=float(coefficient_sympy), **operator_dict, check_validity=False
                )

        self.hilbert_space = hilbert_space

    def _set_vars(self):
        """
        Sets the attribute vars which is a dictionary containing all the Sympy Symbol objects for all the operators present in the circuit
        """
        # Defining the list of variables for periodic operators
        periodic_symbols_sin = [
            sm.symbols("θs" + str(i)) for i in self.var_categories["periodic"]
        ]
        periodic_symbols_cos = [
            sm.symbols("θc" + str(i)) for i in self.var_categories["periodic"]
        ]
        periodic_symbols_n = [
            sm.symbols("n" + str(i)) for i in self.var_categories["periodic"]
        ]

        # Defining the list of discretized_ext variables
        y_symbols = [sm.symbols("θ" + str(i)) for i in self.var_categories["extended"]]
        p_symbols = [sm.symbols("Q" + str(i)) for i in self.var_categories["extended"]]

        if self.ext_basis == "discretized":

            ps_symbols = [
                sm.symbols("Qs" + str(i)) for i in self.var_categories["extended"]
            ]
            sin_symbols = [
                sm.symbols("θs" + str(i)) for i in self.var_categories["extended"]
            ]
            cos_symbols = [
                sm.symbols("θc" + str(i)) for i in self.var_categories["extended"]
            ]

        elif self.ext_basis == "harmonic":

            a_symbols = [
                sm.symbols("a" + str(i)) for i in self.var_categories["extended"]
            ]
            ad_symbols = [
                sm.symbols("ad" + str(i)) for i in self.var_categories["extended"]
            ]
            Nh_symbols = [
                sm.symbols("Nh" + str(i)) for i in self.var_categories["extended"]
            ]
            pos_symbols = [
                sm.symbols("θ" + str(i)) for i in self.var_categories["extended"]
            ]
            sin_symbols = [
                sm.symbols("θs" + str(i)) for i in self.var_categories["extended"]
            ]
            cos_symbols = [
                sm.symbols("θc" + str(i)) for i in self.var_categories["extended"]
            ]
            momentum_symbols = [
                sm.symbols("Q" + str(i)) for i in self.var_categories["extended"]
            ]

        # setting the attribute self.vars
        self.vars: Dict[str, Any] = {
            "periodic": {
                "sin": periodic_symbols_sin,
                "cos": periodic_symbols_cos,
                "number": periodic_symbols_n,
            },
            "identity": [sm.symbols("I")],
        }

        if self.ext_basis == "discretized":
            self.vars["extended"] = {
                "position": y_symbols,
                "momentum": p_symbols,
                "momentum_squared": ps_symbols,
                "sin": sin_symbols,
                "cos": cos_symbols,
            }
        elif self.ext_basis == "harmonic":
            self.vars["extended"] = {
                "annihilation": a_symbols,
                "creation": ad_symbols,
                "number": Nh_symbols,
                "position": pos_symbols,
                "momentum": momentum_symbols,
                "sin": sin_symbols,
                "cos": cos_symbols,
            }

    def generate_hamiltonian_sym_for_numerics(self):
        """
        Generates a symbolic expression which is ready for numerical evaluation starting
        from the expression stored in the attribute hamiltonian_symbolic. Stores the
        result in the attribute _hamiltonian_sym_for_numerics.
        """
        H = (
            self.hamiltonian_symbolic.expand()
        )  # applying expand is critical; otherwise the replacement of p^2 with ps2
        # would not succeed

        # shifting the harmonic oscillator potential to the point of external fluxes
        flux_shift_vars = {}
        for var_index in self.var_categories["extended"]:
            if H.coeff("θ" + str(var_index)) != 0:
                flux_shift_vars[var_index] = sm.symbols("Δθ" + str(var_index))
                H = H.replace(
                    sm.symbols("θ" + str(var_index)),
                    sm.symbols("θ" + str(var_index)) + flux_shift_vars[var_index],
                )  # substituting the flux offset variable offsets to collect the
                # coefficients later
        H = H.expand()

        flux_shift_equations = [
            H.coeff("θ" + str(var_index)).subs(
                [("θ" + str(i), 0) for i in self.var_categories["extended"]]
            )
            for var_index in flux_shift_vars.keys()
        ]  # finding the coefficients of the linear terms

        flux_shifts = sm.linsolve(
            flux_shift_equations, tuple(flux_shift_vars.values())
        )  # solving for the flux offsets

        if len(flux_shifts) != 0:
            flux_shifts = list(list(flux_shifts)[0])
        else:
            flux_shifts = []

        flux_shifts_dict = dict(zip(list(flux_shift_vars.keys()), list(flux_shifts)))

        H = H.subs(
            [
                (sm.symbols("Δθ" + str(var_index)), flux_shifts_dict[var_index])
                for var_index in flux_shifts_dict.keys()
            ]
        )  # substituting the flux offsets to remove the linear terms
        H = H.subs(
            [(var, 0) for var in flux_shift_vars.values()]
        )  # removing the constants from the Hamiltonian

        flux_shifts_dict.update(
            {
                var_index: 0
                for var_index in self.var_categories["extended"]
                if var_index not in flux_shifts_dict
            }
        )
        # remove constants from Hamiltonian
        H = H - H.as_coefficients_dict()[1]
        H = H.expand()
        #############################

        # marking the sin and cos terms of the periodic variables with different symbols
        if len(self.var_categories["periodic"]) > 0:
            H = sm.expand_trig(H).expand()

        for i in self.var_categories["periodic"]:
            H = H.replace(
                sm.cos(1.0 * sm.symbols("θ" + str(i))), sm.symbols("θc" + str(i))
            ).replace(sm.sin(1.0 * sm.symbols("θ" + str(i))), sm.symbols("θs" + str(i)))

        if self.ext_basis == "discretized":

            # marking the squared momentum operators with a separate symbol
            for i in self.var_categories["extended"]:
                H = H.replace(sm.symbols("Q" + str(i)) ** 2, sm.symbols("Qs" + str(i)))

        elif self.ext_basis == "harmonic":
            H = sm.expand_trig(H).expand()

            for i in self.var_categories["extended"]:
                H = H.replace(
                    sm.cos(1.0 * sm.symbols("θ" + str(i))),
                    sm.symbols("θc" + str(i)),
                ).replace(
                    sm.sin(1.0 * sm.symbols("θ" + str(i))),
                    sm.symbols("θs" + str(i)),
                )

        # removing the constants from the Hamiltonian
        coeff_dict = H.as_coefficients_dict()
        constants = [
            i
            for i in coeff_dict
            if "Q" not in str(i)
            and "θ" not in str(i)
            and "n" not in str(i)
            and "a" not in str(i)
            and "Nh" not in str(i)
        ]
        for i in constants:
            H = H - i * coeff_dict[i]

        # associate a identity matrix with the external flux vars
        for ext_flux in self.external_fluxes:
            H = H.subs(ext_flux, ext_flux * sm.symbols("I") * 2 * np.pi)

        # associate a identity matrix with offset charge vars
        for offset_charge in self.offset_charges:
            H = H.subs(offset_charge, offset_charge * sm.symbols("I"))
        setattr(self, "_hamiltonian_sym_for_numerics", H)

    ##################################################################
    ############### Functions to construct the operators #############
    ##################################################################
    def hilbertdim(self):
        """
        Returns the Hilbert dimension of the Circuit instance
        """
        if not self.hierarchical_diagonalization:
            cutoff_names = []
            for cutoffs in self.get_cutoffs().keys():
                if "cutoff_n" in cutoffs:
                    cutoff_names.append(
                        [2 * k + 1 for k in self.get_cutoffs()[cutoffs]]
                    )
                elif "cutoff_ext" in cutoffs:
                    cutoff_names.append([k for k in self.get_cutoffs()[cutoffs]])

            cutoff_names = [
                j for i in list(cutoff_names) for j in i
            ]  # concatenating the sublists
            return np.prod(cutoff_names)
        else:
            return np.prod(
                [
                    self.subsystem_trunc_dims[i][0]
                    if type(self.subsystem_trunc_dims[i]) == list
                    else self.subsystem_trunc_dims[i]
                    for i in range(len(self.system_hierarchy))
                ]
            )

    # helper functions
    def _kron_operator(
        self, operator: Union[csc_matrix, ndarray], index: int
    ) -> Union[csc_matrix, ndarray]:
        """
        Identity wraps the operator with identities generated for all the other variable
        indices present in the current SubSystem.

        Parameters
        ----------
        operator:
            The operator belonging to the variable index set in the argument index.
        index:
            Variable index to which the operator belongs

        Returns
        -------
            Returns the operator which is identity wrapped for the current subsystem.
        """
        # if self.hierarchical_diagonalization:
        #     subsystem_index = self.get_subsystem_index(index)
        #     var_index_list = flatten_list_recursive(
        #         self.system_hierarchy[subsystem_index]
        #     )
        # else:
        var_index_list = (
            self.var_categories["periodic"] + self.var_categories["extended"]
        )

        var_index_list.sort()  # important to make sure that right cutoffs are chosen
        cutoff_dict = self.get_cutoffs()

        cutoff_names = []
        for cutoff_type in cutoff_dict.keys():
            if "cutoff_n" in cutoff_type:
                cutoff_names.append([2 * k + 1 for k in cutoff_dict[cutoff_type]])
            elif "cutoff_ext" in cutoff_type:
                cutoff_names.append([k for k in cutoff_dict[cutoff_type]])

        cutoffs = [
            j for i in list(cutoff_names) for j in i
        ]  # concatenating the sublists
        cutoffs_index_dict = dict(
            zip(
                self.var_categories["periodic"] + self.var_categories["extended"],
                cutoffs,
            )
        )
        cutoff_names = [
            cutoffs_index_dict[i] for i in var_index_list
        ]  # selecting the cutoffs present in

        if self.type_of_matrices == "dense":
            matrix_format = "array"
        elif self.type_of_matrices == "sparse":
            matrix_format = "csc"

        if len(var_index_list) > 1:
            if index > var_index_list[0]:
                Identity_l = sparse.identity(
                    np.prod(cutoff_names[: var_index_list.index(index)]),
                    format=matrix_format,
                )
            if index < var_index_list[-1]:
                Identity_r = sparse.identity(
                    np.prod(cutoff_names[var_index_list.index(index) + 1 :]),
                    format=matrix_format,
                )

            if index == var_index_list[0]:
                return sparse.kron(operator, Identity_r, format=matrix_format)
            elif index == var_index_list[-1]:
                return sparse.kron(Identity_l, operator, format=matrix_format)
            else:
                return sparse.kron(
                    sparse.kron(Identity_l, operator, format=matrix_format),
                    Identity_r,
                    format=matrix_format,
                )
        else:
            return self._change_sparsity(operator)

    def _change_sparsity(
        self, x: Union[csc_matrix, ndarray]
    ) -> Union[csc_matrix, ndarray]:
        """
        Changes the sparsity of sparce csc matrix depending on the attribute
        type_of_matrices

        Parameters
        ----------
        x : Union[csc_matrix, ndarray]

        Returns
        -------
        Union[csc_matrix, ndarray]
            returns the same or the dense matrix version if type_of_matrices is set to
            "dense"
        """
        if sparse.issparse(x):
            if self.type_of_matrices == "sparse":
                return x
            elif self.type_of_matrices == "dense":
                return x.toarray()
        else:
            if self.type_of_matrices == "sparse":
                return sparse.csc_matrix(x)
            elif self.type_of_matrices == "dense":
                return x

    # Identity Operator
    def _identity(self):
        """
        Returns the Identity operator for the entire Hilber space of the circuit.
        """
        if (
            hasattr(self, "hierarchical_diagonalization")
            and self.hierarchical_diagonalization
        ):
            return None
        dim = self.hilbertdim()
        if self.type_of_matrices == "sparse":
            op = sparse.identity(dim)
            return op.tocsc()
        elif self.type_of_matrices == "dense":
            return np.identity(dim)

    # ext basis
    def _identity_phi(self, grid: discretization.Grid1d) -> csc_matrix:
        """
        Returns identity operator in the discretized_phi basis.

        Parameters
        ----------
        grid:
            Grid used to generate the identity operator

        Returns
        -------
            identity operator in the discretized phi basis
        """
        pt_count = grid.pt_count
        return sparse.identity(pt_count, format="csc")

    def _phi_operator(self, grid: discretization.Grid1d) -> csc_matrix:
        """
        Returns phi operator in the discretized_phi basis.

        Parameters
        ----------
        grid:
            Grid used to generate the phi operator

        Returns
        -------
            phi operator in the discretized phi basis
        """
        pt_count = grid.pt_count

        phi_matrix = sparse.dia_matrix((pt_count, pt_count))
        diag_elements = grid.make_linspace()
        phi_matrix.setdiag(diag_elements)
        return phi_matrix

    def _i_d_dphi_operator(self, grid: discretization.Grid1d) -> csc_matrix:
        """
        Returns i*d/dphi operator in the discretized_phi basis.

        Parameters
        ----------
        grid:
            Grid used to generate the identity operator

        Returns
        -------
            i*d/dphi operator in the discretized phi basis
        """
        return grid.first_derivative_matrix(prefactor=-1j)

    def _i_d2_dphi2_operator(self, grid: discretization.Grid1d) -> csc_matrix:
        """
        Returns i*d2/dphi2 operator in the discretized_phi basis.

        Parameters
        ----------
        grid:
            Grid used to generate the identity operator

        Returns
        -------
            i*d2/dphi2 operator in the discretized phi basis
        """
        return grid.second_derivative_matrix(prefactor=-1.0)

    def _cos_phi(self, grid: discretization.Grid1d) -> csc_matrix:
        """
        Returns cos operator in the discretized_phi basis.

        Parameters
        ----------
        grid:
            Grid used to generate the identity operator

        Returns
        -------
            cos operator in the discretized phi basis
        """
        pt_count = grid.pt_count

        cos_op = sparse.dia_matrix((pt_count, pt_count))
        diag_elements = np.cos(grid.make_linspace())
        cos_op.setdiag(diag_elements)
        return cos_op.tocsc()

    def _sin_phi(self, grid: discretization.Grid1d) -> csc_matrix:
        """
        Returns sin operator in the discretized_phi basis.

        Parameters
        ----------
        grid:
            Grid used to generate the identity operator

        Returns
        -------
            sin operator in the discretized phi basis
        """
        pt_count = grid.pt_count

        sin_op = sparse.dia_matrix((pt_count, pt_count))
        diag_elements = np.cos(grid.make_linspace())
        sin_op.setdiag(diag_elements)
        return sin_op.tocsc()

    # charge basis

    def _identity_theta(self, ncut: int) -> csc_matrix:
        """
        Returns Operator identity in the charge basis.
        """
        dim_theta = 2 * ncut + 1
        return sparse.identity(dim_theta, format="csc")

    def _n_theta_operator(self, ncut: int) -> csc_matrix:
        """
        Returns charge operator `n` in the charge basis.
        """
        dim_theta = 2 * ncut + 1
        diag_elements = np.arange(-ncut, ncut + 1)
        n_theta_matrix = sparse.dia_matrix(
            (diag_elements, [0]), shape=(dim_theta, dim_theta)
        ).tocsc()
        return n_theta_matrix

    def _exp_i_theta_operator(self, ncut) -> csc_matrix:
        r"""
        Operator :math:`\cos(\theta)`, acting only on the `\theta` Hilbert subspace.
        """
        dim_theta = 2 * ncut + 1
        matrix = (
            sparse.dia_matrix(([-1.0] * dim_theta, [-1]), shape=(dim_theta, dim_theta))
        ).tocsc()
        return matrix

    def _exp_i_theta_operator_conjugate(self, ncut) -> csc_matrix:
        r"""
        Operator :math:`\cos(\theta)`, acting only on the `\theta` Hilbert subspace.
        """
        dim_theta = 2 * ncut + 1
        matrix = (
            sparse.dia_matrix(([-1.0] * dim_theta, [1]), shape=(dim_theta, dim_theta))
        ).tocsc()
        return matrix

    def _cos_theta(self, ncut: int) -> csc_matrix:
        """Returns operator :math:`\\cos \\varphi` in the charge basis"""
        cos_op = 0.5 * (
            self._exp_i_theta_operator(ncut)
            + self._exp_i_theta_operator_conjugate(ncut)
        )
        return cos_op

    def _sin_theta(self, ncut: int) -> csc_matrix:
        """Returns operator :math:`\\sin \\varphi` in the charge basis"""
        sin_op = (
            -1j
            * 0.5
            * (
                self._exp_i_theta_operator(ncut)
                - self._exp_i_theta_operator_conjugate(ncut)
            )
        )
        return sin_op

    def circuit_operators(self):
        """
        Returns the set of operators needed to evaluate the Hamiltonian of the current instance.
        """
        periodic_vars = self.vars["periodic"]
        normal_vars = self.vars["extended"]

        index_list = [j for i in list(self.var_categories.values()) for j in i]
        cutoff_names = [j for i in list(self.get_cutoffs().values()) for j in i]
        cutoffs = dict(zip(index_list, cutoff_names))

        grids = {}
        for i in self.var_categories["extended"]:
            grids[i] = discretization.Grid1d(
                self.discretized_phi_range[i][0],
                self.discretized_phi_range[i][1],
                cutoffs[i],
            )

        # constructing the operators for extended variables

        if self.ext_basis == "discretized":
            extended_operators = {
                "position": [],
                "momentum": [],
                "momentum_squared": [],
                "cos": [],
                "sin": [],
            }
            for v in normal_vars["position"]:  # position operators
                index = int(get_trailing_number(v.name))
                phi_operator = self._phi_operator(grids[index])
                extended_operators["position"].append(
                    self._kron_operator(phi_operator, index)
                )
                extended_operators["sin"].append(
                    self._kron_operator(self._sin_phi(grids[index]), index)
                )
                extended_operators["cos"].append(
                    self._kron_operator(self._cos_phi(grids[index]), index)
                )
            for v in normal_vars["momentum"]:  # momentum operators
                index = int(get_trailing_number(v.name))
                a_operator = self._i_d_dphi_operator(grids[index])
                extended_operators["momentum"].append(
                    self._kron_operator(a_operator, index)
                )
            for v in normal_vars["momentum_squared"]:  # squared momentum operators
                index = int(get_trailing_number(v.name))
                ps_operator = self._i_d2_dphi2_operator(grids[index])
                extended_operators["momentum_squared"].append(
                    self._kron_operator(ps_operator, index)
                )
        elif self.ext_basis == "harmonic":
            H = self._hamiltonian_sym_for_numerics
            index_list = [j for i in list(self.var_categories.values()) for j in i]
            cutoff_names = [j for i in list(self.get_cutoffs().values()) for j in i]
            cutoffs_dict = dict(zip(index_list, cutoff_names))
            # substitute all the parameter values
            H = H.subs(
                [
                    (param, getattr(self, str(param)))
                    for param in self.param_vars
                    + self.external_fluxes
                    + self.offset_charges
                ]
            )

            # calculate oscillator frequencies and use harmonic oscillator basis
            osc_lengths = {}
            osc_freqs = {}
            extended_operators = {
                "annihilation": [],
                "creation": [],
                "number": [],
                "position": [],
                "momentum": [],
                "sin": [],
                "cos": [],
            }
            for var_index in self.var_categories["extended"]:
                ECi = float(H.coeff("Q" + str(var_index) + "**2").cancel()) / 4
                ELi = float(H.coeff("θ" + str(var_index) + "**2").cancel()) * 2
                osc_freqs[var_index] = (8 * ELi * ECi) ** 0.5
                osc_lengths[var_index] = (8.0 * ECi / ELi) ** 0.25
                ad_operator = op.creation_sparse(cutoffs_dict[var_index])
                a_operator = op.annihilation_sparse(cutoffs_dict[var_index])
                extended_operators["creation"].append(
                    self._kron_operator(a_operator, var_index)
                )
                extended_operators["annihilation"].append(
                    self._kron_operator(ad_operator, var_index)
                )
                extended_operators["number"].append(
                    self._kron_operator(
                        op.number_sparse(cutoffs_dict[var_index]), var_index
                    )
                )
                x_operator = (
                    (ad_operator + a_operator) * osc_lengths[var_index] / (2 ** 0.5)
                )
                extended_operators["position"].append(
                    self._kron_operator(x_operator, var_index)
                )
                extended_operators["momentum"].append(
                    self._kron_operator(
                        1j
                        * (ad_operator - a_operator)
                        / (osc_lengths[var_index] * 2 ** 0.5),
                        var_index,
                    )
                )

                extended_operators["sin"].append(
                    self._kron_operator(
                        sp.linalg.sinm(x_operator.toarray()),
                        var_index,
                    )
                )

                extended_operators["cos"].append(
                    self._kron_operator(
                        sp.linalg.cosm(x_operator.toarray()),
                        var_index,
                    )
                )

            self.osc_lengths = osc_lengths
            self.osc_freqs = osc_freqs

        # constructing the operators for periodic variables
        periodic_operators = {"sin": [], "cos": [], "number": []}
        for v in periodic_vars["sin"]:
            index = int(v.name[2:])
            a_operator = self._change_sparsity(self._sin_theta(cutoffs[index]))
            periodic_operators["sin"].append(self._kron_operator(a_operator, index))
        for v in periodic_vars["cos"]:
            index = int(v.name[2:])
            a_operator = self._change_sparsity(self._cos_theta(cutoffs[index]))
            periodic_operators["cos"].append(self._kron_operator(a_operator, index))
        for v in periodic_vars["number"]:
            index = int(v.name[1:])
            n_operator = self._change_sparsity(self._n_theta_operator(cutoffs[index]))
            periodic_operators["number"].append(self._kron_operator(n_operator, index))

        return {
            "periodic": periodic_operators,
            "extended": extended_operators,
            "identity": [self._identity()],
        }

    ##################################################################
    ################ Functions for parameter queries #################
    ##################################################################
    def get_params(self) -> List[float]:
        """
        Method to get the circuit parameters set using the instance attributes.
        """
        params = []
        for param in self.param_vars:
            params.append(getattr(self, param.name))
        return params

    def get_cutoffs(self) -> Dict[str, list]:
        """
        Method to get the cutoffs for each of the circuit's degree of freedom.
        """
        cutoffs_dict = {
            "cutoff_n": [],
            "cutoff_ext": [],
        }

        for cutoff_type in cutoffs_dict.keys():
            attr_list = [x for x in self.cutoff_names if cutoff_type in x]

            if len(attr_list) > 0:
                attr_list.sort()
                cutoffs_dict[cutoff_type] = [getattr(self, attr) for attr in attr_list]

        return cutoffs_dict

    def get_external_flux(self) -> List[float]:
        """
        Returns all the time independent external flux set using the circuit attributes
        for each of the independent loops detected.
        """
        return [getattr(self, flux.name) for flux in self.external_fluxes]

    def get_offset_charges(self) -> List[float]:
        """
        Returns all the offset charges set using the circuit attributes for each of the
        periodic degree of freedom.
        """
        return [
            getattr(self, offset_charge.name) for offset_charge in self.offset_charges
        ]

    def get_all_operators(self, return_dict: bool = False):
        """
        Returns a list of operators which can be given as an argument to
        self._hamiltonian_sym_for_numericsunc. These operators are not calculated again
        and are fetched directly from the circuit attibutes. Use set_attributes instead
        if the paramaters, expecially cutoffs, are changed.

        Parameters
        ----------

        return_dict: bool
            returns the dictionary of operators if set to True.
        """
        variable_symbols_list = flatten_list(
            self.vars["periodic"].values()
        ) + flatten_list(self.vars["extended"].values())
        operator_list = []
        for operator in variable_symbols_list:
            operator_list.append(getattr(self, operator.name))

        # adding the identity operator
        operator_list.append(self._identity())

        if return_dict:
            variable_symbols_list.append(sm.symbols("I"))
            operator_dict = dict(zip(variable_symbols_list, operator_list))
            return operator_dict

        return operator_list

    @staticmethod
    def default_params() -> Dict[str, Any]:
        # return {"EJ": 15.0, "EC": 0.3, "ng": 0.0, "ncut": 30, "truncated_dim": 10}

        return {}

    def set_operators(self):
        """
        Sets the operator attributes of the circuit with new operators calculated using
        the paramaters set in the circuit attributes. Returns a list of operators
        similar to the method get_operators.
        """

        if self.hierarchical_diagonalization:
            for subsys in self.subsystems.values():
                subsys.set_operators()
        else:
            variable_symbols_list = (
                flatten_list(self.vars["periodic"].values())
                + flatten_list(self.vars["extended"].values())
                + self.vars["identity"]
            )

            # if not self.is_child:
            ops = self.circuit_operators()
            operator_list = flatten_list(ops["periodic"].values()) + flatten_list(
                ops["extended"].values()
            )

            operator_list = operator_list + [self._identity()]

            for x, operator in enumerate(variable_symbols_list):
                setattr(self, operator.name, operator_list[x])

    def get_operator(self, operator_name: str) -> qt.Qobj:
        """
        Returns the operator for the given operator symbol which has the same dimension
        as the hilbertdim of the instance from which the operator is requested.

        Parameters
        ----------
        operator_name:
            Name of a sympy Symbol object which should be one among the symbols in the attribute vars

        Returns
        -------
            operator
        """
        if not self.hierarchical_diagonalization:
            return getattr(self, operator_name)
        else:
            var_index = get_trailing_number(operator_name)
            subsystem_index = self.get_subsystem_index(var_index)
            subsystem = self.subsystems[subsystem_index]
            operator = subsystem.get_operator(operator_name)

            if isinstance(operator, qt.Qobj):
                operator = operator.full()

            operator = convert_matrix_to_qobj(
                operator,
                subsystem,
                op_in_eigenbasis=False,
                evecs=None,
            )
            return identity_wrap(operator, subsystem, list(self.subsystems.values()))

    ##################################################################
    ############# Functions for eigen values and matrices ############
    ##################################################################
    def _is_mat_mul_replacement_necessary(self, term):
        return (
            set(self.var_categories["extended"])
            & set([get_trailing_number(str(i)) for i in term.free_symbols])
        ) and "*" in str(term)

    def _replace_mat_mul_operator(self, term):

        if not self._is_mat_mul_replacement_necessary(term):
            return str(term)

        if self.ext_basis == "discretized":
            term_string = str(term)
            term_var_categories = [
                get_trailing_number(str(i)) for i in term.free_symbols
            ]
            if len(set(term_var_categories) & set(self.var_categories["extended"])) > 1:
                if all(["Q" in var.name for var in term.free_symbols]):
                    term_string = str(term).replace(
                        "*", "@"
                    )  # replacing all the * with @

        elif self.ext_basis == "harmonic":
            term_string = ""
            # replace ** with np.matrix_power
            if "**" in str(term):
                operators = [
                    match.replace("**", "")
                    for match in re.findall(r"[^*]+\*{2}", str(term), re.MULTILINE)
                ]
                exponents = re.findall(r"(?<=\*{2})\d", str(term), re.MULTILINE)

                new_string_list = []
                for x, operator in enumerate(operators):
                    if get_trailing_number(operator) in self.var_categories["extended"]:
                        new_string_list.append(
                            "matrix_power(" + operator + "," + exponents[x] + ")"
                        )
                    else:
                        new_string_list.append(operator + "**" + exponents[x])
                term_string = "*".join(new_string_list)
            else:
                term_string = str(term)

            # replace * with @ in the entire term
            if len(term.free_symbols) > 1:
                term_string = re.sub(
                    r"(?<=[^*])\*{1}(?!\*)", "@", term_string, re.MULTILINE
                )
        return term_string

    def _get_eval_hamiltonian_string(self, H):
        """
        Returns the string which defines the expression for Hamiltonian in harmonic
        oscillator basis
        """
        expr_dict = H.as_coefficients_dict()
        terms_list = list(expr_dict.keys())
        coeff_list = list(expr_dict.values())

        H_string = ""
        for x, term in enumerate(terms_list):
            term_string = (
                str(coeff_list[x]) + "*" + self._replace_mat_mul_operator(term)
            )
            if float(coeff_list[x]) > 0:
                term_string = "+" + term_string
            H_string += term_string

        return H_string

    @staticmethod
    def matrix_power_sparse(x, n: int):
        res = x.copy()
        for i in range(n - 1):
            res = res @ x
        return res

    @staticmethod
    def _cos_dia_dense(x):
        """
        This is a special function to calculate the cos of dense diagonal matrices
        """
        return np.diag(np.cos(x.diagonal()))

    @staticmethod
    def _sin_dia_dense(x):
        """
        This is a special function to calculate the sin of dense diagonal matrices
        """
        return np.diag(np.sin(x.diagonal()))

    def _hamiltonian_for_harmonic_extended_vars(self):

        hamiltonian = self._hamiltonian_sym_for_numerics
        index_list = [j for i in list(self.var_categories.values()) for j in i]
        cutoff_names = [j for i in list(self.get_cutoffs().values()) for j in i]
        cutoffs_dict = dict(zip(index_list, cutoff_names))
        # substitute all the parameter values
        hamiltonian = hamiltonian.subs(
            [
                (param, getattr(self, str(param)))
                for param in self.param_vars
                + self.external_fluxes
                + self.offset_charges
            ]
        )
        hamiltonian = hamiltonian.subs(
            "I", 1
        )  # does not make a difference as all the trignometric expressions are
        # expanded out.
        # remove constants from the Hamiltonian
        hamiltonian -= hamiltonian.as_coefficients_dict()[1]
        hamiltonian = hamiltonian.expand()

        # replace the extended degrees of freedom with harmonic oscillators
        for var_index in self.var_categories["extended"]:
            ECi = float(hamiltonian.coeff("Q" + str(var_index) + "**2").cancel()) / 4
            ELi = float(hamiltonian.coeff("θ" + str(var_index) + "**2").cancel()) * 2
            osc_freq = (8 * ELi * ECi) ** 0.5
            osc_length = (8.0 * ECi / ELi) ** 0.25
            hamiltonian = (
                (
                    hamiltonian
                    - ECi * 4 * sm.symbols("Q" + str(var_index)) ** 2
                    - ELi / 2 * sm.symbols("θ" + str(var_index)) ** 2
                    + osc_freq * (sm.symbols("Nh" + str(var_index)))
                )
                .cancel()
                .expand()
            )

        H_str = self._get_eval_hamiltonian_string(hamiltonian)
        self._H_str_harmonic = H_str

        variable_symbols_list = (
            flatten_list(self.vars["periodic"].values())
            + flatten_list(self.vars["extended"].values())
            + self.vars["identity"]
        )

        variable_str_list = [
            str(operator)
            for operator in variable_symbols_list
            + self.offset_charges
            + self.external_fluxes
        ]
        variable_values_list = (
            self.get_all_operators()
            + self.get_offset_charges()
            + self.get_external_flux()
        )
        variable_dict = dict(zip(variable_str_list, variable_values_list))

        # adding matrix power to the dict
        if self.type_of_matrices == "dense":
            variable_dict["matrix_power"] = np.linalg.matrix_power
            variable_dict["cos"] = self._cos_dia_dense
            variable_dict["sin"] = self._sin_dia_dense
        else:
            variable_dict["matrix_power"] = self.matrix_power_sparse
            variable_dict["cos"] = self._cos_dia
            variable_dict["sin"] = self._sin_dia

        return eval(H_str, variable_dict)

    def _hamiltonian_for_discretized_extended_vars(self):

        H = self._hamiltonian_sym_for_numerics
        H = H.subs(
            [
                (param, getattr(self, str(param)))
                for param in self.param_vars
                + self.external_fluxes
                + self.offset_charges
            ]
        )
        # remove constants from the Hamiltonian
        H = H - H.as_coefficients_dict()[1]
        H = H.expand()

        H_str = self._get_eval_hamiltonian_string(H)
        self._H_str_sparse = H_str

        variable_dict = self.get_all_operators(return_dict=True)
        # changing variables to strings
        variable_dict_str = dict(
            zip([var.name for var in variable_dict.keys()], variable_dict.values())
        )
        variable_dict_str["cos"] = self._cos_dia
        variable_dict_str["sin"] = self._sin_dia

        return eval(H_str, variable_dict_str)

        # replace * with @ for non-diagonal operators

    def hamiltonian(self):
        """
        Returns the Hamiltonian of the Circuit.
        """

        if not self.hierarchical_diagonalization:
            if self.ext_basis == "harmonic":
                return self._hamiltonian_for_harmonic_extended_vars()
            elif self.ext_basis == "discretized":
                return self._hamiltonian_for_discretized_extended_vars()

        else:
            bare_esys = {
                sys_index: sys.eigensys(evals_count=sys.truncated_dim)
                for sys_index, sys in enumerate(self.hilbert_space.subsys_list)
            }
            hamiltonian = self.hilbert_space.hamiltonian(bare_esys=bare_esys)
            if self.type_of_matrices == "dense":
                return hamiltonian.full()
            elif self.type_of_matrices == "sparse":
                return hamiltonian.data.tocsc()

    ##################################################################
    ########### Functions from scqubits.core.qubit_base ##############
    ##################################################################
    def _evals_calc(self, evals_count: int) -> ndarray:
        # dimension of the hamiltonian
        hilbertdim = self.hilbertdim()

        hamiltonian_mat = self.hamiltonian()
        if self.type_of_matrices == "sparse":
            evals = sparse.linalg.eigsh(
                hamiltonian_mat,
                return_eigenvectors=False,
                k=evals_count,
                v0=settings.RANDOM_ARRAY[:hilbertdim],
                which="SA",
            )
        elif self.type_of_matrices == "dense":
            evals = sp.linalg.eigvalsh(
                hamiltonian_mat, subset_by_index=[0, evals_count - 1]
            )
        return np.sort(evals)

    def _esys_calc(self, evals_count: int) -> Tuple[ndarray, ndarray]:
        # dimension of the hamiltonian
        hilbertdim = self.hilbertdim()

        hamiltonian_mat = self.hamiltonian()
        if self.type_of_matrices == "sparse":
            evals, evecs = sparse.linalg.eigsh(
                hamiltonian_mat,
                return_eigenvectors=True,
                k=evals_count,
                which="SA",
                v0=settings.RANDOM_ARRAY[:hilbertdim],
            )
        elif self.type_of_matrices == "dense":
            evals, evecs = sp.linalg.eigh(
                hamiltonian_mat,
                eigvals_only=False,
                subset_by_index=[0, evals_count - 1],
            )
        evals, evecs = order_eigensystem(evals, evecs)
        return evals, evecs

    def matrixelement_table(
        self,
        operator: str,
        evecs: ndarray = None,
        evals_count: int = 6,
        filename: str = None,
        return_datastore: bool = False,
    ) -> ndarray:
        """Returns table of matrix elements for `operator` with respect to the
        eigenstates of the qubit. The operator is given as a string matching a class
        method returning an operator matrix. E.g., for an instance `trm` of Transmon,
        the matrix element table for the charge operator is given by
        `trm.op_matrixelement_table('n_operator')`. When `esys` is set to `None`,
        the eigensystem is calculated on-the-fly.

        Parameters
        ----------
        operator:
            name of class method in string form, returning operator matrix in
            qubit-internal basis.
        evecs:
            if not provided, then the necessary eigenstates are calculated on the fly
        evals_count:
            number of desired matrix elements, starting with ground state
            (default value = 6)
        filename:
            output file name
        return_datastore:
            if set to true, the returned data is provided as a DataStore object
            (default value = False)
        """
        if evecs is None:
            _, evecs = self.eigensys(evals_count=evals_count)
        operator_matrix = getattr(self, operator)
        table = get_matrixelement_table(operator_matrix, evecs)
        if filename or return_datastore:
            data_store = DataStore(
                system_params=self.get_initdata(), matrixelem_table=table
            )
        if filename:
            data_store.filewrite(filename)
        return data_store if return_datastore else table

    ##################################################################
    ############### Functions for plotting potential #################
    ##################################################################
    def potential_energy(self, **kwargs) -> ndarray:
        """
        Returns the full potential of the circuit evaluated in a grid of points as
        chosen by the user or using default variable ranges.

        Parameters
        ----------
        θ<index>:
            value(s) for variable :math:`\theta_i` in the potential.
        """
        periodic_indices = self.var_categories["periodic"]
        discretized_ext_indices = self.var_categories["extended"]
        var_categories = discretized_ext_indices + periodic_indices

        # method to concatenate sublists
        potential_sym = self.potential_symbolic
        for var in self.external_fluxes:
            potential_sym = potential_sym.subs(var, var * np.pi * 2)

        # constructing the grids
        parameters = dict.fromkeys(
            ["θ" + str(index) for index in var_categories]
            + [var.name for var in self.external_fluxes]
            + [var.name for var in self.param_vars]
        )

        for var_name in kwargs:
            if isinstance(kwargs[var_name], np.ndarray):
                parameters[var_name] = kwargs[var_name]
            elif isinstance(kwargs[var_name], (int, float)):
                parameters[var_name] = kwargs[var_name]
            else:
                raise AttributeError(
                    "Only float, Numpy ndarray or int assignments are allowed."
                )

        for var_name in parameters.keys():
            if parameters[var_name] is None:
                if var_name in [
                    var.name for var in self.param_vars + self.external_fluxes
                ]:
                    parameters[var_name] = getattr(self, var_name)
                elif var_name in ["θ" + str(index) for index in var_categories]:
                    raise AttributeError(var_name + " is not set.")

        # creating a meshgrid for multiple dimensions
        sweep_vars = {}
        for var_name in kwargs:
            if isinstance(kwargs[var_name], np.ndarray):
                sweep_vars[var_name] = kwargs[var_name]
        if len(sweep_vars) > 1:
            sweep_vars.update(
                zip(
                    sweep_vars,
                    np.meshgrid(*[grid for grid in sweep_vars.values()]),
                )
            )
            for var_name in sweep_vars:
                parameters[var_name] = sweep_vars[var_name]

        potential_func = sm.lambdify(parameters.keys(), potential_sym, "numpy")

        return potential_func(*parameters.values())

    def plot_potential(self, **kwargs):
        r"""
        Returns the plot of the potential for the circuit instance. Make sure to not set
        more than two variables in the instance.potential to a Numpy array, as the the
        code cannot plot with more than 3 dimensions.

        Parameters
        ----------
        θ<index>: Union[ndarray, float]
            value(s) for the variable :math:`\theta_i` occurring in the potential.
        """

        periodic_indices = self.var_categories["periodic"]
        discretized_ext_indices = self.var_categories["extended"]
        var_categories = discretized_ext_indices + periodic_indices

        # constructing the grids
        parameters = dict.fromkeys(
            ["θ" + str(index) for index in var_categories]
            + [var.name for var in self.external_fluxes]
            + [var.name for var in self.param_vars]
        )

        sweep_vars = {}
        for var_name in kwargs:
            if isinstance(kwargs[var_name], np.ndarray):
                sweep_vars[var_name] = kwargs[var_name]
        if len(sweep_vars) > 1:
            sweep_vars.update(zip(sweep_vars, np.meshgrid(*list(sweep_vars.values()))))
            for var_name in sweep_vars:
                parameters[var_name] = sweep_vars[var_name]

        if len(sweep_vars) > 2:
            raise AttributeError(
                "Cannot plot with a dimension greater than 3; Only give a maximum of "
                "two grid inputs"
            )

        potential_energies = self.potential_energy(**kwargs)

        if len(sweep_vars) == 1:
            plot = plt.plot(*(list(sweep_vars.values()) + [potential_energies]))
            plt.xlabel(list(sweep_vars.keys())[0])
            plt.ylabel("Potential energy in GHz")

        if len(sweep_vars) == 2:
            plot = plt.contourf(*(list(sweep_vars.values()) + [potential_energies]))
            var_names = list(sweep_vars.keys())
            plt.xlabel(var_names[0])
            plt.ylabel(var_names[1])
            cbar = plt.colorbar()
            cbar.set_label("Potential energy in GHz")
        return plot

    ##################################################################
    ############# Functions for plotting wavefunction ################
    ##################################################################
    def _recursive_basis_change(
        self, wf_reshaped, wf_dim, subsystem, relevant_indices=None
    ):
        """
        Method to change the basis recursively, to reverse hierarchical diagonalization
        and get to the basis in which the variables were initially defined.

        Parameters
        ----------
        wf_dim:
            The dimension of the wavefunction which needs to be rewritten in terms of
            the initial basis

        """
        _, U_subsys = subsystem.eigensys(evals_count=subsystem.truncated_dim)
        wf_sublist = list(range(len(wf_reshaped.shape)))
        U_sublist = [wf_dim, len(wf_sublist)]
        target_sublist = wf_sublist.copy()
        target_sublist[wf_dim] = len(wf_sublist)
        wf_new_basis = np.einsum(
            wf_reshaped, wf_sublist, U_subsys.T, U_sublist, target_sublist
        )
        if subsystem.hierarchical_diagonalization:
            wf_shape = list(wf_new_basis.shape)
            wf_shape[wf_dim] = [
                sub_subsys.truncated_dim for sub_subsys in subsystem.subsystems.values()
            ]
            wf_new_basis = wf_new_basis.reshape(flatten_list_recursive(wf_shape))
            for sub_subsys_index, sub_subsys in enumerate(
                subsystem.subsystems.values()
            ):
                if len(set(relevant_indices) & set(sub_subsys.var_categories_list)) > 0:
                    wf_new_basis = self._recursive_basis_change(
                        wf_new_basis,
                        wf_dim + sub_subsys_index,
                        sub_subsys,
                        relevant_indices=relevant_indices,
                    )
        else:
            if len(set(relevant_indices) & set(subsystem.var_categories_list)) > 0:
                wf_shape = list(wf_new_basis.shape)
                wf_shape[wf_dim] = [
                    getattr(subsystem, cutoff_attrib)
                    if "ext" in cutoff_attrib
                    else (2 * getattr(subsystem, cutoff_attrib) + 1)
                    for cutoff_attrib in subsystem.cutoff_names
                ]
                wf_new_basis = wf_new_basis.reshape(flatten_list_recursive(wf_shape))
        return wf_new_basis

    def _basis_change_harm_osc_to_phi(self, wf_original_basis, wf_dim, var_index):
        """
        Method to change the basis from harmonic oscillator to phi basis
        """
        U_ho_phi = np.array(
            [
                osc.harm_osc_wavefunction(
                    n,
                    self._default_grid_phi.make_linspace(),
                    self.osc_lengths[var_index],
                )
                for n in range(getattr(self, "cutoff_ext_" + str(var_index)))
            ]
        )
        wf_sublist = [idx for idx, _ in enumerate(wf_original_basis.shape)]
        U_sublist = [wf_dim, len(wf_sublist)]
        target_sublist = wf_sublist.copy()
        target_sublist[wf_dim] = len(wf_sublist)
        wf_ext_basis = np.einsum(
            wf_original_basis, wf_sublist, U_ho_phi, U_sublist, target_sublist
        )
        return wf_ext_basis

    def _basis_change_n_to_phi(self, wf_original_basis, wf_dim, var_index):
        """
        Method to change the basis from harmonic oscillator to phi basis
        """
        U_n_phi = np.array(
            [
                np.exp(n * self._default_grid_phi.make_linspace() * 1j)
                for n in range(2 * getattr(self, "cutoff_n_" + str(var_index)) + 1)
            ]
        )
        wf_sublist = list(range(len(wf_original_basis.shape)))
        U_sublist = [wf_dim, len(wf_sublist)]
        target_sublist = wf_sublist.copy()
        target_sublist[wf_dim] = len(wf_sublist)
        wf_ext_basis = np.einsum(
            wf_original_basis, wf_sublist, U_n_phi, U_sublist, target_sublist
        )
        return wf_ext_basis

    def _get_var_dim_for_reshaped_wf(self, wf_var_categories, var_index):
        wf_dim = 0
        system_hierarchy_for_vars_chosen = list(
            set(
                [
                    self.get_subsystem_index(index)
                    for index in np.sort(wf_var_categories)
                ]
            )
        )
        for subsys_index in self.subsystems:
            if subsys_index not in system_hierarchy_for_vars_chosen:
                wf_dim += 1
            if subsys_index in system_hierarchy_for_vars_chosen:
                subsys_var_index_list = self.subsystems[
                    subsys_index
                ].var_categories_list
                if var_index not in subsys_var_index_list:
                    wf_dim += len(subsys_var_index_list)
                else:
                    wf_dim += subsys_var_index_list.index(var_index)
                    break
        return wf_dim

    def generate_wf_plot_data(
        self,
        n: int = 0,
        var_categories: Tuple[int] = (1,),
        eigensys: ndarray = None,
        mode: str = "abs",
        change_discrete_charge_to_phi: bool = True,
    ):
        """
        Returns the plot of the probability density of the wavefunction in the requested
        variables for the current Circuit instance.

        Parameters
        ----------
        n:
            integer to choose which wavefunction to plot
        var_categories:
            A tuple containing the indices of the variables chosen to plot the
            wavefunction in. Should not have more than 2 entries.
        mode:
            "abs" or "real" or "imag" for absolute, real or imaginary parts of the
            wavefunction.
        eigensys:
            The object returned by the method instance. `eigensys` is used to avoid the
            re-evaluation of the eigensystems if already evaluated.
        change_discrete_charge_to_phi: bool
            boolean to choose if the discrete charge basis for the periodic variable
            needs to be changed to phi basis.
        """
        # checking to see if eigensys needs to be generated
        if eigensys is None:
            _, wfs = self.eigensys()
        else:
            _, wfs = eigensys

        wf = wfs[:, n]
        if self.hierarchical_diagonalization:
            system_hierarchy_for_vars_chosen = list(
                set(
                    [
                        self.get_subsystem_index(index)
                        for index in np.sort(var_categories)
                    ]
                )
            )  # getting the subsystem index for each of the index dimension
            subsystems_for_vars_chosen = [
                self.subsystems[sys_index]
                for sys_index in system_hierarchy_for_vars_chosen
            ]
            subsys_trunc_dims = [sys.truncated_dim for sys in self.subsystems.values()]
            # reshaping the wavefunctions to truncated dims of subsystems
            wf_hd_reshaped = wf.reshape(*subsys_trunc_dims)

            # **** Converting to the basis in which the variables are defined *****
            wf_original_basis = wf_hd_reshaped
            wf_dim = system_hierarchy_for_vars_chosen[0]
            for subsys_index in system_hierarchy_for_vars_chosen:
                wf_dim = 0
                for sys_index in range(subsys_index):
                    if sys_index in system_hierarchy_for_vars_chosen:
                        wf_dim += len(self.subsystems[sys_index].var_categories_list)
                    else:
                        wf_dim += 1
                wf_original_basis = self._recursive_basis_change(
                    wf_original_basis,
                    wf_dim,
                    self.subsystems[subsys_index],
                    relevant_indices=var_categories,
                )
        else:
            wf_original_basis = wf.reshape(
                *[
                    getattr(self, cutoff_attrib)
                    if "ext" in cutoff_attrib
                    else (2 * getattr(self, cutoff_attrib) + 1)
                    for cutoff_attrib in self.cutoff_names
                ]
            )

        # making a basis change to phi for every var_index
        wf_ext_basis = wf_original_basis
        for x, var_index in enumerate(var_categories):
            # finding the dimension corresponding to the var_index
            if not self.hierarchical_diagonalization:
                wf_dim = var_index - 1
            else:
                wf_dim = self._get_var_dim_for_reshaped_wf(var_categories, var_index)

            if (
                var_index in self.var_categories["extended"]
                and self.ext_basis == "harmonic"
            ):
                wf_ext_basis = self._basis_change_harm_osc_to_phi(
                    wf_ext_basis, wf_dim, var_index
                )
            if change_discrete_charge_to_phi:
                if var_index in self.var_categories["periodic"]:
                    wf_ext_basis = self._basis_change_n_to_phi(
                        wf_ext_basis, wf_dim, var_index
                    )

        # if a probability plot is requested, sum over the dimesnsions not relevant to
        # the ones in var_categories
        if self.hierarchical_diagonalization:
            num_wf_dims = 0
            dims_to_be_summed = []
            for x, subsys_index in enumerate(self.subsystems):
                if subsys_index in system_hierarchy_for_vars_chosen:
                    for index, var_index in enumerate(
                        self.subsystems[subsys_index].var_categories_list
                    ):
                        if var_index not in var_categories:
                            dims_to_be_summed += [num_wf_dims + index]
                    num_wf_dims += len(
                        self.subsystems[subsys_index].var_categories_list
                    )
                else:
                    dims_to_be_summed += [num_wf_dims]
                    num_wf_dims += 1
            wf_plot = np.sum(
                np.abs(wf_ext_basis),
                axis=tuple(dims_to_be_summed),
            )
        else:
            wf_plot = np.sum(
                np.abs(wf_ext_basis),
                axis=tuple(
                    [
                        var_index - 1
                        for var_index in self.var_categories["periodic"]
                        + self.var_categories["extended"]
                        if var_index not in var_categories
                    ]
                ),
            )

        return wf_plot

    def plot_wavefunction(
        self,
        n=0,
        var_categories: Tuple[int] = (1,),
        eigensys=None,
        mode: str = "abs",
        change_discrete_charge_to_phi: bool = True,
    ):
        """
        Returns the plot of the probability density of the wavefunction in the requested
        variables for the current Circuit instance.

        Parameters
        ----------
        n:
            integer to choose which wavefunction to plot
        var_categories:
            A tuple containing the indices of the variables chosen to plot the
            wavefunction in. Should not have more than 2 entries.
        mode:
            "abs" or "real" or "imag" for absolute, real or imaginary parts of the
            wavefunction.
        eigensys:
            The object returned by the method `.eigensys`, is used to avoid the
            re-evaluation of the eigen systems if already evaluated.
        change_discrete_charge_to_phi:
            chooses if the discrete charge basis for the periodic variable
            needs to be changed to phi basis.
        """
        if len(var_categories) > 2:
            raise AttributeError(
                "Cannot plot wavefunction in more than 2 dimensions. The number of "
                "dimensions should be less than 2."
            )
        var_categories = np.sort(var_categories)
        cutoffs_dict = {}  # dictionary for cutoffs for each variable index
        grids_dict = {}
        var_index_dims_dict = {}
        for cutoff_attrib in self.cutoff_names:
            var_index = get_trailing_number(cutoff_attrib)
            cutoffs_dict[var_index] = getattr(self, cutoff_attrib)
            if "cutoff_n" in cutoff_attrib:
                grids_dict[var_index] = self._default_grid_phi.make_linspace()
            else:
                var_index_dims_dict[var_index] = getattr(self, cutoff_attrib)
                if self.ext_basis == "harmonic":
                    grid = self._default_grid_phi.make_linspace()
                elif self.ext_basis == "discretized":
                    grid = np.linspace(
                        self.discretized_phi_range[var_index][0],
                        self.discretized_phi_range[var_index][1],
                        cutoffs_dict[var_index],
                    )
                grids_dict[var_index] = grid

        wf_plot = self.generate_wf_plot_data(
            n=n,
            var_categories=var_categories,
            eigensys=eigensys,
            change_discrete_charge_to_phi=change_discrete_charge_to_phi,
        )

        var_types = []

        for var_index in np.sort(var_categories):
            if var_index in self.var_categories["periodic"]:
                if not change_discrete_charge_to_phi:
                    var_types.append("Charge in units of 2e, variable:")
                else:
                    var_types.append("Dimensionless flux, discrete charge variable:")
            else:
                var_types.append("Dimensionless flux, variable:")

        if len(var_categories) == 1:
            if not change_discrete_charge_to_phi and (
                var_categories[0] in self.var_categories["periodic"]
            ):
                plt.bar(
                    np.arange(-cutoffs_dict[var_index], cutoffs_dict[var_index] + 1)
                    / (2 * np.pi),
                    eval("np." + mode + "(wf_plot.T)"),
                )
            else:
                plt.plot(
                    np.array(grids_dict[var_categories[0]]) / (2 * np.pi),
                    eval("np." + mode + "(wf_plot.T)"),
                )
            plt.xlabel(var_types[0] + str(var_categories[0]))
        elif len(var_categories) == 2:
            x, y = np.meshgrid(
                np.array(grids_dict[var_categories[0]]) / (2 * np.pi),
                np.array(grids_dict[var_categories[1]]) / (2 * np.pi),
            )
            plt.contourf(x, y, np.abs(wf_plot.T))
            plt.xlabel(var_types[0] + str(var_categories[0]))
            plt.ylabel(var_types[1] + str(var_categories[1]))
            plt.colorbar()
        plt.title("Distribution of wavefunction along variables " + str(var_categories))


class Circuit(Subsystem):
    def __init__(
        self,
        symbolic_circuit: SymbolicCircuit,
        ext_basis: str = "discretized",
        initiate_sym_calc: bool = True,
        system_hierarchy: list = None,
        subsystem_trunc_dims: list = None,
        truncated_dim: int = None,
    ):
        """
        Class for analysis of custom superconducting circuits.

        Parameters
        ----------
        symbolic_circuit:
            an instance of the class `SymbolicCircuit`
        ext_basis:
            can be "discretized" or "harmonic" which chooses whether to use discretized
            phi or harmonic oscillator basis for extended variables,
            by default "discretized"
        initiate_sym_calc:
            attribute to initiate Circuit instance, by default `True`
        system_hierarchy:
            A list of lists which is provided by the user to define subsystems,
            by default `None`
        subsystem_trunc_dims : list, optional
            a dict object which can be generated for a specific system_hierarchy using
            the method generate_default_trunc_dims, by default `None`
        truncated_dim:
            truncated dimension if the user wants to use this circuit instance in
            HilbertSpace, by default `None`

        Returns
        -------
            An instance of class `Circuit`
        """
        self.is_child = False
        self.symbolic_circuit: SymbolicCircuit = symbolic_circuit

        self.ext_basis: str = ext_basis
        self.truncated_dim: int = truncated_dim
        self.system_hierarchy: list = system_hierarchy
        self.subsystem_trunc_dims: list = subsystem_trunc_dims

        self.discretized_phi_range: Dict[int, Tuple[float, float]] = {}
        self.cutoff_names: List[str] = []

        # setting default grids for plotting
        self._default_grid_phi: discretization.Grid1d = discretization.Grid1d(
            -6 * np.pi, 6 * np.pi, 200
        )

        self.type_of_matrices: str = (
            "sparse"  # type of matrices used to construct the operators
        )

        circuit_data = {
            "var_categories": symbolic_circuit.var_categories,
            "external_fluxes": symbolic_circuit.external_fluxes,
            "offset_charges": symbolic_circuit.offset_charges,
            "param_vars": symbolic_circuit.param_vars,
            "branches": symbolic_circuit.branches,
            "nodes": symbolic_circuit.nodes,
            "lagrangian_symbolic": symbolic_circuit.lagrangian_symbolic,
            "hamiltonian_symbolic": symbolic_circuit.hamiltonian_symbolic,
            "lagrangian_node_vars": symbolic_circuit.lagrangian_node_vars,
        }

        self.__dict__.update(
            circuit_data
        )  # copying the necessary attributes from symbolic circuit.

        self._sys_type = type(self).__name__
        self._id_str = self._autogenerate_id_str()

        # Hamiltonian function
        if initiate_sym_calc:
            self.initiate_circuit()

    def __repr__(self) -> str:
        return self._id_str

    def initiate_circuit(
        self,
        transformation_matrix: ndarray = None,
        system_hierarchy: list = None,
        subsystem_trunc_dims: list = None,
        closure_branches: List[Branch] = None,
    ):
        """
        Method which re-initializes a circuit instance to update, hierarchical
        diagonalization parameters or closure branches or the variable transformation
        used to describe the circuit.

        Parameters
        ----------
        transformation_matrix:
            A user defined variable transformation which has the dimensions of the
            number nodes (not counting the ground node), by default `None`
        system_hierarchy:
            A list of lists which is provided by the user to define subsystems,
            by default `None`
        subsystem_trunc_dims:
            dict object which can be generated for a specific system_hierarchy using the
            method `generate_default_trunc_dims`, by default `None`
        closure_branches:
            List of branches where external flux variables will be specified, by default
            `None` which then chooses closure branches by an internally generated
            spanning tree.

        Raises
        ------
        Exception
            when system_hierarchy is set and subsystem_trunc_dims is not set.
        """
        system_hierarchy = system_hierarchy or self.system_hierarchy
        subsystem_trunc_dims = subsystem_trunc_dims or self.subsystem_trunc_dims

        self.hierarchical_diagonalization = (
            True if system_hierarchy is not None else False
        )

        self.symbolic_circuit.initiate_symboliccircuit(
            transformation_matrix=transformation_matrix,
            closure_branches=closure_branches,
        )
        self.__dict__.update(self.symbolic_circuit.__dict__)

        # removing any of the old cutoffs
        old_cutoffs = []
        for attr in self.__dict__:
            if "_cutoff_" in attr:
                old_cutoffs.append(attr)
        for attr in old_cutoffs:
            delattr(self, attr)

        # initiating the class properties
        self.cutoff_names = []
        for var_type in self.var_categories.keys():
            if var_type == "periodic":
                for idx, var_index in enumerate(self.var_categories["periodic"]):
                    cutoff = (
                        5
                        if not hasattr(self, "parent")
                        else getattr(self.parent, "cutoff_n_" + str(var_index))
                    )
                    self._make_property(
                        "cutoff_n_" + str(var_index), cutoff, "update_cutoffs"
                    )
                    self.cutoff_names.append("cutoff_n_" + str(var_index))
            if var_type == "extended":
                for idx, var_index in enumerate(self.var_categories["extended"]):
                    cutoff = (
                        30
                        if not hasattr(self, "parent")
                        else getattr(self.parent, "cutoff_ext_" + str(var_index))
                    )
                    self._make_property(
                        "cutoff_ext_" + str(var_index), cutoff, "update_cutoffs"
                    )
                    self.cutoff_names.append("cutoff_ext_" + str(var_index))

        # default values for the parameters
        for idx, param in enumerate(self.param_vars):
            # if harmonic oscillator basis is used, param vars become class properties.
            self._make_property(
                param.name, self.param_init_vals[idx], "update_param_vars"
            )

        # setting the ranges for flux ranges used for discrete phi vars
        for v in self.var_categories["extended"]:
            self.discretized_phi_range[v] = (-6 * np.pi, 6 * np.pi)
        # default values for the external flux vars
        for flux in self.external_fluxes:
            # setting the default to zero external flux
            self._make_property(flux.name, 0.0, "update_external_flux_or_charge")
        # default values for the offset charge vars
        for offset_charge in self.offset_charges:
            # default to zero offset charge
            self._make_property(
                offset_charge.name, 0.0, "update_external_flux_or_charge"
            )

        # changing the matrix type if necessary
        if (
            len(flatten_list(self.var_categories.values())) == 1
            and self.ext_basis == "harmonic"
        ):
            self.type_of_matrices = "dense"

        # setting the __init__params attribute
        self._init_params = (
            [param.name for param in self.param_vars]
            + [flux.name for flux in self.external_fluxes]
            + [offset_charge.name for offset_charge in self.offset_charges]
            + self.cutoff_names
            + ["input_string"]
        )

        self._set_vars()  # setting the attribute vars to store operator symbols

        if len(self.symbolic_circuit.nodes) > 3:
            self.hamiltonian_symbolic = (
                self.symbolic_circuit.generate_symbolic_hamiltonian(
                    substitute_params=True
                )
            )

        if system_hierarchy is not None:
            self.hierarchical_diagonalization = (
                system_hierarchy != []
                and system_hierarchy != flatten_list_recursive(system_hierarchy)
            )

        if not self.hierarchical_diagonalization:
            self.generate_hamiltonian_sym_for_numerics()
            self.set_operators()
        else:
            if system_hierarchy is None:
                self.system_hierarchy = [
                    self.var_categories["periodic"] + self.var_categories["extended"]
                ]
            else:
                self.system_hierarchy = system_hierarchy

            if subsystem_trunc_dims is None:
                raise Exception(
                    "The truncated dimensions attribute for hierarchical "
                    "diagonalization is not set."
                )

            self.subsystem_trunc_dims = subsystem_trunc_dims
            self.generate_hamiltonian_sym_for_numerics()
            self.generate_subsystems()

    @classmethod
    def from_yaml(
        cls,
        input_string: str,
        is_file: bool = True,
        ext_basis="discretized",
        basis_completion="simple",
        initiate_sym_calc=True,
        system_hierarchy: list = None,
        subsystem_trunc_dims: list = None,
        truncated_dim: int = None,
    ):
        """
        Create a Circuit class instance from a circuit graph described in an input
        string in YAML format.

        Parameters
        ----------
        input_string:
            string describing the graph of a circuit in the YAML format.
        ext_basis:
            can be "discretized" or "harmonic" which chooses whether to use discretized
            phi or harmonic oscillator basis for extended variables,
            by default "discretized"
        basis_completion:
            either "simple" or "standard", defines the matrix used for completing the
            transformation matrix. Sometimes used to change the variable transformation
            to result in a simpler symbolic Hamiltonian, by default "simple"
        initiate_sym_calc:
            attribute to initiate Circuit instance, by default `True`
        system_hierarchy:
            A list of lists which is provided by the user to define subsystems,
            by default `None`
        subsystem_trunc_dims:
            a dict object which can be generated for a specific system_hierarchy using
            the method generate_default_trunc_dims, by default `None`
        truncated_dim:
            truncated dimension if the user wants to use this circuit instance in
            `HilbertSpace`, by default `None`

        Returns
        -------
            An instance of class `Circuit`
        """

        symboliccircuit = SymbolicCircuit.from_yaml(
            input_string,
            is_file=is_file,
            basis_completion=basis_completion,
            initiate_sym_calc=True,
        )

        return cls(
            symboliccircuit,
            initiate_sym_calc=initiate_sym_calc,
            ext_basis=ext_basis,
            system_hierarchy=system_hierarchy,
            subsystem_trunc_dims=subsystem_trunc_dims,
            truncated_dim=truncated_dim,
        )


# example input strings
def example_circuit(qubit):
    """
    Returns example input strings for AnalyzeQCircuit and CustomQCircuit for some of the
    popular qubits.

    Parameters
    ----------
    qubit:
        "fluxonium" or "transmon" or "zero_pi" or "cos2phi" choosing the respective
        example input strings.
    """

    # example input strings for popular qubits
    fluxonium = "nodes: 2\nbranches:\nJJ	1,2	Ej	Ecj\nL	1,2	El\nC	1,2	Ec"

    transmon = "nodes: 2\nbranches:\nC\t1,2\tEc\nJJ\t1,2\tEj\tEcj\n"

    cos2phi = (
        "nodes: 4\nbranches:\nC\t1,3\tEc\nJJ\t1,2\tEj\tEcj\nJJ\t3,"
        "4\tEj\tEcj\nL\t1,4\tEl\nL\t2,3\tEl\n\n"
    )

    zero_pi = (
        "nodes: 4\nbranches:\nJJ\t1,2\tEj\tEcj\nL\t2,3\tEl\nJJ\t3,"
        "4\tEj\tEcj\nL\t4,1\tEl\nC\t1,3\tEc\nC\t2,4\tEc\n"
    )

    if qubit == "transmon":
        return transmon
    elif qubit == "cos2phi":
        return cos2phi
    elif qubit == "zero_pi":
        return zero_pi
    elif qubit == "fluxonium":
        return fluxonium
    else:
        raise AttributeError("Qubit not available or invalid input.")