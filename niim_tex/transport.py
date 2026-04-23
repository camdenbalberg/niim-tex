"""Transport layer — USB serial and BLE backends for NIIMBOT printers."""

import serial
import serial.tools.list_ports

from .protocol import packet, parse_response


def find_niimbot_usb():
    """Find a NIIMBOT printer on USB serial. Returns port name or None."""
    for p in serial.tools.list_ports.comports():
        if p.vid == 13587 or "niimbot" in (p.description or "").lower():
            return p.device
    return None


class UsbTransport:
    """USB serial transport for NIIMBOT printers.

    Same packet protocol as BLE, but over a serial port. Much faster
    because there's no BLE connection interval overhead.
    """

    def __init__(self, port=None):
        self.port = port or find_niimbot_usb()
        self.ser = None

    def connect(self):
        if not self.port:
            raise RuntimeError("No NIIMBOT USB printer found. Is it plugged in?")
        self.ser = serial.Serial(self.port, baudrate=115200, timeout=2)
        # Read and discard any stale data
        self.ser.reset_input_buffer()
        return self.port

    def disconnect(self):
        if self.ser and self.ser.is_open:
            self.ser.close()

    @property
    def is_connected(self):
        return self.ser is not None and self.ser.is_open

    def command(self, cmd, data, timeout=5):
        """Send a command and read the response."""
        pkt = packet(cmd, data)
        self.ser.write(pkt)
        self.ser.flush()

        # Read response — accumulate until we find a complete packet
        old_timeout = self.ser.timeout
        self.ser.timeout = timeout
        buf = b""
        while True:
            chunk = self.ser.read(1)
            if not chunk:
                break
            buf += chunk
            # Check if we have a complete packet (ends with 0xAA 0xAA)
            if len(buf) >= 7 and buf[-2:] == b"\xaa\xaa":
                break

        self.ser.timeout = old_timeout
        return parse_response(buf)

    def write_raw(self, data):
        """Send raw bytes without waiting for a response (for image rows)."""
        self.ser.write(data)
        # Flush every write to prevent OS buffering from delaying data
        self.ser.flush()
