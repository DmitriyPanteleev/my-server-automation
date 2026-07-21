"""Общие вспомогательные функции для streaming-repack.py и streaming-move.py.

Модуль не зависит от boto3/botocore и импортируется раньше тяжёлых зависимостей,
чтобы скрипты могли понятно сообщить об их отсутствии. Все функции здесь чистые:
без глобального состояния, S3-клиента и блокировок.
"""

import os
from datetime import datetime, timezone


class ConfigError(RuntimeError):
    """Обязательная переменная окружения не задана или задана некорректно.

    Пайплайн не хранит значений по умолчанию: вся конфигурация приходит из
    окружения (её задаёт Helm-чарт). Отсутствующий или пустой обязательный
    параметр — это ошибка конфигурации, поэтому скрипт падает сразу и явно,
    а не «молча» подставляет какое-то значение.
    """


def bootstrap_log(level, message):
    now = datetime.now()
    ts = now.strftime("%Y-%m-%d %H:%M:%S") + f".{now.microsecond // 1000:03d}"
    print(f"[{ts}] [{level}] {message}", flush=True)


def env_str(name):
    """Обязательная строковая переменная окружения (без значения по умолчанию)."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        raise ConfigError(f"Required environment variable {name} is not set.")
    return raw


def env_str_optional(name):
    """Необязательная строка: отсутствие эквивалентно пустой строке.

    Используется только там, где пустое значение — осознанный и валидный
    вариант поведения (например, отсутствие фильтра или пустой лейбл), а не
    «дефолт, привязанный к конкретному окружению».
    """
    return os.environ.get(name, "")


def env_int(name):
    """Обязательная целочисленная переменная окружения (без значения по умолчанию)."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        raise ConfigError(f"Required integer environment variable {name} is not set.")
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"Invalid integer env {name}={raw!r}.") from exc


def env_float(name):
    """Обязательная вещественная переменная окружения (без значения по умолчанию)."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        raise ConfigError(f"Required float environment variable {name} is not set.")
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"Invalid float env {name}={raw!r}.") from exc


def env_bool(name):
    """Обязательный булев флаг (1/0, true/false, yes/no, on/off), без дефолта."""
    raw = env_str(name).strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"Invalid boolean env {name}={raw!r}.")


def utc_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hms(seconds):
    seconds = int(max(0, seconds))
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def format_client_error(exc):
    response = getattr(exc, "response", {}) or {}
    error = response.get("Error", {}) or {}
    code = error.get("Code", "Unknown")
    message = error.get("Message", str(exc))
    return f"{code}: {message}"


def chunked(values, size):
    for idx in range(0, len(values), size):
        yield values[idx:idx + size]


def next_repack_budget(
    prev_budget,
    move_manifests_drained,
    move_drain_seconds,
    move_window_seconds,
    move_window_capped,
    *,
    pending_backlog_manifests=0,
    margin=0.10,
    delta_band=0.10,
    max_step=0.25,
    floor=0,
    ceil=0,
):
    """Адаптивный бюджет repack на прогон, рассчитанный по последнему прогону move.

    Бюджет — сколько МАНИФЕСТОВ repack может произвести за один прогон. Единица —
    манифест (а не файл), потому что реальное узкое место move — это ровно один
    server-side copy-back компактного ~300 МБ zip'а на каждый манифест (~фиксированная
    стоимость, не зависит от числа исходных файлов в манифесте); удаления дёшевы. В
    файлах ёмкость move скачет в разы от прогона к прогону (файлов-на-манифест зависит
    от того, крупно- или мелкофайловые группы попались), из-за чего бюджет-в-файлах
    ложно колебался и храповиком уезжал вниз. В манифестах оценка стабильна.

    Бюджет подбирается под число манифестов, которое move реально успевает разгрести
    за ночное окно, чтобы move не накапливал бэклог. Из оценённой ёмкости вычитается
    текущий долг манифестов (pending_backlog_manifests) — его move обязан разгрести в
    первую очередь: большой долг прижимает бюджет к минимуму в 1 манифест, нулевой
    возвращает полную ёмкость с запасом margin. Бюджет уменьшается быстро (за один
    шаг), а растёт медленно (мёртвая зона delta_band + ограничение шага max_step),
    чтобы не было колебаний. Аргументы агрегированы по всем pod'ам последнего прогона
    move. Возвращает (new_budget:int, reason:str).
    """
    if move_manifests_drained <= 0 or move_window_seconds <= 0:
        return (int(max(0, prev_budget)), "no-signal")

    if move_window_capped:
        # Move выработал всё окно и не успел: считаем ёмкостью то, что он успел.
        capacity = float(move_manifests_drained)
    else:
        rate = move_manifests_drained / max(move_drain_seconds, 1.0)
        capacity = rate * move_window_seconds

    backlog = max(0.0, float(pending_backlog_manifests))
    # Запас под новый вывод = ёмкость move с учётом margin минус текущий долг.
    target = max(0.0, capacity * (1.0 - margin) - backlog)

    if prev_budget <= 0:
        new = target
        reason = "bootstrap"
    elif target <= prev_budget:
        # Снижаем до цели за один шаг (без мёртвой зоны и ограничения шага):
        # производить меньше всегда безопасно, а разгрести бэклог — срочно.
        new = target
        if backlog > 0:
            reason = "shrink-backlog"
        elif move_window_capped:
            reason = "shrink-capped"
        else:
            reason = "shrink"
    else:
        # Растём плавно, когда долга нет и move справляется. Шаг считается как
        # доля от target (а не от prev_budget), иначе бюджет, прижатый к 1 манифесту,
        # никогда бы не восстановился.
        rel = (target - prev_budget) / prev_budget
        if abs(rel) < delta_band:
            new = prev_budget
            reason = "within-band"
        else:
            new = prev_budget + min(target - prev_budget, target * max_step)
            reason = "grow"

    # Есть реальный сигнал о темпе move, поэтому бюджет не должен схлопнуться в 0:
    # ниже по коду 0 означает «без ограничения». Держим >=1, чтобы большой бэклог
    # тормозил repack, а не снимал ограничение вовсе.
    new = max(new, 1.0)
    if floor > 0:
        new = max(new, floor)
    if ceil > 0:
        new = min(new, ceil)
    return (int(round(new)), reason)


def build_metrics_url(pushgateway_url, job, cluster, service, instance=""):
    """Собрать URL grouping-ключа Prometheus Pushgateway из частей.

    pushgateway_url — это host[:port] без схемы; grouping-ключ кодирует
    job/cluster/service, чтобы метрики попали под нужные лейблы. instance
    добавляет лейбл `instance` для каждого воркера, иначе параллельные воркеры
    пишут в одну серию и затирают друг друга. instance должен быть ограниченным
    и переиспользуемым (индекс Job, а не случайное имя pod'а), чтобы Pushgateway
    не копил мёртвые серии.
    """
    host = pushgateway_url.strip().rstrip("/")
    if "://" not in host:
        host = f"http://{host}"
    url = f"{host}/metrics/job/{job}/cluster/{cluster}/service/{service}"
    if instance != "":
        url = f"{url}/instance/{instance}"
    return url
