"""Generic long builds (make/ninja/cmake --build). Off by default: enable in config/plugins.conf.
Only runs longer than MIN_RUN are reported, so quick builds stay silent."""
import re

MATCH = re.compile(r"(?:^|/)(make|ninja|cargo|bazel)$")
