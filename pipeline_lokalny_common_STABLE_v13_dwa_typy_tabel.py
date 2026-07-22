# -*- coding: utf-8 -*-
"""
pipeline_lokalny_common.py  # v13: mała tabela + duża tabela kolumnowa

Wspólne funkcje dla przenośnego pipeline'u jednej próbki:
- 04_zatwierdz_synchronizacje_lokalnie.py
- 05_generuj_DS_lokalnie.py

Plik ma leżeć w tym samym folderze co skrypty 04 i 05.
Folder ten jest jednym przebiegiem pomiarowym, np. 1HEA4K:

1HEA4K\
├── AE\
│   ├── *_4000.txt
│   └── oscy1\
├── <dowolny podfolder z specimen.dat>\
├── 04_zatwierdz_synchronizacje_lokalnie.py
├── 05_generuj_DS_lokalnie.py
└── pipeline_lokalny_common.py
"""

import json
import re
import statistics
import zipfile
from pathlib import Path

PIPELINE_LOKALNY_COMMON_VERSION = "v13_dwa_typy_tabel_geometrii"
from xml.etree import ElementTree as ET


EXCEL_CANDIDATE_FILENAMES = [
    "Spis próbek - 03.07.2026.xlsx",
    "SpisProbek08.05.2026.xlsx",
]

EXCEL_PATH = Path(r"C:\Users\Hubert\Desktop\Spis próbek - 03.07.2026.xlsx")


def resolve_excel_path(excel_path=EXCEL_PATH):
    """
    Zwraca faktyczny plik Excela używany do mapowania geometrii.

    V10: preferuje nowszy spis z 03.07.2026, bo mini próbki LN
    21p1sp/21p2sp są w arkuszu „Testy 06.07.26-10.07.26”.
    Stary SpisProbek08.05.2026 zostaje tylko jako fallback.
    """
    excel_path = Path(excel_path)

    candidates = []

    # Najpierw katalog wskazany przez EXCEL_PATH, zwykle Desktop.
    if excel_path.parent:
        for filename in EXCEL_CANDIDATE_FILENAMES:
            candidates.append(excel_path.parent / filename)

    # Potem dokładnie ścieżka podana przez caller/default.
    candidates.append(excel_path)

    seen = set()
    unique_candidates = []
    for path in candidates:
        key = str(path).casefold()
        if key in seen:
            continue
        seen.add(key)
        unique_candidates.append(path)

    for path in unique_candidates:
        if path.exists():
            return path

    # Zostawiamy pierwotną ścieżkę, żeby komunikat błędu był czytelny,
    # jeżeli użytkownik nie ma żadnego z plików.
    return excel_path

NS = {
    "a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pr": "http://schemas.openxmlformats.org/package/2006/relationships",
}

IGNORED_SEARCH_DIRS = {"ae", "output", "__pycache__"}


def normalize_text(value):
    value = "" if value is None else str(value)
    value = value.strip().casefold()
    replacements = {
        "ą": "a", "ć": "c", "ę": "e", "ł": "l",
        "ń": "n", "ó": "o", "ś": "s", "ż": "z", "ź": "z",
    }
    for old, new in replacements.items():
        value = value.replace(old, new)
    return re.sub(r"\s+", " ", value)


def compact(value):
    return re.sub(r"[^a-z0-9]+", "", normalize_text(value))


def to_float(value):
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return None


def json_number(value):
    if value is None:
        return None
    return float(value)


def col_to_num(column):
    number = 0
    for char in column.upper():
        if char.isalpha():
            number = number * 26 + (ord(char) - ord("A") + 1)
    return number


def num_to_col(number):
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(ord("A") + remainder) + result
    return result


def split_cell_ref(reference):
    match = re.fullmatch(r"([A-Z]+)(\d+)", reference.upper())
    if not match:
        raise ValueError("Nieprawidłowy adres komórki: {0}".format(reference))
    return match.group(1), int(match.group(2))


def row_cell(cells, row, column_number):
    return cells.get("{0}{1}".format(num_to_col(column_number), row), "")


def read_shared_strings(workbook_zip):
    try:
        xml = ET.fromstring(workbook_zip.read("xl/sharedStrings.xml"))
    except KeyError:
        return []

    values = []
    for item in xml.findall("a:si", NS):
        values.append(
            "".join(node.text or "" for node in item.iterfind(".//a:t", NS))
        )
    return values


def get_cell_value(cell, shared_strings):
    cell_type = cell.attrib.get("t")
    value_node = cell.find("a:v", NS)

    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.iterfind(".//a:t", NS))

    if value_node is None:
        return ""

    value = value_node.text or ""
    if cell_type == "s":
        return shared_strings[int(value)]
    return value


def get_workbook_sheets(workbook_zip):
    workbook_xml = ET.fromstring(workbook_zip.read("xl/workbook.xml"))
    relationships_xml = ET.fromstring(
        workbook_zip.read("xl/_rels/workbook.xml.rels")
    )

    id_to_target = {
        relation.attrib["Id"]: relation.attrib["Target"]
        for relation in relationships_xml.findall("pr:Relationship", NS)
    }

    result = {}
    for sheet in workbook_xml.find("a:sheets", NS):
        relation_id = sheet.attrib[
            "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
        ]
        result[sheet.attrib["name"]] = id_to_target[relation_id]
    return result


def load_sheet_cells(workbook_zip, sheet_target, shared_strings):
    xml = ET.fromstring(workbook_zip.read("xl/" + sheet_target))
    cells = {}
    for cell in xml.findall(".//a:sheetData/a:row/a:c", NS):
        cells[cell.attrib["r"]] = get_cell_value(cell, shared_strings)
    return cells


def load_workbook_cells(excel_path):
    if not excel_path.is_file():
        raise RuntimeError(
            "Nie znaleziono Excela:\n{0}".format(excel_path)
        )

    with zipfile.ZipFile(excel_path, "r") as workbook_zip:
        shared_strings = read_shared_strings(workbook_zip)
        sheets = get_workbook_sheets(workbook_zip)
        return {
            sheet_name: load_sheet_cells(
                workbook_zip,
                sheet_target,
                shared_strings,
            )
            for sheet_name, sheet_target in sheets.items()
        }


def first_numeric_right(cells, row, column_number, max_offset=6):
    """
    W bloku geometrii wartość „przed” jest zwykle w pierwszej komórce
    na prawo od etykiety. Fallback przeszukuje kilka kolejnych komórek.
    """
    for offset in range(1, max_offset + 1):
        value = row_cell(cells, row, column_number + offset)
        numeric = to_float(value)
        if numeric is not None:
            return numeric
    return None


def _cell_ref(column_number, row):
    return "{0}{1}".format(num_to_col(column_number), row)


def _nonempty_left(cells, row, column_number, max_left=5):
    for column in range(column_number - 1, max(0, column_number - max_left - 1), -1):
        value = str(row_cell(cells, row, column)).strip()
        if value:
            return value, _cell_ref(column, row)
    return "", ""


def _nonempty_above(cells, row, column_number, max_up=5):
    for candidate_row in range(row - 1, max(0, row - max_up - 1), -1):
        value = str(row_cell(cells, candidate_row, column_number)).strip()
        if value:
            return value, _cell_ref(column_number, candidate_row)
    return "", ""


def _safe_average(values):
    if not values:
        return None
    return sum(values) / float(len(values))


def _representative_large_table_dimensions(pairs):
    """
    W dużych tabelach RRI/RR bywają jednocześnie pomiary szerokich końców
    próbki (~19 mm) i właściwego przewężenia/części pomiarowej (~4 mm).

    Do naprężenia bierzemy reprezentatywny przekrój części wąskiej:
      - zbieramy tylko kompletne pary szerokość+grubość z części "przed",
      - jeżeli są wartości szerokości znacząco większe od mediany, odrzucamy je,
      - gdy próg niczego sensownie nie odrzuca, zostawiamy wszystkie pary.

    To zachowuje poprawne zachowanie dla próbek bez szerokich barków.
    """
    pairs = [
        (float(width), float(thickness), int(row))
        for width, thickness, row in pairs
        if width is not None and thickness is not None
        and float(width) > 0.0 and float(thickness) > 0.0
    ]
    if not pairs:
        return None

    widths = [item[0] for item in pairs]
    median_width = statistics.median(widths)

    selected = [
        item for item in pairs
        if item[0] <= median_width * 1.50
    ]

    if len(selected) < 2 and len(pairs) >= 2:
        selected = pairs

    width = _safe_average([item[0] for item in selected])
    thickness = _safe_average([item[1] for item in selected])

    return {
        "width_before_mm": width,
        "thickness_before_mm": thickness,
        "rows_used": [item[2] for item in selected],
        "rows_all": [item[2] for item in pairs],
        "median_width_mm": median_width,
        "selection_rule": (
            "duża tabela: średnia z kompletnych par szerokość+grubość; "
            "odrzucono szerokości > 1.5 × mediana szerokości"
        ),
    }


def _detect_large_measurement_tables(sheet_name, cells):
    """
    Obsługuje układ kolumnowy typu:

        RRI25_45_5
        RR4K316L45_01   przed rozciąganiem
            szerokość    grubość    odległość ...

    albo:

        RRI25_45_5
        przed rozciąganiem
        szerokość    grubość ...

    Wartości są POD nagłówkami kolumn, nie po prawej stronie etykiet.
    """
    blocks = []

    for before_ref, before_value in cells.items():
        before_key = compact(before_value)
        if before_key not in {
            "przed",
            "przedtestem",
            "przedrozciaganiem",
            "przedrozciaganie",
        }:
            continue

        before_column_letters, before_row = split_cell_ref(before_ref)
        before_column = col_to_num(before_column_letters)

        header_row = None
        width_column = None
        thickness_column = None
        gauge_column = None

        for candidate_row in range(before_row, before_row + 4):
            for column in range(before_column, before_column + 8):
                label = compact(row_cell(cells, candidate_row, column))
                if label == "szerokosc":
                    for maybe_thickness_column in range(
                        column + 1,
                        min(column + 5, before_column + 9),
                    ):
                        if compact(row_cell(cells, candidate_row, maybe_thickness_column)) == "grubosc":
                            header_row = candidate_row
                            width_column = column
                            thickness_column = maybe_thickness_column
                            break
                if header_row is not None:
                    break
            if header_row is not None:
                break

        if header_row is None:
            continue

        for column in range(width_column, width_column + 8):
            if compact(row_cell(cells, header_row, column)) in {"odleglosc", "odcinekpomiarowy"}:
                gauge_column = column
                break

        sample_label, sample_ref = _nonempty_left(
            cells,
            before_row,
            before_column,
            max_left=5,
        )
        group_label = ""

        if not sample_label:
            sample_label, sample_ref = _nonempty_above(
                cells,
                before_row,
                before_column,
                max_up=5,
            )
        else:
            group_label, _ = _nonempty_above(
                cells,
                before_row,
                before_column,
                max_up=3,
            )

        sample_key = compact(sample_label)
        if not sample_key or not re.search(r"\d", sample_key):
            continue
        if sample_key in {
            "grubosc",
            "szerokosc",
            "odleglosc",
            "odcinekpomiarowy",
            "przed",
            "po",
            "uwagi",
        }:
            continue

        pairs = []
        gauge_values = []

        for row in range(header_row + 1, header_row + 35):
            window_values = [
                str(row_cell(cells, row, column)).strip()
                for column in range(width_column - 2, width_column + 8)
            ]
            row_has_any = any(window_values)

            # Duże tabele są rozdzielone pustym wierszem. Pierwszy pusty wiersz
            # po zebraniu danych kończy blok, żeby nie wciągać następnej próbki.
            if not row_has_any:
                if pairs:
                    break
                continue

            row_text_key = compact(" ".join(window_values))
            if pairs and (
                "przedrozciaganiem" in row_text_key
                or "przedtestem" in row_text_key
                or "porozciaganiu" in row_text_key
            ):
                break

            width = to_float(row_cell(cells, row, width_column))
            thickness = to_float(row_cell(cells, row, thickness_column))

            if width is not None and thickness is not None:
                pairs.append((width, thickness, row))

            if gauge_column is not None:
                gauge = to_float(row_cell(cells, row, gauge_column))
                if gauge is not None and gauge > 0:
                    gauge_values.append(gauge)

        representative = _representative_large_table_dimensions(pairs)
        if representative is None:
            continue

        gauge_length = (
            statistics.median(gauge_values)
            if gauge_values
            else None
        )

        blocks.append(
            {
                "sample_label": sample_label,
                "sample_key": sample_key,
                "sheet": sheet_name,
                "header_cell": sample_ref or before_ref,
                "thickness_before_mm": representative["thickness_before_mm"],
                "width_before_mm": representative["width_before_mm"],
                "gauge_length_before_mm": gauge_length,
                "geometry_table_type": "large_column_table",
                "geometry_rows_used": representative["rows_used"],
                "geometry_rows_all": representative["rows_all"],
                "geometry_selection_rule": representative["selection_rule"],
                "geometry_group_label": group_label,
                "geometry_before_cell": before_ref,
                "geometry_header_row": header_row,
                "geometry_width_column": num_to_col(width_column),
                "geometry_thickness_column": num_to_col(thickness_column),
            }
        )

    return blocks


def detect_geometry_blocks(workbook_sheets):
    """
    Rozpoznaje dwa typy bloków geometrii:

    1) Mały/poprzedni:
       <etykieta próbki>
       ...
       grubość / szerokość / odcinek pomiarowy
       gdzie wartości leżą po prawej stronie etykiet.

    2) Duży/kolumnowy:
       <etykieta próbki>    przed rozciąganiem
           szerokość    grubość    odległość ...
       gdzie wartości leżą pod nagłówkami kolumn.
    """
    blocks = []

    for sheet_name, cells in workbook_sheets.items():
        blocks.extend(_detect_large_measurement_tables(sheet_name, cells))

        for header_ref, header_value in cells.items():
            header_text = str(header_value).strip()
            header_key = compact(header_text)

            if not re.search(r"\d", header_key) or len(header_key) < 4:
                continue
            if header_key in {
                "grubosc", "szerokosc", "odcinekpomiarowy",
                "dlugosccalkowita", "odleglosc",
                "przed", "po", "uwagi",
            }:
                continue

            header_column, header_row = split_cell_ref(header_ref)
            start_column = col_to_num(header_column)

            found = {}

            scan_end_row = header_row + 18
            for next_row in range(header_row + 1, header_row + 19):
                next_key = compact(
                    row_cell(cells, next_row, start_column)
                )
                if (
                    re.search(r"\d", next_key)
                    and len(next_key) >= 4
                    and next_key not in {
                        "grubosc",
                        "szerokosc",
                        "odcinekpomiarowy",
                        "dlugosccalkowita",
                        "odleglosc",
                        "przed",
                        "po",
                        "uwagi",
                    }
                ):
                    scan_end_row = next_row - 1
                    break

            for row in range(header_row + 1, scan_end_row + 1):
                for column in range(start_column, start_column + 8):
                    label = compact(row_cell(cells, row, column))

                    if (
                        label == "grubosc"
                        and "thickness_before_mm" not in found
                    ):
                        found["thickness_before_mm"] = first_numeric_right(
                            cells, row, column
                        )
                    elif (
                        label == "szerokosc"
                        and "width_before_mm" not in found
                    ):
                        found["width_before_mm"] = first_numeric_right(
                            cells, row, column
                        )
                    elif (
                        label in {"odcinekpomiarowy", "odleglosc"}
                        and "gauge_length_before_mm" not in found
                    ):
                        found["gauge_length_before_mm"] = first_numeric_right(
                            cells, row, column
                        )

            if (
                found.get("thickness_before_mm") is not None
                and found.get("width_before_mm") is not None
            ):
                blocks.append(
                    {
                        "sample_label": header_text,
                        "sample_key": header_key,
                        "sheet": sheet_name,
                        "header_cell": header_ref,
                        "thickness_before_mm": found["thickness_before_mm"],
                        "width_before_mm": found["width_before_mm"],
                        "gauge_length_before_mm": found.get(
                            "gauge_length_before_mm"
                        ),
                        "geometry_table_type": "small_label_value_table",
                    }
                )

    if not blocks:
        raise RuntimeError(
            "Nie znaleziono żadnego bloku geometrii z polami "
            "„grubość” i „szerokość” w Excelu."
        )

    unique = {}
    for block in blocks:
        key = (
            block["sheet"],
            block["header_cell"],
            block["sample_label"],
            block["geometry_table_type"],
        )
        unique[key] = block

    return list(unique.values())


def aliases_for_code(value):
    """
    Tworzy warianty kodu do bezpiecznego dopasowania:
      1HLP316LLN -> 1HLP316L
      1_HLP_316L_26 -> 1HLP316L26 / 1HLP316L
      1HEA4K -> 1HEA4K
    """
    key = compact(value)
    aliases = {key}

    if key.endswith("ln"):
        aliases.add(key[:-2])

    if key.endswith("26"):
        aliases.add(key[:-2])

    # Niektóre serie mają robocze H po numerze folderu, którego może nie
    # być w etykiecie geometrycznej. To jest wyłącznie dodatkowy wariant.
    match = re.fullmatch(r"(\d+)h(.+)", key)
    if match:
        aliases.add(match.group(1) + match.group(2))

    # Warianty po ponownym skróceniu.
    for alias in list(aliases):
        if alias.endswith("26"):
            aliases.add(alias[:-2])

    return {item for item in aliases if item}


def geometry_key_variants(value):
    """
    Warianty do ostatniego etapu dopasowania.

    Zasada bazowa:
      - ignorujemy wielkość liter,
      - ignorujemy znaki specjalne,
      - zachowujemy kolejność znaków alfanumerycznych.

    Warianty robocze są dopiero fallbackiem:
      - mini <-> min,
      - 316LN / 316L / 316.
    """
    base = compact(value)
    variants = {base}

    changed = True
    while changed:
        changed = False
        for key in list(variants):
            new_items = {
                key.replace("mini", "min"),
                key.replace("316ln", "316l"),
                key.replace("316ln", "316"),
                key.replace("316l", "316"),
            }
            for item in new_items:
                if item and item not in variants:
                    variants.add(item)
                    changed = True

    return {item for item in variants if item}


def keys_match_by_variants(left, right):
    return bool(geometry_key_variants(left).intersection(
        geometry_key_variants(right)
    ))


def geometry_result(local_root, specimen_path, excel_path, block, mapping_source):
    thickness = block["thickness_before_mm"]
    width = block["width_before_mm"]
    area = thickness * width

    result = {
        "folder_id": local_root.name,
        "sample_label": block["sample_label"],
        "excel_path": str(excel_path),
        "sheet": block["sheet"],
        "header_cell": block["header_cell"],
        "mapping_source": mapping_source,
        "thickness_before_mm": thickness,
        "width_before_mm": width,
        "gauge_length_before_mm": block["gauge_length_before_mm"],
        "cross_section_before_mm2": area,
    }

    for optional_key in (
        "geometry_table_type",
        "geometry_rows_used",
        "geometry_rows_all",
        "geometry_selection_rule",
        "geometry_group_label",
        "geometry_before_cell",
        "geometry_header_row",
        "geometry_width_column",
        "geometry_thickness_column",
    ):
        if optional_key in block:
            result[optional_key] = block[optional_key]

    return result


def choose_unique_geometry_match(matches, source_name, source_description):
    unique = {}
    for block, mapping_source in matches:
        unique[(block["sheet"], block["header_cell"])] = (
            block,
            mapping_source,
        )

    if len(unique) == 1:
        return next(iter(unique.values()))

    if len(unique) > 1:
        options = [
            "{0}!{1}: {2}".format(
                block["sheet"],
                block["header_cell"],
                block["sample_label"],
            )
            for block, _ in unique.values()
        ]
        raise RuntimeError(
            "Niejednoznaczne dopasowanie geometrii dla {0} „{1}”:\n{2}"
            .format(
                source_description,
                source_name,
                "\n".join(options),
            )
        )

    return None, None


def max_row(cells):
    result = 0
    for reference in cells:
        _, row = split_cell_ref(reference)
        result = max(result, row)
    return result


def row_values(cells, row, max_column=20):
    return [
        row_cell(cells, row, column).strip()
        for column in range(1, max_column + 1)
        if str(row_cell(cells, row, column)).strip()
    ]


def resolve_geometry(local_root, specimen_path, excel_path=EXCEL_PATH):
    """
    Mapowanie lokalnego folderu do geometrii.

    V11 — hierarchia:
      1) exact match katalogu ze specimen.dat do etykiety geometrii,
      2) nazwa katalogu nadrzędnego specimen.dat, ignorując wielkość liter
         i znaki specjalne, ale zachowując kolejność znaków,
      3) dopiero potem warianty robocze: mini/min, 316LN/316L/316,
         mapowanie przez wiersze Excela i stare aliasy.

    Wynik jest zapisany również lokalnie do Output\geometria_uzyta.json.
    """
    excel_path = resolve_excel_path(excel_path)
    workbook_sheets = load_workbook_cells(excel_path)
    blocks = detect_geometry_blocks(workbook_sheets)
    by_key = {}
    for block in blocks:
        by_key.setdefault(block["sample_key"], []).append(block)

    local_root_name = local_root.name
    specimen_parent_name = specimen_path.parent.name

    local_root_key = compact(local_root_name)
    specimen_parent_key = compact(specimen_parent_name)

    # 1A. Exact text match — różni się najwyżej wielkością liter.
    # To musi wygrać przed compact-match, bo Excel może zawierać obok siebie:
    #   "1.2_Min_P2_316LN_77"
    #   "12minp2316ln77"
    # Po usunięciu znaków specjalnych oba dają ten sam klucz, ale pełna
    # nazwa katalogu specimen.dat jednoznacznie wskazuje pierwszy blok.
    for source_name, source_description in [
        (
            specimen_parent_name,
            "nazwy katalogu nadrzędnego specimen.dat",
        ),
        (
            local_root_name,
            "nazwy folderu próbki",
        ),
    ]:
        source_text = str(source_name).strip().casefold()
        exact_text_matches = [
            (
                block,
                "exact text match: {0} „{1}”".format(
                    source_description,
                    source_name,
                ),
            )
            for block in blocks
            if str(block["sample_label"]).strip().casefold() == source_text
        ]
        block, mapping_source = choose_unique_geometry_match(
            exact_text_matches,
            source_name,
            source_description,
        )
        if block is not None:
            return geometry_result(
                local_root,
                specimen_path,
                excel_path,
                block,
                mapping_source,
            )

    # 1B. Compact exact match — ignoruje znaki specjalne, zachowuje kolejność.
    for source_name, source_key, source_description in [
        (
            specimen_parent_name,
            specimen_parent_key,
            "znormalizowanej nazwy katalogu nadrzędnego specimen.dat",
        ),
        (
            local_root_name,
            local_root_key,
            "znormalizowanej nazwy folderu próbki",
        ),
    ]:
        exact_matches = [
            (
                block,
                "compact exact match: {0} „{1}”".format(
                    source_description,
                    source_name,
                ),
            )
            for block in blocks
            if block["sample_key"] == source_key
        ]
        block, mapping_source = choose_unique_geometry_match(
            exact_matches,
            source_name,
            source_description,
        )
        if block is not None:
            return geometry_result(
                local_root,
                specimen_path,
                excel_path,
                block,
                mapping_source,
            )

    # 2. Nazwa katalogu nadrzędnego do specimen.dat z normalizacją:
    #    case-insensitive + bez znaków specjalnych + zachowana kolejność znaków.
    #    Tu wchodzą np. różnice Min/Mini albo 316LN/316L, ale tylko dla
    #    katalogu mechanicznego, więc jest to nadal mocniejsze od folderu AE.
    parent_variant_matches = [
        (
            block,
            "dopasowanie po katalogu nadrzędnym specimen.dat: "
            "„{0}” -> „{1}”".format(
                specimen_parent_name,
                block["sample_label"],
            ),
        )
        for block in blocks
        if keys_match_by_variants(specimen_parent_name, block["sample_label"])
    ]
    block, mapping_source = choose_unique_geometry_match(
        parent_variant_matches,
        specimen_parent_name,
        "znormalizowanej nazwy katalogu nadrzędnego specimen.dat",
    )
    if block is not None:
        return geometry_result(
            local_root,
            specimen_path,
            excel_path,
            block,
            mapping_source,
        )

    # 3. Dopiero teraz warianty robocze i stara logika mapowania.
    code_sources = [
        local_root_name,
        specimen_parent_name,
    ]
    aliases = set()
    for source in code_sources:
        aliases.update(aliases_for_code(source))
        aliases.update(geometry_key_variants(source))

    candidates = []

    # Mapowanie przez wiersz arkusza: kod akwizycji i etykieta geometrii
    # w tym samym wierszu, najczęściej w arkuszu „Lista próbek”.
    for sheet_name, cells in workbook_sheets.items():
        for row in range(1, max_row(cells) + 1):
            values = row_values(cells, row)
            value_keys = [compact(value) for value in values]

            code_hit = any(key in aliases for key in value_keys)
            if not code_hit:
                continue

            for value, value_key in zip(values, value_keys):
                if value_key in by_key:
                    for block in by_key[value_key]:
                        candidates.append(
                            (
                                300,
                                block,
                                "mapowanie przez wiersz arkusza „{0}”, "
                                "wiersz {1}; folder={2}; specimen_parent={3}"
                                .format(
                                    sheet_name,
                                    row,
                                    local_root_name,
                                    specimen_parent_name,
                                ),
                            )
                        )

    # Bezpośredni wariant folderu/podfolderu do etykiety geometrii.
    for block in blocks:
        block_aliases = set()
        block_aliases.update(aliases_for_code(block["sample_label"]))
        block_aliases.update(geometry_key_variants(block["sample_label"]))

        if aliases.intersection(block_aliases):
            candidates.append(
                (
                    200,
                    block,
                    "wariant roboczy: folder={0}, specimen_parent={1}, "
                    "etykieta={2}".format(
                        local_root_name,
                        specimen_parent_name,
                        block["sample_label"],
                    ),
                )
            )

    if not candidates:
        raise RuntimeError(
            "Nie udało się dopasować geometrii.\n"
            "Wersja pipeline_lokalny_common: {0}\n"
            "Folder próbki: {1}\n"
            "Katalog nadrzędny specimen.dat: {2}\n"
            "Klucz folderu próbki: {3}\n"
            "Klucz katalogu specimen.dat: {4}\n"
            "Warianty fallback: {5}\n"
            "Excel użyty realnie: {6}".format(
                PIPELINE_LOKALNY_COMMON_VERSION,
                local_root_name,
                specimen_parent_name,
                local_root_key,
                specimen_parent_key,
                ", ".join(sorted(aliases)),
                excel_path,
            )
        )

    candidates.sort(
        key=lambda item: (
            -item[0],
            item[1]["sheet"].casefold(),
            item[1]["header_cell"],
        )
    )

    best_score = candidates[0][0]
    best = [
        candidate for candidate in candidates
        if candidate[0] == best_score
    ]

    unique_blocks = {}
    for _, block, mapping_source in best:
        unique_blocks[
            (block["sheet"], block["header_cell"])
        ] = (block, mapping_source)

    if len(unique_blocks) != 1:
        options = [
            "{0}!{1}: {2}".format(
                block["sheet"],
                block["header_cell"],
                block["sample_label"],
            )
            for block, _ in unique_blocks.values()
        ]
        raise RuntimeError(
            "Niejednoznaczne dopasowanie geometrii po wariantach fallback "
            "dla folderu „{0}” / specimen parent „{1}”:\n{2}".format(
                local_root_name,
                specimen_parent_name,
                "\n".join(options),
            )
        )

    block, mapping_source = next(iter(unique_blocks.values()))
    return geometry_result(
        local_root,
        specimen_path,
        excel_path,
        block,
        mapping_source,
    )


def find_specimen(local_root):
    found = []
    for path in local_root.rglob("specimen.dat"):
        relative_parts = [
            part.casefold() for part in path.relative_to(local_root).parts
        ]
        if any(part in IGNORED_SEARCH_DIRS for part in relative_parts):
            continue
        found.append(path)

    found = sorted(found, key=lambda path: str(path).casefold())

    if len(found) != 1:
        raise RuntimeError(
            "Oczekiwano dokładnie jednego specimen.dat poza AE/Output; "
            "znaleziono {0}: {1}".format(
                len(found),
                [str(path) for path in found],
            )
        )
    return found[0]


def find_ae_txt(local_root):
    ae_dir = local_root / "AE"
    if not ae_dir.is_dir():
        raise RuntimeError("Brak folderu AE: {0}".format(ae_dir))

    found = sorted(
        [
            path for path in ae_dir.rglob("*.txt")
            if path.name.casefold().endswith("_4000.txt")
        ],
        key=lambda path: str(path).casefold(),
    )

    if len(found) != 1:
        raise RuntimeError(
            "Oczekiwano jednego *_4000.txt; znaleziono {0}: {1}".format(
                len(found),
                [str(path) for path in found],
            )
        )
    return found[0]


def find_oscy_dir(local_root):
    ae_dir = local_root / "AE"
    if not ae_dir.is_dir():
        raise RuntimeError("Brak folderu AE: {0}".format(ae_dir))

    found = sorted(
        [
            path for path in ae_dir.iterdir()
            if path.is_dir() and path.name.casefold().startswith("oscy")
        ],
        key=lambda path: path.name.casefold(),
    )

    if len(found) != 1:
        raise RuntimeError(
            "Oczekiwano jednego folderu oscy*; znaleziono {0}: {1}".format(
                len(found),
                [str(path) for path in found],
            )
        )
    return found[0]


def find_tiff(local_root):
    oscy_dir = find_oscy_dir(local_root)
    found = sorted(
        [
            path for path in oscy_dir.iterdir()
            if path.is_file() and path.suffix.casefold() in (".tif", ".tiff")
        ],
        key=lambda path: path.name.casefold(),
    )

    if len(found) != 1:
        raise RuntimeError(
            "Oczekiwano jednego TIFF-a w {0}; znaleziono {1}: {2}".format(
                oscy_dir,
                len(found),
                [path.name for path in found],
            )
        )
    return found[0]


def write_geometry_snapshot(local_root, geometry):
    output_dir = local_root / "Output"
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "geometria_uzyta.json"
    path.write_text(
        json.dumps(geometry, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path
