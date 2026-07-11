"""reviewer_harness_selection — external reviewer harness 选择纯核(R15/R20)。

见 :mod:`core.reviewer_harness_selection.reviewer_harness_selection`。
"""

from core.reviewer_harness_selection.reviewer_harness_selection import (  # noqa: F401
    RULE_TWO_OR_MORE_AVAILABLE_PSEUDO_RANDOM_PAIR,
    RULE_EXACTLY_TWO_AVAILABLE_FORCED_PAIR,
    RULE_SINGLE_AVAILABLE_SAME_HARNESS_DOUBLE,
    RULE_ZERO_AVAILABLE_LAST_RESORT,
    SELECTION_MODE_FORCED_PAIR,
    SELECTION_MODE_NONE_AVAILABLE,
    SELECTION_MODE_PSEUDO_RANDOM_TWO,
    SELECTION_MODE_SAME_HARNESS_DOUBLE,
    ReviewerHarnessSelection,
    compute_reviewer_harness_selection,
    pseudo_random_select_from_eligible_pool,
)
