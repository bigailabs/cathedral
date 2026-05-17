"""v3 dataset catalog + training-export skeleton.

Pairs the signed v3 wire row (see ``cathedral.v3.sign``) with the
private score-record sidecar (see ``cathedral.v3.score_sidecar``) and
the encrypted Hermes TraceBundle (see
``cathedral.eval.bundle_publisher``). Lets us catalog every
bug_isolation_v1 eval privately and, later, fan out into training
datasets (SFT, DPO/RM) without re-running evals.

Skeleton scope: prompt/completion parsing of the Hermes bundle is a
follow-up. This module locks the schemas, the firewall contract
(no hidden oracle in SFT/RM rows) and the storage layout so the
catalog can start accumulating rows immediately.
"""

from cathedral.v3.datasets.catalog import (
    CATALOG_SCHEMA,
    CatalogRow,
    CatalogValidationError,
    append_catalog_row,
    build_catalog_row,
    split_for,
    validate_catalog_row,
)
from cathedral.v3.datasets.export import (
    FAILURE_ANALYSIS_SCHEMA,
    RM_PAIRS_SCHEMA,
    SFT_SUCCESS_SCHEMA,
    export_failure_analysis,
    export_rm_pairs,
    export_sft_success,
)

__all__ = [
    "CATALOG_SCHEMA",
    "FAILURE_ANALYSIS_SCHEMA",
    "RM_PAIRS_SCHEMA",
    "SFT_SUCCESS_SCHEMA",
    "CatalogRow",
    "CatalogValidationError",
    "append_catalog_row",
    "build_catalog_row",
    "export_failure_analysis",
    "export_rm_pairs",
    "export_sft_success",
    "split_for",
    "validate_catalog_row",
]
