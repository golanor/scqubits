# spec_lookup.py
#
# This file is part of scqubits.
#
#    Copyright (c) 2019, Jens Koch and Peter Groszkowski
#    All rights reserved.
#
#    This source code is licensed under the BSD-style license found in the
#    LICENSE file in the root directory of this source tree.
############################################################################

import itertools
import warnings
import weakref
from functools import wraps
from typing import Callable, List, Optional, Tuple, Union, TYPE_CHECKING

import numpy as np
import qutip as qt

import scqubits
import scqubits.io_utils.fileio_serializers as serializers
import scqubits.utils.spectrum_utils as spec_utils

from numpy import ndarray
from qutip import Qobj

if TYPE_CHECKING:
    from scqubits.io_utils.fileio_qutip import QutipEigenstates
    from scqubits.core.qubit_base import QuantumSystem
    from scqubits import ParameterSweep, HilbertSpace, SpectrumData


def check_sync_status(func: Callable) -> Callable:
    @wraps(func)
    def wrapper(self, *args, **kwargs):
        if self._out_of_sync:
            warnings.warn("SCQUBITS\nSpectrum lookup data is out of sync with systems originally involved in generating"
                          " it. This will generally lead to incorrect results. Consider regenerating the lookup data "
                          "using <HilbertSpace>.generate_lookup() or <ParameterSweep>.run()", Warning)
        return func(self, *args, **kwargs)
    return wrapper


class SpectrumLookup(serializers.Serializable):
    """
    The `SpectrumLookup` is an integral building block of the `HilbertSpace` and `ParameterSweep` classes. In both cases
    it provides a convenient way to translate back and forth between labelling of eigenstates and eigenenergies via the
    indices of the dressed spectrum j = 0, 1, 2, ... on one hand, and the bare product-state labels of the form
    (0,0,0), (0,0,1), (0,1,0),... (here for the example of three subsys_list). The lookup table stored in a
    `SpectrumLookup` instance should be generated by calling `<HilbertSpace>.generate_lookup()` in the case of a
    `HilbertSpace` object. For `ParameterSweep` objects, the lookup table is generated automatically upon init, or
    manually via `<ParameterSweep>.run()`.

    Parameters
    ----------
    framework:
    dressed_specdata:
        dressed spectral data needed for generating the lookup mapping
    bare_specdata_list:
        bare spectral data needed for generating the lookup mapping
    """
    def __init__(self,
                 framework: Union['ParameterSweep', 'HilbertSpace'],
                 dressed_specdata: 'SpectrumData',
                 bare_specdata_list: List['SpectrumData']
                 ) -> None:
        self._dressed_specdata = dressed_specdata
        self._bare_specdata_list = bare_specdata_list
        # Store ParameterSweep and/or HilbertSpace objects only as weakref.proxy objects to avoid circular references
        # that would prevent objects from expiring appropriately and being garbage collected
        if isinstance(framework, scqubits.ParameterSweep):
            self._sweep = weakref.proxy(framework)
            self._hilbertspace = weakref.proxy(self._sweep._hilbertspace)
        elif isinstance(framework, scqubits.HilbertSpace):
            self._sweep = None
            self._hilbertspace = weakref.proxy(framework)
        else:
            raise TypeError

        self._canonical_bare_labels = self._generate_bare_labels()
        self._dressed_indices = self._generate_mappings()  # lists of as many elements as there are parameter values.
        # For HilbertSpace objects the above is a single-element list.
        self._out_of_sync = False
        # Setup for Serializable operations
        self._init_params = ['_dressed_specdata', '_bare_specdata_list']

    def _generate_bare_labels(self) -> List[Tuple[int, ...]]:
        """
        Generates the list of bare-state labels in canonical order. For example, for a Hilbert space composed of two
        subsys_list sys1 and sys2, each label is of the type (3,0) meaning sys1 is in bare eigenstate 3, sys2 in bare
        eigenstate 0. The full list the reads
        [(0,0), (0,1), (0,2), ..., (0,max_2),
         (1,0), (1,1), (1,2), ..., (1,max_2),
         ...
         (max_1,0), (max_1,1), (max_1,2), ..., (max_1,max_2)]
        """
        dim_list = self._hilbertspace.subsystem_dims
        subsys_count = self._hilbertspace.subsystem_count

        basis_label_ranges = []
        for subsys_index in range(subsys_count):
            basis_label_ranges.append(range(dim_list[subsys_index]))

        basis_labels_list = list(itertools.product(*basis_label_ranges))   # generate list of bare basis states (tuples)
        return basis_labels_list

    def _generate_mappings(self) -> List[List[int]]:
        """
        For each parameter value of the parameter sweep (may only be one if called from HilbertSpace, so no sweep),
        generate the map between bare states and dressed states.

        Returns
        -------
            each list item is a list of dressed indices whose order corresponds to the ordering of bare indices (as
            stored in .canonical_bare_labels, thus establishing the mapping
        """
        param_indices = range(self._dressed_specdata.param_count)
        dressed_indices_list = []
        for index in param_indices:
            dressed_indices = self._generate_single_mapping(index)
            dressed_indices_list.append(dressed_indices)
        return dressed_indices_list

    def _generate_single_mapping(self, param_index: int) -> List[int]:
        """
        For a single parameter value with index `param_index`, create a list of the dressed-state indices in an order
        that corresponds one to one to the canonical bare-state product states with largest overlap (whenever possible).

        Parameters
        ----------
        param_index:
            index of the parameter value

        Returns
        -------
            dressed-state indices
        """
        overlap_matrix = spec_utils.convert_esys_to_ndarray(self._dressed_specdata.state_table[param_index])

        dressed_indices: List[int] = []
        for bare_basis_index in range(self._hilbertspace.dimension):   # for given bare basis index, find dressed index
            max_position = (np.abs(overlap_matrix[:, bare_basis_index])).argmax()
            max_overlap = np.abs(overlap_matrix[max_position, bare_basis_index])
            if max_overlap < 0.5:     # overlap too low, make no assignment
                dressed_indices.append(None)
            else:
                dressed_indices.append(max_position)
        return dressed_indices

    @check_sync_status
    def dressed_index(self, bare_labels: Tuple[int, ...], param_index: int = 0) -> Union[int, None]:
        """
        For given bare product state return the corresponding dressed-state index.

        Parameters
        ----------
        bare_labels:
            bare_labels = (index, index2, ...)
        param_index:
            index of parameter value of interest

        Returns
        -------
            dressed state index closest to the specified bare state
        """
        try:
            lookup_position = self._canonical_bare_labels.index(bare_labels)
        except ValueError:
            return None
        return self._dressed_indices[param_index][lookup_position]

    @check_sync_status
    def bare_index(self, dressed_index: int, param_index: int = 0) -> Union[Tuple[int, ...], None]:
        """
        For given dressed index, look up the corresponding bare index.

        Returns
        -------
            Bare state specification in tuple form. Example: (1,0,3) means subsystem 1 is in bare state 1, subsystem 2
            in bare state 0, and subsystem 3 in bare state 3.
        """
        try:
            lookup_position = self._dressed_indices[param_index].index(dressed_index)
        except ValueError:
            return None
        basis_labels = self._canonical_bare_labels[lookup_position]
        return basis_labels

    @check_sync_status
    def dressed_eigenstates(self, param_index: int = 0) -> List['QutipEigenstates']:
        """
        Return the list of dressed eigenvectors

        Parameters
        ----------
        param_index:
            position index of parameter value in question, if called from within `ParameterSweep`

        Returns
        -------
            dressed eigenvectors for the external parameter fixed to the value indicated by the provided index
        """
        return self._dressed_specdata.state_table[param_index]

    @check_sync_status
    def dressed_eigenenergies(self, param_index: int = 0) -> ndarray:
        """
        Return the array of dressed eigenenergies

        Parameters
        ----------
            position index of parameter value in question

        Returns
        -------
            dressed eigenenergies for the external parameter fixed to the value indicated by the provided index
        """
        return self._dressed_specdata.energy_table[param_index]

    @check_sync_status
    def energy_bare_index(self, bare_tuple: Tuple[int, ...], param_index: int = 0) -> Union[float, None]:
        """
        Look up dressed energy most closely corresponding to the given bare-state labels

        Parameters
        ----------
        bare_tuple:
            bare state indices
        param_index:
            index specifying the position in the self.param_vals array

        Returns
        -------
            dressed energy, if lookup successful
        """
        dressed_index = self.dressed_index(bare_tuple, param_index)
        if dressed_index is None:
            return None
        return self._dressed_specdata.energy_table[param_index][dressed_index]

    @check_sync_status
    def energy_dressed_index(self, dressed_index: int, param_index: int = 0) -> float:
        """
        Look up the dressed eigenenergy belonging to the given dressed index.

        Parameters
        ----------
        dressed_index:
            index of dressed state of interest
        param_index:
            relevant if used in the context of a ParameterSweep

        Returns
        -------
            dressed energy
        """
        return self._dressed_specdata.energy_table[param_index][dressed_index]

    @check_sync_status
    def bare_eigenstates(self, subsys: 'QuantumSystem', param_index: int = 0) -> ndarray:
        """
        Return ndarray of bare eigenstates for given subsystem and parameter index.
        Eigenstates are expressed in the basis internal to the subsystem.
        """
        framework = self._sweep or self._hilbertspace
        subsys_index = framework.get_subsys_index(subsys)
        return self._bare_specdata_list[subsys_index].state_table[param_index]

    @check_sync_status
    def bare_eigenenergies(self, subsys: 'QuantumSystem', param_index: int = 0) -> ndarray:
        """
        Return list of bare eigenenergies for given subsystem.

        Parameters
        ----------
        subsys:
            Hilbert space subsystem for which bare eigendata is to be looked up
        param_index:
            position index of parameter value in question

        Returns
        -------
            bare eigenenergies for the specified subsystem and the external parameter fixed to the value indicated by
            its index
        """
        subsys_index = self._hilbertspace.index(subsys)
        return self._bare_specdata_list[subsys_index].energy_table[param_index]

    def bare_productstate(self, bare_index: Tuple[int, ...]) -> Qobj:
        """
        Return the bare product state specified by `bare_index`.

        Parameters
        ----------
        bare_index:

        Returns
        -------
            ket in full Hilbert space
        """
        subsys_dims = self._hilbertspace.subsystem_dims
        product_state_list = []
        for subsys_index, state_index in enumerate(bare_index):
            dim = subsys_dims[subsys_index]
            product_state_list.append(qt.basis(dim, state_index))
        return qt.tensor(*product_state_list)
