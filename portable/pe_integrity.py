"""Read and replace an existing Windows Electron ASAR header digest.

This deliberately is not a general PE resource editor. Only the literal,
fixed-size SHA-256 value for resources/app.asar is changed, together with the
PE checksum. Section layout, executable fuses and certificate bytes are kept.
Changing the resource still invalidates the original Authenticode signature.

Payload format: electron/packager/src/resedit.ts and
electron/electron/shell/common/asar/archive_win.cc.
PE layout: https://learn.microsoft.com/en-us/windows/win32/debug/pe-format
"""

from __future__ import annotations

import array
import json
import re
import struct
import sys
from typing import Any


class IntegrityError(ValueError):
    """The executable cannot be updated without ambiguous resource edits."""


_HIGH_BIT = 0x80000000
_HASH = re.compile(r"[0-9a-fA-F]{64}\Z")
_ARCHIVE = "resources/app.asar"
_MAX_IMAGE = 1024 * 1024 * 1024
_MAX_RESOURCE = 16 * 1024 * 1024
_MAX_PAYLOAD = 1024 * 1024
_MAX_ENTRIES = 16384
_MAX_DIRECTORIES = 4096


def _digest(value: str, field: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise IntegrityError(f"{field}: требуется SHA-256 из 64 шестнадцатеричных символов.")
    return value.lower()


def _overlap(first: tuple[int, int], second: tuple[int, int]) -> bool:
    return first[0] < second[1] and second[0] < first[1]


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise IntegrityError("В ресурсе INTEGRITY повторяется ключ JSON.")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise IntegrityError("Ресурс INTEGRITY содержит недопустимое число JSON.")


class _PE:
    def __init__(self, data: bytes):
        if not isinstance(data, bytes):
            raise IntegrityError("Ожидались неизменяемые байты исполняемого файла.")
        if len(data) < 64 or len(data) > _MAX_IMAGE or data[:2] != b"MZ":
            raise IntegrityError("Не поддерживается размер или формат Windows EXE.")
        self.data = data
        self.sections: list[tuple[int, int, int, int]] = []
        self.metadata_ranges: list[tuple[int, int]] = []
        self.leaves: list[dict[str, Any]] = []
        self.visited: set[int] = set()
        self.entry_count = 0

        pe = self.u32(0x3C)
        if pe < 64 or pe % 4 or self.read(pe, 4) != b"PE\0\0":
            raise IntegrityError("Повреждён заголовок PE.")
        self.bounds(pe + 4, 20)
        self.machine = self.u16(pe + 4)
        count = self.u16(pe + 6)
        optional_size = self.u16(pe + 20)
        optional = pe + 24
        self.bounds(optional, optional_size)
        magic = self.u16(optional)
        if magic == 0x10B and self.machine == 0x14C:
            directory_offset, count_offset = 96, 92
            self.pe_format = "PE32"
        elif magic == 0x20B and self.machine in (0x8664, 0xAA64):
            directory_offset, count_offset = 112, 108
            self.pe_format = "PE32+"
        else:
            raise IntegrityError("Архитектура или optional header PE не поддерживается.")
        if not 1 <= count <= 96 or optional_size < directory_offset + 40:
            raise IntegrityError("Недопустимый размер таблиц PE.")
        directory_count = self.u32(optional + count_offset)
        if not 5 <= directory_count <= 32:
            raise IntegrityError("Таблица каталогов PE не поддерживается.")
        if directory_offset + directory_count * 8 > optional_size:
            raise IntegrityError("Каталоги PE выходят за optional header.")
        self.checksum_offset = optional + 64
        self.checksum = self.u32(self.checksum_offset)
        headers_size = self.u32(optional + 60)
        section_table = optional + optional_size
        self.bounds(section_table, count * 40)
        if not section_table + count * 40 <= headers_size <= len(data):
            raise IntegrityError("Недопустимый SizeOfHeaders в PE.")

        raw_ranges: list[tuple[int, int]] = []
        virtual_ranges: list[tuple[int, int]] = []
        for index in range(count):
            position = section_table + index * 40
            virtual_size, virtual, raw_size, raw = struct.unpack_from(
                "<IIII", data, position + 8
            )
            span = max(virtual_size, raw_size)
            if span and (not virtual or virtual + span > 0x100000000):
                raise IntegrityError("Недопустимые виртуальные адреса секции PE.")
            if span:
                virtual_range = (virtual, virtual + span)
                if any(_overlap(virtual_range, old) for old in virtual_ranges):
                    raise IntegrityError("Виртуальные секции PE перекрываются.")
                virtual_ranges.append(virtual_range)
            if raw_size:
                self.bounds(raw, raw_size)
                raw_range = (raw, raw + raw_size)
                if raw < headers_size or any(
                    _overlap(raw_range, old) for old in raw_ranges
                ):
                    raise IntegrityError("Секции файла PE перекрываются.")
                raw_ranges.append(raw_range)
            self.sections.append((virtual, span, raw, raw_size))

        directories = optional + directory_offset
        resource_rva, self.resource_size = struct.unpack_from("<II", data, directories + 16)
        if not resource_rva or not 16 <= self.resource_size <= _MAX_RESOURCE:
            raise IntegrityError("В EXE нет поддерживаемой таблицы ресурсов.")
        self.resource_base = self.map_rva(resource_rva, self.resource_size)
        self.resource_end = self.resource_base + self.resource_size
        self.certificate_offset, self.certificate_size = struct.unpack_from(
            "<II", data, directories + 32
        )
        if bool(self.certificate_offset) != bool(self.certificate_size):
            raise IntegrityError("Повреждён каталог сертификатов PE.")
        if self.certificate_size:
            self.bounds(self.certificate_offset, self.certificate_size)
            certificate_range = (
                self.certificate_offset,
                self.certificate_offset + self.certificate_size,
            )
            if (
                self.certificate_offset % 8
                or self.certificate_size < 8
                or self.certificate_offset < headers_size
                or any(_overlap(certificate_range, old) for old in raw_ranges)
            ):
                raise IntegrityError("Каталог сертификатов пересекается с образом PE.")
        self.walk(0, 0, ())

    def bounds(self, offset: int, size: int) -> None:
        if offset < 0 or size < 0 or offset > len(self.data) - size:
            raise IntegrityError("Смещение PE выходит за границы файла.")

    def read(self, offset: int, size: int) -> bytes:
        self.bounds(offset, size)
        return self.data[offset : offset + size]

    def u16(self, offset: int) -> int:
        self.bounds(offset, 2)
        return struct.unpack_from("<H", self.data, offset)[0]

    def u32(self, offset: int) -> int:
        self.bounds(offset, 4)
        return struct.unpack_from("<I", self.data, offset)[0]

    def map_rva(self, rva: int, size: int) -> int:
        if not rva or size <= 0 or rva + size > 0x100000000:
            raise IntegrityError("Недопустимый адрес ресурса PE.")
        matches = []
        for virtual, span, raw, raw_size in self.sections:
            relative = rva - virtual
            if 0 <= relative and relative + size <= raw_size:
                matches.append(raw + relative)
        if len(matches) != 1:
            raise IntegrityError("Ресурс PE не соответствует одной секции файла.")
        self.bounds(matches[0], size)
        return matches[0]

    def resource_bounds(self, relative: int, size: int) -> int:
        if relative < 0 or size < 0 or relative > self.resource_size - size:
            raise IntegrityError("Таблица ресурса выходит за каталог ресурсов PE.")
        return self.resource_base + relative

    def name(self, encoded: int) -> int | str:
        if not encoded & _HIGH_BIT:
            if encoded > 0xFFFF:
                raise IntegrityError("Числовой идентификатор ресурса PE не поддерживается.")
            return encoded
        relative = encoded & ~_HIGH_BIT
        if relative % 2:
            raise IntegrityError("Имя ресурса PE не выровнено.")
        position = self.resource_bounds(relative, 2)
        length = self.u16(position)
        if not 1 <= length <= 1024:
            raise IntegrityError("Недопустимая длина имени ресурса PE.")
        self.resource_bounds(relative, 2 + length * 2)
        self.metadata_ranges.append((position, position + 2 + length * 2))
        try:
            value = self.read(position + 2, length * 2).decode("utf-16-le", "strict")
        except UnicodeError as exc:
            raise IntegrityError("Повреждено имя ресурса PE.") from exc
        if "\0" in value:
            raise IntegrityError("Имя ресурса PE содержит нулевой символ.")
        return value

    def walk(self, relative: int, depth: int, labels: tuple[Any, ...]) -> None:
        if depth > 2 or relative % 4 or relative in self.visited:
            raise IntegrityError("Цикл, ссылка или неподдерживаемый уровень ресурсов PE.")
        if len(self.visited) >= _MAX_DIRECTORIES:
            raise IntegrityError("Слишком много каталогов ресурсов PE.")
        self.visited.add(relative)
        position = self.resource_bounds(relative, 16)
        named, numbered = struct.unpack_from("<HH", self.data, position + 12)
        count = named + numbered
        self.entry_count += count
        if self.entry_count > _MAX_ENTRIES:
            raise IntegrityError("Слишком много записей ресурсов PE.")
        self.resource_bounds(relative, 16 + count * 8)
        self.metadata_ranges.append((position, position + 16 + count * 8))
        seen: set[tuple[str, Any]] = set()
        for index in range(count):
            encoded, destination = struct.unpack_from("<II", self.data, position + 16 + index * 8)
            if bool(encoded & _HIGH_BIT) != (index < named):
                raise IntegrityError("Повреждены имена или ID в каталоге ресурсов PE.")
            label = self.name(encoded)
            identity = ("name", label.casefold()) if isinstance(label, str) else ("id", label)
            if identity in seen:
                raise IntegrityError("Ресурс PE имеет неоднозначный идентификатор.")
            seen.add(identity)
            path = labels + (label,)
            if depth < 2:
                if not destination & _HIGH_BIT:
                    raise IntegrityError("Не поддерживается структура каталога ресурсов PE.")
                self.walk(destination & ~_HIGH_BIT, depth + 1, path)
                continue
            if destination & _HIGH_BIT or isinstance(label, str) or destination % 4:
                raise IntegrityError("Не поддерживается языковая запись ресурса PE.")
            leaf = self.resource_bounds(destination, 16)
            self.metadata_ranges.append((leaf, leaf + 16))
            rva, size, codepage, reserved = struct.unpack_from("<IIII", self.data, leaf)
            if reserved or not size:
                raise IntegrityError("Повреждён дескриптор данных ресурса PE.")
            offset = self.map_rva(rva, size)
            self.leaves.append(
                {"path": path, "offset": offset, "size": size, "codepage": codepage}
            )

    def integrity(self, expected: str) -> list[dict[str, Any]]:
        selected = []
        for leaf in self.leaves:
            resource_type, resource_name, language = leaf["path"]
            if (
                not isinstance(resource_type, str)
                or resource_type.casefold() != "integrity"
                or not isinstance(resource_name, str)
                or resource_name.casefold() != "electronasar"
            ):
                continue
            offset, size = leaf["offset"], leaf["size"]
            payload_range = (offset, offset + size)
            if (
                size > _MAX_PAYLOAD
                or offset < self.resource_base
                or offset + size > self.resource_end
                or any(_overlap(payload_range, old) for old in self.metadata_ranges)
                or any(
                    other is not leaf
                    and _overlap(payload_range, (other["offset"], other["offset"] + other["size"]))
                    for other in self.leaves
                )
            ):
                raise IntegrityError("Данные INTEGRITY пересекаются с другими ресурсами PE.")
            payload = self.read(offset, size)
            hash_offset, original_hash = _payload_hash(payload, expected)
            selected.append(
                {
                    "language": language,
                    "codepage": leaf["codepage"],
                    "offset": offset,
                    "size": size,
                    "hash_offset": offset + hash_offset,
                    "original_hash": original_hash,
                }
            )
        if not selected:
            raise IntegrityError("В EXE отсутствует ресурс INTEGRITY/ELECTRONASAR для app.asar.")
        return selected


def _payload_hash(payload: bytes, expected: str) -> tuple[int, str]:
    try:
        text = payload.decode("utf-8", "strict")
        config = json.loads(
            text, object_pairs_hook=_unique_object, parse_constant=_invalid_constant
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise IntegrityError("INTEGRITY должен содержать поддерживаемый UTF-8 JSON.") from exc
    if not isinstance(config, list) or not 1 <= len(config) <= 64:
        raise IntegrityError("Не поддерживается список архивов в ресурсе INTEGRITY.")
    seen = set()
    selected = None
    for item in config:
        if not isinstance(item, dict) or set(item) != {"file", "alg", "value"}:
            raise IntegrityError("Не поддерживается схема JSON ресурса INTEGRITY.")
        filename, algorithm, value = item["file"], item["alg"], item["value"]
        if (
            not isinstance(filename, str)
            or not 1 <= len(filename) <= 1024
            or "\0" in filename
            or ":" in filename
            or not isinstance(algorithm, str)
            or algorithm.lower() != "sha256"
        ):
            raise IntegrityError("Не поддерживаются путь или алгоритм в INTEGRITY.")
        canonical = filename.replace("\\", "/").lower()
        if any(part in ("", ".", "..") for part in canonical.split("/")):
            raise IntegrityError("Ресурс INTEGRITY содержит небезопасный путь архива.")
        if canonical in seen:
            raise IntegrityError("Архив повторяется в ресурсе INTEGRITY.")
        seen.add(canonical)
        digest = _digest(value, "Хеш INTEGRITY")
        if canonical == _ARCHIVE:
            if digest != expected:
                raise IntegrityError("Хеш заголовка app.asar не совпадает с ресурсом EXE.")
            selected = value
    if selected is None:
        raise IntegrityError("Ресурс INTEGRITY не содержит resources/app.asar.")
    literal = selected.encode("ascii")
    pattern = re.compile(rb'"value"\s*:\s*"(' + re.escape(literal) + rb')"')
    matches = list(pattern.finditer(payload))
    if len(matches) != 1 or payload.count(literal) != 1:
        raise IntegrityError("Неоднозначный или экранированный хеш INTEGRITY; EXE не изменён.")
    return matches[0].start(1), selected


def inspect_integrity(data: bytes, expected_header_hash: str) -> dict[str, Any]:
    """Return resource offsets after validating every selected language entry.

    ``expected_header_hash`` is SHA-256 of the exact raw ASAR JSON header, not
    of the entire archive. A missing, inconsistent or unsupported resource
    raises IntegrityError. No Windows APIs or external tools are called.
    """
    expected = _digest(expected_header_hash, "Хеш заголовка ASAR")
    image = _PE(data)
    resources = image.integrity(expected)
    return {
        "machine": image.machine,
        "pe_format": image.pe_format,
        "archive": _ARCHIVE,
        "header_hash": expected,
        "resources": resources,
        "resource_count": len(resources),
        "checksum_offset": image.checksum_offset,
        "checksum": image.checksum,
        "certificate_offset": image.certificate_offset,
        "certificate_size": image.certificate_size,
        "has_embedded_certificate": bool(image.certificate_size),
    }


def _checksum(data: bytearray, checksum_offset: int) -> int:
    """PE one's-complement checksum, excluding its four-byte field."""
    total = 0
    view = memoryview(data)
    for first, last in ((0, checksum_offset), (checksum_offset + 4, len(data))):
        for position in range(first, last, 1024 * 1024):
            end = min(position + 1024 * 1024, last)
            words = array.array("H")
            even_end = end - ((end - position) % 2)
            words.frombytes(view[position:even_end])
            if sys.byteorder != "little":
                words.byteswap()
            total += sum(words)
            if even_end != end:
                total += data[even_end]
            total = (total & 0xFFFF) + (total >> 16)
    total = (total & 0xFFFF) + (total >> 16)
    total = (total & 0xFFFF) + (total >> 16)
    return (total + len(data)) & 0xFFFFFFFF


def replace_integrity(data: bytes, old_header_hash: str, new_header_hash: str) -> bytes:
    """Replace existing fixed-size digests and refresh PE CheckSum.

    Certificate data is left byte-for-byte intact, although Authenticode
    becomes invalid because the changed resource is covered by its digest.
    The caller must separately reject MSIX installations and stage/backup
    both the executable and archive before replacing installed files.
    """
    old = _digest(old_header_hash, "Исходный хеш заголовка ASAR")
    new = _digest(new_header_hash, "Новый хеш заголовка ASAR")
    metadata = inspect_integrity(data, old)
    if old == new:
        return data
    result = bytearray(data)
    replacement = new.encode("ascii")
    for resource in metadata["resources"]:
        position = resource["hash_offset"]
        result[position : position + 64] = replacement
    struct.pack_into("<I", result, metadata["checksum_offset"], _checksum(result, metadata["checksum_offset"]))
    return bytes(result)
