# -*- coding: utf-8 -*-
"""
00_audyt_struktury_folderow.py

AUDYT TYLKO DO ODCZYTU.
Nie zmienia danych, nie uruchamia DIAdem, nie wykonuje synchronizacji.

Uruchom z folderu serii, np.:
    py 00_audyt_struktury_folderow.py

Skrypt:
  - przeszukuje folder, w którym sam leży;
  - znajduje każdy katalog o nazwie AE;
  - dla folderu nadrzędnego każdego AE wypisuje:
      * wszystkie specimen.dat,
      * wszystkie *_4000.txt w AE,
      * pierwszy niepusty wiersz każdego *_4000.txt,
      * podstawowe ścieżki i liczby plików;
  - zapisuje pełny raport do Output\\audit_struktury_folderow\\.

Nie kwalifikuje folderów jako „dobre” ani „złe”.
Pokazuje wyłącznie faktyczną strukturę plików.
"""

import csv
import json
import re
from datetime import datetime
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT_DIR / "Output" / "audit_struktury_folderow"

SKIP_DIRECTORY_NAMES = {
    "output",
    "__pycache__",
    ".git",
    ".venv",
    "venv",
}


def now_stamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def should_skip(path):
    try:
        parts = [
            part.casefold()
            for part in path.relative_to(ROOT_DIR).parts
        ]
    except ValueError:
        return True

    return any(part in SKIP_DIRECTORY_NAMES for part in parts)


def relative_or_absolute(path):
    try:
        return str(path.relative_to(ROOT_DIR))
    except ValueError:
        return str(path)


def read_first_nonempty_line(path):
    """
    Odczytuje wyłącznie pierwszy niepusty wiersz, aby pokazać nagłówek.
    """
    encodings = ("utf-8-sig", "utf-8", "cp1250", "latin-1")

    for encoding in encodings:
        try:
            with path.open("r", encoding=encoding, errors="strict") as handle:
                for line in handle:
                    text = line.strip()
                    if text:
                        return text, encoding
            return "", encoding
        except UnicodeDecodeError:
            continue
        except OSError as exc:
            return "<BŁĄD ODCZYTU: {0}>".format(exc), ""

    return "<NIE ROZPOZNANO KODOWANIA>", ""


def all_specimen_files(sample_root):
    return sorted(
        [
            path
            for path in sample_root.rglob("*")
            if path.is_file()
            and path.name.casefold() == "specimen.dat"
            and not should_skip(path)
        ],
        key=lambda path: str(path).casefold(),
    )


def all_4000_files(ae_dir):
    return sorted(
        [
            path
            for path in ae_dir.rglob("*.txt")
            if path.is_file()
            and path.name.casefold().endswith("_4000.txt")
            and not should_skip(path)
        ],
        key=lambda path: str(path).casefold(),
    )


def all_ae_files(ae_dir):
    return sorted(
        [
            path
            for path in ae_dir.rglob("*")
            if path.is_file() and not should_skip(path)
        ],
        key=lambda path: str(path).casefold(),
    )


def inspect_ae_folder(ae_dir):
    sample_root = ae_dir.parent
    specimens = all_specimen_files(sample_root)
    files_4000 = all_4000_files(ae_dir)
    ae_files = all_ae_files(ae_dir)

    txt_records = []
    for path in files_4000:
        header, encoding = read_first_nonempty_line(path)
        txt_records.append(
            {
                "path": relative_or_absolute(path),
                "name": path.name,
                "header": header,
                "encoding": encoding,
                "bytes": path.stat().st_size,
            }
        )

    return {
        "sample_root": relative_or_absolute(sample_root),
        "sample_root_name": sample_root.name,
        "ae_dir": relative_or_absolute(ae_dir),
        "specimen_dat": [
            relative_or_absolute(path)
            for path in specimens
        ],
        "files_4000": txt_records,
        "ae_file_count": len(ae_files),
        "specimen_count": len(specimens),
        "files_4000_count": len(files_4000),
    }


def print_record(index, total, record):
    print("")
    print("=" * 90)
    print("[{0}/{1}] FOLDER NADRZĘDNY AE: {2}".format(
        index,
        total,
        record["sample_root"],
    ))
    print("AE: {0}".format(record["ae_dir"]))
    print("Liczba wszystkich plików w AE: {0}".format(
        record["ae_file_count"],
    ))

    print("specimen.dat ({0}):".format(record["specimen_count"]))
    if record["specimen_dat"]:
        for path in record["specimen_dat"]:
            print("  - {0}".format(path))
    else:
        print("  - BRAK")

    print("*_4000.txt ({0}):".format(record["files_4000_count"]))
    if record["files_4000"]:
        for txt in record["files_4000"]:
            print("  - {0}".format(txt["path"]))
            print("      nagłówek: {0}".format(
                txt["header"] or "<PUSTY>",
            ))
            if txt["encoding"]:
                print("      kodowanie: {0}".format(txt["encoding"]))
            print("      rozmiar: {0} B".format(txt["bytes"]))
    else:
        print("  - BRAK")


def write_text_report(path, records):
    lines = [
        "AUDYT STRUKTURY FOLDERÓW — TYLKO DO ODCZYTU",
        "Data: {0}".format(datetime.now().isoformat(timespec="seconds")),
        "Root: {0}".format(ROOT_DIR),
        "Znalezione foldery AE: {0}".format(len(records)),
        "",
    ]

    for index, record in enumerate(records, start=1):
        lines.extend([
            "=" * 90,
            "[{0}/{1}] FOLDER NADRZĘDNY AE: {2}".format(
                index,
                len(records),
                record["sample_root"],
            ),
            "AE: {0}".format(record["ae_dir"]),
            "Liczba wszystkich plików w AE: {0}".format(
                record["ae_file_count"],
            ),
            "specimen.dat ({0}):".format(record["specimen_count"]),
        ])

        if record["specimen_dat"]:
            lines.extend(
                "  - {0}".format(item)
                for item in record["specimen_dat"]
            )
        else:
            lines.append("  - BRAK")

        lines.append("*_4000.txt ({0}):".format(
            record["files_4000_count"],
        ))

        if record["files_4000"]:
            for txt in record["files_4000"]:
                lines.append("  - {0}".format(txt["path"]))
                lines.append("      nagłówek: {0}".format(
                    txt["header"] or "<PUSTY>",
                ))
                lines.append("      kodowanie: {0}".format(
                    txt["encoding"] or "<nieustalone>",
                ))
                lines.append("      rozmiar: {0} B".format(
                    txt["bytes"],
                ))
        else:
            lines.append("  - BRAK")

        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


def write_csv_report(path, records):
    fields = [
        "sample_root",
        "ae_dir",
        "ae_file_count",
        "specimen_count",
        "specimen_paths",
        "files_4000_count",
        "file_4000_path",
        "file_4000_name",
        "header",
        "encoding",
        "bytes",
    ]

    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fields,
            delimiter=";",
        )
        writer.writeheader()

        for record in records:
            common = {
                "sample_root": record["sample_root"],
                "ae_dir": record["ae_dir"],
                "ae_file_count": record["ae_file_count"],
                "specimen_count": record["specimen_count"],
                "specimen_paths": " | ".join(
                    record["specimen_dat"]
                ),
                "files_4000_count": record["files_4000_count"],
            }

            if not record["files_4000"]:
                writer.writerow(common)
                continue

            for txt in record["files_4000"]:
                row = dict(common)
                row.update(
                    {
                        "file_4000_path": txt["path"],
                        "file_4000_name": txt["name"],
                        "header": txt["header"],
                        "encoding": txt["encoding"],
                        "bytes": txt["bytes"],
                    }
                )
                writer.writerow(row)


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    ae_dirs = sorted(
        [
            path
            for path in ROOT_DIR.rglob("AE")
            if path.is_dir() and not should_skip(path)
        ],
        key=lambda path: str(path).casefold(),
    )

    print("=" * 90)
    print("AUDYT STRUKTURY FOLDERÓW — TYLKO DO ODCZYTU")
    print("Root: {0}".format(ROOT_DIR))
    print("Folderów AE: {0}".format(len(ae_dirs)))
    print("=" * 90)

    records = [
        inspect_ae_folder(ae_dir)
        for ae_dir in ae_dirs
    ]

    for index, record in enumerate(records, start=1):
        print_record(index, len(records), record)

    stamp = now_stamp()
    text_path = OUTPUT_DIR / (
        "audit_struktury_{0}.txt".format(stamp)
    )
    json_path = OUTPUT_DIR / (
        "audit_struktury_{0}.json".format(stamp)
    )
    csv_path = OUTPUT_DIR / (
        "audit_struktury_{0}.csv".format(stamp)
    )

    write_text_report(text_path, records)
    json_path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now().isoformat(
                    timespec="seconds",
                ),
                "root": str(ROOT_DIR),
                "records": records,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    write_csv_report(csv_path, records)

    print("")
    print("=" * 90)
    print("GOTOWE")
    print("TXT:  {0}".format(text_path))
    print("JSON: {0}".format(json_path))
    print("CSV:  {0}".format(csv_path))
    print("=" * 90)


if __name__ == "__main__":
    main()
