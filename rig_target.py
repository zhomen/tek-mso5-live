"""Real Tektronix MSO58 driver for tek-mso5-live.

Talks SCPI over the scope's socket server (TCP port 4000). A background
thread refreshes the readings about twice a second; the publisher's poll
reads those cached values, so a slow network never stalls it. Control
writes go straight to the scope and are read back.

On connect the driver adds two measurements on CH1 (frequency and
peak-to-peak), which the operator approved. Readings are NaN when the
scope has no valid result or the link is down -- never a stale value.
"""

import math
import socket
import threading
import time

import numpy as np

HOST = "10.233.69.122"
PORT = 4000
REFRESH_S = 0.5
TRACE_POINTS = 1000
INVALID = 9.0e37  # the scope reports 9.91E37 when a measurement has no result


class MSO5:
    def __init__(self, host=HOST, port=PORT):
        self._addr = (host, port)
        self._lock = threading.Lock()
        self._sock = None
        self._file = None
        self._meas = {}
        self.idn = ""
        self.meas_frequency = math.nan
        self.meas_vpp = math.nan
        self.ch1_trace = np.full(TRACE_POINTS, math.nan)
        self.last_error = ""
        self._ch1_scale = math.nan
        self._horizontal_scale = math.nan
        self._run = 0
        self._stop = threading.Event()
        self._thread = None
        self._pending = []  # queued control writes (list ops are atomic in CPython)

    # ---- link ----
    def _send(self, cmd):
        self._sock.sendall((cmd + "\n").encode())

    def _query(self, cmd):
        self._send(cmd)
        return self._file.readline().decode(errors="replace").strip()

    def _query_block(self, cmd):
        """Read an IEEE 488.2 definite-length binary block (#<n><len><data>)."""
        self._send(cmd)
        if self._file.read(1) != b"#":
            raise IOError("bad binary block header")
        n = int(self._file.read(1))
        length = int(self._file.read(n))
        data = self._file.read(length)  # buffered read: returns all bytes or raises at EOF
        if len(data) != length:
            raise IOError("short binary block")
        self._file.readline()  # trailing newline
        return data

    def _open(self):
        self._sock = socket.create_connection(self._addr, timeout=5)
        self._file = self._sock.makefile("rb")  # buffered, so read(n) returns n bytes
        self._send("HEADER 0")
        self._send("VERBOSE 1")
        self.idn = self._query("*IDN?")

    def connect(self):
        with self._lock:
            self._open()
            self._setup_measurements()
            self._send("DATA:SOURCE CH1")
            self._send("DATA:ENCDG RIBINARY")
            self._send("WFMOUTPRE:BYT_NR 1")
            self._send("DATA:START 1")
            self._send("DATA:STOP " + self._query("HORIZONTAL:RECORDLENGTH?"))
            self._read_controls()
        self._refresh()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        self._apply_pending()  # a final stop queued at shutdown still reaches the scope
        with self._lock:
            self._drop()

    def _setup_measurements(self):
        """Find or add CH1 FREQUENCY and PK2PK measurements."""
        for kind in ("FREQUENCY", "PK2PK"):
            name = self._find_meas(kind)
            if name is None:
                before = set(self._meas_names())
                self._send("MEASUREMENT:ADDMEAS " + kind)
                self._query("*OPC?")
                name = (set(self._meas_names()) - before).pop()
                self._send(f"MEASUREMENT:{name}:SOURCE1 CH1")
            self._meas[kind] = name

    def _meas_names(self):
        listing = self._query("MEASUREMENT:LIST?")
        return [] if listing.upper() == "NONE" else listing.split(",")

    def _find_meas(self, kind):
        for name in self._meas_names():
            t = self._query(f"MEASUREMENT:{name}:TYPE?").upper()
            src = self._query(f"MEASUREMENT:{name}:SOURCE1?").upper()
            if t.startswith(kind[:4]) and src == "CH1":
                return name
        return None

    def _read_controls(self):
        self._ch1_scale = float(self._query("CH1:SCALE?"))
        self._horizontal_scale = float(self._query("HORIZONTAL:SCALE?"))
        self._run = int(self._query("ACQUIRE:STATE?"))

    # ---- background refresh ----
    def _loop(self):
        while not self._stop.is_set():
            self._apply_pending()
            self._refresh()
            self._apply_pending()
            self._stop.wait(REFRESH_S)

    def _apply_pending(self):
        """Send queued control writes; the I/O thread is the only scope talker."""
        while self._pending:
            try:
                cmd, query, attr, cast = self._pending.pop(0)
            except IndexError:
                return
            try:
                with self._lock:
                    if self._sock is None:
                        self._open()
                    self._send(cmd)
                    self._query("*OPC?")
                    setattr(self, attr, cast(self._query(query)))
            except (OSError, ValueError) as e:
                self.last_error = f"{type(e).__name__}: {e}"
                self._drop()

    def _refresh(self):
        try:
            with self._lock:
                if self._sock is None:
                    self._open()
                freq = self._meas_value("FREQUENCY")
                vpp = self._meas_value("PK2PK")
            self._apply_pending()  # a stop never waits behind the trace download
            with self._lock:
                trace = self._fetch_trace()
            self.meas_frequency, self.meas_vpp, self.ch1_trace = freq, vpp, trace
            self.last_error = ""
        except (OSError, ValueError) as e:
            self.last_error = f"{type(e).__name__}: {e}"
            self.meas_frequency = self.meas_vpp = math.nan
            self.ch1_trace = np.full(TRACE_POINTS, math.nan)
            self._drop()

    def _drop(self):
        try:
            if self._sock:
                self._sock.close()
        finally:
            self._sock = self._file = None

    def _meas_value(self, kind):
        v = float(self._query(f"MEASUREMENT:{self._meas[kind]}:RESULTS:CURRENTACQ:MEAN?"))
        return math.nan if abs(v) > INVALID else v

    def _fetch_trace(self):
        ymult = float(self._query("WFMOUTPRE:YMULT?"))
        yoff = float(self._query("WFMOUTPRE:YOFF?"))
        yzero = float(self._query("WFMOUTPRE:YZERO?"))
        raw = np.frombuffer(self._query_block("CURVE?"), dtype=np.int8)
        volts = (raw.astype(float) - yoff) * ymult + yzero
        # Thin the full record to TRACE_POINTS evenly spaced samples.
        idx = np.linspace(0, len(volts) - 1, TRACE_POINTS).round().astype(int)
        return volts[idx]

    # ---- controls (setters are assigned by the publisher on change) ----
    # Setters only queue the command, so the publisher's poll never blocks
    # on the network; the I/O thread sends it and stores the scope's readback.
    def _write(self, cmd, query, attr, cast):
        self._pending.append((cmd, query, attr, cast))

    @property
    def ch1_scale(self):
        return self._ch1_scale

    @ch1_scale.setter
    def ch1_scale(self, v):
        self._write(f"CH1:SCALE {float(v):.6E}", "CH1:SCALE?", "_ch1_scale", float)

    @property
    def horizontal_scale(self):
        return self._horizontal_scale

    @horizontal_scale.setter
    def horizontal_scale(self, v):
        self._write(f"HORIZONTAL:SCALE {float(v):.6E}", "HORIZONTAL:SCALE?",
                    "_horizontal_scale", float)

    @property
    def run(self):
        return self._run

    @run.setter
    def run(self, v):
        self._write(f"ACQUIRE:STATE {1 if int(v) else 0}", "ACQUIRE:STATE?", "_run", int)


class Bench:
    def __init__(self):
        self.scope = MSO5()


def build_target():
    bench = Bench()
    bench.scope.connect()
    return bench
