"""Typed, bounded numeric transport. Base64 avoids JSON integer parsing overhead."""
import base64
import numpy as np

MAX_BYTES = 24 << 20


def pack(array):
    array = np.asarray(array, dtype='<u8')
    if array.nbytes > MAX_BYTES:
        raise ValueError('Numeric payload exceeds the bounded compute buffer')
    return {'data': base64.b64encode(array.tobytes()).decode('ascii'), 'shape': list(array.shape)}


def unpack(value):
    if not isinstance(value, dict) or not isinstance(value.get('data'), str) or len(value['data']) > MAX_BYTES * 4 // 3:
        raise ValueError('Invalid numeric payload')
    raw = base64.b64decode(value['data'], validate=True)
    shape = value.get('shape')
    if (not isinstance(shape, list) or len(shape) > 2 or any(type(n) is not int or n < 0 for n in shape)
            or len(raw) > MAX_BYTES or len(raw) % 8):
        raise ValueError('Invalid numeric shape')
    return np.frombuffer(raw, dtype='<u8').reshape(shape)


def pack_rows(rows):
    lengths = [len(row) for row in rows]
    return {'lengths': lengths, 'values': pack(np.concatenate(rows) if rows else np.array([], dtype=np.uint64))}


def unpack_rows(value):
    values = unpack(value['values'])
    lengths = value['lengths']
    if (values.ndim != 1 or len(lengths) > 100000 or any(type(n) is not int or n < 0 for n in lengths)
            or sum(lengths) != len(values)):
        raise ValueError('Invalid ragged numeric payload')
    out, start = [], 0
    for length in lengths:
        out.append(values[start:start+length])
        start += length
    return out
