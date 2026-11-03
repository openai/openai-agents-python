"""Observe only agents._native's Schannel trust call in the isolated TLS test.

Only the system-roots test child may load this context. The context must stay
open until the real Session and its cancellation have joined.
This file deliberately has no module, process, import, or policy selector.
"""

from __future__ import annotations

import asyncio
import ctypes as c
import hashlib
import os
import struct
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

# Fixed-width Windows fields: ctypes.wintypes.DWORD would be host-long on Linux.
U32 = c.c_uint32
I32 = c.c_int32
PTR = c.c_void_p
CERT_CHAIN_POLICY_SSL = 4
CERT_E_UNTRUSTEDROOT = 0x800B0109
CERT_E_CHAINING = 0x800B010A
PAGE_READWRITE = 0x04


class TrustStatus(c.Structure):
    _fields_ = [("dwErrorStatus", U32), ("dwInfoStatus", U32)]


class ChainContextPrefix(c.Structure):
    _fields_ = [
        ("cbSize", U32),
        ("TrustStatus", TrustStatus),
        ("cChain", U32),
        ("rgpChain", PTR),
    ]


class SimpleChainPrefix(c.Structure):
    _fields_ = [
        ("cbSize", U32),
        ("TrustStatus", TrustStatus),
        ("cElement", U32),
        ("rgpElement", PTR),
    ]


class ChainElementPrefix(c.Structure):
    _fields_ = [("cbSize", U32), ("pCertContext", PTR)]


class CertificateContext(c.Structure):
    _fields_ = [
        ("dwCertEncodingType", U32),
        ("pbCertEncoded", PTR),
        ("cbCertEncoded", U32),
        ("pCertInfo", PTR),
        ("hCertStore", PTR),
    ]


class ChainPolicyStatus(c.Structure):
    _fields_ = [
        ("cbSize", U32),
        ("dwError", U32),
        ("lChainIndex", I32),
        ("lElementIndex", I32),
        ("pvExtraPolicyStatus", PTR),
    ]


def _fixture_leaf_matches(chain_pointer: int, expected_der: bytes, digest: bytes) -> bool:
    """Read only the leaf of the first evaluated simple chain, after CryptoAPI."""
    if not chain_pointer:
        return False
    chain = ChainContextPrefix.from_address(chain_pointer)
    if chain.cbSize < c.sizeof(ChainContextPrefix) or chain.cChain == 0 or not chain.rgpChain:
        return False
    simple_pointer = PTR.from_address(chain.rgpChain).value
    if not simple_pointer:
        return False
    simple = SimpleChainPrefix.from_address(simple_pointer)
    if simple.cbSize < c.sizeof(SimpleChainPrefix) or simple.cElement == 0 or not simple.rgpElement:
        return False
    element_pointer = PTR.from_address(simple.rgpElement).value
    if not element_pointer:
        return False
    element = ChainElementPrefix.from_address(element_pointer)
    if element.cbSize < c.sizeof(ChainElementPrefix) or not element.pCertContext:
        return False
    certificate = CertificateContext.from_address(element.pCertContext)
    if certificate.cbCertEncoded != len(expected_der) or not certificate.pbCertEncoded:
        return False
    der = c.string_at(certificate.pbCertEncoded, certificate.cbCertEncoded)
    return hashlib.sha256(der).digest() == digest


class _NativeImport:
    """A bounded view of the loaded, verified agents._native PE image only."""

    def __init__(self, kernel: object) -> None:
        from agents import _native

        native_path = Path(_native.__file__).resolve(strict=True)
        if native_path.suffix.lower() != ".pyd":
            raise AssertionError("agents._native is not a Windows Python extension")
        base = kernel.GetModuleHandleW(str(native_path))
        if not base:
            raise c.WinError(c.get_last_error())
        path = c.create_unicode_buffer(32768)
        count = kernel.GetModuleFileNameW(base, path, len(path))
        if count == 0 or count >= len(path) or not os.path.samefile(path.value, native_path):
            raise AssertionError("Loaded module does not match agents._native.__file__")
        self.base = base
        # Windows has already loaded this exact PE image. Only inspect its first
        # mapped page until SizeOfImage and the import-directory bounds are known.
        header = c.string_at(base, 4096)
        if header[:2] != b"MZ":
            raise AssertionError("SDK extension has no PE DOS header")
        nt = struct.unpack_from("<I", header, 0x3C)[0]
        if nt < 0x40 or nt + 24 + 128 > len(header) or header[nt : nt + 4] != b"PE\0\0":
            raise AssertionError("Unexpected SDK PE header layout")
        machine = struct.unpack_from("<H", header, nt + 4)[0]
        optional_size = struct.unpack_from("<H", header, nt + 20)[0]
        optional = nt + 24
        if (
            machine != 0x8664
            or optional_size < 128
            or struct.unpack_from("<H", header, optional)[0] != 0x20B
            or struct.unpack_from("<I", header, optional + 108)[0] < 2
        ):
            raise AssertionError("Prototype requires the Windows CI x64 PE32+ native module")
        self.size = struct.unpack_from("<I", header, optional + 56)[0]
        self.imports, self.import_size = struct.unpack_from("<II", header, optional + 120)
        if self.size < 4096 or not self.imports or self.import_size < 20:
            raise AssertionError("Native SDK PE has no normal import directory")
        self._check(self.imports, self.import_size)

    def _check(self, rva: int, size: int) -> None:
        if rva <= 0 or size < 0 or rva > self.size - size:
            raise AssertionError("Native SDK import points outside its own mapped image")

    def read(self, rva: int, size: int) -> bytes:
        self._check(rva, size)
        return c.string_at(self.base + rva, size)

    def name(self, rva: int) -> bytes:
        result = bytearray()
        for offset in range(256):
            value = self.read(rva + offset, 1)
            if value == b"\0":
                return bytes(result)
            result.extend(value)
        raise AssertionError("Native SDK import name is not terminated")

    def schannel_policy_slot(self) -> int:
        found = []
        terminated = False
        for offset in range(0, self.import_size - 19, 20):
            fields = struct.unpack("<IIIII", self.read(self.imports + offset, 20))
            if fields == (0, 0, 0, 0, 0):
                terminated = True
                break
            lookup, _timestamp, _forward_chain, dll_name, address_table = fields
            if self.name(dll_name).lower() != b"crypt32.dll":
                continue
            if not lookup or not address_table:
                raise AssertionError("Crypt32 import has no name table; refusing an address scan")
            for index in range(4096):
                thunk = struct.unpack("<Q", self.read(lookup + index * 8, 8))[0]
                if thunk == 0:
                    break
                if thunk & (1 << 63):
                    continue
                if self.name(thunk + 2) == b"CertVerifyCertificateChainPolicy":
                    self._check(address_table + index * 8, 8)
                    found.append(self.base + address_table + index * 8)
            else:
                raise AssertionError("Crypt32 import name table is not terminated")
        if not terminated or len(found) != 1 or found[0] % 8:
            raise AssertionError("Exactly one aligned named SDK Crypt32 policy import is required")
        return found[0]


def _set_import_slot(kernel: object, slot: int, expected: int, replacement: int) -> None:
    cell = PTR.from_address(slot)
    if cell.value != expected:
        raise AssertionError("SDK Crypt32 import changed; refusing to overwrite another hook")
    previous_protection = U32()
    if not kernel.VirtualProtect(slot, c.sizeof(PTR), PAGE_READWRITE, c.byref(previous_protection)):
        raise c.WinError(c.get_last_error())
    try:
        # Aligned pointer-sized write in this process, only while no Session is
        # active during install or after Session.close() has joined during restore.
        cell.value = replacement
    finally:
        ignored = U32()
        if not kernel.VirtualProtect(
            slot, c.sizeof(PTR), previous_protection.value, c.byref(ignored)
        ):
            raise c.WinError(c.get_last_error())


@contextmanager
def fixture_schannel_rejection(expected_leaf_der: bytes) -> Iterator[asyncio.Future[int]]:
    """Yield only a real native SSL rejection of the fixture. Close Session first."""
    if (
        sys.platform != "win32"
        or c.sizeof(PTR) != 8
        or os.environ.get("OPENAI_AGENTS_TEST_SCHANNEL_CHILD") != "1"
    ):
        raise AssertionError(
            "Schannel fixture observer requires a dedicated Windows x64 test child"
        )
    if not expected_leaf_der:
        raise AssertionError("The exact synthetic TLS fixture leaf DER is required")
    loop = asyncio.get_running_loop()
    verdict: asyncio.Future[int] = loop.create_future()
    digest = hashlib.sha256(expected_leaf_der).digest()
    kernel = c.WinDLL("kernel32", use_last_error=True)
    kernel.GetModuleHandleW.argtypes = [c.c_wchar_p]
    kernel.GetModuleHandleW.restype = PTR
    kernel.GetModuleFileNameW.argtypes = [PTR, c.c_wchar_p, U32]
    kernel.GetModuleFileNameW.restype = U32
    kernel.VirtualProtect.argtypes = [PTR, c.c_size_t, U32, c.POINTER(U32)]
    kernel.VirtualProtect.restype = I32
    crypt32 = c.WinDLL("crypt32", use_last_error=True)
    api = c.WINFUNCTYPE(I32, PTR, PTR, PTR, PTR, use_last_error=True)
    exported = c.cast(crypt32.CertVerifyCertificateChainPolicy, PTR).value
    image = _NativeImport(kernel)
    slot = image.schannel_policy_slot()
    original_pointer = PTR.from_address(slot).value
    if not exported or original_pointer != exported:
        raise AssertionError("SDK policy import does not point to Crypt32's original function")
    original = api(original_pointer)

    def publish(code: int | None, failure: str | None) -> None:
        if not verdict.done():
            if failure is None:
                assert code is not None
                verdict.set_result(code)
            else:
                verdict.set_exception(AssertionError(failure))

    @api
    def observe(policy: int, chain: int, parameters: int, status_pointer: int) -> int:
        # WINFUNCTYPE preserves the foreign thread's LastError in ctypes'
        # private slot. Nesting the original with the same option transfers it
        # back to Crypt32; restore its exact outgoing value after observing.
        result = original(policy, chain, parameters, status_pointer)
        original_last_error = c.get_last_error()
        try:
            if policy == CERT_CHAIN_POLICY_SSL and _fixture_leaf_matches(
                chain, expected_leaf_der, digest
            ):
                code = None
                failure = None
                if not result:
                    failure = "CryptoAPI policy call failed, not a certificate rejection"
                elif not status_pointer:
                    failure = "CryptoAPI returned no fixture policy status"
                else:
                    status = ChainPolicyStatus.from_address(status_pointer)
                    if status.cbSize < c.sizeof(ChainPolicyStatus):
                        failure = "CryptoAPI returned an incomplete fixture policy status"
                    elif status.dwError not in {CERT_E_UNTRUSTEDROOT, CERT_E_CHAINING}:
                        failure = f"Unexpected fixture SSL policy verdict: 0x{status.dwError:08x}"
                    else:
                        code = status.dwError
                loop.call_soon_threadsafe(publish, code, failure)
        except BaseException as error:
            # A ctypes callback must never propagate a Python exception or turn
            # an observer error into a guessed BOOL. Forward the original result.
            loop.call_soon_threadsafe(
                publish, None, f"Fixture observer error: {type(error).__name__}"
            )
        finally:
            c.set_last_error(original_last_error)
        return result

    observer_pointer = c.cast(observe, PTR).value
    assert observer_pointer is not None
    try:
        # Include installation in cleanup: VirtualProtect restoring the page
        # can fail after the aligned pointer write has already happened.
        _set_import_slot(kernel, slot, original_pointer, observer_pointer)
        yield verdict
    finally:
        # The test must leave its actual Session context first. Do not release
        # the callback until no native code can still call the patched import.
        if PTR.from_address(slot).value == observer_pointer:
            try:
                _set_import_slot(kernel, slot, observer_pointer, original_pointer)
            finally:
                if PTR.from_address(slot).value == observer_pointer:
                    # A stale freed callback in a live native IAT is unsafe.
                    # The dedicated test child must fail and exit if the OS
                    # refuses restoration; its parent already joins it.
                    os._exit(70)
        elif PTR.from_address(slot).value != original_pointer:
            raise AssertionError("SDK Crypt32 import changed during fixture observation")
