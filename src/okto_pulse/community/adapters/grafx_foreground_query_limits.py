"""Finite native operator ceilings for foreground readers, not rebuild scans."""
from types import MappingProxyType


FOREGROUND_QUERY_LIMITS = MappingProxyType({
    'max_result_rows': 10000,
    'max_intermediate_rows': 50000,
    'max_traversal_expansions': 50000,
    'max_traversal_paths': 10000,
    'query_memory_budget_bytes': 16 * 1024 * 1024,
})
FOREGROUND_READER_BUFFER_MB = 16


def foreground_query_options(configured):
    """Preserve all native options and never widen an operator's lower bound."""
    options = dict(configured)
    for key, ceiling in FOREGROUND_QUERY_LIMITS.items():
        current = options.get(key)
        if current is not None and (type(current) is not int or current <= 0):
            raise ValueError(f'invalid_foreground_query_option:{key}')
        options[key] = ceiling if current is None else min(current, ceiling)
    return options
