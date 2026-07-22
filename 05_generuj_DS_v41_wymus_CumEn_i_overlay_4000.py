# --------------------------------------------------------------------
# 05_generuj_DS_v41_wymus_CumEn_i_overlay_4000.py
#
# Uruchamiaj WEWNĄTRZ DIAdem 2021, z panelu SCRIPT.
#
# GENERATOR v14 — KALIBRACJA ODEJMOWANA Z MARGINESEM:
#   1) importuje specimen.dat dla wszystkich zatwierdzonych próbek,
#   2) oblicza Stress [MPa],
#   3) przesuwa WYŁĄCZNIE Time specimen na natywną oś Time AE,
#   4) importuje pełne AE; Cumulative EA counts = pełny cumsum minus baseline kalibracji
#   5) oblicza czas końcowy TIFF-a z PNG segmentów w AE\oscy1,
#   6) tworzy REPORT: jedna zakładka na próbkę,
#   7) zapisuje dane .tdm oraz layout .tdr.
#
# OŚ CZASU:
#   Time AE          = niezmieniony; referencja dla AE i TIFF-a.
#   Time specimen    = Time specimen raw - offset_AE_to_mech_s.
#
# UKŁAD OSI W RAPORCIE:
#   Lewa oś 1        Frequency [kHz], 0–125; fizyczne osadzenie TIFF-a.
#   Lewa oś 2        Stress [MPa].
#   Prawa oś         Cumulative EA counts.
#
# TIFF:
#   Każdy pełny PNG segment = 25 s.
#   Ostatni PNG ma nazwę np.:
#       spektrogram_segment_45_24p4s.png
#   co daje:
#       T_TIFF = 45 * 25 + 24.4 = 1149.4 s
#
# WYMAGANIA:
#   Output\mapowanie_Excel_geometria.json
#   Output\Synchronizacja\sync_parametry_zatwierdzone.json
#
# Domyślnie program WYMAGA zatwierdzonej synchronizacji wszystkich sześciu
# próbek. Jeżeli nie jest kompletna, przerwie pracę bez zmiany Data Portal.
# --------------------------------------------------------------------

import csv
import hashlib
import json
import math
import os
import re
import statistics
import subprocess
import sys
import traceback
from datetime import datetime
from pathlib import Path

if "DIAdem" not in sys.modules:
    raise RuntimeError(
        "Ten skrypt uruchamiaj wyłącznie w DIAdem 2021, z panelu SCRIPT."
    )

from DIAdem import Application as dd


def _resolve_local_root_from_diadem():
    """
    DIAdem 2021 może zwrócić CurrentScriptPath jako pełną ścieżkę pliku
    albo jako folder, w którym ten plik leży. Obsługujemy oba warianty.

    Folder roboczy może być pojedynczą próbką albo folderem nadrzędnym
    zawierającym dowolną liczbę podfolderów-próbek.
    """
    raw = str(dd.CurrentScriptPath).strip().strip('"')
    if not raw:
        raise RuntimeError(
            "DIAdem zwrócił pusty CurrentScriptPath. "
            "Otwórz skrypt z dysku i uruchom go w panelu SCRIPT."
        )

    current = Path(raw)

    if current.is_file():
        root = current.parent
    elif current.is_dir():
        # W Twojej instalacji: CurrentScriptPath = folder z końcowym '\'.
        root = current
    elif current.suffix.casefold() in (".py", ".vbs", ".bas"):
        root = current.parent
    else:
        root = current

    if not root.is_dir():
        raise RuntimeError(
            "Nie udało się ustalić folderu roboczego z CurrentScriptPath:\n"
            "{0}\n"
            "Odczytany root:\n{1}".format(raw, root)
        )

    return root

ROOT_DIR = _resolve_local_root_from_diadem()
sys.path.insert(0, str(ROOT_DIR))

# V40: nie importujemy już pipeline_lokalny_common.py, bo lokalnie bywa
# odtwarzany/cofany przez starszy workflow. Używamy stałej kopii pod inną
# nazwą, leżącej obok tego skryptu.
import importlib.util

STABLE_COMMON_PATH = ROOT_DIR / "pipeline_lokalny_common_STABLE_v13_dwa_typy_tabel.py"
if not STABLE_COMMON_PATH.is_file():
    raise RuntimeError(
        "Brak wymaganego stabilnego modułu common:\n{0}\n\n"
        "Skopiuj do folderu serii plik pipeline_lokalny_common_STABLE_v13_dwa_typy_tabel.py."
        .format(STABLE_COMMON_PATH)
    )

_spec_common = importlib.util.spec_from_file_location(
    "pipeline_lokalny_common_STABLE_v13_dwa_typy_tabel",
    STABLE_COMMON_PATH,
)
_stable_common = importlib.util.module_from_spec(_spec_common)
_spec_common.loader.exec_module(_stable_common)

local_find_ae_txt = _stable_common.find_ae_txt
local_find_specimen = _stable_common.find_specimen
local_resolve_geometry = _stable_common.resolve_geometry
local_write_geometry_snapshot = _stable_common.write_geometry_snapshot

print("STABLE COMMON PATH:", STABLE_COMMON_PATH)
print(
    "STABLE COMMON VERSION:",
    getattr(_stable_common, "PIPELINE_LOKALNY_COMMON_VERSION", "BRAK"),
)


# ========================= KONFIGURACJA ==============================

# ROOT_DIR jest ustawiany automatycznie z dd.CurrentScriptPath powyżej.

# True = bez kompletnej listy APPROVED program nie generuje częściowego DS.
REQUIRE_ALL_SAMPLES_APPROVED = True

# None = wszystkie zatwierdzone próbki.
# Do kontrolnego przebiegu można wpisać np. ["1HLP316LLN"].
TARGET_SAMPLE_IDS = None

# Czy przed importem usunąć dane z bieżącego Data Portal?
CLEAR_DATA_PORTAL_AT_START = True

# Czy utworzyć nowy layout REPORT, usuwając bieżący layout w DIAdem?
REPLACE_CURRENT_REPORT_LAYOUT = True

# Artefakty końcowe.
SAVE_TDM_DATA = True
SAVE_TDR_LAYOUT = True

# Eksport PDF domyślnie wyłączony. Najpierw sprawdzamy TDR w DIAdem.
# Można ustawić True po potwierdzeniu wyglądu zakładek.
EXPORT_EACH_SHEET_TO_PDF = False

# W trybie roboczym błędy TIFF/PNG nie zatrzymują obliczenia Stress i AE.
# Gdy ustawisz True, brak poprawnego TIFF-a albo czasu TIFF-a przerwie próbkę.
STRICT_TIFF_REQUIRED = False

# Stała długość pełnego segmentu PNG spektrogramu.
PNG_SEGMENT_DURATION_S = 25.0

# Zatwierdzony pakiet synchronizacji może objąć tylko część uderzeń
# kalibracyjnych. Po jego końcu doliczamy margines i właśnie z tego punktu
# odczytujemy baseline odejmowany od całej krzywej.
CALIBRATION_TAIL_MARGIN_S = 3.0

# Skala pionowa TIFF-a.
FREQUENCY_MIN_KHZ = 0.0
FREQUENCY_MAX_KHZ = 125.0

# V37:
# Baseline subtraction zostaje jako poprawna definicja raportowa.
# Cumulative EA energy = krzywa skumulowana z *_4000.txt minus baseline
# z okolic synchronizacji.
SUBTRACT_AE_BASELINE_FOR_REPORT = True

# Pozycjonowanie trzech osi pionowych w procentach szerokości obszaru wykresu.
# Frequency: lewy brzeg; Stress: druga oś po lewej; Cumulative: prawa.
STRESS_AXIS_OFFSET_LEFT_PERCENT = -6.82
CUMULATIVE_AXIS_OFFSET_RIGHT_PERCENT = 0.0

# STYL RAPORTU
FONT_NAME = "Times New Roman"

# OLE_COLOR w stylu Windows: r + 256*g + 65536*b
COLOR_BLACK = 0
COLOR_NAVY = 8388608  # RGB(0, 0, 128)

# Pozycje podpisów osi Y (przesunięcie poziome / X position).
FREQUENCY_LABEL_X_POSITION = -4.6
STRESS_LABEL_X_POSITION = -6.05

# Prostokąt podpisu próbki przy dolnej krawędzi arkusza.
# Osie zajmują Y=13...88, więc stopka jest całkowicie poza obrazem TIFF.
TITLE_X1 = 18
TITLE_X2 = 86
TITLE_Y1 = 90
TITLE_Y2 = 98

# OSTATNI ARKUSZ: PORÓWNANIE 3×3
GENERATE_COMPARISON_SHEET = True
COMPARISON_SHEET_NAME = "Porównanie"
COMPARISON_COLUMNS = 3
COMPARISON_ROWS = 3
COMPARISON_PANELS_PER_PAGE = COMPARISON_COLUMNS * COMPARISON_ROWS

# Pozycje dziewięciu małych osi 2D na stronie porównawczej (3×3).
# Trzy kolumny wracają do układu porównawczego 3×3.
COMPARISON_LEFT_X = 14.0
COMPARISON_RIGHT_X = 90.0
COMPARISON_TOP_Y1 = 7.0
COMPARISON_TOP_Y2 = 24.5
COMPARISON_MIDDLE_Y1 = 37.0
COMPARISON_MIDDLE_Y2 = 54.5
COMPARISON_BOTTOM_Y1 = 67.0
COMPARISON_BOTTOM_Y2 = 84.5
COMPARISON_COLUMN_GAP = 0.15

# ALTERNATYWNY PIPELINE FILTERED
#
# Jeżeli w ROOT_DIR leży colorOscy.py, generator automatycznie:
#   1) uruchamia go dla każdej próbki w <sample>\AE\oscy1,
#   2) importuje counts_vs_time_0p01s.csv,
#   3) tworzy po standardowej stronie próbki stronę *_filtered.
#
# Jeżeli colorOscy.py nie istnieje, zachowanie 05 pozostaje dokładnie takie
# jak dotąd — tylko standardowe strony i standardowe *_4000.txt.
# FOscy.py jest kompletnym pipeline'em filtered uruchamianym poza DIAdem:
#   AE\oscy1\*.npy
#     -> AE\oscy1-filtered\filtered_segments\
#     -> AE\oscy1-filtered\counts_vs_time_0p01s.csv
#     -> AE\oscy1-filtered\all_segments_filtered_coloured.tif
#
# Nie uruchamiamy osobno filterOscy.py ani colorOscy.py.
FOSCY_SCRIPT_NAME = "FOscy.py"
FILTERED_OUTPUT_DIR_NAME = "oscy1-filtered"
FILTERED_TIFF_NAME = "all_segments_filtered_coloured.tif"
FILTERED_COUNTS_CSV_NAME = "counts_vs_time_0p01s.csv"
FILTERED_SHEET_SUFFIX = "_filtered"
FILTERED_TITLE_SUFFIX = " | FILTERED"
FILTERED_LOG_SUBDIR = "FOscy"

# None = najpierw próbujemy Windows Python Launcher: py -3.13.
# Jeżeli launcher nie istnieje, fallback to Python DIAdem (sys.executable).
# Można wpisać pełną ścieżkę do konkretnego python.exe, gdyby w przyszłości
# filtrowany pipeline miał działać w osobnym środowisku.
COLOR_OSCY_PYTHON_EXE = None

# V20 NIE URUCHAMIA FOscy.py. Skrypt DIAdem wyłącznie odczytuje gotowe:
#   <sample>\AE\oscy1-filtered\all_segments_filtered_coloured.tif
#   <sample>\AE\oscy1-filtered\counts_vs_time_0p01s.csv
#
# Wyniki tworzy zewnętrzny batch:
#   00_uruchom_FOscy_wszystkie_proby.bat
#
# Polityka:
#   - brak gotowych wyników dla wszystkich próbek -> tylko standardowy raport;
#   - komplet wyników dla wszystkich -> standard + filtered;
#   - wyniki tylko dla części -> błąd przed Data Portal, aby nie mieszać serii.
STRICT_FILTERED_PIPELINE = True
FILTERED_BATCH_RUNNER_NAME = "00_batchuj_FOscy_wszystkie_proby_v1.py"
FILTERED_BATCH_WRAPPER_NAME = "00_uruchom_FOscy_wszystkie_proby.bat"

# V22: każdy katalog z AE/ jest osobną próbą, także katalog "- filtered".
# Nie ma już wariantów generowanych z jednego folderu przez FOscy.
TREAT_AE_FOLDERS_AS_INDEPENDENT_SAMPLES = True

# KOLEJNOŚĆ PANELI PORÓWNAŃ
# True: na początku 05 pojawia się okno DIAdem do wyboru kolejności.
# Ta sama kolejność jest użyta przez Porównanie oraz Porównanie_filtered.
ASK_COMPARISON_ORDER = True
COMPARISON_ORDER_FILENAME = "comparison_order.json"
COMPARISON_ORDER_DIALOG_FILENAME = "_comparison_order_dialog.vbs"

# InputBox/VBS potrafi uciąć lub zniekształcić bardzo długą listę.
# Jeśli którykolwiek z poniższych plików istnieje w katalogu głównym serii,
# kolejność paneli zostanie pobrana z pliku TXT zamiast z popupu.
COMPARISON_ORDER_TEXT_FILENAMES = [
    "kolejność próbek.txt",
    "kolejnosc próbek.txt",
    "kolejnosc_probek.txt",
    "kolejnosc_porownania.txt",
    "comparison_order.txt",
]

# Od V28 popup nie jest używany do kolejności porównań.
# Skrypt zawsze wymaga pliku TXT z kolejnością. Gdy go brakuje albo jest
# błędny, generuje pomocniczą nieuporządkowaną listę wykrytych tokenów.
COMPARISON_ORDER_REQUIRED_FILENAME = "kolejność próbek.txt"
COMPARISON_ORDER_UNSORTED_TEMPLATE_FILENAME = (
    "kolejność próbek - WYKRYTE NIEUPORZĄDKOWANE.txt"
)

# Etykiety próbek w małych panelach Porównania.
COMPARISON_PANEL_LABEL_TOP_GAP = 4.7
COMPARISON_PANEL_LABEL_HEIGHT = 3.0

# ======================================================================


OUTPUT_DIR = ROOT_DIR / "Output" / "DS"
GEOMETRY_MANIFEST_PATH = ROOT_DIR / "Output" / "mapowanie_Excel_geometria.json"
SYNC_APPROVALS_PATH = (
    ROOT_DIR / "Output" / "Synchronizacja" / "sync_parametry_zatwierdzone.json"
)

OUTPUT_BASENAME = ROOT_DIR.name + "_DS"
DATA_TDM_PATH = OUTPUT_DIR / (OUTPUT_BASENAME + ".tdm")
LAYOUT_TDR_PATH = OUTPUT_DIR / (OUTPUT_BASENAME + ".tdr")
MANIFEST_DS_PATH = OUTPUT_DIR / (OUTPUT_BASENAME + "_manifest.json")
LOG_CSV_PATH = OUTPUT_DIR / (OUTPUT_BASENAME + "_log.csv")
LOG_TXT_PATH = OUTPUT_DIR / (OUTPUT_BASENAME + "_log.txt")

# Dodatkowy log awaryjny w katalogu głównym, na wypadek gdyby problem dotyczył
# tworzenia Output\DS albo użytkownik szukał logu w folderze skryptu.
EMERGENCY_LOG_PATH = ROOT_DIR / "05_generuj_DS_AWARIA.txt"



def select_main_ae_4000_local(sample_root):
    """
    Wybiera główny plik AE danego folderu-próbki.

    V41 — zasada twarda:
      - jeżeli w AE istnieje CumEn_4000.txt, zawsze jest plikiem głównym,
      - wszystkie pozostałe *_4000.txt są overlayami,
      - jeżeli nie ma CumEn_4000.txt, wolno mieć tylko jeden *_4000.txt.
    """
    sample_root = Path(sample_root)
    ae_dir = sample_root / "AE"

    if not ae_dir.is_dir():
        raise RuntimeError("Brak folderu AE: {0}".format(ae_dir))

    candidates = sorted(
        [
            path for path in ae_dir.rglob("*.txt")
            if path.is_file()
            and path.name.casefold().endswith("_4000.txt")
        ],
        key=lambda path: str(path).casefold(),
    )

    if not candidates:
        raise RuntimeError(
            "{0}: nie znaleziono żadnego *_4000.txt w {1}."
            .format(sample_root.name, ae_dir)
        )

    preferred = [
        path for path in candidates
        if path.name.casefold() == "cumen_4000.txt"
    ]

    if len(preferred) == 1:
        return preferred[0]

    if len(preferred) > 1:
        raise RuntimeError(
            "{0}: więcej niż jeden CumEn_4000.txt: {1}".format(
                sample_root.name,
                [str(path) for path in preferred],
            )
        )

    if len(candidates) == 1:
        return candidates[0]

    raise RuntimeError(
        "{0}: nie można wybrać głównego *_4000.txt.\n"
        "Nie ma CumEn_4000.txt, a znaleziono więcej niż jeden plik:\n  {1}\n"
        "Dla wielu plików AE wymagany jest CumEn_4000.txt jako główna krzywa."
        .format(
            sample_root.name,
            "\n  ".join(str(path) for path in candidates),
        )
    )


def assert_main_ae_selection_is_consistent(sample_id, sample_dir, main_ae_path):
    """
    Ochrona przed sytuacją ze zrzutu: folder zawiera CumEn_4000.txt,
    ale Data Portal dostaje Events No. / EA Energy jako główną krzywą.
    """
    all_paths = all_ae_4000_files_local(sample_dir)
    cumen_paths = [
        path for path in all_paths
        if path.name.casefold() == "cumen_4000.txt"
    ]

    if cumen_paths and Path(main_ae_path).name.casefold() != "cumen_4000.txt":
        raise RuntimeError(
            "{0}: znaleziono CumEn_4000.txt, ale jako main AE wybrano {1}.\n"
            "To jest niedozwolone w V41.\n"
            "Wszystkie znalezione *_4000.txt:\n  {2}"
            .format(
                sample_id,
                main_ae_path,
                "\n  ".join(str(path) for path in all_paths),
            )
        )

    return {
        "all_4000": [str(path) for path in all_paths],
        "main_4000": str(main_ae_path),
        "overlay_4000": [
            str(path)
            for path in additional_ae_4000_files_local(sample_dir, main_ae_path)
        ],
    }


def find_optional_cumen6db_4000_local(sample_root):
    """
    Zachowane dla zgodności z wcześniejszymi wersjami, ale od V34 overlaye
    nie są ograniczone do CumEn6dB. Funkcja może być użyta diagnostycznie.
    """
    sample_root = Path(sample_root)
    ae_dir = sample_root / "AE"

    if not ae_dir.is_dir():
        return None

    matches = sorted(
        [
            path for path in ae_dir.rglob("*.txt")
            if path.is_file()
            and path.name.casefold() == "cumen6db_4000.txt"
        ],
        key=lambda path: str(path).casefold(),
    )

    if not matches:
        return None

    if len(matches) > 1:
        raise RuntimeError(
            "{0}: znaleziono więcej niż jeden CumEn6dB_4000.txt: {1}"
            .format(sample_root.name, [str(path) for path in matches])
        )

    return matches[0]


def all_ae_4000_files_local(sample_root):
    """
    Wszystkie pliki *_4000.txt w AE, uporządkowane alfabetycznie.
    """
    sample_root = Path(sample_root)
    ae_dir = sample_root / "AE"

    if not ae_dir.is_dir():
        return []

    return sorted(
        [
            path for path in ae_dir.rglob("*.txt")
            if path.is_file()
            and path.name.casefold().endswith("_4000.txt")
        ],
        key=lambda path: str(path).casefold(),
    )


def additional_ae_4000_files_local(sample_root, main_ae_path):
    """
    Każdy *_4000.txt poza głównym plikiem AE staje się dodatkową krzywą.

    Główny plik AE to nadal:
      - CumEn_4000.txt, gdy istnieje,
      - albo pojedynczy zwykły *_4000.txt w folderach bez CumEn.
    """
    main_resolved = Path(main_ae_path).resolve()

    return [
        path
        for path in all_ae_4000_files_local(sample_root)
        if path.resolve() != main_resolved
    ]


def ae_curve_label_from_path(path):
    """
    Czytelna etykieta metody z nazwy pliku, bez końcówki _4000.
    """
    stem = Path(path).stem
    if stem.casefold().endswith("_4000"):
        stem = stem[:-5]
    return stem or Path(path).stem


def ae_curve_channel_fragment(path, index):
    """
    Krótki, bezpieczny fragment nazwy kanału.
    """
    label = ae_curve_label_from_path(path)
    text = re.sub(r"[^0-9A-Za-z_]+", "_", label).strip("_")
    if not text:
        text = "curve"
    return "{0:02d}_{1}".format(index, text[:30])



def _is_sample_root_local(path):
    path = Path(path)
    if not (path / "AE").is_dir():
        return False
    try:
        local_find_specimen(path)
        select_main_ae_4000_local(path)
        return True
    except Exception:
        return False


def _safe_sample_id_local(root, sample_root):
    root = Path(root).resolve()
    sample_root = Path(sample_root).resolve()
    raw = root.name if sample_root == root else str(sample_root.relative_to(root))
    result = re.sub(r"[^A-Za-z0-9_]+", "_", raw).strip("_")
    return result or sample_root.name


FILTERED_FOLDER_NAME_RE = re.compile(
    r"(?:\s*[-_]\s*filtered|\s+filtered)$",
    re.IGNORECASE,
)


def base_folder_name_for_geometry(folder_name):
    return FILTERED_FOLDER_NAME_RE.sub("", str(folder_name)).strip()


def resolve_geometry_for_sample(sample_id, meta):
    """
    Dopasowuje geometrię do folderu. Folder "- filtered" może dziedziczyć
    tylko mapping geometrii z folderu bazowego — nadal pozostaje osobną próbą
    dla synchronizacji i REPORT.
    """
    sample_root = meta["sample_root"]

    try:
        return local_resolve_geometry(sample_root, meta["specimen_path"])
    except Exception as original_error:
        base_name = base_folder_name_for_geometry(sample_root.name)
        if base_name == sample_root.name:
            raise

        base_root = sample_root.parent / base_name
        if not base_root.is_dir():
            raise RuntimeError(
                "{0}: nie dopasowano geometrii bezpośrednio ({1}); "
                "nie istnieje folder bazowy: {2}"
                .format(sample_id, original_error, base_root)
            )

        try:
            inherited = local_resolve_geometry(
                base_root,
                local_find_specimen(base_root),
            )
        except Exception as inherited_error:
            raise RuntimeError(
                "{0}: nie dopasowano geometrii bezpośrednio ({1}) ani przez "
                "folder bazowy {2} ({3})."
                .format(
                    sample_id,
                    original_error,
                    base_root,
                    inherited_error,
                )
            )

        geometry = dict(inherited)
        geometry["folder_id"] = sample_root.name
        geometry["geometry_inherited_from_folder"] = str(base_root)
        geometry["mapping_source"] = (
            str(inherited.get("mapping_source", ""))
            + " | geometria odziedziczona przez "
            + base_root.name
        )
        return geometry


def build_sample_index_local(root):
    """
    Własne wyszukiwanie próbek. Nie wymaga build_sample_index z common.py,
    dzięki czemu działa także ze starszym lokalnym common.py.
    """
    root = Path(root).resolve()

    if _is_sample_root_local(root):
        roots = [root]
    else:
        roots = []
        for ae_dir in root.rglob("AE"):
            if not ae_dir.is_dir():
                continue
            parts = [part.casefold() for part in ae_dir.relative_to(root).parts]
            if any(part in {"output", "__pycache__"} for part in parts):
                continue
            candidate = ae_dir.parent
            if _is_sample_root_local(candidate):
                roots.append(candidate.resolve())

        unique = {}
        for item in roots:
            unique[str(item).casefold()] = item
        roots = sorted(unique.values(), key=lambda item: str(item).casefold())

    if not roots:
        raise RuntimeError(
            "Nie znaleziono żadnej próbki. Wymagane są AE/ oraz jeden "
            "specimen.dat poza AE/Output."
        )

    index = {}
    counters = {}
    for sample_root in roots:
        base_id = _safe_sample_id_local(root, sample_root)
        key = base_id.casefold()
        counters[key] = counters.get(key, 0) + 1
        sample_id = base_id if counters[key] == 1 else "{0}_{1}".format(
            base_id, counters[key]
        )
        index[sample_id] = {
            "sample_id": sample_id,
            "sample_root": sample_root,
            "relative_path": (
                "." if sample_root == root
                else str(sample_root.relative_to(root))
            ),
            "specimen_path": local_find_specimen(sample_root),
            "ae_txt_path": select_main_ae_4000_local(sample_root),
        }
    return index


SAMPLE_INDEX = build_sample_index_local(ROOT_DIR)


# ============================ NARZĘDZIA OGÓLNE ===============================

def now_text():
    return datetime.now().isoformat(timespec="seconds")


def to_float(value):
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return None


def read_text_any_encoding(path):
    for encoding in ("utf-8-sig", "utf-8", "cp1250", "cp1252", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            pass
    return path.read_text(encoding="latin-1", errors="replace")


def atomic_write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)


def safe_unlink(path):
    try:
        if path.is_file():
            path.unlink()
    except OSError:
        pass


def discover_samples(root):
    return list(SAMPLE_INDEX.keys())


def find_one_file(root, predicate, label):
    found = sorted(
        [path for path in root.rglob("*") if path.is_file() and predicate(path)],
        key=lambda path: str(path).casefold(),
    )
    if len(found) != 1:
        raise RuntimeError(
            "{0}: oczekiwano dokładnie jednego pliku; znaleziono {1}: {2}".format(
                label,
                len(found),
                [str(path) for path in found],
            )
        )
    return found[0]


def find_specimen(sample_dir):
    return find_one_file(
        sample_dir,
        lambda path: path.name.casefold() == "specimen.dat",
        "specimen.dat dla {0}".format(sample_dir.name),
    )


def find_ae_txt(sample_dir):
    return select_main_ae_4000_local(sample_dir)


def find_oscy1(sample_dir):
    ae_dir = sample_dir / "AE"
    oscy_folders = sorted(
        [
            path for path in ae_dir.iterdir()
            if path.is_dir() and path.name.casefold().startswith("oscy")
        ],
        key=lambda path: path.name.casefold(),
    )

    if len(oscy_folders) != 1:
        raise RuntimeError(
            "Oczekiwano jednego folderu oscy* w {0}; znaleziono {1}: {2}".format(
                ae_dir,
                len(oscy_folders),
                [str(path) for path in oscy_folders],
            )
        )

    return oscy_folders[0]


def preferred_tiff_for_sample(sample_dir, oscy_dir):
    """
    Jedyne dopuszczalne tło spektrogramu:
        <sample>\AE\oscy1\PSD.tiff

    Bez PSD.tif i bez fallbacku do "jedynego TIFF-a w oscy1".
    Ten fallback był niebezpieczny, bo mógł podstawić obraz z inną paletą.
    """
    del sample_dir

    candidate = oscy_dir / "PSD.tiff"
    if candidate.is_file() and candidate.stat().st_size > 0:
        return candidate

    return None


def find_tiff(oscy_dir):
    tiffs = sorted(
        [
            path for path in oscy_dir.iterdir()
            if path.is_file() and path.suffix.casefold() in (".tif", ".tiff")
        ],
        key=lambda path: path.name.casefold(),
    )

    if len(tiffs) != 1:
        raise RuntimeError(
            "Oczekiwano jednego TIFF-a w {0}; znaleziono {1}: {2}".format(
                oscy_dir,
                len(tiffs),
                [path.name for path in tiffs],
            )
        )

    return tiffs[0]


def find_channel(group, channel_name):
    for index in range(1, group.Channels.Count + 1):
        channel = group.Channels(index)
        if channel.Name == channel_name:
            return channel

    raise RuntimeError(
        "Nie znaleziono kanału „{0}” w grupie „{1}”.".format(
            channel_name,
            group.Name,
        )
    )


def set_unit(channel, unit):
    channel.UnitSymbol = unit


def get_float64_channel_type():
    """
    Różne wersje/instalacje DIAdem eksponują stałą typu kanału pod nieco
    inną nazwą. Najpierw próbujemy nazwy używanej zwykle przy kanałach,
    potem starszego/wariantowego aliasu.
    """
    if hasattr(dd, "DataTypeChnFloat64"):
        return dd.DataTypeChnFloat64
    if hasattr(dd, "DataTypeFloat64"):
        return dd.DataTypeFloat64
    raise RuntimeError("Nie znaleziono stałej typu kanału Float64 w DIAdem.")


def set_values_block(channel, values):
    """
    DIAdem/Python bywa wrażliwy na liczbę argumentów SetValuesBlock.
    Próbujemy najpierw prostą sygnaturę, potem wariant z pozycją startową.
    """
    try:
        channel.SetValuesBlock(values)
        return
    except Exception as first_error:
        try:
            channel.SetValuesBlock(values, 1, dd.eValueBlockValueOverwrite)
            return
        except Exception as second_error:
            raise RuntimeError(
                "Nie udało się zapisać wartości do kanału {0}. "
                "SetValuesBlock(values): {1}; "
                "SetValuesBlock(values, 1, overwrite): {2}".format(
                    channel.Name,
                    first_error,
                    second_error,
                )
            )


def add_numeric_channel(group, name, unit, values):
    channel = group.Channels.Add(name, get_float64_channel_type())
    channel.UnitSymbol = unit
    set_values_block(channel, values)
    return channel


# ============================ MANIFESTY WEJŚCIOWE =============================

def load_geometry_manifest():
    """
    Geometria dla każdej znalezionej próbki jest brana bezpośrednio z Excela.
    Dodatkowo zapisujemy lokalny snapshot w folderze każdej próbki.
    """
    result = {}
    for sample_id, meta in SAMPLE_INDEX.items():
        geometry = resolve_geometry_for_sample(sample_id, meta)
        local_write_geometry_snapshot(meta["sample_root"], geometry)
        result[sample_id.casefold()] = geometry
    return result


def load_sync_approvals():
    if not SYNC_APPROVALS_PATH.is_file():
        raise RuntimeError(
            "Brak zatwierdzonej synchronizacji:\n{0}\n"
            "Najpierw uruchom 04_zatwierdz_synchronizacje_uniwersalnie.py.".format(
                SYNC_APPROVALS_PATH
            )
        )

    with SYNC_APPROVALS_PATH.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    samples = data.get("samples", {})
    if not samples:
        raise RuntimeError(
            "Plik synchronizacji nie ma sekcji samples: {0}".format(
                SYNC_APPROVALS_PATH
            )
        )

    return samples


def select_samples(all_samples, approvals):
    if TARGET_SAMPLE_IDS is not None:
        requested = list(TARGET_SAMPLE_IDS)
    else:
        requested = list(all_samples)

    missing_folders = [
        sample_id for sample_id in requested
        if sample_id not in all_samples
    ]
    if missing_folders:
        raise RuntimeError(
            "Nie znaleziono folderów wybranych próbek: {0}".format(
                ", ".join(missing_folders)
            )
        )

    unapproved = []
    approved = []

    for sample_id in requested:
        approval, approval_key = get_approval_for_sample(sample_id, approvals)
        status = approval.get("status", "") if approval is not None else ""
        if status == "APPROVED":
            approved.append(sample_id)
        else:
            unapproved.append(
                "{0} ({1})".format(sample_id, status or "BRAK_DECYZJI")
            )

    if REQUIRE_ALL_SAMPLES_APPROVED and unapproved:
        raise RuntimeError(
            "Nie można wygenerować końcowego DS, ponieważ brak zatwierdzeń:\n"
            "{0}\n\n"
            "Uruchom skrypt 04 i zatwierdź lub przejrzyj te próbki.".format(
                "\n".join(unapproved)
            )
        )

    if not approved:
        raise RuntimeError("Brak próbek ze statusem APPROVED.")

    return approved, unapproved


# ================================ DANE AE ====================================

def _normalise_ae_header(value):
    return re.sub(r"[^a-z0-9]+", "", str(value).casefold())


def _find_baseline_index(ae_times, requested_baseline_time_s):
    baseline_index = None
    epsilon = 1e-9

    for index, time_s in enumerate(ae_times):
        if time_s <= requested_baseline_time_s + epsilon:
            baseline_index = index
        else:
            break

    if baseline_index is None:
        raise RuntimeError(
            "Nie ma wiersza AE przed końcem baseline kalibracji "
            "({0:.6g} s).".format(requested_baseline_time_s)
        )

    return baseline_index


def discrete_cumulative_energy_rate(ae_times, cumulative_energy, ae_path):
    """
    Pochodna wsteczna: (CumEn[i] - CumEn[i-1]) / (t[i] - t[i-1]).
    Przypisana do t[i], więc skok CumEn staje się ostrym pikiem w
    rzeczywistym czasie zdarzenia.
    """
    if len(ae_times) != len(cumulative_energy):
        raise RuntimeError(
            "Niezgodna długość Time AE i CumEn w {0}.".format(ae_path)
        )

    if len(ae_times) < 2:
        raise RuntimeError(
            "Za mało punktów CumEn do obliczenia pochodnej: {0}.".format(
                ae_path
            )
        )

    result = [0.0]
    for index in range(1, len(ae_times)):
        dt = ae_times[index] - ae_times[index - 1]
        if dt <= 0:
            raise RuntimeError(
                "Czas CumEn nie jest rosnący w {0}, wiersz około {1}."
                .format(ae_path, index + 2)
            )

        increment = cumulative_energy[index] - cumulative_energy[index - 1]
        tolerance = max(1e-9, abs(cumulative_energy[index]) * 1e-12)

        if increment < -tolerance:
            raise RuntimeError(
                "CumEn maleje w {0}, między wierszami {1} i {2}: {3:.6g}."
                .format(ae_path, index + 1, index + 2, increment)
            )

        result.append(max(0.0, increment) / dt)

    return result


def parse_ae_4000(ae_path):
    """
    Obsługuje:
      time[s]  eventsNo.  EAenergy [arb.units]
    oraz:
      time[s]  CumEn [arb.units]

    CumEn jest już skumulowane; do wykrywania pików służy wyłącznie
    d(CumEn)/dt, nie ponowne cumsum.
    """
    text = read_text_any_encoding(ae_path)
    lines = text.splitlines()

    if not lines:
        raise RuntimeError("Pusty plik AE: {0}".format(ae_path))

    header = lines[0].strip()
    header_key = _normalise_ae_header(header)

    has_events = (
        "eventsno" in header_key
        or "eventno" in header_key
        or "events" in header_key
    )
    has_cumen = (
        "cumen" in header_key
        or "cumulativeenergy" in header_key
        or "cumenergy" in header_key
    )

    if "time" not in header_key or not (has_events or has_cumen):
        raise RuntimeError(
            "Nieoczekiwany nagłówek AE w {0}: {1}".format(ae_path, header)
        )

    times = []
    col2 = []
    col3 = []
    invalid_rows = []

    for line_number, line in enumerate(lines[1:], start=2):
        if not line.strip():
            continue
        fields = line.split()
        needed = 3 if has_events else 2
        if len(fields) < needed:
            invalid_rows.append(line_number)
            continue
        try:
            times.append(float(fields[0].replace(",", ".")))
            col2.append(float(fields[1].replace(",", ".")))
            if has_events:
                col3.append(float(fields[2].replace(",", ".")))
        except ValueError:
            invalid_rows.append(line_number)

    if invalid_rows:
        raise RuntimeError(
            "Nieprawidłowe wiersze AE, np. {0}.".format(invalid_rows[:10])
        )

    if len(times) < 2:
        raise RuntimeError("Za mało danych w pliku AE: {0}".format(ae_path))

    for index in range(1, len(times)):
        if times[index] <= times[index -1]:
            raise RuntimeError(
                "Czas AE nie jest rosnący w {0}, wiersz około {1}."
                .format(ae_path, index + 2)
            )

    if has_events:
        return {
            "mode": "events",
            "times": times,
            "events": col2,
            "energies": col3,
            "cumulative_source": None,
            "energy_rate": None,
            "report_time_channel": "Time AE counts",
            "report_cumulative_channel": "Cumulative EA counts",
            "report_axis_label": "Cumulative EA counts",
            "report_unit": "counts",
            "source_description": (
                "pełny cumsum(Events No.) minus baseline kalibracji"
            ),
        }

    energy_rate = discrete_cumulative_energy_rate(times, col2, ae_path)
    return {
        "mode": "cumulative_energy",
        "times": times,
        "events": None,
        "energies": col2,
        "cumulative_source": col2,
        "energy_rate": energy_rate,
        "report_time_channel": "Time AE energy",
        "report_cumulative_channel": "Cumulative EA energy",
        "report_axis_label": "Cumulative EA energy [arb.units]",
        "report_unit": "arb.units",
        "source_description": (
            "CumEn minus baseline kalibracji; d(CumEn)/dt służy wyłącznie "
            "do wykrywania pików synchronizacji"
        ),
    }


def cumulative_full(values):
    result = []
    current = 0.0
    for value in values:
        current += value
        result.append(current)
    if not result:
        raise RuntimeError("Brak danych do skumulowania.")
    return result


def baseline_subtracted_series(
    ae_times,
    source_cumulative,
    sync_end_ae_s,
    tail_margin_s,
):
    if len(ae_times) != len(source_cumulative):
        raise RuntimeError(
            "Niezgodna długość Time AE ({0}) i krzywej skumulowanej ({1})."
            .format(len(ae_times), len(source_cumulative))
        )
    if tail_margin_s < 0:
        raise RuntimeError(
            "CALIBRATION_TAIL_MARGIN_S nie może być ujemne: {0}."
            .format(tail_margin_s)
        )

    requested = sync_end_ae_s + tail_margin_s
    baseline_index = _find_baseline_index(ae_times, requested)
    baseline_value = source_cumulative[baseline_index]
    corrected = [value - baseline_value for value in source_cumulative]
    return {
        "cumulative": corrected,
        "baseline_counts": baseline_value,
        "baseline_rows": baseline_index + 1,
        "baseline_time_s": ae_times[baseline_index],
        "requested_baseline_time_s": requested,
    }


def validate_baseline_subtracted_series(
    source_cumulative,
    corrected_cumulative,
    baseline_value,
):
    if len(source_cumulative) != len(corrected_cumulative):
        raise RuntimeError(
            "Niezgodna długość krzywej źródłowej i skorygowanej."
        )
    tolerance = max(
        1e-7,
        max([abs(value) for value in source_cumulative] + [1.0]) * 1e-12,
    )
    for index, raw_value in enumerate(source_cumulative):
        expected = raw_value - baseline_value
        if abs(corrected_cumulative[index] - expected) > tolerance:
            raise RuntimeError(
                "Błąd odejmowania baseline w wierszu {0}: {1} != {2}."
                .format(index + 1, corrected_cumulative[index], expected)
            )


# ============================ CZAS TIFF-A ====================================

LAST_SEGMENT_RE = re.compile(
    r"segment[_\-\s]*(?P<index>\d+)[_\-\s]*(?P<seconds>\d+(?:[p\.,]\d+)?)s",
    re.IGNORECASE,
)


def get_tiff_end_time(oscy_dir):
    """
    Przykład:
      spektrogram_segment_45_24p4s.png

    Indeks 45 oznacza 45 pełnych segmentów po 25 s przed ostatnim:
      45*25 + 24.4 = 1149.4 s.

    Sprawdzamy też, czy liczba PNG ≈ indeks_ostatniego + 1.
    Niezgodność zapisujemy jako ostrzeżenie, ale nie zmieniamy fizycznego
    czasu wynikającego z nazwy końcowego segmentu.
    """
    pngs = sorted(
        [
            path for path in oscy_dir.iterdir()
            if path.is_file() and path.suffix.casefold() == ".png"
        ],
        key=lambda path: path.name.casefold(),
    )

    if not pngs:
        raise RuntimeError("Brak PNG segmentów w {0}.".format(oscy_dir))

    parsed = []
    for path in pngs:
        match = LAST_SEGMENT_RE.search(path.stem)
        if not match:
            continue

        index = int(match.group("index"))
        seconds = float(
            match.group("seconds")
            .replace("p", ".")
            .replace(",", ".")
        )
        parsed.append((index, seconds, path))

    if not parsed:
        raise RuntimeError(
            "Nie znalazłem nazwy segmentu typu "
            "spektrogram_segment_45_24p4s.png w {0}.".format(oscy_dir)
        )

    last_index, last_seconds, last_png = max(parsed, key=lambda item: item[0])

    if last_seconds < 0.0 or last_seconds > PNG_SEGMENT_DURATION_S + 1e-9:
        raise RuntimeError(
            "Czas końcowego segmentu PNG jest poza zakresem 0–{0} s: "
            "{1} ({2}).".format(
                PNG_SEGMENT_DURATION_S,
                last_seconds,
                last_png.name,
            )
        )

    tiff_end_s = last_index * PNG_SEGMENT_DURATION_S + last_seconds

    warnings = []
    expected_png_count = last_index + 1
    if len(pngs) != expected_png_count:
        warnings.append(
            "PNG: znaleziono {0}, ale indeks ostatniego segmentu {1} "
            "sugeruje {2} plików. Czas TIFF-a obliczono z nazwy końcowego "
            "segmentu, nie z liczby plików.".format(
                len(pngs),
                last_index,
                expected_png_count,
            )
        )

    return {
        "tiff_end_s": tiff_end_s,
        "png_count": len(pngs),
        "last_segment_index": last_index,
        "last_segment_seconds": last_seconds,
        "last_png": str(last_png),
        "warnings": warnings,
    }


def resolve_tiff_info(sample_dir, fallback_end_s):
    """
    Zwraca informację o TIFF-ie i czasie TIFF-a.

    Dla wszystkich próbek używa wyłącznie AE\oscy1\PSD.tiff jako tła.

    Wersja robocza nie zatrzymuje obliczeń Stress/AE tylko dlatego, że:
      - brakuje PNG,
      - nazwy PNG nie pasują do wzorca,
      - liczba segmentów jest niespójna.

    Jeżeli można znaleźć TIFF, ale nie da się wyliczyć jego czasu z PNG,
    używamy fallback_end_s = koniec Time AE i zapisujemy ostrzeżenie.
    """
    warnings = []
    oscy_dir = None
    tiff_path = ""

    try:
        oscy_dir = find_oscy1(sample_dir)
    except Exception as exc:
        message = "TIFF/oscy: nie znaleziono folderu oscy*: {0}".format(exc)
        if STRICT_TIFF_REQUIRED:
            raise
        warnings.append(message)
        return {
            "oscy_dir": "",
            "tiff_path": "",
            "tiff_end_s": fallback_end_s,
            "png_count": 0,
            "last_segment_index": "",
            "last_segment_seconds": "",
            "last_png": "",
            "warnings": warnings,
        }

    preferred_tiff = preferred_tiff_for_sample(sample_dir, oscy_dir)

    if preferred_tiff is not None:
        tiff_path = str(preferred_tiff)
    else:
        message = (
            "TIFF: brak wymaganego tła PSD.tiff w {0}. "
            "Nie użyto fallbacku do innych TIFF-ów, aby nie mieszać palet."
            .format(oscy_dir)
        )
        if STRICT_TIFF_REQUIRED:
            raise RuntimeError(message)
        warnings.append(message)
        tiff_path = ""

    try:
        tiff_info = get_tiff_end_time(oscy_dir)
        tiff_info["oscy_dir"] = str(oscy_dir)
        tiff_info["tiff_path"] = tiff_path
        selected_name = Path(tiff_path).name if tiff_path else ""
        selected_message = (
            "TIFF background selected: {0}".format(selected_name)
            if selected_name
            else "TIFF background selected: BRAK"
        )
        tiff_info["warnings"] = (
            warnings
            + [selected_message]
            + tiff_info.get("warnings", [])
        )
        return tiff_info
    except Exception as exc:
        message = (
            "TIFF time: nie udało się policzyć czasu TIFF-a z PNG; "
            "użyto końca Time AE = {0:.6f} s. Szczegół: {1}: {2}"
        ).format(fallback_end_s, type(exc).__name__, exc)

        if STRICT_TIFF_REQUIRED:
            raise

        warnings.append(message)
        return {
            "oscy_dir": str(oscy_dir),
            "tiff_path": tiff_path,
            "tiff_end_s": fallback_end_s,
            "png_count": len([
                path for path in oscy_dir.iterdir()
                if path.is_file() and path.suffix.casefold() == ".png"
            ]),
            "last_segment_index": "",
            "last_segment_seconds": "",
            "last_png": "",
            "warnings": warnings,
        }



# ========================== ALTERNATYWNY PIPELINE FOscy =========================

# V20 jest bezpośrednio uruchamiany wewnątrz DIAdem.
# Globalna flaga jest ustalana w main() WYŁĄCZNIE po sprawdzeniu gotowych
# artefaktów zapisanych wcześniej przez zewnętrzny batch FOscy.
FILTERED_REPORT_ENABLED = False


def filtered_pipeline_enabled():
    """
    V22 nie tworzy wariantów z jednego folderu. Każdy folder z AE jest
    normalną, niezależną próbką, dlatego ta gałąź jest zawsze wyłączona.
    """
    return False


def filtered_output_paths_for_sample(sample_id):
    """
    Lokalizuje output FOscy dla jednej próbki bez uruchamiania procesu.
    """
    sample_dir = SAMPLE_INDEX[sample_id]["sample_root"]
    oscy_dir = find_oscy1(sample_dir)
    ae_dir = oscy_dir.parent
    filtered_dir = ae_dir / FILTERED_OUTPUT_DIR_NAME

    return {
        "sample_dir": sample_dir,
        "oscy_dir": oscy_dir,
        "ae_dir": ae_dir,
        "filtered_dir": filtered_dir,
        "tiff_path": filtered_dir / FILTERED_TIFF_NAME,
        "counts_path": filtered_dir / FILTERED_COUNTS_CSV_NAME,
        "params_path": filtered_dir / "params.json",
    }


def inspect_filtered_outputs(selected_samples):
    """
    Sprawdza kompletność wyłącznie na poziomie plików.

    Nie parsuje jeszcze CSV; to następuje później, tuż przed Data.Root.Clear().
    Dzięki temu popup może już zawierać *_filtered, ale DIAdem nie próbuje
    uruchamiać żadnego zewnętrznego procesu ani otwierać CMD.
    """
    ready = []
    missing_records = []

    for sample_id in selected_samples:
        paths = filtered_output_paths_for_sample(sample_id)

        missing = [
            str(path)
            for path in (paths["tiff_path"], paths["counts_path"])
            if not path.is_file() or path.stat().st_size <= 0
        ]

        if missing:
            missing_records.append(
                {
                    "sample_id": sample_id,
                    "missing": missing,
                }
            )
        else:
            ready.append(sample_id)

    if not ready:
        return {
            "enabled": False,
            "state": "none",
            "missing_records": missing_records,
        }

    if len(ready) == len(selected_samples):
        return {
            "enabled": True,
            "state": "complete",
            "missing_records": [],
        }

    # Częściowa seria jest niejednoznaczna: część paneli miałaby filtered,
    # a część nie. Zatrzymujemy się zanim DIAdem wyczyści Data Portal.
    details = []
    for record in missing_records:
        details.append(
            "{0}:\n  {1}".format(
                record["sample_id"],
                "\n  ".join(record["missing"]),
            )
        )

    raise RuntimeError(
        "Znaleziono wyniki FOscy tylko dla części próbek.\n\n"
        "Najpierw uruchom zewnętrzny batch:\n"
        "  {0}\n\n"
        "Brakujące artefakty:\n{1}".format(
            ROOT_DIR / FILTERED_BATCH_WRAPPER_NAME,
            "\n".join(details),
        )
    )


def _normalise_csv_header(value):
    return re.sub(r"[^a-z0-9]+", "", str(value).casefold())


def _parse_csv_number(value, path, line_number, column_name):
    text = str(value).strip().replace("\ufeff", "")
    if not text:
        raise RuntimeError(
            "{0}, wiersz {1}, kolumna {2}: pusta wartość."
            .format(path, line_number, column_name)
        )

    try:
        return float(text.replace(",", "."))
    except ValueError:
        raise RuntimeError(
            "{0}, wiersz {1}, kolumna {2}: nie jest liczbą: {3!r}"
            .format(path, line_number, column_name, text)
        )


def _detect_csv_dialect(text):
    """
    counts_vs_time_0p01s.csv może być:
      - CSV z przecinkiem,
      - CSV ze średnikiem i przecinkiem dziesiętnym,
      - TSV.

    Wybieramy separator na podstawie nagłówka, bez zgadywania po każdej linii.
    """
    first_line = next(
        (line for line in text.splitlines() if line.strip()),
        "",
    )

    if ";" in first_line:
        return ";"
    if "\t" in first_line:
        return "\t"
    if "," in first_line:
        return ","
    return None


def parse_filtered_counts_csv(csv_path):
    """
    Odczytuje counts_vs_time_0p01s.csv.

    Akceptowane nagłówki są celowo elastyczne, np.:
      time_s,count
      Time [s];Counts
      time,cumulative_counts

    Zwraca:
      times,
      values,
      values_are_cumulative,
      opis kolumn.

    Jeżeli nazwa kolumny zawiera cumulative/cumsum/cum, druga kolumna jest
    traktowana jako już skumulowana. W przeciwnym razie jako liczba zdarzeń
    w kolejnych krokach 0.01 s.
    """
    text = read_text_any_encoding(csv_path)
    lines = [line for line in text.splitlines() if line.strip()]

    if len(lines) < 2:
        raise RuntimeError(
            "Za mało wierszy w filtered counts CSV: {0}".format(csv_path)
        )

    delimiter = _detect_csv_dialect(text)

    if delimiter is None:
        rows = [line.split() for line in lines]
    else:
        rows = list(csv.reader(lines, delimiter=delimiter))

    rows = [
        [str(value).strip().lstrip("\ufeff") for value in row]
        for row in rows
        if row
    ]

    if len(rows) < 2:
        raise RuntimeError(
            "Nie udało się odczytać danych z: {0}".format(csv_path)
        )

    header = rows[0]
    normalised = [_normalise_csv_header(value) for value in header]

    time_index = None
    value_index = None

    for index, name in enumerate(normalised):
        if (
            name.startswith("time")
            or name in ("t", "seconds", "second", "times")
        ):
            time_index = index
            break

    for index, name in enumerate(normalised):
        if (
            "count" in name
            or "event" in name
            or "hit" in name
            or "cumulative" in name
            or "cumsum" in name
        ):
            if index != time_index:
                value_index = index
                break

    # Gdy plik nie ma czytelnego nagłówka, próbujemy klasycznego układu
    # dwóch kolumn. Wtedy pierwszy wiersz zostanie potraktowany jako dane.
    has_header = time_index is not None and value_index is not None

    if not has_header:
        if len(rows[0]) < 2:
            raise RuntimeError(
                "Nie rozpoznano kolumn czasu i zliczeń w {0}. "
                "Nagłówek: {1!r}".format(csv_path, header)
            )
        time_index = 0
        value_index = 1
        data_rows = rows
        time_name = "column_1"
        value_name = "column_2"
    else:
        data_rows = rows[1:]
        time_name = header[time_index]
        value_name = header[value_index]

    values_are_cumulative = any(
        token in normalised[value_index]
        for token in ("cumulative", "cumsum", "cum")
    )

    times = []
    values = []

    for local_index, row in enumerate(
        data_rows,
        start=(2 if has_header else 1),
    ):
        if max(time_index, value_index) >= len(row):
            raise RuntimeError(
                "{0}, wiersz {1}: za mało kolumn."
                .format(csv_path, local_index)
            )

        time_s = _parse_csv_number(
            row[time_index],
            csv_path,
            local_index,
            time_name,
        )
        value = _parse_csv_number(
            row[value_index],
            csv_path,
            local_index,
            value_name,
        )

        times.append(time_s)
        values.append(value)

    if len(times) < 2:
        raise RuntimeError(
            "Za mało punktów w filtered counts CSV: {0}".format(csv_path)
        )

    for index in range(1, len(times)):
        if times[index] <= times[index - 1]:
            raise RuntimeError(
                "{0}: czas nie jest ściśle rosnący między wierszami {1} i {2}: "
                "{3} <= {4}."
                .format(
                    csv_path,
                    index,
                    index + 1,
                    times[index],
                    times[index - 1],
                )
            )

    return {
        "times": times,
        "values": values,
        "values_are_cumulative": values_are_cumulative,
        "time_column": time_name,
        "value_column": value_name,
        "delimiter": delimiter or "whitespace",
    }


def inferred_series_end_time(times):
    """
    counts_vs_time_0p01s opisuje biny czasu; tło TIFF powinno kończyć się
    na końcu ostatniego binu, nie na jego początku.
    """
    if len(times) < 2:
        return times[-1]

    steps = [
        times[index] - times[index - 1]
        for index in range(1, len(times))
    ]
    positive = [step for step in steps if step > 0]

    if not positive:
        return times[-1]

    return times[-1] + statistics.median(positive)


def resolve_filtered_tiff_end_time(filtered_dir, times):
    """
    Preferuje liczbę klatek zapisaną przez colorOscy.py w
    colour_tiff_params.json. Jeżeli liczba klatek odpowiada CSV, końcem
    tła jest t0 + n_klatek * dt. W przeciwnym razie używa końca serii CSV.
    """
    fallback_end = inferred_series_end_time(times)

    # FOscy.py zapisuje params.json. Pozostawiamy również fallback do
    # colour_tiff_params.json dla starszych, już gotowych serii.
    params_path = filtered_dir / "params.json"
    if not params_path.is_file():
        params_path = filtered_dir / "colour_tiff_params.json"

    if not params_path.is_file() or len(times) < 2:
        return fallback_end

    try:
        params = json.loads(params_path.read_text(encoding="utf-8"))
        frame_count = int(params.get("total_time_frames", 0))
        if frame_count <= 0:
            return fallback_end

        step = statistics.median([
            times[index] - times[index - 1]
            for index in range(1, len(times))
        ])

        if frame_count == len(times):
            return times[0] + frame_count * step

        # Tolerujemy różnicę jednej próbki na końcu.
        if abs(frame_count - len(times)) <= 1:
            return max(fallback_end, times[0] + frame_count * step)

        return fallback_end
    except Exception:
        return fallback_end

#=====HELPER - stosuje Offset do próbek * - filtered===================

def base_sync_id_for_sample(sample_id):
    text = str(sample_id)
    suffix = FILTERED_SHEET_SUFFIX
    if text.casefold().endswith(suffix.casefold()):
        return text[:-len(suffix)]
    return text


def get_approval_for_sample(sample_id, approvals):
    candidates = [sample_id]

    base_id = base_sync_id_for_sample(sample_id)
    if base_id != sample_id:
        candidates.append(base_id)

    approvals_casefold = {
        str(key).casefold(): value
        for key, value in approvals.items()
    }

    for candidate in candidates:
        if candidate in approvals:
            return approvals[candidate], candidate

        key = str(candidate).casefold()
        if key in approvals_casefold:
            return approvals_casefold[key], candidate

    return None, sample_id

def prepare_filtered_pipeline_for_sample(sample_id, sample_dir, log_dir):
    """
    Odczytuje istniejące wyniki FOscy. Nie uruchamia FOscy.py ani CMD.
    Parametr log_dir zostaje dla zgodności z wcześniejszą strukturą wywołań.
    """
    del sample_dir
    del log_dir

    paths = filtered_output_paths_for_sample(sample_id)
    filtered_dir = paths["filtered_dir"]
    tiff_path = paths["tiff_path"]
    counts_path = paths["counts_path"]

    missing = [
        str(path)
        for path in (tiff_path, counts_path)
        if not path.is_file() or path.stat().st_size <= 0
    ]
    if missing:
        raise RuntimeError(
            "{0}: brak gotowych wyników FOscy:\n{1}\n\n"
            "Najpierw uruchom zewnętrzny batch:\n{2}".format(
                sample_id,
                "\n".join(missing),
                ROOT_DIR / FILTERED_BATCH_WRAPPER_NAME,
            )
        )

    counts = parse_filtered_counts_csv(counts_path)
    tiff_end_s = resolve_filtered_tiff_end_time(
        filtered_dir,
        counts["times"],
    )

    return {
        "oscy_dir": str(paths["oscy_dir"]),
        "filtered_dir": str(filtered_dir),
        "tiff_path": str(tiff_path),
        "counts_csv_path": str(counts_path),
        "tiff_end_s": tiff_end_s,
        "times": counts["times"],
        "values": counts["values"],
        "values_are_cumulative": counts["values_are_cumulative"],
        "time_column": counts["time_column"],
        "value_column": counts["value_column"],
        "delimiter": counts["delimiter"],
        "foscy_log": "",
    }


def prepare_filtered_pipeline_for_all_samples(selected_samples):
    """
    Odczytuje komplet już istniejących danych filtered.

    Wywoływane przed Data.Root.Clear() i przed Report.NewLayout(), aby błędny
    CSV albo brak TIFF-a nie naruszył aktualnie otwartego REPORT.
    """
    if not filtered_pipeline_enabled():
        return None

    result = {}

    for sample_id in selected_samples:
        sample_dir = SAMPLE_INDEX[sample_id]["sample_root"]

        print("\n[FILTERED READ] {0}".format(sample_id))
        artifact = prepare_filtered_pipeline_for_sample(
            sample_id,
            sample_dir,
            OUTPUT_DIR,
        )
        result[sample_id] = artifact
        print(
            "  TIFF filtered end={0:.3f} s | counts={1} | CSV={2}"
            .format(
                artifact["tiff_end_s"],
                len(artifact["times"]),
                Path(artifact["counts_csv_path"]).name,
            )
        )

    return result


def cumulative_from_filtered_source(
    times,
    values,
    values_are_cumulative,
    sync_end_ae_s,
    tail_margin_s,
):
    """
    Stosuje do filtered CSV tę samą definicję baseline, co do *_4000.txt:
      cumulative - value z ostatniego punktu <= sync_end + margin.

    Jeżeli CSV zawiera surowe count/bin, najpierw powstaje cumsum.
    Jeżeli zawiera już cumulative/cumsum, używamy go bez kolejnego sumowania.
    """
    if len(times) != len(values):
        raise RuntimeError(
            "Filtered CSV: różna liczba czasu ({0}) i zliczeń ({1})."
            .format(len(times), len(values))
        )

    raw_cumulative = (
        list(values)
        if values_are_cumulative
        else cumulative_full(values)
    )

    requested_baseline_time_s = sync_end_ae_s + tail_margin_s
    baseline_index = None

    for index, time_s in enumerate(times):
        if time_s <= requested_baseline_time_s + 1e-9:
            baseline_index = index
        else:
            break

    if baseline_index is None:
        raise RuntimeError(
            "Filtered CSV nie ma punktu przed końcem baseline "
            "({0:.6g} s).".format(requested_baseline_time_s)
        )

    baseline_counts = raw_cumulative[baseline_index]
    corrected = [
        value - baseline_counts
        for value in raw_cumulative
    ]

    return {
        "cumulative": corrected,
        "baseline_counts": baseline_counts,
        "baseline_rows": baseline_index + 1,
        "baseline_time_s": times[baseline_index],
        "requested_baseline_time_s": requested_baseline_time_s,
    }


# ========================= KOLEJNOŚĆ PANELI PORÓWNAŃ ========================

def comparison_order_path():
    return OUTPUT_DIR / COMPARISON_ORDER_FILENAME


def _normalise_order_token(value):
    return re.sub(r"[^a-z0-9]+", "", str(value).casefold())


def comparison_item_token(sample_id, variant):
    """Token popupu: standard = ID, filtered = ID_filtered."""
    return (
        sample_id + FILTERED_SHEET_SUFFIX
        if variant == "filtered"
        else sample_id
    )


def comparison_item_display_label(sample_id, variant):
    """Tekst ponad panelem REPORT."""
    return (
        sample_id + " [filtered]"
        if variant == "filtered"
        else sample_id
    )


def build_available_comparison_items(sample_ids):
    """
    Lista realnych wykresów do porównania, nie tylko lista próbek.

    Gdy FOscy.py istnieje, każda próbka ma dwa niezależne wpisy:
      sample_id
      sample_id_filtered

    Domyślnie: wszystkie standardowe, potem wszystkie filtered. Popup
    pozwala je dowolnie przeplatać.
    """
    items = []
    for sample_id in sample_ids:
        items.append(
            {
                "sample_id": sample_id,
                "variant": "standard",
                "token": comparison_item_token(sample_id, "standard"),
                "display_label": comparison_item_display_label(
                    sample_id,
                    "standard",
                ),
            }
        )

    if filtered_pipeline_enabled():
        for sample_id in sample_ids:
            items.append(
                {
                    "sample_id": sample_id,
                    "variant": "filtered",
                    "token": comparison_item_token(sample_id, "filtered"),
                    "display_label": comparison_item_display_label(
                        sample_id,
                        "filtered",
                    ),
                }
            )

    return items


def comparison_item_catalog(items):
    catalog = {}
    for item in items:
        key = _normalise_order_token(item["token"])
        if key in catalog:
            raise RuntimeError(
                "Niejednoznaczny token panelu: {0}".format(
                    item["token"]
                )
            )
        catalog[key] = item
    return catalog


def validate_comparison_order(order, available_items):
    """
    Waliduje scenariusz paneli porównawczych.

    Od V34 lista nie musi być permutacją. Ten sam token może wystąpić wiele
    razy, np. gdy każdą trójkę paneli otwiera ta sama próbka kontrolna.

    Warunek twardy:
      - każdy wpisany token musi istnieć w katalogu dostępnych paneli.

    Nie wymagamy:
      - użycia każdego dostępnego tokenu,
      - braku powtórek,
      - długości równej liczbie wykrytych próbek.
    """
    catalog = comparison_item_catalog(available_items)
    resolved = []
    unknown = []

    for value in order:
        key = _normalise_order_token(value)
        if key not in catalog:
            unknown.append(str(value))
            continue

        resolved.append(catalog[key]["token"])

    if unknown:
        raise RuntimeError(
            "Niepoprawna kolejność paneli porównawczych — nieznane: "
            + ", ".join(unknown)
        )

    if not resolved:
        raise RuntimeError(
            "Niepoprawna kolejność paneli porównawczych — lista jest pusta."
        )

    return resolved


def sample_ids_from_comparison_order(order, available_items):
    """
    Zwraca unikalne sample_id użyte w kolejności paneli.

    Od V36 ta lista steruje też importem do Data Portal. Dzięki temu Data
    Portal nie zapełnia się próbkami, których nie ma w kolejność próbek.txt.
    Powtórki paneli są zachowane w comparison_order, ale import próbki jest
    wykonywany tylko raz.
    """
    catalog = comparison_item_catalog(available_items)
    result = []
    seen = set()

    for token in order:
        key = _normalise_order_token(token)
        item = catalog[key]
        sample_id = item["sample_id"]

        if sample_id in seen:
            continue

        seen.add(sample_id)
        result.append(sample_id)

    return result


def load_saved_comparison_order(available_items):
    """
    Ładuje pełną poprzednią kolejność paneli.

    Dla pliku z V18 zawierającego wyłącznie standardowe próbki zachowuje
    stary blok standardowy i dopisuje nowy blok *_filtered.
    """
    default_order = [item["token"] for item in available_items]
    path = comparison_order_path()

    if not path.is_file():
        return default_order

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        candidate = payload.get("order", [])

        if not isinstance(candidate, list):
            return default_order

        try:
            return validate_comparison_order(candidate, available_items)
        except RuntimeError:
            pass

        standard_items = [
            item for item in available_items
            if item["variant"] == "standard"
        ]
        filtered_items = [
            item for item in available_items
            if item["variant"] == "filtered"
        ]

        old_standard_order = validate_comparison_order(
            candidate,
            standard_items,
        )

        if filtered_items:
            return old_standard_order + [
                item["token"] for item in filtered_items
            ]

    except Exception:
        pass

    return default_order


def _write_order_dialog_vbs(default_text, available_items):
    """
    CP1250 jest celowe: VBS w polskim Windows/DIAdem źle wyświetlał UTF-8,
    stąd „zachowaÄ kolejnoÅÄ” w V18.
    """
    dialog_path = OUTPUT_DIR / COMPARISON_ORDER_DIALOG_FILENAME

    prompt_lines = [
        "Kolejność konkretnych paneli na stronach porównawczych.",
        "",
        "Wpisz wszystkie pozycje w żądanej kolejności, rozdzielając przecinkami.",
        "Końcówka _filtered oznacza fizyczny folder próbki zakończony ' - filtered'.",
        "Każde 9 wpisów tworzy kolejną stronę porównania.",
        "Pozycje 1–3 są w górnym rzędzie, 4–6 w środkowym, 7–9 w dolnym.",
        "",
        "Dostępne:",
    ]

    prompt_lines.extend(
        "{0}. {1}".format(index, item["token"])
        for index, item in enumerate(available_items, start=1)
    )

    prompt_lines.extend([
        "",
        "Anuluj lub pozostaw puste pole, aby zachować domyślną kolejność.",
    ])

    prompt = "\\n".join(prompt_lines).replace('"', '""')
    default_value = str(default_text).replace('"', '""')

    vbs = (
        'Sub SelectComparisonOrder\n'
        '  comparison_order_dialog_value = InputBox("'
        + prompt.replace("\\n", '" & vbCrLf & "')
        + '", "DIAdem — kolejność wykresów porównawczych", "'
        + default_value
        + '")\n'
        'End Sub\n'
    )

    dialog_path.write_text(vbs, encoding="cp1250")
    return dialog_path



def parse_comparison_order_text(text):
    """
    Kolejność może być zapisana po przecinkach, średnikach albo po liniach.
    Puste elementy i końcowy przecinek są ignorowane.
    """
    pieces = re.split(r"[,;\n\r]+", str(text))
    return [
        piece.strip()
        for piece in pieces
        if piece.strip()
    ]


def find_comparison_order_text_file():
    """
    Szuka jawnej listy kolejności w katalogu głównym serii.

    Nie szuka rekurencyjnie, żeby przypadkiem nie złapać starego logu albo
    pliku z Output.
    """
    for filename in COMPARISON_ORDER_TEXT_FILENAMES:
        path = ROOT_DIR / filename
        if path.is_file():
            return path
    return None


def available_order_tokens(available_items):
    """
    Nieuporządkowana lista tokenów w takiej kolejności, w jakiej skrypt
    wykrył próbki/panele.
    """
    return [
        item["token"]
        for item in available_items
    ]


def write_unsorted_comparison_order_template(available_items, reason):
    """
    Generuje plik pomocniczy do utworzenia ręcznej kolejności.

    Ten plik nie jest używany jako konfiguracja wejściowa; to szkic do
    skopiowania i ręcznego ułożenia.
    """
    tokens = available_order_tokens(available_items)
    path = ROOT_DIR / COMPARISON_ORDER_UNSORTED_TEMPLATE_FILENAME

    text = "\n".join(
        [
            "# To jest NIEUPORZĄDKOWANA lista paneli wykryta przez 05.",
            "# Powód utworzenia: {0}".format(reason),
            "#",
            "# Skopiuj poniższą listę, ustaw własną kolejność i zapisz jako:",
            "# {0}".format(COMPARISON_ORDER_REQUIRED_FILENAME),
            "#",
            "# Format może być po liniach albo po przecinkach.",
            "",
            ", ".join(tokens),
            "",
            "# Ten sam zestaw po liniach:",
            "",
        ]
        + tokens
        + [""]
    )

    path.write_text(text, encoding="utf-8")
    return path


def require_comparison_order_text_file(available_items):
    """
    Od V28 brak pliku TXT jest błędem kontrolowanym, nie powodem do popupu.
    """
    path = find_comparison_order_text_file()
    if path is not None:
        return path

    template_path = write_unsorted_comparison_order_template(
        available_items,
        "nie znaleziono pliku kolejności",
    )

    raise RuntimeError(
        "Brak pliku kolejności paneli porównawczych.\n"
        "Od V28 kolejność jest zawsze czytana z pliku TXT, bez popupu.\n\n"
        "Wygenerowano nieuporządkowaną listę wykrytych tokenów:\n"
        "{0}\n\n"
        "Ustaw w niej właściwą kolejność i zapisz jako:\n"
        "{1}\n\n"
        "Akceptowane nazwy pliku kolejności: {2}".format(
            template_path,
            ROOT_DIR / COMPARISON_ORDER_REQUIRED_FILENAME,
            ", ".join(COMPARISON_ORDER_TEXT_FILENAMES),
        )
    )


def load_comparison_order_from_text_file(available_items):
    """
    Plik TXT ma pierwszeństwo przed VBS InputBox. To omija limit długości
    pola tekstowego oraz literówki wynikające z ręcznego przepisywania.
    """
    path = require_comparison_order_text_file(available_items)

    encodings = ("utf-8-sig", "utf-8", "cp1250", "latin-1")
    last_error = None

    for encoding in encodings:
        try:
            text = path.read_text(encoding=encoding)
            tokens = parse_comparison_order_text(text)
            if not tokens:
                raise RuntimeError(
                    "Plik kolejności jest pusty: {0}".format(path)
                )

            try:
                resolved = validate_comparison_order(
                    tokens,
                    available_items,
                )
            except RuntimeError as exc:
                template_path = write_unsorted_comparison_order_template(
                    available_items,
                    "plik kolejności istnieje, ale nie przeszedł walidacji",
                )
                raise RuntimeError(
                    "{0}\n\n"
                    "Plik kolejności: {1}\n"
                    "Wygenerowano pomocniczą nieuporządkowaną listę "
                    "aktualnie wykrytych tokenów:\n"
                    "{2}\n\n"
                    "Porównaj ją z plikiem kolejności i popraw brakujące, "
                    "nadmiarowe albo zniekształcone tokeny.".format(
                        exc,
                        path,
                        template_path,
                    )
                )

            return {
                "path": path,
                "encoding": encoding,
                "tokens": resolved,
            }

        except UnicodeDecodeError as exc:
            last_error = exc
            continue
        except Exception:
            raise

    raise RuntimeError(
        "Nie można odczytać pliku kolejności {0}. Ostatni błąd: {1}"
        .format(path, last_error)
    )

def choose_comparison_order(available_items):
    """
    Od V28 jedyne źródło kolejności paneli to plik TXT.

    Celowo ignorujemy:
      - zapisane comparison_order.json jako źródło wejściowe,
      - popup VBS/InputBox.

    comparison_order.json jest nadal zapisywany po poprawnym odczycie TXT,
    ale tylko jako ślad wykonania, nie jako konfiguracja wejściowa.
    """
    text_file_order = load_comparison_order_from_text_file(
        available_items
    )
    chosen = text_file_order["tokens"]

    atomic_write_json(
        comparison_order_path(),
        {
            "saved_at": now_text(),
            "source": "plik TXT: {0}".format(
                text_file_order["path"]
            ),
            "encoding": text_file_order["encoding"],
            "order": chosen,
            "panel_count": len(chosen),
            "note": (
                "Od V28 kolejność wejściowa jest zawsze czytana z TXT; "
                "ten JSON jest tylko zapisem ostatniego poprawnego odczytu."
            ),
        },
    )

    print(
        "Kolejność porównań wczytana z pliku TXT: {0}".format(
            text_file_order["path"]
        )
    )

    return chosen


def reorder_prepared_comparison_items(
    prepared_samples,
    order_tokens,
    available_items,
):
    """
    Każdy token staje się konkretnym panelem z wariantem standard/filtered.
    """
    sample_by_id = {
        sample["sample_id"]: sample
        for sample in prepared_samples
    }
    item_by_token = {
        item["token"]: item
        for item in available_items
    }

    ordered = []
    for token in order_tokens:
        item = item_by_token.get(token)
        if item is None:
            raise RuntimeError(
                "Brak panelu w katalogu porównań: {0}".format(token)
            )

        sample_info = sample_by_id.get(item["sample_id"])
        if sample_info is None:
            raise RuntimeError(
                "Brak przygotowanej próbki dla panelu {0}."
                .format(token)
            )

        if item["variant"] == "filtered" and not sample_info.get("filtered"):
            raise RuntimeError(
                "{0}: wybrano filtered, ale FOscy nie przygotował danych."
                .format(token)
            )

        ordered.append(
            {
                "sample": sample_info,
                "sample_id": item["sample_id"],
                "variant": item["variant"],
                "token": item["token"],
                "display_label": item["display_label"],
            }
        )

    return ordered


def add_comparison_panel_label(
    sheet,
    display_label,
    token,
    x1,
    x2,
    y1,
    row,
    column,
    warnings,
):
    """
    Podpis jest poza AxisSystemem, więc nie zmienia rozmiaru spektrogramu.
    """
    try:
        safe_token = re.sub(r"[^A-Za-z0-9_]+", "_", token).strip("_")

        label = sheet.Objects.Add(
            dd.eReportObjectText,
            "ComparisonLabel_{0}_{1}_{2}".format(
                row,
                column,
                safe_token,
            ),
        )
        label.Text = display_label

        position = label.Position.ByCoordinate
        position.X1 = x1
        position.X2 = x2
        position.Y1 = max(0.5, y1 - COMPARISON_PANEL_LABEL_TOP_GAP)
        position.Y2 = max(
            position.Y1 + 0.5,
            y1 - COMPARISON_PANEL_LABEL_TOP_GAP
            + COMPARISON_PANEL_LABEL_HEIGHT,
        )

        set_title_style(label, warnings)
    except Exception as exc:
        warnings.append(
            "Comparison label {0}: {1}".format(display_label, exc)
        )


# ========================= IMPORT DO DATA PORTAL =============================

def normalise_mechanical_group(group, group_index, sample_id, geometry, approval):
    """
    Minimalny układ wymagany przez generator:
      1 Time
      2 Axial displacement
      3 Axial force

    Kanały 4..N są opcjonalne. W typowych danych są to temperatury, ale
    nie są potrzebne ani do Stress [MPa], ani do synchronizacji, ani do
    wykresu końcowego. Dzięki temu specimen.dat z 3, 4, 5, 6 lub większą
    liczbą kanałów działa tak samo w pipeline.
    """
    mechanical_channel_count = group.Channels.Count

    if mechanical_channel_count < 3:
        raise RuntimeError(
            "{0}: specimen.dat zaimportował tylko {1} kanałów; "
            "do raportu wymagane są minimum 3: Time, displacement, Force."
            .format(sample_id, mechanical_channel_count)
        )

    raw_time = group.Channels(1)
    displacement = group.Channels(2)
    force = group.Channels(3)

    raw_time.Name = "Time specimen raw"
    raw_time.UnitSymbol = "s"

    displacement.Name = "Axial displacement"
    displacement.UnitSymbol = "mm"

    force.Name = "Force"
    force.UnitSymbol = "N"

    # Kanały pomocnicze zachowujemy w Data Portal, ale nie narzucamy już
    # ich liczby. Nazwy są ujednolicone wyłącznie dla czytelności.
    for channel_index in range(4, mechanical_channel_count + 1):
        optional_channel = group.Channels(channel_index)
        optional_channel.Name = "Temp {0}".format(channel_index - 3)
        optional_channel.UnitSymbol = "degC"

    area_mm2 = to_float(geometry.get("cross_section_before_mm2"))
    if area_mm2 is None or area_mm2 <= 0:
        raise RuntimeError(
            "{0}: brak poprawnego A0 w manifeście geometrii.".format(sample_id)
        )

    offset_ae_to_mech = to_float(approval.get("offset_ae_to_mech_s"))
    if offset_ae_to_mech is None:
        raise RuntimeError(
            "{0}: brak offset_ae_to_mech_s w zatwierdzeniu synchronizacji."
            .format(sample_id)
        )

    # Mechanika zostaje przeniesiona na niezmienioną oś Time AE/TIFF:
    #
    #   Time specimen = Time specimen raw - offset_AE_to_mech
    #
    # DIAdem pracuje tu w trybie quantity-based: kanał czasu ma jednostkę [s],
    # a zwykła liczba jest bezwymiarowa. VU(..., "s") nadaje offsetowi
    # jednostkę sekund, więc dodawanie jest fizycznie poprawne.
    mechanical_time_offset_s = -offset_ae_to_mech
    dd.Calculate(
        'Ch("[{0}]/Time specimen") = '
        'Ch("[{0}]/Time specimen raw") + VU({1:.15g},"s")'.format(
            group_index,
            mechanical_time_offset_s,
        )
    )
    time_specimen = find_channel(group, "Time specimen")
    time_specimen.UnitSymbol = "s"

    # N/mm² = MPa.
    dd.Calculate(
        'Ch("[{0}]/Stress") = Ch("[{0}]/Force") / ({1:.15g})'.format(
            group_index,
            area_mm2,
        )
    )
    stress = find_channel(group, "Stress")
    stress.UnitSymbol = "MPa"

    return {
        "area_mm2": area_mm2,
        "offset_ae_to_mech_s": offset_ae_to_mech,
        "mechanical_time_offset_s": -offset_ae_to_mech,
        "mechanical_channel_count": mechanical_channel_count,
        "mechanical_rows": time_specimen.Size,
        "time_specimen_end_s": time_specimen.Maximum,
        "stress_min_mpa": stress.Minimum,
        "stress_max_mpa": stress.Maximum,
    }


def cumulative_for_report(times, source_cumulative, sync_end_ae):
    """
    Przygotowuje krzywą do REPORT/Data Portal.

    Domyślnie V37 zostawia poprawkę baseline'u:
      - CumEn_* z pliku jest przesuwane o baseline z okolic synchronizacji,
      - eventsNo jest kumulowane, a potem analogicznie przesuwane.

    Tryb surowy można włączyć ręcznie przez:
        SUBTRACT_AE_BASELINE_FOR_REPORT = False
    """
    if SUBTRACT_AE_BASELINE_FOR_REPORT:
        cumulative_info = baseline_subtracted_series(
            times,
            source_cumulative,
            sync_end_ae,
            CALIBRATION_TAIL_MARGIN_S,
        )
        cumulative = cumulative_info["cumulative"]

        validate_baseline_subtracted_series(
            source_cumulative,
            cumulative,
            cumulative_info["baseline_counts"],
        )

        cumulative_info["display_mode"] = "baseline_subtracted"
        return cumulative, cumulative_info

    cumulative = list(source_cumulative)

    return cumulative, {
        "requested_baseline_time_s": sync_end_ae,
        "baseline_time_s": sync_end_ae,
        "baseline_counts": 0.0,
        "baseline_rows": 0,
        "display_mode": "raw_cumulative",
    }


def add_ae_data(group, ae_data, approval):
    """
    Zapisuje kanały odpowiednie dla źródła AE.

    Events No. -> Cumulative EA counts.
    CumEn      -> Cumulative EA energy; oryginalnego CumEn nie sumujemy
                  drugi raz.
    """
    sync_end_ae = to_float(approval.get("sync_end_ae_time_s"))
    if sync_end_ae is None:
        sync_end_ae = to_float(approval.get("ae_zero_time_s"))
    if sync_end_ae is None:
        raise RuntimeError(
            "{0}: brak sync_end_ae_time_s w zatwierdzeniu synchronizacji."
            .format(group.Name)
        )

    times = ae_data["times"]
    mode = ae_data["mode"]
    source_cumulative = (
        cumulative_full(ae_data["events"])
        if mode == "events"
        else ae_data["cumulative_source"]
    )

    cumulative, cumulative_info = cumulative_for_report(
        times,
        source_cumulative,
        sync_end_ae,
    )

    add_numeric_channel(group, "Time AE", "s", times)

    if mode == "events":
        add_numeric_channel(
            group, "Events No.", "events/0.1s", ae_data["events"]
        )
        add_numeric_channel(
            group, "EA Energy", "arb.units", ae_data["energies"]
        )
    elif mode == "cumulative_energy":
        add_numeric_channel(
            group, "CumEn", "arb.units", ae_data["cumulative_source"]
        )
        add_numeric_channel(
            group, "d(CumEn)/dt", "arb.units/s", ae_data["energy_rate"]
        )
    else:
        raise RuntimeError(
            "{0}: nieobsługiwany tryb AE: {1}".format(group.Name, mode)
        )

    add_numeric_channel(
        group,
        ae_data["report_time_channel"],
        "s",
        times,
    )
    add_numeric_channel(
        group,
        ae_data["report_cumulative_channel"],
        ae_data["report_unit"],
        cumulative,
    )

    return {
        "ae_rows": len(times),
        "ae_time_end_s": times[-1],
        "sync_end_ae_time_s": sync_end_ae,
        "ae_zero_time_s": sync_end_ae,
        "ae_count_rows": len(times),
        "calibration_tail_margin_s": CALIBRATION_TAIL_MARGIN_S,
        "calibration_baseline_requested_time_s": (
            cumulative_info["requested_baseline_time_s"]
        ),
        "calibration_baseline_time_s": cumulative_info["baseline_time_s"],
        "calibration_baseline_counts": cumulative_info["baseline_counts"],
        "calibration_baseline_rows": cumulative_info["baseline_rows"],
        "cumulative_ea_final": cumulative[-1],
        "source_mode": mode,
        "report_time_channel": ae_data["report_time_channel"],
        "report_cumulative_channel": ae_data["report_cumulative_channel"],
        "report_axis_label": ae_data["report_axis_label"],
        "cumulative_definition": ae_data["source_description"],
    }


def build_overlay_cumulative_from_ae_data(ae_data, sync_end_ae):
    """
    Zwraca skorygowaną krzywą skumulowaną dla dodatkowego *_4000.txt.
    """
    if ae_data["mode"] == "events":
        source_cumulative = cumulative_full(ae_data["events"])
    elif ae_data["mode"] == "cumulative_energy":
        source_cumulative = ae_data["cumulative_source"]
    else:
        raise RuntimeError(
            "Nieobsługiwany tryb dodatkowej krzywej AE: {0}"
            .format(ae_data["mode"])
        )

    cumulative, cumulative_info = cumulative_for_report(
        ae_data["times"],
        source_cumulative,
        sync_end_ae,
    )

    return source_cumulative, cumulative, cumulative_info


def add_all_ae_4000_overlay_data(group, sample_dir, approval, main_ae_path):
    """
    Dodaje do raportu każdą dodatkową krzywą z plików *_4000.txt.

    Nie zmienia synchronizacji ani głównej krzywej. Synchronizacja nadal
    wynika z pliku wybranego jako main_ae_path, a wszystkie pozostałe
    *_4000.txt są tylko overlayami na tej samej prawej osi.
    """
    sync_end_ae = to_float(approval.get("sync_end_ae_time_s"))
    if sync_end_ae is None:
        sync_end_ae = to_float(approval.get("ae_zero_time_s"))
    if sync_end_ae is None:
        raise RuntimeError(
            "{0}: brak sync_end_ae_time_s dla dodatkowych *_4000.txt."
            .format(group.Name)
        )

    overlays = []

    for overlay_index, overlay_path in enumerate(
        additional_ae_4000_files_local(sample_dir, main_ae_path),
        start=1,
    ):
        ae_data_overlay = parse_ae_4000(overlay_path)
        source_cumulative, cumulative, cumulative_info = (
            build_overlay_cumulative_from_ae_data(
                ae_data_overlay,
                sync_end_ae,
            )
        )

        label = ae_curve_label_from_path(overlay_path)
        fragment = ae_curve_channel_fragment(overlay_path, overlay_index)

        time_channel = "Time AE overlay " + fragment
        raw_channel = "AE overlay source " + fragment
        rate_channel = "AE overlay rate " + fragment
        cumulative_channel = "Cumulative AE overlay " + fragment

        add_numeric_channel(
            group,
            time_channel,
            "s",
            ae_data_overlay["times"],
        )

        if ae_data_overlay["mode"] == "events":
            add_numeric_channel(
                group,
                raw_channel,
                "events/0.1s",
                ae_data_overlay["events"],
            )
        else:
            add_numeric_channel(
                group,
                raw_channel,
                "arb.units",
                ae_data_overlay["cumulative_source"],
            )
            add_numeric_channel(
                group,
                rate_channel,
                "arb.units/s",
                ae_data_overlay["energy_rate"],
            )

        add_numeric_channel(
            group,
            cumulative_channel,
            ae_data_overlay["report_unit"],
            cumulative,
        )

        overlays.append(
            {
                "kind": label,
                "source_path": str(overlay_path),
                "source_mode": ae_data_overlay["mode"],
                "time_channel": time_channel,
                "raw_channel": raw_channel,
                "rate_channel": (
                    rate_channel
                    if ae_data_overlay["mode"] == "cumulative_energy"
                    else ""
                ),
                "cumulative_channel": cumulative_channel,
                "axis_label": ae_data_overlay["report_axis_label"],
                "unit": ae_data_overlay["report_unit"],
                "rows": len(ae_data_overlay["times"]),
                "time_end_s": ae_data_overlay["times"][-1],
                "baseline_requested_time_s": (
                    cumulative_info["requested_baseline_time_s"]
                ),
                "baseline_time_s": cumulative_info["baseline_time_s"],
                "baseline_counts": cumulative_info["baseline_counts"],
                "baseline_rows": cumulative_info["baseline_rows"],
                "cumulative_final": cumulative[-1],
                "definition": (
                    "Dodatkowy *_4000.txt: {0}; ta sama korekta baseline "
                    "co dla głównej krzywej.".format(label)
                ),
            }
        )

    return overlays


def add_filtered_ae_data(group, filtered_artifact, approval):
    """
    Dodaje kanały wyłącznie dla alternatywnej strony FILTERED.

    Nie nadpisuje:
      Time AE,
      Events No.,
      Time AE counts,
      Cumulative EA counts.

    Dzięki temu standardowa karta próbki nadal pokazuje dane z *_4000.txt.
    """
    sync_end_ae = to_float(approval.get("sync_end_ae_time_s"))
    if sync_end_ae is None:
        sync_end_ae = to_float(approval.get("ae_zero_time_s"))

    if sync_end_ae is None:
        raise RuntimeError(
            "{0}: brak sync_end_ae_time_s dla filtered pipeline."
            .format(group.Name)
        )

    cumulative_info = cumulative_from_filtered_source(
        filtered_artifact["times"],
        filtered_artifact["values"],
        filtered_artifact["values_are_cumulative"],
        sync_end_ae,
        CALIBRATION_TAIL_MARGIN_S,
    )

    times = filtered_artifact["times"]
    values = filtered_artifact["values"]
    cumulative = cumulative_info["cumulative"]

    add_numeric_channel(group, "Time filtered counts", "s", times)
    add_numeric_channel(
        group,
        "Filtered counts source",
        "counts/0.01s",
        values,
    )
    add_numeric_channel(
        group,
        "Cumulative filtered counts",
        "counts",
        cumulative,
    )

    return {
        "rows": len(times),
        "time_end_s": times[-1],
        "series_end_s": inferred_series_end_time(times),
        "source_is_cumulative": filtered_artifact["values_are_cumulative"],
        "source_column": filtered_artifact["value_column"],
        "source_time_column": filtered_artifact["time_column"],
        "source_csv_path": filtered_artifact["counts_csv_path"],
        "tiff_path": filtered_artifact["tiff_path"],
        "tiff_end_s": filtered_artifact["tiff_end_s"],
        "baseline_requested_time_s": (
            cumulative_info["requested_baseline_time_s"]
        ),
        "baseline_time_s": cumulative_info["baseline_time_s"],
        "baseline_counts": cumulative_info["baseline_counts"],
        "baseline_rows": cumulative_info["baseline_rows"],
        "cumulative_final": cumulative[-1],
    }



def import_prepare_one_sample(
    sample_id,
    geometry_manifest,
    approvals,
    filtered_artifacts=None,
):
    if sample_id not in SAMPLE_INDEX:
        raise RuntimeError(
            "Nie znaleziono próbki {0} w bieżącym folderze. Dostępne: {1}"
            .format(sample_id, ", ".join(SAMPLE_INDEX.keys()))
        )

    meta = SAMPLE_INDEX[sample_id]
    sample_dir = meta["sample_root"]

    geometry = geometry_manifest.get(sample_id.casefold())
    if geometry is None:
        raise RuntimeError(
            "{0}: brak rekordu w mapowanie_Excel_geometria.json.".format(
                sample_id
            )
        )

    approval, approval_key = get_approval_for_sample(sample_id, approvals)
    if approval is None or approval.get("status") != "APPROVED":
        raise RuntimeError(
            "{0}: synchronizacja nie ma statusu APPROVED. "
            "Sprawdzono także bazową nazwę: {1}."
            .format(sample_id, approval_key)
        )

    # Najpierw dane liczbowe. TIFF/PNG rozwiązywany jest dopiero po obliczeniu
    # Stress i AE, żeby problem tła nie zatrzymywał diagnostyki naprężeń.
    specimen_path = meta["specimen_path"]
    ae_path = meta["ae_txt_path"]

    ae_selection_info = assert_main_ae_selection_is_consistent(
        sample_id,
        sample_dir,
        ae_path,
    )

    print("")
    print("[AE 4000] {0}".format(sample_id))
    print("  MAIN: {0}".format(ae_selection_info["main_4000"]))
    if ae_selection_info["overlay_4000"]:
        print("  OVERLAY:")
        for overlay_path in ae_selection_info["overlay_4000"]:
            print("    {0}".format(overlay_path))
    else:
        print("  OVERLAY: BRAK")

    ae_data = parse_ae_4000(ae_path)

    group_count_before = dd.Data.Root.ChannelGroups.Count
    dd.DataFileLoad(str(specimen_path), "")
    group_count_after = dd.Data.Root.ChannelGroups.Count

    if group_count_after != group_count_before + 1:
        raise RuntimeError(
            "{0}: import specimen.dat powinien dodać jedną grupę, ale liczba "
            "grup zmieniła się z {1} do {2}.".format(
                sample_id,
                group_count_before,
                group_count_after,
            )
        )

    group_index = group_count_after
    group = dd.Data.Root.ChannelGroups(group_index)
    group.Name = sample_id

    mechanical_info = normalise_mechanical_group(
        group,
        group_index,
        sample_id,
        geometry,
        approval,
    )
    ae_info = add_ae_data(
        group,
        ae_data,
        approval,
    )

    ae_info["overlay_curves"] = add_all_ae_4000_overlay_data(
        group,
        sample_dir,
        approval,
        ae_path,
    )

    filtered_info = None
    if filtered_artifacts is not None:
        filtered_artifact = filtered_artifacts.get(sample_id)
        if filtered_artifact is None:
            raise RuntimeError(
                "{0}: brak przygotowanych artefaktów filtered.".format(
                    sample_id
                )
            )

        filtered_info = add_filtered_ae_data(
            group,
            filtered_artifact,
            approval,
        )

    # TIFF i jego czas: po danych liczbowych, z fallbackiem na koniec AE
    # w trybie roboczym.
    tiff_info = resolve_tiff_info(
        sample_dir=sample_dir,
        fallback_end_s=ae_info["ae_time_end_s"],
    )

    x_end_s = max(
        tiff_info["tiff_end_s"],
        ae_info["ae_time_end_s"],
        mechanical_info["time_specimen_end_s"],
    )

    if filtered_info is not None:
        filtered_info["x_end_s"] = max(
            filtered_info["tiff_end_s"],
            filtered_info["series_end_s"],
            mechanical_info["time_specimen_end_s"],
        )

    return {
        "sample_id": sample_id,
        "group_index": group_index,
        "group": group,
        "specimen_path": str(specimen_path),
        "ae_txt_path": str(ae_path),
        "oscy_dir": tiff_info.get("oscy_dir", ""),
        "tiff_path": tiff_info.get("tiff_path", ""),
        "tiff_end_s": tiff_info["tiff_end_s"],
        "tiff_png_count": tiff_info["png_count"],
        "tiff_last_segment_index": tiff_info["last_segment_index"],
        "tiff_last_segment_seconds": tiff_info["last_segment_seconds"],
        "tiff_last_png": tiff_info["last_png"],
        "tiff_warnings": tiff_info["warnings"],
        "x_end_s": x_end_s,
        "geometry_sample_label": geometry.get("sample_label", ""),
        "mechanical": mechanical_info,
        "ae": ae_info,
        "ae_overlay_curves": ae_info.get("overlay_curves", []),
        "ae_4000_selection": ae_selection_info,
        "filtered": filtered_info,
    }


# ============================== REPORT DIAdem ================================

def try_set_nested_attribute(owner, dotted_path, value, warnings, description):
    """
    Best effort dla zagnieżdżonych właściwości COM, np. Label.Font.Name.
    """
    try:
        target = owner
        parts = dotted_path.split(".")
        for part in parts[:-1]:
            target = getattr(target, part)
        setattr(target, parts[-1], value)
        return True
    except Exception as exc:
        warnings.append(
            "{0}: nie ustawiono ({1}: {2})".format(
                description,
                dotted_path,
                exc,
            )
        )
        return False


def resolve_constant(*names, fallback=None):
    for name in names:
        if hasattr(dd, name):
            return getattr(dd, name)
    return fallback


def apply_font_family(text_owner, warnings, description):
    """
    Próbuje ustawić Times New Roman dla etykiet i liczb osi.
    """
    paths = [
        "Font.Name",
        "Label.Font.Name",
        "Scaling.Font.Name",
        "Scaling.TextFont.Name",
        "Ticks.Font.Name",
        "TickLabel.Font.Name",
        "ScaleText.Font.Name",
    ]
    seen = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        try_set_nested_attribute(
            text_owner,
            path,
            FONT_NAME,
            warnings,
            description + " / font",
        )


def apply_text_color(text_owner, color_value, warnings, description):
    """
    Ustawia kolor tekstu dla etykiet i (jeśli API pozwoli) liczb osi.
    """
    paths = [
        "Font.Color",
        "Label.Font.Color",
        "Scaling.Font.Color",
        "Scaling.TextFont.Color",
        "Ticks.Font.Color",
        "TickLabel.Font.Color",
        "ScaleText.Font.Color",
        "Color",
        "Label.Color",
    ]
    seen = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        try_set_nested_attribute(
            text_owner,
            path,
            color_value,
            warnings,
            description + " / text color",
        )


def apply_axis_label_x_position(axis_object, x_position, warnings, description):
    """
    Ustawia przesunięcie poziome podpisu osi Y.
    """
    paths = [
        "Label.Position.X",
        "Label.Position.ByCoordinate.X",
        "Label.X",
    ]
    success = False
    for path in paths:
        success = try_set_nested_attribute(
            axis_object,
            path,
            x_position,
            warnings,
            description + " / label X position",
        ) or success
    return success


def apply_stress_tick_alignment_right(axis_object, warnings):
    """
    Liczby osi Stress po lewej, wyrównane do prawej.
    Nazwy właściwości różnią się między wersjami, więc próbujemy kilka.
    """
    right_constant = resolve_constant(
        "eHorTextAlignRight",
        "eTextAlignmentRight",
        "eAlignmentRight",
        fallback=2,
    )

    paths = [
        "Scaling.TextAlignment",
        "Ticks.Alignment",
        "TickLabel.Alignment",
        "ScaleText.Alignment",
        "Label.Alignment",
    ]
    for path in paths:
        try_set_nested_attribute(
            axis_object,
            path,
            right_constant,
            warnings,
            "Stress axis / tick alignment",
        )


def apply_curve_color(curve, color_constant, warnings, description):
    """
    DIAdem kolor linii jest obiektem Color; nie liczbą OLE_COLOR.
    """
    try:
        curve.Shape.Settings.Line.Color.SetPredefinedColor(color_constant)
    except Exception as exc:
        warnings.append("{0}: {1}".format(description, exc))


def get_min_line_width_constant():
    return resolve_constant(
        "eLineWidth0000",
        "eLineWidth0050",
        "eLineWidth0100",
        fallback=None,
    )


def apply_min_curve_width(curve, warnings, description):
    width_constant = get_min_line_width_constant()
    if width_constant is None:
        warnings.append(description + ": brak stałej minimalnej grubości linii.")
        return

    paths = [
        "Shape.Settings.Line.Width",
        "Line.Width",
    ]
    success = False
    for path in paths:
        success = try_set_nested_attribute(
            curve,
            path,
            width_constant,
            warnings,
            description + " / min line width",
        ) or success

    if not success:
        warnings.append(description + ": nie udało się ustawić minimalnej grubości.")


def remove_object_frame(report_object, warnings, description):
    """
    Próbuje wyłączyć ramkę/obrys obiektu wykresu.
    """
    none_color = resolve_constant(
        "eColorIndexTransparent",
        "eColorIndexNone",
        fallback=None,
    )

    attempts = [
        ("Frame.Visible", False),
        ("Frame.Enable", False),
        ("Frame.Width", 0),
        ("Frame.Line.Width", 0),
        ("Border.Visible", False),
        ("Border.Enable", False),
        ("Border.Width", 0),
        ("Border.Line.Width", 0),
    ]

    for path, value in attempts:
        try_set_nested_attribute(
            report_object,
            path,
            value,
            warnings,
            description + " / no frame",
        )

    if none_color is not None:
        for path in (
            "Frame.Color",
            "Frame.Line.Color",
            "Border.Color",
            "Border.Line.Color",
        ):
            try:
                target = report_object
                parts = path.split(".")
                for part in parts[:-1]:
                    target = getattr(target, part)
                color_obj = getattr(target, parts[-1])
                color_obj.SetPredefinedColor(none_color)
            except Exception as exc:
                warnings.append(
                    "{0}: nie ustawiono koloru ramki na none ({1}: {2})".format(
                        description,
                        path,
                        exc,
                    )
                )


def move_stress_numbers_to_right(axis_object, warnings):
    """
    Próbuje przenieść liczby osi Stress na prawą stronę osi
    (czyli bliżej pola wykresu).
    """
    constants = [
        resolve_constant("eAxisNumberPositionRight", fallback=None),
        resolve_constant("eAxisNumbersPositionRight", fallback=None),
        resolve_constant("eAxisTextPositionRight", fallback=None),
        resolve_constant("eTextPositionRight", fallback=None),
        resolve_constant("ePositionRight", fallback=None),
        resolve_constant("eHorAlignmentRight", fallback=None),
    ]
    constants = [value for value in constants if value is not None]

    paths = [
        "Numbers.Position",
        "Numbers.RelativePosition",
        "Scaling.NumberPosition",
        "Scaling.TextPosition",
        "Ticks.Position",
        "TickLabel.Position",
        "ScaleText.Position",
    ]

    any_success = False
    for value in constants:
        for path in paths:
            any_success = try_set_nested_attribute(
                axis_object,
                path,
                value,
                warnings,
                "Stress axis / numbers right side",
            ) or any_success

    # Fallback geometryczny: dodatni offset liczb względem osi.
    for path in (
        "Numbers.OffsetX",
        "Scaling.TextOffsetX",
        "TickLabel.OffsetX",
        "ScaleText.OffsetX",
    ):
        any_success = try_set_nested_attribute(
            axis_object,
            path,
            1.0,
            warnings,
            "Stress axis / numbers right side",
        ) or any_success

    if not any_success:
        warnings.append(
            "Stress axis / numbers right side: API nie udostępniło "
            "jednoznacznej właściwości ustawienia strony liczb."
        )


def disable_grid(axis_system, warnings):
    none_mode = resolve_constant(
        "e2DAxisGridModeNone",
        "eAxisGridModeNone",
        fallback=0,
    )

    try:
        axis_system.Settings.Grid.DisplayMode = none_mode
    except Exception as exc:
        warnings.append("Grid off: {0}".format(exc))


def set_title_style(title_object, warnings):
    try:
        title_object.Font.Name = FONT_NAME
        title_object.Font.Color.SetPredefinedColor(dd.eColorIndexBlack)
    except Exception as exc:
        warnings.append("Title style: {0}".format(exc))

    # Bez ramki tekstu.
    for path, value in (
        ("Frame.Visible", False),
        ("Frame.Enable", False),
        ("Border.Visible", False),
        ("Border.Enable", False),
    ):
        try_set_nested_attribute(
            title_object,
            path,
            value,
            warnings,
            "Title / no frame",
        )


def set_predefined_color(color_object, color_constant, warnings, description):
    try:
        color_object.SetPredefinedColor(color_constant)
        return True
    except Exception as exc:
        warnings.append(
            "{0}: nie ustawiono koloru ({1})".format(description, exc)
        )
        return False


def set_axis_numbers_exact(
    axis_object,
    color_constant,
    relative_position=None,
    text_alignment=None,
    warnings=None,
    description="Axis",
):
    """
    Właściwy obiekt DIAdem dla liczb podziałki to YAxis.Numbers.
    Nie próbujemy już pośrednich ścieżek typu Scaling.TextPosition.
    """
    if warnings is None:
        warnings = []

    try:
        axis_object.Numbers.UseCurveColor = False
    except Exception as exc:
        warnings.append("{0}: Numbers.UseCurveColor ({1})".format(description, exc))

    try:
        axis_object.Numbers.Font.Name = FONT_NAME
    except Exception as exc:
        warnings.append("{0}: Numbers.Font.Name ({1})".format(description, exc))

    try:
        axis_object.Numbers.Font.Color.SetPredefinedColor(color_constant)
    except Exception as exc:
        warnings.append("{0}: Numbers.Font.Color ({1})".format(description, exc))

    if relative_position is not None:
        try:
            axis_object.Numbers.RelativePosition = relative_position
        except Exception as exc:
            warnings.append(
                "{0}: Numbers.RelativePosition ({1})".format(description, exc)
            )

    if text_alignment is not None:
        try:
            axis_object.Numbers.TextAlignment = text_alignment
        except Exception as exc:
            warnings.append(
                "{0}: Numbers.TextAlignment ({1})".format(description, exc)
            )


def style_axis_numbers(axis_object, color_constant, warnings, description):
    """
    W DIAdem liczby podziałki są osobnym obiektem Axis.Numbers,
    niezależnym od Axis.Label.
    """
    try:
        axis_object.Numbers.Font.Name = FONT_NAME
    except Exception as exc:
        warnings.append(
            "{0}: Numbers.Font.Name ({1})".format(description, exc)
        )

    try:
        axis_object.Numbers.Font.Color.SetPredefinedColor(color_constant)
    except Exception as exc:
        warnings.append(
            "{0}: Numbers.Font.Color ({1})".format(description, exc)
        )


def style_axis_label_exact(axis_object, color_constant, warnings, description):
    """
    Wymusza kolor niezależny od krzywej i styluje podpis osi.
    """
    try:
        axis_object.Label.UseCurveColor = False
    except Exception as exc:
        warnings.append(
            "{0}: Label.UseCurveColor ({1})".format(description, exc)
        )

    try:
        axis_object.Label.Font.Name = FONT_NAME
    except Exception as exc:
        warnings.append(
            "{0}: Label.Font.Name ({1})".format(description, exc)
        )

    try:
        axis_object.Label.Font.Color.SetPredefinedColor(color_constant)
    except Exception as exc:
        warnings.append(
            "{0}: Label.Font.Color ({1})".format(description, exc)
        )


def set_axis_label_offset_x_exact(axis_object, offset_x, warnings, description):
    """
    OffsetX działa tylko przy manualnym RelativePosition.
    """
    try:
        axis_object.Label.RelativePosition = dd.eAxisLabelRelativePositionManual
        axis_object.Label.OffsetX = offset_x
    except Exception as exc:
        warnings.append(
            "{0}: Label.OffsetX ({1})".format(description, exc)
        )


def style_axis_texts(axis_x, frequency_axis, stress_axis, cumulative_axis, warnings):
    # X: podpis i liczby czarne, Times New Roman.
    style_axis_label_exact(axis_x, dd.eColorIndexBlack, warnings, "X axis")
    set_axis_numbers_exact(
        axis_x,
        dd.eColorIndexBlack,
        warnings=warnings,
        description="X axis",
    )

    # Frequency: podpis i liczby czarne, Times New Roman.
    style_axis_label_exact(
        frequency_axis,
        dd.eColorIndexBlack,
        warnings,
        "Frequency axis",
    )
    set_axis_numbers_exact(
        frequency_axis,
        dd.eColorIndexBlack,
        relative_position=dd.eRelativePositionLeft,
        text_alignment=dd.eAxisLabelTextAlignmentRight,
        warnings=warnings,
        description="Frequency axis",
    )
    set_axis_label_offset_x_exact(
        frequency_axis,
        FREQUENCY_LABEL_X_POSITION,
        warnings,
        "Frequency axis",
    )

    # Stress jest drugą osią po lewej.
    # Liczby mają leżeć po LEWEJ stronie jej kresek — w zewnętrznym
    # marginesie — oraz być wyrównane do prawej, czyli dosunięte do osi.
    style_axis_label_exact(
        stress_axis,
        dd.eColorIndexBlack,
        warnings,
        "Stress axis",
    )
    set_axis_numbers_exact(
        stress_axis,
        dd.eColorIndexBlack,
        relative_position=dd.eRelativePositionLeft,
        text_alignment=dd.eAxisLabelTextAlignmentRight,
        warnings=warnings,
        description="Stress axis",
    )
    set_axis_label_offset_x_exact(
        stress_axis,
        STRESS_LABEL_X_POSITION,
        warnings,
        "Stress axis",
    )

    # Prawa oś: Times New Roman; jej kolor zachowujemy jako zgodny z krzywą.
    try:
        cumulative_axis.Label.UseCurveColor = True
        cumulative_axis.Numbers.UseCurveColor = True
        cumulative_axis.Label.Font.Name = FONT_NAME
        cumulative_axis.Numbers.Font.Name = FONT_NAME
    except Exception as exc:
        warnings.append("Cumulative axis / font-color: {0}".format(exc))


def try_set_attribute(owner, attribute, value, warnings, description):
    """
    Ustawienia estetyczne/etykiety nie mogą unieruchomić generowania danych.
    Krytyczne odwołania kanałów i tło TIFF-a są obsługiwane poza tą funkcją.
    """
    try:
        setattr(owner, attribute, value)
        return True
    except Exception as exc:
        warnings.append(
            "{0}: nie ustawiono ({1}: {2})".format(
                description,
                attribute,
                exc,
            )
        )
        return False


def configure_manual_scale(scaling, begin, end, warnings, description):
    if end <= begin:
        end = begin + 1.0

    try_set_attribute(
        scaling,
        "AutoScalingType",
        dd.eAxisAutoScalingBeginEndManual,
        warnings,
        description + " / typ skali",
    )
    try_set_attribute(
        scaling,
        "Begin",
        begin,
        warnings,
        description + " / początek",
    )
    try_set_attribute(
        scaling,
        "End",
        end,
        warnings,
        description + " / koniec",
    )


def nice_stress_range(stress_min, stress_max):
    lower = 0.0
    upper = max(0.0, float(stress_max))

    if upper <= lower:
        return 0.0, 1.0

    margin = 0.06 * (upper - lower)
    return lower, upper + margin


def report_object_name(name, max_len=40):
    """
    DIAdem REPORT ma limit długości nazwy obiektu, m.in. krzywej: 40 znaków.

    To skraca wyłącznie wewnętrzną nazwę obiektu REPORT. Nie zmienia nazw
    kanałów, etykiet osi ani tytułów.
    """
    text = str(name)

    if len(text) <= max_len:
        return text

    digest = hashlib.sha1(
        text.encode("utf-8", errors="ignore")
    ).hexdigest()[:8]
    prefix_len = max_len - len(digest) - 1

    if prefix_len < 1:
        return digest[:max_len]

    return text[:prefix_len] + "_" + digest


def add_curve(axis_system, name, group_index, x_channel_name, y_channel_name, y_axis_name):
    curve = axis_system.Curves2D.Add(
        dd.e2DShapeLine,
        report_object_name(name),
    )
    curve.Shape.XChannel.Reference = "[{0}]/{1}".format(
        group_index,
        x_channel_name,
    )
    curve.Shape.YChannel.Reference = "[{0}]/{1}".format(
        group_index,
        y_channel_name,
    )
    curve.YAxisReference = y_axis_name
    return curve


def set_axis_label(axis_y, text, warnings, description):
    """
    W DIAdem 2021 Label jest obiektem. Dla części konfiguracji API
    nazwa właściwości tekstu bywa udostępniana jako Text.
    """
    try:
        axis_y.Label.Text = text
    except Exception as exc:
        warnings.append(
            "{0}: nie ustawiono etykiety osi „{1}” ({2})".format(
                description,
                text,
                exc,
            )
        )


def set_x_axis_label(axis_x, text, warnings):
    try:
        axis_x.Label.Text = text
    except Exception as exc:
        warnings.append(
            "X-axis: nie ustawiono etykiety „{0}” ({1})".format(text, exc)
        )


def configure_axis_offsets(axis_system, frequency_axis, stress_axis, cumulative_axis, warnings):
    """
    Pionowe osie ustawiamy względem początku/końca osi X:
      Frequency      — lewy brzeg,
      Stress         — odsunięta druga oś po lewej,
      Cumulative AE  — prawy brzeg.
    """
    try_set_attribute(
        frequency_axis,
        "OffsetOrigin",
        dd.e2DAxisOffsetOriginAxisBegin,
        warnings,
        "Frequency axis / offset origin",
    )
    try_set_attribute(
        frequency_axis,
        "OffsetHorizontal",
        0.0,
        warnings,
        "Frequency axis / offset horizontal",
    )

    try_set_attribute(
        stress_axis,
        "OffsetOrigin",
        dd.e2DAxisOffsetOriginAxisBegin,
        warnings,
        "Stress axis / offset origin",
    )
    try_set_attribute(
        stress_axis,
        "OffsetHorizontal",
        STRESS_AXIS_OFFSET_LEFT_PERCENT,
        warnings,
        "Stress axis / offset horizontal",
    )

    try_set_attribute(
        cumulative_axis,
        "OffsetOrigin",
        dd.e2DAxisOffsetOriginAxisEnd,
        warnings,
        "Cumulative axis / offset origin",
    )
    try_set_attribute(
        cumulative_axis,
        "OffsetHorizontal",
        CUMULATIVE_AXIS_OFFSET_RIGHT_PERCENT,
        warnings,
        "Cumulative axis / offset horizontal",
    )


def build_report_sheet(sample_info, is_first_sheet, variant="standard"):
    """
    Tworzy jedną zakładkę Reportu.

    variant="standard":
      TIFF z oscy1 + Cumulative EA counts z *_4000.txt.

    variant="filtered":
      TIFF z oscy1-filtered + Cumulative filtered counts z
      counts_vs_time_0p01s.csv.

    Mechanika i osie Frequency/Stress pozostają wspólne.
    """
    warnings = []
    sample_id = sample_info["sample_id"]
    group_index = sample_info["group_index"]

    if variant == "filtered":
        filtered = sample_info.get("filtered")
        if not filtered:
            raise RuntimeError(
                "{0}: zażądano strony filtered bez danych filtered."
                .format(sample_id)
            )

        report_key = sample_id + FILTERED_SHEET_SUFFIX
        title_sample_id = sample_id + FILTERED_TITLE_SUFFIX
        selected_tiff_path = filtered["tiff_path"]
        selected_tiff_end_s = filtered["tiff_end_s"]
        selected_x_end_s = filtered["x_end_s"]
        count_x_channel = "Time filtered counts"
        count_y_channel = "Cumulative filtered counts"
        cumulative_final = filtered["cumulative_final"]
        baseline_time_s = filtered["baseline_time_s"]
        cumulative_axis_label = "Cumulative filtered counts"
    else:
        report_key = sample_id
        title_sample_id = sample_id
        selected_tiff_path = sample_info.get("tiff_path", "")
        selected_tiff_end_s = sample_info["tiff_end_s"]
        selected_x_end_s = sample_info["x_end_s"]
        count_x_channel = sample_info["ae"]["report_time_channel"]
        count_y_channel = sample_info["ae"]["report_cumulative_channel"]
        cumulative_axis_label = sample_info["ae"]["report_axis_label"]
        cumulative_final = max(
            [sample_info["ae"]["cumulative_ea_final"]]
            + [
                curve["cumulative_final"]
                for curve in sample_info["ae"].get("overlay_curves", [])
            ]
        )
        baseline_time_s = sample_info["ae"]["ae_zero_time_s"]

    if is_first_sheet:
        sheet = dd.Report.ActiveSheet
        sheet.Name = report_key
    else:
        sheet = dd.Report.Sheets.Add(report_key)
        sheet.Activate()

    axis_system = sheet.Objects.Add(
        dd.eReportObject2DAxisSystem,
        "DS_" + report_key,
    )
    remove_object_frame(axis_system, warnings, "Axis system")

    # Białe tło całego obiektu osi: TIFF przykrywa środek, a marginesy
    # dla czarnych podpisów/liczb nie są czarne.
    try:
        axis_system.Settings.BackgroundColor.SetPredefinedColor(dd.eColorIndexWhite)
    except Exception as exc:
        warnings.append("Axis system / white background: {0}".format(exc))

    # Duży centralny wykres. Dodatkowe lewe pole daje miejsce dla Stress.
    position = axis_system.Position.ByCoordinate
    position.X1 = 18
    position.X2 = 86
    position.Y1 = 13
    position.Y2 = 88

    # Tworzymy trzy osie Y.
    frequency_axis = axis_system.YAxis
    frequency_axis.Name = "FrequencyAxis"

    stress_axis = axis_system.YAxisList.Add("StressAxis")
    cumulative_axis = axis_system.YAxisList.Add("CumulativeAxis")

    configure_axis_offsets(
        axis_system,
        frequency_axis,
        stress_axis,
        cumulative_axis,
        warnings,
    )

    # Skale.
    configure_manual_scale(
        axis_system.XAxis.Scaling,
        0.0,
        selected_x_end_s,
        warnings,
        "Time [s]",
    )
    configure_manual_scale(
        frequency_axis.Scaling,
        FREQUENCY_MIN_KHZ,
        FREQUENCY_MAX_KHZ,
        warnings,
        "Frequency [kHz]",
    )

    stress_low, stress_high = nice_stress_range(
        sample_info["mechanical"]["stress_min_mpa"],
        sample_info["mechanical"]["stress_max_mpa"],
    )
    configure_manual_scale(
        stress_axis.Scaling,
        stress_low,
        stress_high,
        warnings,
        "Stress [MPa]",
    )

    cumulative_end = max(1.0, cumulative_final * 1.05)
    configure_manual_scale(
        cumulative_axis.Scaling,
        0.0,
        cumulative_end,
        warnings,
        cumulative_axis_label,
    )

    # Etykiety osi.
    set_x_axis_label(axis_system.XAxis, "Time [s]", warnings)
    set_axis_label(
        frequency_axis,
        "Frequency [kHz]",
        warnings,
        "Frequency axis",
    )
    set_axis_label(stress_axis, "Stress [MPa]", warnings, "Stress axis")
    set_axis_label(
        cumulative_axis,
        cumulative_axis_label,
        warnings,
        "Cumulative axis",
    )

    # Styl czcionek i podpisów.
    style_axis_texts(
        axis_system.XAxis,
        frequency_axis,
        stress_axis,
        cumulative_axis,
        warnings,
    )

    # Bez siatki.
    disable_grid(axis_system, warnings)

    # TIFF jako obraz tła z własnym końcem czasu i zakresem 0–125 kHz.
    if selected_tiff_path:
        background = axis_system.Settings.BackgroundImage
        background.FileName = selected_tiff_path

        scaling = background.Scaling
        scaling.Enable = True
        scaling.XBegin = 0.0
        scaling.XEnd = selected_tiff_end_s
        scaling.XUnit = "s"
        scaling.YBegin = FREQUENCY_MIN_KHZ
        scaling.YEnd = FREQUENCY_MAX_KHZ
        scaling.YUnit = "kHz"
    else:
        warnings.append("Brak TIFF-a; wygenerowano osie i krzywe bez tła.")

    # Krzywe przypisane do właściwych osi.
    stress_curve = add_curve(
        axis_system,
        "Stress_curve",
        group_index,
        "Time specimen",
        "Stress",
        stress_axis.Name,
    )
    cumulative_curve = add_curve(
        axis_system,
        "Cumulative_EA_curve_" + variant,
        group_index,
        count_x_channel,
        count_y_channel,
        cumulative_axis.Name,
    )

    overlay_curves = []
    if variant != "filtered":
        for overlay_index, overlay in enumerate(
            sample_info["ae"].get("overlay_curves", []),
            start=1,
        ):
            overlay_curves.append(
                add_curve(
                    axis_system,
                    "Cumulative_EA_overlay_{0}_{1}".format(
                        overlay["kind"],
                        overlay_index,
                    ),
                    group_index,
                    overlay["time_channel"],
                    overlay["cumulative_channel"],
                    cumulative_axis.Name,
                )
            )

    # Grubość linii jest ustawieniem estetycznym.
    apply_min_curve_width(
        stress_curve,
        warnings,
        "Stress curve",
    )
    apply_min_curve_width(
        cumulative_curve,
        warnings,
        "Cumulative curve",
    )
    for overlay_curve in overlay_curves:
        apply_min_curve_width(
            overlay_curve,
            warnings,
            "Cumulative overlay curve",
        )

    # Kolory krzywych.
    apply_curve_color(
        stress_curve,
        dd.eColorIndexBlack,
        warnings,
        "Stress curve / color",
    )
    apply_curve_color(
        cumulative_curve,
        dd.eColorIndexDarkBlue,
        warnings,
        "Cumulative curve / color",
    )
    overlay_color = resolve_constant(
        "eColorIndexRed",
        "eColorIndexOrange",
        fallback=dd.eColorIndexBlack,
    )
    for overlay_curve in overlay_curves:
        apply_curve_color(
            overlay_curve,
            overlay_color,
            warnings,
            "Cumulative overlay curve / color",
        )

    # Tytuł umieszczamy jako obiekt tekstowy, ale nie ryzykujemy całego raportu,
    # jeśli dany preset DIAdem ma inną konfigurację tekstu.
    try:
        title = sheet.Objects.Add(dd.eReportObjectText, "Title_" + report_key)
        overlay_names = [
            curve["kind"]
            for curve in sample_info["ae"].get("overlay_curves", [])
        ]
        overlay_note = (
            " | overlays: " + ", ".join(overlay_names[:4])
            + ("..." if len(overlay_names) > 4 else "")
            if variant != "filtered" and overlay_names
            else ""
        )
        cumulative_title_part = (
            "Cumulative EA from {0:.3f} s".format(baseline_time_s)
            if SUBTRACT_AE_BASELINE_FOR_REPORT
            else "Cumulative EA raw"
        )
        title.Text = (
            "{0} | A0 = {1:.4f} mm² | "
            "Time specimen offset = {2:+.6f} s | "
            "{3}{4}"
        ).format(
            title_sample_id,
            sample_info["mechanical"]["area_mm2"],
            sample_info["mechanical"]["mechanical_time_offset_s"],
            cumulative_title_part,
            overlay_note,
        )
        title_position = title.Position.ByCoordinate
        title_position.X1 = TITLE_X1
        title_position.X2 = TITLE_X2
        title_position.Y1 = TITLE_Y1
        title_position.Y2 = TITLE_Y2
        set_title_style(title, warnings)
    except Exception as exc:
        warnings.append("Title: {0}".format(exc))

    return {
        "sheet_name": sheet.Name,
        "axis_object_name": axis_system.Name,
        "warnings": warnings,
    }



# ========================= ARKUSZ PORÓWNAWCZY 2×3 ============================

def comparison_panel_position(index, total_panels):
    """
    Rozmieszczenie w kolejności:
        1 2 3
        4 5 6
        7 8 9
    """
    row = index // COMPARISON_COLUMNS
    column = index % COMPARISON_COLUMNS

    total_width = COMPARISON_RIGHT_X - COMPARISON_LEFT_X
    panel_width = (
        total_width - COMPARISON_COLUMN_GAP * (COMPARISON_COLUMNS - 1)
    ) / COMPARISON_COLUMNS

    x1 = COMPARISON_LEFT_X + column * (
        panel_width + COMPARISON_COLUMN_GAP
    )
    x2 = x1 + panel_width

    if row == 0:
        y1 = COMPARISON_TOP_Y1
        y2 = COMPARISON_TOP_Y2
    elif row == 1:
        y1 = COMPARISON_MIDDLE_Y1
        y2 = COMPARISON_MIDDLE_Y2
    else:
        y1 = COMPARISON_BOTTOM_Y1
        y2 = COMPARISON_BOTTOM_Y2

    return row, column, x1, x2, y1, y2


def hide_axis_text(axis_object, warnings, description):
    """
    W wewnętrznych panelach porównania nie powtarzamy liczb ani podpisów
    osi Y. Samych osi/krzywych nie usuwamy, bo są potrzebne do skali.
    """
    try:
        axis_object.Label.Text = ""
    except Exception as exc:
        warnings.append(
            "{0}: nie ukryto podpisu osi ({1})".format(description, exc)
        )

    try:
        axis_object.Numbers.Visible = False
    except Exception:
        # Fallback: gdy Visible nie występuje, tekst jest przezroczysty.
        transparent = resolve_constant(
            "eColorIndexTransparent",
            "eColorIndexNone",
            fallback=None,
        )
        if transparent is not None:
            try:
                axis_object.Numbers.Font.Color.SetPredefinedColor(transparent)
            except Exception as exc:
                warnings.append(
                    "{0}: nie ukryto liczb osi ({1})".format(
                        description,
                        exc,
                    )
                )


def set_comparison_axis_texts(
    axis_system,
    frequency_axis,
    stress_axis,
    cumulative_axis,
    row,
    column,
    cumulative_axis_label,
    warnings,
):
    """
    Wzór z pliku:
      - Frequency i Stress tylko w pierwszej kolumnie danego rzędu,
      - Cumulative EA tylko w ostatniej kolumnie danego rzędu,
      - Time [s] pod każdym panelem.
    """
    set_x_axis_label(axis_system.XAxis, "Time [s]", warnings)

    # Każdy panel ma podpis czasu w Times New Roman, kolorem czarnym.
    style_axis_label_exact(
        axis_system.XAxis,
        dd.eColorIndexBlack,
        warnings,
        "Comparison X axis",
    )
    set_axis_numbers_exact(
        axis_system.XAxis,
        dd.eColorIndexBlack,
        warnings=warnings,
        description="Comparison X axis",
    )

    if column == 0:
        set_axis_label(
            frequency_axis,
            "Frequency [kHz]",
            warnings,
            "Comparison Frequency axis",
        )
        set_axis_label(
            stress_axis,
            "Stress [MPa]",
            warnings,
            "Comparison Stress axis",
        )

        style_axis_label_exact(
            frequency_axis,
            dd.eColorIndexBlack,
            warnings,
            "Comparison Frequency axis",
        )
        set_axis_numbers_exact(
            frequency_axis,
            dd.eColorIndexBlack,
            relative_position=dd.eRelativePositionLeft,
            text_alignment=dd.eAxisLabelTextAlignmentRight,
            warnings=warnings,
            description="Comparison Frequency axis",
        )
        set_axis_label_offset_x_exact(
            frequency_axis,
            FREQUENCY_LABEL_X_POSITION,
            warnings,
            "Comparison Frequency axis",
        )

        style_axis_label_exact(
            stress_axis,
            dd.eColorIndexBlack,
            warnings,
            "Comparison Stress axis",
        )
        set_axis_numbers_exact(
            stress_axis,
            dd.eColorIndexBlack,
            relative_position=dd.eRelativePositionLeft,
            text_alignment=dd.eAxisLabelTextAlignmentRight,
            warnings=warnings,
            description="Comparison Stress axis",
        )
        set_axis_label_offset_x_exact(
            stress_axis,
            STRESS_LABEL_X_POSITION,
            warnings,
            "Comparison Stress axis",
        )
    else:
        hide_axis_text(
            frequency_axis,
            warnings,
            "Comparison Frequency axis / interior panel",
        )
        hide_axis_text(
            stress_axis,
            warnings,
            "Comparison Stress axis / interior panel",
        )

    if column == COMPARISON_COLUMNS - 1:
        set_axis_label(
            cumulative_axis,
            cumulative_axis_label,
            warnings,
            "Comparison Cumulative axis",
        )
        try:
            cumulative_axis.Label.UseCurveColor = True
            cumulative_axis.Numbers.UseCurveColor = True
            cumulative_axis.Label.Font.Name = FONT_NAME
            cumulative_axis.Numbers.Font.Name = FONT_NAME
        except Exception as exc:
            warnings.append(
                "Comparison Cumulative axis / font-color: {0}".format(exc)
            )
    else:
        hide_axis_text(
            cumulative_axis,
            warnings,
            "Comparison Cumulative axis / interior panel",
        )


def configure_comparison_background(axis_system, sample_info, warnings):
    """
    Każdy panel dostaje własne TIFF i własny fizyczny czas TIFF-a.
    """
    if not sample_info.get("tiff_path"):
        warnings.append(
            "{0}: brak TIFF-a w panelu porównawczym.".format(
                sample_info["sample_id"]
            )
        )
        return

    try:
        background = axis_system.Settings.BackgroundImage
        background.FileName = sample_info["tiff_path"]

        scaling = background.Scaling
        scaling.Enable = True
        scaling.XBegin = 0.0
        scaling.XEnd = sample_info["tiff_end_s"]
        scaling.XUnit = "s"
        scaling.YBegin = FREQUENCY_MIN_KHZ
        scaling.YEnd = FREQUENCY_MAX_KHZ
        scaling.YUnit = "kHz"
    except Exception as exc:
        warnings.append(
            "{0}: TIFF background ({1})".format(
                sample_info["sample_id"],
                exc,
            )
        )


def comparison_variant_values(sample_info, variant):
    """
    Dane panelu. Wariant jest wybierany dla każdego panelu niezależnie.
    """
    if variant == "filtered":
        filtered = sample_info.get("filtered")
        if not filtered:
            raise RuntimeError(
                "{0}: brak danych filtered dla panelu porównawczego."
                .format(sample_info["sample_id"])
            )

        return {
            "variant": "filtered",
            "tiff_path": filtered["tiff_path"],
            "tiff_end_s": filtered["tiff_end_s"],
            "x_end_s": filtered["x_end_s"],
            "count_x_channel": "Time filtered counts",
            "count_y_channel": "Cumulative filtered counts",
            "cumulative_axis_label": "Cumulative filtered counts",
            "cumulative_final": filtered["cumulative_final"],
            "overlay_curves": [],
        }

    return {
        "variant": "standard",
        "tiff_path": sample_info.get("tiff_path", ""),
        "tiff_end_s": sample_info["tiff_end_s"],
        "x_end_s": sample_info["x_end_s"],
        "count_x_channel": sample_info["ae"]["report_time_channel"],
        "count_y_channel": sample_info["ae"]["report_cumulative_channel"],
        "cumulative_axis_label": sample_info["ae"]["report_axis_label"],
        "cumulative_final": max(
            [sample_info["ae"]["cumulative_ea_final"]]
            + [
                curve["cumulative_final"]
                for curve in sample_info["ae"].get("overlay_curves", [])
            ]
        ),
        "overlay_curves": sample_info["ae"].get("overlay_curves", []),
    }


def configure_comparison_background(axis_system, visual, warnings, panel_token):
    if not visual.get("tiff_path"):
        warnings.append(
            "{0}: brak TIFF-a w panelu porównawczym."
            .format(panel_token)
        )
        return

    try:
        background = axis_system.Settings.BackgroundImage
        background.FileName = visual["tiff_path"]

        scaling = background.Scaling
        scaling.Enable = True
        scaling.XBegin = 0.0
        scaling.XEnd = visual["tiff_end_s"]
        scaling.XUnit = "s"
        scaling.YBegin = FREQUENCY_MIN_KHZ
        scaling.YEnd = FREQUENCY_MAX_KHZ
        scaling.YUnit = "kHz"
    except Exception as exc:
        warnings.append(
            "{0}: TIFF background ({1})".format(panel_token, exc)
        )


def build_comparison_sheet_page(comparison_items, sheet_name):
    """
    Jedna strona 3×3, dowolnie mieszająca standardowe i filtered panele.

    Skala Cumulative EA jest obliczana dopiero po ułożeniu paneli:
      - pozycje 1–3: wspólna skala górnego rzędu,
      - pozycje 4–6: wspólna skala środkowego rzędu,
      - pozycje 7–9: wspólna skala dolnego rzędu.
    """
    warnings = []

    if not comparison_items:
        return {
            "sheet_name": "",
            "panel_count": 0,
            "warnings": ["Brak paneli dla arkusza porównawczego."],
        }

    sheet = dd.Report.Sheets.Add(sheet_name)
    sheet.Activate()

    global_stress_max = max(
        item["sample"]["mechanical"]["stress_max_mpa"]
        for item in comparison_items
    )
    comparison_stress_low, comparison_stress_high = nice_stress_range(
        0.0,
        global_stress_max,
    )

    row_cumulative_ends = {}
    for row_index in range(
        int(math.ceil(len(comparison_items) / float(COMPARISON_COLUMNS)))
    ):
        row_items = comparison_items[
            row_index * COMPARISON_COLUMNS:
            (row_index + 1) * COMPARISON_COLUMNS
        ]

        if row_items:
            row_axis_labels = {
                comparison_variant_values(
                    item["sample"],
                    item["variant"],
                )["cumulative_axis_label"]
                for item in row_items
            }
            if len(row_axis_labels) > 1:
                warnings.append(
                    "Rząd porównania {0} miesza różne wielkości prawej osi: "
                    "{1}. Ułóż osobno foldery z Events No. i CumEn, aby "
                    "wspólna skala rzędu miała sens."
                    .format(row_index + 1, " | ".join(sorted(row_axis_labels)))
                )

            row_cumulative_ends[row_index] = max(
                1.0,
                max(
                    comparison_variant_values(
                        item["sample"],
                        item["variant"],
                    )["cumulative_final"]
                    for item in row_items
                ) * 1.05,
            )
        else:
            row_cumulative_ends[row_index] = 1.0

    panels = []

    for index, item in enumerate(comparison_items):
        sample_info = item["sample"]
        token = item["token"]
        visual = comparison_variant_values(
            sample_info,
            item["variant"],
        )

        row, column, x1, x2, y1, y2 = comparison_panel_position(
            index,
            len(comparison_items),
        )

        add_comparison_panel_label(
            sheet,
            item["display_label"],
            token,
            x1,
            x2,
            y1,
            row + 1,
            column + 1,
            warnings,
        )

        safe_token = re.sub(r"[^A-Za-z0-9_]+", "_", token).strip("_")

        axis_system = sheet.Objects.Add(
            dd.eReportObject2DAxisSystem,
            report_object_name(
                "Comparison_{0}_{1}".format(index + 1, safe_token)
            ),
        )
        remove_object_frame(
            axis_system,
            warnings,
            "Comparison axis system " + token,
        )

        position = axis_system.Position.ByCoordinate
        position.X1 = x1
        position.X2 = x2
        position.Y1 = y1
        position.Y2 = y2

        # Token próbki może powtarzać się na stronie porównawczej.
        # DIAdem wymaga unikalnych nazw osi pomocniczych, dlatego do nazw osi
        # dodajemy numer panelu. Bez tego powtórzony panel kontrolny powoduje
        # kolizję typu: StressAxis_316LLN1510_filtered already exists.
        comparison_axis_suffix = "{0}_{1}".format(index + 1, safe_token)

        frequency_axis = axis_system.YAxis
        stress_axis = axis_system.YAxisList.Add(
            report_object_name(
                "StressAxis_" + comparison_axis_suffix
            )
        )
        cumulative_axis = axis_system.YAxisList.Add(
            report_object_name(
                "CumulativeAxis_" + comparison_axis_suffix
            )
        )

        configure_axis_offsets(
            axis_system,
            frequency_axis,
            stress_axis,
            cumulative_axis,
            warnings,
        )

        configure_manual_scale(
            axis_system.XAxis.Scaling,
            0.0,
            visual["x_end_s"],
            warnings,
            "Comparison {0} / Time".format(token),
        )
        configure_manual_scale(
            frequency_axis.Scaling,
            FREQUENCY_MIN_KHZ,
            FREQUENCY_MAX_KHZ,
            warnings,
            "Comparison {0} / Frequency".format(token),
        )
        configure_manual_scale(
            stress_axis.Scaling,
            comparison_stress_low,
            comparison_stress_high,
            warnings,
            "Comparison {0} / Stress".format(token),
        )
        configure_manual_scale(
            cumulative_axis.Scaling,
            0.0,
            row_cumulative_ends[row],
            warnings,
            "Comparison {0} / Cumulative".format(token),
        )

        set_comparison_axis_texts(
            axis_system,
            frequency_axis,
            stress_axis,
            cumulative_axis,
            row,
            column,
            visual["cumulative_axis_label"],
            warnings,
        )
        disable_grid(axis_system, warnings)
        configure_comparison_background(
            axis_system,
            visual,
            warnings,
            token,
        )

        stress_curve = add_curve(
            axis_system,
            "Stress_curve_{0}_{1}".format(index + 1, safe_token),
            sample_info["group_index"],
            "Time specimen",
            "Stress",
            stress_axis.Name,
        )
        cumulative_curve = add_curve(
            axis_system,
            "Cumulative_curve_{0}_{1}".format(index + 1, safe_token),
            sample_info["group_index"],
            visual["count_x_channel"],
            visual["count_y_channel"],
            cumulative_axis.Name,
        )

        overlay_curves = []
        for overlay_index, overlay in enumerate(
            visual.get("overlay_curves", []),
            start=1,
        ):
            overlay_curves.append(
                add_curve(
                    axis_system,
                    "Cumulative_overlay_{0}_{1}_{2}".format(
                        index + 1,
                        safe_token,
                        overlay_index,
                    ),
                    sample_info["group_index"],
                    overlay["time_channel"],
                    overlay["cumulative_channel"],
                    cumulative_axis.Name,
                )
            )

        apply_min_curve_width(
            stress_curve,
            warnings,
            "Comparison Stress curve " + token,
        )
        apply_min_curve_width(
            cumulative_curve,
            warnings,
            "Comparison Cumulative curve " + token,
        )
        for overlay_curve in overlay_curves:
            apply_min_curve_width(
                overlay_curve,
                warnings,
                "Comparison Cumulative overlay curve " + token,
            )
        apply_curve_color(
            stress_curve,
            dd.eColorIndexBlack,
            warnings,
            "Comparison Stress curve color " + token,
        )
        apply_curve_color(
            cumulative_curve,
            dd.eColorIndexDarkBlue,
            warnings,
            "Comparison Cumulative curve color " + token,
        )
        overlay_color = resolve_constant(
            "eColorIndexRed",
            "eColorIndexOrange",
            fallback=dd.eColorIndexBlack,
        )
        for overlay_curve in overlay_curves:
            apply_curve_color(
                overlay_curve,
                overlay_color,
                warnings,
                "Comparison Cumulative overlay curve color " + token,
            )

        panels.append(
            {
                "token": token,
                "sample_id": item["sample_id"],
                "variant": item["variant"],
                "row": row + 1,
                "column": column + 1,
                "axis_object_name": axis_system.Name,
                "x_end_s": visual["x_end_s"],
                "cumulative_axis_end": row_cumulative_ends[row],
            }
        )

    return {
        "sheet_name": sheet.Name,
        "panel_count": len(panels),
        "stress_axis_min": comparison_stress_low,
        "stress_axis_max": comparison_stress_high,
        "panels": panels,
        "warnings": warnings,
    }


def comparison_page_name(page_items, name_counts):
    """
    Nazwa strony opisuje jej zawartość:
      tylko standard -> Porównanie
      tylko filtered -> Porównanie_filtered
      mieszana       -> Porównanie_mixed
    """
    variants = {item["variant"] for item in page_items}

    if variants == {"standard"}:
        base_name = COMPARISON_SHEET_NAME
    elif variants == {"filtered"}:
        base_name = COMPARISON_SHEET_NAME + FILTERED_SHEET_SUFFIX
    else:
        base_name = COMPARISON_SHEET_NAME + "_mixed"

    name_counts[base_name] = name_counts.get(base_name, 0) + 1
    page_number = name_counts[base_name]

    return (
        base_name
        if page_number == 1
        else base_name + "_" + str(page_number)
    )


def build_comparison_sheets(comparison_items):
    """
    Dzieli pełną kolejność z pliku TXT na strony po dziewięć paneli (3×3).
    """
    pages = []
    name_counts = {}

    for start_index in range(0, len(comparison_items), COMPARISON_PANELS_PER_PAGE):
        chunk = comparison_items[
            start_index:start_index + COMPARISON_PANELS_PER_PAGE
        ]
        sheet_name = comparison_page_name(chunk, name_counts)
        pages.append(build_comparison_sheet_page(chunk, sheet_name))

    warnings = []
    panel_count = 0
    for page in pages:
        panel_count += page.get("panel_count", 0)
        warnings.extend(page.get("warnings", []))

    return {
        "sheet_name": " / ".join(
            page.get("sheet_name", "") for page in pages
        ),
        "page_count": len(pages),
        "panel_count": panel_count,
        "order": [item["token"] for item in comparison_items],
        "pages": pages,
        "warnings": warnings,
    }


# ============================= LOGI I WYJŚCIE ================================

def build_log_record(sample_info, report_info):
    return {
        "sample_id": sample_info["sample_id"],
        "variant": "standard",
        "status": "OK",
        "group_index": sample_info["group_index"],
        "geometry_sample_label": sample_info["geometry_sample_label"],
        "area_mm2": sample_info["mechanical"]["area_mm2"],
        "offset_ae_to_mech_s": sample_info["mechanical"]["offset_ae_to_mech_s"],
        "mechanical_time_offset_s": sample_info["mechanical"]["mechanical_time_offset_s"],
        "ae_zero_time_s": sample_info["ae"]["ae_zero_time_s"],
        "mechanical_channel_count": sample_info["mechanical"].get(
            "mechanical_channel_count", ""
        ),
        "mechanical_rows": sample_info["mechanical"]["mechanical_rows"],
        "ae_rows": sample_info["ae"]["ae_rows"],
        "ae_count_rows": sample_info["ae"]["ae_count_rows"],
        "ae_source_mode": sample_info["ae"]["source_mode"],
        "cumulative_channel": sample_info["ae"]["report_cumulative_channel"],
        "cumulative_axis_label": sample_info["ae"]["report_axis_label"],
        "calibration_tail_margin_s": sample_info["ae"]["calibration_tail_margin_s"],
        "calibration_baseline_requested_time_s": sample_info["ae"]["calibration_baseline_requested_time_s"],
        "calibration_baseline_time_s": sample_info["ae"]["calibration_baseline_time_s"],
        "calibration_baseline_counts": sample_info["ae"]["calibration_baseline_counts"],
        "calibration_baseline_rows": sample_info["ae"]["calibration_baseline_rows"],
        "cumulative_ea_final": sample_info["ae"]["cumulative_ea_final"],
        "overlay_curves": "; ".join(
            curve["kind"]
            for curve in sample_info["ae"].get("overlay_curves", [])
        ),
        "ae_4000_main": sample_info.get("ae_4000_selection", {}).get(
            "main_4000",
            "",
        ),
        "ae_4000_all": " | ".join(
            sample_info.get("ae_4000_selection", {}).get("all_4000", [])
        ),
        "ae_4000_overlay": " | ".join(
            sample_info.get("ae_4000_selection", {}).get("overlay_4000", [])
        ),
        "time_specimen_end_s": sample_info["mechanical"]["time_specimen_end_s"],
        "time_ae_end_s": sample_info["ae"]["ae_time_end_s"],
        "tiff_end_s": sample_info["tiff_end_s"],
        "x_end_s": sample_info["x_end_s"],
        "tiff_path": sample_info["tiff_path"],
        "tiff_png_count": sample_info["tiff_png_count"],
        "tiff_last_segment_index": sample_info["tiff_last_segment_index"],
        "tiff_last_segment_seconds": sample_info["tiff_last_segment_seconds"],
        "report_sheet": report_info["sheet_name"],
        "report_axis_object": report_info["axis_object_name"],
        "filtered_counts_csv_path": "",
        "filtered_counts_source_column": "",
        "filtered_counts_is_cumulative": "",
        "warnings": " | ".join(
            sample_info["tiff_warnings"] + report_info["warnings"]
        ),
        "message": "",
    }


def build_filtered_log_record(sample_info, report_info):
    """
    Osobny rekord manifestu dla karty *_filtered.

    Jest celowo osobny, bo kolejne etapy (06 PPT) rozpoznają tytuł slajdu
    po report_sheet. Dzięki temu filtered karta odziedziczy tę samą geometrię
    próbki zamiast dostać techniczną nazwę arkusza.
    """
    filtered = sample_info["filtered"]
    record = build_log_record(sample_info, report_info)

    record["sample_id"] = sample_info["sample_id"] + FILTERED_SHEET_SUFFIX
    record["variant"] = "filtered"
    record["ae_rows"] = filtered["rows"]
    record["ae_count_rows"] = filtered["rows"]
    record["ae_zero_time_s"] = filtered["baseline_time_s"]
    record["calibration_baseline_requested_time_s"] = (
        filtered["baseline_requested_time_s"]
    )
    record["calibration_baseline_time_s"] = filtered["baseline_time_s"]
    record["calibration_baseline_counts"] = filtered["baseline_counts"]
    record["calibration_baseline_rows"] = filtered["baseline_rows"]
    record["cumulative_ea_final"] = filtered["cumulative_final"]
    record["time_ae_end_s"] = filtered["time_end_s"]
    record["tiff_end_s"] = filtered["tiff_end_s"]
    record["x_end_s"] = filtered["x_end_s"]
    record["tiff_path"] = filtered["tiff_path"]
    record["tiff_png_count"] = ""
    record["tiff_last_segment_index"] = ""
    record["tiff_last_segment_seconds"] = ""
    record["filtered_counts_csv_path"] = filtered["source_csv_path"]
    record["filtered_counts_source_column"] = filtered["source_column"]
    record["filtered_counts_is_cumulative"] = filtered["source_is_cumulative"]

    return record


def build_error_record(sample_id, exc):
    return {
        "sample_id": sample_id,
        "variant": "",
        "status": "BLAD",
        "group_index": "",
        "geometry_sample_label": "",
        "area_mm2": "",
        "offset_ae_to_mech_s": "",
        "mechanical_time_offset_s": "",
        "ae_zero_time_s": "",
        "mechanical_channel_count": "",
        "mechanical_rows": "",
        "ae_rows": "",
        "ae_count_rows": "",
        "ae_source_mode": "",
        "cumulative_channel": "",
        "cumulative_axis_label": "",
        "calibration_tail_margin_s": "",
        "calibration_baseline_requested_time_s": "",
        "calibration_baseline_time_s": "",
        "calibration_baseline_counts": "",
        "calibration_baseline_rows": "",
        "cumulative_ea_final": "",
        "ae_4000_main": "",
        "ae_4000_all": "",
        "ae_4000_overlay": "",
        "time_specimen_end_s": "",
        "time_ae_end_s": "",
        "tiff_end_s": "",
        "x_end_s": "",
        "tiff_path": "",
        "tiff_png_count": "",
        "tiff_last_segment_index": "",
        "tiff_last_segment_seconds": "",
        "report_sheet": "",
        "report_axis_object": "",
        "filtered_counts_csv_path": "",
        "filtered_counts_source_column": "",
        "filtered_counts_is_cumulative": "",
        "warnings": "",
        "message": "{0}: {1}".format(type(exc).__name__, exc),
    }


def write_logs(
    records,
    selected_samples,
    comparison_info=None,
    comparison_order=None,
):
    # comparison_order jest lokalne dla main(), dlatego musi być przekazane
    # jawnie; write_logs nie może odwoływać się do niego jako do globalnego.
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    fields = [
        "sample_id",
        "variant",
        "status",
        "group_index",
        "geometry_sample_label",
        "area_mm2",
        "offset_ae_to_mech_s",
        "mechanical_time_offset_s",
        "ae_zero_time_s",
        "mechanical_channel_count",
        "mechanical_rows",
        "ae_rows",
        "ae_count_rows",
        "ae_source_mode",
        "cumulative_channel",
        "cumulative_axis_label",
        "calibration_tail_margin_s",
        "calibration_baseline_requested_time_s",
        "calibration_baseline_time_s",
        "calibration_baseline_counts",
        "calibration_baseline_rows",
        "cumulative_ea_final",
        "overlay_curves",
        "ae_4000_main",
        "ae_4000_all",
        "ae_4000_overlay",
        "time_specimen_end_s",
        "time_ae_end_s",
        "tiff_end_s",
        "x_end_s",
        "tiff_path",
        "tiff_png_count",
        "tiff_last_segment_index",
        "tiff_last_segment_seconds",
        "report_sheet",
        "report_axis_object",
        "filtered_counts_csv_path",
        "filtered_counts_source_column",
        "filtered_counts_is_cumulative",
        "warnings",
        "message",
    ]

    with LOG_CSV_PATH.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter=";", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)

    lines = [
        "UNIWERSALNY GENERATOR DS — DIAdem",
        "Data: {0}".format(now_text()),
        "Root: {0}".format(ROOT_DIR),
        "Próbki: {0}".format(", ".join(selected_samples)),
        "",
        "Oś czasu:",
        "  Time AE = bez zmian (referencja AE/TIFF).",
        "  Time specimen = Time specimen raw - offset_AE_to_mech_s.",
        "",
        "Osie reportu:",
        "  Lewa 1: Frequency [kHz], 0–125; TIFF.",
        "  Lewa 2: Stress [MPa].",
        "  Prawa:  Cumulative EA counts albo Cumulative EA energy, zależnie od źródła.",
        "",
    ]

    for record in records:
        lines.append("[{0}] {1}".format(record["status"], record["sample_id"]))
        for key in fields[1:]:
            if record.get(key, "") != "":
                lines.append("  {0}: {1}".format(key, record[key]))
        lines.append("")

    LOG_TXT_PATH.write_text("\n".join(lines), encoding="utf-8")

    manifest = {
        "generated_at": now_text(),
        "root": str(ROOT_DIR),
        "files": {
            "tdm": str(DATA_TDM_PATH),
            "tdr": str(LAYOUT_TDR_PATH),
            "log_csv": str(LOG_CSV_PATH),
            "log_txt": str(LOG_TXT_PATH),
        },
        "axis_layout": {
            "left_axis_1": "Frequency [kHz], 0–125; background TIFF",
            "left_axis_2": "Stress [MPa]",
            "right_axis": "Cumulative EA counts lub Cumulative EA energy; zależnie od źródła",
            "x_axis": "Time [s]; Time AE is native / Time specimen is offset",
        },
        "comparison_order": {
            "path": str(comparison_order_path()),
            "order": list(comparison_order or []),
            "kind": "individual folders containing AE; -filtered is a separate sample",
        },
        "filtered_pipeline": {
            "enabled": False,
            "mode": "disabled in V22; folders - filtered are independent samples",
            "batch_runner": str(ROOT_DIR / FILTERED_BATCH_RUNNER_NAME),
            "batch_wrapper": str(ROOT_DIR / FILTERED_BATCH_WRAPPER_NAME),
            "output_dir_name": FILTERED_OUTPUT_DIR_NAME,
            "tiff_name": FILTERED_TIFF_NAME,
            "counts_csv_name": FILTERED_COUNTS_CSV_NAME,
            "sheet_suffix": FILTERED_SHEET_SUFFIX,
        },
        "comparison_sheet": comparison_info or {},
        "records": records,
    }
    atomic_write_json(MANIFEST_DS_PATH, manifest)


def save_final_artifacts():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if SAVE_TDM_DATA:
        # TDM może składać się z pliku opisowego i pliku binarnego.
        # Usuwamy typowe poprzednie artefakty, by nie zostawić starego
        # zestawu danych przy ponownym uruchomieniu.
        for path in (
            DATA_TDM_PATH,
            DATA_TDM_PATH.with_suffix(".tdx"),
            DATA_TDM_PATH.with_suffix(".tdms"),
        ):
            safe_unlink(path)

        dd.DataFileSave(str(DATA_TDM_PATH), "TDM")

    if SAVE_TDR_LAYOUT:
        safe_unlink(LAYOUT_TDR_PATH)
        dd.Report.SaveLayout(str(LAYOUT_TDR_PATH))

    if EXPORT_EACH_SHEET_TO_PDF:
        pdf_dir = OUTPUT_DIR / "PDF"
        pdf_dir.mkdir(exist_ok=True)

        for sheet in dd.Report.Sheets:
            sheet.Activate()
            pdf_path = pdf_dir / (sheet.Name + ".pdf")
            safe_unlink(pdf_path)
            sheet.ExportToPDF(str(pdf_path))


def write_emergency_log(message):
    """
    Minimalny log awaryjny: zapisuje tam, gdzie użytkownik na pewno go znajdzie,
    nawet jeśli standardowy log Output\DS nie powstał.
    """
    text = (
        "05_generuj_DS_v41_wymus_CumEn_i_overlay_4000 — LOG AWARYJNY\n"
        "Data: {0}\n"
        "Root: {1}\n"
        "\n{2}\n"
    ).format(now_text(), ROOT_DIR, message)

    try:
        EMERGENCY_LOG_PATH.write_text(text, encoding="utf-8")
    except Exception:
        pass

    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUTPUT_DIR / "HLP_w_LN_DS_AWARIA.txt").write_text(text, encoding="utf-8")
    except Exception:
        pass


# ================================== MAIN =====================================

def main():
    if not ROOT_DIR.is_dir():
        raise RuntimeError("ROOT_DIR nie istnieje: {0}".format(ROOT_DIR))

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    geometry_manifest = load_geometry_manifest()
    approvals = load_sync_approvals()
    all_samples = discover_samples(ROOT_DIR)
    selected_samples, unapproved = select_samples(all_samples, approvals)

    # Każdy katalog z AE/ jest samodzielną próbką, również - filtered.
    # Nie uruchamiamy ani nie odczytujemy FOscy jako dodatkowej gałęzi.
    global FILTERED_REPORT_ENABLED
    FILTERED_REPORT_ENABLED = False
    filtered_artifacts = None

    comparison_available_items = build_available_comparison_items(
        selected_samples
    )
    comparison_order = choose_comparison_order(
        comparison_available_items
    )

    selected_samples = sample_ids_from_comparison_order(
        comparison_order,
        comparison_available_items,
    )

    print("=" * 78)
    print("FINALNY GENERATOR DS")
    print("Root: {0}".format(ROOT_DIR))
    print("Próbki importowane z listy kolejności: {0}".format(
        ", ".join(selected_samples)
    ))
    print("Kolejność porównania: {0}".format(", ".join(comparison_order)))
    print("Tryb Cumulative EA: {0}".format(
        "baseline-subtracted"
        if SUBTRACT_AE_BASELINE_FOR_REPORT
        else "raw z *_4000.txt / kumulacja eventsNo bez odejmowania baseline"
    ))
    print("Time AE: bez zmian | Time specimen: przesunięty na oś AE/TIFF.")
    print(
        "Model folderów: każdy katalog z AE/ jest odrębną próbką; "
        "foldery - filtered nie są wariantami technicznymi."
    )
    print("=" * 78)

    # Najpierw przygotowujemy wszystkie dane.
    # Nie budujemy reportu, dopóki wszystkie wskazane importy nie przejdą.
    prepared_samples = []
    records = []

    if CLEAR_DATA_PORTAL_AT_START:
        dd.Data.Root.Clear()

    for sample_id in selected_samples:
        try:
            sample_info = import_prepare_one_sample(
                sample_id,
                geometry_manifest,
                approvals,
                filtered_artifacts=filtered_artifacts,
            )
            prepared_samples.append(sample_info)

            print("\n[DATA OK] {0}".format(sample_id))
            print(
                "  A0={0:.4f} mm² | offset Time specimen={1:+.6f} s".format(
                    sample_info["mechanical"]["area_mm2"],
                    sample_info["mechanical"]["mechanical_time_offset_s"],
                )
            )
            print(
                "  Marker synchronizacji AE={0:.3f} s | Cumulative final={1:g}".format(
                    sample_info["ae"]["ae_zero_time_s"],
                    sample_info["ae"]["cumulative_ea_final"],
                )
            )
            print(
                "  TIFF end={0:.3f} s | X end={1:.3f} s".format(
                    sample_info["tiff_end_s"],
                    sample_info["x_end_s"],
                )
            )

        except Exception as exc:
            records.append(build_error_record(sample_id, exc))
            print("\n[DATA BŁĄD] {0}".format(sample_id))
            print("  {0}: {1}".format(type(exc).__name__, exc))
            print(traceback.format_exc())

    if len(prepared_samples) != len(selected_samples):
        write_logs(
            records,
            selected_samples,
            comparison_info={},
            comparison_order=comparison_order,
        )
        raise RuntimeError(
            "Przerwano przed tworzeniem REPORT, bo co najmniej jedna próbka "
            "nie przeszła importu. Sprawdź:\n{0}".format(LOG_TXT_PATH)
        )

    # Każdy wpis popupu staje się jednym panelem; standard i filtered
    # mogą leżeć obok siebie na tej samej stronie.
    ordered_comparison_items = reorder_prepared_comparison_items(
        prepared_samples,
        comparison_order,
        comparison_available_items,
    )

    # Dopiero teraz budujemy layout.
    if REPLACE_CURRENT_REPORT_LAYOUT:
        dd.Report.NewLayout()

    # Przyspiesza tworzenie wielu arkuszy.
    try:
        dd.Report.Settings.Page.LockUpdate = True
    except Exception:
        pass

    comparison_info = {}
    try:
        first_report_sheet = True

        for sample_info in prepared_samples:
            report_info = build_report_sheet(
                sample_info,
                is_first_sheet=first_report_sheet,
                variant="standard",
            )
            first_report_sheet = False
            records.append(build_log_record(sample_info, report_info))

            print("\n[REPORT OK] {0} -> zakładka {1}".format(
                sample_info["sample_id"],
                report_info["sheet_name"],
            ))
            for warning in report_info["warnings"]:
                print("  UWAGA: {0}".format(warning))

            # Bezpośrednio po stronie standardowej pojawia się filtered.
            if sample_info.get("filtered") is not None:
                filtered_report_info = build_report_sheet(
                    sample_info,
                    is_first_sheet=False,
                    variant="filtered",
                )
                records.append(
                    build_filtered_log_record(
                        sample_info,
                        filtered_report_info,
                    )
                )

                print("[REPORT OK] {0} -> zakładka {1}".format(
                    sample_info["sample_id"] + FILTERED_SHEET_SUFFIX,
                    filtered_report_info["sheet_name"],
                ))
                for warning in filtered_report_info["warnings"]:
                    print("  UWAGA: {0}".format(warning))

        comparison_info = {}
        if GENERATE_COMPARISON_SHEET:
            comparison_info = build_comparison_sheets(
                ordered_comparison_items
            )

            print("\n[REPORT OK] {0} -> {1} paneli na {2} stronie/stronach".format(
                comparison_info["sheet_name"],
                comparison_info["panel_count"],
                comparison_info["page_count"],
            ))
            for warning in comparison_info.get("warnings", []):
                print("  UWAGA: {0}".format(warning))


    finally:
        try:
            dd.Report.Settings.Page.LockUpdate = False
        except Exception:
            pass

    dd.Report.Refresh()

    # Zapis danych/layoutu po udanym zakończeniu tworzenia stron.
    save_final_artifacts()
    write_logs(
        records,
        selected_samples,
        comparison_info=comparison_info,
        comparison_order=comparison_order,
    )

    # Otwiera REPORT i aktywuje pierwszą próbkę.
    try:
        dd.WndOpen("REPORT")
        dd.Report.Sheets(1).Activate()
    except Exception:
        pass

    print("\n" + "=" * 78)
    print("GOTOWE")
    print("Dane TDM: {0}".format(DATA_TDM_PATH if SAVE_TDM_DATA else "pominięto"))
    print("Layout TDR: {0}".format(LAYOUT_TDR_PATH if SAVE_TDR_LAYOUT else "pominięto"))
    print("Manifest: {0}".format(MANIFEST_DS_PATH))
    if comparison_info:
        print(
            "Arkusze porównawcze: {0} ({1} paneli, {2} stron)".format(
                comparison_info.get("sheet_name", ""),
                comparison_info.get("panel_count", 0),
                comparison_info.get("page_count", 0),
            )
        )

    print("Log: {0}".format(LOG_TXT_PATH))
    print("=" * 78)


try:
    main()
except Exception as error:
    tb = traceback.format_exc()
    message = "{0}: {1}\n\n{2}".format(type(error).__name__, error, tb)
    write_emergency_log(message)

    print("\n" + "=" * 78)
    print("BŁĄD KOŃCOWY")
    print("{0}: {1}".format(type(error).__name__, error))
    print(tb)
    print("Log awaryjny:")
    print("  {0}".format(EMERGENCY_LOG_PATH))
    print("  {0}".format(OUTPUT_DIR / "HLP_w_LN_DS_AWARIA.txt"))
    print("=" * 78)
    raise
