from copy import deepcopy
from types import SimpleNamespace


class MemoryStore:
    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return deepcopy(self.data.get(key, default))

    def put(self, key, value, *, skip_if_exists=False):
        if skip_if_exists and key in self.data:
            return False
        self.data[key] = deepcopy(value)
        return True

    def pop(self, key, default=None):
        return self.data.pop(key, default)

    def keys(self):
        return list(self.data.keys())

    def items(self):
        return [(key, deepcopy(value)) for key, value in self.data.items()]

    def __setitem__(self, key, value):
        self.data[key] = deepcopy(value)

    def __getitem__(self, key):
        return deepcopy(self.data[key])


class FakeRequest:
    def __init__(self, token="", host="203.0.113.10", headers=None):
        self.cookies = {}
        if token:
            self.cookies["raiw_session"] = token
        self.client = SimpleNamespace(host=host)
        self.headers = dict(headers or {})
