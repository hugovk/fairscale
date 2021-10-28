# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import logging
from collections import defaultdict, abc
from enum import Enum
import warnings
from typing import Any, Dict, List, Optional, Tuple
import remote_pdb
import torch
from torch.cuda.amp import GradScaler as TorchGradScaler
import torch.distributed as dist
from torch.optim import Optimizer
from torch.cuda.amp.common import amp_definitely_not_available

from .oss import OSS


class _GeneralMultiDeviceReplicator(object):
    """
    Lazily serves copies of a tensor to requested devices.  Copies are cached per-device.
    """
    def __init__(self, master_tensor: torch.Tensor) -> None:
        assert master_tensor.is_cuda or master_tensor.device.type == "xla" or master_tensor.device.type == "cpu"
        self.master = master_tensor
        self._per_device_tensors: Dict[torch.device, torch.Tensor] = {}

    def get(self, device) -> torch.Tensor:
        retval = self._per_device_tensors.get(device, None)
        if retval is None:
            retval = self.master.to(device=device, non_blocking=True, copy=True)
            self._per_device_tensors[device] = retval
        return retval


# Defines default_factory for GradScaler's _per_optimizer_states defaultdict,
# as well as associated "enum" values.  Prefers defining these at top level because
# - Lambdas can't be pickled, so we don't want to supply a lambda as the factory.
# - Defining READY, UNSCALED, STEPPED and _refresh_per_optimizer_state within GradScaler
#   causes a circular reference, which we'd rather avoid.
class OptState(Enum):
    READY = 0
    UNSCALED = 1
    STEPPED = 2


def _refresh_per_optimizer_state():
    return {"stage": OptState.READY, "found_inf_per_device": {}}


# class GradScaler(TorchGradScaler):
#     def _unscale_grads_(
#         self, optimizer: Optimizer, inv_scale: torch.Tensor, found_inf: torch.Tensor, allow_fp16: bool
#     ) -> Dict[torch.device, torch.Tensor]:
#         return super()._unscale_grads_(optimizer, inv_scale, found_inf, True)


class ShardedGradScaler(TorchGradScaler):
    """
    A shard-aware :class:`GradScaler<torch.cuda.amp.GradScaler>`, to be used in conjunction with
    :class:`OSS` and :class:`ShardedOptimizer`.

    Interface and usecases are not changed, more explanations can be found in the corresponding pytorch
    documentation https://pytorch.org/docs/stable/amp.html#torch.cuda.amp.GradScaler
    """

    _scale: Optional[torch.Tensor]
    _grows_tracker: Optional[torch.Tensor]
    _per_optimizer_states: Dict[int, Dict[str, Any]]

    def __init__(
        self,
        init_scale=2.0 ** 16,
        growth_factor=2.0,
        backoff_factor=0.5,
        growth_interval=2000,
        enabled=True,
        process_group: Any = dist.group.WORLD,
    ):
        if enabled and amp_definitely_not_available():
            warnings.warn("torch.cuda.amp.GradScaler is enabled, but CUDA is not available.  Disabling.")
            self._enabled = False
        else:
            self._enabled = enabled

        if self._enabled:
            assert growth_factor > 1.0, "The growth factor must be > 1.0."
            assert backoff_factor < 1.0, "The backoff factor must be < 1.0."

            self._init_scale = init_scale
            # self._scale will be lazily initialized during the first call to scale()
            self._scale = None
            self._growth_factor = growth_factor
            self._backoff_factor = backoff_factor
            self._growth_interval = growth_interval
            self._init_growth_tracker = 0
            # self._growth_tracker will be lazily initialized during the first call to scale()
            self._growth_tracker = None
            self._per_optimizer_states = defaultdict(_refresh_per_optimizer_state)
            self.display_warning = True
            self.group = process_group

    def _check_scale_growth_tracker(self, funcname) -> Tuple[torch.Tensor, torch.Tensor]:
        fix = "This may indicate your script did not use scaler.scale(loss or outputs) earlier in the iteration."
        assert self._scale is not None, "Attempted {} but _scale is None.  ".format(funcname) + fix
        assert self._growth_tracker is not None, "Attempted {} but _growth_tracker is None.  ".format(funcname) + fix
        return (self._scale, self._growth_tracker)

    def _lazy_init_scale_growth_tracker(self, dev):
        assert self._growth_tracker is None, "_growth_tracker initialized before _scale"
        self._scale = torch.full((1,), self._init_scale, dtype=torch.float32, device=dev)
        self._growth_tracker = torch.full((1,), self._init_growth_tracker, dtype=torch.int32, device=dev)

    def scale(self, outputs):
        """
        Multiplies ('scales') a tensor or list of tensors by the scale factor.

        Returns scaled outputs.  If this instance of :class:`GradScaler` is not enabled, outputs are returned
        unmodified.

        Args:
            outputs (Tensor or iterable of Tensors):  Outputs to scale.
        """
        if not self._enabled:
            return outputs

        # Short-circuit for the common case.
        if isinstance(outputs, torch.Tensor):
            assert outputs.is_cuda or outputs.device.type == "xla" or outputs.device.type == "cpu"
            if self._scale is None:
                self._lazy_init_scale_growth_tracker(outputs.device)
            assert self._scale is not None
            return outputs * self._scale.to(device=outputs.device, non_blocking=True)

        # Invoke the more complex machinery only if we're treating multiple outputs.
        stash: List[_GeneralMultiDeviceReplicator] = []  # holds a reference that can be overwritten by apply_scale

        def apply_scale(val):
            if isinstance(val, torch.Tensor):
                assert val.is_cuda or val.device.type == "xla" or val.device.type == "cpu"
                if len(stash) == 0:
                    if self._scale is None:
                        self._lazy_init_scale_growth_tracker(val.device)
                    assert self._scale is not None
                    stash.append(_GeneralMultiDeviceReplicator(self._scale))
                return val * stash[0].get(val.device)
            elif isinstance(val, abc.Iterable):
                iterable = map(apply_scale, val)
                if isinstance(val, list) or isinstance(val, tuple):
                    return type(val)(iterable)
                else:
                    return iterable
            else:
                raise ValueError("outputs must be a Tensor or an iterable of Tensors")

        return apply_scale(outputs)

    def _foreach_non_finite_check_and_unscale_cpu_(self, grads, found_inf, inv_scale):
        if len(grads) == 0:
            return
        assert inv_scale.numel() == 1, "inv_scale must be a 1-element tensor."
        assert found_inf.numel() == 1, "found_inf must be a 1-element tensor."

        expected_device = grads[0].device
        # expected_dtype = type(grads[0])
        for tensor in grads[0]:
            assert tensor.device == expected_device, "grads must be on the same device"

            # check for non_overlapping_and_dense doesn't exist in the python world
            # as remarked here https://github.com/pytorch/pytorch/blob/master/aten/src/ATen/native/cuda/AmpKernels.cu#L108
            # we assume tensor is not MTA safe. iterate through each item regardless of dtype
            # if type(tensor) is not expected_dtype:
            if torch.isinf(tensor).any().item() is True or torch.isnan(tensor).any().item() is True:
                found_inf.data = torch.tensor([1.0])
                break
            else:
                tensor.data *= inv_scale.item()

    def _unscale_grads_(self, optimizer, inv_scale, found_inf, allow_fp16=True):
        per_device_inv_scale = _GeneralMultiDeviceReplicator(inv_scale)
        per_device_found_inf = _GeneralMultiDeviceReplicator(found_inf)

        # To set up _amp_foreach_non_finite_check_and_unscale_, split grads by device and dtype.
        # There could be hundreds of grads, so we'd like to iterate through them just once.
        # However, we don't know their devices or dtypes in advance.

        # https://stackoverflow.com/questions/5029934/defaultdict-of-defaultdict
        # Google says mypy struggles with defaultdicts type annotations.
        per_device_and_dtype_grads = defaultdict(lambda: defaultdict(list))  # type: ignore[var-annotated]
        with torch.no_grad():
            for group in optimizer.param_groups:
                for param in group["params"]:
                    if param.grad is None:
                        continue
                    if (not allow_fp16) and param.grad.dtype == torch.float16:
                        raise ValueError("Attempting to unscale FP16 gradients.")
                    if param.grad.is_sparse:
                        # is_coalesced() == False means the sparse grad has values with duplicate indices.
                        # coalesce() deduplicates indices and adds all values that have the same index.
                        # For scaled fp16 values, there's a good chance coalescing will cause overflow,
                        # so we should check the coalesced _values().
                        if param.grad.dtype is torch.float16:
                            param.grad = param.grad.coalesce()
                        to_unscale = param.grad._values()
                    else:
                        to_unscale = param.grad

                    # TODO: is there a way to split by device and dtype without appending in the inner loop?
                    per_device_and_dtype_grads[to_unscale.device][to_unscale.dtype].append(to_unscale)

            for device, per_dtype_grads in per_device_and_dtype_grads.items():
                for grads in per_dtype_grads.values():
                    if "cpu" in str(grads[0].device):
                        self._foreach_non_finite_check_and_unscale_cpu_(
                            grads, per_device_found_inf.get(device), per_device_inv_scale.get(device)
                        )
                    else:
                        torch._amp_foreach_non_finite_check_and_unscale_(
                            grads, per_device_found_inf.get(device), per_device_inv_scale.get(device)
                        )

        return per_device_found_inf._per_device_tensors

    def unscale_(self, optimizer: Optimizer) -> None:
        # Could be a mistake, this scaler is supposed to work with ZeroRedundancyOptimizer only
        if self.display_warning and not isinstance(optimizer, OSS):
            logging.warning(
                "ShardedGradScaler is to be used in combination with a sharded optimizer, this could not be checked"
            )

        self.display_warning = False  # Only warn once

        # Call the upstream unscale_ method which will only act on this rank's gradients
        super().unscale_(optimizer)

        # Synchronize the detected inf across the ranks
        optimizer_state = self._per_optimizer_states[id(optimizer)]
        last_handle = None

        for v in optimizer_state["found_inf_per_device"].values():
            if v.device.type == 'cpu':
                v_on_cuda = v.cuda()
                last_handle = dist.all_reduce(v_on_cuda, async_op=True, group=self.group)
                v_on_cuda.cpu()
            else:
                last_handle = dist.all_reduce(v, async_op=True, group=self.group)

        # Make sure that the calls are done before moving out.
        # The calls are executed in sequence, waiting for the last one is enough
        if last_handle is not None:
            last_handle.wait()

    def _maybe_opt_step(self, optimizer, optimizer_state, *args, **kwargs):
        retval = None
        if not sum(v.item() for v in optimizer_state["found_inf_per_device"].values()):
            retval = optimizer.step(*args, **kwargs)
        return retval

    def step(self, optimizer, *args, **kwargs):
        """
        :meth:`step` carries out the following two operations:

        1.  Internally invokes ``unscale_(optimizer)`` (unless :meth:`unscale_` was explicitly called for ``optimizer``
            earlier in the iteration).  As part of the :meth:`unscale_`, gradients are checked for infs/NaNs.
        2.  If no inf/NaN gradients are found, invokes ``optimizer.step()`` using the unscaled
            gradients.  Otherwise, ``optimizer.step()`` is skipped to avoid corrupting the params.

        ``*args`` and ``**kwargs`` are forwarded to ``optimizer.step()``.

        Returns the return value of ``optimizer.step(*args, **kwargs)``.

        Args:
            optimizer (torch.optim.Optimizer):  Optimizer that applies the gradients.
            args:  Any arguments.
            kwargs:  Any keyword arguments.

        .. warning::
            Closure use is not currently supported.
        """
        if not self._enabled:
            return optimizer.step(*args, **kwargs)

        if "closure" in kwargs:
            raise RuntimeError("Closure use is not currently supported if GradScaler is enabled.")

        self._check_scale_growth_tracker("step")

        optimizer_state = self._per_optimizer_states[id(optimizer)]

        if optimizer_state["stage"] is OptState.STEPPED:
            raise RuntimeError("step() has already been called since the last update().")

        retval = None

        if hasattr(optimizer, "_step_supports_amp_scaling") and optimizer._step_supports_amp_scaling:
            # This optimizer has customized scale-handling logic, so we can call optimizer.step() directly.
            # The contract with custom optimizers is that their step() should accept an additional,
            # optional grad_scaler kwarg.  We append self to the kwargs so the custom optimizer has full information:
            # it can query its own state, invoke unscale_ on itself, etc
            retval = optimizer.step(*args, **dict(kwargs, grad_scaler=self))
            optimizer_state["stage"] = OptState.STEPPED
            return retval

        if optimizer_state["stage"] is OptState.READY:
            self.unscale_(optimizer)

        assert len(optimizer_state["found_inf_per_device"]) > 0, "No inf checks were recorded for this optimizer."
        retval = self._maybe_opt_step(optimizer, optimizer_state, *args, **kwargs)
        optimizer_state["stage"] = OptState.STEPPED
        return retval

    def _amp_update_scale_cpu_(self, found_inf):
        if found_inf == float("inf") or found_inf == -float("inf"):
            self._scale *= self._backoff_factor
            self._growth_tracker = 0
        else:
            successful = self._growth_tracker + 1
            if successful == self._growth_interval:
                self._scale *= self._growth_factor
                self._growth_tracker = 0
            else:
                self._growth_tracker = successful

    def update(self, new_scale=None):
        """
        Updates the scale factor.

        If any optimizer steps were skipped the scale is multiplied by ``backoff_factor``
        to reduce it. If ``growth_interval`` unskipped iterations occurred consecutively,
        the scale is multiplied by ``growth_factor`` to increase it.

        Passing ``new_scale`` sets the new scale value manually. (``new_scale`` is not
        used directly, it's used to fill GradScaler's internal scale tensor. So if
        ``new_scale`` was a tensor, later in-place changes to that tensor will not further
        affect the scale GradScaler uses internally.)

        Args:
            new_scale (float or :class:`torch.cuda.FloatTensor`, optional, default=None):  New scale factor.

        .. warning::
            :meth:`update` should only be called at the end of the iteration, after ``scaler.step(optimizer)`` has
            been invoked for all optimizers used this iteration.
        """

        if not self._enabled:
            return

        _scale, _growth_tracker = self._check_scale_growth_tracker("update")

        if new_scale is not None:
            # Accept a new user-defined scale.
            if isinstance(new_scale, float):
                self._scale.fill_(new_scale)  # type: ignore[union-attr]
            else:
                reason = "new_scale should be a float or a 1-element torch.cuda.FloatTensor with requires_grad=False."
                assert isinstance(new_scale, torch.cuda.FloatTensor), reason  # type: ignore[attr-defined]
                assert new_scale.numel() == 1, reason
                assert new_scale.requires_grad is False, reason
                self._scale.copy_(new_scale)  # type: ignore[union-attr]
        else:
            # Consume shared inf/nan data collected from optimizers to update the scale.
            # If all found_inf tensors are on the same device as self._scale, this operation is asynchronous.
            found_infs = [
                found_inf.to(device=_scale.device, non_blocking=True)
                for state in self._per_optimizer_states.values()
                for found_inf in state["found_inf_per_device"].values()
            ]

            assert len(found_infs) > 0, "No inf checks were recorded prior to update."

            found_inf_combined = found_infs[0]
            if len(found_infs) > 1:
                for i in range(1, len(found_infs)):
                    found_inf_combined += found_infs[i]

            if _scale.device.type == "cpu":
                print("using cpu")
                self._amp_update_scale_cpu_(found_inf_combined)
            else:
                print("using gpu")
                torch._amp_update_scale_(
                    self._scale,
                    self._growth_tracker,
                    found_inf_combined,
                    self._growth_factor,
                    self._backoff_factor,
                    self._growth_interval,
                )

        # To prepare for next iteration, clear the data collected from optimizers this iteration.
        self._per_optimizer_states = defaultdict(_refresh_per_optimizer_state)

    def _get_scale_async(self):
        return self._scale

    def get_scale(self):
        """
        Returns a Python float containing the current scale, or 1.0 if scaling is disabled.

        .. warning::
            :meth:`get_scale` incurs a CPU-GPU sync.
        """
        if self._enabled:
            return self._init_scale if self._scale is None else self._get_scale_async().item()
        else:
            return 1.0

    def get_growth_factor(self):
        r"""
        Returns a Python float containing the scale growth factor.
        """
        return self._growth_factor

    def set_growth_factor(self, new_factor):
        r"""
        Args:
            new_scale (float):  Value to use as the new scale growth factor.
        """
        self._growth_factor = new_factor

    def get_backoff_factor(self):
        r"""
        Returns a Python float containing the scale backoff factor.
        """
        return self._backoff_factor

    def set_backoff_factor(self, new_factor):
        r"""
        Args:
            new_scale (float):  Value to use as the new scale backoff factor.
        """
        self._backoff_factor = new_factor

    def get_growth_interval(self):
        r"""
        Returns a Python int containing the growth interval.
        """
        return self._growth_interval

    def set_growth_interval(self, new_interval):
        r"""
        Args:
            new_interval (int):  Value to use as the new growth interval.
        """
        self._growth_interval = new_interval

    def _get_growth_tracker(self):
        if self._enabled:
            return self._init_growth_tracker if self._growth_tracker is None else self._growth_tracker.item()
        else:
            return 0

    def is_enabled(self):
        r"""
        Returns a bool indicating whether this instance is enabled.
        """
        return self._enabled

    def state_dict(self):
        r"""
        Returns the state of the scaler as a :class:`dict`.  It contains five entries:

        * ``"scale"`` - a Python float containing the current scale
        * ``"growth_factor"`` - a Python float containing the current growth factor
        * ``"backoff_factor"`` - a Python float containing the current backoff factor
        * ``"growth_interval"`` - a Python int containing the current growth interval
        * ``"_growth_tracker"`` - a Python int containing the number of recent consecutive unskipped steps.

        If this instance is not enabled, returns an empty dict.

        .. note::
           If you wish to checkpoint the scaler's state after a particular iteration, :meth:`state_dict`
           should be called after :meth:`update`.
        """
        return (
            {
                "scale": self.get_scale(),
                "growth_factor": self._growth_factor,
                "backoff_factor": self._backoff_factor,
                "growth_interval": self._growth_interval,
                "_growth_tracker": self._get_growth_tracker(),
            }
            if self._enabled
            else {}
        )

    def load_state_dict(self, state_dict):
        r"""
        Loads the scaler state.  If this instance is disabled, :meth:`load_state_dict` is a no-op.

        Args:
           state_dict(dict): scaler state.  Should be an object returned from a call to :meth:`state_dict`.
        """
        if not self._enabled:
            return

        if len(state_dict) == 0:
            raise RuntimeError(
                "The source state dict is empty, possibly because it was saved "
                "from a disabled instance of GradScaler."
            )

        self._init_scale = state_dict["scale"]
        if self._scale is not None:
            self._scale.fill_(state_dict["scale"])
        self._growth_factor = state_dict["growth_factor"]
        self._backoff_factor = state_dict["backoff_factor"]
        self._growth_interval = state_dict["growth_interval"]
        self._init_growth_tracker = state_dict["_growth_tracker"]
        if self._growth_tracker is not None:
            self._growth_tracker.fill_(state_dict["_growth_tracker"])

    def __getstate__(self):
        state = self.__dict__.copy()
        if self._enabled:
            assert len(self._per_optimizer_states) == 0, (
                "A GradScaler instance may only be pickled at the beginning "
                "of an iteration, or at the end after scaler.update()."
            )
            # Pickling _scale and _growth_tracker Tensors directly triggers
            # "warnings.warn("pickle support for Storage will be removed in 1.5..."
            # so instead, we set the unpickled instance up to reinitialize them lazily.
            state["_init_scale"] = self.get_scale()
            state["_init_growth_tracker"] = self._get_growth_tracker()
            state["_scale"] = None
            state["_growth_tracker"] = None
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)

    def _check_inf_per_device(self, optimizer):
        _scale, _ = self._check_scale_growth_tracker("_check_inf_per_device")

        dummy_inv_scale = torch.full((1,), 1.0, dtype=torch.float32, device=_scale.device)
        found_inf = torch.full((1,), 0.0, dtype=torch.float32, device=_scale.device)

        self._per_optimizer_states[id(optimizer)]["found_inf_per_device"] = self._unscale_grads_(
            optimizer, dummy_inv_scale, found_inf, True
        )

        return self._per_optimizer_states[id(optimizer)]["found_inf_per_device"]

    def _found_inf_per_device(self, optimizer):
        return self._per_optimizer_states[id(optimizer)]["found_inf_per_device"]
