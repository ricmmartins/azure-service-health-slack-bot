import json
import time

import pytest

from scripts.operation_lock import (
    DEFAULT_LOCK_NAME,
    OperationJournal,
    OperationLock,
    OperationLockError,
    membership_fingerprint,
)
from scripts.manage_alert_scopes import ScopeManagerError
from fake_blob_lock import FakeBlobClient, FakeBlobError, FakeBlobService


SUBSCRIPTION_ID = "central-sub"
RESOURCE_GROUP = "rg-central"


class FakeArm:
    """Models the immutable role-assignment lock and ARM journals."""

    def __init__(self):
        self.calls = []
        self.locks = {}
        self.blob_service_factory = FakeBlobService()
        self.locks = self.blob_service_factory.store
        self.deployments = {}
        self._etag_counter = 0

    def _next_etag(self):
        self._etag_counter += 1
        return f"etag-{self._etag_counter}"

    def invoke(self, *args):
        self.calls.append(args)
        arguments = list(args)
        if arguments[:2] == ["resource", "list"]:
            return [
                {
                    "name": "stlocktest",
                    "tags": {
                        "service-health-purpose": "operation-lock"
                    },
                }
            ]
        if arguments[:3] == ["storage", "account", "show"]:
            return {
                "primaryEndpoints": {
                    "blob": "https://stlocktest.blob.core.windows.net/"
                }
            }
        if arguments[:4] == ["storage", "account", "keys", "list"]:
            return [{"value": "fake-storage-key"}]
        assert arguments[0] == "rest"
        method = arguments[arguments.index("--method") + 1].lower()
        uri = arguments[arguments.index("--uri") + 1]
        headers = []
        if "--headers" in arguments:
            start = arguments.index("--headers") + 1
            for value in arguments[start:]:
                if value in ("--body",):
                    break
                headers.append(value)
        body = None
        if "--body" in arguments:
            body_arg = arguments[arguments.index("--body") + 1]
            assert body_arg.startswith("@")
            with open(body_arg[1:], encoding="utf-8") as handle:
                body = json.load(handle)
        if "/providers/Microsoft.Resources/deployments/" in uri:
            return self._deployment(method, uri, headers, body)
        raise AssertionError(f"Unsupported URI: {uri}")

    @staticmethod
    def _name(uri, marker):
        segment = uri.split(marker, 1)[1]
        return segment.split("?", 1)[0]

    def _lock(self, method, uri, headers, body):
        name = DEFAULT_LOCK_NAME
        if method == "get":
            resource = self.locks.get(name)
            if resource is None:
                raise ScopeManagerError(
                    f"Azure CLI command failed for {uri}",
                    status_code=404,
                    error_code="NotFound",
                )
            return resource
        if method == "put":
            if name in self.locks:
                raise ScopeManagerError(f"Azure CLI command failed: 409 Conflict for {uri}")
            for header in headers:
                if header.startswith("If-Match="):
                    expected = header.split("=", 1)[1]
                    if (
                        name not in self.locks
                        or expected != self.locks[name]["etag"]
                    ):
                        raise ScopeManagerError(
                            "Azure CLI command failed: "
                            f"412 PreconditionFailed for {uri}"
                        )
            etag = self._next_etag()
            resource = {
                "id": uri,
                "name": name,
                "type": "Microsoft.Authorization/roleAssignments",
                "etag": etag,
                "properties": body["properties"],
            }
            self.locks[name] = resource
            return resource
        if method == "delete":
            resource = self.locks.get(name)
            if resource is None:
                return None
            for header in headers:
                if header.startswith("If-Match="):
                    expected = header.split("=", 1)[1]
                    if expected != resource["etag"]:
                        raise ScopeManagerError(
                            f"Azure CLI command failed: 412 PreconditionFailed for {uri}"
                        )
            del self.locks[name]
            return None
        raise AssertionError(f"Unsupported lock method: {method}")

    def _deployment(self, method, uri, headers, body):
        name = self._name(uri, "/deployments/")
        if method == "get":
            resource = self.deployments.get(name)
            if resource is None:
                raise ScopeManagerError(
                    f"Azure CLI command failed for {uri}",
                    status_code=404,
                    error_code="DeploymentNotFound",
                )
            return resource
        if method == "put":
            value = body["properties"]["template"]["outputs"]["journalState"]["value"]
            resource = {
                "id": uri,
                "name": name,
                "etag": self._next_etag(),
                "properties": {"outputs": {"journalState": {"type": "Object", "value": value}}},
            }
            self.deployments[name] = resource
            return resource
        if method == "delete":
            resource = self.deployments.get(name)
            for header in headers:
                if (
                    header.startswith("If-Match=")
                    and resource is not None
                    and header.split("=", 1)[1] != resource["etag"]
                ):
                    raise ScopeManagerError(
                        "Journal precondition failed",
                        status_code=412,
                        error_code="PreconditionFailed",
                    )
            self.deployments.pop(name, None)
            return None
        raise AssertionError(f"Unsupported deployment method: {method}")


def lock(fake=None):
    return OperationLock(fake or FakeArm(), SUBSCRIPTION_ID, RESOURCE_GROUP)


def journal(fake):
    return OperationJournal(fake, SUBSCRIPTION_ID, RESOURCE_GROUP)


def stored_lock_metadata(fake):
    return json.loads(fake.locks[DEFAULT_LOCK_NAME]["data"])


def test_acquire_stores_metadata_and_verifies_nonce_readback():
    fake = FakeArm()
    instance = lock(fake)

    handle = instance.acquire(
        environment="production",
        command="add-subscription",
        target="subscription 'abc'",
        caller="operator@example.com",
    )

    assert handle.nonce
    notes = stored_lock_metadata(fake)
    assert notes["environment"] == "production"
    assert notes["command"] == "add-subscription"
    assert notes["nonce"] == handle.nonce
    assert notes["caller"] == "operator@example.com"


def test_acquire_fails_closed_on_conflict_without_breaking_existing_lock():
    fake = FakeArm()
    first = lock(fake).acquire(
        environment="production", command="add-subscription",
        target="t", caller="a",
    )

    with pytest.raises(OperationLockError, match="another operation appears to be in progress"):
        lock(fake).acquire(
            environment="production", command="remove-subscription",
            target="t", caller="b",
        )

    assert stored_lock_metadata(fake)["nonce"] == first.nonce


def test_acquire_removes_new_blob_when_lease_acquisition_fails(monkeypatch):
    fake = FakeArm()

    def fail_lease(_self, lease_duration=60):
        del lease_duration
        raise FakeBlobError("storage unavailable", 500)

    monkeypatch.setattr(FakeBlobClient, "acquire_lease", fail_lease)

    with pytest.raises(OperationLockError, match="Could not acquire"):
        lock(fake).acquire(
            environment="production",
            command="rotate",
            target="secret",
            caller="operator@example.com",
        )

    assert DEFAULT_LOCK_NAME not in fake.locks


def test_acquire_removes_leased_blob_when_readback_fails(monkeypatch):
    fake = FakeArm()

    def fail_readback(_self, lease=None):
        del lease
        raise FakeBlobError("read unavailable", 500)

    monkeypatch.setattr(FakeBlobClient, "download_blob", fail_readback)

    with pytest.raises(OperationLockError, match="verification failed"):
        lock(fake).acquire(
            environment="production",
            command="rotate",
            target="secret",
            caller="operator@example.com",
        )

    assert DEFAULT_LOCK_NAME not in fake.locks


def test_revalidate_detects_lost_ownership():
    fake = FakeArm()
    instance = lock(fake)
    handle = instance.acquire(environment="e", command="c", target="t", caller="a")

    del fake.locks[DEFAULT_LOCK_NAME]

    with pytest.raises(OperationLockError, match="no longer held"):
        instance.revalidate(handle)


def test_renew_extends_owned_blob_lease_expiry():
    fake = FakeArm()
    now = [1000.0]
    instance = OperationLock(
        fake,
        SUBSCRIPTION_ID,
        RESOURCE_GROUP,
        clock=lambda: now[0],
    )
    handle = instance.acquire(
        environment="e",
        command="c",
        target="t",
        caller="a",
        ttl_seconds=60,
    )
    now[0] = 1030.0

    instance.renew(handle, ttl_seconds=120)

    metadata = stored_lock_metadata(fake)
    assert metadata["expiresAt"] == 1150.0
    assert handle.metadata["expiresAt"] == 1150.0


def test_heartbeat_renews_lease_during_blocking_work():
    fake = FakeArm()
    instance = OperationLock(
        fake,
        SUBSCRIPTION_ID,
        RESOURCE_GROUP,
        heartbeat_interval=0.01,
    )
    handle = instance.acquire(
        environment="e", command="c", target="t", caller="a"
    )

    time.sleep(0.04)

    assert handle.lease.renew_count >= 2
    instance.release(handle)


def test_renew_refuses_lock_recreated_by_another_owner():
    fake = FakeArm()
    instance = lock(fake)
    handle = instance.acquire(
        environment="e", command="c", target="t", caller="a"
    )
    del fake.locks[DEFAULT_LOCK_NAME]
    lock(fake).acquire(
        environment="e", command="other", target="t", caller="b"
    )

    with pytest.raises(OperationLockError, match="no longer held"):
        instance.renew(handle)


def test_release_is_owner_only_and_deletes_leased_blob():
    fake = FakeArm()
    instance = lock(fake)
    handle = instance.acquire(environment="e", command="c", target="t", caller="a")

    instance.release(handle)

    assert DEFAULT_LOCK_NAME not in fake.locks


def test_release_refuses_to_remove_a_lock_owned_by_another_operation():
    fake = FakeArm()
    instance = lock(fake)
    handle = instance.acquire(environment="e", command="c", target="t", caller="a")
    # Simulate another process recreating the lock with a different nonce
    # after this handle's owner lost track of it (e.g. crash + manual clear).
    del fake.locks[DEFAULT_LOCK_NAME]
    other_handle = lock(fake).acquire(environment="e", command="c2", target="t2", caller="b")

    with pytest.raises(OperationLockError, match="no longer held"):
        instance.release(handle)

    assert DEFAULT_LOCK_NAME in fake.locks
    assert stored_lock_metadata(fake)["nonce"] == other_handle.nonce


def test_release_is_idempotent_when_owned_lock_is_already_absent():
    fake = FakeArm()
    instance = lock(fake)
    handle = instance.acquire(environment="e", command="c", target="t", caller="a")
    del fake.locks[DEFAULT_LOCK_NAME]

    instance.release(handle)


def test_non_not_found_error_with_404_in_resource_id_is_not_swallowed():
    class MisleadingErrorArm(FakeArm):
        def _deployment(self, method, uri, headers, body):
            if method == "get":
                raise ScopeManagerError(
                    "Internal failure for /resources/build-404",
                    status_code=500,
                    error_code="InternalServerError",
                )
            return super()._deployment(method, uri, headers, body)

    with pytest.raises(ScopeManagerError, match="build-404"):
        journal(MisleadingErrorArm()).read("missing")


def test_held_context_manager_acquires_and_releases():
    fake = FakeArm()
    instance = lock(fake)

    with instance.held(environment="e", command="c", target="t", caller="a") as handle:
        assert DEFAULT_LOCK_NAME in fake.locks
        assert handle.nonce

    assert DEFAULT_LOCK_NAME not in fake.locks


def test_held_context_manager_releases_even_on_exception():
    fake = FakeArm()
    instance = lock(fake)

    with pytest.raises(RuntimeError):
        with instance.held(environment="e", command="c", target="t", caller="a"):
            raise RuntimeError("boom")

    assert DEFAULT_LOCK_NAME not in fake.locks


def test_recover_requires_force():
    fake = FakeArm()
    lock(fake).acquire(environment="e", command="c", target="t", caller="a")

    with pytest.raises(OperationLockError, match="explicit"):
        lock(fake).recover(force=False, expected_environment="e")

    assert DEFAULT_LOCK_NAME in fake.locks


def test_recover_never_breaks_an_unexpired_lock_even_with_force():
    fake = FakeArm()
    lock(fake).acquire(environment="e", command="c", target="t", caller="a", ttl_seconds=900)

    with pytest.raises(OperationLockError, match="not expired"):
        lock(fake).recover(force=True, expected_environment="e")

    assert DEFAULT_LOCK_NAME in fake.locks


def test_recover_refuses_a_lock_from_a_different_environment():
    fake = FakeArm()
    clock = iter([1000.0, 1000.0, 2000.0])
    instance = OperationLock(fake, SUBSCRIPTION_ID, RESOURCE_GROUP, clock=lambda: next(clock))
    instance.acquire(environment="staging", command="c", target="t", caller="a", ttl_seconds=1)

    recovering = OperationLock(fake, SUBSCRIPTION_ID, RESOURCE_GROUP, clock=lambda: 2000.0)
    with pytest.raises(OperationLockError, match="different environment"):
        recovering.recover(force=True, expected_environment="production")

    assert DEFAULT_LOCK_NAME in fake.locks


def test_recover_removes_an_expired_lock_from_the_same_environment():
    fake = FakeArm()
    clock = iter([1000.0, 1000.0])
    instance = OperationLock(fake, SUBSCRIPTION_ID, RESOURCE_GROUP, clock=lambda: next(clock))
    instance.acquire(environment="production", command="c", target="t", caller="a", ttl_seconds=1)
    fake.blob_service_factory.expire_lease(DEFAULT_LOCK_NAME)

    recovering = OperationLock(fake, SUBSCRIPTION_ID, RESOURCE_GROUP, clock=lambda: 5000.0)
    result = recovering.recover(force=True, expected_environment="production")

    assert result["Status"] == "Recovered"
    assert DEFAULT_LOCK_NAME not in fake.locks


def test_recover_is_idempotent_when_already_absent():
    fake = FakeArm()
    result = lock(fake).recover(force=True, expected_environment="production")
    assert result == {"Status": "AlreadyAbsent", "LockName": DEFAULT_LOCK_NAME}


def test_status_reports_active_stale_and_absent_without_mutation():
    fake = FakeArm()
    instance = OperationLock(
        fake,
        SUBSCRIPTION_ID,
        RESOURCE_GROUP,
        clock=lambda: 1000.0,
    )
    instance.acquire(
        environment="production",
        command="rotate",
        target="secret",
        caller="operator",
        ttl_seconds=10,
    )

    assert instance.status("production")["Status"] == "Active"
    stale = OperationLock(
        fake,
        SUBSCRIPTION_ID,
        RESOURCE_GROUP,
        clock=lambda: 2000.0,
    )
    assert stale.status("production")["Status"] == "StaleBlocking"
    del fake.locks[DEFAULT_LOCK_NAME]
    assert stale.status("production")["Status"] == "Unlocked"


def test_acquire_rejects_oversized_metadata():
    fake = FakeArm()
    instance = lock(fake)
    with pytest.raises(OperationLockError, match="512-byte"):
        instance.acquire(
            environment="e",
            command="c",
            target="t" * 600,
            caller="a",
        )
    assert fake.locks == {}


def test_journal_records_reads_and_clears_state():
    fake = FakeArm()
    instance = journal(fake)
    state = {"Command": "add-subscription", "State": "Started"}

    instance.record("op-1", state)
    assert instance.read("op-1") == state

    instance.record("op-1", {**state, "State": "Completed"})
    assert instance.read("op-1")["State"] == "Completed"

    instance.clear("op-1")
    assert instance.read("op-1") is None


def test_journal_clear_of_missing_entry_is_a_no_op():
    fake = FakeArm()
    journal(fake).clear("never-existed")


def test_journal_hashes_long_operation_ids_within_arm_name_limit():
    instance = journal(FakeArm())
    operation_id = (
        "add-subscription-subscription-"
        "d61e43e0-4793-4b0e-ac08-002e8c18763f"
    )

    name = instance._deployment_name(operation_id)

    assert len(name) == 64
    assert name == instance._deployment_name(operation_id)
    assert name != instance._deployment_name(f"{operation_id}-different")


def test_journal_record_waits_until_output_is_durable():
    class AsyncJournalArm(FakeArm):
        def __init__(self):
            super().__init__()
            self.pending = {}

        def _deployment(self, method, uri, headers, body):
            name = self._name(uri, "/deployments/")
            if method == "put":
                value = body["properties"]["template"]["outputs"][
                    "journalState"
                ]["value"]
                resource = {
                    "etag": self._next_etag(),
                    "properties": {
                        "provisioningState": "Running",
                        "outputs": {},
                    },
                }
                self.deployments[name] = resource
                self.pending[name] = value
                return resource
            if method == "get" and name in self.pending:
                resource = self.deployments[name]
                resource["properties"] = {
                    "provisioningState": "Succeeded",
                    "outputs": {
                        "journalState": {
                            "type": "Object",
                            "value": self.pending.pop(name),
                        }
                    },
                }
                return resource
            return super()._deployment(
                method, uri, headers, body
            )

    fake = AsyncJournalArm()
    instance = journal(fake)

    instance.record("async", {"State": "Started"})

    assert instance.read("async") == {"State": "Started"}


def test_membership_fingerprint_is_deterministic_and_order_independent():
    a = membership_fingerprint("managementGroup", "MG-1", ["sub-b", "sub-a"])
    b = membership_fingerprint("managementGroup", "MG-1", ["SUB-A", "SUB-B"])
    c = membership_fingerprint("managementGroup", "MG-1", ["sub-a"])

    assert a == b
    assert a != c
