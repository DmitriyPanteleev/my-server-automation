#!/usr/bin/env python3

import hashlib
import json
import multiprocessing
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
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone

from common import (
    bootstrap_log,
    build_metrics_url,
    env_bool,
    env_float,
    env_int,
    env_str,
    env_str_optional,
    format_client_error,
    next_repack_budget,
    utc_iso,
)
from zip_merge import merge_payloads

warnings.filterwarnings("ignore", message="Boto3 will no longer support Python .*")


try:
    import boto3
    from boto3.s3.transfer import TransferConfig
    from botocore.config import Config
    from botocore.exceptions import ClientError
    from s3_lease import S3LeaseClient
except ImportError as exc:
    bootstrap_log("ERROR", f"Required Python module is missing: {exc}. Install boto3 before running streaming-repack.py.")
    raise SystemExit(1)


def basename_s3(key):
    return key.rstrip("/").rsplit("/", 1)[-1]


def basename_without_zip(key):
    name = basename_s3(key)
    return name[:-4] if name.endswith(".zip") else name


SRC_BUCKET = env_str("SRC_BUCKET")
DST_BUCKET = env_str("DST_BUCKET")
ARTIFACTS_BUCKET = env_str("ARTIFACTS_BUCKET")

# Базовый служебный префикс обязателен; подпрефиксы по умолчанию выводятся из него
# и при необходимости переопределяются отдельными env (это производная величина, а
# не «дефолт, привязанный к окружению»).
ARTIFACTS_PREFIX = env_str("ARTIFACTS_PREFIX")
MANIFESTS_PREFIX = os.environ.get("MANIFESTS_PREFIX", f"{ARTIFACTS_PREFIX}/manifests")
STATE_PREFIX = os.environ.get("STATE_PREFIX", f"{ARTIFACTS_PREFIX}/state")
LOCKS_PREFIX = os.environ.get("LOCKS_PREFIX", f"{ARTIFACTS_PREFIX}/locks")
RUNS_PREFIX = os.environ.get("RUNS_PREFIX", f"{ARTIFACTS_PREFIX}/runs/repack")
LEASES_PREFIX = os.environ.get("LEASES_PREFIX", f"{ARTIFACTS_PREFIX}/leases")
SHARD_DONE_PREFIX = os.environ.get("SHARD_DONE_PREFIX", f"{ARTIFACTS_PREFIX}/shards/done")
# Префиксы верхнего уровня, КУДА пишет сам пайплайн: их нельзя обрабатывать как
# исходные proxy на полном прогоне (ONLY_TARGET пуст), иначе repack подаст свой
# же вывод на вход. REPACK_SUFFIX добавляется к имени proxy и образует префикс
# компактного вывода (<proxy><suffix>/); QUARANTINE_PREFIX совпадает с move, туда
# складываются битые файлы. См. include_proxy / is_reserved_top_prefix.
REPACK_SUFFIX = env_str("REPACK_SUFFIX")
QUARANTINE_PREFIX = env_str("QUARANTINE_PREFIX")
# Дополнительные верхнеуровневые префиксы, которые repack НЕ трогает как исходные
# данные (например, шарды со своим собственным retention). Задаётся списком через
# запятую в RESERVED_TOP_PREFIXES; пусто = таких префиксов нет (no-op).
# См. is_reserved_top_prefix.
RESERVED_TOP_PREFIXES = tuple(
    item.strip()
    for item in env_str_optional("RESERVED_TOP_PREFIXES").split(",")
    if item.strip()
)
# Управляющий слой адаптивного бюджета (общий с streaming-move). repack ограничивает
# число МАНИФЕСТОВ за прогон тем, что move успевает разгрести за ночное окно (один
# манифест = один ~300 МБ copy-back move, ~фиксированная стоимость). См.
# common.next_repack_budget.
CONTROL_PREFIX = os.environ.get("CONTROL_PREFIX", f"{ARTIFACTS_PREFIX}/control")
MOVE_STATS_PREFIX = os.environ.get("MOVE_STATS_PREFIX", f"{CONTROL_PREFIX}/move-stats")
MOVE_LAST_RUN_KEY = os.environ.get("MOVE_LAST_RUN_KEY", f"{CONTROL_PREFIX}/move-last-run.json")
BUDGET_KEY = os.environ.get("BUDGET_KEY", f"{CONTROL_PREFIX}/repack-budget.json")
# Счётчики продукции по каждому pod'у за прогон. Работа перетекает между pod'ами,
# поэтому бюджет считается по СУММЕ всех pod'ов, а не по локальному счётчику:
# каждый pod публикует свой счётчик здесь, и все суммируют их для решения о стопе.
REPACK_PROGRESS_PREFIX = os.environ.get("REPACK_PROGRESS_PREFIX", f"{CONTROL_PREFIX}/repack-progress")

RUN_DATE = os.environ.get("RUN_DATE", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
RUN_TS = os.environ.get("RUN_TS", datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
SHARD_RUN_ID = os.environ.get("SHARD_RUN_ID") or RUN_TS
WORKER_ID = os.environ.get("WORKER_ID") or os.environ.get("HOSTNAME") or f"worker-{os.getpid()}"
# Стабильный ограниченный id воркера для grouping-ключа метрик: в Indexed Job это
# completion index (0..N-1), переиспользуемый каждый прогон, чтобы Pushgateway не
# копил мёртвые серии. По умолчанию "" — один общий ключ (не-indexed режим).
WORKER_INDEX = os.environ.get("WORKER_INDEX") or os.environ.get("JOB_COMPLETION_INDEX") or ""

SIZE_LIMIT_MB = env_int("SIZE_LIMIT_MB")
SIZE_LIMIT = SIZE_LIMIT_MB * 1024 * 1024

DOWNLOAD_WORKERS = max(1, env_int("DOWNLOAD_WORKERS"))
MAX_PARALLEL_GROUPS = max(1, env_int("MAX_PARALLEL_GROUPS"))

WINDOW_STOP_HOUR_UTC = env_int("WINDOW_STOP_HOUR_UTC")
SOFT_STOP_GRACE_SECONDS = env_int("SOFT_STOP_GRACE_SECONDS")
LOCK_STALE_AFTER_SECONDS = env_int("LOCK_STALE_AFTER_SECONDS")
LEASE_TTL_SECONDS = max(60, env_int("LEASE_TTL_SECONDS"))
LEASE_HEARTBEAT_SECONDS = max(10, env_int("LEASE_HEARTBEAT_SECONDS"))
CLAIM_IDLE_SLEEP_SECONDS = max(1, env_int("CLAIM_IDLE_SLEEP_SECONDS"))
SHARDING_ENABLED = env_bool("SHARDING_ENABLED")

# Один S3-префикс, ограничивающий прогон. Пусто — обрабатывать всё. Префикс proxy
# (напр. "proxyA/") выбирает весь proxy и все его группы; префикс группы
# (напр. "proxyA/groupX.rt/") выбирает одну группу.
ONLY_TARGET = env_str_optional("ONLY_TARGET")

PENDING_MANIFEST_INSPECT_LIMIT = max(0, env_int("PENDING_MANIFEST_INSPECT_LIMIT"))
S3_MAX_ATTEMPTS = max(1, env_int("S3_MAX_ATTEMPTS"))
TRACEBACK_ON_ERROR = env_bool("TRACEBACK_ON_ERROR")
DOWNLOAD_MULTIPART_THRESHOLD_MB = max(1, env_int("DOWNLOAD_MULTIPART_THRESHOLD_MB"))
DOWNLOAD_MULTIPART_CHUNKSIZE_MB = max(1, env_int("DOWNLOAD_MULTIPART_CHUNKSIZE_MB"))
DOWNLOAD_PART_WORKERS = max(1, env_int("DOWNLOAD_PART_WORKERS"))
UPLOAD_MULTIPART_THRESHOLD_MB = max(1, env_int("UPLOAD_MULTIPART_THRESHOLD_MB"))
UPLOAD_MULTIPART_CHUNKSIZE_MB = max(1, env_int("UPLOAD_MULTIPART_CHUNKSIZE_MB"))
UPLOAD_PART_WORKERS = max(1, env_int("UPLOAD_PART_WORKERS"))

# Уровень пересжатия компактного payload'а (0..9). Вход — уже сжатые zip'ы,
# поэтому меньший уровень экономит CPU почти без потери размера.
REPACK_COMPRESS_LEVEL = min(9, max(0, env_int("REPACK_COMPRESS_LEVEL")))
# Число процессов-воркеров для слияния батчей. Слияние CPU-bound и держит GIL,
# поэтому пул процессов позволяет одному pod'у задействовать больше одного ядра.
# 1 — слияние без пула, в основном потоке.
MERGE_PROCESSES = max(1, env_int("MERGE_PROCESSES"))

# Классы хранилища S3, из которых repack вправе скачивать исходники. Архивные
# классы (GLACIER/DEEP_ARCHIVE) недоступны для GET без restore (часы/дни), поэтому
# их объекты не входят ни в один батч: пайплайн их не качает, не компактит и не
# удаляет, а курсор группы проматывается за них. Граница класса внутри группы
# монотонна (старое архивное -> новое STANDARD), так что это разовый сдвиг.
# Пусто или "*" -> проверка выключена (любой класс допустим).
# Рекомендуется НЕ включать в список INTELLIGENT_TIERING: листинг не отличает
# горячий IT-объект от ушедшего в архивный тир (ARCHIVE_ACCESS/DEEP_ARCHIVE_ACCESS),
# а тот недоступен для GET.
_raw_allowed_storage_classes = env_str_optional("ALLOWED_STORAGE_CLASSES").strip()
STORAGE_CLASS_GUARD_ENABLED = _raw_allowed_storage_classes not in ("", "*")
ALLOWED_STORAGE_CLASSES = frozenset(
    cls.strip().upper() for cls in _raw_allowed_storage_classes.split(",") if cls.strip()
)


def is_eligible_storage_class(storage_class):
    """True, если объект этого класса немедленно доступен для GET (его можно скачать).

    Листинг и HEAD для STANDARD часто опускают StorageClass — пустое значение
    трактуем как STANDARD. При выключенной проверке допустим любой класс.
    """
    if not STORAGE_CLASS_GUARD_ENABLED:
        return True
    return (storage_class or "STANDARD").upper() in ALLOWED_STORAGE_CLASSES


# Адаптивный бюджет продукции (МАНИФЕСТОВ за прогон, суммарно по pod'ам), подстраивается
# под измеренную ночную ёмкость move. Выключен или не задан (и статистики move ещё
# нет) — repack работает без ограничения, так что первый прогон стартует чисто.
REPACK_BUDGET_ENABLED = env_bool("REPACK_BUDGET_ENABLED")
# Явный стартовый бюджет (манифестов), используется только пока move ни разу не отчитался.
REPACK_MANIFEST_BUDGET_PER_RUN = max(0, env_int("REPACK_MANIFEST_BUDGET_PER_RUN"))
# Параметры регулятора: целиться на BUDGET_MARGIN ниже ёмкости move; игнорировать
# изменения меньше BUDGET_DELTA_BAND (мёртвая зона против колебаний); двигаться к
# цели не более чем на BUDGET_MAX_STEP за прогон. FLOOR/CEIL — опциональные границы.
BUDGET_MARGIN = max(0.0, min(0.9, env_float("BUDGET_MARGIN")))
BUDGET_DELTA_BAND = max(0.0, min(0.9, env_float("BUDGET_DELTA_BAND")))
BUDGET_MAX_STEP = max(0.01, min(1.0, env_float("BUDGET_MAX_STEP")))
BUDGET_FLOOR = max(0, env_int("BUDGET_FLOOR"))
BUDGET_CEIL = max(0, env_int("BUDGET_CEIL"))

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

REPACK_LOCK_KEY = f"{LOCKS_PREFIX}/repack.lock"
RUN_MARKER_KEY = f"{RUNS_PREFIX}/{RUN_TS}.json"

cfg = Config(
    region_name=AWS_REGION,
    max_pool_connections=(
        (MAX_PARALLEL_GROUPS * DOWNLOAD_WORKERS * DOWNLOAD_PART_WORKERS)
        + UPLOAD_PART_WORKERS
        + 32
    ),
    retries={
        "mode": "standard",
        "total_max_attempts": S3_MAX_ATTEMPTS,
    },
    s3={
        "us_east_1_regional_endpoint": "regional",
        "addressing_style": "virtual",
    },
)
s3 = boto3.client("s3", config=cfg)
download_transfer_config = TransferConfig(
    multipart_threshold=DOWNLOAD_MULTIPART_THRESHOLD_MB * 1024 * 1024,
    multipart_chunksize=DOWNLOAD_MULTIPART_CHUNKSIZE_MB * 1024 * 1024,
    max_concurrency=DOWNLOAD_PART_WORKERS,
    use_threads=True,
)
upload_transfer_config = TransferConfig(
    multipart_threshold=UPLOAD_MULTIPART_THRESHOLD_MB * 1024 * 1024,
    multipart_chunksize=UPLOAD_MULTIPART_CHUNKSIZE_MB * 1024 * 1024,
    max_concurrency=UPLOAD_PART_WORKERS,
    use_threads=True,
)

PUT_OBJECT_SUPPORTS_IF_NONE_MATCH = (
    "IfNoneMatch" in s3.meta.service_model.operation_model("PutObject").input_shape.members
)

log_lock = threading.Lock()
counter_lock = threading.Lock()

counters = {
    "proxies_processed": 0,
    "groups_total": 0,
    "groups_processed": 0,
    "pending_manifests": 0,
    "bad_files": 0,
    "files_compacted": 0,
    # Число произведённых compact-манифестов (по одному ~300 МБ copy-back move на
    # каждый) — единица бюджета. Считаем только «repacked»-манифесты; purge-манифесты
    # (без payload'а) для move дёшевы и в бюджет не входят, как и в move.copied_files.
    "manifests_created": 0,
    "archived_skipped": 0,
}

WORKDIR = None
REPACK_LOCK_ACQUIRED = False
STOP_REQUESTED = False
# Глобальный лимит МАНИФЕСТОВ за прогон, суммарно по всем pod'ам (0 = без
# ограничения). Вычисляется на старте по ночной ёмкости move; проверяется в цикле
# батчей против суммы опубликованных счётчиков всех pod'ов.
RUN_MANIFEST_BUDGET = 0
BUDGET_THROTTLED = False
# Группы (proxy_name, group_name), у которых на старте остались неразгребённые
# манифесты прошлого прогона. repack их пропускает (per-group interlock), чтобы не
# класть новый вывод поверх недогнанной группы; разгребённые группы при этом идут
# своим чередом. PENDING_MANIFEST_COUNT — общий долг (число манифестов), он питает
# адаптивный бюджет. Заполняется scan_pending_backlog() до захвата групп.
PENDING_GROUPS = set()
PENDING_MANIFEST_COUNT = 0

_merge_pool = None
_merge_pool_lock = threading.Lock()


def get_merge_pool():
    global _merge_pool
    if MERGE_PROCESSES <= 1:
        return None
    if _merge_pool is None:
        with _merge_pool_lock:
            if _merge_pool is None:
                _merge_pool = ProcessPoolExecutor(
                    max_workers=MERGE_PROCESSES,
                    mp_context=multiprocessing.get_context("spawn"),
                )
    return _merge_pool


def shutdown_merge_pool():
    global _merge_pool
    if _merge_pool is not None:
        _merge_pool.shutdown(wait=False, cancel_futures=True)
        _merge_pool = None


def run_merge(out_zip, arcname, inputs):
    """Слить батч, вынося CPU-работу в процесс-воркер, когда пул включён.

    Возвращает (bad_pairs, produced), где bad_pairs — список (key, reason).
    """
    pool = get_merge_pool()
    if pool is None:
        return merge_payloads(out_zip, arcname, inputs, REPACK_COMPRESS_LEVEL)
    return pool.submit(merge_payloads, out_zip, arcname, inputs, REPACK_COMPRESS_LEVEL).result()


def log(level, message):
    now = datetime.now()
    ts = now.strftime("%Y-%m-%d %H:%M:%S") + f".{now.microsecond // 1000:03d}"
    stream = sys.stderr if level == "ERROR" else sys.stdout
    with log_lock:
        print(f"[{ts}] [{level}] {message}", file=stream, flush=True)


def set_counter(name, value):
    with counter_lock:
        counters[name] = value


def inc_counter(name, value=1):
    with counter_lock:
        counters[name] += value
        return counters[name]


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


def compute_repack_stop_ts():
    try:
        run_date = datetime.strptime(RUN_DATE, "%Y-%m-%d").date()
    except ValueError as exc:
        log("ERROR", f"Invalid RUN_DATE={RUN_DATE!r}; expected YYYY-MM-DD.")
        raise SystemExit(1) from exc

    stop_dt = datetime.combine(run_date, datetime.min.time(), tzinfo=timezone.utc)
    stop_dt += timedelta(hours=WINDOW_STOP_HOUR_UTC)
    return int(stop_dt.timestamp())


REPACK_STOP_TS = compute_repack_stop_ts()
SOFT_STOP_TS = REPACK_STOP_TS - SOFT_STOP_GRACE_SECONDS


def request_stop(reason):
    global STOP_REQUESTED
    if not STOP_REQUESTED:
        STOP_REQUESTED = True
        log("WARN", reason)


def signal_handler(_signum, _frame):
    request_stop("TERM/INT received, graceful stop requested")
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
        request_stop("Soft stop: repack window end is near.")
        return True
    return False


class LockPutError(Exception):
    pass


def put_lock_object_if_absent(body):
    if PUT_OBJECT_SUPPORTS_IF_NONE_MATCH:
        try:
            s3.put_object(
                Bucket=ARTIFACTS_BUCKET,
                Key=REPACK_LOCK_KEY,
                Body=body,
                IfNoneMatch="*",
            )
            return
        except ClientError as exc:
            raise LockPutError(format_client_error(exc)) from exc

    if not WORKDIR:
        raise LockPutError("WORKDIR is not initialized")

    lock_file = os.path.join(WORKDIR, "repack-lock.json")
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
                REPACK_LOCK_KEY,
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


def get_json_object(bucket, key):
    response = s3.get_object(Bucket=bucket, Key=key)
    body = response["Body"].read()
    return json.loads(body.decode("utf-8"))


def encode_pretty_json(value):
    return (json.dumps(value, indent=2) + "\n").encode("utf-8")


def put_json_object(bucket, key, value, *, pretty=True):
    if pretty:
        body = encode_pretty_json(value)
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


def head_size_and_class(bucket, key):
    """Вернуть (size, storage_class) объекта или (None, None), если он недоступен."""
    try:
        response = s3.head_object(Bucket=bucket, Key=key)
        return int(response.get("ContentLength", 0)), (response.get("StorageClass") or "STANDARD")
    except ClientError:
        return None, None


def acquire_lock():
    global REPACK_LOCK_ACQUIRED

    lock_body = {
        "run_date": RUN_DATE,
        "run_ts": RUN_TS,
        "started_at": utc_iso(),
    }
    lock_bytes = (json.dumps(lock_body, separators=(",", ":")) + "\n").encode("utf-8")

    try:
        put_lock_object_if_absent(lock_bytes)
        REPACK_LOCK_ACQUIRED = True
        return
    except LockPutError as exc:
        put_error = str(exc)

    existing = {}
    try:
        existing = get_json_object(ARTIFACTS_BUCKET, REPACK_LOCK_KEY)
    except Exception:
        if put_error:
            log("ERROR", f"Failed to acquire repack lock: s3://{ARTIFACTS_BUCKET}/{REPACK_LOCK_KEY} aws_error={put_error}")
        else:
            log("ERROR", f"Failed to acquire repack lock: s3://{ARTIFACTS_BUCKET}/{REPACK_LOCK_KEY}")
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
                "Existing repack lock is stale, removing and retrying: "
                f"s3://{ARTIFACTS_BUCKET}/{REPACK_LOCK_KEY} "
                f"age_seconds={age_seconds} threshold_seconds={LOCK_STALE_AFTER_SECONDS}",
            )
            delete_object_quiet(ARTIFACTS_BUCKET, REPACK_LOCK_KEY)

            try:
                put_lock_object_if_absent(lock_bytes)
                REPACK_LOCK_ACQUIRED = True
                return
            except LockPutError as exc:
                put_error = str(exc)

    log(
        "ERROR",
        "Repack lock already exists: "
        f"s3://{ARTIFACTS_BUCKET}/{REPACK_LOCK_KEY} "
        f"run_date={existing.get('run_date') or '<unknown>'} "
        f"run_ts={existing.get('run_ts') or '<unknown>'} "
        f"started_at={existing.get('started_at') or '<unknown>'} "
        f"aws_error={put_error or '<none>'}",
    )
    raise SystemExit(1)


def cleanup_lock():
    if REPACK_LOCK_ACQUIRED and REPACK_LOCK_KEY:
        delete_object_quiet(ARTIFACTS_BUCKET, REPACK_LOCK_KEY)


def on_exit(exit_code):
    failure_value = 0 if exit_code == 0 else 1

    if exit_code == 0:
        log("INFO", "Script completed successfully.")
    else:
        log("ERROR", f"Script failed. Exit code={exit_code}.")

    snapshot = counter_snapshot()
    send_metric("failure", failure_value)
    send_metric("proxies_processed", snapshot["proxies_processed"])
    send_metric("groups_processed", snapshot["groups_processed"])
    send_metric("pending_manifests", snapshot["pending_manifests"])
    send_metric("bad_files", snapshot["bad_files"])
    send_metric("files_compacted", snapshot["files_compacted"])
    send_metric("manifests_created", snapshot["manifests_created"])
    send_metric("archived_skipped", snapshot["archived_skipped"])
    send_metric("repack_manifest_budget", RUN_MANIFEST_BUDGET)
    send_metric("repack_throttled", 1 if BUDGET_THROTTLED else 0)

    shutdown_merge_pool()
    cleanup_lock()

    if WORKDIR and os.path.isdir(WORKDIR):
        shutil.rmtree(WORKDIR, ignore_errors=True)


def list_json_keys(bucket, prefix):
    keys = []
    paginator = s3.get_paginator("list_objects_v2")

    for page in paginator.paginate(
        Bucket=bucket,
        Prefix=prefix,
        PaginationConfig={"PageSize": 1000},
    ):
        for obj in page.get("Contents", []):
            key = obj.get("Key", "")
            if key.endswith(".json"):
                keys.append(key)

    keys.sort()
    return keys


def manifest_group_from_key(manifest_key):
    """Достать (proxy_name, group_name) из ключа манифеста или None.

    Ключи вида "<MANIFESTS_PREFIX>/<proxy>/<group>/<file>.json"; нужны только
    proxy/group, поэтому это чистый разбор строки без S3 GET.
    """
    prefix = MANIFESTS_PREFIX.rstrip("/") + "/"
    if not manifest_key.startswith(prefix):
        return None
    rel = manifest_key[len(prefix):]
    parts = rel.split("/")
    if len(parts) < 3 or not parts[0] or not parts[1]:
        return None
    return parts[0], parts[1]


def scan_pending_backlog():
    """Определить, у каких групп остались неразгребённые манифесты, не блокируя прогон.

    Реализует per-group interlock: backlog-группы запоминаются, чтобы
    build_group_candidates пропустил только их, а разгребённые группы шли своим
    чередом. Общее число манифестов питает адаптивный бюджет (большой долг
    тормозит новую продукцию, пока move догоняет).

    Выполняется один раз на старте, до захвата групп, поэтому все присутствующие
    манифесты — из прошлых прогонов (этот ещё ничего не произвёл), то есть чистый
    долг. Перечисляются только ключи манифестов (ограниченный префикс), без GET.

    Заполняет глобальные PENDING_GROUPS / PENDING_MANIFEST_COUNT.
    """
    global PENDING_GROUPS, PENDING_MANIFEST_COUNT
    try:
        manifest_keys = list_json_keys(ARTIFACTS_BUCKET, f"{MANIFESTS_PREFIX}/")
    except Exception as exc:
        # Fail-safe: если нельзя понять, какие группы в долгу, не рискуем класть
        # вывод поверх недогнанной группы — блокируем прогон.
        log("ERROR", f"Failed to list pending manifests under s3://{ARTIFACTS_BUCKET}/{MANIFESTS_PREFIX}/: {exc}")
        raise SystemExit(1)

    per_group = {}
    for manifest_key in manifest_keys:
        parsed = manifest_group_from_key(manifest_key)
        if parsed is None:
            continue
        per_group[parsed] = per_group.get(parsed, 0) + 1

    PENDING_GROUPS = set(per_group.keys())
    PENDING_MANIFEST_COUNT = len(manifest_keys)
    set_counter("pending_manifests", PENDING_MANIFEST_COUNT)
    send_metric("pending_manifests", PENDING_MANIFEST_COUNT)

    if PENDING_MANIFEST_COUNT == 0:
        log("INFO", "No pending move manifests; all groups are free for repack.")
        return

    log(
        "INFO",
        f"Pending move manifests: {PENDING_MANIFEST_COUNT} across {len(PENDING_GROUPS)} group(s). "
        "These groups are skipped this run (per-group interlock); move drains them "
        "and the adaptive budget is reduced by the backlog so the debt shrinks.",
    )
    top = sorted(per_group.items(), key=lambda kv: kv[1], reverse=True)[:PENDING_MANIFEST_INSPECT_LIMIT]
    for (proxy_name, group_name), count in top:
        log("INFO", f"  backlog: {count:6d}  {proxy_name}/{group_name}")
    if len(PENDING_GROUPS) > PENDING_MANIFEST_INSPECT_LIMIT:
        log("INFO", f"  backlog: ... {len(PENDING_GROUPS) - PENDING_MANIFEST_INSPECT_LIMIT} more group(s)")


def list_common_prefixes(bucket, prefix=""):
    prefixes = []
    paginator = s3.get_paginator("list_objects_v2")
    kwargs = {
        "Bucket": bucket,
        "Delimiter": "/",
        "PaginationConfig": {"PageSize": 1000},
    }
    if prefix:
        kwargs["Prefix"] = prefix

    for page in paginator.paginate(**kwargs):
        for item in page.get("CommonPrefixes", []):
            item_prefix = item.get("Prefix", "")
            if item_prefix:
                prefixes.append(item_prefix)

    return prefixes


def list_group_objects(group_prefix, start_after=""):
    objects = []
    paginator = s3.get_paginator("list_objects_v2")
    kwargs = {
        "Bucket": SRC_BUCKET,
        "Prefix": group_prefix,
        "PaginationConfig": {"PageSize": 1000},
    }
    if start_after:
        kwargs["StartAfter"] = start_after

    for page in paginator.paginate(**kwargs):
        for obj in page.get("Contents", []):
            key = obj.get("Key", "")
            if key.endswith(".zip"):
                objects.append((key, int(obj.get("Size", 0)), obj.get("StorageClass") or "STANDARD"))

    objects.sort(key=lambda item: item[0])
    return objects


def upload_zip_file(local_path, bucket, key):
    s3.upload_file(
        local_path,
        bucket,
        key,
        ExtraArgs={"ContentType": "application/zip"},
        Config=upload_transfer_config,
    )


def download_one(remote_key, dest_dir):
    local_path = os.path.join(dest_dir, basename_s3(remote_key))
    s3.download_file(
        SRC_BUCKET,
        remote_key,
        local_path,
        Config=download_transfer_config,
    )
    return local_path


def download_batch_files(dest_dir, keys):
    if not keys:
        return

    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as executor:
        futures = [executor.submit(download_one, key, dest_dir) for key in keys]
        for future in futures:
            future.result()


def write_state(state_key, proxy_name, group_name, next_start_after, carry_key, carry_size, carry_open):
    state = {
        "proxy": proxy_name,
        "group": group_name,
        "next_start_after": next_start_after,
        "carry_key": carry_key,
        "carry_size": int(carry_size or 0),
        "carry_open": bool(carry_open),
        "updated_by": "repack",
        "updated_at": utc_iso(),
    }
    put_json_object(ARTIFACTS_BUCKET, state_key, state)


def load_group_state(state_key):
    carry_key = ""
    carry_size = 0
    carry_open = False
    next_start_after = ""

    try:
        state = get_json_object(ARTIFACTS_BUCKET, state_key)
        if not isinstance(state, dict):
            return carry_key, carry_size, carry_open, next_start_after
    except Exception:
        return carry_key, carry_size, carry_open, next_start_after

    carry_key = str(state.get("carry_key") or "")
    try:
        carry_size = int(state.get("carry_size") or 0)
    except (TypeError, ValueError):
        carry_size = 0

    raw_carry_open = state.get("carry_open", False)
    carry_open = raw_carry_open is True or str(raw_carry_open).lower() == "true"
    next_start_after = str(state.get("next_start_after") or "")
    return carry_key, carry_size, carry_open, next_start_after


def process_group(group_prefix, proxy_name, proxy_dir, dst_proxy_prefix, src_proxy_prefix, should_abort=None):
    group_name = basename_s3(group_prefix)
    group_dir = os.path.join(proxy_dir, group_name)
    state_key = f"{STATE_PREFIX}/{proxy_name}/{group_name}.json"

    os.makedirs(group_dir, exist_ok=True)

    try:
        carry_key, carry_size, carry_open, next_start_after = load_group_state(state_key)

        if carry_open and carry_key:
            actual_carry_size, carry_class = head_size_and_class(SRC_BUCKET, carry_key)
            if actual_carry_size is None:
                log("WARN", f"carry_key is not accessible anymore, dropping carry: {carry_key}")
                carry_key = ""
                carry_size = 0
                carry_open = False
            elif not is_eligible_storage_class(carry_class):
                log("WARN", f"carry_key moved to non-retrievable storage class {carry_class}, dropping carry: {carry_key}")
                carry_key = ""
                carry_size = 0
                carry_open = False
            else:
                carry_size = actual_carry_size

        all_files = list_group_objects(group_prefix, next_start_after)
        file_count = len(all_files)
        new_total_size = sum(size for _key, size, _sc in all_files)

        log("INFO", f"==== Processing group: {group_name} ({group_prefix}) ====")
        log(
            "INFO",
            f"group={group_name} carry_open={str(carry_open).lower()} "
            f"carry_key={carry_key or '<none>'} "
            f"next_start_after={next_start_after or '<none>'} "
            f"new_files={file_count} new_total_size={new_total_size} limit_bytes={SIZE_LIMIT}",
        )

        if file_count == 0 and ((not carry_open) or not carry_key):
            log("INFO", f"Nothing to do for group {group_name}.")
            return True

        file_index = 0
        batch_num = 1
        carry_available = carry_open
        carry_closed_persisted = False
        completed = True

        while True:
            if should_soft_stop():
                log("INFO", f"Soft stop: stop creating new batches for group {group_name}")
                completed = False
                break

            # Суммарный бюджет могли исчерпать другие pod'ы, пока мы держим группу;
            # останавливаемся до создания следующего батча.
            if check_repack_budget():
                log("INFO", f"Production budget reached; stop before next batch for group {group_name}")
                completed = False
                break

            if should_abort is not None and should_abort():
                log("WARN", f"Lease lost; abandoning group mid-flight without marking done: {group_name}")
                completed = False
                break

            total_size = 0
            uses_carry = False
            files = []

            if carry_available and carry_key:
                files.append(carry_key)
                total_size = carry_size
                uses_carry = True
                carry_available = False

            # Промотать ведущие архивные файлы (GLACIER/...): их нельзя скачать, в
            # батч они не входят. Двигаем курсор за них, чтобы будущие прогоны их не
            # перечисляли (целиком архивная группа так листается один раз). Только
            # когда батч ещё пуст: carry и STANDARD лежат выше границы, архив — ниже.
            if not files:
                last_skipped_key = ""
                skipped = 0
                while file_index < file_count:
                    key, _size, storage_class = all_files[file_index]
                    if not key:
                        file_index += 1
                        continue
                    if is_eligible_storage_class(storage_class):
                        break
                    last_skipped_key = key
                    skipped += 1
                    file_index += 1
                if skipped:
                    total_skipped = inc_counter("archived_skipped", skipped)
                    send_metric("archived_skipped", total_skipped)
                    log(
                        "INFO",
                        f"group={group_name}: skipped {skipped} archived (non-retrievable) "
                        f"source file(s); advancing cursor past {last_skipped_key}",
                    )
                    write_state(state_key, proxy_name, group_name, last_skipped_key, "", 0, False)
                    next_start_after = last_skipped_key

            while file_index < file_count:
                key, size, storage_class = all_files[file_index]

                if not key:
                    file_index += 1
                    continue

                # Архивный файл — жёсткая граница батча: не включаем (недоступен для
                # GET). Текущий пробег STANDARD закрываем здесь; на следующей итерации
                # блок выше промотает курсор за этот архивный участок.
                if not is_eligible_storage_class(storage_class):
                    break

                if total_size + size > SIZE_LIMIT and files:
                    break

                files.append(key)
                total_size += size
                file_index += 1

            if not files:
                break

            has_more = file_index < file_count

            if len(files) == 1:
                only_key = files[0]
                if uses_carry:
                    if has_more and not carry_closed_persisted:
                        log("INFO", f"Carry file is closed for future runs; next key does not fit: {carry_key}")
                        write_state(state_key, proxy_name, group_name, next_start_after, "", 0, False)
                        carry_closed_persisted = True
                        continue

                    log("INFO", f"Carry-only batch left untouched for {group_name}.")
                    break

                if has_more:
                    log(
                        "INFO",
                        "Singleton file cannot merge with the immediate next file, "
                        f"closing it and advancing state: {only_key}",
                    )
                    write_state(state_key, proxy_name, group_name, only_key, "", 0, False)
                    next_start_after = only_key
                    continue

                log("INFO", f"Tail singleton file detected, waiting for future arrivals: {only_key}")
                break

            if not has_more and total_size < SIZE_LIMIT:
                log(
                    "INFO",
                    f"Tail batch below size limit, waiting for future arrivals: "
                    f"group={group_name} files={len(files)} size={total_size} limit={SIZE_LIMIT}",
                )
                break

            first_key = files[0]
            last_key = files[-1]
            first_file = basename_without_zip(first_key)
            last_file = basename_without_zip(last_key)

            batch_dir = tempfile.mkdtemp(prefix=f"batch.{batch_num}.", dir=group_dir)
            download_dir = os.path.join(batch_dir, "in")
            output_dir = os.path.join(batch_dir, "out")
            os.makedirs(download_dir, exist_ok=True)
            os.makedirs(output_dir, exist_ok=True)

            try:
                log(
                    "INFO",
                    f"group={group_name} batch={batch_num} files={len(files)} "
                    f"size={total_size} first={first_file} last={last_file}",
                )

                download_batch_files(download_dir, files)

                inputs = []
                for remote_key in files:
                    local_zip = os.path.join(download_dir, basename_s3(remote_key))
                    if not os.path.isfile(local_zip):
                        raise RuntimeError(
                            "Downloaded file not found: "
                            f"remote_key={remote_key} local_path={local_zip}"
                        )
                    inputs.append((remote_key, local_zip))

                out_zip = os.path.join(output_dir, f"{first_file}.zip")
                bad_pairs, produced = run_merge(out_zip, first_file, inputs)
                bad_keys = [key for key, _reason in bad_pairs]

                for key, reason in bad_pairs:
                    log("WARN", f"Skipping bad/unreadable source file: key={key} reason={reason}")

                if bad_keys:
                    bad_total = inc_counter("bad_files", len(bad_keys))
                    send_metric("bad_files", bad_total)
                    log(
                        "ERROR",
                        f"group={group_name} batch={batch_num} bad_files={len(bad_keys)} "
                        f"keys={bad_keys}; skipped from compaction and marked for deletion",
                    )

                batch_id = f"{RUN_TS}__{first_file}__{last_file}"
                manifest_key = f"{MANIFESTS_PREFIX}/{proxy_name}/{group_name}/{batch_id}.json"

                manifest = {
                    "run_id": SHARD_RUN_ID,
                    "worker_id": WORKER_ID,
                    "run_date": RUN_DATE,
                    "run_ts": RUN_TS,
                    "proxy": proxy_name,
                    "group": group_name,
                    "src_bucket": SRC_BUCKET,
                    "dst_bucket": DST_BUCKET,
                    "src_proxy_prefix": src_proxy_prefix,
                    "dst_proxy_prefix": dst_proxy_prefix,
                    "src_keys": files,
                    "src_count": len(files),
                    "bad_src_keys": bad_keys,
                    "first_src_key": first_key,
                    "last_src_key": last_key,
                    "created_at": utc_iso(),
                }

                if produced:
                    compacted_size = os.path.getsize(out_zip)
                    dest_key = f"{dst_proxy_prefix}/{group_name}/{batch_id}.zip"

                    upload_zip_file(out_zip, DST_BUCKET, dest_key)
                    log("INFO", f"Uploaded compacted batch to s3://{DST_BUCKET}/{dest_key}")

                    manifest.update({
                        "target_key": first_key,
                        "compacted_key": dest_key,
                        "compacted_size": compacted_size,
                        "status": "repacked",
                    })
                else:
                    # В батче нет годного payload'а: все файлы битые/пустые. Пишем
                    # purge-манифест, чтобы streaming-move удалил эти исходные ключи
                    # и сдвинул курсор, сохранив консистентность последовательности.
                    log(
                        "ERROR",
                        f"group={group_name} batch={batch_num} has no usable payload; "
                        f"emitting purge manifest for {len(files)} source keys "
                        f"(first={first_file} last={last_file})",
                    )
                    manifest.update({
                        "target_key": "",
                        "compacted_key": "",
                        "compacted_size": 0,
                        "status": "purge",
                    })

                put_json_object(ARTIFACTS_BUCKET, manifest_key, manifest)
                log("INFO", f"Uploaded manifest to s3://{ARTIFACTS_BUCKET}/{manifest_key}")
                inc_counter("files_compacted", len(files))
                # Единица бюджета — compact-манифест (один ~300 МБ copy-back move).
                # purge-манифесты в бюджет не входят (для move дёшевы, без copy-back).
                if produced:
                    inc_counter("manifests_created", 1)
                publish_repack_progress()
            finally:
                shutil.rmtree(batch_dir, ignore_errors=True)

            batch_num += 1

            if check_repack_budget():
                log("INFO", f"Stop creating new batches for group {group_name}: production budget reached.")
                completed = False
                break

        return completed
    finally:
        shutil.rmtree(group_dir, ignore_errors=True)


def _aggregate_move_run(run_id):
    """Свести статистику всех pod'ов одного прогона move в единый сигнал о темпе.

    Возвращает None, если читаемых записей нет.
    """
    prefix = f"{MOVE_STATS_PREFIX}/{run_id}/"
    drained = 0
    manifests_drained = 0
    drain_seconds = 0
    window_seconds = 0
    capped = False
    pods = 0
    for key in list_json_keys(ARTIFACTS_BUCKET, prefix):
        try:
            rec = get_json_object(ARTIFACTS_BUCKET, key)
        except Exception:
            continue
        if not isinstance(rec, dict):
            continue
        pods += 1
        drained += int(rec.get("files_drained") or 0)
        # copied_files == число манифестов, перемещённых этим pod'ом (один copy-back
        # на манифест); сумма по pod'ам оценивает число разгребённых манифестов и
        # позволяет repack перевести свой счётчик долга в оценку числа файлов.
        manifests_drained += int(rec.get("copied_files") or 0)
        # Длительность прогона — по самому медленному pod'у; окна у всех одинаковы.
        drain_seconds = max(drain_seconds, int(rec.get("drain_seconds") or 0))
        window_seconds = max(window_seconds, int(rec.get("window_seconds") or 0))
        # capped — ТОЛЬКО подлинное исчерпание временного окна (под выработал окно и
        # остановился по soft-stop). Остаток манифестов (pending_at_end>0) сюда НЕ
        # входит: бэклог уже отдельно вычитается в common.next_repack_budget через
        # pending_backlog_manifests, а если под закончил раньше окна с остатком — это
        # застрявшие группы, а не нехватка времени. Раньше pending_at_end ошибочно
        # выставлял capped=True: ёмкость считалась по успетому объёму И ещё вычитался
        # бэклог — двойной штраф, схлопывавший бюджет (death spiral).
        if bool(rec.get("window_capped")):
            capped = True
    if pods == 0:
        return None
    return {
        "files_drained": drained,
        "manifests_drained": manifests_drained,
        "drain_seconds": drain_seconds,
        "window_seconds": window_seconds,
        "window_capped": capped,
        "pods": pods,
    }


def _prune_move_stats(keep_run_id):
    """Best-effort: удалить директории статистики move, кроме только что использованной."""
    try:
        seen = set()
        for key in list_json_keys(ARTIFACTS_BUCKET, f"{MOVE_STATS_PREFIX}/"):
            rel = key[len(MOVE_STATS_PREFIX) + 1:]
            run = rel.split("/", 1)[0]
            if run and run != keep_run_id and run not in seen:
                seen.add(run)
        for run in seen:
            for key in list_json_keys(ARTIFACTS_BUCKET, f"{MOVE_STATS_PREFIX}/{run}/"):
                s3.delete_object(Bucket=ARTIFACTS_BUCKET, Key=key)
    except Exception as exc:
        log("WARN", f"Failed to prune old move stats: {exc}")


def resolve_repack_budget():
    """Установить RUN_MANIFEST_BUDGET (лимит МАНИФЕСТОВ прогона) по ёмкости move.

    Читает сведённый темп последнего прогона move (в манифестах — по одному ~300 МБ
    copy-back на манифест), пересчитывает глобальный бюджет через
    common.next_repack_budget (идемпотентно: привязано к id прогона move, так что все
    pod'ы repack сходятся к одному значению) и сохраняет его. Без ограничения (0),
    если бюджет выключен, move ещё не отчитался и стартовое значение не задано, либо
    при любой ошибке.
    """
    global RUN_MANIFEST_BUDGET
    if not REPACK_BUDGET_ENABLED:
        RUN_MANIFEST_BUDGET = 0
        return

    try:
        try:
            pointer = get_json_object(ARTIFACTS_BUCKET, MOVE_LAST_RUN_KEY)
        except Exception:
            pointer = None
        latest_run = str((pointer or {}).get("run_id") or "")

        budget_doc = {}
        try:
            budget_doc = get_json_object(ARTIFACTS_BUCKET, BUDGET_KEY) or {}
        except Exception:
            budget_doc = {}
        prev_budget = int(budget_doc.get("manifest_budget") or 0)
        if prev_budget <= 0:
            prev_budget = REPACK_MANIFEST_BUDGET_PER_RUN  # может остаться 0 (старт без ограничения)

        global_budget = prev_budget
        if not latest_run:
            log("INFO", "Budget: no move run reported yet; "
                f"using bootstrap budget={prev_budget or 'uncapped'}.")
        elif str(budget_doc.get("based_on_move_run") or "") == latest_run:
            log("INFO", f"Budget: already applied move run {latest_run}; budget={prev_budget}.")
        else:
            agg = _aggregate_move_run(latest_run)
            if agg is None:
                log("INFO", f"Budget: move run {latest_run} has no stats; budget={prev_budget or 'uncapped'}.")
            else:
                # Долг move — это ровно число pending-манифестов (единица та же, что и
                # бюджет), пересчёт в файлы больше не нужен.
                global_budget, reason = next_repack_budget(
                    prev_budget,
                    agg["manifests_drained"],
                    agg["drain_seconds"],
                    agg["window_seconds"],
                    agg["window_capped"],
                    pending_backlog_manifests=PENDING_MANIFEST_COUNT,
                    margin=BUDGET_MARGIN,
                    delta_band=BUDGET_DELTA_BAND,
                    max_step=BUDGET_MAX_STEP,
                    floor=BUDGET_FLOOR,
                    ceil=BUDGET_CEIL,
                )
                put_json_object(ARTIFACTS_BUCKET, BUDGET_KEY, {
                    "manifest_budget": global_budget,
                    "based_on_move_run": latest_run,
                    "reason": reason,
                    "prev_budget": prev_budget,
                    "move_manifests_drained": agg["manifests_drained"],
                    "move_files_drained": agg["files_drained"],
                    "move_drain_seconds": agg["drain_seconds"],
                    "move_window_seconds": agg["window_seconds"],
                    "move_window_capped": agg["window_capped"],
                    "move_pods": agg["pods"],
                    "pending_manifests": PENDING_MANIFEST_COUNT,
                    "updated_at": utc_iso(),
                    "updated_by": WORKER_ID,
                })
                log("INFO",
                    f"Budget: move run {latest_run} drained={agg['manifests_drained']} manifests / "
                    f"{agg['files_drained']} files capped={agg['window_capped']}; "
                    f"backlog={PENDING_MANIFEST_COUNT} manifests "
                    f"-> global budget {prev_budget}->{global_budget} manifests ({reason})")
                _prune_move_stats(latest_run)

        if global_budget and global_budget > 0:
            RUN_MANIFEST_BUDGET = int(global_budget)
            log("INFO", f"Repack manifest budget this run (global, summed across pods): {RUN_MANIFEST_BUDGET}")
            prune_stale_repack_progress()
        else:
            RUN_MANIFEST_BUDGET = 0
            log("INFO", "Repack manifest budget: uncapped this run.")
    except Exception as exc:
        log("WARN", f"Failed to resolve repack budget (running uncapped): {exc}")
        RUN_MANIFEST_BUDGET = 0


def publish_repack_progress():
    """Опубликовать счётчики этого pod'а, чтобы соседи их суммировали (best-effort).

    manifests_created — единица бюджета; files_compacted публикуем рядом для метрик.
    """
    if RUN_MANIFEST_BUDGET <= 0:
        return
    try:
        idx = WORKER_INDEX if WORKER_INDEX != "" else "0"
        put_json_object(
            ARTIFACTS_BUCKET,
            f"{REPACK_PROGRESS_PREFIX}/{SHARD_RUN_ID}/{idx}.json",
            {
                "manifests_created": counters["manifests_created"],
                "files_compacted": counters["files_compacted"],
                "worker_index": idx,
                "at": utc_iso(),
            },
        )
    except Exception as exc:
        log("WARN", f"Failed to publish repack progress: {exc}")


def aggregate_repack_progress():
    """Суммировать число МАНИФЕСТОВ всех pod'ов этого прогона (свой уже учтён)."""
    total = 0
    try:
        for key in list_json_keys(ARTIFACTS_BUCKET, f"{REPACK_PROGRESS_PREFIX}/{SHARD_RUN_ID}/"):
            try:
                rec = get_json_object(ARTIFACTS_BUCKET, key)
                total += int((rec or {}).get("manifests_created") or 0)
            except Exception:
                continue
    except Exception as exc:
        log("WARN", f"Failed to read repack progress (using local tally): {exc}")
        return counters["manifests_created"]
    # Не отдаём значение меньше собственного счётчика (вдруг наша публикация отстала).
    return max(total, counters["manifests_created"])


def check_repack_budget():
    """Запросить мягкий стоп, когда СУММАРНОЕ число манифестов достигло бюджета.

    Работа перетекает между pod'ами, поэтому сравниваем сумму счётчиков всех pod'ов
    с глобальным бюджетом, а не локальный счётчик (иначе каждый pod выбрал бы бюджет целиком).
    """
    global BUDGET_THROTTLED
    if RUN_MANIFEST_BUDGET <= 0:
        return False
    total = aggregate_repack_progress()
    if total >= RUN_MANIFEST_BUDGET:
        if not BUDGET_THROTTLED:
            BUDGET_THROTTLED = True
            send_metric("repack_throttled", 1)
            request_stop(
                f"Production budget reached: run created ~{total} manifests "
                f">= budget {RUN_MANIFEST_BUDGET} (sized to move's measured drain capacity). "
                "Throttling repack so move can fully drain tonight; it resumes next run."
            )
        return True
    return False


def prune_stale_repack_progress():
    """Best-effort: удалить счётчики прогресса от прежних прогонов (с другими run id)."""
    try:
        seen = set()
        for key in list_json_keys(ARTIFACTS_BUCKET, f"{REPACK_PROGRESS_PREFIX}/"):
            rel = key[len(REPACK_PROGRESS_PREFIX) + 1:]
            run = rel.split("/", 1)[0]
            if run and run != SHARD_RUN_ID:
                seen.add(run)
        for run in seen:
            for key in list_json_keys(ARTIFACTS_BUCKET, f"{REPACK_PROGRESS_PREFIX}/{run}/"):
                s3.delete_object(Bucket=ARTIFACTS_BUCKET, Key=key)
    except Exception as exc:
        log("WARN", f"Failed to prune stale repack progress: {exc}")


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
    else:
        marker = {
            "run_date": RUN_DATE,
            "run_ts": RUN_TS,
            "run_id": SHARD_RUN_ID,
            "worker_id": WORKER_ID,
            "status": status,
            "created_at": extra.get("created_at") or extra.get("finished_at") or utc_iso(),
        }

    try:
        put_json_object(ARTIFACTS_BUCKET, RUN_MARKER_KEY, marker, pretty=False)
    except Exception as exc:
        log("WARN", f"Failed to write run marker s3://{ARTIFACTS_BUCKET}/{RUN_MARKER_KEY}: {exc}")


def process_proxy(src_proxy_prefix):
    global STOP_REQUESTED

    proxy_name = basename_s3(src_proxy_prefix)
    dst_proxy_prefix = f"{proxy_name}{REPACK_SUFFIX}"
    proxy_dir = os.path.join(WORKDIR, proxy_name)

    log("INFO", f"==== Processing proxy: {proxy_name} ({src_proxy_prefix}) ====")
    log("INFO", f"Dest proxy root: s3://{DST_BUCKET}/{dst_proxy_prefix}/")

    os.makedirs(proxy_dir, exist_ok=True)

    try:
        group_prefixes = list_common_prefixes(SRC_BUCKET, src_proxy_prefix)
        groups_in_proxy = len(group_prefixes)
        groups_total = inc_counter("groups_total", groups_in_proxy)
        send_metric("groups_total", groups_total)

        if not group_prefixes:
            log("INFO", f"No groups found under {src_proxy_prefix}, skipping proxy.")
            proxies_processed = inc_counter("proxies_processed")
            send_metric("proxies_processed", proxies_processed)
            return 0

        proxy_failed = False
        pending = {}
        group_iter = iter(group_prefixes)

        def submit_until_full(executor):
            global STOP_REQUESTED
            nonlocal pending
            while len(pending) < MAX_PARALLEL_GROUPS:
                try:
                    group_prefix = next(group_iter)
                except StopIteration:
                    return

                if not group_prefix:
                    continue

                if not include_group(group_prefix):
                    continue

                if should_soft_stop():
                    log("INFO", f"Soft stop: stop before next group in proxy {proxy_name}.")
                    STOP_REQUESTED = True
                    return

                future = executor.submit(
                    process_group,
                    group_prefix,
                    proxy_name,
                    proxy_dir,
                    dst_proxy_prefix,
                    src_proxy_prefix,
                )
                pending[future] = group_prefix

        with ThreadPoolExecutor(max_workers=MAX_PARALLEL_GROUPS) as executor:
            submit_until_full(executor)

            while pending:
                done, _not_done = wait(pending, timeout=1, return_when=FIRST_COMPLETED)

                if not done:
                    continue

                for future in done:
                    group_prefix = pending.pop(future)
                    try:
                        future.result()
                    except Exception as exc:
                        proxy_failed = True
                        log("ERROR", f"Group failed: {group_prefix}: {exc}")
                        if TRACEBACK_ON_ERROR:
                            for line in traceback.format_exc().rstrip().splitlines():
                                log("ERROR", line)

                    groups_processed = inc_counter("groups_processed")
                    send_metric("groups_processed", groups_processed)

                if not STOP_REQUESTED:
                    submit_until_full(executor)

        if proxy_failed:
            log("ERROR", f"One or more groups failed for proxy {proxy_name}")
            return 1

        proxies_processed = inc_counter("proxies_processed")
        send_metric("proxies_processed", proxies_processed)
        return 0
    finally:
        shutil.rmtree(proxy_dir, ignore_errors=True)


def is_reserved_top_prefix(top_prefix):
    """True для префикса верхнего уровня, который repack не должен перечислять как
    исходный proxy: либо туда пишет сам пайплайн (компактный вывод <proxy>-repack/,
    артефакты, карантин) — иначе на полном прогоне repack повторно компактил бы свой
    же вывод и впустую обходил префикс артефактов, — либо это префикс из
    RESERVED_TOP_PREFIXES (прод-шарды со своим коротким retention)."""
    name = top_prefix.rstrip("/")
    if not name:
        return False
    if name in RESERVED_TOP_PREFIXES:
        return True
    if name.endswith(REPACK_SUFFIX):
        return True
    for reserved in (ARTIFACTS_PREFIX, QUARANTINE_PREFIX):
        reserved = reserved.rstrip("/")
        # Совпадение с самим префиксом или с верхним уровнем, который его содержит
        # (например, ARTIFACTS_PREFIX="a/b" делает зарезервированным префикс "a/").
        if reserved and (name == reserved or reserved.startswith(name + "/")):
            return True
    return False


def include_proxy(src_proxy_prefix):
    # Не заходим в префиксы, куда пишет пайплайн (компактный вывод, артефакты,
    # карантин) — см. is_reserved_top_prefix.
    if is_reserved_top_prefix(src_proxy_prefix):
        log("INFO", f"Skipping reserved (non-source) prefix: {src_proxy_prefix}")
        return False
    # Заходим в proxy, если цель внутри него или сам proxy является целью.
    if not ONLY_TARGET:
        return True
    return src_proxy_prefix.startswith(ONLY_TARGET) or ONLY_TARGET.startswith(src_proxy_prefix)


def include_group(group_prefix):
    if not ONLY_TARGET:
        return True
    return group_prefix.startswith(ONLY_TARGET) or ONLY_TARGET.startswith(group_prefix)


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


def build_group_candidates():
    candidates = []
    proxy_prefixes = list_common_prefixes(SRC_BUCKET)
    proxy_count = 0
    skipped_backlogged = 0

    for src_proxy_prefix in proxy_prefixes:
        if not src_proxy_prefix or not include_proxy(src_proxy_prefix):
            continue

        proxy_count += 1
        proxy_name = basename_s3(src_proxy_prefix)
        dst_proxy_prefix = f"{proxy_name}{REPACK_SUFFIX}"
        group_prefixes = list_common_prefixes(SRC_BUCKET, src_proxy_prefix)

        for group_prefix in group_prefixes:
            if not group_prefix or not include_group(group_prefix):
                continue

            group_name = basename_s3(group_prefix)
            # Per-group interlock: пропускаем группу, которую move ещё не разгрёб,
            # чтобы repack не клал новый компактный вывод поверх неразгребённых
            # манифестов этой группы. Разгребённые группы идут своим чередом.
            if (proxy_name, group_name) in PENDING_GROUPS:
                skipped_backlogged += 1
                continue

            candidates.append(
                {
                    "proxy_name": proxy_name,
                    "group_name": group_name,
                    "group_prefix": group_prefix,
                    "src_proxy_prefix": src_proxy_prefix,
                    "dst_proxy_prefix": dst_proxy_prefix,
                }
            )

    if skipped_backlogged:
        log(
            "INFO",
            f"Per-group interlock: skipped {skipped_backlogged} group(s) with pending "
            f"manifests; {len(candidates)} group(s) free for repack this run.",
        )
    send_metric("proxies_total", proxy_count)
    send_metric("groups_backlogged", skipped_backlogged)
    set_counter("groups_total", len(candidates))
    send_metric("groups_total", len(candidates))
    return candidates


def mark_group_done(scope, candidate):
    key = group_done_key(scope, candidate["proxy_name"], candidate["group_name"])
    marker = {
        "run_id": SHARD_RUN_ID,
        "worker_id": WORKER_ID,
        "run_date": RUN_DATE,
        "run_ts": RUN_TS,
        "proxy": candidate["proxy_name"],
        "group": candidate["group_name"],
        "group_prefix": candidate["group_prefix"],
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


def process_sharded_groups():
    candidates = build_group_candidates()
    if not candidates:
        log("INFO", "No group candidates found. Nothing to do.")
        return 0

    log(
        "INFO",
        "Sharded repack worker started: "
        f"run_id={SHARD_RUN_ID} worker_id={WORKER_ID} candidates={len(candidates)} "
        f"lease_ttl_seconds={LEASE_TTL_SECONDS}",
    )

    lease_client = S3LeaseClient(
        client=s3,
        bucket=ARTIFACTS_BUCKET,
        owner=WORKER_ID,
        ttl_seconds=LEASE_TTL_SECONDS,
        heartbeat_seconds=LEASE_HEARTBEAT_SECONDS,
        log=log,
    )
    ordered_candidates = ordered_group_candidates(candidates)

    while not should_soft_stop():
        claimed_in_pass = False

        for candidate in ordered_candidates:
            if should_soft_stop():
                break

            # Не берём новую группу, когда суммарный бюджет прогона исчерпан.
            if check_repack_budget():
                break

            done_key = group_done_key("repack", candidate["proxy_name"], candidate["group_name"])
            if object_exists(ARTIFACTS_BUCKET, done_key):
                continue

            lease_key = group_lease_key("repack", candidate["proxy_name"], candidate["group_name"])
            lease = lease_client.try_acquire(
                lease_key,
                {
                    "scope": "repack",
                    "run_id": SHARD_RUN_ID,
                    "run_date": RUN_DATE,
                    "run_ts": RUN_TS,
                    "proxy": candidate["proxy_name"],
                    "group": candidate["group_name"],
                    "group_prefix": candidate["group_prefix"],
                },
            )
            if lease is None:
                continue

            claimed_in_pass = True
            group_id = group_candidate_id(candidate)
            lease_releasable = False

            try:
                lease.start_heartbeat()

                if object_exists(ARTIFACTS_BUCKET, done_key):
                    log("INFO", f"Group already completed by another worker after claim: {group_id}")
                    lease_releasable = True
                    continue

                log("INFO", f"Claimed group for repack: {group_id}")
                proxy_dir = os.path.join(WORKDIR, candidate["proxy_name"])
                group_completed = process_group(
                    candidate["group_prefix"],
                    candidate["proxy_name"],
                    proxy_dir,
                    candidate["dst_proxy_prefix"],
                    candidate["src_proxy_prefix"],
                    should_abort=lambda: lease.lost,
                )

                if not group_completed:
                    log("INFO", f"Group was not marked done because repack stopped before completion: {group_id}")
                    lease_releasable = True
                    break

                if lease.lost:
                    raise RuntimeError(f"Cannot mark group done because S3 lease was lost: {group_id}")

                mark_group_done("repack", candidate)
                groups_processed = inc_counter("groups_processed")
                send_metric("groups_processed", groups_processed)
                lease_releasable = True
                log("INFO", f"Completed sharded repack group: {group_id}")
            except Exception as exc:
                log("ERROR", f"Sharded repack group failed: {group_id}: {exc}")
                if TRACEBACK_ON_ERROR:
                    for line in traceback.format_exc().rstrip().splitlines():
                        log("ERROR", line)
                return 1
            finally:
                lease.stop_heartbeat()
                if lease_releasable:
                    lease.release()

        if STOP_REQUESTED:
            break

        if not claimed_in_pass:
            if lease_client.has_active_leases(group_lease_prefix("repack")):
                log("INFO", "No free repack groups right now; waiting for active leases.")
                sleep_until_next_claim()
                continue
            log("INFO", "No free or active repack group leases remain.")
            break

    if STOP_REQUESTED:
        log("INFO", "Sharded repack worker stopped gracefully.")

    return 0


def main_impl():
    global WORKDIR, STOP_REQUESTED

    log("INFO", "Starting streaming repack process...")
    log("INFO", f"RUN_DATE={RUN_DATE} RUN_TS={RUN_TS}")
    log("INFO", f"SHARDING_ENABLED={int(SHARDING_ENABLED)} SHARD_RUN_ID={SHARD_RUN_ID} WORKER_ID={WORKER_ID}")
    log("INFO", f"SRC_BUCKET=s3://{SRC_BUCKET}/")
    log("INFO", f"DST_BUCKET=s3://{DST_BUCKET}/")
    log("INFO", f"ARTIFACTS_BUCKET=s3://{ARTIFACTS_BUCKET}/")
    log("INFO", f"MANIFESTS_PREFIX={MANIFESTS_PREFIX}")
    log("INFO", f"STATE_PREFIX={STATE_PREFIX}")
    log("INFO", f"SIZE_LIMIT_BYTES={SIZE_LIMIT} ({SIZE_LIMIT_MB} MB)")
    log("INFO", f"DOWNLOAD_WORKERS={DOWNLOAD_WORKERS} MAX_PARALLEL_GROUPS={MAX_PARALLEL_GROUPS}")
    log("INFO", f"MERGE_PROCESSES={MERGE_PROCESSES} REPACK_COMPRESS_LEVEL={REPACK_COMPRESS_LEVEL}")
    log("INFO", f"METRICS_URL={METRICS_URL}")
    log("INFO", f"Soft stop at UTC ts={SOFT_STOP_TS}")
    if ONLY_TARGET:
        log("WARN", f"Running with target filter enabled: ONLY_TARGET={ONLY_TARGET}")
    else:
        log("WARN", "No target filter set: processing ALL proxies and groups.")

    WORKDIR = tempfile.mkdtemp()
    log("INFO", f"WORKDIR={WORKDIR}")

    scan_pending_backlog()
    resolve_repack_budget()
    if not SHARDING_ENABLED:
        acquire_lock()

    put_run_marker("running", started_at=utc_iso())

    if SHARDING_ENABLED:
        result = process_sharded_groups()

        if STOP_REQUESTED:
            put_run_marker("partial", stopped_at=utc_iso())
            log("INFO", "Repack stopped gracefully by time window.")
            return result

        if result != 0:
            put_run_marker("failed", finished_at=utc_iso())
            return result

        put_run_marker("success", created_at=utc_iso())
        log("INFO", "streaming-repack.py completed successfully.")
        return result

    # Не-шардированный путь: здесь нет захвата/пропуска по группам, поэтому
    # действует глобальный interlock — прогон не запускается, пока есть хоть один
    # ожидающий манифест, чтобы не класть вывод поверх недогнанной группы.
    # (В проде работает шардированный путь с per-group interlock.)
    if PENDING_MANIFEST_COUNT > 0:
        log("ERROR",
            f"Refusing non-sharded repack while {PENDING_MANIFEST_COUNT} manifest(s) are pending; "
            "run or repair streaming-move first.")
        put_run_marker("failed", finished_at=utc_iso())
        raise SystemExit(1)

    proxy_prefixes = list_common_prefixes(SRC_BUCKET)
    proxies_total = len(proxy_prefixes)
    send_metric("proxies_total", proxies_total)

    for src_proxy_prefix in proxy_prefixes:
        if not src_proxy_prefix:
            continue

        if not include_proxy(src_proxy_prefix):
            continue

        if should_soft_stop():
            log("INFO", "Soft stop: stop before next proxy.")
            STOP_REQUESTED = True
            break

        if process_proxy(src_proxy_prefix) != 0:
            return 1

        if STOP_REQUESTED:
            break

    if STOP_REQUESTED:
        put_run_marker("partial", stopped_at=utc_iso())
        log("INFO", "Repack stopped gracefully by time window.")
        return 0

    put_run_marker("success", created_at=utc_iso())
    log("INFO", "streaming-repack.py completed successfully.")
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
