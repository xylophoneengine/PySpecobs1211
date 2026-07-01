"""Pure-Python driver for the JETI specbos 1211 spectroradiometer.

Communicates directly with the device's SCPI-compatible firmware over USB
serial (via FTDI) or TCP, without the Windows-only JETI SDK.

Typical usage::

    from specbos1211 import Specbos1211

    with Specbos1211.from_serial() as dev:
        dev.configure(wbeg=380, wend=780, step=1)
        print(dev.firmware_version())
        spd = dev.measure('sprad')          # shape: (401,) in W sr^-1 m^-2 nm^-1
        wavelengths = dev.wavelengths       # shape: (401,) in nm
"""

from __future__ import annotations

import select
import socket
import time
from typing import Literal

import numpy as np
import serial
import serial.tools.list_ports as list_ports


class JetiError(Exception):
    """Raised for any communication or protocol error with the specbos 1211.

    Covers NAK responses from the device, read/write timeouts, unexpected
    protocol bytes, failed device identification, and auto-detect failures.
    """


class _SerialTransport:
    """Low-level USB serial transport layer for the specbos 1211.

    Owns and manages a ``serial.Serial`` port. Translates high-level
    protocol operations (send, ACK/NAK, BELL, CR-delimited lines) into
    raw byte I/O over the FTDI USB-serial adapter.

    Device framing conventions:
        - Commands are ASCII strings terminated with CR (0x0D, ``\\r``).
        - ACK byte: 0x06.  NAK byte: 0x15.  BELL byte: 0x07.
        - Spectral data blocks end with ``\\r\\r``; scalar responses with ``\\r``.
    """

    def __init__(self, port: str, baud: int, timeout: float) -> None:
        """Open and configure the serial port.

        Args:
            port: OS device path, e.g. ``/dev/tty.usbserial-XXXX`` on macOS.
            baud: Baud rate. The specbos 1211 defaults to 921600.
            timeout: Per-read blocking timeout in seconds. Applies to every
                individual ``serial.Serial.read()`` call; not a wall-clock
                deadline for a complete operation.

        Raises:
            serial.SerialException: If the port cannot be opened (missing,
                in use, or insufficient permissions).
        """
        self._ser = serial.Serial(
            port=port,
            baudrate=baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
            timeout=timeout,
        )

    def send(self, cmd: str) -> None:
        """Send an ASCII command string terminated with CR.

        The firmware requires CR (0x0D) as the command terminator; CRLF is
        not accepted and will cause a NAK.

        Args:
            cmd: Command string without any terminator, e.g. ``'*IDN?'``.

        Raises:
            serial.SerialException: If the write fails at the OS level.
        """
        self._ser.write((cmd + '\r').encode('ascii'))

    def _read_byte(self) -> int:
        """Read exactly one byte from the serial port.

        Returns:
            The byte value as an integer (0-255).

        Raises:
            JetiError: If no byte arrives before the port timeout expires.
        """
        b = self._ser.read(1)
        if not b:
            raise JetiError('timeout waiting for response')
        return b[0]

    def read_ack(self) -> None:
        """Read the device's ACK/NAK response byte after a command.

        Expects 0x06 (ACK). On 0x15 (NAK), automatically queries the
        human-readable error string from ``*STAT:TXTERR?`` before raising.

        Raises:
            JetiError: If the device returns NAK (includes device error text),
                if no byte arrives within the timeout, or if an unexpected
                byte is received.
        """
        b = self._read_byte()
        if b == 0x06:
            return
        if b == 0x15:
            self.send('*STAT:TXTERR?')
            msg = self.read_line()
            raise JetiError(f'device NAK: {msg}')
        raise JetiError(f'unexpected response byte: 0x{b:02x}')

    def read_until_bell(self) -> None:
        """Block until the device sends a BELL byte (0x07).

        The specbos 1211 sends BELL after a measurement integration period
        completes, signalling that the spectral or radiometric data is ready
        to read.

        Raises:
            JetiError: If the port timeout expires before BELL arrives.
        """
        while True:
            b = self._ser.read(1)
            if not b:
                raise JetiError('timeout waiting for BELL')
            if b[0] == 0x07:
                return

    def read_lines(self) -> list[str]:
        """Read a spectral data block terminated by a double CR (``\\r\\r``).

        Accumulates bytes until ``\\r\\r`` is seen, then splits on ``\\r``
        and strips whitespace from each non-empty token.

        Returns:
            List of non-empty, whitespace-stripped ASCII strings, one per
            data line in the block.

        Raises:
            JetiError: If the port timeout expires before ``\\r\\r`` arrives.
        """
        buf = b''
        while not buf.endswith(b'\r\r'):
            chunk = self._ser.read(1)
            if not chunk:
                raise JetiError('timeout reading spectral data')
            buf += chunk
        return [s.decode('ascii').strip() for s in buf.split(b'\r') if s.strip()]

    def read_line(self) -> str:
        """Read a single CR-terminated ASCII line.

        Used for scalar responses (firmware version, radiometric totals,
        error messages). Strips the trailing CR and surrounding whitespace.

        Returns:
            The decoded, stripped response string.

        Raises:
            JetiError: If the port timeout expires before CR arrives.
        """
        buf = b''
        while not buf.endswith(b'\r'):
            chunk = self._ser.read(1)
            if not chunk:
                raise JetiError('timeout reading line')
            buf += chunk
        return buf.rstrip(b'\r').decode('ascii').strip()

    def data_waiting(self, timeout: float = 0.0) -> bool:
        """Check whether unread bytes are available in the receive buffer.

        Used to detect whether the firmware has sent an additional response
        line, e.g. the second line of the version string on older firmware.

        Args:
            timeout: Optional pause in seconds before checking, to give the
                device time to transmit. Defaults to 0 (immediate check).

        Returns:
            ``True`` if at least one byte is waiting in the OS receive buffer.
        """
        if timeout > 0:
            time.sleep(timeout)
        return self._ser.in_waiting > 0

    def close(self) -> None:
        """Release the serial port back to the OS.

        Safe to call multiple times; subsequent calls are no-ops if the port
        is already closed.
        """
        self._ser.close()


class _TCPTransport:
    """Low-level TCP/IP transport layer for the specbos 1211 LAN interface.

    Owns and manages a blocking TCP socket connected to the device's
    built-in Ethernet adapter. Exposes the same protocol interface as
    ``_SerialTransport``; ``Specbos1211`` treats both transports identically.

    The device listens on TCP port 2101 by default (configurable via the
    front-panel menu). See *Operating Instructions scb1211.pdf*, section 2.3.
    """

    def __init__(self, host: str, port: int, timeout: float) -> None:
        """Connect a TCP socket to the device.

        Args:
            host: IPv4 address or hostname of the specbos 1211, e.g.
                ``'192.168.1.100'``.
            port: TCP port the device is listening on. Defaults to 2101.
            timeout: Socket-level timeout in seconds applied to every
                blocking recv/send call.

        Raises:
            OSError: If the connection is refused or the host is unreachable.
        """
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(timeout)
        self._sock.connect((host, port))

    def send(self, cmd: str) -> None:
        """Send an ASCII command string terminated with CR.

        Args:
            cmd: Command string without any terminator, e.g. ``'*MEAS:SPRAD'``.

        Raises:
            OSError: If the underlying ``sendall`` call fails.
        """
        self._sock.sendall((cmd + '\r').encode('ascii'))

    def _recv(self, n: int) -> bytes:
        """Receive exactly ``n`` bytes from the socket.

        Args:
            n: Number of bytes to receive.

        Returns:
            A ``bytes`` object of length ``n``.

        Raises:
            JetiError: If the socket times out or the device closes the
                connection before ``n`` bytes are received.
        """
        try:
            data = self._sock.recv(n)
        except (socket.timeout, TimeoutError) as e:
            raise JetiError(f'timeout reading from device: {e}') from e
        if not data:
            raise JetiError('connection closed by device')
        return data

    def read_ack(self) -> None:
        """Read the device's ACK/NAK response byte after a command.

        Expects 0x06 (ACK). On 0x15 (NAK), automatically queries the
        human-readable error string from ``*STAT:TXTERR?`` before raising.

        Raises:
            JetiError: If the device returns NAK (includes device error text),
                if the socket times out, or if an unexpected byte is received.
        """
        b = self._recv(1)
        if b[0] == 0x06:
            return
        if b[0] == 0x15:
            self.send('*STAT:TXTERR?')
            msg = self.read_line()
            raise JetiError(f'device NAK: {msg}')
        raise JetiError(f'unexpected response byte: 0x{b[0]:02x}')

    def read_until_bell(self) -> None:
        """Block until the device sends a BELL byte (0x07).

        The specbos 1211 sends BELL after integration completes, signalling
        that data is ready to read.

        Raises:
            JetiError: If the socket times out before BELL arrives.
        """
        while True:
            b = self._recv(1)
            if b[0] == 0x07:
                return

    def read_lines(self) -> list[str]:
        """Read a spectral data block terminated by a double CR (``\\r\\r``).

        Returns:
            List of non-empty, whitespace-stripped ASCII strings, one per
            data line in the block.

        Raises:
            JetiError: If the socket times out before ``\\r\\r`` arrives.
        """
        buf = b''
        while not buf.endswith(b'\r\r'):
            buf += self._recv(1)
        return [s.decode('ascii').strip() for s in buf.split(b'\r') if s.strip()]

    def read_line(self) -> str:
        """Read a single CR-terminated ASCII line.

        Returns:
            The decoded, stripped response string.

        Raises:
            JetiError: If the socket times out before CR arrives.
        """
        buf = b''
        while not buf.endswith(b'\r'):
            buf += self._recv(1)
        return buf.rstrip(b'\r').decode('ascii').strip()

    def data_waiting(self, timeout: float = 0.0) -> bool:
        """Check whether the socket has data ready to read.

        Uses ``select`` so the call never blocks beyond ``timeout`` seconds.

        Args:
            timeout: How long (in seconds) to wait for data to appear.
                Defaults to 0 (non-blocking poll).

        Returns:
            ``True`` if at least one byte is available to receive immediately.
        """
        r, _, _ = select.select([self._sock], [], [], timeout)
        return bool(r)

    def close(self) -> None:
        """Close the TCP socket.

        Safe to call multiple times.
        """
        self._sock.close()


def _find_serial_port() -> str:
    """Auto-detect the specbos 1211 USB serial port by FTDI VID/PID.

    Scans all available serial ports for an FTDI device with VID=0x0403 and
    PID=0x6001, which is the USB-serial adapter embedded in the specbos 1211
    hardware.

    Returns:
        The OS device path of the matched port, e.g.
        ``'/dev/tty.usbserial-XXXX'``.

    Raises:
        JetiError: If no FTDI device is found, or if more than one is found
            (ambiguous; caller must pass ``port=`` explicitly).
    """
    FTDI_VID = 0x0403
    FTDI_PID = 0x6001
    candidates = [
        p.device for p in list_ports.comports()
        if p.vid == FTDI_VID and p.pid == FTDI_PID
    ]
    if len(candidates) == 0:
        raise JetiError('no specbos 1211 found (no FTDI device at VID=0x0403/PID=0x6001)')
    if len(candidates) > 1:
        raise JetiError(
            f'multiple FTDI devices found: {candidates} -- pass port= explicitly'
        )
    return candidates[0]


class Specbos1211:
    """Driver for the JETI specbos 1211 spectroradiometer.

    Communicates over USB serial or TCP using the device's SCPI-compatible
    firmware protocol. The driver is transport-agnostic: construct via
    ``from_serial()`` or ``from_tcp()`` and the rest of the API is identical.

    Intended to be used as a context manager so the connection is always
    cleanly closed::

        with Specbos1211.from_serial() as dev:
            dev.configure(wbeg=380, wend=780, step=1)
            spd = dev.measure('sprad')

    Attributes:
        wavelengths: Wavelength axis (nm) matching the last ``configure()``
            call, or ``None`` if ``configure()`` has not been called yet.
    """

    def __init__(self, transport: _SerialTransport | _TCPTransport) -> None:
        """Wrap an already-opened transport.

        Prefer the class methods ``from_serial()`` and ``from_tcp()`` over
        calling this constructor directly.

        Args:
            transport: An open ``_SerialTransport`` or ``_TCPTransport``
                instance. The ``Specbos1211`` object takes exclusive ownership.
        """
        self._transport = transport
        # Wavelength range parameters, populated by configure().
        self._wbeg: int | None = None
        self._wend: int | None = None
        self._step: int | None = None

    @classmethod
    def from_serial(
        cls,
        port: str | None = None,
        baud: int = 921600,
        timeout: float = 60.0,
    ) -> 'Specbos1211':
        """Create a driver connected via USB serial.

        Args:
            port: OS device path to the serial port, e.g.
                ``'/dev/tty.usbserial-XXXX'``. If ``None``, the port is
                auto-detected by scanning for an FTDI VID=0x0403/PID=0x6001
                device.
            baud: Baud rate. Defaults to 921600, which is the specbos 1211
                factory default.
            timeout: Per-read timeout in seconds. Must be long enough to
                cover the longest expected integration time. Defaults to 60 s.

        Returns:
            A ``Specbos1211`` instance with an open (but not yet verified)
            serial connection. Call ``connect()`` or use as a context manager
            to verify device identity before measuring.

        Raises:
            JetiError: If auto-detection finds zero or multiple FTDI devices,
                or if ``baud`` or ``timeout`` is invalid.
            serial.SerialException: If the specified port cannot be opened.
        """
        _VALID_BAUDS = {38400, 115200, 921600}
        if baud not in _VALID_BAUDS:
            raise JetiError(
                f'baud={baud} is not supported: must be one of {sorted(_VALID_BAUDS)}'
            )
        if timeout <= 0:
            raise JetiError(f'timeout={timeout} must be > 0')
        if port is None:
            port = _find_serial_port()
        return cls(_SerialTransport(port, baud, timeout))

    @classmethod
    def from_tcp(
        cls,
        host: str,
        port: int = 2101,
        timeout: float = 60.0,
    ) -> 'Specbos1211':
        """Create a driver connected via TCP/IP (LAN interface).

        Args:
            host: IPv4 address or hostname of the specbos 1211 Ethernet port.
            port: TCP port the device listens on. Factory default is 2101,
                configurable via the front-panel menu.
            timeout: Socket-level timeout in seconds. Defaults to 60 s.

        Returns:
            A ``Specbos1211`` instance with an open TCP connection. Call
            ``connect()`` or use as a context manager before measuring.

        Raises:
            JetiError: If ``host`` is empty, ``port`` is outside 1..65535,
                or ``timeout`` is not positive.
            OSError: If the TCP connection cannot be established.
        """
        if not host:
            raise JetiError('host must be a non-empty string')
        if not (1 <= port <= 65535):
            raise JetiError(f'port={port} is out of range: must be 1..65535')
        if timeout <= 0:
            raise JetiError(f'timeout={timeout} must be > 0')
        return cls(_TCPTransport(host, port, timeout))

    def connect(self) -> None:
        """Verify the connection by querying and checking the device identity.

        Sends ``*IDN?`` and confirms the response contains ``'JETI'`` or
        ``'SB1211'``. This is called automatically by ``__enter__``; call it
        explicitly when not using the context manager.

        Raises:
            JetiError: If the IDN response does not identify a specbos 1211.
        """
        self._transport.send('*IDN?')
        idn = self._transport.read_line()
        if 'JETI' not in idn and 'SB1211' not in idn:
            raise JetiError(f'not a specbos 1211: IDN response was {idn!r}')

    def disconnect(self) -> None:
        """Close the underlying transport.

        Releases the serial port or TCP socket. Called automatically by
        ``__exit__``; safe to call multiple times.
        """
        self._transport.close()

    def __enter__(self) -> 'Specbos1211':
        """Enter the context manager: open and verify the connection.

        Returns:
            The ``Specbos1211`` instance itself.

        Raises:
            JetiError: If the device identity check fails (see ``connect()``).
        """
        self.connect()
        return self

    def __exit__(self, *_) -> None:
        """Exit the context manager: close the transport unconditionally."""
        self.disconnect()

    @property
    def wavelengths(self) -> np.ndarray | None:
        """Wavelength axis (nm) matching the current spectral configuration.

        Computed from the ``wbeg``, ``wend``, and ``step`` values passed to
        the last ``configure()`` call. The length equals the number of
        spectral samples returned by ``measure('sprad')`` or
        ``measure('raw')``.

        Returns:
            A 1-D float64 NumPy array of wavelengths in nm, or ``None`` if
            ``configure()`` has not been called yet.
        """
        if self._wbeg is None:
            return None
        return np.arange(self._wbeg, self._wend + self._step, self._step, dtype=float)

    def configure(
        self,
        wbeg: int = 380,
        wend: int = 780,
        step: int = 1,
        averages: int = 1,
        tint: float = 0.0,
        max_tint: float = 60000.0,
    ) -> None:
        """Push measurement configuration to the device.

        Sends a sequence of ``*CONF:*`` commands to set the wavelength range,
        spectral step size, number of averages, integration time mode, and
        output format. All parameters are written atomically as far as the
        firmware is concerned; a NAK on any sub-command raises immediately.

        Integration time modes:

        - **Auto** (``tint=0.0``): The firmware selects the integration time
          automatically up to ``max_tint`` milliseconds (``*CONF:EXPO 1``).
        - **Fixed** (``tint>0``): The firmware uses exactly ``tint``
          milliseconds (``*CONF:EXPO 2``).

        The output format is always set to 4 (floating-point ASCII) so that
        ``_parse_floats`` can decode it.

        Args:
            wbeg: Start wavelength in nm (inclusive). Must be an integer in
                200..1099. Defaults to 380.
            wend: End wavelength in nm (inclusive). Must be an integer in
                201..1100 and strictly greater than ``wbeg``. Defaults to 780.
            step: Wavelength step size in nm. Must be an integer in 1..10.
                Defaults to 1.
            averages: Number of exposures to average per measurement. Must be
                an integer in 1..10000. Defaults to 1.
            tint: Fixed integration time in milliseconds. Must be 0.0 for
                auto-exposure or a value in 1..60000 for fixed exposure.
                Defaults to 0.0 (auto).
            max_tint: Auto-exposure ceiling in milliseconds. Must be in
                1000..60000. Ignored when ``tint > 0``. Defaults to 60000.

        Raises:
            JetiError: If any argument fails a type or range check, or if the
                device returns NAK for any configuration command.
        """
        if not isinstance(wbeg, int):
            raise JetiError(f'wbeg must be an int, got {type(wbeg).__name__}')
        if not isinstance(wend, int):
            raise JetiError(f'wend must be an int, got {type(wend).__name__}')
        if not isinstance(step, int):
            raise JetiError(f'step must be an int, got {type(step).__name__}')
        if not isinstance(averages, int):
            raise JetiError(f'averages must be an int, got {type(averages).__name__}')
        if not isinstance(tint, (int, float)):
            raise JetiError(f'tint must be a number, got {type(tint).__name__}')
        if not isinstance(max_tint, (int, float)):
            raise JetiError(f'max_tint must be a number, got {type(max_tint).__name__}')
        if not (200 <= wbeg <= 1099):
            raise JetiError(f'wbeg={wbeg} is out of range: firmware requires 200..1099 nm')
        if not (201 <= wend <= 1100):
            raise JetiError(f'wend={wend} is out of range: firmware requires 201..1100 nm')
        if wbeg >= wend:
            raise JetiError(f'wbeg={wbeg} must be strictly less than wend={wend}')
        if not (1 <= step <= 10):
            raise JetiError(f'step={step} is out of range: firmware requires 1..10 nm')
        if not (1 <= averages <= 10000):
            raise JetiError(f'averages={averages} is out of range: firmware requires 1..10000')
        if tint != 0.0 and not (1 <= tint <= 60000):
            raise JetiError(f'tint={tint} is out of range: firmware requires 1..60000 ms (or 0.0 for auto)')
        if tint == 0.0 and not (1000 <= max_tint <= 60000):
            raise JetiError(f'max_tint={max_tint} is out of range: firmware requires 1000..60000 ms')

        if tint == 0.0:
            # Auto-exposure: firmware picks integration time up to max_tint.
            self._transport.send('*CONF:EXPO 1')
            self._transport.read_ack()
            self._transport.send(f'*CONF:MAXTINT {int(max_tint)}')
            self._transport.read_ack()
        else:
            # Fixed integration time: firmware uses exactly tint ms.
            self._transport.send('*CONF:EXPO 2')
            self._transport.read_ack()
            self._transport.send(f'*CONF:TINT {int(tint)}')
            self._transport.read_ack()

        self._transport.send(f'*CONF:WRAN {wbeg} {wend} {step}')
        self._transport.read_ack()
        self._transport.send(f'*CONF:AVER {averages}')
        self._transport.read_ack()
        self._transport.send('*CONF:FORM 4')
        self._transport.read_ack()

        self._wbeg = wbeg
        self._wend = wend
        self._step = step

    @staticmethod
    def _parse_floats(lines: list[str]) -> np.ndarray:
        """Convert a list of ASCII strings to a float64 NumPy array.

        Non-numeric lines (e.g. unit-label headers sent by firmware >=3.0)
        are silently ignored, so callers do not need to pre-filter the list.

        Args:
            lines: Raw ASCII lines from ``read_lines()``, possibly mixed with
                non-numeric label strings.

        Returns:
            A 1-D float64 NumPy array containing only the successfully parsed
            numeric values. May be empty if no numeric line is found.
        """
        # Newer firmware prepends a unit-label line before the float data; skip it.
        values = []
        for v in lines:
            try:
                values.append(float(v))
            except ValueError:
                pass
        return np.array(values)

    def measure(
        self,
        quantity: Literal['sprad', 'raw', 'radio'] = 'sprad',
    ) -> np.ndarray:
        """Trigger a measurement and return the result as a NumPy array.

        The device performs integration (duration depends on ``configure()``
        settings), then streams the result. The call blocks until data is
        fully received.

        Supported quantities:

        - **``'sprad'``** -- Spectral radiance in W sr^-1 m^-2 nm^-1.
          Returns a 1-D array with one value per wavelength step.  Use
          ``wavelengths`` for the corresponding nm axis.
          Command: ``*MEAS:SPRAD``.

        - **``'raw'``** -- Raw ADC counts (dimensionless detector signal).
          Temporarily switches the output format to 9 (raw), measures, then
          restores format 4. Same shape as ``'sprad'``.
          Command: ``*MEAS`` with ``*CONF:FORM 9``.

        - **``'radio'``** -- Integrated radiometric total in W sr^-1 m^-2
          (a scalar). Returns a 0-D float64 array.
          Command: ``*MEAS:RADIO``.

        Note:
            Firmware >=3.0 sends a unit-label ``\\r\\r`` block before the
            numeric data block. This method automatically detects and skips
            that extra block.

        Args:
            quantity: Which quantity to measure. Must be ``'sprad'``,
                ``'raw'``, or ``'radio'``. Defaults to ``'sprad'``.

        Returns:
            For ``'sprad'`` and ``'raw'``: a 1-D float64 array of length
            ``(wend - wbeg) / step + 1``.
            For ``'radio'``: a scalar ``np.float64``.

        Raises:
            JetiError: If ``quantity`` is not one of the accepted values, if
                the device returns NAK, if a timeout occurs during integration
                or data transfer, or if the response cannot be parsed.
        """
        _VALID_QUANTITIES = {'sprad', 'raw', 'radio'}
        if quantity not in _VALID_QUANTITIES:
            raise JetiError(
                f'quantity={quantity!r} is invalid: must be one of {sorted(_VALID_QUANTITIES)}'
            )

        if quantity == 'sprad':
            self._transport.send('*MEAS:SPRAD')
            self._transport.read_ack()
            self._transport.read_until_bell()
            result = self._parse_floats(self._transport.read_lines())
            # Firmware >=3.0 sends a unit-label block (\r\r) then the data block (\r\r).
            if result.size == 0:
                result = self._parse_floats(self._transport.read_lines())
            return result

        elif quantity == 'raw':
            self._transport.send('*CONF:FORM 9')
            self._transport.read_ack()
            try:
                self._transport.send('*MEAS')
                self._transport.read_ack()
                self._transport.read_until_bell()
                result = self._parse_floats(self._transport.read_lines())
                if result.size == 0:
                    result = self._parse_floats(self._transport.read_lines())
            finally:
                # Always restore format 4 so subsequent sprad calls work correctly.
                self._transport.send('*CONF:FORM 4')
                self._transport.read_ack()
            return result

        elif quantity == 'radio':
            self._transport.send('*MEAS:RADIO')
            self._transport.read_ack()
            self._transport.read_until_bell()
            line = self._transport.read_line()
            value = float(line.split(':')[-1].strip())
            return np.float64(value)

    def firmware_version(self) -> str:
        """Query the device firmware version string.

        Handles a firmware-version incompatibility: firmware <=1.49 sends the
        version as two separate CR-terminated lines, while >=3.0 sends it as
        a single combined line. Both cases are normalised to a single string.

        Returns:
            Firmware version string, e.g. ``'V3.01'`` or
            ``'V1.49 / build 20210603'``.

        Raises:
            JetiError: If the device times out before responding.
        """
        self._transport.send('*VERS?')
        line1 = self._transport.read_line()
        # Firmware <=1.49 sends two CR-terminated lines; >=3.0 sends one combined line.
        if self._transport.data_waiting(0.05):
            return f'{line1} / {self._transport.read_line()}'
        return line1

    def reset(self) -> None:
        """Send a hardware reset command and wait for the device to restart.

        Issues ``*RST`` and blocks for 500 ms to allow the firmware to
        complete its restart sequence before further commands are sent.

        Raises:
            JetiError: If the device NAKs the reset command.
        """
        self._transport.send('*RST')
        self._transport.read_ack()
        time.sleep(0.5)
