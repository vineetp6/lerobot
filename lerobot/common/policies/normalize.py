import torch
from torch import Tensor, nn


def create_stats_buffers(
    shapes: dict[str, list[int]],
    modes: dict[str, str],
    stats: dict[str, dict[str, Tensor]] | None = None,
) -> dict[str, dict[str, nn.ParameterDict]]:
    """
    Create buffers per modality (e.g. "observation.image", "action") containing their mean, std, min, max
    statistics.

    Args: (see Normalize and Unnormalize)

    Returns:
        dict: A dictionary where keys are modalities and values are `nn.ParameterDict` containing
            `nn.Parameters` set to `requires_grad=False`, suitable to not be updated during backpropagation.
    """
    stats_buffers = {}

    for key, mode in modes.items():
        assert mode in ["mean_std", "min_max"]

        shape = tuple(shapes[key])

        if "image" in key:
            # sanity checks
            assert len(shape) == 3, f"number of dimensions of {key} != 3 ({shape=}"
            c, h, w = shape
            assert c < h and c < w, f"{key} is not channel first ({shape=})"
            # override image shape to be invariant to height and width
            shape = (c, 1, 1)

        # Note: we initialize mean, std, min, max to infinity. They should be overwritten
        # downstream by `stats` or `policy.load_state_dict`, as expected. During forward,
        # we assert they are not infinity anymore.

        buffer = {}
        if mode == "mean_std":
            mean = torch.ones(shape, dtype=torch.float32) * torch.inf
            std = torch.ones(shape, dtype=torch.float32) * torch.inf
            buffer = nn.ParameterDict(
                {
                    "mean": nn.Parameter(mean, requires_grad=False),
                    "std": nn.Parameter(std, requires_grad=False),
                }
            )
        elif mode == "min_max":
            min = torch.ones(shape, dtype=torch.float32) * torch.inf
            max = torch.ones(shape, dtype=torch.float32) * torch.inf
            buffer = nn.ParameterDict(
                {
                    "min": nn.Parameter(min, requires_grad=False),
                    "max": nn.Parameter(max, requires_grad=False),
                }
            )

        if stats is not None:
            if mode == "mean_std":
                buffer["mean"].data = stats[key]["mean"]
                buffer["std"].data = stats[key]["std"]
            elif mode == "min_max":
                buffer["min"].data = stats[key]["min"]
                buffer["max"].data = stats[key]["max"]

        stats_buffers[key] = buffer
    return stats_buffers


class Normalize(nn.Module):
    """Normalizes data (e.g. "observation.image") for more stable and faster convergence during training."""

    def __init__(
        self,
        shapes: dict[str, list[int]],
        modes: dict[str, str],
        stats: dict[str, dict[str, Tensor]] | None = None,
    ):
        """
        Args:
            shapes (dict): A dictionary where keys are input modalities (e.g. "observation.image") and values
            are their shapes (e.g. `[3,96,96]`]). These shapes are used to create the tensor buffer containing
            mean, std, min, max statistics. If the provided `shapes` contain keys related to images, the shape
            is adjusted to be invariant to height and width, assuming a channel-first (c, h, w) format.
            modes (dict): A dictionary where keys are output modalities (e.g. "observation.image") and values
                are their normalization modes among:
                    - "mean_std": subtract the mean and divide by standard deviation.
                    - "min_max": map to [-1, 1] range.
            stats (dict, optional): A dictionary where keys are output modalities (e.g. "observation.image")
                and values are dictionaries of statistic types and their values (e.g.
                `{"mean": torch.randn(3,1,1)}, "std": torch.randn(3,1,1)}`). If provided, as expected for
                training the model for the first time, these statistics will overwrite the default buffers. If
                not provided, as expected for finetuning or evaluation, the default buffers should to be
                overwritten by a call to `policy.load_state_dict(state_dict)`. That way, initializing the
                dataset is not needed to get the stats, since they are already in the policy state_dict.
        """
        super().__init__()
        self.shapes = shapes
        self.modes = modes
        self.stats = stats
        # `self.buffer_observation_state["mean"]` contains `torch.tensor(state_dim)`
        stats_buffers = create_stats_buffers(shapes, modes, stats)
        for key, buffer in stats_buffers.items():
            setattr(self, "buffer_" + key.replace(".", "_"), buffer)

    # TODO(rcadene): should we remove torch.no_grad?
    @torch.no_grad
    def forward(self, batch: dict[str, Tensor]) -> dict[str, Tensor]:
        for key, mode in self.modes.items():
            buffer = getattr(self, "buffer_" + key.replace(".", "_"))

            if mode == "mean_std":
                mean = buffer["mean"]
                std = buffer["std"]
                assert not torch.isinf(mean).any(), (
                    "`mean` is infinity. You forgot to initialize with `stats` as argument, or called "
                    "`policy.load_state_dict`."
                )
                assert not torch.isinf(std).any(), (
                    "`std` is infinity. You forgot to initialize with `stats` as argument, or called "
                    "`policy.load_state_dict`."
                )
                batch[key] = (batch[key] - mean) / (std + 1e-8)
            elif mode == "min_max":
                min = buffer["min"]
                max = buffer["max"]
                assert not torch.isinf(min).any(), (
                    "`min` is infinity. You forgot to initialize with `stats` as argument, or called "
                    "`policy.load_state_dict`."
                )
                assert not torch.isinf(max).any(), (
                    "`max` is infinity. You forgot to initialize with `stats` as argument, or called "
                    "`policy.load_state_dict`."
                )
                # normalize to [0,1]
                batch[key] = (batch[key] - min) / (max - min)
                # normalize to [-1, 1]
                batch[key] = batch[key] * 2 - 1
            else:
                raise ValueError(mode)
        return batch


class Unnormalize(nn.Module):
    """
    Similar to `Normalize` but unnormalizes output data (e.g. `{"action": torch.randn(b,c)}`) in their
    original range used by the environment.
    """

    def __init__(
        self,
        shapes: dict[str, list[int]],
        modes: dict[str, str],
        stats: dict[str, dict[str, Tensor]] | None = None,
    ):
        """
        Args:
            shapes (dict): A dictionary where keys are input modalities (e.g. "observation.image") and values
            are their shapes (e.g. `[3,96,96]`]). These shapes are used to create the tensor buffer containing
            mean, std, min, max statistics. If the provided `shapes` contain keys related to images, the shape
            is adjusted to be invariant to height and width, assuming a channel-first (c, h, w) format.
            modes (dict): A dictionary where keys are output modalities (e.g. "observation.image") and values
                are their normalization modes among:
                    - "mean_std": subtract the mean and divide by standard deviation.
                    - "min_max": map to [-1, 1] range.
            stats (dict, optional): A dictionary where keys are output modalities (e.g. "observation.image")
                and values are dictionaries of statistic types and their values (e.g.
                `{"mean": torch.randn(3,1,1)}, "std": torch.randn(3,1,1)}`). If provided, as expected for
                training the model for the first time, these statistics will overwrite the default buffers. If
                not provided, as expected for finetuning or evaluation, the default buffers should to be
                overwritten by a call to `policy.load_state_dict(state_dict)`. That way, initializing the
                dataset is not needed to get the stats, since they are already in the policy state_dict.
        """
        super().__init__()
        self.shapes = shapes
        self.modes = modes
        self.stats = stats
        # `self.buffer_observation_state["mean"]` contains `torch.tensor(state_dim)`
        stats_buffers = create_stats_buffers(shapes, modes, stats)
        for key, buffer in stats_buffers.items():
            setattr(self, "buffer_" + key.replace(".", "_"), buffer)

    # TODO(rcadene): should we remove torch.no_grad?
    @torch.no_grad
    def forward(self, batch: dict[str, Tensor]) -> dict[str, Tensor]:
        for key, mode in self.modes.items():
            buffer = getattr(self, "buffer_" + key.replace(".", "_"))

            if mode == "mean_std":
                mean = buffer["mean"]
                std = buffer["std"]
                assert not torch.isinf(mean).any(), (
                    "`mean` is infinity. You forgot to initialize with `stats` as argument, or called "
                    "`policy.load_state_dict`."
                )
                assert not torch.isinf(std).any(), (
                    "`std` is infinity. You forgot to initialize with `stats` as argument, or called "
                    "`policy.load_state_dict`."
                )
                batch[key] = batch[key] * std + mean
            elif mode == "min_max":
                min = buffer["min"]
                max = buffer["max"]
                assert not torch.isinf(min).any(), (
                    "`min` is infinity. You forgot to initialize with `stats` as argument, or called "
                    "`policy.load_state_dict`."
                )
                assert not torch.isinf(max).any(), (
                    "`max` is infinity. You forgot to initialize with `stats` as argument, or called "
                    "`policy.load_state_dict`."
                )
                batch[key] = (batch[key] + 1) / 2
                batch[key] = batch[key] * (max - min) + min
            else:
                raise ValueError(mode)
        return batch