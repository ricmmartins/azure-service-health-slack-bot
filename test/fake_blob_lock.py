from __future__ import annotations

import itertools


class FakeBlobError(RuntimeError):
    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


class FakeDownload:
    def __init__(self, data: bytes):
        self.data = data

    def readall(self) -> bytes:
        return self.data


class FakeLease:
    def __init__(self, blob, lease_id: str):
        self.blob = blob
        self.id = lease_id
        self.renew_count = 0

    def renew(self):
        if self.blob.lease_id != self.id:
            raise FakeBlobError("lease lost", 412)
        self.renew_count += 1


class FakeBlobClient:
    _leases = itertools.count(1)

    def __init__(self, store: dict, name: str):
        self.store = store
        self.name = name

    @property
    def lease_id(self):
        item = self.store.get(self.name)
        return item.get("lease_id") if item else None

    def exists(self):
        return self.name in self.store

    def upload_blob(self, data, overwrite=False, lease=None):
        current = self.store.get(self.name)
        if current is not None and not overwrite:
            raise FakeBlobError("already exists", 409)
        if current is not None and current.get("lease_id"):
            if lease is None or lease.id != current["lease_id"]:
                raise FakeBlobError("lease mismatch", 412)
        self.store[self.name] = {
            "data": bytes(data),
            "lease_id": current.get("lease_id") if current else None,
        }

    def acquire_lease(self, lease_duration=60):
        current = self.store.get(self.name)
        if current is None:
            raise FakeBlobError("missing", 404)
        if current.get("lease_id"):
            raise FakeBlobError("already leased", 409)
        lease_id = f"lease-{next(self._leases)}"
        current["lease_id"] = lease_id
        return FakeLease(self, lease_id)

    def download_blob(self, lease=None):
        current = self.store.get(self.name)
        if current is None:
            raise FakeBlobError("missing", 404)
        if lease is not None and lease.id != current.get("lease_id"):
            raise FakeBlobError("lease mismatch", 412)
        return FakeDownload(current["data"])

    def delete_blob(self, lease=None):
        current = self.store.get(self.name)
        if current is None:
            raise FakeBlobError("missing", 404)
        if current.get("lease_id"):
            if lease is None or lease.id != current["lease_id"]:
                raise FakeBlobError("active lease", 412)
        del self.store[self.name]


class FakeContainerClient:
    def __init__(self, store: dict):
        self.store = store

    def get_blob_client(self, name: str):
        return FakeBlobClient(self.store, name)


class FakeBlobService:
    def __init__(self):
        self.store = {}

    def __call__(self, account_url: str, credential: str):
        return self

    def get_container_client(self, _name: str):
        return FakeContainerClient(self.store)

    def expire_lease(self, name: str):
        if name in self.store:
            self.store[name]["lease_id"] = None
