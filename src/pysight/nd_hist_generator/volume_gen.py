"""
__author__ = Hagai Har-Gil
"""
import attr
from attr.validators import instance_of
import pandas as pd
import numpy as np
import itertools
from typing import List, Generator


@attr.s(slots=True)
class VolumeGenerator:
    """
    Generate the list of volume chunks to be processed.
    Main method is "create_frame_slices", which returns a generator containing
    slice objects that signify the chunks of volumes to be processed simultaneously.
    Inputs:
        :param data pd.DataFrame: The entire dataset
        :param data_shape tuple: Shape of the final n-dimensional array (from the Output object)
        :param MAX_BYTES_ALLOWED int: Number of bytes that can be held in RAM ("magic number")
    """
    data = attr.ib(validator=instance_of(pd.DataFrame), repr=False)
    data_shape = attr.ib(validator=instance_of(tuple))
    MAX_BYTES_ALLOWED = attr.ib(default=int(300e6), validator=instance_of(int))
    num_of_frames = attr.ib(init=False)
    frames = attr.ib(init=False)
    bytes_per_frames = attr.ib(init=False)
    full_frame_chunks = attr.ib(init=False)
    frame_slices = attr.ib(init=False)
    chunk_size = attr.ib(init=False)
    num_of_chunks = attr.ib(init=False)

    def create_frame_slices(self, create_slices=True) -> Generator:
        """
        Main method for the pipeline. Returns a generator with slices that
        signify the start time and end time of all frames.
        :param create_slices bool: Used for testing, always keep true.
        """
        self.bytes_per_frames = np.prod(self.data_shape[1:]) * 8
        self.chunk_size = max(1, self.MAX_BYTES_ALLOWED // self.bytes_per_frames)
        self.frames = self.__create_vol_list()
        self.num_of_chunks = int(max(1, len(self.frames) // self.chunk_size))
        self.full_frame_chunks = self.__grouper()
        if create_slices:
            self.frame_slices = self.__generate_frame_slices()
            return self.frame_slices

    def __create_vol_list(self):
        volume_times = np.unique(self.data.index.get_level_values('Frames'))
        self.num_of_frames = len(volume_times)
        volume_times = list(volume_times)
        return volume_times

    def __grouper(self) -> Generator:
        """
        Chunk volume times into maximal-sized groups of values.
        grouper('ABCDEFG', 3, 'x') --> ABC DEF Gxx
        """
        args = [iter(self.frames)] * self.chunk_size
        return itertools.zip_longest(*args, fillvalue=np.nan)

    def __generate_frame_slices(self) -> Generator:
        if self.chunk_size == 1:
            start, end = itertools.tee(self.full_frame_chunks)
            next(end, None)
            return (slice(s, e) for s, e in zip(start, end))

        start_and_end = []
        for chunk in self.full_frame_chunks:
            first, last = chunk[0], chunk[-1]
            if np.isnan(last):
                for val in reversed(chunk[:-1]):
                    if val is not np.nan:
                        last = val
                        break
            start_and_end.append((first, last))

        return (slice(np.uint64(t[0]), np.uint64(t[1])) for t in start_and_end)