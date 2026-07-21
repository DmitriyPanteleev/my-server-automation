"""Распределённые лизы (аренды) на объектах S3 для шардированной очереди работ.

Лиза — это S3-объект с TTL, который захватывается атомарным условным PutObject
(IfNoneMatch/IfMatch), поэтому несколько pod'ов берут разные группы без отдельной
БД и без двойной обработки. Владелец продлевает лизу heartbeat'ом; протухшую (TTL
истёк) лизу другой pod может перехватить. Так repack и move параллелят работу по
группам proxy.
"""

import json
import socket
import threading
import time
from datetime import datetime, timezone

from botocore.exceptions import ClientError


def utc_iso(ts=None):
    if ts is None:
        ts = time.time()
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def format_client_error(exc):
    response = getattr(exc, "response", {}) or {}
    error = response.get("Error", {}) or {}
    code = error.get("Code", "Unknown")
    message = error.get("Message", str(exc))
    return f"{code}: {message}"


def is_precondition_failed(exc):
    response = getattr(exc, "response", {}) or {}
    error = response.get("Error", {}) or {}
    code = str(error.get("Code") or "")
    status = int((response.get("ResponseMetadata") or {}).get("HTTPStatusCode") or 0)
    return code in {"PreconditionFailed", "ConditionalRequestConflict"} or status in {409, 412}


def require_conditional_put_support(s3):
    members = s3.meta.service_model.operation_model("PutObject").input_shape.members
    missing = [name for name in ("IfNoneMatch", "IfMatch") if name not in members]
    if missing:
        raise RuntimeError(
            "S3 conditional PutObject support is required for sharding leases; "
            f"missing={','.join(missing)}. Use a newer boto3/botocore runtime."
        )


def delete_supports_if_match(s3):
    members = s3.meta.service_model.operation_model("DeleteObject").input_shape.members
    return "IfMatch" in members


class S3Lease:
    """Одна захваченная лиза: продлевает себя heartbeat'ом и освобождает при release."""

    def __init__(
        self,
        *,
        client,
        bucket,
        key,
        etag,
        owner,
        ttl_seconds,
        heartbeat_seconds,
        metadata,
        log,
    ):
        self.client = client
        self.bucket = bucket
        self.key = key
        self.etag = etag
        self.owner = owner
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.heartbeat_seconds = max(1, int(heartbeat_seconds))
        self.metadata = dict(metadata)
        self.log = log
        self.lost = False
        self._stop_event = threading.Event()
        self._thread = None
        self._delete_if_match = delete_supports_if_match(client)

    def body(self):
        now = time.time()
        payload = dict(self.metadata)
        payload.update(
            {
                "owner": self.owner,
                "hostname": socket.gethostname(),
                "updated_at": utc_iso(now),
                "expires_at": utc_iso(now + self.ttl_seconds),
                "ttl_seconds": self.ttl_seconds,
            }
        )
        return (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")

    def renew_once(self):
        try:
            response = self.client.put_object(
                Bucket=self.bucket,
                Key=self.key,
                Body=self.body(),
                ContentType="application/json",
                IfMatch=self.etag,
            )
            self.etag = response.get("ETag", self.etag)
            return True
        except ClientError as exc:
            self.lost = True
            self.log("ERROR", f"Lost S3 lease s3://{self.bucket}/{self.key}: {format_client_error(exc)}")
            return False

    def start_heartbeat(self):
        if self._thread is not None:
            return

        def run():
            while not self._stop_event.wait(self.heartbeat_seconds):
                if not self.renew_once():
                    return

        self._thread = threading.Thread(target=run, name="s3-lease-heartbeat", daemon=True)
        self._thread.start()

    def stop_heartbeat(self):
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def release(self):
        kwargs = {
            "Bucket": self.bucket,
            "Key": self.key,
        }
        if self._delete_if_match and self.etag:
            kwargs["IfMatch"] = self.etag
        try:
            self.client.delete_object(**kwargs)
        except ClientError as exc:
            self.log("WARN", f"Failed to release S3 lease s3://{self.bucket}/{self.key}: {format_client_error(exc)}")


class S3LeaseClient:
    """Захват лиз: создаёт новые, перехватывает протухшие, проверяет активные по префиксу."""

    def __init__(self, *, client, bucket, owner, ttl_seconds, heartbeat_seconds, log):
        require_conditional_put_support(client)
        self.client = client
        self.bucket = bucket
        self.owner = owner
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.heartbeat_seconds = max(1, int(heartbeat_seconds))
        self.log = log

    def _new_lease(self, key, etag, metadata):
        return S3Lease(
            client=self.client,
            bucket=self.bucket,
            key=key,
            etag=etag,
            owner=self.owner,
            ttl_seconds=self.ttl_seconds,
            heartbeat_seconds=self.heartbeat_seconds,
            metadata=metadata,
            log=self.log,
        )

    def _put_new(self, key, metadata):
        lease = self._new_lease(key, "", metadata)
        response = self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=lease.body(),
            ContentType="application/json",
            IfNoneMatch="*",
        )
        lease.etag = response.get("ETag", "")
        return lease

    def _get_existing(self, key):
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        body = response["Body"].read()
        try:
            payload = json.loads(body.decode("utf-8"))
            if not isinstance(payload, dict):
                payload = {}
        except Exception:
            payload = {}
        return payload, response.get("ETag", "")

    def _is_stale(self, payload):
        expires_ts = parse_utc(payload.get("expires_at"))
        if expires_ts is not None:
            return expires_ts <= time.time()
        updated_ts = parse_utc(payload.get("updated_at"))
        if updated_ts is None:
            return True
        return updated_ts + self.ttl_seconds <= time.time()

    def try_acquire(self, key, metadata):
        """Попытаться захватить лизу: создать новую или перехватить протухшую.

        Возвращает S3Lease при успехе или None, если лиза держится живым владельцем
        либо её перехватил кто-то другой в гонке.
        """
        try:
            return self._put_new(key, metadata)
        except ClientError as exc:
            if not is_precondition_failed(exc):
                raise

        try:
            existing, etag = self._get_existing(key)
        except ClientError as exc:
            if is_precondition_failed(exc):
                return None
            raise

        if not self._is_stale(existing):
            return None

        lease = self._new_lease(key, etag, metadata)
        try:
            response = self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=lease.body(),
                ContentType="application/json",
                IfMatch=etag,
            )
            lease.etag = response.get("ETag", "")
            self.log("WARN", f"Stole stale S3 lease: s3://{self.bucket}/{key}")
            return lease
        except ClientError as exc:
            if is_precondition_failed(exc):
                return None
            raise

    def has_active_leases(self, prefix):
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(
            Bucket=self.bucket,
            Prefix=prefix.rstrip("/") + "/",
            PaginationConfig={"PageSize": 1000},
        ):
            for obj in page.get("Contents", []):
                key = obj.get("Key", "")
                if not key.endswith(".json"):
                    continue
                try:
                    payload, _etag = self._get_existing(key)
                except ClientError:
                    continue
                if not self._is_stale(payload):
                    return True
        return False
