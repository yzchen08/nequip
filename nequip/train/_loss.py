import inspect
import logging

import torch.nn
from torch_runstats.scatter import scatter, scatter_mean

from nequip.data import AtomicDataDict, _GRAPH_FIELDS
from nequip.utils import instantiate_from_cls_name


class SimpleLoss:
    """wrapper to compute weighted loss function

    Args:

    func_name (str): any loss function defined in torch.nn that
        takes "reduction=none" as init argument, uses prediction tensor,
        and reference tensor for its call functions, and outputs a vector
        with the same shape as pred/ref
    params (str): arguments needed to initialize the function above

    Return:

    if mean is True, return a scalar; else return the error matrix of each entry
    """

    def __init__(self, func_name: str, params: dict = {}):
        self.ignore_nan = params.get("ignore_nan", False)
        func, _ = instantiate_from_cls_name(
            torch.nn,
            class_name=func_name,
            prefix="",
            positional_args=dict(reduction="none"),
            optional_args=params,
            all_args={},
        )
        self.func_name = func_name
        self.func = func

    def __call__(
        self,
        pred: dict,
        ref: dict,
        key: str,
        mean: bool = True,
    ):
        ref = ref[key]
        # make sure prediction is promoted to dtype of reference
        pred = pred[key].to(ref.dtype)
        # zero the nan entries
        has_nan = self.ignore_nan and torch.isnan(ref.mean())
        if has_nan:
            not_nan = (ref == ref).int()
            loss = self.func(pred, torch.nan_to_num(ref, nan=0.0)) * not_nan
            if mean:
                return loss.sum() / not_nan.sum()
            else:
                return loss
        else:
            loss = self.func(pred, ref)
            if mean:
                return loss.mean()
            else:
                return loss


class PerAtomLoss(SimpleLoss):
    def __call__(
        self,
        pred: dict,
        ref: dict,
        key: str,
        mean: bool = True,
    ):
        if key not in _GRAPH_FIELDS:
            raise RuntimeError(
                f"Doesn't make sense to do a `PerAtom` loss on field `{key}`, which isn't registered as a graph (global) field. If it is a graph-level field, register it with `graph_fields: [\"{key}\"]`; otherwise you don't need to specify `PerAtom` for loss on per-node fields."
            )
        ref_dict = ref
        ref = ref[key]
        # make sure prediction is promoted to dtype of reference
        pred = pred[key].to(ref.dtype)
        # zero the nan entries
        has_nan = self.ignore_nan and torch.isnan(ref.sum())
        N = torch.bincount(ref_dict[AtomicDataDict.BATCH_KEY])
        # as many dimensions of size 1 as there are non-batch dimensions in the data
        N = N.reshape((-1,) + (1,) * (pred.ndim - 1))
        if has_nan:
            not_nan = (ref == ref).int()
            loss = self.func(pred, torch.nan_to_num(ref, nan=0.0)) * not_nan / N
            if self.func_name == "MSELoss":
                loss = loss / N
            assert loss.shape == pred.shape  # [atom, dim]
            if mean:
                return loss.sum() / not_nan.sum()
            else:
                return loss
        else:
            loss = self.func(pred, ref)
            loss = loss / N
            if self.func_name == "MSELoss":
                loss = loss / N
            assert loss.shape == pred.shape  # [atom, dim]
            if mean:
                return loss.mean()
            else:
                return loss


class PerSpeciesLoss(SimpleLoss):
    """Compute loss for each species and average among the same species
    before summing them up.

    Args same as SimpleLoss
    """

    def __call__(
        self,
        pred: dict,
        ref: dict,
        key: str,
        mean: bool = True,
    ):
        if not mean:
            raise NotImplementedError("Cannot handle this yet")
        ref = ref[key]
        # make sure prediction is promoted to dtype of reference
        pred_dict = pred
        pred = pred[key].to(ref.dtype)

        has_nan = self.ignore_nan and torch.isnan(ref.mean())

        if has_nan:
            not_nan = (ref == ref).int()
            per_atom_loss = self.func(pred, torch.nan_to_num(ref, nan=0.0)) * not_nan
        else:
            per_atom_loss = self.func(pred, ref)

        reduce_dims = tuple(i + 1 for i in range(len(per_atom_loss.shape) - 1))

        spe_idx = pred_dict[AtomicDataDict.ATOM_TYPE_KEY].squeeze(-1)
        if has_nan:
            if len(reduce_dims) > 0:
                per_atom_loss = per_atom_loss.sum(dim=reduce_dims)
            assert per_atom_loss.ndim == 1

            per_species_loss = scatter(per_atom_loss, spe_idx, dim=0)

            assert per_species_loss.ndim == 1  # [type]

            N = scatter(not_nan, spe_idx, dim=0)
            N = N.sum(reduce_dims)
            N = N.reciprocal()
            N_species = ((N == N).int()).sum()
            assert N.ndim == 1  # [type]

            per_species_loss = (per_species_loss * N).sum() / N_species

            return per_species_loss

        else:

            if len(reduce_dims) > 0:
                per_atom_loss = per_atom_loss.mean(dim=reduce_dims)
            assert per_atom_loss.ndim == 1

            # offset species index by 1 to use 0 for nan
            _, inverse_species_index = torch.unique(spe_idx, return_inverse=True)

            per_species_loss = scatter_mean(per_atom_loss, inverse_species_index, dim=0)
            assert per_species_loss.ndim == 1  # [type]

            return per_species_loss.mean()


def find_loss_function(name: str, params):
    """
    Search for loss functions in this module

    If the name starts with PerSpecies, return the PerSpeciesLoss instance
    """

    wrapper_list = dict(
        perspecies=PerSpeciesLoss,
        peratom=PerAtomLoss,
    )

    if isinstance(name, str):
        for key in wrapper_list:
            if name.lower().startswith(key):
                logging.debug(f"create loss instance {wrapper_list[key]}")
                return wrapper_list[key](name[len(key) :], params)
        return SimpleLoss(name, params)
    elif inspect.isclass(name):
        return SimpleLoss(name, params)
    elif callable(name):
        return name
    else:
        raise NotImplementedError(f"{name} Loss is not implemented")
