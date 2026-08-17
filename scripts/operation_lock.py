#!/usr/bin/env python3
"""Shared distributed operation lock and nonsecret journal for day-2 tooling.

Both `manage_alert_scopes.py` and `manage_slack_token.py` mutate resources
that belong to a single central deployment (one subscription/resource group).
Concurrent operators (or a retried automation run) racing against the same
central deployment must be serialized, and any partially applied mutation
must be discoverable and recoverable without guessing.

`OperationLock` implements a mutex using atomic Blob creation plus a finite,
renewable Blob lease in a dedicated lock-only storage account.

* The private blob body carries a compact JSON
  document identifying the environment, command, target, caller, a random
  nonce, and start/expiry timestamps.
* After acquisition the lock is immediately read back and the nonce is
  compared before any mutation.
* Every mutation revalidates the nonce immediately before doing anything
  destructive.
* Release is owner-only: it reads through the held lease, verifies the nonce,
  and deletes the blob with the lease ID.
* Lock recovery never happens implicitly. `recover()` requires an explicit
  `force=True` from an operator, requires the lock to have already expired,
  and requires the recovering caller to be operating on the same
  environment recorded in the stale lock's metadata.

`OperationJournal` persists nonsecret command/target/fingerprint/state
records as ARM deployments (`Microsoft.Resources/deployments`) with an empty
resource list and the payload surfaced only through a deployment output.
This keeps the journal entirely nonsecret (ARM deployment history is
readable by anyone with Reader access) and durable without provisioning any
new storage.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

# This module is intentionally self-contained (no import from
# manage_alert_scopes.py or configure_secure_webhook.py) so it has no
# circular dependency on either script that imports it.


LOCK_API_VERSION = "2022-04-01"
DEPLOYMENT_API_VERSION = "2021-04-01"
DEFAULT_LOCK_NAME = "service-health-operation"
DEFAULT_JOURNAL_PREFIX = "service-health-journal"
MAX_DEPLOYMENT_NAME_LENGTH = 64
DEFAULT_TTL_SECONDS = 15 * 60
MAX_NOTES_LENGTH = 512
NOT_FOUND_ERROR_CODES = frozenset(
    {"NotFound", "ResourceNotFound", "DeploymentNotFound"}
)


def member(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, dict) else default


def nested(value: Any, *path: str) -> Any:
    for segment in path:
        value = member(value, segment)
        if value is None:
            return None
    return value


class OperationLockError(RuntimeError):
    """A fail-closed distributed-lock or journal error.

    Deliberately does not subclass the caller's `ScopeManagerError` (that
    would reintroduce a circular import). Callers should catch
    `(ScopeManagerError, OperationLockError)` together.
    """


class LockHandle:
    """An acquired lock's identity, used to revalidate and release it."""

    __slots__ = (
        "name",
        "nonce",
        "metadata",
        "etag",
        "lease",
        "blob",
        "heartbeat_stop",
        "heartbeat_thread",
        "heartbeat_errors",
    )

    def __init__(
        self,
        name: str,
        nonce: str,
        metadata: dict[str, Any],
        etag: str | None,
        lease: Any = None,
        blob: Any = None,
    ) -> None:
        self.name = name
        self.nonce = nonce
        self.metadata = metadata
        self.etag = etag
        self.lease = lease
        self.blob = blob
        self.heartbeat_stop = threading.Event()
        self.heartbeat_thread = None
        self.heartbeat_errors = []


def _is_not_found(error: Exception) -> bool:
    status_code = getattr(error, "status_code", None)
    error_code = getattr(error, "error_code", None)
    return status_code == 404 or error_code in NOT_FOUND_ERROR_CODES


def _rest_json(
    azure: Any,  # AzureCli-compatible: exposes .invoke(*arguments)
    method: str,
    uri: str,
    body: dict[str, Any] | None = None,
    headers: Iterable[str] = (),
) -> Any:
    """Invoke `az rest`, writing any body through a temporary file exactly
    like `GraphClient.request`, so nonsecret payloads never appear as a
    literal command-line argument."""
    arguments = ["rest", "--method", method.lower(), "--uri", uri]
    body_path: Path | None = None
    try:
        header_args = list(headers)
        if body is not None:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".json",
                delete=False,
            ) as handle:
                json.dump(body, handle, separators=(",", ":"))
                body_path = Path(handle.name)
            header_args = ["Content-Type=application/json", *header_args]
        if header_args:
            arguments.extend(["--headers", *header_args])
        if body_path is not None:
            arguments.extend(["--body", f"@{body_path}"])
        return azure.invoke(*arguments)
    finally:
        if body_path is not None:
            body_path.unlink(missing_ok=True)


def membership_fingerprint(
    scope_kind: str,
    scope_id: str,
    member_ids: Iterable[str],
) -> str:
    """A deterministic, order-independent fingerprint of a scope's expected
    membership, used to detect drift between journal entries and reality."""
    normalized = sorted(
        {str(value).strip().casefold() for value in member_ids if str(value).strip()}
    )
    payload = json.dumps(
        {
            "scopeKind": str(scope_kind),
            "scopeId": str(scope_id).strip().casefold(),
            "members": normalized,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class OperationLock:
    """A distributed mutex backed by an atomic finite Blob lease."""

    def __init__(
        self,
        azure: Any,  # AzureCli-compatible: exposes .invoke(*arguments)
        subscription_id: str,
        resource_group: str,
        lock_name: str = DEFAULT_LOCK_NAME,
        clock: Callable[[], float] = time.time,
        nonce_factory: Callable[[], str] = lambda: secrets.token_hex(16),
        api_version: str = LOCK_API_VERSION,
        heartbeat_interval: float = 20.0,
    ) -> None:
        self.azure = azure
        self.subscription_id = subscription_id
        self.resource_group = resource_group
        self.lock_name = lock_name
        self.clock = clock
        self.nonce_factory = nonce_factory
        self.api_version = api_version
        self.heartbeat_interval = heartbeat_interval

    def _blob(self) -> Any:
        resources = [
            item
            for item in (
                self.azure.invoke(
                    "resource",
                    "list",
                    "--subscription",
                    self.subscription_id,
                    "--resource-group",
                    self.resource_group,
                    "--resource-type",
                    "Microsoft.Storage/storageAccounts",
                )
                or []
            )
            if member(member(item, "tags", {}), "service-health-purpose")
            == "operation-lock"
        ]
        if len(resources) != 1:
            raise OperationLockError(
                "Could not uniquely resolve the operation-lock storage account."
            )
        account_name = str(member(resources[0], "name", "") or "")
        details = self.azure.invoke(
            "storage",
            "account",
            "show",
            "--subscription",
            self.subscription_id,
            "--resource-group",
            self.resource_group,
            "--name",
            account_name,
        )
        endpoint = str(
            nested(details, "primaryEndpoints", "blob")
            or nested(details, "properties", "primaryEndpoints", "blob")
            or ""
        )
        keys = self.azure.invoke(
            "storage",
            "account",
            "keys",
            "list",
            "--subscription",
            self.subscription_id,
            "--resource-group",
            self.resource_group,
            "--account-name",
            account_name,
        )
        key = str(member((keys or [{}])[0], "value", "") or "")
        if not endpoint or not key:
            raise OperationLockError(
                "Operation-lock storage endpoint or access key is unavailable."
            )
        factory = getattr(self.azure, "blob_service_factory", None)
        if factory is None:
            from azure.storage.blob import BlobServiceClient

            factory = BlobServiceClient
        service = factory(account_url=endpoint, credential=key)
        return service.get_container_client(
            "operation-locks"
        ).get_blob_client(self.lock_name)

    @staticmethod
    def _metadata(blob: Any, lease: Any = None) -> dict[str, Any]:
        try:
            payload = blob.download_blob(lease=lease).readall()
        except Exception as exc:
            if getattr(exc, "status_code", None) == 404:
                return {}
            raise
        try:
            parsed = json.loads(payload.decode("utf-8"))
        except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _start_heartbeat(self, handle: LockHandle) -> None:
        def heartbeat() -> None:
            while not handle.heartbeat_stop.wait(
                self.heartbeat_interval
            ):
                try:
                    handle.lease.renew()
                except Exception as exc:
                    handle.heartbeat_errors.append(exc)
                    return

        thread = threading.Thread(
            target=heartbeat,
            name=f"{handle.name}-lease-heartbeat",
            daemon=True,
        )
        handle.heartbeat_thread = thread
        thread.start()

    @staticmethod
    def _check_heartbeat(handle: LockHandle) -> None:
        if handle.heartbeat_errors:
            raise OperationLockError(
                f"Operation lock '{handle.name}' Blob lease heartbeat failed."
            ) from handle.heartbeat_errors[0]

    @staticmethod
    def _stop_heartbeat(handle: LockHandle) -> None:
        handle.heartbeat_stop.set()
        if handle.heartbeat_thread is not None:
            handle.heartbeat_thread.join(timeout=5)

    def acquire(
        self,
        environment: str,
        command: str,
        target: str,
        caller: str,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> LockHandle:
        nonce = self.nonce_factory()
        started_at = self.clock()
        metadata = {
            "environment": environment,
            "command": command,
            "target": target,
            "caller": caller,
            "nonce": nonce,
            "startedAt": started_at,
            "expiresAt": started_at + ttl_seconds,
        }
        payload = json.dumps(
            metadata, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        if len(payload) > MAX_NOTES_LENGTH:
            raise OperationLockError(
                "Operation lock metadata exceeds the 512-byte limit."
            )
        blob = self._blob()
        try:
            blob.upload_blob(payload, overwrite=False)
        except Exception as exc:
            if getattr(exc, "status_code", None) not in {409, 412}:
                raise
            raise OperationLockError(
                f"Could not acquire operation lock '{self.lock_name}': another "
                f"operation appears to be in progress.\n{exc}"
            ) from exc
        try:
            lease = blob.acquire_lease(lease_duration=60)
        except Exception as exc:
            if getattr(exc, "status_code", None) in {409, 412}:
                raise OperationLockError(
                    f"Could not acquire operation lock '{self.lock_name}': "
                    "the newly created lock blob was leased concurrently."
                ) from exc
            cleanup_error = None
            try:
                blob.delete_blob()
            except Exception as delete_exc:
                cleanup_error = delete_exc
            error = OperationLockError(
                f"Could not acquire the Blob lease for operation lock "
                f"'{self.lock_name}'."
            )
            if cleanup_error is not None:
                error.add_note(
                    "The unleased lock blob could not be removed: "
                    f"{cleanup_error}"
                )
            raise error from exc
        try:
            current_metadata = self._metadata(blob, lease)
            if current_metadata.get("nonce") != nonce:
                raise OperationLockError(
                    f"Operation lock '{self.lock_name}' verification failed: "
                    "the read-back nonce did not match immediately after "
                    "acquisition. Another operation may have raced this one."
                )
        except Exception as verification_exc:
            try:
                blob.delete_blob(lease=lease)
            except Exception as cleanup_exc:
                verification_exc.add_note(
                    "The leased lock blob could not be removed after "
                    f"verification failed: {cleanup_exc}"
                )
            if isinstance(verification_exc, OperationLockError):
                raise
            raise OperationLockError(
                f"Operation lock '{self.lock_name}' verification failed."
            ) from verification_exc
        handle = LockHandle(
            name=self.lock_name,
            nonce=nonce,
            metadata=metadata,
            etag=None,
            lease=lease,
            blob=blob,
        )
        self._start_heartbeat(handle)
        return handle

    def revalidate(self, handle: LockHandle) -> None:
        self._check_heartbeat(handle)
        blob = handle.blob
        try:
            current_metadata = self._metadata(blob, handle.lease)
        except Exception as exc:
            raise OperationLockError(
                f"Operation lock '{handle.name}' is no longer held; the blob "
                "lease was lost."
            ) from exc
        if current_metadata.get("nonce") != handle.nonce:
            raise OperationLockError(
                f"Operation lock '{handle.name}' is no longer held by this "
                "process; refusing to proceed with the mutation."
            )

    def renew(
        self,
        handle: LockHandle,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        """Renew and revalidate the finite blob lease."""
        self._check_heartbeat(handle)
        blob = handle.blob
        try:
            current_metadata = self._metadata(blob, handle.lease)
        except Exception as exc:
            raise OperationLockError(
                f"Operation lock '{handle.name}' is no longer held; the blob "
                "lease was lost."
            ) from exc
        if current_metadata.get("nonce") != handle.nonce:
            raise OperationLockError(
                f"Operation lock '{handle.name}' is no longer held by this "
                "process; refusing to revalidate it."
            )
        handle.lease.renew()
        renewed = dict(current_metadata)
        renewed["expiresAt"] = self.clock() + ttl_seconds
        blob.upload_blob(
            json.dumps(
                renewed, separators=(",", ":"), sort_keys=True
            ).encode("utf-8"),
            overwrite=True,
            lease=handle.lease,
        )
        handle.metadata = renewed

    def release(self, handle: LockHandle) -> None:
        self._stop_heartbeat(handle)
        blob = handle.blob
        try:
            current_metadata = self._metadata(blob, handle.lease)
        except Exception as exc:
            if getattr(exc, "status_code", None) == 404:
                return
            raise OperationLockError(
                f"Operation lock '{handle.name}' is no longer held; the blob "
                "lease was lost."
            ) from exc
        if not current_metadata:
            return
        if current_metadata.get("nonce") != handle.nonce:
            raise OperationLockError(
                f"Refusing to release operation lock '{handle.name}': it is "
                "currently owned by a different operation."
            )
        try:
            blob.delete_blob(lease=handle.lease)
        except Exception as exc:
            if getattr(exc, "status_code", None) != 404:
                raise OperationLockError(
                    f"Operation lock '{handle.name}' release failed; the "
                    "leased blob remains blocking."
                ) from exc

    def status(self, expected_environment: str | None = None) -> dict[str, Any]:
        """Return lock health without modifying or recovering the lock."""
        blob = self._blob()
        if not blob.exists():
            return {"Status": "Unlocked", "LockName": self.lock_name}
        metadata = self._metadata(blob)
        if not metadata:
            raise OperationLockError(
                f"Operation lock '{self.lock_name}' metadata is invalid; it "
                "remains blocking."
            )
        environment = metadata.get("environment")
        if expected_environment and environment != expected_environment:
            raise OperationLockError(
                f"Operation lock '{self.lock_name}' belongs to a different "
                f"environment ('{environment}')."
            )
        expires_at = metadata.get("expiresAt")
        if not isinstance(expires_at, (int, float)):
            raise OperationLockError(
                f"Operation lock '{self.lock_name}' expiry metadata is invalid; "
                "it remains blocking."
            )
        return {
            "Status": (
                "StaleBlocking" if expires_at <= self.clock() else "Active"
            ),
            "LockName": self.lock_name,
            "Environment": environment,
            "Command": metadata.get("command"),
            "Caller": metadata.get("caller"),
            "StartedAt": metadata.get("startedAt"),
            "ExpiresAt": expires_at,
        }

    def recover(self, force: bool, expected_environment: str) -> dict[str, Any]:
        """Explicitly break an abandoned lock. Never called implicitly."""
        if not force:
            raise OperationLockError(
                "Recovering an operation lock requires an explicit, "
                "operator-confirmed force flag."
            )
        blob = self._blob()
        if not blob.exists():
            return {"Status": "AlreadyAbsent", "LockName": self.lock_name}
        metadata = self._metadata(blob)
        if metadata.get("environment") != expected_environment:
            raise OperationLockError(
                f"Refusing to recover operation lock '{self.lock_name}': it "
                f"belongs to a different environment ('{metadata.get('environment')}')."
            )
        expires_at = metadata.get("expiresAt")
        if not isinstance(expires_at, (int, float)) or expires_at > self.clock():
            raise OperationLockError(
                f"Refusing to recover operation lock '{self.lock_name}': it has "
                "not expired. Wait for expiry or confirm the owning operation "
                "is truly abandoned before retrying."
            )
        try:
            blob.delete_blob()
        except Exception as exc:
            raise OperationLockError(
                "The stale lock still has an active blob lease and cannot be "
                "recovered safely."
            ) from exc
        if blob.exists():
            raise OperationLockError(
                f"Operation lock '{self.lock_name}' recovery was not proven: "
                "the lock remains present."
            )
        return {
            "Status": "Recovered",
            "LockName": self.lock_name,
            "PriorMetadata": metadata,
        }

    def held(
        self,
        environment: str,
        command: str,
        target: str,
        caller: str,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> "_HeldLock":
        return _HeldLock(self, environment, command, target, caller, ttl_seconds)


class _HeldLock:
    """Context manager wrapping acquire()/release() around a mutation."""

    def __init__(
        self,
        lock: OperationLock,
        environment: str,
        command: str,
        target: str,
        caller: str,
        ttl_seconds: int,
    ) -> None:
        self.lock = lock
        self.environment = environment
        self.command = command
        self.target = target
        self.caller = caller
        self.ttl_seconds = ttl_seconds
        self.handle: LockHandle | None = None

    def __enter__(self) -> LockHandle:
        self.handle = self.lock.acquire(
            self.environment,
            self.command,
            self.target,
            self.caller,
            self.ttl_seconds,
        )
        return self.handle

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self.handle is not None:
            self.lock.release(self.handle)
        return False


class OperationJournal:
    """A nonsecret, durable operation journal backed by ARM deployments."""

    def __init__(
        self,
        azure: Any,  # AzureCli-compatible: exposes .invoke(*arguments)
        subscription_id: str,
        resource_group: str,
        deployment_prefix: str = DEFAULT_JOURNAL_PREFIX,
        api_version: str = DEPLOYMENT_API_VERSION,
    ) -> None:
        self.azure = azure
        self.subscription_id = subscription_id
        self.resource_group = resource_group
        self.deployment_prefix = deployment_prefix
        self.api_version = api_version

    def _deployment_name(self, operation_id: str) -> str:
        name = f"{self.deployment_prefix}-{operation_id}"
        if len(name) <= MAX_DEPLOYMENT_NAME_LENGTH:
            return name
        digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]
        prefix_length = MAX_DEPLOYMENT_NAME_LENGTH - len(digest) - 1
        return f"{name[:prefix_length]}-{digest}"

    def _uri(self, operation_id: str) -> str:
        name = self._deployment_name(operation_id)
        return (
            f"/subscriptions/{self.subscription_id}/resourceGroups/{self.resource_group}"
            f"/providers/Microsoft.Resources/deployments/{name}"
            f"?api-version={self.api_version}"
        )

    def record(self, operation_id: str, state: dict[str, Any]) -> None:
        template = {
            "$schema": (
                "https://schema.management.azure.com/schemas/2019-04-01/"
                "deploymentTemplate.json#"
            ),
            "contentVersion": "1.0.0.0",
            "resources": [],
            "outputs": {
                "journalState": {"type": "object", "value": state},
            },
        }
        body = {
            "properties": {
                "mode": "Incremental",
                "template": template,
                "parameters": {},
            }
        }
        response = _rest_json(
            self.azure, "put", self._uri(operation_id), body=body
        )
        for attempt in range(30):
            resource = (
                response
                if attempt == 0 and isinstance(response, dict)
                else self._resource(operation_id)
            )
            provisioning_state = str(
                nested(resource, "properties", "provisioningState")
                or ""
            ).casefold()
            stored = nested(
                resource,
                "properties",
                "outputs",
                "journalState",
                "value",
            )
            if (
                provisioning_state in {"", "succeeded"}
                and stored == state
            ):
                return
            if provisioning_state in {
                "failed",
                "canceled",
                "cancelled",
            }:
                raise OperationLockError(
                    "Operation journal deployment reached terminal state "
                    f"'{provisioning_state}'."
                )
            time.sleep(min(0.25 * (attempt + 1), 2.0))
        raise OperationLockError(
            "Operation journal durability was not proven before the "
            "mutation timeout."
        )

    def _resource(self, operation_id: str) -> dict[str, Any] | None:
        try:
            response = _rest_json(
                self.azure, "get", self._uri(operation_id)
            )
        except Exception as exc:
            if _is_not_found(exc):
                return None
            raise
        if not isinstance(response, dict):
            raise OperationLockError(
                "Operation journal read did not return an ARM resource."
            )
        return response

    def read(self, operation_id: str) -> dict[str, Any] | None:
        response = self._resource(operation_id)
        if response is None:
            return None
        return nested(response, "properties", "outputs", "journalState", "value")

    def clear(self, operation_id: str) -> bool:
        try:
            _rest_json(
                self.azure,
                "delete",
                self._uri(operation_id),
            )
        except Exception as exc:
            if _is_not_found(exc):
                return True
            raise
        return self._resource(operation_id) is None
