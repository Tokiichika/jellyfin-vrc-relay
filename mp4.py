"""Bounded ISO-BMFF metadata inspection, without decoding or downloading mdat."""
import hashlib
import struct


class MP4Error(ValueError):
    pass


def boxes(data):
    offset = 0
    while offset < len(data):
        if len(data) - offset < 8:
            raise MP4Error('MP4 box truncated')
        size, kind = struct.unpack_from('>I4s', data, offset)
        header = 8
        if size == 1:
            if len(data) - offset < 16:
                raise MP4Error('MP4 extended box truncated')
            size = struct.unpack_from('>Q', data, offset + 8)[0]
            header = 16
        elif size == 0:
            size = len(data) - offset
        if size < header or offset + size > len(data):
            raise MP4Error('MP4 box bounds invalid')
        yield kind, data[offset + header:offset + size]
        offset += size


def aac_descriptor(data):
    """ES_Descriptor -> DecoderConfigDescriptor, object type 0x40 is AAC."""
    def descriptor(value, offset):
        tag = value[offset]
        offset += 1
        length = 0
        for _ in range(4):
            byte = value[offset]; offset += 1
            length = (length << 7) | (byte & 127)
            if not byte & 128:
                break
        else:
            raise MP4Error('invalid descriptor length')
        if offset + length > len(value):
            raise MP4Error('descriptor truncated')
        return tag, value[offset:offset + length]
    try:
        tag, es = descriptor(data, 4)
        if tag != 3:
            return False
        flags, offset = es[2], 3
        if flags & 128:
            offset += 2
        if flags & 64:
            offset += 1 + es[offset]
        if flags & 32:
            offset += 2
        tag, config = descriptor(es, offset)
        return tag == 4 and config[0] == 0x40
    except (IndexError, MP4Error):
        return False


def inspect(read_range, size):
    offset, moov = 0, None
    for _ in range(128):
        if offset + 8 > size:
            break
        header = read_range(offset, min(offset + 15, size - 1))
        length, kind = struct.unpack_from('>I4s', header)
        header_size = 8
        if length == 1:
            if len(header) < 16:
                raise MP4Error('extended box truncated')
            length = struct.unpack_from('>Q', header, 8)[0]
            header_size = 16
        elif length == 0:
            length = size - offset
        if length < header_size or offset + length > size:
            raise MP4Error('invalid top-level box')
        if kind == b'moov':
            if length > 8 * 1024 * 1024:
                raise MP4Error('MP4 metadata exceeds 8 MiB')
            moov = read_range(offset + header_size, offset + length - 1)
            break
        offset += length
    if moov is None:
        raise MP4Error('missing MP4 metadata')
    codecs = []
    def walk(data, depth=0):
        if depth > 8:
            raise MP4Error('metadata nesting too deep')
        for kind, body in boxes(data):
            if kind in (b'trak', b'mdia', b'minf', b'stbl'):
                walk(body, depth + 1)
            elif kind == b'stsd':
                if len(body) < 8:
                    raise MP4Error('invalid sample descriptions')
                for codec, entry in boxes(body[8:]):
                    if codec in (b'avc1', b'avc3'):
                        codecs.append('h264')
                    elif codec == b'mp4a':
                        if len(entry) < 28:
                            raise MP4Error('audio sample entry truncated')
                        children = dict(boxes(entry[28:]))
                        codecs.append('aac' if aac_descriptor(children.get(b'esds', b'')) else 'unknown_audio')
                    else:
                        codecs.append(codec.decode('ascii', errors='replace'))
    walk(moov)
    if not codecs or 'h264' not in codecs or any(c not in ('h264', 'aac') for c in codecs):
        raise MP4Error('需要含 H.264 视频的 MP4；有音频时必须是 AAC，不支持 DASH 分离轨道或其他编码')
    return {'source_video': 'h264', 'source_audio': 'aac' if 'aac' in codecs else '无音轨',
            'moov_hash': hashlib.sha256(moov).hexdigest()}
