#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright (c) 2022-2023 Satpy developers
#
# This file is part of satpy.
#
# satpy is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# satpy is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE.  See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with
# satpy.  If not, see <http://www.gnu.org/licenses/>.
"""VIIRS NOAA enterprise EDR product reader.

This module defines the :class:`VIIRSJRRFileHandler` file handler, to
be used for reading VIIRS EDR products generated by the NOAA enterprise
suite, which are downloadable via NOAA CLASS or on NOAA's AWS buckets.

A wide variety of such products exist and, at present, only a subset are supported.

 - Cloud mask: JRR-CloudMask_v2r3_j01_s202112250807275_e202112250808520_c202112250837300.nc
 - Cloud products: JRR-CloudHeight_v2r3_j01_s202112250807275_e202112250808520_c202112250837300.nc
 - Aerosol detection: JRR-ADP_v2r3_j01_s202112250807275_e202112250808520_c202112250839550.nc
 - Aerosol optical depth: JRR-AOD_v2r3_j01_s202112250807275_e202112250808520_c202112250839550.nc
 - Surface reflectance: SurfRefl_v1r1_j01_s202112250807275_e202112250808520_c202112250845080.nc
 - Land Surface Temperature: LST_v2r0_npp_s202307241724558_e202307241726200_c202307241854058.nc

All products use the same base reader ``viirs_edr`` and can be read through satpy with::

    import satpy
    import glob

    filenames = glob.glob('JRR-ADP*.nc')
    scene = satpy.Scene(filenames, reader='viirs_edr')
    scene.load(['smoke_concentration'])

.. note::

    Multiple products contain datasets with the same name! For example, both the cloud mask
    and aerosol detection files contain a cloud mask, but these are not identical.
    For clarity, the aerosol file cloudmask is named `cloud_mask_adp` in this reader.

Vegetation Indexes
^^^^^^^^^^^^^^^^^^

The NDVI and EVI products can be loaded from CSPP-produced Surface Reflectance
files. By default, these products are filtered based on the Surface Reflectance
Quality Flags. This is used to remove/mask pixels in certain cloud or water
regions. This behavior can be disabled by providing the reader keyword argument
``filter_veg`` and setting it to ``False``. For example::

    scene = satpy.Scene(filenames, reader='viirs_edr', reader_kwargs={"filter_veg": False})

"""
from __future__ import annotations

import logging
from typing import Iterable

import xarray as xr

from satpy import DataID
from satpy.readers.file_handlers import BaseFileHandler
from satpy.utils import get_chunk_size_limit

LOG = logging.getLogger(__name__)
M_COLS = 3200


class VIIRSJRRFileHandler(BaseFileHandler):
    """NetCDF4 reader for VIIRS Active Fires."""

    def __init__(self, filename, filename_info, filetype_info):
        """Initialize the geo filehandler."""
        super(VIIRSJRRFileHandler, self).__init__(filename, filename_info,
                                                  filetype_info)
        # use entire scans as chunks
        row_chunks_m = max(get_chunk_size_limit() // 4 // M_COLS, 1)  # 32-bit floats
        row_chunks_i = row_chunks_m * 2
        self.nc = xr.open_dataset(self.filename,
                                  decode_cf=True,
                                  mask_and_scale=True,
                                  chunks={
                                      'Columns': -1,
                                      'Rows': row_chunks_m,
                                      'Along_Scan_375m': -1,
                                      'Along_Track_375m': row_chunks_i,
                                      'Along_Scan_750m': -1,
                                      'Along_Track_750m': row_chunks_m,
                                  })
        if 'Columns' in self.nc.dims:
            self.nc = self.nc.rename({'Columns': 'x', 'Rows': 'y'})
        elif 'Along_Track_375m' in self.nc.dims:
            self.nc = self.nc.rename({'Along_Scan_375m': 'x', 'Along_Track_375m': 'y'})
            self.nc = self.nc.rename({'Along_Scan_750m': 'x', 'Along_Track_750m': 'y'})

        # For some reason, no 'standard_name' is defined in some netCDF files, so
        # here we manually make the definitions.
        if 'Latitude' in self.nc:
            self.nc['Latitude'].attrs.update({'standard_name': 'latitude'})
        if 'Longitude' in self.nc:
            self.nc['Longitude'].attrs.update({'standard_name': 'longitude'})

        self.algorithm_version = filename_info['platform_shortname']
        self.sensor_name = 'viirs'

    def rows_per_scans(self, data_arr: xr.DataArray) -> int:
        """Get number of array rows per instrument scan based on data resolution."""
        return 16 if data_arr.shape[1] == M_COLS else 32

    def get_dataset(self, dataset_id: DataID, info: dict) -> xr.DataArray:
        """Get the dataset."""
        data_arr = self.nc[info['file_key']]
        data_arr = self._mask_invalid(data_arr, info)
        units = info.get("units", data_arr.attrs.get("units"))
        if units is None or units == "unitless":
            units = "1"
        if units == "%" and data_arr.attrs.get("units") in ("1", "unitless"):
            data_arr *= 100.0  # turn into percentages
        data_arr.attrs["units"] = units
        if "standard_name" in info:
            data_arr.attrs["standard_name"] = info["standard_name"]
        self._decode_flag_meanings(data_arr)
        data_arr.attrs["platform_name"] = self.platform_name
        data_arr.attrs["sensor"] = self.sensor_name
        data_arr.attrs["rows_per_scan"] = self.rows_per_scans(data_arr)
        if data_arr.attrs.get("standard_name") in ("longitude", "latitude"):
            # recursive swath definitions are a problem for the base reader right now
            # delete the coordinates here so the base reader doesn't try to
            # make a SwathDefinition
            data_arr = data_arr.reset_coords(drop=True)
        return data_arr

    def _mask_invalid(self, data_arr: xr.DataArray, ds_info: dict) -> xr.DataArray:
        # xarray auto mask and scale handled any fills from the file
        valid_range = ds_info.get("valid_range", data_arr.attrs.get("valid_range"))
        if "valid_min" in data_arr.attrs and valid_range is None:
            valid_range = (data_arr.attrs["valid_min"], data_arr.attrs["valid_max"])
        if valid_range is not None:
            return data_arr.where((valid_range[0] <= data_arr) & (data_arr <= valid_range[1]))
        return data_arr

    @staticmethod
    def _decode_flag_meanings(data_arr: xr.DataArray):
        flag_meanings = data_arr.attrs.get("flag_meanings", None)
        if isinstance(flag_meanings, str) and "\n" not in flag_meanings:
            # only handle CF-standard flag meanings
            data_arr.attrs['flag_meanings'] = [flag for flag in data_arr.attrs['flag_meanings'].split(' ')]

    @property
    def start_time(self):
        """Get first date/time when observations were recorded."""
        return self.filename_info['start_time']

    @property
    def end_time(self):
        """Get last date/time when observations were recorded."""
        return self.filename_info['end_time']

    @property
    def platform_name(self):
        """Get platform name."""
        platform_path = self.filename_info['platform_shortname']
        platform_dict = {'NPP': 'Suomi-NPP',
                         'JPSS-1': 'NOAA-20',
                         'J01': 'NOAA-20',
                         'JPSS-2': 'NOAA-21',
                         'J02': 'NOAA-21'}
        return platform_dict[platform_path.upper()]

    def available_datasets(self, configured_datasets=None):
        """Get information of available datasets in this file.

        Args:
            configured_datasets (list): Series of (bool or None, dict) in the
                same way as is returned by this method (see below). The bool
                is whether the dataset is available from at least one
                of the current file handlers. It can also be ``None`` if
                no file handler before us knows how to handle it.
                The dictionary is existing dataset metadata. The dictionaries
                are typically provided from a YAML configuration file and may
                be modified, updated, or used as a "template" for additional
                available datasets. This argument could be the result of a
                previous file handler's implementation of this method.

        Returns:
            Iterator of (bool or None, dict) pairs where dict is the
            dataset's metadata. If the dataset is available in the current
            file type then the boolean value should be ``True``, ``False``
            if we **know** about the dataset but it is unavailable, or
            ``None`` if this file object is not responsible for it.

        """
        # keep track of what variables the YAML has configured, so we don't
        # duplicate entries for them in the dynamic portion
        handled_var_names = set()
        for is_avail, ds_info in (configured_datasets or []):
            file_key = ds_info.get("file_key", ds_info["name"])
            # we must add all variables here even if another file handler has
            # claimed the variable. It could be another instance of this file
            # type and we don't want to add that variable dynamically if the
            # other file handler defined it by the YAML definition.
            handled_var_names.add(file_key)
            if is_avail is not None:
                # some other file handler said it has this dataset
                # we don't know any more information than the previous
                # file handler so let's yield early
                yield is_avail, ds_info
                continue
            if self.file_type_matches(ds_info['file_type']) is None:
                # this is not the file type for this dataset
                yield None, ds_info
            yield file_key in self.nc, ds_info

        yield from self._dynamic_variables_from_file(handled_var_names)

    def _dynamic_variables_from_file(self, handled_var_names: set) -> Iterable[tuple[bool, dict]]:
        ftype = self.filetype_info["file_type"]
        m_lon_name = f"longitude_{ftype}"
        m_lat_name = f"latitude_{ftype}"
        m_coords = (m_lon_name, m_lat_name)
        i_lon_name = f"longitude_i_{ftype}"
        i_lat_name = f"latitude_i_{ftype}"
        i_coords = (i_lon_name, i_lat_name)
        for var_name in self.nc.variables.keys():
            data_arr = self.nc[var_name]
            is_lon = "longitude" in var_name.lower()
            is_lat = "latitude" in var_name.lower()
            if var_name in handled_var_names and not (is_lon or is_lat):
                # skip variables that YAML had configured, but allow lon/lats
                # to be reprocessed due to our dynamic coordinate naming
                continue
            if data_arr.ndim != 2:
                # only 2D arrays supported at this time
                continue
            res = 750 if data_arr.shape[1] == M_COLS else 375
            ds_info = {
                "file_key": var_name,
                "file_type": ftype,
                "name": var_name,
                "resolution": res,
                "coordinates": m_coords if res == 750 else i_coords,
            }
            if is_lon:
                ds_info["standard_name"] = "longitude"
                ds_info["units"] = "degrees_east"
                ds_info["name"] = m_lon_name if res == 750 else i_lon_name
                # recursive coordinate/SwathDefinitions are not currently handled well in the base reader
                del ds_info["coordinates"]
            elif is_lat:
                ds_info["standard_name"] = "latitude"
                ds_info["units"] = "degrees_north"
                ds_info["name"] = m_lat_name if res == 750 else i_lat_name
                # recursive coordinate/SwathDefinitions are not currently handled well in the base reader
                del ds_info["coordinates"]
            yield True, ds_info


class VIIRSSurfaceReflectanceWithVIHandler(VIIRSJRRFileHandler):
    """File handler for surface reflectance files with optional vegetation indexes."""

    def __init__(self, *args, filter_veg: bool = True, **kwargs) -> None:
        """Initialize file handler and keep track of vegetation index filtering."""
        super().__init__(*args, **kwargs)
        self._filter_veg = filter_veg

    def _mask_invalid(self, data_arr: xr.DataArray, ds_info: dict) -> xr.DataArray:
        new_data_arr = super()._mask_invalid(data_arr, ds_info)
        if ds_info["file_key"] in ("NDVI", "EVI") and self._filter_veg:
            good_mask = self._get_veg_index_good_mask()
            new_data_arr = new_data_arr.where(good_mask)
        return new_data_arr

    def _get_veg_index_good_mask(self) -> xr.DataArray:
        # each mask array should be TRUE when pixels are UNACCEPTABLE
        qf1 = self.nc['QF1 Surface Reflectance']
        has_sun_glint = (qf1 & 0b11000000) > 0
        is_cloudy = (qf1 & 0b00001100) > 0  # mask everything but "confident clear"
        cloud_quality = (qf1 & 0b00000011) < 0b10

        qf2 = self.nc['QF2 Surface Reflectance']
        has_snow_or_ice = (qf2 & 0b00100000) > 0
        has_cloud_shadow = (qf2 & 0b00001000) > 0
        water_mask = (qf2 & 0b00000111)
        has_water = (water_mask <= 0b010) | (water_mask == 0b101)  # shallow water, deep ocean, arctic

        qf7 = self.nc['QF7 Surface Reflectance']
        has_aerosols = (qf7 & 0b00001100) > 0b1000  # high aerosol quantity
        adjacent_to_cloud = (qf7 & 0b00000010) > 0

        bad_mask = (
                has_sun_glint |
                is_cloudy |
                cloud_quality |
                has_snow_or_ice |
                has_cloud_shadow |
                has_water |
                has_aerosols |
                adjacent_to_cloud
        )
        # upscale from M-band resolution to I-band resolution
        bad_mask_iband_dask = bad_mask.data.repeat(2, axis=1).repeat(2, axis=0)
        good_mask_iband = xr.DataArray(~bad_mask_iband_dask, dims=qf1.dims)
        return good_mask_iband


class VIIRSLSTHandler(VIIRSJRRFileHandler):
    """File handler to handle LST file scale factor and offset weirdness."""

    _manual_scalings = {
        "VLST": ("LST_ScaleFact", "LST_Offset"),
        "emis_m15": ("LSE_ScaleFact", "LSE_Offset"),
        "emis_m16": ("LSE_ScaleFact", "LSE_Offset"),
        "emis_bbe": ("LSE_ScaleFact", "LSE_Offset"),
        "Satellite_Azimuth_Angle": ("AZI_ScaleFact", "AZI_Offset"),
    }

    def __init__(self, *args, **kwargs):
        """Initialize the file handler and unscale necessary variables."""
        super().__init__(*args, **kwargs)

        # Update variables with external scale factor and offset
        self._scale_data()

    def _scale_data(self):
        for var_name in list(self.nc.variables.keys()):
            if var_name not in self._manual_scalings:
                continue
            data_arr = self.nc[var_name]
            scale_factor = self.nc[self._manual_scalings[var_name][0]]
            add_offset = self.nc[self._manual_scalings[var_name][1]]
            data_arr.data = data_arr.data * scale_factor.data + add_offset.data
            self.nc[var_name] = data_arr
