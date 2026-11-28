"""Blocked lifecycle results. Diagnosis text never echoes credential material."""


class LifecycleError(Exception):
    def __init__(self, code, http, diagnosis, plan=None):
        super().__init__(diagnosis)
        self.code = code
        self.http = http
        self.diagnosis = diagnosis
        self.plan = {} if plan is None else plan
