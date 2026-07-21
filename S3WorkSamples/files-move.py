#!/usr/bin/env python3

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
import warnings
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from datetime import datetime, time as dt_time, timedelta, timezone

from common import (
    bootstrap_log,
    build_metrics_url,
    chunked,
    env_bool,
    env_int,
    env_str,
    env_str_optional,
    format_client_error,
    hms,
    utc_iso,
)

warnings.filterwarnings("ignore", message="Boto3 will no longer support Python .*")


try:
    import boto3
    from boto3.s3.transfer import TransferConfig
    from botocore.config import Config
    from botocore.exceptions import ClientError
    from s3_lease import S3LeaseClient, is_precondition_failed
except ImportError as exc:
    bootstrap_log("ERROR", f"Required Python module is missing: {exc}. Install boto3 before running streaming-move.py.")
    raise SystemExit(1)


def basename_s3(key):
    return key.rstrip("/").rsplit("/", 1)[-1]


SRC_BUCKET = env_str("SRC_BUCKET")
DST_BUCKET = env_str("DST_BUCKET")
ARTIFACTS_BUCKET = env_str("ARTIFACTS_BUCKET")
# Битые/нечитаемые исходные файлы копируются сюда (по их proxy/group) перед
# удалением из исходного места, чтобы их можно было разобрать позже. Если бакет
# не задан отдельно — используется исходный (отличается только префикс).
QUARANTINE_BUCKET = os.environ.get("QUARANTINE_BUCKET") or SRC_BUCKET

# Базовый служебный префикс обязателен; подпрефиксы по умолчанию выводятся из него
# и при необходимости переопределяются отдельными env (производная величина, а не
# «дефолт, привязанный к окружению»).
ARTIFACTS_PREFIX = env_str("ARTIFACTS_PREFIX")
MANIFESTS_PREFIX = os.environ.get("MANIFESTS_PREFIX", f"{ARTIFACTS_PREFIX}/manifests")
STATE_PREFIX = os.environ.get("STATE_PREFIX", f"{ARTIFACTS_PREFIX}/state")
LOCKS_PREFIX = os.environ.get("LOCKS_PREFIX", f"{ARTIFACTS_PREFIX}/locks")
MOVE_ARTIFACTS_PREFIX = os.environ.get("MOVE_ARTIFACTS_PREFIX", f"{ARTIFACTS_PREFIX}/move")
RUNS_PREFIX = os.environ.get("RUNS_PREFIX", f"{ARTIFACTS_PREFIX}/runs/move")
LEASES_PREFIX = os.environ.get("LEASES_PREFIX", f"{ARTIFACTS_PREFIX}/leases")
SHARD_DONE_PREFIX = os.environ.get("SHARD_DONE_PREFIX", f"{ARTIFACTS_PREFIX}/shards/done")
QUARANTINE_PREFIX = env_str("QUARANTINE_PREFIX")
# Управляющий слой адаптивного бюджета (общий с repack): каждый pod move пишет сюда,
# сколько он разгрёб за прогон, чтобы repack подобрал бюджет под ночную ёмкость move.
# См. common.next_repack_budget.
CONTROL_PREFIX = os.environ.get("CONTROL_PREFIX", f"{ARTIFACTS_PREFIX}/control")
MOVE_STATS_PREFIX = os.environ.get("MOVE_STATS_PREFIX", f"{CONTROL_PREFIX}/move-stats")
MOVE_LAST_RUN_KEY = os.environ.get("MOVE_LAST_RUN_KEY", f"{CONTROL_PREFIX}/move-last-run.json")

RUN_DATE = os.environ.get("RUN_DATE", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
RUN_TS = os.environ.get("RUN_TS", datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
RUN_START_TS = int(time.time())
SHARD_RUN_ID = os.environ.get("SHARD_RUN_ID") or RUN_TS
WORKER_ID = os.environ.get("WORKER_ID") or os.environ.get("HOSTNAME") or f"worker-{os.getpid()}"
# Стабильный ограниченный id воркера для grouping-ключа метрик (completion index
# Indexed Job); по умолчанию "" — один общий ключ (не-indexed режим).
WORKER_INDEX = os.environ.get("WORKER_INDEX") or os.environ.get("JOB_COMPLETION_INDEX") or ""

WINDOW_STOP_HOUR_UTC = env_int("WINDOW_STOP_HOUR_UTC")
SOFT_STOP_GRACE_SECONDS = env_int("SOFT_STOP_GRACE_SECONDS")
LOCK_STALE_AFTER_SECONDS = env_int("LOCK_STALE_AFTER_SECONDS")
LEASE_TTL_SECONDS = max(60, env_int("LEASE_TTL_SECONDS"))
LEASE_HEARTBEAT_SECONDS = max(10, env_int("LEASE_HEARTBEAT_SECONDS"))
CLAIM_IDLE_SLEEP_SECONDS = max(1, env_int("CLAIM_IDLE_SLEEP_SECONDS"))
SHARDING_ENABLED = env_bool("SHARDING_ENABLED")

# Авто-восстановление «залипших» манифестов (по умолчанию включено). Если курсор
# группы уже продвинут ЗА ещё ожидающий манифест, такой манифест нельзя безопасно
# перекопировать (в target могли лечь более новые данные), и он висел бы вечно,
# блокируя repack через interlock. При включённом флаге move ретайрит его, только
# если размер target байт-в-байт совпадает с compacted_size (доказательство, что
# copy-back состоялся). 0 — вместо этого жёстко падать с ошибкой.
AUTO_RECONCILE_STALE = env_bool("MOVE_AUTO_RECONCILE")

MOVE_WORKERS = max(1, env_int("MOVE_WORKERS"))
MOVE_PREFETCH = max(1, env_int("MOVE_PREFETCH"))
REPORT_EVERY = env_int("REPORT_EVERY")
S3_MAX_ATTEMPTS = max(1, env_int("S3_MAX_ATTEMPTS"))
TRACEBACK_ON_ERROR = env_bool("TRACEBACK_ON_ERROR")
COPY_MULTIPART_THRESHOLD_MB = max(1, env_int("COPY_MULTIPART_THRESHOLD_MB"))
COPY_MULTIPART_CHUNKSIZE_MB = max(1, env_int("COPY_MULTIPART_CHUNKSIZE_MB"))
# Параллельных частей multipart-copy на один компактный zip. Параллельные части
# ускоряют copy-back в разы по сравнению с последовательным.
COPY_PART_WORKERS = max(1, env_int("COPY_PART_WORKERS"))
# Параллельных батчей DeleteObjects (по 1000 ключей) внутри группы. Параллелизм
# подводит один префикс к потолку S3 ~3500 req/s; ~8 — это «колено»: выше потолок
# превышается и провоцирует 503 SlowDown с обвалом пропускной способности, поэтому
# НЕ повышать без повторных замеров. Манифесты по-прежнему применяются
# последовательно (курсор группы двигается по порядку) — параллелятся только
# удаления внутри одного манифеста.
MOVE_DELETE_WORKERS = max(1, env_int("MOVE_DELETE_WORKERS"))

# Ретрай манифеста поверх ретраев botocore. Если манифест не дался из-за транзиентной
# ошибки (сеть/чтение/кратковременный сбой S3), пробуем обработать его заново
# несколько раз с паузой, а не бросаем сразу в бэклог. Оригиналы целы (repack их уже
# прочёл при пережатии), а повтор обработки идемпотентен, поэтому это безопасно.
# Детерминированные сбои (несовпадение размера target, отсутствие компактного
# объекта и т.п.) НЕ ретраятся — повтор им не поможет.
MANIFEST_MAX_ATTEMPTS = max(1, env_int("MANIFEST_MAX_ATTEMPTS"))
MANIFEST_RETRY_BASE_SECONDS = max(0, env_int("MANIFEST_RETRY_BASE_SECONDS"))

AWS_REGION = env_str("AWS_REGION")
PUSHGATEWAY_URL = env_str("PUSHGATEWAY_URL")
# Префикс имён всех метрик Prometheus (без завершающего "_").
METRICS_PREFIX = env_str("METRICS_PREFIX")
METRICS_JOB = env_str("METRICS_JOB")
METRICS_CLUSTER = env_str("METRICS_CLUSTER")
METRICS_SERVICE = env_str("METRICS_SERVICE")
METRICS_URL = os.environ.get("METRICS_URL") or build_metrics_url(
    PUSHGATEWAY_URL, METRICS_JOB, METRICS_CLUSTER, METRICS_SERVICE, WORKER_INDEX
)

MOVE_LOCK_KEY = f"{LOCKS_PREFIX}/move.lock"
RUN_MARKER_KEY = f"{RUNS_PREFIX}/{RUN_TS}.json"
DONE_PREFIX = f"{MOVE_ARTIFACTS_PREFIX}/done"

cfg = Config(
    region_name=AWS_REGION,
    max_pool_connections=(MOVE_WORKERS * COPY_PART_WORKERS) + MOVE_DELETE_WORKERS + 16,
    retries={
        # Move удаляет огромное число мелких объектов под одним префиксом, и S3
        # отвечает 503 SlowDown при достижении лимита запросов на префикс. Режим
        # "adaptive" добавляет клиентский ограничитель темпа с откатом и плавным
        # разгоном (в отличие от "standard"); вместе с увеличенным числом попыток
        # это поглощает SlowDown вместо падения манифеста.
        "mode": "adaptive",
        "total_max_attempts": S3_MAX_ATTEMPTS,
    },
    s3={
        "us_east_1_regional_endpoint": "regional",
        "addressing_style": "virtual",
    },
)
s3 = boto3.client("s3", config=cfg)
copy_transfer_config = TransferConfig(
    multipart_threshold=COPY_MULTIPART_THRESHOLD_MB * 1024 * 1024,
    multipart_chunksize=COPY_MULTIPART_CHUNKSIZE_MB * 1024 * 1024,
    max_concurrency=COPY_PART_WORKERS,
    use_threads=True,
)

log_lock = threading.Lock()
counter_lock = threading.Lock()
state_locks_lock = threading.Lock()
state_locks = {}

_PUT_OBJECT_MEMBERS = s3.meta.service_model.operation_model("PutObject").input_shape.members
PUT_OBJECT_SUPPORTS_IF_NONE_MATCH = "IfNoneMatch" in _PUT_OBJECT_MEMBERS
# Атомарный compare-and-set для состояния требует обоих условных заголовков. Без
# них откатываемся на best-effort read-then-write (под защитой in-process блокировки);
# boto3 >= 1.42 поддерживает оба.
STATE_CAS_ENABLED = PUT_OBJECT_SUPPORTS_IF_NONE_MATCH and ("IfMatch" in _PUT_OBJECT_MEMBERS)
STATE_CAS_MAX_ATTEMPTS = max(1, env_int("STATE_CAS_MAX_ATTEMPTS"))

counters = {
    "manifests_total": 0,
    "processed": 0,
    "skipped": 0,
    "reconciled": 0,
    "failed": 0,
    "groups_failed": 0,
    "copied_files": 0,
    "deleted_files": 0,
    "purged_files": 0,
    "quarantined_files": 0,
    "quarantine_failures": 0,
}

WORKDIR = None
MOVE_LOCK_ACQUIRED = False
STOP_REQUESTED = False
STOP_REASON = ""


def log(level, message):
    now = datetime.now()
    ts = now.strftime("%Y-%m-%d %H:%M:%S") + f".{now.microsecond // 1000:03d}"
    stream = sys.stderr if level == "ERROR" else sys.stdout
    with log_lock:
        print(f"[{ts}] [{level}] {message}", file=stream, flush=True)


def inc_counter(name, value=1):
    with counter_lock:
        counters[name] += value


def set_counter(name, value):
    with counter_lock:
        counters[name] = value


def counter_snapshot():
    with counter_lock:
        return dict(counters)


def send_metric(name, value):
    # name — суффикс метрики без префикса; полное имя = <METRICS_PREFIX>_<name>,
    # чтобы префикс имён метрик был единым настраиваемым параметром пайплайна.
    metric = f"{METRICS_PREFIX}_{name}"
    body = (
        f"# TYPE {metric} gauge\n"
        f"# HELP {metric} {metric}\n"
        f"{metric} {value}\n"
    ).encode("utf-8")
    request = urllib.request.Request(METRICS_URL, data=body, method="POST")

    try:
        with urllib.request.urlopen(request, timeout=2):
            return
    except Exception as exc:
        log("WARN", f"Failed to send metric {metric}: {exc}")


def compute_move_stop_ts():
    now = datetime.now(timezone.utc)
    stop_hour = WINDOW_STOP_HOUR_UTC
    stop_date = now.date() if now.hour < stop_hour else (now + timedelta(days=1)).date()

    if stop_hour >= 24:
        stop_date += timedelta(days=stop_hour // 24)
        stop_hour = stop_hour % 24

    stop_dt = datetime.combine(stop_date, dt_time(hour=stop_hour), tzinfo=timezone.utc)
    return int(stop_dt.timestamp())


MOVE_STOP_TS = compute_move_stop_ts()
SOFT_STOP_TS = MOVE_STOP_TS - SOFT_STOP_GRACE_SECONDS


def request_stop(reason):
    global STOP_REQUESTED, STOP_REASON
    if not STOP_REQUESTED:
        STOP_REQUESTED = True
        STOP_REASON = reason
        log("WARN" if reason.startswith("TERM") or reason.startswith("INT") else "INFO", reason)


def signal_handler(signum, _frame):
    name = signal.Signals(signum).name
    request_stop(f"{name} received, graceful stop requested")
    if WORKDIR:
        try:
            open(os.path.join(WORKDIR, ".stop"), "a", encoding="utf-8").close()
        except OSError:
            pass


signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)


def should_soft_stop():
    if STOP_REQUESTED:
        return True
    if WORKDIR and os.path.exists(os.path.join(WORKDIR, ".stop")):
        request_stop("Stop marker found, graceful stop requested")
        return True
    if int(time.time()) >= SOFT_STOP_TS:
        request_stop("Soft stop: move window end is near.")
        return True
    return False


class LockPutError(Exception):
    pass


class ManifestTransientError(Exception):
    """Транзиентный сбой обработки манифеста, пригодный для повторной попытки."""
    pass


def put_lock_object_if_absent(body):
    if PUT_OBJECT_SUPPORTS_IF_NONE_MATCH:
        try:
            s3.put_object(
                Bucket=ARTIFACTS_BUCKET,
                Key=MOVE_LOCK_KEY,
                Body=body,
                IfNoneMatch="*",
            )
            return
        except ClientError as exc:
            raise LockPutError(format_client_error(exc)) from exc

    if not WORKDIR:
        raise LockPutError("WORKDIR is not initialized")

    lock_file = os.path.join(WORKDIR, "move-lock.json")
    with open(lock_file, "wb") as file_obj:
        file_obj.write(body)

    try:
        proc = subprocess.run(
            [
                "aws",
                "s3api",
                "put-object",
                "--bucket",
                ARTIFACTS_BUCKET,
                "--key",
                MOVE_LOCK_KEY,
                "--body",
                lock_file,
                "--if-none-match",
                "*",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise LockPutError("Required command not found: aws") from exc

    if proc.returncode != 0:
        error = " ".join((proc.stderr or "").split()) or f"aws exited with {proc.returncode}"
        raise LockPutError(error)


def object_exists(bucket, key):
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError:
        return False


def head_size(bucket, key):
    try:
        response = s3.head_object(Bucket=bucket, Key=key)
        return int(response.get("ContentLength", 0))
    except ClientError:
        return None


def get_json_object(bucket, key):
    response = s3.get_object(Bucket=bucket, Key=key)
    body = response["Body"].read()
    return json.loads(body.decode("utf-8"))


def encode_jq_json(value):
    return (json.dumps(value, indent=2) + "\n").encode("utf-8")


def put_json_object(bucket, key, value, *, pretty=True):
    if pretty:
        body = encode_jq_json(value)
    else:
        body = (json.dumps(value, separators=(",", ":")) + "\n").encode("utf-8")
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType="application/json",
    )


def delete_object_quiet(bucket, key):
    try:
        s3.delete_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        log("WARN", f"Failed to delete s3://{bucket}/{key}: {format_client_error(exc)}")


def _delete_one_chunk(bucket, chunk):
    s3.delete_objects(
        Bucket=bucket,
        Delete={
            "Objects": [{"Key": key} for key in chunk],
            "Quiet": True,
        },
    )
    inc_counter("deleted_files", len(chunk))


def delete_keys_exact(bucket, keys):
    if not keys:
        return

    chunks = list(chunked(keys, 1000))

    # Один батч или параллелизм выключен: простой последовательный путь.
    if MOVE_DELETE_WORKERS <= 1 or len(chunks) <= 1:
        for chunk in chunks:
            _delete_one_chunk(bucket, chunk)
        return

    # Разносим батчи DeleteObjects (по 1000 ключей) по ограниченному пулу. Все ключи
    # одного манифеста лежат под одним префиксом группы, поэтому параллелизм подводит
    # этот раздел к потолку S3 ~3500 req/s; MOVE_DELETE_WORKERS его ограничивает, а
    # adaptive-ретраи откатываются на 503 SlowDown. Ошибки пробрасываются (после
    # завершения текущих батчей), чтобы манифест явно падал.
    with ThreadPoolExecutor(max_workers=MOVE_DELETE_WORKERS) as executor:
        futures = [executor.submit(_delete_one_chunk, bucket, chunk) for chunk in chunks]
        first_error = None
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:  # noqa: BLE001 - surfaced below
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error


def quarantine_key_for(proxy_name, group_name, src_key):
    return f"{QUARANTINE_PREFIX}/{proxy_name}/{group_name}/{basename_s3(src_key)}"


def quarantine_bad_keys(proxy_name, group_name, bad_keys):
    """Скопировать каждый битый/нечитаемый исходный объект в карантинный префикс.

    Возвращает множество ключей, которые НЕ удалось поместить в карантин. Битый
    файл никогда не должен останавливать пайплайн (только логи/метрики/алерты), но
    и терять улики нельзя: при неудаче копирования вызывающий код оставляет оригинал
    на месте (исключает из удаления), а сбой логируется, считается и алертится.
    Успешно карантинированные ключи удаляются вместе с остальным батчем.

    Для repacked-манифестов битый target_key перезатирается компактным артефактом,
    поэтому это должно выполняться до copy-back, чтобы захватить оригинал.
    """
    failed = set()
    for src_key in bad_keys:
        dst_key = quarantine_key_for(proxy_name, group_name, src_key)
        try:
            s3.copy(
                {"Bucket": SRC_BUCKET, "Key": src_key},
                QUARANTINE_BUCKET,
                dst_key,
                ExtraArgs={"MetadataDirective": "COPY"},
                Config=copy_transfer_config,
            )
            inc_counter("quarantined_files")
            log(
                "WARN",
                f"Quarantined bad source file: s3://{SRC_BUCKET}/{src_key} -> "
                f"s3://{QUARANTINE_BUCKET}/{dst_key}",
            )
        except Exception as exc:
            failed.add(src_key)
            inc_counter("quarantine_failures")
            log(
                "ERROR",
                f"Failed to quarantine bad source file s3://{SRC_BUCKET}/{src_key} -> "
                f"s3://{QUARANTINE_BUCKET}/{dst_key}: {exc}; keeping original in place",
            )
    return failed


def state_lock(proxy_name, group_name):
    key = f"{proxy_name}\0{group_name}"
    with state_locks_lock:
        lock = state_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            state_locks[key] = lock
        return lock


def state_key_for_group(proxy_name, group_name):
    return f"{STATE_PREFIX}/{proxy_name}/{group_name}.json"


def read_group_state_with_etag(proxy_name, group_name):
    """Вернуть (state_dict, etag). etag равен None, если объект отсутствует.

    Существующее, но битое состояние возвращается как ({}, etag), чтобы вызывающий
    код мог сделать compare-and-set против него (и перезаписать) без гонки.
    """
    state_key = state_key_for_group(proxy_name, group_name)
    try:
        response = s3.get_object(Bucket=ARTIFACTS_BUCKET, Key=state_key)
    except ClientError:
        return {}, None

    etag = response.get("ETag") or None
    try:
        payload = json.loads(response["Body"].read().decode("utf-8"))
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return payload, etag


def read_group_state(proxy_name, group_name):
    state, _etag = read_group_state_with_etag(proxy_name, group_name)
    return state


def put_state_conditional(state_key, value, etag):
    body = encode_jq_json(value)
    kwargs = {
        "Bucket": ARTIFACTS_BUCKET,
        "Key": state_key,
        "Body": body,
        "ContentType": "application/json",
    }
    if etag:
        kwargs["IfMatch"] = etag
    else:
        kwargs["IfNoneMatch"] = "*"
    s3.put_object(**kwargs)


def state_confirms_manifest_moved(state, manifest_key, last_src_key, target_key, compacted_size):
    current_next = str(state.get("next_start_after") or "")
    if not current_next or not last_src_key or current_next < last_src_key:
        return False

    if str(state.get("last_move_manifest_key") or "") == manifest_key:
        return True

    carry_key = str(state.get("carry_key") or "")
    try:
        carry_size = int(state.get("carry_size") or 0)
    except (TypeError, ValueError):
        carry_size = 0
    raw_carry_open = state.get("carry_open", False)
    carry_open = raw_carry_open is True or str(raw_carry_open).lower() == "true"

    return carry_open and carry_key == target_key and carry_size == compacted_size


def state_was_moved_past_manifest(state, last_src_key):
    current_next = str(state.get("next_start_after") or "")
    return (
        str(state.get("updated_by") or "") == "move"
        and current_next
        and last_src_key
        and current_next > last_src_key
    )


def write_state_if_newer(proxy_name, group_name, next_start_after, carry_key, carry_size, manifest_key="", carry_open=True):
    state_key = state_key_for_group(proxy_name, group_name)
    with state_lock(proxy_name, group_name):
        # In-process блокировка сериализует писателей внутри воркера; условный
        # PutObject (IfMatch/IfNoneMatch по прочитанному etag) закрывает зазор между
        # воркерами, когда lease увели как протухший и два воркера трогают одну группу.
        # При конфликте CAS перечитываем и решаем заново, чтобы защита от отката курсора
        # держалась против того, что записал другой писатель.
        for attempt in range(1, STATE_CAS_MAX_ATTEMPTS + 1):
            current_state, etag = read_group_state_with_etag(proxy_name, group_name)
            current_next = str(current_state.get("next_start_after") or "")

            if current_next and next_start_after and current_next > next_start_after:
                log(
                    "INFO",
                    "Skip state rewind for "
                    f"{proxy_name}/{group_name}: current_next={current_next} "
                    f"candidate_next={next_start_after}",
                )
                return

            next_state = {
                "proxy": proxy_name,
                "group": group_name,
                "next_start_after": next_start_after,
                "carry_key": carry_key,
                "carry_size": int(carry_size or 0),
                "carry_open": bool(carry_open),
                "updated_by": "move",
                "updated_at": utc_iso(),
            }
            if manifest_key:
                next_state["last_move_manifest_key"] = manifest_key

            if not STATE_CAS_ENABLED:
                put_json_object(ARTIFACTS_BUCKET, state_key, next_state)
                return

            try:
                put_state_conditional(state_key, next_state, etag)
                return
            except ClientError as exc:
                if is_precondition_failed(exc) and attempt < STATE_CAS_MAX_ATTEMPTS:
                    log(
                        "INFO",
                        f"State CAS conflict for {proxy_name}/{group_name}; "
                        f"re-reading and retrying (attempt {attempt}/{STATE_CAS_MAX_ATTEMPTS})",
                    )
                    continue
                raise


def acquire_lock():
    global MOVE_LOCK_ACQUIRED

    lock_body = {
        "run_date": RUN_DATE,
        "run_ts": RUN_TS,
        "started_at": utc_iso(),
    }
    lock_bytes = (json.dumps(lock_body, separators=(",", ":")) + "\n").encode("utf-8")

    try:
        put_lock_object_if_absent(lock_bytes)
        MOVE_LOCK_ACQUIRED = True
        return
    except LockPutError as exc:
        put_error = str(exc)

    existing = {}
    try:
        existing = get_json_object(ARTIFACTS_BUCKET, MOVE_LOCK_KEY)
    except Exception:
        log("ERROR", f"Failed to acquire move lock: s3://{ARTIFACTS_BUCKET}/{MOVE_LOCK_KEY} aws_error={put_error}")
        raise SystemExit(1)

    existing_started_at = str(existing.get("started_at") or "")
    if existing_started_at:
        try:
            started_dt = datetime.fromisoformat(existing_started_at.replace("Z", "+00:00"))
            if started_dt.tzinfo is None:
                started_dt = started_dt.replace(tzinfo=timezone.utc)
            age_seconds = int(datetime.now(timezone.utc).timestamp() - started_dt.timestamp())
        except ValueError:
            age_seconds = -1

        if LOCK_STALE_AFTER_SECONDS > 0 and age_seconds >= LOCK_STALE_AFTER_SECONDS:
            log(
                "WARN",
                "Existing move lock is stale, removing and retrying: "
                f"s3://{ARTIFACTS_BUCKET}/{MOVE_LOCK_KEY} "
                f"age_seconds={age_seconds} threshold_seconds={LOCK_STALE_AFTER_SECONDS}",
            )
            delete_object_quiet(ARTIFACTS_BUCKET, MOVE_LOCK_KEY)

            try:
                put_lock_object_if_absent(lock_bytes)
                MOVE_LOCK_ACQUIRED = True
                return
            except LockPutError as exc:
                put_error = str(exc)

    log(
        "ERROR",
        "Move lock already exists: "
        f"s3://{ARTIFACTS_BUCKET}/{MOVE_LOCK_KEY} "
        f"run_date={existing.get('run_date') or '<unknown>'} "
        f"run_ts={existing.get('run_ts') or '<unknown>'} "
        f"started_at={existing.get('started_at') or '<unknown>'} "
        f"aws_error={put_error or '<none>'}",
    )
    raise SystemExit(1)


def cleanup_lock():
    if MOVE_LOCK_ACQUIRED and MOVE_LOCK_KEY:
        delete_object_quiet(ARTIFACTS_BUCKET, MOVE_LOCK_KEY)


def record_move_run_stat(snapshot, exit_code):
    """Сохранить статистику разгребания этого pod'а для адаптивного бюджета repack.

    repack сводит записи всех pod'ов последнего прогона move, чтобы рассчитать
    следующий бюджет продукции (common.next_repack_budget). Best-effort: этот учёт
    никогда не должен валить прогон.
    """
    try:
        drained = int(snapshot.get("deleted_files", 0)) + int(snapshot.get("purged_files", 0))
        drain_seconds = max(0, int(time.time()) - RUN_START_TS)
        window_seconds = max(0, MOVE_STOP_TS - RUN_START_TS)
        window_capped = bool(STOP_REQUESTED and "window" in (STOP_REASON or "").lower())
        try:
            pending_at_end = len(list(list_manifest_keys()))
        except Exception:
            pending_at_end = -1
        idx = WORKER_INDEX if WORKER_INDEX != "" else "0"
        key = f"{MOVE_STATS_PREFIX}/{SHARD_RUN_ID}/{idx}.json"
        put_json_object(
            ARTIFACTS_BUCKET,
            key,
            {
                "run_id": SHARD_RUN_ID,
                "run_ts": RUN_TS,
                "worker_index": idx,
                "worker_id": WORKER_ID,
                "files_drained": drained,
                "copied_files": int(snapshot.get("copied_files", 0)),
                "drain_seconds": drain_seconds,
                "window_seconds": window_seconds,
                "window_capped": window_capped,
                "pending_at_end": pending_at_end,
                "exit_code": exit_code,
                "ended_at": utc_iso(),
            },
        )
        # Указатель на последний прогон move, чтобы repack читал статистику только
        # этого прогона (все pod'ы пишут один run_id; последний побеждает, значение то же).
        put_json_object(
            ARTIFACTS_BUCKET,
            MOVE_LAST_RUN_KEY,
            {"run_id": SHARD_RUN_ID, "run_ts": RUN_TS, "ended_at": utc_iso()},
        )
        log(
            "INFO",
            f"Recorded move run stat: files_drained={drained} drain_s={drain_seconds} "
            f"window_s={window_seconds} window_capped={window_capped} pending_at_end={pending_at_end}",
        )
    except Exception as exc:
        log("WARN", f"Failed to record move run stat: {exc}")


def on_exit(exit_code):
    failure_value = 0 if exit_code == 0 else 1

    if exit_code == 0:
        log("INFO", "Script completed successfully.")
    else:
        log("ERROR", f"Script failed. Exit code={exit_code}.")

    snapshot = counter_snapshot()
    send_metric("move_failure", failure_value)
    send_metric("move_manifests_total", snapshot["manifests_total"])
    send_metric("move_manifests_processed", snapshot["processed"])
    send_metric("move_manifests_reconciled_total", snapshot["reconciled"])
    send_metric("move_files_copied_total", snapshot["copied_files"])
    send_metric("move_files_deleted_total", snapshot["deleted_files"])
    send_metric("move_files_purged_total", snapshot["purged_files"])
    send_metric("move_files_quarantined_total", snapshot["quarantined_files"])
    send_metric("move_quarantine_failures_total", snapshot["quarantine_failures"])
    # Группы, изолированные за прогон (сбой манифеста после ретраев / перехват лизы /
    # крах). В здоровом прогоне = 0; >0 — сигнал, что какая-то группа застряла и её
    # надо разобрать вручную, пока бэклог не накопился. На это вешается алерт.
    send_metric("move_groups_failed_total", snapshot["groups_failed"])

    if exit_code == 0:
        send_metric("move_last_success_timestamp_seconds", int(time.time()))

    record_move_run_stat(snapshot, exit_code)

    cleanup_lock()

    if WORKDIR and os.path.isdir(WORKDIR):
        shutil.rmtree(WORKDIR, ignore_errors=True)


def list_manifest_keys(prefix=None):
    keys = []
    paginator = s3.get_paginator("list_objects_v2")
    list_prefix = prefix if prefix is not None else f"{MANIFESTS_PREFIX}/"

    for page in paginator.paginate(
        Bucket=ARTIFACTS_BUCKET,
        Prefix=list_prefix,
        PaginationConfig={"PageSize": 1000},
    ):
        for obj in page.get("Contents", []):
            key = obj.get("Key", "")
            if key.endswith(".json"):
                keys.append(key)

    keys.sort()
    return keys


def done_key_for_manifest(manifest_key):
    manifest_rel = manifest_key
    prefix = f"{MANIFESTS_PREFIX}/"
    if manifest_rel.startswith(prefix):
        manifest_rel = manifest_rel[len(prefix):]

    if manifest_rel.endswith(".json"):
        manifest_rel = manifest_rel[:-5]

    return f"{DONE_PREFIX}/{manifest_rel}.done.json"


def fail_manifest(manifest_key, message):
    log("ERROR", f"{message}: {manifest_key}")
    inc_counter("failed")
    return False


def put_done_marker(
    done_key,
    manifest_key,
    compacted_key,
    target_key,
    first_src_key,
    last_src_key,
    *,
    result,
    reason="",
):
    done_marker = {
        "run_id": SHARD_RUN_ID,
        "worker_id": WORKER_ID,
        "run_date": RUN_DATE,
        "run_ts": RUN_TS,
        "manifest_key": manifest_key,
        "compacted_key": compacted_key,
        "target_key": target_key,
        "first_src_key": first_src_key,
        "last_src_key": last_src_key,
        "status": "done",
        "result": result,
        "done_at": utc_iso(),
    }
    if reason:
        done_marker["reason"] = reason

    put_json_object(ARTIFACTS_BUCKET, done_key, done_marker)


def cleanup_manifest_artifacts(manifest_key, compacted_key):
    if compacted_key:
        delete_object_quiet(DST_BUCKET, compacted_key)
    delete_object_quiet(ARTIFACTS_BUCKET, manifest_key)


def cleanup_done_manifest(manifest_key, done_key):
    compacted_key = ""

    try:
        manifest = get_json_object(ARTIFACTS_BUCKET, manifest_key)
        if isinstance(manifest, dict):
            compacted_key = str(manifest.get("compacted_key") or "")
    except Exception as exc:
        log("WARN", f"Failed to read completed manifest before cleanup: {manifest_key}: {exc}")

    if not compacted_key:
        try:
            done_marker = get_json_object(ARTIFACTS_BUCKET, done_key)
            if isinstance(done_marker, dict):
                compacted_key = str(done_marker.get("compacted_key") or "")
        except Exception as exc:
            log("WARN", f"Failed to read done marker before cleanup: {done_key}: {exc}")

    cleanup_manifest_artifacts(manifest_key, compacted_key)


def process_purge_manifest(
    manifest_key,
    done_key,
    proxy_name,
    group_name,
    src_keys,
    bad_src_keys,
    first_src_key,
    last_src_key,
):
    if not proxy_name or not group_name or not last_src_key or not src_keys:
        return fail_manifest(manifest_key, "Purge manifest is missing required fields")

    with state_lock(proxy_name, group_name):
        current_state = read_group_state(proxy_name, group_name)
        current_next = str(current_state.get("next_start_after") or "")

        # Считаем no-op, только если именно этот purge-манифест уже применён (сбой
        # после записи состояния, но до done-маркера). При любом другом положении
        # курсора всё равно удаляем: операция идемпотентна, а удалять эти битые ключи
        # всегда безопасно, поэтому мы не оставляем их «висящими».
        already_applied = (
            str(current_state.get("updated_by") or "") == "move"
            and current_next
            and current_next >= last_src_key
            and str(current_state.get("last_move_manifest_key") or "") == manifest_key
        )
        if already_applied:
            log(
                "INFO",
                "Skip purge manifest already applied by move: "
                f"manifest={manifest_key} current_next={current_next} last_src_key={last_src_key}",
            )
            put_done_marker(
                done_key,
                manifest_key,
                "",
                "",
                first_src_key,
                last_src_key,
                result="stale",
                reason="already-applied",
            )
            cleanup_manifest_artifacts(manifest_key, "")
            inc_counter("skipped")
            return True

        quarantine_failed = set()
        if bad_src_keys:
            quarantine_failed = quarantine_bad_keys(proxy_name, group_name, bad_src_keys)

        purge_keys = [key for key in src_keys if key not in quarantine_failed]

        log(
            "WARN",
            f"Purging {len(purge_keys)} bad/unusable source keys from s3://{SRC_BUCKET}/ "
            f"for {proxy_name}/{group_name} (first={first_src_key} last={last_src_key})",
        )
        delete_keys_exact(SRC_BUCKET, purge_keys)
        inc_counter("purged_files", len(purge_keys))

        write_state_if_newer(
            proxy_name,
            group_name,
            last_src_key,
            "",
            0,
            manifest_key=manifest_key,
            carry_open=False,
        )

        put_done_marker(
            done_key,
            manifest_key,
            "",
            "",
            first_src_key,
            last_src_key,
            result="purged",
        )

        cleanup_manifest_artifacts(manifest_key, "")

    inc_counter("processed")
    return True


def process_manifest(manifest_key):
    try:
        done_key = done_key_for_manifest(manifest_key)

        if object_exists(ARTIFACTS_BUCKET, done_key):
            inc_counter("skipped")
            cleanup_done_manifest(manifest_key, done_key)
            return True

        log("INFO", f"Processing manifest: s3://{ARTIFACTS_BUCKET}/{manifest_key}")
        manifest = get_json_object(ARTIFACTS_BUCKET, manifest_key)

        status = str(manifest.get("status") or "")
        if status not in ("repacked", "purge"):
            inc_counter("skipped")
            return True

        src_keys = manifest.get("src_keys") or []
        if not isinstance(src_keys, list):
            src_keys = []
        src_keys = [str(key) for key in src_keys if key]

        bad_src_keys = manifest.get("bad_src_keys") or []
        if isinstance(bad_src_keys, list):
            bad_src_keys = [str(key) for key in bad_src_keys if key]
        else:
            bad_src_keys = []

        compacted_key = str(manifest.get("compacted_key") or "")
        target_key = str(manifest.get("target_key") or (src_keys[0] if src_keys else ""))
        proxy_name = str(manifest.get("proxy") or "")
        group_name = str(manifest.get("group") or "")
        first_src_key = str(manifest.get("first_src_key") or (src_keys[0] if src_keys else ""))
        last_src_key = str(manifest.get("last_src_key") or (src_keys[-1] if src_keys else ""))

        if status == "purge":
            return process_purge_manifest(
                manifest_key,
                done_key,
                proxy_name,
                group_name,
                src_keys,
                bad_src_keys,
                first_src_key,
                last_src_key,
            )

        try:
            compacted_size = int(manifest.get("compacted_size") or 0)
        except (TypeError, ValueError):
            return fail_manifest(manifest_key, "Manifest has invalid compacted_size")

        if not compacted_key or not target_key or not proxy_name or not group_name or not last_src_key:
            return fail_manifest(manifest_key, "Manifest is missing required fields")

        with state_lock(proxy_name, group_name):
            current_state = read_group_state(proxy_name, group_name)
            current_next = str(current_state.get("next_start_after") or "")
            if state_confirms_manifest_moved(
                current_state,
                manifest_key,
                last_src_key,
                target_key,
                compacted_size,
            ):
                log(
                    "INFO",
                    "Skip stale manifest because move state is already advanced: "
                    f"manifest={manifest_key} current_next={current_next} "
                    f"last_src_key={last_src_key}",
                )
                put_done_marker(
                    done_key,
                    manifest_key,
                    compacted_key,
                    target_key,
                    first_src_key,
                    last_src_key,
                    result="stale",
                    reason="state-already-advanced",
                )
                cleanup_manifest_artifacts(manifest_key, compacted_key)
                inc_counter("skipped")
                return True
            if state_was_moved_past_manifest(current_state, last_src_key):
                # Предыдущий прогон move уже переместил этот батч (updated_by=move,
                # курсор группы стоит ЗА last_src_key манифеста), но упал до записи
                # done-маркера и удаления манифеста — поэтому он висит и блокирует
                # repack через interlock. Слепо перекопировать НЕЛЬЗЯ (в target могли
                # лечь новые данные), но если размер target байт-в-байт совпадает с
                # compacted_size, copy-back этого манифеста точно состоялся (target_key
                # уникален на батч, совпадение ~300 МБ не случайно). При совпадении —
                # авто-восстановление: ретайрим манифест. Иначе явно падаем для разбора.
                if not AUTO_RECONCILE_STALE:
                    return fail_manifest(
                        manifest_key,
                        "Move state is already past this manifest; refusing to overwrite target",
                    )

                reconcile_target_size = head_size(SRC_BUCKET, target_key)
                if reconcile_target_size is not None and reconcile_target_size == compacted_size:
                    log(
                        "WARN",
                        "Auto-reconciling stale manifest: move state was advanced "
                        "past it by a prior (crashed) move run and the target size "
                        "matches compacted_size, so the copy-back provably completed: "
                        f"manifest={manifest_key} current_next={current_next} "
                        f"last_src_key={last_src_key} target={target_key} "
                        f"compacted_size={compacted_size} "
                        f"updated_by={current_state.get('updated_by') or '<unknown>'}",
                    )
                    # Copy-back доказанно состоялся, но прошлый прогон мог упасть ДО
                    # удаления слитых источников (удаление идёт после копии). Эти
                    # источники полностью представлены в компактном zip'е на target_key,
                    # так что оставить их — значит навсегда «потерять» их за продвинутым
                    # курсором (дублирование). Повторяем удаление идемпотентно; битые
                    # ключи остаются на месте, чтобы не потерять улики.
                    bad_src_key_set = set(bad_src_keys)
                    reconcile_delete_keys = [
                        key for key in src_keys
                        if key and key != target_key and key not in bad_src_key_set
                    ]
                    if reconcile_delete_keys:
                        log(
                            "INFO",
                            f"Reconcile: ensuring {len(reconcile_delete_keys)} merged "
                            f"source keys are deleted from s3://{SRC_BUCKET}/ (idempotent)",
                        )
                        delete_keys_exact(SRC_BUCKET, reconcile_delete_keys)
                    put_done_marker(
                        done_key,
                        manifest_key,
                        compacted_key,
                        target_key,
                        first_src_key,
                        last_src_key,
                        result="stale",
                        reason="auto-reconciled-state-advanced",
                    )
                    cleanup_manifest_artifacts(manifest_key, compacted_key)
                    inc_counter("reconciled")
                    inc_counter("skipped")
                    return True

                return fail_manifest(
                    manifest_key,
                    "Move state is already past this manifest and target size "
                    f"({reconcile_target_size}) does not match compacted_size "
                    f"({compacted_size}); refusing to overwrite target -- "
                    "manual investigation required",
                )
            if current_next and current_next >= last_src_key:
                log(
                    "WARN",
                    "State is already advanced, but it does not prove this manifest "
                    "was moved; processing manifest to preserve source data: "
                    f"manifest={manifest_key} current_next={current_next} "
                    f"last_src_key={last_src_key} "
                    f"updated_by={current_state.get('updated_by') or '<unknown>'}",
                )

            compacted_size_actual = head_size(DST_BUCKET, compacted_key)
            if compacted_size_actual is None or compacted_size_actual <= 0:
                return fail_manifest(
                    manifest_key,
                    f"Compacted object is missing or invalid: s3://{DST_BUCKET}/{compacted_key}",
                )
            # Сверяем фактический размер компактного объекта с заявленным в манифесте
            # ДО разрушающей copy на target_key (target_key — обычно один из оригиналов).
            # При расхождении (битая запись артефакта / неконсистентный манифест) verify
            # после копии всё равно упал бы, но target уже был бы перезатёрт. Падаем
            # здесь — оригиналы остаются нетронутыми, группа изолируется чисто.
            if compacted_size_actual != compacted_size:
                return fail_manifest(
                    manifest_key,
                    f"Compacted object size mismatch before copy: "
                    f"actual={compacted_size_actual} manifest={compacted_size} "
                    f"s3://{DST_BUCKET}/{compacted_key}",
                )

            # Карантиним битые исходные файлы до copy-back: target_key может быть одним
            # из них и сейчас будет перезатёрт компактным артефактом, поэтому оригинал
            # надо захватить первым. Сбой копирования не блокирует манифест — такой ключ
            # просто остаётся на месте (ниже).
            quarantine_failed = set()
            if bad_src_keys:
                log(
                    "WARN",
                    f"Manifest {manifest_key} flagged {len(bad_src_keys)} bad source files "
                    f"removed during compaction: {bad_src_keys}",
                )
                quarantine_failed = quarantine_bad_keys(proxy_name, group_name, bad_src_keys)

            if target_key in quarantine_failed:
                # Батч всегда пишет компактный артефакт в target_key, поэтому нельзя
                # одновременно сохранить битый оригинал target и не блокировать группу.
                # Карантин для него уже не удался — оригинал теряется при copy-back;
                # громко логируем, но продолжаем: битый файл не должен останавливать пайплайн.
                log(
                    "ERROR",
                    f"Bad target file could not be quarantined and is overwritten by the "
                    f"compacted artifact: s3://{SRC_BUCKET}/{target_key}",
                )

            log("INFO", f"Copy back: s3://{DST_BUCKET}/{compacted_key} -> s3://{SRC_BUCKET}/{target_key}")
            s3.copy(
                {
                    "Bucket": DST_BUCKET,
                    "Key": compacted_key,
                },
                SRC_BUCKET,
                target_key,
                ExtraArgs={"MetadataDirective": "COPY"},
                Config=copy_transfer_config,
            )
            inc_counter("copied_files")

            target_size = head_size(SRC_BUCKET, target_key)
            if target_size is None or target_size != compacted_size:
                return fail_manifest(
                    manifest_key,
                    f"Verification failed after copy for {target_key}: "
                    f"actual_size={target_size} expected_size={compacted_size}",
                )

            # Ключи, которые не удалось карантинировать, остаются на месте, чтобы сбой
            # копирования не терял улики; остальные удаляются вместе с батчем (учитываются
            # как deleted_files, не purged_files).
            delete_keys = [
                key for key in src_keys
                if key and key != target_key and key not in quarantine_failed
            ]
            if delete_keys:
                log("INFO", f"Deleting {len(delete_keys)} merged source keys from s3://{SRC_BUCKET}/")
                delete_keys_exact(SRC_BUCKET, delete_keys)

            write_state_if_newer(
                proxy_name,
                group_name,
                last_src_key,
                target_key,
                compacted_size,
                manifest_key=manifest_key,
            )

            put_done_marker(
                done_key,
                manifest_key,
                compacted_key,
                target_key,
                first_src_key,
                last_src_key,
                result="processed",
            )

            cleanup_manifest_artifacts(manifest_key, compacted_key)

        inc_counter("processed")
        return True
    except Exception as exc:
        # Любое необработанное исключение здесь — это уже после ретраев botocore
        # (сеть/таймаут/устойчивый S3 SlowDown) или ошибка чтения манифеста. Считаем
        # его транзиентным и пробрасываем наверх для повторной попытки: оригиналы целы
        # (repack их уже прочёл), повтор идемпотентен. Детерминированные сбои сюда не
        # попадают — они идут через fail_manifest и возвращают False без исключения.
        if TRACEBACK_ON_ERROR:
            for line in traceback.format_exc().rstrip().splitlines():
                log("ERROR", line)
        raise ManifestTransientError(str(exc)) from exc


def _sleep_interruptible(seconds):
    """Поспать до `seconds`, прервавшись раньше при soft-stop. True, если прервано."""
    deadline = time.time() + max(0, seconds)
    while time.time() < deadline:
        if should_soft_stop():
            return True
        time.sleep(min(1.0, max(0.0, deadline - time.time())))
    return False


def process_manifest_with_retry(manifest_key):
    """Обработать манифест с ретраем транзиентных ошибок.

    Возвращает True (успех/пропуск) либо False, если манифест не дался —
    детерминированно (через fail_manifest) или после исчерпания попыток. False
    не теряет данные: манифест остаётся на месте и будет повторён следующим
    прогоном move; вызывающий код изолирует группу, а не валит весь под.
    """
    for attempt in range(1, MANIFEST_MAX_ATTEMPTS + 1):
        try:
            return process_manifest(manifest_key)
        except ManifestTransientError as exc:
            if attempt < MANIFEST_MAX_ATTEMPTS and not should_soft_stop():
                delay = MANIFEST_RETRY_BASE_SECONDS * attempt
                log(
                    "WARN",
                    f"Manifest hit a transient error (attempt {attempt}/{MANIFEST_MAX_ATTEMPTS}), "
                    f"retrying in {delay}s: {manifest_key}: {exc}",
                )
                if _sleep_interruptible(delay):
                    log("WARN", f"Soft stop during manifest retry; leaving it pending: {manifest_key}")
                    inc_counter("failed")
                    return False
                continue
            log("ERROR", f"Manifest failed after {attempt} attempt(s): {manifest_key}: {exc}")
            inc_counter("failed")
            return False
    return False


def progress_reporter(total, done_event):
    """Периодически логировать прогресс, пока не выставлен done_event.

    total может быть None (шардированный режим), где множество манифестов растёт по
    мере появления новых от repack; тогда знаменателем служит текущий счётчик
    manifests_total, а процент/ETA — приблизительные.
    """
    start_ts = time.time()
    while not done_event.wait(REPORT_EVERY):
        snapshot = counter_snapshot()
        live_total = total if total is not None else snapshot["manifests_total"]
        done = snapshot["processed"] + snapshot["skipped"] + snapshot["failed"]
        elapsed = time.time() - start_ts
        rate = done / elapsed if elapsed > 0 else 0
        pct = (done * 100 / live_total) if live_total else 0
        eta = (live_total - done) / rate if rate > 0 and live_total else 0
        log(
            "INFO",
            "Progress: "
            f"done={done}/{live_total} {pct:.2f}% "
            f"processed={snapshot['processed']} skipped={snapshot['skipped']} failed={snapshot['failed']} "
            f"copied={snapshot['copied_files']} deleted={snapshot['deleted_files']} "
            f"rate={rate:.1f} manifests/s elapsed={hms(elapsed)} eta={hms(eta)}",
        )


def process_manifests(manifest_keys):
    max_pending = max(MOVE_WORKERS, MOVE_WORKERS * MOVE_PREFETCH)
    iterator = iter(manifest_keys)
    pending = {}
    submitted = 0

    def submit_until_full(executor):
        nonlocal submitted
        while len(pending) < max_pending:
            if should_soft_stop():
                return
            try:
                manifest_key = next(iterator)
            except StopIteration:
                return
            future = executor.submit(process_manifest, manifest_key)
            pending[future] = manifest_key
            submitted += 1

    with ThreadPoolExecutor(max_workers=MOVE_WORKERS) as executor:
        submit_until_full(executor)

        while pending:
            done, _not_done = wait(pending, timeout=1, return_when=FIRST_COMPLETED)

            if not done:
                should_soft_stop()
                continue

            for future in done:
                manifest_key = pending.pop(future)
                try:
                    future.result()
                except Exception as exc:
                    log("ERROR", f"Unexpected worker failure for {manifest_key}: {exc}")
                    if TRACEBACK_ON_ERROR:
                        for line in traceback.format_exc().rstrip().splitlines():
                            log("ERROR", line)
                    inc_counter("failed")

            if not STOP_REQUESTED:
                submit_until_full(executor)

    if STOP_REQUESTED and submitted < len(manifest_keys):
        log("INFO", f"Stopped after submitting {submitted}/{len(manifest_keys)} manifests.")


def put_run_marker(status, **extra):
    if status == "running":
        marker = {
            "run_date": RUN_DATE,
            "run_ts": RUN_TS,
            "run_id": SHARD_RUN_ID,
            "worker_id": WORKER_ID,
            "started_at": extra["started_at"],
            "status": "running",
        }
    elif status == "partial":
        marker = {
            "run_date": RUN_DATE,
            "run_ts": RUN_TS,
            "run_id": SHARD_RUN_ID,
            "worker_id": WORKER_ID,
            "status": "partial",
            "stopped_at": extra["stopped_at"],
        }
    elif "processed" in extra or "failed" in extra:
        marker = {
            "run_date": RUN_DATE,
            "run_ts": RUN_TS,
            "run_id": SHARD_RUN_ID,
            "worker_id": WORKER_ID,
            "status": status,
            "finished_at": extra["finished_at"],
            "processed": int(extra["processed"]),
            "failed": int(extra["failed"]),
        }
    else:
        marker = {
            "run_date": RUN_DATE,
            "run_ts": RUN_TS,
            "run_id": SHARD_RUN_ID,
            "worker_id": WORKER_ID,
            "status": status,
            "finished_at": extra["finished_at"],
        }

    try:
        put_json_object(ARTIFACTS_BUCKET, RUN_MARKER_KEY, marker, pretty=False)
    except Exception as exc:
        log("WARN", f"Failed to write run marker s3://{ARTIFACTS_BUCKET}/{RUN_MARKER_KEY}: {exc}")


def manifest_group_from_key(manifest_key):
    prefix = f"{MANIFESTS_PREFIX}/"
    if not manifest_key.startswith(prefix) or not manifest_key.endswith(".json"):
        return None

    rel = manifest_key[len(prefix):]
    parts = rel.split("/")
    if len(parts) < 3:
        return None

    proxy_name = parts[0]
    group_name = parts[1]
    if not proxy_name or not group_name:
        return None

    return proxy_name, group_name


def group_candidate_id(candidate):
    return f"{candidate['proxy_name']}/{candidate['group_name']}"


def ordered_group_candidates(candidates):
    return sorted(
        candidates,
        key=lambda candidate: hashlib.sha1(
            f"{WORKER_ID}\0{group_candidate_id(candidate)}".encode("utf-8")
        ).hexdigest(),
    )


def group_lease_key(scope, proxy_name, group_name):
    return f"{LEASES_PREFIX}/{scope}/{SHARD_RUN_ID}/{proxy_name}/{group_name}.json"


def group_done_key(scope, proxy_name, group_name):
    return f"{SHARD_DONE_PREFIX}/{scope}/{SHARD_RUN_ID}/{proxy_name}/{group_name}.json"


def group_lease_prefix(scope):
    return f"{LEASES_PREFIX}/{scope}/{SHARD_RUN_ID}"


def build_manifest_group_candidates():
    manifest_keys = list_manifest_keys()
    groups = {}

    for manifest_key in manifest_keys:
        parsed = manifest_group_from_key(manifest_key)
        if parsed is None:
            continue
        proxy_name, group_name = parsed
        groups[(proxy_name, group_name)] = {
            "proxy_name": proxy_name,
            "group_name": group_name,
            "manifest_prefix": f"{MANIFESTS_PREFIX}/{proxy_name}/{group_name}/",
        }

    set_counter("manifests_total", len(manifest_keys))
    return list(groups.values()), len(manifest_keys)


def mark_group_done(scope, candidate, manifest_count):
    key = group_done_key(scope, candidate["proxy_name"], candidate["group_name"])
    marker = {
        "run_id": SHARD_RUN_ID,
        "worker_id": WORKER_ID,
        "run_date": RUN_DATE,
        "run_ts": RUN_TS,
        "proxy": candidate["proxy_name"],
        "group": candidate["group_name"],
        "manifest_count": int(manifest_count),
        "status": "done",
        "done_at": utc_iso(),
    }
    put_json_object(ARTIFACTS_BUCKET, key, marker, pretty=False)


def sleep_until_next_claim():
    deadline = time.time() + CLAIM_IDLE_SLEEP_SECONDS
    while time.time() < deadline:
        if should_soft_stop():
            return
        time.sleep(min(1, max(0, deadline - time.time())))


def process_manifest_group(candidate, should_abort=None):
    manifest_keys = list_manifest_keys(candidate["manifest_prefix"])
    if not manifest_keys:
        log("INFO", f"No manifests remain for claimed group: {group_candidate_id(candidate)}")
        return True, 0

    log(
        "INFO",
        f"Processing manifest group: {group_candidate_id(candidate)} manifests={len(manifest_keys)}",
    )

    failed = False
    stopped = False
    for manifest_key in manifest_keys:
        if should_soft_stop():
            stopped = True
            break
        if should_abort is not None and should_abort():
            log("WARN", f"Lease lost; abandoning manifest group mid-flight: {group_candidate_id(candidate)}")
            stopped = True
            break
        if not process_manifest_with_retry(manifest_key):
            # Останавливаем группу на первом не давшемся манифесте: курсор группы
            # двигается строго по порядку, поэтому продолжать дальше нельзя — иначе
            # пропущенный манифест остался бы «позади» продвинувшегося курсора и
            # завис бы навсегда. Группа целиком останется в очереди до след. прогона.
            failed = True
            break

    return (not failed) and (not stopped), len(manifest_keys)


def process_sharded_manifest_groups():
    log(
        "INFO",
        "Sharded move worker started: "
        f"run_id={SHARD_RUN_ID} worker_id={WORKER_ID} lease_ttl_seconds={LEASE_TTL_SECONDS}",
    )

    lease_client = S3LeaseClient(
        client=s3,
        bucket=ARTIFACTS_BUCKET,
        owner=WORKER_ID,
        ttl_seconds=LEASE_TTL_SECONDS,
        heartbeat_seconds=LEASE_HEARTBEAT_SECONDS,
        log=log,
    )

    # Группы, не давшиеся в этом прогоне (сбой манифеста после ретраев, перехват лизы
    # или неожиданный крах). Их пропускаем до конца прогона, чтобы не крутиться на одной
    # и той же проблемной группе и дать поду разгребать остальные. Группа остаётся в
    # очереди и будет повторена следующим ночным прогоном move с чистого листа.
    failed_groups = set()

    while not should_soft_stop():
        candidates, manifest_count = build_manifest_group_candidates()
        if manifest_count == 0:
            if lease_client.has_active_leases(group_lease_prefix("move")):
                log("INFO", "No move manifests visible, but active move leases exist; waiting.")
                sleep_until_next_claim()
                continue
            log("INFO", "No pending manifests found. Nothing to do.")
            return 0
        if not candidates:
            log("ERROR", f"Found pending manifests but could not derive any proxy/group candidates: {manifest_count}")
            return 1

        log("INFO", f"Found pending manifests: {manifest_count} groups={len(candidates)}")
        claimed_in_pass = False

        for candidate in ordered_group_candidates(candidates):
            if should_soft_stop():
                break
            if group_candidate_id(candidate) in failed_groups:
                continue

            lease_key = group_lease_key("move", candidate["proxy_name"], candidate["group_name"])
            lease = lease_client.try_acquire(
                lease_key,
                {
                    "scope": "move",
                    "run_id": SHARD_RUN_ID,
                    "run_date": RUN_DATE,
                    "run_ts": RUN_TS,
                    "proxy": candidate["proxy_name"],
                    "group": candidate["group_name"],
                    "manifest_prefix": candidate["manifest_prefix"],
                },
            )
            if lease is None:
                continue

            claimed_in_pass = True
            group_id = group_candidate_id(candidate)
            lease_releasable = False
            isolate_group = False

            try:
                lease.start_heartbeat()
                ok, processed_count = process_manifest_group(candidate, should_abort=lambda: lease.lost)

                if lease.lost:
                    # Лизу перехватил другой воркер (протухла из-за медленного
                    # heartbeat'а). Группа теперь принадлежит ему — не трогаем её (ни
                    # release, ни done) и не валим под; пропускаем до конца прогона.
                    log("WARN", f"Lease lost mid-flight; leaving group to its new owner: {group_id}")
                    isolate_group = True
                elif not ok:
                    if STOP_REQUESTED:
                        log("INFO", f"Group was not marked done because move stopped before completion: {group_id}")
                        lease_releasable = True
                        break
                    # Манифест в группе не дался даже после ретраев. НЕ валим весь под
                    # (иначе backoffLimit убьёт весь флот и оставит бэклог) — изолируем
                    # группу: освобождаем лизу и берём другие. Группа останется в
                    # очереди и будет повторена следующим ночным прогоном move.
                    log("ERROR", f"Group has a manifest that failed after retries; isolating it and continuing: {group_id}")
                    inc_counter("groups_failed")
                    lease_releasable = True
                    isolate_group = True
                else:
                    mark_group_done("move", candidate, processed_count)
                    lease_releasable = True
                    log("INFO", f"Completed sharded move group: {group_id}")
            except Exception as exc:
                # Непредвиденная ошибка на уровне группы (не штатный сбой манифеста).
                # Тоже изолируем группу, а не валим под: освобождаем лизу и продолжаем.
                log("ERROR", f"Sharded move group crashed unexpectedly; isolating it and continuing: {group_id}: {exc}")
                if TRACEBACK_ON_ERROR:
                    for line in traceback.format_exc().rstrip().splitlines():
                        log("ERROR", line)
                inc_counter("groups_failed")
                lease_releasable = not lease.lost
                isolate_group = True
            finally:
                lease.stop_heartbeat()
                if lease_releasable and not lease.lost:
                    lease.release()

            if isolate_group:
                failed_groups.add(group_id)

        if STOP_REQUESTED:
            break

        if not claimed_in_pass:
            if lease_client.has_active_leases(group_lease_prefix("move")):
                log("INFO", "No free move groups right now; waiting for active leases.")
                sleep_until_next_claim()
                continue
            log("INFO", "No free or active move group leases remain.")
            return 0

    if STOP_REQUESTED:
        log("INFO", "Sharded move worker stopped gracefully.")

    return 0


def main_impl():
    global WORKDIR

    log("INFO", "Starting streaming move process...")
    log("INFO", f"RUN_DATE={RUN_DATE} RUN_TS={RUN_TS}")
    log("INFO", f"SHARDING_ENABLED={int(SHARDING_ENABLED)} SHARD_RUN_ID={SHARD_RUN_ID} WORKER_ID={WORKER_ID}")
    log("INFO", f"SRC_BUCKET=s3://{SRC_BUCKET}/")
    log("INFO", f"DST_BUCKET=s3://{DST_BUCKET}/")
    log("INFO", f"ARTIFACTS_BUCKET=s3://{ARTIFACTS_BUCKET}/")
    log("INFO", f"MANIFESTS_PREFIX={MANIFESTS_PREFIX}")
    log("INFO", f"STATE_PREFIX={STATE_PREFIX}")
    log("INFO", f"DONE_PREFIX={DONE_PREFIX}")
    log("INFO", f"MOVE_WORKERS={MOVE_WORKERS} MOVE_PREFETCH={MOVE_PREFETCH}")
    log("INFO", f"MOVE_DELETE_WORKERS={MOVE_DELETE_WORKERS} COPY_PART_WORKERS={COPY_PART_WORKERS}")
    log("INFO", f"METRICS_URL={METRICS_URL}")
    log("INFO", f"Soft stop at UTC ts={SOFT_STOP_TS}")

    WORKDIR = tempfile.mkdtemp()
    log("INFO", f"WORKDIR={WORKDIR}")

    if not SHARDING_ENABLED:
        acquire_lock()

    put_run_marker("running", started_at=utc_iso())

    if SHARDING_ENABLED:
        done_event = threading.Event()
        reporter = None
        if REPORT_EVERY > 0:
            reporter = threading.Thread(
                target=progress_reporter, args=(None, done_event), daemon=True
            )
            reporter.start()

        try:
            result = process_sharded_manifest_groups()
        finally:
            done_event.set()
            if reporter:
                reporter.join(timeout=1)

        snapshot = counter_snapshot()

        if STOP_REQUESTED:
            put_run_marker("partial", stopped_at=utc_iso())
            log("INFO", "Move stopped gracefully by time window or signal.")
            return result

        put_run_marker(
            "success" if result == 0 and snapshot["failed"] == 0 else "failed",
            finished_at=utc_iso(),
            processed=snapshot["processed"],
            failed=snapshot["failed"],
        )

        if result != 0 or snapshot["failed"] > 0:
            return 1

        log("INFO", "streaming-move.py completed successfully.")
        return 0

    manifest_keys = list_manifest_keys()
    counters["manifests_total"] = len(manifest_keys)

    if not manifest_keys:
        log("INFO", "No pending manifests found. Nothing to do.")
        put_run_marker("success", finished_at=utc_iso())
        return 0

    log("INFO", f"Found pending manifests: {len(manifest_keys)}")

    done_event = threading.Event()
    reporter = None
    if REPORT_EVERY > 0:
        reporter = threading.Thread(target=progress_reporter, args=(len(manifest_keys), done_event), daemon=True)
        reporter.start()

    try:
        process_manifests(manifest_keys)
    finally:
        done_event.set()
        if reporter:
            reporter.join(timeout=1)

    snapshot = counter_snapshot()

    if STOP_REQUESTED:
        put_run_marker("partial", stopped_at=utc_iso())
        log("INFO", "Move stopped gracefully by time window or signal.")
        return 0

    log(
        "INFO",
        "Summary: "
        f"manifests_total={snapshot['manifests_total']}, "
        f"processed={snapshot['processed']}, "
        f"skipped={snapshot['skipped']}, "
        f"reconciled={snapshot['reconciled']}, "
        f"failed={snapshot['failed']}, "
        f"copied_files={snapshot['copied_files']}, "
        f"deleted_files={snapshot['deleted_files']}, "
        f"purged_files={snapshot['purged_files']}, "
        f"quarantined_files={snapshot['quarantined_files']}, "
        f"quarantine_failures={snapshot['quarantine_failures']}",
    )

    put_run_marker(
        "success" if snapshot["failed"] == 0 else "failed",
        finished_at=utc_iso(),
        processed=snapshot["processed"],
        failed=snapshot["failed"],
    )

    if snapshot["failed"] > 0:
        return 1

    log("INFO", "streaming-move.py completed successfully.")
    return 0


def main():
    exit_code = 1
    try:
        exit_code = main_impl()
    except SystemExit as exc:
        code = exc.code
        exit_code = code if isinstance(code, int) else 1
    except Exception as exc:
        log("ERROR", f"Unhandled error: {exc}")
        for line in traceback.format_exc().rstrip().splitlines():
            log("ERROR", line)
        exit_code = 1
    finally:
        on_exit(exit_code)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
