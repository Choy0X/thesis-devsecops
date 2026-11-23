"""Bounded physical Parquet output for already validated Arrow tables.

Only row-group layout changes. Admission, source projection, record ordering,
schemas, values and checkpoint authority belong to the caller and are unchanged.
"""
from __future__ import annotations

MAX_ROWS = 64
MAX_BYTES = 4 << 20
SCHEMA = 'bounded-parquet-output/1'


def identity():
    return {'schema': SCHEMA, 'max_rows': MAX_ROWS, 'max_arrow_bytes': MAX_BYTES,
            'oversized_single_row': 'flush_previous_then_write_intact'}


class BoundedParquetWriter:
    """Accumulate at most 64 rows and 4MiB of actual referenced Arrow buffers.

    Root buffer sizes are summed conservatively across retained tables. A slice
    can reference a larger parent than Table.nbytes/get_total_buffer_size report,
    so any
    over-budget slice is compacted with Arrow array concatenation before it can
    be retained. A genuinely oversized single row is written intact after the
    preceding buffer is flushed. Existing caller row/resource limits still apply.
    """
    def __init__(self, where, schema, *, max_rows=MAX_ROWS, max_bytes=MAX_BYTES, writer_factory=None):
        if type(max_rows) is not int or not 0 < max_rows <= MAX_ROWS:
            raise ValueError('bounded Parquet row cap must be between 1 and 64')
        if type(max_bytes) is not int or not 0 < max_bytes <= MAX_BYTES:
            raise ValueError('bounded Parquet byte cap must be between 1 and 4MiB')
        if writer_factory is None:
            from pyarrow.parquet import ParquetWriter
            writer_factory = ParquetWriter
        self.schema, self.max_rows, self.max_bytes = schema, max_rows, max_bytes
        self.writer = writer_factory(where, schema)
        self._tables = []
        self.pending_rows = self.pending_bytes = 0
        self.closed = self.failed = False

    def _ensure_open(self):
        if self.closed or self.failed:
            raise ValueError('bounded Parquet writer is closed or failed')

    def _discard(self):
        self._tables.clear()
        self.pending_rows = self.pending_bytes = 0

    @staticmethod
    def _retained_bytes(table):
        import pyarrow as pa
        roots = {}
        def visit(array):
            for buffer in array.buffers():
                if buffer is not None:
                    while buffer.parent is not None:
                        buffer = buffer.parent
                    roots[(buffer.address, buffer.size)] = buffer.size
            kind = array.type
            if isinstance(array, pa.ExtensionArray):
                visit(array.storage)
            elif pa.types.is_dictionary(kind):
                visit(array.dictionary)
            elif pa.types.is_struct(kind) or pa.types.is_union(kind):
                for index in range(kind.num_fields):
                    visit(array.field(index))
            elif pa.types.is_map(kind):
                visit(array.keys)
                visit(array.items)
            elif (pa.types.is_list(kind) or pa.types.is_large_list(kind) or pa.types.is_fixed_size_list(kind)
                  or pa.types.is_list_view(kind) or pa.types.is_large_list_view(kind)):
                visit(array.values)
            elif pa.types.is_run_end_encoded(kind):
                visit(array.run_ends)
                visit(array.values)
        for column in table.columns:
            for chunk in column.chunks:
                visit(chunk)
        return sum(roots.values())

    @staticmethod
    def _compact(table):
        import pyarrow as pa
        # Unlike combine_chunks(), concat_arrays also copies a single sliced
        # chunk and releases the unrelated parent buffers for primitive/nested
        # arrays. Dictionaries retain their value dictionary as part of schema-
        # preserving Arrow storage and remain subject to actual-buffer checks.
        return pa.Table.from_arrays([pa.concat_arrays(column.chunks) for column in table.columns], schema=table.schema)

    def _write(self, table):
        try:
            self.writer.write_table(table)
        except BaseException:
            self.failed = True
            self._discard()
            raise

    def _append(self, table):
        retained = self._retained_bytes(table)
        if retained > self.max_bytes:
            table = self._compact(table)
            retained = self._retained_bytes(table)
        if retained > self.max_bytes and table.num_rows > 1:
            for offset in range(table.num_rows):
                self._append(table.slice(offset, 1))
            return
        if retained > self.max_bytes:
            self.flush()
            self._write(table)
            return
        if self.pending_rows + table.num_rows > self.max_rows or self.pending_bytes + retained > self.max_bytes:
            self.flush()
        self._tables.append(table)
        self.pending_rows += table.num_rows
        self.pending_bytes += retained
        if self.pending_rows == self.max_rows or self.pending_bytes == self.max_bytes:
            self.flush()

    def write_table(self, table):
        import pyarrow as pa
        self._ensure_open()
        if not isinstance(table, pa.Table):
            raise TypeError('bounded Parquet output requires an Arrow Table')
        if not table.schema.equals(self.schema, check_metadata=True):
            raise ValueError('Arrow table schema differs from output schema')
        table.validate()
        for offset in range(0, table.num_rows, self.max_rows):
            self._append(table.slice(offset, self.max_rows))

    def flush(self):
        import pyarrow as pa
        self._ensure_open()
        if not self._tables:
            return
        tables, self._tables = self._tables, []
        self.pending_rows = self.pending_bytes = 0
        self._write(pa.concat_tables(tables))

    def close(self):
        if self.closed:
            return
        try:
            self.flush()
        except BaseException:
            self._discard()
            self.closed = True
            try:
                self.writer.close()
            except BaseException:       # ignored: the flush error re-raised below matters more than closing the discarded partial file
                pass
            raise
        self.closed = True
        self.writer.close()

    def __enter__(self):
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is None:
            self.close()
        else:
            self._discard()
            self.closed = True
            try:
                self.writer.close()
            except BaseException:
                # ignored: closing a partial output must not mask cancellation or the
                # original write failure. Such output is never published.
                pass
        return False
