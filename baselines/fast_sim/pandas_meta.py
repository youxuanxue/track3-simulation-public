"""Byte-exact ``b'pandas'`` parquet schema metadata, built without pandas.

The native write path must attach pandas-compatible schema metadata to the
message ledger so the official scorer's ``pd.read_parquet`` (no
``dtype_backend``) restores the nullable Int64 columns instead of promoting
19-digit ns timestamps to float64. ``streaming.ParquetSink`` used to derive it
through ``pa.Table.from_pandas(as_pandas(empty), preserve_index=False)`` —
correct, but it costs a ~250 ms pandas import in every ``simulate`` process.

pandas 1.5.3's ``from_pandas`` output is a constant JSON blob for these two
fixed schemas (ground truth dumped from the stable image:
``out/exp-0007/pandas_meta_ground_truth.json``), so this module rebuilds it
deterministically. Key order and ``json.dumps`` default separators
(``", "`` / ``": "``) reproduce the pandas/pyarrow serialization byte for
byte; ``creator.version`` follows the installed pyarrow, exactly like
``from_pandas``. ``out/exp-0007/check_pandas_meta.py`` asserts byte-equality
against the live pandas path inside the image.

The trace schema carries **no** pandas metadata on the native path (verified
against the stable image), and this module preserves that: only the ledger
gets a metadata dict.
"""

from __future__ import annotations

import json

import pyarrow as pa

# pandas version pinned by the Dockerfile numeric stack. The byte-equality
# check in out/exp-0007/ fails loudly if the image ever drifts from it.
_PANDAS_VERSION = "1.5.3"

# (name, pandas_type, numpy_type) per column, in schema order. The numpy_type
# is what ``native.as_pandas``'s types_mapper yields: every int64 column maps
# to pandas Int64Dtype and every int32 to Int32Dtype — not just the nullable
# ones — and string columns map to StringDtype ("unicode" / "string").
_LEDGER_COLUMNS: list[tuple[str, str, str]] = [
    ("seq", "int64", "Int64"),
    ("t_recv_ns", "int64", "Int64"),
    ("t_send_ns", "int64", "Int64"),
    ("latency_ns", "int64", "Int64"),
    ("src_id", "int32", "Int32"),
    ("dst_id", "int32", "Int32"),
    ("message_id", "int64", "Int64"),
    ("msg_type", "unicode", "string"),
    ("order_id", "int64", "Int64"),
    ("causal_parent", "int64", "Int64"),
]


def _pandas_json(columns: list[tuple[str, str, str]]) -> bytes:
    payload = {
        "index_columns": [],
        "column_indexes": [],
        "columns": [
            {
                "name": name,
                "field_name": name,
                "pandas_type": pandas_type,
                "numpy_type": numpy_type,
                "metadata": None,
            }
            for name, pandas_type, numpy_type in columns
        ],
        "creator": {"library": "pyarrow", "version": pa.__version__},
        "pandas_version": _PANDAS_VERSION,
    }
    return json.dumps(payload).encode("utf-8")


#: Exact replacement for
#: ``pa.Table.from_pandas(as_pandas(<empty ledger>), preserve_index=False).schema.metadata``.
LEDGER_SCHEMA_METADATA: dict[bytes, bytes] = {b"pandas": _pandas_json(_LEDGER_COLUMNS)}

#: The native trace schema intentionally carries no pandas metadata.
TRACE_SCHEMA_METADATA = None
