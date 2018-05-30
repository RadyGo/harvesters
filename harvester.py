# ----------------------------------------------------------------------------
#
# Copyright 2018 EMVA
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# ----------------------------------------------------------------------------


# Standard library imports
import io
import pathlib
import sys
import time
import zipfile

# Related third party imports
from PyQt5.QtCore import QThread, pyqtSignal, QMutexLocker, QMutex
from PyQt5.QtWidgets import QApplication

import numpy as np
#from scipy import ndimage

from genapi import NodeMap
from gentl import TimeoutException, AccessDeniedException
from gentl import GenTLProducer, BufferToken, EventManagerNewBuffer
from gentl import DEVICE_ACCESS_FLAGS_LIST, EVENT_TYPE_LIST, \
    ACQ_START_FLAGS_LIST, ACQ_STOP_FLAGS_LIST, ACQ_QUEUE_TYPE_LIST, \
    TL_CHAR_ENCODING_LIST

# Local application/library specific imports
from icon import Icon
from port import ConcretePort
from processor import Processor


__version__= '1.0.0, ' + 'Y2018.M05.D25'


class ImageAcquisitionThread(QThread):
    sig_status = pyqtSignal()

    def __init__(self, mutex=None, parent=None):
        #
        super().__init__(parent)

        #
        self._worker = None
        self._is_running = False
        self._mutex = mutex

    def setup(self, worker):
        self._worker = worker
        self._is_running = True

    def stop(self):
        with QMutexLocker(self._mutex):
            self._is_running = False

    def run(self):
        while self._is_running:
            self._worker()

    def join(self):
        while self._is_running:
            pass


class ImageInformation:
    def __init__(self, buffer, node_map, ndarray):
        #
        super().__init__()

        #
        self._buffer = buffer
        self._node_map = node_map
        self._ndarray = ndarray

    @property
    def buffer(self):
        return self._buffer

    @property
    def node_map(self):
        return self._node_map

    @property
    def ndarray(self):
        return self._ndarray


class FromBytesToNumpy1D(Processor):
    def __init__(self):
        #
        super().__init__(
            brief_description='Converts a Python bytes object to Numpy 1D array'
        )

    def process(self, input: ImageInformation):
        output = ImageInformation(
            input.buffer,
            input.node_map,
            np.frombuffer(input.buffer.raw_buffer, dtype='uint8')
        )
        return output


class FromNumpy1DToNumpy2D(Processor):
    def __init__(self):
        #
        super().__init__(
            brief_description='Converts a Numpy 1D array to Numpy 1D array')

    def process(self, input: ImageInformation):
        #
        pixel_format = input.node_map.PixelFormat.get_entry(
            input.buffer.pixel_format
        )
        symbolic = pixel_format.symbolic

        #
        mono_formats = ['Mono8']
        rgb_formats = ['RGB8', 'RGB8Packed']
        bayer_formats = ['BayerGR8', 'BayerGB8', 'BayerRG8', 'BayerBG8']

        #
        ndarray = None
        if symbolic in mono_formats:
            ndarray = input.ndarray.reshape(
                input.buffer.height, input.buffer.width
            )
        elif symbolic in rgb_formats:
            ndarray = input.ndarray.reshape(
                input.buffer.height, input.buffer.width, 3
            )
        elif symbolic in bayer_formats:
            # TODO: Convert the raw data.
            ndarray = input.ndarray.reshape(
                input.buffer.height, input.buffer.width
            )
        output = ImageInformation(
            input.buffer, input.node_map, ndarray
        )

        return output


class Rotate(Processor):
    def __init__(self, angle=0):
        #
        super().__init__(brief_description='Rotate Numpy 2D array')

        #
        self._angle = angle

    def process(self, input: ImageInformation):
        #
        #ndarray = ndimage.rotate(input.ndarray, self._angle)  # Import scipy.
        ndarray = None
        output = ImageInformation(
            input.buffer, input.node_map, ndarray
        )
        return output


class Statistics:
    def __init__(self):
        #
        super().__init__()

        #
        self._timestamp_base = 0
        self._has_acquired_1st_timestamp = False
        self._fps = 0.
        self._num_images = 0
        self._fps_max = 0.

    def set_timestamp(self, timestamp):
        # TODO: Harvester is temporarily expecting to have ns timestamps.
        if not self._has_acquired_1st_timestamp:
            self._timestamp_base = timestamp
            self._has_acquired_1st_timestamp = True
        else:
            diff = timestamp - self._timestamp_base
            if diff > 0:
                fps = self._num_images / (diff * 0.000000001)
                if fps > self._fps_max:
                    self._fps_max = fps
                self._fps = fps
            else:
                self._fps = 0.

    def reset(self):
        self._timestamp_base = 0
        self._has_acquired_1st_timestamp = False
        self._fps = 0.
        self._num_images = 0
        self._fps_max = 0.

    def increment_num_images(self, num=1):
        self._num_images += num

    @property
    def fps(self):
        return self._fps

    @property
    def fps_max(self):
        return self._fps_max


class Harvester:
    _encodings = {
        TL_CHAR_ENCODING_LIST.TL_CHAR_ENCODING_ASCII: 'ascii',
        TL_CHAR_ENCODING_LIST.TL_CHAR_ENCODING_UTF8: 'utf8'
    }

    def __init__(self, frontend: str= ''):
        #
        super().__init__()

        #
        self._connecting_device = None
        self._is_acquiring_images = False

        #
        self._cti_file_paths = []
        self._producers = []
        self._systems = []
        self._interfaces = []
        self._device_info_list = []
        self._data_stream = None
        self._event_manager = None

        #
        self._raw_buffers = []
        self._buffer_tokens = []
        self._announced_buffers = []
        self._latest_gentl_buffer = None

        #
        self._node_map = None

        #
        self._has_revised_list = False
        self._timeout_for_update = 1000  # ms
        self._has_acquired_1st_image = False

        #
        self._thread_image_acquisition = None

        #
        self._mutex = QMutex()
        self._latest_texture_data = None
        self._feature_tree_model = None

        #
        self._thread_statistics_measurement = None
        self._current_width = 0
        self._current_height = 0
        self._current_pixel_format = ''

        #
        self._statistics_update_cycle = 1  # s
        self._statistics_latest = Statistics()
        self._statistics_overall = Statistics()
        self._statistics_list = [
            self._statistics_latest, self._statistics_overall
        ]

        #
        self._frontend = frontend

        #
        self._timeout_for_image_acquisition = 100  # ms

        #
        self._processing_units = []
        self.add_processor(FromBytesToNumpy1D())
        self.add_processor(FromNumpy1DToNumpy2D())
        # You may want to add other processors.

        #
        if self._frontend.lower() == 'pyqt':
            from frontend_pyqt import HarvesterGUI
            self._gui = HarvesterGUI(harvester_core=self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect_device()
        self.release_all_resources()

    @property
    def node_map(self):
        return self._node_map

    @property
    def connecting_device(self):
        return self._connecting_device

    @property
    def cti_file_paths(self):
        return self._cti_file_paths

    @property
    def is_acquiring_images(self):
        return self._is_acquiring_images

    @property
    def device_info_list(self):
        return self._device_info_list

    @property
    def timeout_for_update(self):
        return self._timeout_for_update

    @timeout_for_update.setter
    def timeout_for_update(self, ms):
        self._timeout_for_update = ms

    @property
    def timeout_for_image_acquisition(self):
        return self._timeout_for_image_acquisition

    @property
    def mutex(self):
        return self._mutex

    @timeout_for_image_acquisition.setter
    def timeout_for_image_acquisition(self, ms):
        with QMutexLocker(self._mutex):
            self._timeout_for_image_acquisition = ms

    def get_latest_image(self, return_copy=True):
        if self._latest_texture_data is not None:
            if return_copy:
                with QMutexLocker(self._mutex):
                    return np.array(self._latest_texture_data)
            else:
                return self._latest_texture_data
        else:
            return None

    @property
    def processing_units(self):
        return self._processing_units

    @property
    def has_revised_list(self):
        return self._has_revised_list

    @has_revised_list.setter
    def has_revised_list(self, value):
        self._has_revised_list = value

    @property
    def gui(self):
        return self._gui

    def add_processor(self, processing_unit: Processor):
        self._processing_units.append(processing_unit)

    def connect_device(self, index):
        if self.connecting_device is None:
            # Instantiate a GenTL Device module.
            self._connecting_device = self._device_info_list[
                index].create_device()

            # Then open it.
            try:
                self.connecting_device.open(
                    DEVICE_ACCESS_FLAGS_LIST.DEVICE_ACCESS_EXCLUSIVE
                )
            except AccessDeniedException as e:
                print(e)
                self.disconnect_device()
            else:
                # And get an alias of its GenTL Port module.
                port = self.connecting_device.remote_port

                # Inquire it's URL information.
                # TODO: Consider a case where len(url_info_list) > 1.
                url = port.url_info_list[0].url

                # And parse the URL.
                location, others = url.split(':', 1)
                file_name, address, size = others.split(';')
                address = int(address, 16)

                # It may specify the schema version.
                delimiter = '?'
                if delimiter in size:
                    size, _ = size.split(delimiter)
                size = int(size, 16)

                # Now we get the file content.
                content = port.read(address, size)

                # But wait, we have to check if it's a zip file or not.
                content = content[1]
                file_content = io.BytesIO(content)

                # Let's check the reality.
                if zipfile.is_zipfile(file_content):
                    # Yes, that's a zip file.
                    file_content = zipfile.ZipFile(file_content, 'r')

                    # Extract the file content from the zip file.
                    for file_info in file_content.infolist():
                        if pathlib.Path(
                                file_info.filename).suffix.lower() == '.xml':
                            content = file_content.read(file_info).decode(
                                self._encodings[
                                    # device -> interface -> system
                                    self.connecting_device.parent.parent.char_encoding
                                ]
                            )
                            break

                # Instantiate a GenICam node map object.
                self._node_map = NodeMap()

                # Then load the XML file content on the node map object.
                self.node_map.load_xml_from_string(content)

                # Instantiate a concrete port object of the remote device's
                # port.
                concrete_port = ConcretePort(self.connecting_device.remote_port)

                # And finally connect the concrete port on the node map
                # object.
                self.node_map.connect(concrete_port, port.name)

    def disconnect_device(self):
        """
        Disconnects the connecting device from Harvester.

        :return: None.
        """
        if self.connecting_device:
            if self.connecting_device.is_open():
                self.stop_image_acquisition()
                self.connecting_device.close()
            #
            self._connecting_device = None
            self._node_map = None

    def start_image_acquisition(self):
        if self.is_acquiring_images:
            # If it's pausing drawing images, just resume it and
            # immediately return this method.
            if self.gui.canvas.is_pausing:
                self.gui.canvas.resume_drawing()
        else:
            #
            self._data_stream = self.connecting_device.create_data_stream()
            self._data_stream.open(self.connecting_device.data_stream_ids[0])
            min_num_buffers = self._data_stream.buffer_announce_min

            if self._data_stream.defines_payload_size():
                buffer_size = self._data_stream.payload_size
            else:
                buffer_size = self.node_map.PayloadSize.value

            num_buffers = min_num_buffers * 2

            self._raw_buffers = self._create_raw_buffers(
                num_buffers, buffer_size
            )
            self._buffer_tokens = self._create_buffer_tokens(
                self._raw_buffers
            )
            self._announced_buffers = self._announce_buffers(
                self._buffer_tokens
            )
            self._queue_announced_buffers(self._announced_buffers)

            #
            et = self._data_stream.register_event(
                EVENT_TYPE_LIST.EVENT_NEW_BUFFER
            )
            self._event_manager = EventManagerNewBuffer(et)

            # Start image acquisition.
            # TODO:
            self._data_stream.start_acquisition(
                ACQ_START_FLAGS_LIST.ACQ_START_FLAGS_DEFAULT, -1
            )

            #
            self._is_acquiring_images = True

            #
            self.initialize_acquisition_statistics()
            self._thread_statistics_measurement = ImageAcquisitionThread(
                mutex=self._mutex
            )
            self._thread_statistics_measurement.setup(
                self._worker_acquisition_statistics
            )
            self._thread_statistics_measurement.start()

            #
            self._thread_image_acquisition = ImageAcquisitionThread(
                mutex=self._mutex
            )
            self._thread_image_acquisition.setup(
                self._worker_image_acquisition
            )
            self._thread_image_acquisition.start()

            #
            self.node_map.AcquisitionStart.execute()

    def _worker_acquisition_statistics(self):
        if not self.is_acquiring_images:
            return

        time.sleep(self._statistics_update_cycle)

        with QMutexLocker(self._mutex):
            #
            if self._frontend.lower() == 'pyqt':
                self.gui.statusBar().showMessage(
                    'W: {0} x H: {1}, {2}, '
                    '{3:.1f} fps in the last {4:.1f} s, '
                    '{5:.1f} fps for over all'.format(
                        self._current_width,
                        self._current_height,
                        self._current_pixel_format,
                        self._statistics_latest.fps,
                        self._statistics_update_cycle,
                        self._statistics_overall.fps,
                    )
                )
            self._statistics_latest.reset()

    def _worker_image_acquisition(self):
        try:
            if self.is_acquiring_images:
                time.sleep(0.001)
                self._event_manager.update_event_data(
                    self._timeout_for_image_acquisition
                )
        except TimeoutException as e:
            print(e)
        else:
            with QMutexLocker(self._mutex):
                #
                if not self.is_acquiring_images:
                    return

                # We've got a new image so now we can reuse the buffer that
                # we had kept.
                if self._latest_gentl_buffer is not None:
                    self._data_stream.queue_buffer(
                        self._latest_gentl_buffer
                    )

                buffer = self._event_manager.buffer

                #
                for statistics in self._statistics_list:
                    statistics.increment_num_images()
                    statistics.set_timestamp(buffer.timestamp)

                #
                if not self._has_acquired_1st_image:
                    if self._frontend.lower() == 'pyqt':
                        self.gui.canvas.set_rect(
                            buffer.width, buffer.height
                        )
                    self._has_acquired_1st_image = True

                input = ImageInformation(
                    buffer, self.node_map, None
                )
                output = None

                for pu in self._processing_units:
                    output = pu.process(input)
                    input = output

                #
                if output.ndarray is not None:
                    self._latest_texture_data = output.ndarray
                    self._latest_gentl_buffer = buffer

    @staticmethod
    def _create_raw_buffers(num_buffers, size):
        # Instantiate a list object.
        raw_buffers = []

        # Append bytes objects to the list.
        # The number is specified by num_buffer and the buffer size is
        # specified by size.
        for _ in range(num_buffers):
            raw_buffers.append(bytes(size))

        # Then return the list.
        return raw_buffers

    @staticmethod
    def _create_buffer_tokens(raw_buffers):
        # Instantiate a list object.
        _buffer_tokens = []

        # Append Buffer Token object to the list.
        for i in range(len(raw_buffers)):
            _buffer_tokens.append(
                BufferToken(raw_buffers[i], i)
            )

        # Then returns the list.
        return _buffer_tokens

    def _announce_buffers(self, _buffer_tokens):
        #
        announced_buffers = []

        # Iterate announcing buffers in the Buffer Tokens.
        for i in range(len(_buffer_tokens)):
            # Get an announced buffer.
            announced_buffer = self._data_stream.announce_buffer(
                _buffer_tokens[i]
            )

            # And append it to the list.
            announced_buffers.append(announced_buffer)

        # Then return the list of announced Buffer objects.
        return announced_buffers

    def _queue_announced_buffers(self, buffers):
        for buffer in buffers:
            self._data_stream.queue_buffer(buffer)

    def stop_image_acquisition(self):

        if self.is_acquiring_images:
            #
            self._is_acquiring_images = False

            #
            self._thread_image_acquisition.stop()
            self._thread_image_acquisition.wait()

            self._thread_statistics_measurement.stop()
            self._thread_statistics_measurement.wait()

            with QMutexLocker(self._mutex):

                #
                self._event_manager.flush_event_queue()

                # Stop image acquisition.
                self._data_stream.stop_acquisition(
                    ACQ_STOP_FLAGS_LIST.ACQ_STOP_FLAGS_KILL
                )
                self.node_map.AcquisitionStop.execute()

                # Flash the queue for image acquisition process.
                self._data_stream.flush_buffer_queue(
                    ACQ_QUEUE_TYPE_LIST.ACQ_QUEUE_ALL_DISCARD
                )

                # Unregister the registered event.
                self._event_manager.unregister_event()

                #
                for i in range(len(self._announced_buffers)):
                    _ = self._data_stream.revoke_buffer(
                        self._announced_buffers[i]
                    )
                #
                self._data_stream.close()

                #
                self._event_manager = None
                self._announced_buffers = []
                self._data_stream = None
                self._latest_gentl_buffer = None

                for statistics in self._statistics_list:
                    statistics.reset()

    def initialize_acquisition_statistics(self):
        self._current_width = self.node_map.Width.value
        self._current_height = self.node_map.Height.value
        self._current_pixel_format = self.node_map.PixelFormat.value

    def add_file_path(self, file_path: str):
        if file_path not in self._cti_file_paths:
            self._cti_file_paths.append(file_path)

    def remove_file_path(self, file_path: str):
        if file_path in self._cti_file_paths:
            self._cti_file_paths.remove(file_path)

    def clear_file_paths(self):
        self._cti_file_paths = []

    def _open_gentl_producers(self):
        #
        for file_path in self._cti_file_paths:
            producer = GenTLProducer.create_producer()
            producer.open(file_path)
            self._producers.append(producer)

    def _open_systems(self):
        for producer in self._producers:
            system = producer.create_system()
            system.open()
            self._systems.append(system)

    def _release_gentl_producers(self):
        for producer in self._producers:
            if producer and producer.is_open():
                producer.close()
        self._producers = []

    def _release_systems(self):
        for system in self._systems:
            if system is not None and system.is_open():
                system.close()
        self._systems = []

    def _release_interfaces(self):
        if self._interfaces is not None:
            for iface in self._interfaces:
                if iface.is_open():
                    iface.close()
        self._interfaces = []

    def _release_device_info_list(self):
        if self.device_info_list is not None:
            self._device_info_list = []

    def initialize_device_info_list(self):
        self._open_gentl_producers()
        self._open_systems()
        self._update_device_list()

    def update_device_info_list(self):
        self._release_device_info_list()
        self._release_interfaces()
        self._update_device_list()

    def _update_device_list(self):
        for system in self._systems:
            #
            system.update_interface_info_list(self.timeout_for_update)

            #
            for i_info in system.interface_info_list:
                iface = i_info.create_interface()
                iface.open()
                iface.update_device_info_list(self.timeout_for_update)
                self._interfaces.append(iface)
                for d_info in iface.device_info_list:
                    self.device_info_list.append(d_info)

        #
        self._has_revised_list = True

    def release_all_resources(self):
        self.disconnect_device()
        self._release_device_info_list()
        self._release_interfaces()
        self._release_systems()
        self._release_gentl_producers()
        self.clear_file_paths()


if __name__ == '__main__':
    #
    my_app = QApplication(sys.argv)
    my_app.setWindowIcon(Icon('genicam_logo_i.png'))

    #
    with Harvester(frontend='PyQt') as harvester:
        harvester.gui.show()
        sys.exit(my_app.exec_())
