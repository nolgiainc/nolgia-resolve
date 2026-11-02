# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 NOLGIA Inc
"""Write DaVinci Resolve grade stills (.drx) with a whole labelled node tree.

Resolve's scripting cannot add nodes, but Graph.ApplyGradeFromDRX applies a
saved grade with its node tree. A .drx is XML; each version's <Body> is the hex
of one marker byte (0x81) and a Zstandard frame holding a protobuf message.
This module writes the frame with raw (stored) blocks, which every Zstandard
decoder reads, so it needs nothing outside Python's standard library.

What is known of the message (read from Resolve Studio 21.1.1 exports, then
checked by applying written files and reading the graph and the picture back):
  graph.7    one per node: 1 id, 2 number, 4/5 position, 6 label, 9 the grade
  graph.8    one per link: 1 from node, 3 to node, 7 link id
  graph.9    the input; graph.10 the output (3.1 = node count + 1, 3.4 = last node)
  a grade's parameters are {1: id, 2: value} with value {1: float} | {2: int} | {5: text}
"""

import struct
import time
import uuid

# Parameter ids (Resolve 21.1.1), each checked in Resolve: set here, read in the palette.
SATURATION = 0x06000005          # Primaries Saturation, 1.0 = 50 in the palette
OFFSET = (0x0600007D, 0x0600007E, 0x0600007F)  # Offset wheel R, G, B; 1.0 moves code values by OFFSET_CODE
CONTRAST = 0x860000C1            # Primaries Contrast, 1.0 = none
PIVOT = 0x860000C0               # Primaries Pivot (code value)
KEY_OUTPUT_GAIN = 0x0C30001D     # Key palette, Key Output Gain (scales the key, not the node's LUT)
LUT_KIND = 0x860000A0            # 6 = .cube, 8 = .dctl
LUT_PATH = 0x860000A1            # relative to Resolve's LUT folder, backslashes

OFFSET_CODE = 0.18333            # measured: Offset 0.1 moves every code value by 0.01833

F32, INT, STR = "f", "i", "s"

# The empty timeline (track) grade of a still, as Resolve writes it.
TRACK_BODY = bytes.fromhex(
    "0a6a08ffffffff0f10011a4808800f10b8081d0000803f20800f28b808350000803f38800f40b80848ffffffff0f7a240000803f"
    "0000000000000000000000000000803f0000000000000000000000000000803f2080c60a4a0408021040520408031040580160fe"
    "b3c49d0d1a2020be0128b401380140554a00520c0a0a08818080800c1202100260ff99e2ce06")

NODE_SPACING = 240
ROW_SPACING = 230
ROW_LENGTH = 4


def _varint(value):
    if value < 0:
        value += 1 << 64
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def encode(fields):
    """[(field, wire type, value)] -> protobuf bytes; a list value is a nested message."""
    out = bytearray()
    for field, wire, value in fields:
        out += _varint((field << 3) | wire)
        if wire == 0:
            out += _varint(value)
        elif wire == 5:
            out += value
        elif wire == 2:
            if isinstance(value, list):
                value = encode(value)
            out += _varint(len(value)) + value
        else:
            raise ValueError("wire type %d" % wire)
    return bytes(out)


def decode(data):
    """protobuf bytes -> [(field, wire type, value)], one level (for tests and checks)."""
    out, i = [], 0
    while i < len(data):
        key, i = _read_varint(data, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _read_varint(data, i)
        elif wire == 5:
            value, i = data[i:i + 4], i + 4
        elif wire == 1:
            value, i = data[i:i + 8], i + 8
        elif wire == 2:
            n, i = _read_varint(data, i)
            value, i = data[i:i + n], i + n
        else:
            raise ValueError("wire type %d" % wire)
        out.append((field, wire, value))
    return out


def _read_varint(data, i):
    result = shift = 0
    while True:
        byte = data[i]
        i += 1
        result |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return result, i


def zstd_stored(data):
    """A Zstandard frame of raw blocks: single segment, content size given, no checksum."""
    out = bytearray(b"\x28\xb5\x2f\xfd\xa0") + struct.pack("<I", len(data))
    if not data:
        return bytes(out + (1).to_bytes(3, "little"))
    pos = 0
    while pos < len(data):
        chunk = data[pos:pos + 128 * 1024]
        pos += len(chunk)
        last = 1 if pos >= len(data) else 0
        out += (last | (len(chunk) << 3)).to_bytes(3, "little") + chunk
    return bytes(out)


def unstored(frame):
    """Read back a frame zstd_stored wrote (raw blocks only)."""
    if frame[:4] != b"\x28\xb5\x2f\xfd" or frame[4] != 0xA0:
        raise ValueError("not a stored Zstandard frame from this module")
    size = struct.unpack("<I", frame[5:9])[0]
    out, i = bytearray(), 9
    while True:
        header = int.from_bytes(frame[i:i + 3], "little")
        i += 3
        if (header >> 1) & 3:
            raise ValueError("compressed block")
        n = header >> 3
        out += frame[i:i + n]
        i += n
        if header & 1:
            break
    if len(out) != size:
        raise ValueError("size mismatch")
    return bytes(out)


def param(pid, kind, value):
    if kind == F32:
        inner = [(1, 5, struct.pack("<f", float(value)))]
    elif kind == INT:
        inner = [(2, 0, int(value))]
    else:
        inner = [(5, 2, value.encode("utf-8"))]
    return (3, 2, [(1, 0, pid), (2, 2, inner)])


class Node(object):
    """One corrector node: a label and its parameters."""

    def __init__(self, label, offset=None, saturation=None, contrast=None, pivot=None, lut=None, key_gain=None):
        self.label = label
        self.offset = offset            # (r, g, b) Offset wheel values (OFFSET_CODE per unit)
        self.saturation = saturation    # 1.0 = no change
        self.contrast = contrast
        self.pivot = pivot
        self.lut = lut                  # path relative to the LUT folder, "/" or "\\"
        self.key_gain = key_gain

    def params(self):
        out = []
        if self.saturation is not None and abs(self.saturation - 1.0) > 1e-6:
            out.append((SATURATION, F32, self.saturation))
        if self.offset is not None:
            for pid, value in zip(OFFSET, self.offset):
                if abs(value) > 1e-7:
                    out.append((pid, F32, value))
        if self.pivot is not None and self.contrast is not None and abs(self.contrast - 1.0) > 1e-6:
            out.append((PIVOT, F32, self.pivot))
            out.append((CONTRAST, F32, self.contrast))
        if self.key_gain is not None and abs(self.key_gain - 1.0) > 1e-6:
            out.append((KEY_OUTPUT_GAIN, F32, self.key_gain))
        if self.lut:
            path = self.lut.replace("/", "\\")
            out.append((LUT_KIND, INT, 8 if path.lower().endswith(".dctl") else 6))
            out.append((LUT_PATH, STR, path))
        return out


def node_position(i, count):
    """Rows of four, left to right, so the whole tree shows in a normal node editor at Resolve's own zoom."""
    row, col = divmod(i - 1, ROW_LENGTH if count > ROW_LENGTH + 1 else count)
    return 190 + NODE_SPACING * col, 180 + ROW_SPACING * row


def graph_body(nodes, width=1920, height=1080):
    """The clip grade message for serial nodes, in order."""
    res = [(1, 0, width), (2, 0, height), (3, 5, struct.pack("<f", 1.0)), (4, 0, width), (5, 0, height),
           (6, 5, struct.pack("<f", 1.0)), (7, 0, width), (8, 0, height), (9, 0, 0xFFFFFFFF)]
    g = [(1, 0, 1), (2, 0, 1), (3, 2, res)]
    for i, node in enumerate(nodes, start=1):
        grade = [(1, 0, 1)] + [param(*p) for p in node.params()]
        x, y = node_position(i, len(nodes))
        fields = [(1, 0, i), (2, 0, i), (4, 0, x), (5, 0, y)]
        if node.label:
            fields.append((6, 2, node.label.encode("utf-8")))
        fields += [(7, 0, 1), (8, 0, 44),
                   (9, 2, [(1, 2, [(1, 0, 1), (3, 0, 1), (6, 2, [(2, 2, grade)])]), (2, 2, bytes.fromhex("0304050612"))]),
                   (10, 2, [(1, 2, [(1, 0, 0xC0000001), (2, 2, [(2, 0, 2)])])])]
        g.append((7, 2, fields))
    n = len(nodes)
    for i in range(1, n):
        g.append((8, 2, [(1, 0, i), (3, 0, i + 1), (5, 0, 64), (6, 0, 64), (7, 0, i)]))
    g.append((9, 2, [(1, 0, 1), (2, 0, 80), (3, 2, [(1, 0, 1), (2, 0, 64), (3, 0, 1), (4, 0, 1)])]))
    g.append((10, 2, [(1, 0, 2), (2, 0, 64), (3, 2, [(1, 0, n + 1), (2, 0, 64), (3, 0, 2), (4, 0, n)])]))
    g.append((11, 0, 1))
    top = [(1, 2, g),
           (3, 2, [(4, 0, 190), (5, 0, 180), (7, 0, 1), (8, 0, 84), (9, 2, [(2, 2, b"\x06")]),
                   (10, 2, [(1, 2, [(1, 0, 0xC0000001), (2, 2, [(2, 0, 2)])])])])]
    return encode(top)


_VERSION = """  <ListMgt::LmVersion DbId="{id}">
   <FieldsBlob/>
   <Name/>
   <HasCorrection>{has}</HasCorrection>
   <VerType>{kind}</VerType>
   <ImplVersion>1</ImplVersion>
   <IncludedInRecording>true</IncludedInRecording>
   <FlatPassEnabled>false</FlatPassEnabled>
   <RGBAOutputEnabled>false</RGBAOutputEnabled>
   <Body>{body}</Body>
   <UseVersionClipProcParams>true</UseVersionClipProcParams>
  </ListMgt::LmVersion>
"""


def _xml_text(text):
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def drx_text(nodes, width=1920, height=1080, label="NOLGIA grade"):
    body = (b"\x81" + zstd_stored(graph_body(nodes, width, height))).hex()
    track = (b"\x81" + zstd_stored(TRACK_BODY)).hex()
    return ('<?xml version="1.0" encoding="UTF-8"?>\r\n<!--DbAppVer="21.1.1.0010" DbPrjVer="17"-->\r\n'
            '<Gallery::GyStill DbId="%s">\n <FieldsBlob/>\n <SrcHint>%s</SrcHint>\n <SrcType>1</SrcType>\n'
            ' <GalleryPath/>\n <Label>%s</Label>\n <RecTC>00:00:00:00</RecTC>\n <SrcTC>00:00:00:00</SrcTC>\n'
            ' <DpxDescriptor>50</DpxDescriptor>\n <Width>%d</Width>\n <Height>%d</Height>\n <BitDepth>10</BitDepth>\n'
            ' <PAR>1</PAR>\n <Endianship>1</Endianship>\n <CreateTime>%s</CreateTime>\n <pClipFullVer>\n'
            % (uuid.uuid4(), _xml_text(label), _xml_text(label), width, height, time.strftime("%Y-%m-%dT%H:%M:%S.000"))
            + _VERSION.format(id=uuid.uuid4(), has="true", kind=0, body=body)
            + " </pClipFullVer>\n <pTrackVer>\n"
            + _VERSION.format(id=uuid.uuid4(), has="false", kind=1, body=track)
            + " </pTrackVer>\n <PrimaryCCMode>0</PrimaryCCMode>\n <TrackThumbnails/>\n <ReelName/>\n</Gallery::GyStill>\n")


def write(path, nodes, width=1920, height=1080, label="NOLGIA grade"):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(drx_text(nodes, width, height, label))
    return path
