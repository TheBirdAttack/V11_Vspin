# v11_driver.py

import asyncio
import logging
import math
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Union

import pylabrobot
from pylabrobot.config import Config
from pylabrobot.centrifuge.vspin_backend import VSpinBackend


# =========================================================
# ZENTRALE KONFIGURATION
# =========================================================
SPIN_DURATION = 10

# From the two original-software sniff logs:
#   initial bucket 1: home 6733 -> target 12070
#   initial bucket 1: home 2661 -> target  7998
# Both are home + 5337. 5362 works mechanically on some units, but does not
# reproduce the original Access2 sequence.
BUCKET_1_OFFSET = 5337

TICKS_PER_REVOLUTION = 8000
BUCKET_2_OFFSET = TICKS_PER_REVOLUTION // 2
POSITION_TOLERANCE = 15

DEFAULT_READ_TIMEOUT = 0.20
STATUS_POLL_INTERVAL = 0.08
TACH_TO_RPM = -14.69320388
# =========================================================


config = Config(logging=Config.Logging(level=logging.DEBUG, log_dir=Path("my_logs")))
pylabrobot.configure(config)


CommandInput = Union[str, bytes, bytearray]


@dataclass
class StatusSnapshot:
    status: int
    current_position: int
    unknown1: int = 0
    tachometer: int = 0
    unknown2: int = 0
    home_position: int = 0
    checksum: int = 0
    raw: bytes = b""


def _with_checksum(cmd: bytes) -> bytes:
    """Repair the last byte of a VSpin command."""
    if len(cmd) <= 2 or cmd[0] != 0xAA:
        return cmd
    payload = cmd[1:-1]
    return b"\xaa" + payload + bytes([sum(payload) & 0xFF])


def _to_bytes(cmd: CommandInput) -> bytes:
    if isinstance(cmd, str):
        return bytes.fromhex(cmd)
    return bytes(cmd)


def _build_position_command(position: int) -> bytes:
    pos = int(position).to_bytes(4, byteorder="little", signed=False)
    payload = b"\x01\xd4\x97" + pos + bytes.fromhex("c3f52800d71a0000")
    return _with_checksum(b"\xaa" + payload + b"\x00")


def _build_spin_command(
    current_position: int,
    rpm: int,
    duration: float,
    acceleration: float = 0.8,
) -> tuple[bytes, int]:
    ticks_per_second = (int(rpm) / 60.0) * TICKS_PER_REVOLUTION
    accel_ticks_per_second2 = 12903.2 * float(acceleration)
    distance_accel = int(0.5 * (ticks_per_second**2) / accel_ticks_per_second2)
    distance_at_speed = ticks_per_second * float(duration)
    final_position = int(current_position + distance_accel + distance_at_speed)

    if final_position > 2**32 - 1:
        raise ValueError("Spin destination exceeds 32-bit VSpin position counter.")

    rpm_b = int(int(rpm) * 4473.925).to_bytes(4, byteorder="little", signed=False)
    accel_b = int(9.15 * 100 * float(acceleration)).to_bytes(
        4, byteorder="little", signed=False
    )
    pos_b = final_position.to_bytes(4, byteorder="little", signed=False)
    payload = b"\x01\xd4\x97" + pos_b + rpm_b + accel_b
    return _with_checksum(b"\xaa" + payload + b"\x00"), final_position


def _build_deceleration_command(deceleration: float = 0.8) -> bytes:
    decc = int(9.15 * 100 * float(deceleration)).to_bytes(
        2, byteorder="little", signed=False
    )
    return _with_checksum(bytes.fromhex("aa0194b600000000") + decc + b"\x00\x00\x00")


class BlindVSpinBackend(VSpinBackend):
    async def setup(self):
        try:
            print(f"\n[TREIBER] Setup start (Bucket-1 offset: {BUCKET_1_OFFSET})")
            await self.io.setup()

            self._command_lock = asyncio.Lock()
            self._ignore_pylabrobot_locks = False
            self._is_spinning = False
            self._stop_requested = False
            self.current_abs_pos = 0
            self.home_position = 0
            self.bucket_1_offset = int(BUCKET_1_OFFSET)
            self._motion_is_prepared = False

            await self.configure_and_initialize()
            await self._startup_handshake()
            await self._enable_telemetry_and_pneumatics()
            await self.rehome_and_sync()

            print("[TREIBER] Hardware bereit. GUI kann jetzt Bucket 1 anfahren.")

        except Exception as e:
            print(f"\n[FATALER FEHLER IM SETUP] {e}")
            traceback.print_exc()
            raise

    # =========================================================
    # LOW-LEVEL KOMMUNIKATION
    # =========================================================
    async def _send_safe(
        self,
        cmd_input: CommandInput,
        retries: int = 3,
        timeout: float = DEFAULT_READ_TIMEOUT,
        expect_response: bool = True,
        expected_len: Optional[int] = None,
    ) -> bytes:
        cmd = _to_bytes(cmd_input)

        for attempt in range(1, retries + 1):
            resp = await self._send_command(
                cmd, read_timeout=timeout, expected_len=expected_len
            )
            if resp or not expect_response:
                return resp

            print(
                f"[WARNUNG] Keine Antwort auf {cmd.hex()} "
                f"(Versuch {attempt}/{retries})"
            )
            await asyncio.sleep(0.15)

        raise TimeoutError(f"Keine Antwort auf Befehl {cmd.hex()}")

    async def _send_command(
        self,
        cmd: bytes,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
        expected_len: Optional[int] = None,
    ) -> bytes:
        if getattr(self, "_ignore_pylabrobot_locks", False):
            if cmd == bytes.fromhex("aa0226000028"):
                return bytes.fromhex("0008080010")
            if cmd == bytes.fromhex("aa0226200048"):
                return bytes.fromhex("0030083068")

        cmd = _with_checksum(bytes(cmd))
        expected_len = expected_len or self._expected_response_len(cmd)
        lock = self._get_command_lock()

        await asyncio.sleep(0.02)
        is_poll = cmd in (bytes.fromhex("aa010e0f"), bytes.fromhex("aa020e10"))
        if not is_poll:
            print(f"[LIVE] Sende: {cmd.hex()}")

        async with lock:
            written = await self.io.write(cmd)
            if written != len(cmd):
                raise RuntimeError(
                    f"FTDI write incomplete: {written}/{len(cmd)} bytes written"
                )
            resp = await self._read_available(
                timeout=read_timeout, expected_len=expected_len
            )

        if not is_poll:
            print(f"[LIVE] Antwort: {resp.hex() if resp else '(LEER)'}")
        return resp

    def _get_command_lock(self) -> asyncio.Lock:
        lock = getattr(self, "_command_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._command_lock = lock
        return lock

    @staticmethod
    def _expected_response_len(cmd: bytes) -> Optional[int]:
        if cmd == bytes.fromhex("aa010e0f"):
            return 14
        if cmd == bytes.fromhex("aa020e10"):
            return 5
        if cmd in (bytes.fromhex("aa002101ff21"), bytes.fromhex("aa002102ff22")):
            return 2
        if cmd in (bytes.fromhex("aa01132034"), bytes.fromhex("aa02132035")):
            return 4
        return None

    async def _read_available(
        self,
        timeout: float = DEFAULT_READ_TIMEOUT,
        expected_len: Optional[int] = None,
        quiet_time: float = 0.050,
    ) -> bytes:
        """Read binary VSpin responses without waiting for a CR byte.

        PyLabRobot's default reader waits for 0x0d. Most VSpin status replies in
        the sniffs are raw 2/5/14 byte packets and do not end in CR, which is the
        main source of avoidable timeouts in the old drivers.
        """
        data = b""
        started = time.monotonic()
        last_data = None

        while time.monotonic() - started < timeout:
            chunk = await self.io.read(25)
            if chunk:
                data += bytes(chunk)
                last_data = time.monotonic()
                if expected_len is not None and len(data) >= expected_len:
                    break
                continue

            if (
                data
                and expected_len is None
                and last_data is not None
                and time.monotonic() - last_data >= quiet_time
            ):
                break

            await asyncio.sleep(0.003)

        return data

    # =========================================================
    # LOG-NAHE INITIALISIERUNG
    # =========================================================
    async def configure_and_initialize(self):
        await self.set_configuration_data()
        for _ in range(2):
            await self.io.write(b"\x00" * 20)
            for i in range(33):
                packet = b"\xaa" + bytes([i & 0xFF, 0x0E, 0x0E + (i & 0xFF)]) + b"\x00" * 8
                await self.io.write(packet)
            await self._send_command(bytes.fromhex("aaff0f0e"), read_timeout=0.08)

    async def _startup_handshake(self):
        await self._send_safe("aa002101ff21", expected_len=2)
        await self._send_safe("aa01132034", expected_len=4)
        await self._send_safe("aa002102ff22", expected_len=2)
        await self._send_safe("aa02132035", expected_len=4)

        # The original software writes this and then accepts repeated read
        # timeouts for roughly two seconds. Do not retry it as a fatal command.
        await self._send_safe(
            "aa002103ff23",
            timeout=0.15,
            expect_response=False,
        )
        await self._drain_startup_silence(2.0)

        await self._send_safe("aaff1a142d", timeout=0.12, expect_response=False)

        # In the sniffs these two probes are transitional: depending on timing
        # they may return 2 bytes, 5 bytes, 14 bytes, or nothing. The stable
        # 14-byte mode is established by aa01121f32 below.
        await self._send_safe(
            "aa010e0f",
            timeout=0.30,
            expected_len=None,
            expect_response=False,
        )
        await self._send_safe(
            "aa020e10",
            timeout=0.30,
            expected_len=5,
            expect_response=False,
        )

        try:
            await self.io.set_baudrate(57600)
        except AttributeError:
            pass

        await self.io.set_rts(True)
        await self.io.set_dtr(True)

    async def _drain_startup_silence(self, seconds: float):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            await self._read_available(timeout=0.08, expected_len=None, quiet_time=0.01)
            await asyncio.sleep(0.03)

    async def _enable_telemetry_and_pneumatics(self):
        print("[SETUP] Aktiviere Telemetrie und Pneumatiksequenz...")

        await self._send_safe("aa01121f32", timeout=0.35, expected_len=14)

        for _ in range(8):
            await self._send_safe("aa0220ff0f30", timeout=0.12)

        for cmd in (
            "aa0220df0f10",
            "aa0220df0e0f",
            "aa0220df0c0d",
            "aa0220df0809",
        ):
            await self._send_safe(cmd, timeout=0.12)

        for _ in range(4):
            await self._send_safe("aa0226000028", timeout=0.12)

        await self._send_safe("aa02120317", timeout=0.12)

        for _ in range(5):
            await self._send_safe("aa0226200048", timeout=0.15)
            await self._send_safe("aa020e10", timeout=0.12, expected_len=5)
            await self._send_safe("aa0226000028", timeout=0.15)
            await self._send_safe("aa020e10", timeout=0.12, expected_len=5)

        await self._send_safe("aa020e10", timeout=0.12, expected_len=5)
        await self._send_safe("aa0226000129", timeout=0.15)
        await self._poll_io_status(0.35)
        await self._send_safe("aa0226000028", timeout=0.15)
        await self._poll_io_status(0.35)
        self._motion_is_prepared = True

    async def _poll_io_status(self, seconds: float):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            await self._send_safe("aa020e10", timeout=0.10, expected_len=5)
            await asyncio.sleep(0.04)

    async def _motor_enable(self):
        await self._send_safe("aa0117021a", timeout=0.30, expected_len=14)
        await self._send_safe(
            "aa01e6c800b00496000f004b00a00f050007",
            timeout=0.30,
            expected_len=14,
        )
        await self._send_safe("aa0117041c", timeout=0.30, expected_len=14)
        await self._send_safe("aa01170119", timeout=0.30, expected_len=14)
        await self._send_safe("aa010b0c", timeout=0.30, expected_len=14)

    # =========================================================
    # STATUS UND WARTEN
    # =========================================================
    async def _get_positions_and_tachometer(self) -> StatusSnapshot:
        resp = await self._send_command(
            bytes.fromhex("aa010e0f"),
            read_timeout=0.25,
            expected_len=14,
        )
        status = self._parse_status(resp)
        self.current_abs_pos = int(status.current_position)
        self.home_position = int(status.home_position)
        return status

    def _parse_status(self, resp: bytes) -> StatusSnapshot:
        if resp:
            candidate = self._find_status_packet(resp)
            if candidate is not None:
                return candidate

            short_status = self._find_short_status(resp)
            if short_status is not None:
                return StatusSnapshot(
                    status=short_status,
                    current_position=int(getattr(self, "current_abs_pos", 0)),
                    home_position=int(getattr(self, "home_position", 0)),
                    raw=resp,
                )

        return StatusSnapshot(
            status=0x19,
            current_position=int(getattr(self, "current_abs_pos", 0)),
            home_position=int(getattr(self, "home_position", 0)),
            raw=resp,
        )

    @staticmethod
    def _find_status_packet(resp: bytes) -> Optional[StatusSnapshot]:
        known_status = {0x08, 0x09, 0x11, 0x18, 0x19, 0x88, 0x89, 0x91, 0x99}

        for start in range(0, max(0, len(resp) - 13)):
            packet = resp[start : start + 14]
            if len(packet) < 14 or packet[0] not in known_status:
                continue
            checksum_ok = (sum(packet[:-1]) & 0xFF) == packet[-1]
            if not checksum_ok:
                continue

            return StatusSnapshot(
                status=packet[0],
                current_position=int.from_bytes(packet[1:5], "little", signed=False),
                unknown1=packet[5],
                tachometer=int.from_bytes(packet[6:8], "little", signed=True),
                unknown2=packet[8],
                home_position=int.from_bytes(packet[9:13], "little", signed=False),
                checksum=packet[13],
                raw=packet,
            )

        return None

    @staticmethod
    def _find_short_status(resp: bytes) -> Optional[int]:
        known_status = {0x08, 0x09, 0x11, 0x18, 0x19, 0x88, 0x89, 0x91, 0x99}

        if len(resp) == 5 and resp[0] == 0x00 and (sum(resp[:-1]) & 0xFF) == resp[-1]:
            if resp[2] in known_status:
                return resp[2]

        for idx, value in enumerate(resp):
            if value not in known_status:
                continue
            if idx + 1 < len(resp) and resp[idx + 1] == value:
                return value
            if len(resp) == 1:
                return value

        return None

    async def _wait_for_idle(
        self,
        label: str = "Bewegung",
        timeout: float = 30.0,
        target_position: Optional[int] = None,
        tolerance: int = POSITION_TOLERANCE,
    ) -> StatusSnapshot:
        start = time.monotonic()
        last_status = None
        last_report = 0.0

        while True:
            status = await self._get_positions_and_tachometer()
            last_status = status

            is_idle_status = status.status in (0x09, 0x11, 0x91)
            is_stopped = abs(status.tachometer) <= 2
            is_at_target = (
                target_position is None
                or abs(int(status.current_position) - int(target_position)) <= tolerance
            )

            if is_idle_status and is_stopped and is_at_target:
                print(
                    f"[IDLE] {label}: status=0x{status.status:02x}, "
                    f"pos={status.current_position}, home={status.home_position}"
                )
                return status

            now = time.monotonic()
            if now - last_report >= 3.0:
                print(
                    f"[WAIT] {label}: status=0x{status.status:02x}, "
                    f"pos={status.current_position}, tach={status.tachometer}, "
                    f"home={status.home_position}, raw={status.raw.hex() or '(leer)'}"
                )
                last_report = now

            if time.monotonic() - start > timeout:
                raise TimeoutError(
                    f"{label} nicht idle nach {timeout:.1f}s "
                    f"(status=0x{status.status:02x}, pos={status.current_position}, "
                    f"tach={status.tachometer}, home={status.home_position})"
                )

            await asyncio.sleep(STATUS_POLL_INTERVAL)

    async def _get_status(self) -> bytes:
        return await self._send_command(
            bytes.fromhex("aa020e10"),
            read_timeout=0.18,
            expected_len=5,
        )

    async def get_position(self) -> int:
        return (await self._get_positions_and_tachometer()).current_position

    async def get_home_position(self) -> int:
        return (await self._get_positions_and_tachometer()).home_position

    async def get_tachometer(self) -> float:
        return (await self._get_positions_and_tachometer()).tachometer * TACH_TO_RPM

    async def get_door_open(self) -> bool:
        resp = await self._get_status()
        if len(resp) < 3:
            raise IOError(f"Ungueltiger Tuerstatus: {resp.hex()}")
        return (resp[2] & 0b0010) != 0

    # =========================================================
    # HOMING UND POSITIONIERUNG
    # =========================================================
    async def rehome_and_sync(self):
        print("\n[INFO] Suche Nullpunkt (Homing)...")
        await self._motor_enable()
        await self._send_safe("aa010001", timeout=0.30, expected_len=14)
        await self._send_safe(
            "aa01e605006400000000003200e80301006e",
            timeout=0.30,
            expected_len=14,
        )
        await self._send_safe("aa0194b61283000012010000f3", timeout=0.30, expected_len=14)
        await self._send_safe("aa01192842", timeout=0.30, expected_len=14)

        status = await self._wait_for_idle(label="Homing", timeout=35.0)
        if status.home_position == 0:
            print("[INFO] Homing idle, warte auf volles 14-Byte-Statuspaket...")
            await self._send_safe(
                "aa01121f32",
                timeout=0.35,
                expected_len=14,
                expect_response=False,
            )
            status = await self._wait_for_full_status(timeout=5.0)

        self.current_abs_pos = int(status.current_position)
        self.home_position = int(status.home_position)
        self.bucket_1_offset = int(BUCKET_1_OFFSET)

        print(
            f"[INFO] Homing fertig. Sensor/Home={self.home_position}, "
            f"Motor={self.current_abs_pos}, Bucket1=Home+{self.bucket_1_offset}."
        )

    async def _wait_for_full_status(self, timeout: float) -> StatusSnapshot:
        end = time.monotonic() + timeout
        last_raw = b""

        while time.monotonic() < end:
            resp = await self._send_command(
                bytes.fromhex("aa010e0f"),
                read_timeout=0.40,
                expected_len=14,
            )
            last_raw = resp
            status = self._find_status_packet(resp)
            if status is not None and status.home_position != 0:
                self.current_abs_pos = int(status.current_position)
                self.home_position = int(status.home_position)
                return status
            await asyncio.sleep(0.10)

        raise TimeoutError(
            "Homing ist idle, aber es kommt kein vollstaendiges 14-Byte-"
            f"Statuspaket mit Home-Position. Letzte Rohdaten: {last_raw.hex() or '(leer)'}"
        )

    async def go_to_position(self, position: int):
        position = int(position)
        print(f"\n[BEFEHL] Motorfahrt zu Koordinate {position}...")
        await self._motor_enable()
        await self._send_safe("aa01e6c800b00496000f004b00a00f050007", timeout=0.20)
        await self._send_safe(_build_position_command(position), timeout=0.25)
        await self._wait_for_idle(
            label=f"Position {position}",
            timeout=25.0,
            target_position=position,
        )

    async def go_to_bucket(self, bucket_num: int):
        if bucket_num not in (1, 2):
            raise ValueError("bucket_num muss 1 oder 2 sein")

        target = await self._get_bucket_target(bucket_num)
        home = int(getattr(self, "home_position", 0))
        print(f"\n[MATHEMATIK] Bucket {bucket_num}")
        print(f"[MATHEMATIK] Home/Sensor: {home}")
        print(f"[MATHEMATIK] Offset: {self.bucket_1_offset}")
        print(f"[MATHEMATIK] Ziel: {target}")

        if not getattr(self, "_motion_is_prepared", False):
            await self._prepare_bucket_motion()

        await self.go_to_position(target)
        await self.index_rotor()

    async def go_to_bucket1(self):
        await self.go_to_bucket(1)

    async def go_to_bucket2(self):
        await self.go_to_bucket(2)

    async def _get_bucket_target(self, bucket_num: int) -> int:
        status = await self._get_positions_and_tachometer()
        home = int(status.home_position or getattr(self, "home_position", 0))
        current = int(status.current_position)

        if home == 0:
            # During the first few setup polls home is zero. Force a real homing
            # result before we calculate a bucket target.
            await self.rehome_and_sync()
            status = await self._get_positions_and_tachometer()
            home = int(status.home_position)
            current = int(status.current_position)

        target = home + int(self.bucket_1_offset)
        if bucket_num == 2:
            target += BUCKET_2_OFFSET

        while target <= current + POSITION_TOLERANCE:
            target += TICKS_PER_REVOLUTION

        return target

    async def _prepare_bucket_motion(self):
        await self._send_safe("aa0226000129", timeout=0.20)
        await self._poll_io_status(0.25)
        await self._send_safe("aa0226000028", timeout=0.20)
        await self._poll_io_status(0.25)
        self._motion_is_prepared = True

    async def index_rotor(self):
        print("\n[PNEUMATIK] Rotor/Bucket arretieren...")
        await self._send_safe("aa0117021a", timeout=0.20)
        await self._send_safe("aa0226000129", timeout=0.25)
        await self._poll_io_status(0.35)
        self._motion_is_prepared = False

    async def unindex_rotor(self):
        print("\n[PNEUMATIK] Rotor/Bucket freigeben...")
        await self._send_safe("aa0226200048", timeout=0.25)
        await self._poll_io_status(0.25)
        self._motion_is_prepared = True

    # =========================================================
    # TUER
    # =========================================================
    async def open_door(self):
        print("\n[PNEUMATIK] Tuer auf...")
        try:
            if await self.get_door_open():
                print("[PNEUMATIK] Tuer ist bereits offen.")
                return
        except Exception:
            pass

        await self._send_safe("aa022600072f", timeout=0.30)
        await self._wait_for_door(open_expected=True, timeout=4.0)

    async def close_door(self):
        print("\n[PNEUMATIK] Tuer zu...")
        try:
            if not await self.get_door_open():
                print("[PNEUMATIK] Tuer ist bereits geschlossen.")
                return
        except Exception:
            pass

        await self._send_safe("aa022600052d", timeout=0.30)
        await self._wait_for_door(open_expected=False, timeout=4.0)
        self._motion_is_prepared = False

    async def _wait_for_door(self, open_expected: bool, timeout: float):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                if await self.get_door_open() is open_expected:
                    return
            except Exception:
                pass
            await asyncio.sleep(0.12)

    # =========================================================
    # SPIN
    # =========================================================
    async def custom_spin(self, rpm: int, duration: float):
        rpm = int(rpm)
        duration = float(duration)
        acceleration = 0.8
        deceleration = 0.8

        if rpm < 1 or rpm > 3000:
            raise ValueError("RPM muss zwischen 1 und 3000 liegen.")
        if duration < 1:
            raise ValueError("Spin-Dauer muss mindestens 1 Sekunde sein.")

        print(f"\n[SPIN] Starte {rpm} RPM fuer {duration:.1f}s.")
        self._stop_requested = False
        self._is_spinning = True
        self._ignore_pylabrobot_locks = False

        try:
            await self._prepare_spin_motion()
            await self._motor_enable()
            await self._send_safe("aa01e60500640000000000fd00803e01000c", timeout=0.25)

            current = await self.get_position()
            spin_cmd, final_position = _build_spin_command(
                current_position=current,
                rpm=rpm,
                duration=duration,
                acceleration=acceleration,
            )
            await self._send_safe(spin_cmd, timeout=0.25)
            print(f"[SPIN] Zielposition: {final_position}")

            await self._wait_for_speed_or_motion(rpm=rpm, final_position=final_position)
            await self._hold_spin(duration)

            print("\n[SPIN] Bremse mit 80% Deceleration.")
            await self._send_deceleration(deceleration)

        except asyncio.CancelledError:
            await self._send_deceleration(0.8)
            raise
        except Exception as e:
            print(f"\n[FEHLER] Spin abgebrochen: {e}")
            traceback.print_exc()
            await self._send_deceleration(0.8)
            raise
        finally:
            self._ignore_pylabrobot_locks = False
            self._is_spinning = False

        await self._wait_for_idle(label="Spin-Auslauf", timeout=90.0)
        await self.rehome_and_sync()
        await self.go_to_bucket(1)

    async def _prepare_spin_motion(self):
        await self._send_safe("aa0226000129", timeout=0.20)
        await self._poll_io_status(0.40)
        await self._send_safe("aa0226000028", timeout=0.20)
        await self._poll_io_status(0.30)
        self._motion_is_prepared = True

    async def _wait_for_speed_or_motion(self, rpm: int, final_position: int):
        deadline = time.monotonic() + 25.0
        while time.monotonic() < deadline and not self._stop_requested:
            status = await self._get_positions_and_tachometer()
            live_rpm = int(status.tachometer * TACH_TO_RPM)
            print(
                f"\r[SPIN] Beschleunigung | RPM ~{live_rpm} | "
                f"pos {status.current_position}",
                end="",
            )

            if live_rpm >= rpm * 0.92:
                print()
                return
            if status.current_position >= final_position:
                print()
                return

            await asyncio.sleep(0.25)
        print()

    async def _hold_spin(self, duration: float):
        started = time.monotonic()
        while not self._stop_requested and time.monotonic() - started < duration:
            status = await self._get_positions_and_tachometer()
            live_rpm = int(status.tachometer * TACH_TO_RPM)
            elapsed = int(time.monotonic() - started)
            print(f"\r[SPIN] Laufzeit: {elapsed}/{int(duration)}s | RPM ~{live_rpm}", end="")
            await asyncio.sleep(1.0)
        print()

    async def _send_deceleration(self, deceleration: float):
        await self._send_safe("aa01e60500640000000000fd00803e01000c", timeout=0.25)
        await self._send_safe(_build_deceleration_command(deceleration), timeout=0.25)

    async def stop_spin_async(self):
        print("\n[BEFEHL] STOP gedrueckt. Bremse...")
        self._stop_requested = True
        self._is_spinning = False
        await self._send_deceleration(0.8)

    async def stop(self):
        try:
            await self.io.stop()
        except AttributeError:
            pass
