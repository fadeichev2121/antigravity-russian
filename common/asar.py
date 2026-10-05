"""Electron ASAR archive rewriting; only changed files get new integrity records."""
import copy
import hashlib
import json
import struct

def digest(data):
    return hashlib.sha256(data).hexdigest()

class Asar:
    def __init__(self, data):
        self.data = data
        _, header_size, _, json_size = struct.unpack("<4I", data[:16])
        self.header = json.loads(data[16:16 + json_size])
        self.base = 8 + header_size

    def entry(self, name):
        item = self.header
        for part in name.split("/"):
            item = item["files"][part]
        return item

    def read(self, name):
        item = self.entry(name)
        start = self.base + int(item["offset"])
        return self.data[start:start + item["size"]]

    def replace(self, updates):
        header = copy.deepcopy(self.header)
        parts = []
        offset = 0

        def walk(directory, prefix=""):
            nonlocal offset
            for name, entry in directory["files"].items():
                full = prefix + name
                if "files" in entry:
                    walk(entry, full + "/")
                elif "offset" in entry and not entry.get("unpacked"):
                    content = updates.get(full, self.read(full))
                    entry["offset"] = str(offset)
                    entry["size"] = len(content)
                    if "integrity" in entry and full in updates:
                        block_size = entry["integrity"].get("blockSize", 4 * 1024 * 1024)
                        entry["integrity"] = {
                            "algorithm": "SHA256", "hash": digest(content),
                            "blockSize": block_size,
                            "blocks": [digest(content[i:i + block_size]) for i in range(0, len(content), block_size)] or [digest(b"")]
                        }
                    parts.append(content)
                    offset += len(content)
        walk(header)
        encoded = json.dumps(header, separators=(",", ":"), ensure_ascii=False).encode()
        payload = struct.pack("<I", len(encoded)) + encoded
        payload += b"\0" * (-len(payload) % 4)
        header_pickle = struct.pack("<I", len(payload)) + payload
        return struct.pack("<II", 4, len(header_pickle)) + header_pickle + b"".join(parts), digest(encoded)
