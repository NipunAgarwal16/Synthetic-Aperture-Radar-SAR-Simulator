import logging
from typing import Any, Dict, Optional, Tuple

import flox.xarray
import numpy as np
import xarray as xr

from . import geocoding, scene

logger = logging.getLogger(__name__)

ONE_SECOND = np.timedelta64(1, "s")


def sum_weights(
    initial_weights: xr.DataArray,
    azimuth_index: xr.DataArray,
    slant_range_index: xr.DataArray,
    multilook: Optional[Tuple[int, int]] = None,
) -> xr.DataArray:
    geocoded = initial_weights.assign_coords(
        slant_range_index=slant_range_index, azimuth_index=azimuth_index
    )

    flat_sum: xr.DataArray = flox.xarray.xarray_reduce(
        geocoded,
        geocoded.slant_range_index,
        geocoded.azimuth_index,
        func="sum",
        method="map-reduce",
    )

    if multilook:
        flat_sum = flat_sum.rolling(
            azimuth_index=multilook[0],
            slant_range_index=multilook[1],
            center=True,
            min_periods=multilook[0] * multilook[1] // 2 + 1,
        ).mean()

    weights_sum = flat_sum.interp(
        slant_range_index=slant_range_index,
        azimuth_index=azimuth_index,
        method="nearest",
    )

    return weights_sum


def compute_gamma_area(
    dem_ecef: xr.DataArray,
    dem_direction: xr.DataArray,
) -> xr.DataArray:
    dem_oriented_area = scene.compute_dem_oriented_area(dem_ecef)
    gamma_area: xr.DataArray = xr.dot(dem_oriented_area, -dem_direction, dims="axis")  # type: ignore
    gamma_area = gamma_area.where(gamma_area > 0, 0)
    return gamma_area


def gamma_weights_bilinear(
    dem_coords: xr.Dataset,
    slant_range_time0: float,
    azimuth_time0: np.datetime64,
    slant_range_time_interval_s: float,
    azimuth_time_interval_s: float,
    slant_range_spacing_m: float = 1,
    azimuth_spacing_m: float = 1,
) -> xr.DataArray:
    # compute dem image coordinates
    azimuth_index = ((dem_coords.azimuth_time - azimuth_time0) / ONE_SECOND) / (
        azimuth_time_interval_s
    )

    slant_range_index = (dem_coords.slant_range_time - slant_range_time0) / (
        slant_range_time_interval_s
    )

    slant_range_index_0 = np.floor(slant_range_index).astype(int).compute()
    slant_range_index_1 = np.ceil(slant_range_index).astype(int).compute()
    azimuth_index_0 = np.floor(azimuth_index).astype(int).compute()
    azimuth_index_1 = np.ceil(azimuth_index).astype(int).compute()

    logger.info("compute gamma areas 1/4")
    w_00 = abs(
        (azimuth_index_1 - azimuth_index) * (slant_range_index_1 - slant_range_index)
    )
    tot_area_00 = sum_weights(
        dem_coords["gamma_area"] * w_00,
        azimuth_index=azimuth_index_0,
        slant_range_index=slant_range_index_0,
    )

    logger.info("compute gamma areas 2/4")
    w_01 = abs(
        (azimuth_index_1 - azimuth_index) * (slant_range_index_0 - slant_range_index)
    )
    tot_area_01 = sum_weights(
        dem_coords["gamma_area"] * w_01,
        azimuth_index=azimuth_index_0,
        slant_range_index=slant_range_index_1,
    )

    logger.info("compute gamma areas 3/4")
    w_10 = abs(
        (azimuth_index_0 - azimuth_index) * (slant_range_index_1 - slant_range_index)
    )
    tot_area_10 = sum_weights(
        dem_coords["gamma_area"] * w_10,
        azimuth_index=azimuth_index_1,
        slant_range_index=slant_range_index_0,
    )

    logger.info("compute gamma areas 4/4")
    w_11 = abs(
        (azimuth_index_0 - azimuth_index) * (slant_range_index_0 - slant_range_index)
    )
    tot_area_11 = sum_weights(
        dem_coords["gamma_area"] * w_11,
        azimuth_index=azimuth_index_1,
        slant_range_index=slant_range_index_1,
    )

    tot_area = tot_area_00 + tot_area_01 + tot_area_10 + tot_area_11

    normalized_area = tot_area / (azimuth_spacing_m * slant_range_spacing_m)
    return normalized_area


def gamma_weights_nearest(
    dem_coords: xr.Dataset,
    slant_range_time0: float,
    azimuth_time0: np.datetime64,
    slant_range_time_interval_s: float,
    azimuth_time_interval_s: float,
    slant_range_spacing_m: float = 1,
    azimuth_spacing_m: float = 1,
) -> xr.DataArray:
    # compute dem image coordinates
    azimuth_index = np.round(
        (dem_coords.azimuth_time - azimuth_time0) / ONE_SECOND / azimuth_time_interval_s
    ).astype(int)

    slant_range_index = np.round(
        (dem_coords.slant_range_time - slant_range_time0) / slant_range_time_interval_s
    ).astype(int)

    logger.info("compute gamma areas 1/1")

    tot_area = sum_weights(
        dem_coords["gamma_area"],
        azimuth_index=azimuth_index,
        slant_range_index=slant_range_index,
    )

    normalized_area = tot_area / (azimuth_spacing_m * slant_range_spacing_m)
    return normalized_area


def azimuth_slant_range_grid(
    attrs: Dict[str, Any],
    slant_range_time0: float,
    azimuth_time0: float,
    grouping_area_factor: Tuple[float, float] = (3.0, 3.0),
) -> Dict[str, Any]:

    if attrs["product_type"] == "SLC":
        slant_range_spacing_m = (
            attrs["range_pixel_spacing"]
            * np.sin(attrs["incidence_angle_mid_swath"])
            * grouping_area_factor[1]
        )
    else:
        slant_range_spacing_m = attrs["range_pixel_spacing"] * grouping_area_factor[1]

    slant_range_time_interval_s = (
        slant_range_spacing_m * 2 / geocoding.SPEED_OF_LIGHT  # ignore type
    )

    grid_parameters: Dict[str, Any] = {
        "slant_range_time0": slant_range_time0,
        "slant_range_time_interval_s": slant_range_time_interval_s,
        "slant_range_spacing_m": slant_range_spacing_m,
        "azimuth_time0": azimuth_time0,
        "azimuth_time_interval_s": attrs["azimuth_time_interval"]
        * grouping_area_factor[0],
        "azimuth_spacing_m": attrs["azimuth_pixel_spacing"] * grouping_area_factor[0],
    }
    return grid_parameters