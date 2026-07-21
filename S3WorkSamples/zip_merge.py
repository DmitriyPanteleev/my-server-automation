"""Слияние payload'ов для streaming-repack, вынесено для запуска в пуле процессов.

Основная CPU-нагрузка repack — распаковка множества мелких zip-файлов стриминга
и пересжатие их payload'ов в один компактный zip. Эта работа держит GIL, поэтому
streaming-repack выносит merge_payloads() в ProcessPoolExecutor. Модуль импортирует
только stdlib и не держит глобального состояния: метод spawn переимпортирует его в
каждом воркере, и он не должен тянуть boto3 или перезапускать основной скрипт.
"""

import os
import zipfile

CHUNK_SIZE = 1024 * 1024


class BadInputFile(Exception):
    pass


class _MemberReadError(Exception):
    """Поднимается в процессе слияния, когда member не проходит CRC при стриминге."""

    def __init__(self, remote_key, reason):
        super().__init__(reason)
        self.remote_key = remote_key
        self.reason = reason


def _payload_member(path):
    """Вернуть единственный payload-member zip'а стриминга (только метаданные).

    Проверяет central directory и форму «ровно один payload-member» без распаковки;
    CRC проверяется позже при стриминге в вывод. Поднимает BadInputFile для файлов,
    которые нельзя компактить (не zip, пустой или несколько member'ов, например
    мусор __MACOSX/._*).
    """
    try:
        zin = zipfile.ZipFile(path, "r")
    except zipfile.BadZipFile as exc:
        size = os.path.getsize(path) if os.path.exists(path) else -1
        sig = ""
        if os.path.exists(path):
            with open(path, "rb") as file_obj:
                sig = file_obj.read(8).hex()
        raise BadInputFile(f"not a valid zip (size={size} first8_hex={sig})") from exc

    with zin:
        members = [name for name in zin.namelist() if not name.endswith("/")]
        if not members:
            raise BadInputFile("zip has no payload members")
        if len(members) > 1:
            raise BadInputFile(f"zip has multiple payload members: {members}")
        return members[0]


def _write_merge(out_zip, arcname, good, compresslevel):
    """Стримить все исправные member'ы в один компактный member.

    Каждый payload распаковывается ровно один раз; zipfile проверяет CRC member'а
    по мере вычитки потока и поднимает BadZipFile при несовпадении, что мы отдаём
    как _MemberReadError, чтобы вызывающий код выкинул этот ключ и пересобрал заново.
    """
    written_any = False
    last_byte = b""

    with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=compresslevel) as zout:
        # Все payload'ы склеиваются в один member, чей несжатый размер заранее
        # неизвестен. SIZE_LIMIT ограничивает сжатый вход батча, но хорошо
        # сжимаемый текст стриминга после распаковки легко превышает порог ZIP64
        # в 2 ГиБ — поэтому форсируем zip64 явно, иначе zipfile возьмёт 32-битные
        # заголовки и упадёт на закрытии member'а.
        with zout.open(arcname, "w", force_zip64=True) as out:
            for remote_key, path, member in good:
                member_started = False
                try:
                    with zipfile.ZipFile(path, "r") as zin, zin.open(member, "r") as src:
                        while True:
                            chunk = src.read(CHUNK_SIZE)
                            if not chunk:
                                break
                            need_line_separator = (
                                written_any
                                and not member_started
                                and last_byte != b"\n"
                                and chunk[:1] != b"\n"
                            )
                            if need_line_separator:
                                out.write(b"\n")
                            written_any = True
                            member_started = True
                            last_byte = chunk[-1:]
                            out.write(chunk)
                except (zipfile.BadZipFile, OSError) as exc:
                    raise _MemberReadError(remote_key, f"zip integrity check failed: {exc}") from exc

    return written_any


def merge_payloads(out_zip, arcname, inputs, compresslevel):
    """Слить payload'ы переданных inputs в один компактный zip.

    inputs — список кортежей (remote_key, local_path). Файлы, которые нельзя
    компактить, пропускаются (никогда не пишутся частично), их ключи собираются
    с понятной причиной. Возвращает (bad, produced):
      - bad: список (remote_key, reason) для пропущенных файлов
      - produced: True, только если записан хотя бы один непустой payload.

    Битый CRC обнаруживается лишь при стриминге; тогда ключ выкидывается и вывод
    пересобирается с нуля, чтобы загруженный артефакт не был частичным или битым.
    Порча редка, поэтому штатный путь — один проход распаковки.
    """
    bad = {}
    ordered = []

    for remote_key, path in inputs:
        try:
            member = _payload_member(path)
        except BadInputFile as exc:
            bad[remote_key] = str(exc)
            continue
        ordered.append((remote_key, path, member))

    while True:
        good = [item for item in ordered if item[0] not in bad]
        if not good:
            return list(bad.items()), False
        try:
            produced = _write_merge(out_zip, arcname, good, compresslevel)
            return list(bad.items()), produced
        except _MemberReadError as exc:
            bad[exc.remote_key] = exc.reason
