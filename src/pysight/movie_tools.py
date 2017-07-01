"""
#!/usr/bin/env python
# -*- coding: utf-8 -*-
__author__ = Hagai Hargil
"""
import attr
from attr.validators import instance_of
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from typing import List, Iterator, Tuple, Iterable, Generator, Deque
from numba import jit, float64, uint64, int64
from collections import OrderedDict, namedtuple, deque
import warnings


@attr.s
class Movie(object):
    """
    A holder for Volume objects to be displayed consecutively.
    """
    data            = attr.ib()
    reprate         = attr.ib(default=80e6, validator=instance_of(float))
    x_pixels        = attr.ib(default=512, validator=instance_of(int))
    y_pixels        = attr.ib(default=512, validator=instance_of(int))
    z_pixels        = attr.ib(default=1, validator=instance_of(int))
    name            = attr.ib(default='Movie', validator=instance_of(str))
    binwidth        = attr.ib(default=800e-12, validator=instance_of(float))
    fill_frac       = attr.ib(default=80.0, validator=instance_of(float))
    big_tiff        = attr.ib(default=True, validator=instance_of(bool))
    bidir           = attr.ib(default=True, validator=instance_of(bool))
    num_of_channels = attr.ib(default=1, validator=instance_of(int))
    outputs         = attr.ib(default={}, validator=instance_of(dict))
    censor          = attr.ib(default=False, validator=instance_of(bool))
    flim            = attr.ib(default=False, validator=instance_of(bool))
    lst_metadata    = attr.ib(default={}, validator=instance_of(dict))
    exp_params      = attr.ib(default={}, validator=instance_of(dict))
    summed_mem      = attr.ib(init=False)
    stack           = attr.ib(init=False)
    summed_tif      = attr.ib(init=False)
    all_tif_ptr     = attr.ib(init=False)

    @property
    def bins_bet_pulses(self) -> int:
        if self.flim:
            return int(np.ceil(1 / (self.reprate * self.binwidth)))
        else:
            return 1

    @property
    def list_of_volume_times(self) -> List[np.uint64]:
        """ All volumes start-times in the movie. """

        volume_times = np.unique(self.data.index.get_level_values('Frames')).astype(np.uint64)

        if len(volume_times) > 1:
            diff_between_frames = np.mean(np.diff(volume_times))
        else:
            diff_between_frames = np.uint64(np.max(self.data['time_rel_frames']))

        volume_times = list(volume_times)
        volume_times.append(np.uint64(volume_times[-1] + diff_between_frames))

        return volume_times

    def run(self):
        """
        Main pipeline for the movie object
        :return:
        """
        self.__create_outputs()
        self.__print_outputs()
        print("Movie object created, analysis done.")

    def gen_of_volumes(self, channel_num: int) -> Iterator:
        """
        Populate the deque containing the volumes as a generator.
        Creates a list for each channel in the data. Channels start with 1.
        """

        list_of_frames: List[int] = self.list_of_volume_times  # saves a bit of computation
        for idx, current_time in enumerate(list_of_frames[:-1]):  # populate deque with frames
            cur_data = self.data.xs(key=(current_time, channel_num), level=('Frames', 'Channel'), drop_level=False)
            if not cur_data.empty:
                yield Volume(data=cur_data, x_pixels=self.x_pixels, y_pixels=self.y_pixels,
                             z_pixels=self.z_pixels, number=idx, abs_start_time=current_time,
                             reprate=self.reprate, binwidth=self.binwidth, empty=False,
                             end_time=(list_of_frames[idx + 1] - list_of_frames[idx]),
                             bidir=self.bidir, fill_frac=self.fill_frac, censor=self.censor)
            else:
                yield Volume(data=cur_data, x_pixels=self.x_pixels,
                             y_pixels=self.y_pixels, z_pixels=self.z_pixels, number=idx,
                             reprate=self.reprate, binwidth=self.binwidth, empty=True,
                             end_time=(list_of_frames[idx + 1] - list_of_frames[idx]),
                             bidir=self.bidir, fill_frac=self.fill_frac, censor=self.censor)

    def __create_outputs(self) -> None:
        """
        Create the outputs according to the outputs dictionary.
        """
        from tifffile import TiffWriter

        if not self.outputs:
            warnings.warn("No outputs requested. Data is still accessible using the dataframe variable.")
            return

        funcs_to_execute = []
        if 'memory' in self.outputs:
            self.summed_mem = {i: 0 for i in range(1, self.num_of_channels + 1)}
            self.stack = {i: deque() for i in range(1, self.num_of_channels + 1)}
            funcs_to_execute.append(self.__create_memory_output)
        if 'tif' in self.outputs:
            funcs_to_execute.append(self.__create_all_tif)
            self.all_tif_ptr = {channel: TiffWriter(f'{self.name[:-4]}_chan_{channel}_stack.tif',
                                                    bigtiff=self.big_tiff, software='PySight', imagej=True)
                                for channel in range(1, self.num_of_channels + 1)}
        if 'summed' in self.outputs:
            self.summed_tif = {i: 0 for i in range(1, self.num_of_channels + 1)}
            funcs_to_execute.append(self.__create_summed_tif)

        VolTuple = namedtuple('VolumeHist', ('hist', 'edges'))
        data_of_vol = VolTuple

        for chan in range(1, self.num_of_channels + 1):
            for vol in self.gen_of_volumes(channel_num=chan):
                data_of_vol.hist, data_of_vol.edges = vol.create_hist()
                for func in funcs_to_execute:
                    func(data=data_of_vol.hist, channel=chan)

            if 'summed' in self.outputs:
                self.__save_summed_tif(channel=chan)

        # Close open file pointers (if any)
        for idx in range(1, self.num_of_channels + 1):
            try:
                self.all_tif_ptr[idx].close()
            except NameError:
                pass

    def __create_memory_output(self, data: np.ndarray, channel: int):
        """
        If the user desired, create two memory constructs -
        A summed array of all images (for a specific channel), and a stack containing
        all images in a serial manner.
        :param data: Data to be saved.
        :param channel: Current spectral channel of data
        """
        self.stack[channel].append(data)
        self.summed_mem[channel] += data

    def __create_all_tif(self, data: np.ndarray, channel: int):
        """
        Save incrementally new data to an open file on the disk
        :param data: Data to save
        :param channel: Current spectral channel of data
        """
        try:
            # Dimension order: Z-Lifetime (C)-T-Y-X-S
            # Might need to swap Z and T.
            self.all_tif_ptr[channel].save(data.reshape(self.z_pixels, self.bins_bet_pulses,
                                           1, self.y_pixels, self.x_pixels, 1),
                                           metadata=self.lst_metadata)
        except PermissionError:
            warnings.warn("Permission Error: Not allowed to save file to original directory.")
        except IOError:
            warnings.warn("IO Error.")

    def __create_summed_tif(self, data: np.ndarray, channel: int):
        """
        Create a summed variable later to be saved as the channel's data
        :param data: Data to be saved
        :param channel: Spectral channel of data to be saved
        """
        self.summed_tif[channel] += data

    def __save_summed_tif(self, channel: int):
        """
        Save once
        :param channel:
        :return:
        """
        from tifffile import TiffWriter

        with TiffWriter(f'{self.name[:-4]}_chan_{channel}_summed.tif',
                        bigtiff=self.big_tiff, software='PySight', imagej=True) as tif:
            try:
                # Dimension order: Z-Lifetime (C)-T-Y-X-S
                # Might need to swap Z and T.
                tif.save(self.summed_tif[channel].reshape(self.z_pixels, self.bins_bet_pulses,
                                                          1, self.y_pixels, self.x_pixels, 1),
                         metadata=self.lst_metadata)
            except PermissionError:
                warnings.warn("Permission Error: Not allowed to save file to original directory.")
            except IOError:
                warnings.warn("IO Error.")

    def __print_outputs(self) -> None:
        """
        Print to console the outputs that were generated.
        """
        if not self.outputs:
            return

        print('======================================================= \nOutputs:\n--------')
        if 'tif' in self.outputs:
            print(f'Tiff stack created with name {self.name[:-4]}_chan_X_stack.tif, \none for each channel.')

        if 'memory' in self.outputs:
            print('The full data is present in dictionary form (key per channel) under `movie.stack`, '
                  'and in stacked form under `movie.summed_mem`.')

        if 'summed' in self.outputs:
            print(f'Summed tiff file created with name {self.name[:-4]}_chan_X_summed.tif, \none for each channel.')

    def __nano_flim(self, data: np.ndarray) -> None:
        pass


@attr.s(slots=True)
class Volume(object):
    """
    A Movie() is a sequence of volumes. Each volume contains frames in a plane.
    """
    data           = attr.ib(validator=instance_of(pd.DataFrame))
    x_pixels       = attr.ib(default=512, validator=instance_of(int))
    y_pixels       = attr.ib(default=512, validator=instance_of(int))
    z_pixels       = attr.ib(default=1, validator=instance_of(int))
    number         = attr.ib(default=1, validator=instance_of(int))  # the volume's ordinal number
    reprate        = attr.ib(default=80e6, validator=instance_of(float))  # laser repetition rate, relevant for FLIM
    end_time       = attr.ib(default=np.uint64(100), validator=instance_of(np.uint64))
    binwidth       = attr.ib(default=800e-12, validator=instance_of(float))
    bidir          = attr.ib(default=False, validator=instance_of(bool))  # Bi-directional scanning
    fill_frac      = attr.ib(default=80.0, validator=instance_of(float))
    abs_start_time = attr.ib(default=np.uint64(0), validator=instance_of(np.uint64))
    empty          = attr.ib(default=False, validator=instance_of(bool))
    censor         = attr.ib(default=False, validator=instance_of(bool))

    @property
    def metadata(self) -> OrderedDict:
        """
        Creates the metadata of the volume to be created, to be used for creating the actual images
        using histograms. Metadata can include the first photon arrival time, start and end of volume, etc.
        :return: Dictionary of all needed metadata.
        """

        metadata = OrderedDict()
        jitter = 0.02  # 2% of jitter of the signals that creates volumes

        # Volume metadata
        volume_start: int = 0
        metadata['Volume'] = Struct(start=volume_start, end=self.end_time, num=self.x_pixels + 1)

        # y-axis metadata
        y_start, y_end, self.empty = metadata_ydata(data=self.data, jitter=jitter, bidir=self.bidir,
                                                    fill_frac=self.fill_frac)
        if y_end == 1:  # single pixel in frame
            metadata['Y'] = Struct(start=y_start, end=self.end_time, num=self.y_pixels + 1)
        else:
            metadata['Y'] = Struct(start=y_start, end=y_end, num=self.y_pixels + 1)

        # z-axis metadata
        if 'Phase' in self.data.columns:
            z_start = -1
            z_end = 1
            metadata['Z'] = Struct(start=z_start, end=z_end, num=self.z_pixels + 1)

        # Laser pulses metadata
        if 'time_rel_pulse' in self.data.columns:
            try:
                laser_start = 0
                laser_end = np.ceil(1 / (self.reprate * self.binwidth)).astype(np.uint8)
                metadata['Laser'] = Struct(start=laser_start, end=laser_end, num=laser_end + 1)
            except ZeroDivisionError:
                laser_start = 0
                warnings.warn('No laser reprate provided. Assuming 80.3 MHz.')
                laser_end = np.ceil(1 / (80.3e6 * self.binwidth)).astype(np.uint8)
                metadata['Laser'] = Struct(start=laser_start, end=laser_end, num=laser_end + 1)

        return metadata

    def __create_hist_edges(self):
        """
        Create three vectors that will create the grid of the frame. Uses Numba internal function for optimization.
        :return: Tuple of np.array
        """
        metadata = self.metadata
        list_of_edges = []

        if self.empty is not True:
            for num_of_dims, key in enumerate(metadata, 1):
                if 'Volume' == key:
                    list_of_edges.append(self.__create_line_array())
                else:
                    list_of_edges.append(create_linspace(start=metadata[key].start,
                                                         stop=metadata[key].end,
                                                         num=metadata[key].num))

            return list_of_edges, num_of_dims
        else:
            return list(np.ones(len(metadata)))

    def __create_line_array(self):
        """
        Generates the edges of the final histogram using the line signal from the data
        :return: np.array
        """
        lines = self.data.index.get_level_values('Lines').categories.values
        lines.sort()
        if len(lines) > 1:
            if len(lines) < self.x_pixels:
                raise ValueError(f'Not enough line events in volume number {self.number}.')
            else:
                mean_diff = np.diff(lines).mean()
                return np.r_[lines[:self.x_pixels], np.array([lines[self.x_pixels - 1] + mean_diff], dtype='uint64')]
        else:  # single pixel frames, perhaps
            return np.r_[lines, lines + self.end_time]

    def create_hist(self) -> Tuple[np.ndarray, Iterable]:
        """
        Create the histogram of data using calculated edges.
        :return: np.ndarray of shape [num_of_cols, num_of_rows] with the histogram data, and edges
        """

        list_of_data_columns = []

        if not self.empty:
            list_of_edges, num_of_dims = self.__create_hist_edges()
            list_of_data_columns.append(self.data['time_rel_frames'].values)
            list_of_data_columns.append(self.data['time_rel_line'].values)
            try:
                list_of_data_columns.append(self.data['Phase'].values)
            except KeyError:
                pass
            try:
                list_of_data_columns.append(self.data['time_rel_pulse'].values)
            except KeyError:
                pass

            data_to_be_hist = np.reshape(list_of_data_columns, (num_of_dims, self.data.shape[0])).T

            assert data_to_be_hist.shape[0] == self.data.shape[0]
            assert len(list_of_data_columns) == data_to_be_hist.shape[1]

            hist, edges = np.histogramdd(sample=data_to_be_hist, bins=list_of_edges)

            if self.censor:
                hist = self.__censor_correction(hist)

            return hist.astype(np.int16), edges
        else:
            return np.zeros((self.x_pixels, self.y_pixels, self.z_pixels), dtype=np.int16), (0, 0, 0)

    def show(self) -> None:
        """ Show the Volume. Mainly for debugging purposes, as the Movie object doesn't use it. """

        hist, edges = self.create_hist()
        plt.figure()
        # plt.imshow(hist, cmap='gray')
        plt.title(f'Volume number {self.number}')
        plt.axis('off')

    def __censor_correction(self, data) -> np.ndarray:
        """
        Add censor correction to the data after being histogrammed
        :param data:
        :return:
        """
        rel_idx = np.argwhere(np.sum(data, axis=-1) > 1)
        split = np.split(rel_idx, 2, axis=1)
        squeezed = np.squeeze(data[split[0], split[1], :])
        return data


def validate_number_larger_than_zero(instance, attribute, value: int=0):
    """
    Validator for attrs module - makes sure line numbers and row numbers are larger than 0.
    """

    if value >= instance.attribute:
        raise ValueError(f"{attribute} has to be larger than {value}.")


@jit((float64[:](int64, uint64, uint64)), nopython=True, cache=True)
def create_linspace(start, stop, num):
    linspaces = np.linspace(start, stop, num)
    assert np.all(np.diff(linspaces) > 0)
    return linspaces


def metadata_ydata(data: pd.DataFrame, jitter: float=0.02, bidir: bool = True, fill_frac: float = 0):
    """
    Create the metadata for the y-axis.
    """
    lines_start: int = 0
    empty: bool = False

    unique_indices: np.ndarray = np.unique(data.index.get_level_values('Lines'))
    if unique_indices.shape[0] > 1:  # TODO: Doesn't make sense that there's a single line in a frame
        diffs: np.ndarray = np.diff(unique_indices)
        diffs_max: float = diffs.max()
    else:
        empty = False
        lines_end = 1
        return lines_start, lines_end, empty

    try:
        if diffs_max > ((1 + 4 * jitter) * np.mean(diffs)):  # Noisy data
            lines_end = np.mean(diffs) * (1 + jitter)
        else:
            lines_end = int(diffs_max)
    except ValueError:
        lines_end = 0
        empty = True

    # Case where it's a unidirectional scan and we dump back-phase photons
    if not bidir:
        lines_end /= 2

    if fill_frac > 0:
        lines_end = lines_end * fill_frac/100

    return lines_start, int(lines_end), empty


@attr.s
class Struct(object):
    """ Basic struct-like object for data keeping. """

    start = attr.ib()
    end = attr.ib()
    num = attr.ib(default=None)
