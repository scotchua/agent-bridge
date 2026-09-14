"""Conservative macOS resource sampling for the local queue runtime."""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import time
from typing import Callable

from .spool import ResourceSnapshot

CommandRunner = Callable[[list[str]], str]


def system_runner(argv: list[str]) -> str:
    return subprocess.check_output(argv, text=True, stderr=subprocess.DEVNULL, timeout=2)


def foundation_thermal_state() -> str:
    """Read NSProcessInfo.thermalState without compiling or third-party packages."""
    ctypes.CDLL("/System/Library/Frameworks/Foundation.framework/Foundation")
    objc = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.objc_getClass.argtypes = [ctypes.c_char_p]
    objc.sel_registerName.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]
    send = objc.objc_msgSend
    send.restype = ctypes.c_void_p
    send.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    process = send(objc.objc_getClass(b"NSProcessInfo"), objc.sel_registerName(b"processInfo"))
    if not process:
        raise RuntimeError("NSProcessInfo unavailable")
    send.restype = ctypes.c_long
    value = send(process, objc.sel_registerName(b"thermalState"))
    if value == 0:
        return "normal"
    if value in {1, 2, 3}:
        return "high"
    return "unknown"


class MacSampler:
    """Read only macOS status probes. Unknown data fails queue admission closed.

    macOS does not expose a stable, universal thermal-state command suitable for
    this service.  Supply a site-reviewed ``thermal_probe`` that returns
    ``normal`` only when it has fresh evidence; without one this sampler reports
    ``unknown``.  The foundation consequently defers bulk work (and conservatively
    interactive work) rather than pretending the device is cool.
    """

    def __init__(self, runner: CommandRunner = system_runner,
                 thermal_probe: Callable[[], str] | None = foundation_thermal_state,
                 cores_probe: Callable[[], int | None] = os.cpu_count,
                 clock: Callable[[], float] = time.time):
        self.runner, self.thermal_probe, self.cores_probe, self.clock = runner, thermal_probe, cores_probe, clock
        self.last_details: dict[str, object] = {}

    @staticmethod
    def _memory(text: str) -> str:
        match = re.search(r"memory\s+free\s+percentage:\s*(\d+(?:\.\d+)?)%", text, re.I)
        if not match:
            raise ValueError("memory_pressure output not recognized")
        return "normal" if float(match.group(1)) >= 20.0 else "high"

    @staticmethod
    def _ac_power(text: str) -> bool:
        return bool(re.search(r"Now drawing from ['\"]AC Power['\"]", text))

    @staticmethod
    def _idle_seconds(text: str) -> float:
        match = re.search(r"[\"']?HIDIdleTime[\"']?\s*=\s*(\d+)", text)
        if not match:
            raise ValueError("ioreg output not recognized")
        return int(match.group(1)) / 1_000_000_000

    @staticmethod
    def _load(text: str) -> float:
        match = re.search(r"load averages?:\s*([0-9]+(?:\.[0-9]+)?)", text, re.I)
        if not match:
            raise ValueError("uptime output not recognized")
        return float(match.group(1))

    def sample(self) -> ResourceSnapshot:
        memory = self.runner(["/usr/bin/memory_pressure", "-Q"])
        battery = self.runner(["/usr/bin/pmset", "-g", "batt"])
        idle = self.runner(["/usr/sbin/ioreg", "-c", "IOHIDSystem"])
        load = self.runner(["/usr/bin/uptime"])
        cores = self.cores_probe()
        thermal = self.thermal_probe() if self.thermal_probe else "unknown"
        if thermal not in {"normal", "high", "unknown"}:
            raise ValueError("thermal probe returned invalid state")
        self.last_details = {"load_1": self._load(load), "cpu_cores": int(cores or 0),
                             "thermal_source": "configured" if self.thermal_probe else "unavailable"}
        if self.last_details["cpu_cores"] <= 0:
            raise ValueError("invalid core count")
        return ResourceSnapshot(self.clock(), self._memory(memory), thermal,
                                self._ac_power(battery), self._idle_seconds(idle),
                                self.last_details["load_1"] / self.last_details["cpu_cores"])
