"""Labeled synthetic host. Observations here are not host-capability proof."""

import copy


class SyntheticHost:
    test_double = True
    proves_host_capability = False
    label = "synthetic-test-double"

    def __init__(self, probe, faults=()):
        self.probe = copy.deepcopy(probe)
        self.faults = set(faults)
        self.calls = []
