import os
import crccheck.checksum
import serial
from pyScienceMode2.enums import Type
import time
import threading
from pyScienceMode2.utils import *
import numpy as np

# Notes :
# This code needs to be used in parallel with the "ScienceMode2 - Description and protocol" document


class RehastimGeneric:
    """
    Class used for the communication with Rehastim.

    Attributes
    ----------
    packet_count : int
        Contain the number of packet sent to the Rehastim since the Init.
    port : class Serial
        Used to control the COM port.
    debug_reha_show_log : bool
        Tell if the log will be displayed (True) or not (False).
    debug_reha_show_com : bool
        Tell if the communication will be displayed (True) or not (False). Except watchdog.
    debug_reha_show_watchdog : bool
        Tell if the watchdog will be displayed (True) or not (False).
    time_last_cmd : int
        Time of the last command which was sent to the Rehastim.
    packet_send_history : list[int]
        Last packet sent to the Rehastim. Used for error and debugging purposes.
    reha_connected : bool
        Tell if the computer is connected (True) to the Rehastim or not (False).
    multiple_packet_flag : int
        Flag raised when multiple packet are waiting in the port COM. The methode _check_multiple_packet_rec() needs to
        be used after each call of _calling_ack() or wait_for_packet() in order to process those packets.
    buffer_rec : list[int]
        Contain the packet receive which has not been processed.
    __thread_watchdog: threading.Thread
        ID of the thread responsible for sending regularly a watchdog.

    Class Attributes
    ----------------
    VERSION : int
        version of software of Rehastim.
    START_BYTE : int
        Start byte of protocol. (Science Mode2 Description Protocol, 2.2 Packet structure)
    STOP_BYTE : int
        Stop byte of protocol.
    STUFFING_BYTE : int
        Stuffing byte of protocol.
    STUFFING_KEY : int
        Stuffing key of protocol.
    MAX_PACKET_BYTES : int
        Number max of bytes per packet in protocol.
    BAUD_RATE : int
        Baud rate of protocol.
    TYPES : dict
        Dictionary which contains Rehastim commands for protocol and their values. (Motomed command are not implemented)
    RECEIVE, SEND, ERR : int
        Packet type. Used for _packet_show() method.
    """

    VERSION = 0x01

    START_BYTE = 0xF0
    STOP_BYTE = 0x0F
    STUFFING_BYTE = 0x81
    STUFFING_KEY = 0x55
    MAX_PACKET_BYTES = 69
    STUFFED_BYTES = [240, 15, 129, 85, 10]

    BAUD_RATE = 460800

    def __init__(self, port, show_log=False, with_motomed: bool = False):
        self.port = serial.Serial(
            port,
            self.BAUD_RATE,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_EVEN,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.1,
        )
        self.port_open = True
        self.debug_reha_show_log = False
        self.debug_reha_show_com = False
        self.debug_reha_show_watchdog = False
        self.time_last_cmd = 0
        self.packet_count = 0
        self.reha_connected = False
        self.show_log = show_log

        self.time_last_cmd = 0
        self.packet_send_history = []

        self.multiple_packet_flag = 0
        self.buffer_rec = []
        self.read_port_time = 0.0
        self.last_ack = None
        self.last_init_ack = None
        self.motomed_values = None
        self.last_phase_result = None
        self._motomed_command_done = True
        self.is_motomed_connected = with_motomed
        self.max_motomed_values = 100
        self.max_phase_result = 1
        self.__wait_for_ack = False
        self.__thread_watchdog = None
        self.lock = threading.Lock()
        self.event = threading.Event()
        self.event_ack = threading.Event()
        if self.is_motomed_connected:
            self._start_motomed_thread()
        self.Type = Type

    def _get_last_ack(self, init=False):
        if self.is_motomed_connected:
            if init:
                while not self.last_init_ack:
                    pass
                last_ack = self.last_init_ack
                self.last_init_ack = None
            else:
                while not self.last_ack:
                    pass
                last_ack = self.last_ack
                self.last_ack = None
            return last_ack
        else:
            while 1:
                packet = self._read_packet()
                if packet and len(packet) != 0:
                    break
            if self.show_log and self.Type(packet[-1][6]).value in [t.value for t in self.Type]:
                print(f"Ack received by rehastim: {self.Type(packet[-1][6]).name}")
            return packet[-1]

    def _start_motomed_thread(self):
        """
        Start the thread which sends watchdog.
        """
        if self.debug_reha_show_log:
            print("Start Motomed data thread")
        self.__thread_motomed = threading.Thread(target=self._catch_motomed_data)
        self.__thread_motomed.start()

    def _catch_motomed_data(self):
        time_to_sleep = 0.05
        while 1 and self.is_motomed_connected:
            packets = self._read_packet()
            tic = time.time()
            if packets:
                for packet in packets:
                    if len(packet) > 7:
                        if self.show_log and packet[6] in [t.value for t in self.Type]:
                            print(f"Ack received by rehastim: {self.Type(packet[6]).name}")
                        if packet[6] == self.Type["PhaseResult"].value:
                            self._phase_result_ack(packet)
                        elif packet[6] == self.Type["ActualValues"].value:
                            self._actual_values_ack(packet)
                        elif packet[6] == 90:
                            pass
                        elif packet[6] == self.Type["MotomedCommandDone"].value:
                            self.event.set()
                        elif packet[6] in [t.value for t in self.Type]:
                            if packet[6] == 1:
                                self.last_init_ack = packet
                                self.event_ack.set()
                            else:
                                self.last_ack = packet
                                self.event_ack.set()

            loop_duration = tic - time.time()
            time.sleep(time_to_sleep - loop_duration)

    def _actual_values_ack(self, packet: (list, str)):
        # handle the LSB and MSB and stuffed bytes
        count = 0
        if packet[8] == 129:
            angle = 255 * signed_int(packet[7:8]) + packet[9] ^ self.STUFFING_KEY
            count += 1
        else:
            angle = 255 * signed_int(packet[7:8]) + packet[8]

        if packet[10 + count] == 129:
            speed = signed_int(packet[10 + count + 1:10 + count + 2]) ^ self.STUFFING_KEY
            count += 1
        else:
            speed = signed_int(packet[10:11])

        if packet[12 + count] == 129:
            torque = signed_int(packet[12 + count + 1:12 + count + 2]) ^ self.STUFFING_KEY
            count += 1
        else:
            torque = signed_int(packet[12:13])

        actual_values = np.array([angle, speed, torque])[:, np.newaxis]
        if self.motomed_values is None:
            self.motomed_values = actual_values
        elif self.motomed_values.shape[1] < self.max_motomed_values:
            self.motomed_values = np.append(self.motomed_values, actual_values, axis=1)
        else:
            self.motomed_values = np.append(self.motomed_values[:, 1:], actual_values, axis=1)

    def _phase_result_ack(self, packet: (list, str)):
        msb_passive_distance = int(packet[8])
        lsb_passive_distance = int(packet[9])
        msb_active_distance = int(packet[10])
        lsb_active_distance = int(packet[11])
        average_power = int(packet[12])
        maximum_power = int(packet[13])
        msb_phase_duration = int(packet[14])
        lsb_phase_duration = int(packet[15])
        msb_active_phase_duration = int(packet[16])
        lsb_active_phase_duration = int(packet[17])
        msb_phase_work = int(packet[18])
        lsb_phase_work = int(packet[19])
        success_value = int(packet[20])
        symmetry = int(packet[21])
        average_muscle_tone = int(packet[22])

        passive_distance = msb_passive_distance * 255 + lsb_passive_distance
        active_distance = None
        phase_duration = None
        active_phase_duration = None
        phase_work = None
        last_phase_result = np.array(
            [
                passive_distance,
                active_distance,
                average_power,
                maximum_power,
                phase_duration,
                active_phase_duration,
                phase_work,
                success_value,
                symmetry,
                average_muscle_tone,
            ]
        )[:, np.newaxis]

        if self.last_phase_result is None:
            self.last_phase_result = last_phase_result
        elif self.last_phase_result.shape[1] < self.max_phase_result:
            self.last_phase_result = np.append(self.last_phase_result, last_phase_result, axis=1)
        else:
            self.last_phase_result = np.append(self.last_phase_result[:, 1:], last_phase_result, axis=1)

    def _watchdog(self):
        """
        Send a watchdog if the last command send by the pc was more than 500ms ago and if the rehastim is connected.
        """
        while 1 and self.is_connected():
            if time.time() - self.time_last_cmd > 0.5:
                self._send_generic_packet("Watchdog", packet=[], packet_number=self.packet_count)
                time.sleep(0.5)

    def is_connected(self) -> bool:
        """
        Checks if the pc and the rehastim are connected.

        Returns
        -------
        True if connected, False if not.
        """
        if self.time_last_cmd - time.time() > 1.2 or not self.reha_connected:
            self.reha_connected = False
            return False
        else:
            return True

    def _send_generic_packet(self, cmd: str, packet: list, packet_number: int = None):
        """ """
        if cmd == "InitAck":
            packet = self._init_ack(packet_number)
            self.event.set()
            self._start_watchdog()
        elif cmd == "Watchdog":
            packet = self._packet_watchdog()
        if self.show_log:
            if self.Type(packet[6]).name != "Watchdog":
                print(f"Command sent to Rehastim : {self.Type(packet[6]).name}")
        self.lock.acquire()
        self.port.write(packet)
        if cmd == "InitAck":
            self.reha_connected = True
        # if self.is_motomed_connected and cmd != "Watchdog":
        #     self.event.wait()
        self.lock.release()
        self.time_last_cmd = time.time()
        self.packet_send_history = packet
        self.packet_count = (self.packet_count + 1) % 256
        self.event_ack.clear()
        self.event.clear()
        if cmd == "InitAck":
            self.event.set()
            return "InitAck"

    def _init_ack(self, packet_count: int) -> bytes:
        """
        Returns the packet corresponding to an InitAck.

        Parameters
        ----------
        packet_count: int
             Packet number of Rehastim.

        Returns
        -------
        packet: list
            Packet corresponding to an InitAck.
        """
        packet = self._packet_construction(packet_count, "InitAck", [0])
        return packet

    def _packet_construction(self, packet_count: int, packet_type: str, packet_data: list = None) -> bytes:
        """
        Constructs the packet which will be sent to the Rehastim.

        Parameters
        ----------
        packet_count: int
            Correspond to the number of packet sent to the Rehastim since the Init if the packet that will be sent.
            In the case of an acknowledged of a command of the Rehastim. The packet_count represent the number of packet
            that the rehastim has sent to the pc since the init.
        packet_type: str
            Correspond to the command that will be sent. The str is coded into an int with the class attributes TYPES.
        packet_data: list[int]
            Contain the data of the packet.

        Returns
        -------
        packet_construct: bytes
            Packet constructed which will be sent.
        """
        packet = [self.START_BYTE]
        packet_command = self.Type[packet_type].value
        packet_payload = [packet_count, packet_command]
        packet_payload = self._stuff_packet_byte(packet_payload)
        if packet_data is not None:
            packet_data = self._stuff_packet_byte(packet_data, command_data=True)
            packet_payload += packet_data

        checksum = crccheck.crc.Crc8.calc(packet_payload)
        checksum = self._stuff_byte(checksum)
        data_length = self._stuff_byte(len(packet_payload))

        packet = packet + [self.STUFFING_BYTE] + [checksum] + [self.STUFFING_BYTE] + [data_length] + packet_payload + [self.STOP_BYTE]
        return b"".join([byte.to_bytes(1, "little") for byte in packet])

    def _stuff_packet_byte(self, packet: list, command_data=False) -> list:
        """
        Stuffs each byte if necessary of the packet.

        Parameters
        ----------
        packet: list[int]
            Packet containing the bytes that will be checked and stuffed if necessary.

        Returns
        -------
        packet: list[int]
            Packet returned with stuffed byte.
        """
        # Stuff the byte equal to 0xf0 (240), 0x0f (15), 0x81(129), 0x55 (85) and 0x0a(10)
        # (for more details check : Science_Mode2_Description_Protocol_20121212.pdf, 2.2 PacketStructure)

        if command_data:
            packet_tmp = []
            for i in range(len(packet)):
                if packet[i] in self.STUFFED_BYTES:
                    packet_tmp = packet_tmp + [self.STUFFING_BYTE, self._stuff_byte(packet[i])]
                else:
                    packet_tmp.append(packet[i])
            return packet_tmp
        else:
            for i in range(len(packet)):
                if packet[i] in self.STUFFED_BYTES:
                    packet[i] = (self._stuff_byte(packet[i]))
            return packet

    def _stuff_byte(self, byte: int) -> int:
        """
        Stuffs the byte given. (Science_Mode2_Description_Protocol, 2.2 Packet Structure)

        Parameters
        ----------
        byte: int
            Byte which needs to be stuffed.

        Returns
        -------
        byte_stuffed: int
            The byte stuffed.
        """
        # return bytes(a ^ b for (a, b) in zip(byte, bitarray(self.STUFFING_KEY)))
        return (byte & ~self.STUFFING_KEY) | (~byte & self.STUFFING_KEY)

    def close_port(self):
        """
        Closes the port.
        """
        self.port.close()

    def _read_packet(self):
        """
        Processes packet(s) that are waiting in the port.
        If there is only one packet, the packet is returned.
        If they are multiples packet, the first one is returned, the other are stored into self.buffer_rec and a flag
        (self.multiple_packet_flag) is raised.
        If the data received does not start with a START_BYTE, an empty packet is returned.

        Returns
        -------
        The first packet received or an empty packet if the data does not start with a START_BYTE
        """
        packet = bytes()
        while True:
            packet_tmp = self.port.read(self.port.inWaiting())
            if len(packet_tmp) != 0:
                packet += packet_tmp
            else:
                if packet and packet[-1] == self.STOP_BYTE:
                    break
        packet_list = []
        if len(packet) > 8:
            first_start_byte = packet.index(self.START_BYTE)
            packet_tmp = packet[first_start_byte:]
            while len(packet_tmp) != 0:
                next_stop_byte = packet_tmp.index(self.STOP_BYTE)
                packet_list.append(packet_tmp[:next_stop_byte + 1])
                packet_tmp = packet_tmp[next_stop_byte + 1:]
            return packet_list

    def disconnect(self):
        """
        Disconnect the pc to the Rehastim by stopping sending watchdog.
        """
        self._stop_watchdog()
        if self.is_motomed_connected:
            self._stop_motomed()

    def _start_watchdog(self):
        """
        Start the thread which sends watchdog.
        """
        if self.debug_reha_show_log:
            print("Start Watchdog")
        self.reha_connected = True
        self.__thread_watchdog = threading.Thread(target=self._watchdog)
        self.__thread_watchdog.start()

    def _stop_watchdog(self):
        """
        Stop the thread which sends watchdog.
        """
        self.reha_connected = False
        self.__thread_watchdog.join()

    def _stop_motomed(self):
        """
        Stop the thread which sends watchdog.
        """
        self.is_motomed_connected = False
        self.__thread_motomed.join()

    def _packet_watchdog(self) -> bytes:
        """
        Constructs the watchdog packet.

        Returns
        -------
        packet: list
            Packet corresponding to the watchdog
        """
        packet = self._packet_construction(self.packet_count, "Watchdog")
        return packet
