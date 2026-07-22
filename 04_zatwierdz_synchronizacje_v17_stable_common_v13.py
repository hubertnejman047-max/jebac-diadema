#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
04_zatwierdz_synchronizacje_v17_stable_common_v13.py

INTERAKTYWNE ZATWIERDZANIE SYNCHRONIZACJI MECHANIKA ↔ AE.

Uruchamiaj zwykłym Pythonem z folderu Pomiary:
    py 04_zatwierdz_synchronizacje.py

Tylko jedna próbka:
    py 04_zatwierdz_synchronizacje_v6_foldery_AE_CumEn.py --sample 0FFF45_1310_filtered

Ponowne przejrzenie już zatwierdzonych próbek:
    py 04_zatwierdz_synchronizacje.py --redo

Szersze / węższe okno kontroli:
    py 04_zatwierdz_synchronizacje.py --window 50

Szukanie pików dalej niż pierwsze 180 s:
    py 04_zatwierdz_synchronizacje.py --search-end 300

Co robi program:
  1. Pobiera A0 bezpośrednio przez pipeline_lokalny_common.py.
  2. Czyta specimen.dat i oblicza Stress [MPa].
  3. Czyta AE\\*_4000.txt.
     - gdy plik ma Events No., piki są wykrywane po liczbie zliczeń;
     - gdy plik ma wyłącznie CumEn, piki są wykrywane po dyskretnej
       pochodnej d(CumEn)/dt.
  4. Wykrywa kandydatów na 1–3 blisko położone piki synchronizacyjne.
     Kandydaci są pokazywani chronologicznie od wczesnych pakietów AE,
     a nie według samego matematycznego podobieństwa odstępów.
  5. Dla każdego kandydata pokazuje:
       - Stress po przesunięciu na natywną oś Time AE,
       - Events No. na natywnej osi Time AE,
       - 50-sekundowe okno wokół wybranej sekwencji pików,
       - pełny przebieg AE jako kontekst; duży późny pik może wskazywać
         zerwanie, ale NIE jest używany do synchronizacji.
  6. Po zatwierdzeniu zapisuje offset oraz początek liczenia
     Cumulative EA counts.

Zasada osi czasu w całym projekcie:
  Time AE zostaje bez zmian, bo do niego przypisane jest tło TIFF.
  Time specimen jest przesuwany na oś AE:

      Time specimen = Time specimen raw - offset_AE_to_mech

Przykład:
  offset_AE_to_mech = -4,61113285 s
  Time specimen = Time specimen raw + 4,61113285 s

Sterowanie w oknie:
  Y / Enter / przycisk TAK       zatwierdź aktualnego kandydata
  N / → / przycisk NASTĘPNY      następny kandydat
  P / ← / przycisk POPRZEDNI     poprzedni kandydat
  ↑                              zwiększ margines po ostatnim piku o 0,1 s
  ↓                              zmniejsz margines po ostatnim piku o 0,1 s
  R                              przywróć domyślny margines 0,3 s
  X / przycisk AUTOMAT           wróć do pełnej automatycznej listy kandydatów
  Mysz: przeciągnij po panelu AE ręcznie wskazany pakiet impulsów;
                                 kandydaci będą budowani tylko z tego zakresu
  Dolna mapa: lewy klik          przesuń oglądany wycinek bez zmiany synchronizacji
  Dolna mapa: kółko ↑ / ↓        przewijaj oglądany wycinek wcześniej / później
  S / przycisk POMIŃ             pomiń próbkę, bez synchronizacji
  Q / przycisk ZAKOŃCZ           zapisz dotychczasowe decyzje i zakończ

Wyniki:
  Output\\Synchronizacja\\sync_parametry_zatwierdzone.json
  Output\\Synchronizacja\\sync_decyzje.csv
  Output\\Synchronizacja\\sync_kandydaci_do_zatwierdzenia.csv
  Output\\Synchronizacja\\potwierdzenie_<próbka>.png
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

# V17: nie importujemy pipeline_lokalny_common.py, bo lokalnie bywa
# odtwarzany/cofany przez starszy workflow. Używamy stałej kopii pod inną
# nazwą, leżącej obok tego skryptu.
import importlib.util

STABLE_COMMON_PATH = SCRIPT_DIR / "pipeline_lokalny_common_STABLE_v13_dwa_typy_tabel.py"
if not STABLE_COMMON_PATH.is_file():
    raise RuntimeError(
        "Brak wymaganego stabilnego modułu common:\n{0}\n\n"
        "Skopiuj do folderu serii plik "
        "pipeline_lokalny_common_STABLE_v13_dwa_typy_tabel.py."
        .format(STABLE_COMMON_PATH)
    )

_spec_common = importlib.util.spec_from_file_location(
    "pipeline_lokalny_common_STABLE_v13_dwa_typy_tabel",
    STABLE_COMMON_PATH,
)
_stable_common = importlib.util.module_from_spec(_spec_common)
_spec_common.loader.exec_module(_stable_common)

common_resolve_geometry = _stable_common.resolve_geometry
common_write_geometry_snapshot = _stable_common.write_geometry_snapshot

print("STABLE COMMON PATH:", STABLE_COMMON_PATH)
print(
    "STABLE COMMON VERSION:",
    getattr(_stable_common, "PIPELINE_LOKALNY_COMMON_VERSION", "BRAK"),
)

try:
    import matplotlib.pyplot as plt
    from matplotlib.widgets import Button, Slider, SpanSelector
except ImportError:
    print(
        "BŁĄD: Nie znaleziono biblioteki matplotlib.\n"
        "Zainstaluj ją jednym poleceniem:\n"
        "    py -m pip install matplotlib"
    )
    raise SystemExit(2)


# ================================ USTAWIENIA ================================

DEFAULT_SEARCH_START_S = 0.0
DEFAULT_SEARCH_END_S = 180.0
DEFAULT_SYNC_MAX_GAP_S = 3.0
DEFAULT_POST_SYNC_MARGIN_S = 0.30
DEFAULT_VIEW_WINDOW_S = 50.0

# Liczba kandydatów do obejrzenia dla jednej próbki.
MAX_CANDIDATES_PER_SAMPLE = 18

# Parametry automatycznego wyszukiwania pików.
STRESS_TOP_N = 45
AE_TOP_N = 45
STRESS_COLLAPSE_WINDOW_S = 0.20
AE_COLLAPSE_WINDOW_S = 0.30

SKIP_DIRECTORY_NAMES = {"output", "__pycache__"}

# Folder "... - filtered" jest odrębną próbą, ale dla geometrii może
# odziedziczyć mapping z odpowiadającego mu folderu bez końcówki.
FILTERED_FOLDER_SUFFIX_RE = re.compile(
    r"(?:\s*[-_]\s*filtered|\s+filtered)$",
    re.IGNORECASE,
)


# =============================== MODELE DANYCH ===============================

@dataclass
class Peak:
    source: str
    sample_id: str
    time_s: float
    value: float
    score: float
    rank: int = 0


@dataclass
class SyncCandidate:
    sample_id: str
    candidate_rank: int
    confidence: str
    score: float
    offset_ae_to_mech_s: float
    matched_peak_count: int
    stress_peaks: List[Peak]
    ae_peaks: List[Peak]
    residuals_s: List[float]
    pattern_rms_s: float


@dataclass
class DecisionRecord:
    sample_id: str
    status: str
    decision_at: str
    candidate_rank: str
    confidence: str
    offset_ae_to_mech_s: str
    mechanical_time_offset_s: str
    ae_zero_time_s: str
    mech_zero_time_s: str
    post_sync_margin_s: str
    matched_peak_count: str
    stress_peak_times_s: str
    ae_peak_times_s: str
    message: str


@dataclass
class AEData:
    """
    Ujednolicony opis sygnału AE używanego do synchronizacji.

    events:
      plik time[s] / eventsNo. / EAenergy — peak_signal = Events No.

    cumulative_energy:
      plik time[s] / CumEn — peak_signal = d(CumEn)/dt, aby pojedyncze
      skoki energii wróciły do postaci ostrych pików.
    """
    times: List[float]
    peak_signal: List[float]
    auxiliary_values: List[float]
    source_kind: str
    signal_label: str
    overview_label: str
    cumulative_label: str
    ae_path: str


# =============================== NARZĘDZIA I/O ===============================

def to_float(value: object) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return None


def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def atomic_write_json(path: Path, payload: dict) -> None:
    """Zapis odporny na zamknięcie programu w trakcie pracy."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(path)


def read_text_any_encoding(path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1250", "cp1252", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="latin-1", errors="replace")


def median(values: List[float]) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    return (values[middle - 1] + values[middle]) / 2.0


def quantile(values: List[float], q: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    index = (len(values) - 1) * q
    lower = int(math.floor(index))
    upper = int(math.ceil(index))
    if lower == upper:
        return values[lower]
    return values[lower] * (upper - index) + values[upper] * (index - lower)


def rms(values: List[float]) -> float:
    if not values:
        return 0.0
    return math.sqrt(sum(value * value for value in values) / len(values))


def robust_threshold(values: List[float], z: float, q: float) -> float:
    if not values:
        return 0.0
    med = median(values)
    mad = median([abs(value - med) for value in values])
    robust = med + z * 1.4826 * mad
    return max(robust, quantile(values, q))


def format_times(peaks: List[Peak]) -> str:
    return " | ".join("{0:.6f}".format(peak.time_s) for peak in peaks)


def format_values(values: List[float]) -> str:
    return " | ".join("{0:+.6f}".format(value) for value in values)


# ============================ ODCZYT DANYCH WEJŚCIOWYCH ======================


def _safe_sample_id(root: Path, sample_root: Path) -> str:
    """
    Znormalizowany identyfikator folderu:
      0FFF45_1310 - filtered -> 0FFF45_1310_filtered
    """
    root = root.resolve()
    sample_root = sample_root.resolve()

    raw = (
        root.name
        if sample_root == root
        else str(sample_root.relative_to(root))
    )

    result = re.sub(r"[^A-Za-z0-9_]+", "_", raw).strip("_")
    return result or sample_root.name


def build_sample_index(root: Path) -> Dict[str, Path]:
    """
    Każdy katalog z bezpośrednim podfolderem AE, specimen.dat i jednym
    *_4000.txt jest osobną próbką.

    Dotyczy zarówno katalogów bazowych, jak i nazw kończących się
    "- filtered".
    """
    root = root.resolve()
    roots = []

    for ae_dir in root.rglob("AE"):
        if not ae_dir.is_dir():
            continue

        try:
            relative_parts = [
                part.casefold()
                for part in ae_dir.relative_to(root).parts
            ]
        except ValueError:
            continue

        if any(
            part in SKIP_DIRECTORY_NAMES
            for part in relative_parts
        ):
            continue

        sample_root = ae_dir.parent

        try:
            find_one_specimen(sample_root)
            find_one_ae_txt(sample_root)
        except Exception as exc:
            # Ignorujemy wyłącznie katalogi AE, które nie wyglądają jak folder
            # próbki. Katalog z specimen.dat albo jakimkolwiek *_4000.txt
            # ma być widoczny jako błąd konfiguracji, nie cicho pomijany.
            has_specimen = any(
                path.is_file() and path.name.casefold() == "specimen.dat"
                for path in sample_root.rglob("*")
            )
            has_4000 = any(
                path.is_file()
                and path.name.casefold().endswith("_4000.txt")
                for path in (sample_root / "AE").rglob("*")
            )

            if has_specimen or has_4000:
                raise RuntimeError(
                    "Folder wygląda na próbkę, ale nie można wybrać danych "
                    "wejściowych: {0}\nSzczegół: {1}".format(
                        sample_root,
                        exc,
                    )
                )
            continue

        roots.append(sample_root.resolve())

    unique_roots = {
        str(path).casefold(): path
        for path in roots
    }

    if not unique_roots:
        raise RuntimeError(
            "Nie znaleziono żadnego folderu próbki zawierającego AE/, "
            "specimen.dat i *_4000.txt."
        )

    index = {}
    used_ids = {}

    for sample_root in sorted(
        unique_roots.values(),
        key=lambda path: str(path).casefold(),
    ):
        base_id = _safe_sample_id(root, sample_root)
        key = base_id.casefold()
        used_ids[key] = used_ids.get(key, 0) + 1

        sample_id = (
            base_id
            if used_ids[key] == 1
            else "{0}_{1}".format(base_id, used_ids[key])
        )

        index[sample_id] = sample_root

    return index


def base_folder_name_for_geometry(folder_name: str) -> str:
    """
    Zdejmuje wyłącznie końcówkę oznaczającą fizyczny folder filtrowany.

      0FFF45_1310 - filtered -> 0FFF45_1310
      1ctRR45 - filtered     -> 1ctRR45

    Używane tylko jako awaryjny fallback geometrii, gdy common nie ma
    bezpośredniego mapowania folderu "- filtered".
    """
    return FILTERED_FOLDER_SUFFIX_RE.sub(
        "",
        str(folder_name),
    ).strip()

def canonical_geometry_id(sample_id: str) -> str:
    """
    Do geometrii folder "X - filtered" może użyć rekordu folderu "X".
    To dotyczy wyłącznie A0/etykiety geometrii; synchronizacja i AE pozostają
    niezależne dla obu fizycznych folderów.
    """
    return FILTERED_FOLDER_SUFFIX_RE.sub("", sample_id).strip()


def _resolve_geometry_directly(sample_root: Path) -> dict:
    """
    Jedno źródło prawdy: pipeline_lokalny_common.py.

    Helper czyta bezpośrednio Excel wskazany w jego EXCEL_PATH; nie ma tu
    manifestu z audytu ani wymagania audit_excel_geometria.py.
    """
    specimen_path = find_one_specimen(sample_root)
    return common_resolve_geometry(sample_root, specimen_path)


def build_geometry_map(sample_index: Dict[str, Path]) -> Dict[str, dict]:
    """
    Buduje słownik geometrii bezpośrednio z pipeline_lokalny_common.py.

    Dla folderu "- filtered":
      1. próbujemy dopasowania bezpośredniego;
      2. gdy Excel nie zna takiego folderu, bierzemy geometrię folderu
         bazowego bez końcówki "- filtered".

    Wynik geometrii jest dodatkowo zapisywany przez helper do:
      <folder próbki>\\Output\\geometria_uzyta.json
    """
    result = {}

    for sample_id, sample_root in sample_index.items():
        direct_error = None

        try:
            geometry = _resolve_geometry_directly(sample_root)
        except Exception as exc:
            direct_error = exc
            base_folder_name = base_folder_name_for_geometry(
                sample_root.name
            )

            if base_folder_name == sample_root.name:
                raise RuntimeError(
                    "{0}: pipeline_lokalny_common.py nie dopasował geometrii "
                    "dla folderu {1}.\\n{2}".format(
                        sample_id,
                        sample_root,
                        exc,
                    )
                )

            base_root = sample_root.parent / base_folder_name
            if not base_root.is_dir():
                raise RuntimeError(
                    "{0}: nie dopasowano geometrii bezpośrednio ({1}); "
                    "nie istnieje folder bazowy {2}."
                    .format(sample_id, exc, base_root)
                )

            try:
                geometry = _resolve_geometry_directly(base_root)
            except Exception as base_exc:
                raise RuntimeError(
                    "{0}: nie dopasowano geometrii ani dla folderu "
                    "filtered ({1}), ani bazowego ({2})."
                    .format(sample_id, direct_error, base_exc)
                )

            geometry = dict(geometry)
            geometry["folder_id"] = sample_root.name
            geometry["geometry_inherited_from_folder"] = str(base_root)
            geometry["mapping_source"] = (
                str(geometry.get("mapping_source", ""))
                + " | geometria odziedziczona z folderu "
                + base_root.name
            )

        common_write_geometry_snapshot(sample_root, geometry)
        result[sample_id.casefold()] = geometry

    return result


def geometry_for_sample(
    sample_id: str,
    geometry_map: Dict[str, dict],
) -> dict:
    geometry = geometry_map.get(sample_id.casefold())

    if geometry is None:
        raise RuntimeError(
            "Brak wcześniej przygotowanej geometrii dla {0}.".format(
                sample_id
            )
        )

    return geometry


def resolve_requested_sample_id(
    requested: str,
    sample_index: Dict[str, Path],
) -> str:
    """Obsługuje zarówno nazwę folderu, jak i znormalizowany sample_id."""
    needle = str(requested).casefold()
    for sample_id, sample_root in sample_index.items():
        if (
            needle == sample_id.casefold()
            or needle == sample_root.name.casefold()
        ):
            return sample_id

    raise RuntimeError(
        "Nie znaleziono próbki {0}. Dostępne: {1}".format(
            requested,
            ", ".join(sample_index.keys()),
        )
    )


def find_one_specimen(sample_dir: Path) -> Path:
    found = sorted(
        [
            path for path in sample_dir.rglob("*")
            if path.is_file() and path.name.casefold() == "specimen.dat"
        ],
        key=lambda path: str(path).casefold(),
    )
    if len(found) != 1:
        raise RuntimeError(
            "Oczekiwano dokładnie jednego specimen.dat; znaleziono {0}: {1}".format(
                len(found), [str(path) for path in found]
            )
        )
    return found[0]


def find_one_ae_txt(sample_dir: Path) -> Path:
    """
    Wybiera właściwy główny przebieg AE.

    Priorytet:
      1. dokładnie CumEn_4000.txt — energia całej aktywności po filtracji;
      2. gdy go nie ma, dokładnie jeden dowolny *_4000.txt.

    CumEn6dB_4000.txt jest przebiegiem pomocniczym ponad próg 6 dB i nie
    powinien zastępować CumEn_4000.txt przy synchronizacji ani głównej energii.
    """
    ae_dir = sample_dir / "AE"
    if not ae_dir.is_dir():
        raise RuntimeError("Brak folderu AE: {0}".format(ae_dir))

    found = sorted(
        [
            path for path in ae_dir.rglob("*.txt")
            if path.name.casefold().endswith("_4000.txt")
        ],
        key=lambda path: str(path).casefold(),
    )

    preferred = [
        path for path in found
        if path.name.casefold() == "cumen_4000.txt"
    ]

    if len(preferred) == 1:
        return preferred[0]

    if len(preferred) > 1:
        raise RuntimeError(
            "Znaleziono więcej niż jeden CumEn_4000.txt w {0}: {1}".format(
                ae_dir,
                [str(path) for path in preferred],
            )
        )

    if len(found) == 1:
        return found[0]

    raise RuntimeError(
        "Nie można wybrać głównego *_4000.txt dla {0}. "
        "Znaleziono: {1}. Oczekiwano CumEn_4000.txt albo dokładnie jednego "
        "pliku *_4000.txt.".format(
            sample_dir.name,
            [str(path) for path in found],
        )
    )


def parse_specimen(specimen_path: Path, area_mm2: float) -> Tuple[List[float], List[float]]:
    """
    Odczyt tekstowego specimen.dat.

    Układ danych:
      kolumna 1 = Time [s]
      kolumna 2 = Axial displacement [mm]
      kolumna 3 = Axial force [N]
      kolumny 4–6 = temperatury

    Zwraca:
      time_mech_raw_s, stress_MPa
    """
    text = read_text_any_encoding(specimen_path)
    lines = text.splitlines()

    header_index = None
    for index, line in enumerate(lines):
        normalized = line.casefold()
        if normalized.startswith("time") and "force" in normalized:
            header_index = index
            break

    if header_index is None:
        raise RuntimeError(
            "Nie znaleziono nagłówka Time/Force w pliku {0}".format(specimen_path)
        )

    data_start = header_index + 2
    times = []
    stresses = []

    for line in lines[data_start:]:
        if not line.strip():
            continue

        fields = line.split()
        if len(fields) < 3:
            continue

        try:
            time_s = float(fields[0].replace(",", "."))
            force_n = float(fields[2].replace(",", "."))
        except ValueError:
            continue

        times.append(time_s)
        stresses.append(force_n / area_mm2)

    if len(times) < 10:
        raise RuntimeError(
            "Za mało poprawnych punktów po odczycie specimen.dat ({0}).".format(
                len(times)
            )
        )

    return times, stresses


def _normalise_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def discrete_cumulative_energy_rate(
    times: List[float],
    cumulative_energy: List[float],
    ae_path: Path,
) -> List[float]:
    """
    Pochodna wsteczna:

      rate[i] = (CumEn[i] - CumEn[i - 1]) / (t[i] - t[i - 1])

    Wartość jest przypisana do końca przedziału (time[i]), więc nagły wzrost
    skumulowanej energii wraca jako ostry pik w prawdziwym czasie zdarzenia.
    """
    if len(times) != len(cumulative_energy):
        raise RuntimeError(
            "Niezgodna długość czasu i CumEn w {0}.".format(ae_path)
        )

    if len(times) < 2:
        raise RuntimeError(
            "Za mało punktów CumEn do obliczenia pochodnej: {0}".format(
                ae_path
            )
        )

    rates = [0.0]

    for index in range(1, len(times)):
        dt = times[index] - times[index - 1]
        if dt <= 0:
            raise RuntimeError(
                "Czas CumEn nie jest rosnący w {0}, wiersz około {1}."
                .format(ae_path, index + 2)
            )

        increment = cumulative_energy[index] - cumulative_energy[index - 1]

        # CumEn powinno rosnąć. Niewielkie ujemne artefakty numeryczne
        # obcinamy do zera; faktyczny spadek oznacza uszkodzony plik.
        tolerance = max(
            1e-9,
            abs(cumulative_energy[index]) * 1e-12,
        )
        if increment < -tolerance:
            raise RuntimeError(
                "CumEn maleje w {0}, między wierszami {1} i {2}: "
                "{3:.6g}.".format(
                    ae_path,
                    index + 1,
                    index + 2,
                    increment,
                )
            )

        rates.append(max(0.0, increment) / dt)

    return rates


def parse_ae_txt(ae_path: Path) -> AEData:
    """
    Obsługiwane formaty *_4000.txt:

    A) zwykły AE:
       time[s]  eventsNo.  EAenergy [arb.units]

    B) przefiltrowany:
       time[s]  CumEn [arb.units]

    Format B nie ma liczby zliczeń. Do synchronizacji używa więc
    d(CumEn)/dt, natomiast oryginalne CumEn pozostaje osobno jako
    auxiliary_values.
    """
    text = read_text_any_encoding(ae_path)
    lines = text.splitlines()

    if not lines:
        raise RuntimeError("Pusty plik AE: {0}".format(ae_path))

    header_fields = lines[0].strip().split()
    header_key = _normalise_header(lines[0])

    if "time" not in header_key:
        raise RuntimeError(
            "Nieoczekiwany nagłówek AE w {0}: {1}".format(
                ae_path,
                lines[0],
            )
        )

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

    if not has_events and not has_cumen:
        raise RuntimeError(
            "Nie rozpoznano Events No. ani CumEn w nagłówku AE: {0}"
            .format(lines[0])
        )

    times = []
    col2 = []
    col3 = []
    invalid_rows = []

    for line_number, line in enumerate(lines[1:], start=2):
        if not line.strip():
            continue

        fields = line.split()

        required_columns = 3 if has_events else 2
        if len(fields) < required_columns:
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
            "Nieprawidłowe wiersze AE, np. {0}.".format(
                invalid_rows[:10]
            )
        )

    if len(times) < 10:
        raise RuntimeError(
            "Za mało poprawnych punktów po odczycie AE ({0}).".format(
                len(times)
            )
        )

    for index in range(1, len(times)):
        if times[index] <= times[index - 1]:
            raise RuntimeError(
                "Czas AE nie jest rosnący w {0}, wiersz około {1}."
                .format(ae_path, index + 2)
            )

    if has_events:
        return AEData(
            times=times,
            peak_signal=col2,
            auxiliary_values=col3,
            source_kind="events",
            signal_label="Events No. [events/0.1 s]",
            overview_label="log10(Events No. + 1)",
            cumulative_label="Cumulative EA counts",
            ae_path=str(ae_path),
        )

    cumulative_energy = col2
    energy_rate = discrete_cumulative_energy_rate(
        times,
        cumulative_energy,
        ae_path,
    )

    return AEData(
        times=times,
        peak_signal=energy_rate,
        auxiliary_values=cumulative_energy,
        source_kind="cumulative_energy",
        signal_label="d(CumEn)/dt [arb.units/s]",
        overview_label="log10(d(CumEn)/dt + 1)",
        cumulative_label="Cumulative EA energy [arb.units]",
        ae_path=str(ae_path),
    )


# ============================ WYKRYWANIE KANDYDATÓW ==========================

def in_window(time_s: float, start_s: float, end_s: Optional[float]) -> bool:
    if time_s < start_s:
        return False
    if end_s is not None and time_s > end_s:
        return False
    return True


def collapse_peaks(
    peaks: List[Peak],
    collapse_window_s: float,
    top_n: int,
) -> List[Peak]:
    """
    Łączy kilka próbek opisujących ten sam bardzo wąski impuls w jeden pik.
    Zostawia punkt z najwyższym score.
    """
    peaks = sorted(peaks, key=lambda peak: peak.time_s)
    clusters = []

    for peak in peaks:
        if not clusters:
            clusters.append([peak])
            continue

        if peak.time_s - clusters[-1][-1].time_s <= collapse_window_s:
            clusters[-1].append(peak)
        else:
            clusters.append([peak])

    collapsed = [max(cluster, key=lambda peak: peak.score) for cluster in clusters]
    collapsed = sorted(collapsed, key=lambda peak: peak.score, reverse=True)[:top_n]

    for rank, peak in enumerate(collapsed, start=1):
        peak.rank = rank

    return sorted(collapsed, key=lambda peak: peak.time_s)


def detect_stress_peaks(
    sample_id: str,
    times: List[float],
    stress: List[float],
    search_start_s: float,
    search_end_s: Optional[float],
) -> List[Peak]:
    """
    Uderzenia mechaniczne są lepiej widoczne na gwałtownej zmianie nachylenia
    Stress(t) niż na samej wartości Stress, która podczas próby może rosnąć.
    """
    scored = []

    for index in range(2, len(times)):
        time_s = times[index]
        if not in_window(time_s, search_start_s, search_end_s):
            continue

        dt_previous = times[index - 1] - times[index - 2]
        dt_current = times[index] - times[index - 1]
        if dt_previous <= 0 or dt_current <= 0:
            continue

        slope_previous = (stress[index - 1] - stress[index - 2]) / dt_previous
        slope_current = (stress[index] - stress[index - 1]) / dt_current
        curvature_score = abs(slope_current - slope_previous)

        scored.append((time_s, stress[index], curvature_score))

    if not scored:
        return []

    scores = [item[2] for item in scored]
    threshold = robust_threshold(scores, z=10.0, q=0.995)

    raw = [
        Peak(
            source="stress",
            sample_id=sample_id,
            time_s=time_s,
            value=stress_value,
            score=score,
        )
        for time_s, stress_value, score in scored
        if score >= threshold
    ]

    # Fallback: nawet gdy próg jest zbyt surowy, chcemy użytkownikowi pokazać
    # najlepsze możliwe kandydaty, zamiast twierdzić, że pików nie ma.
    if len(raw) < 5:
        raw = [
            Peak(
                source="stress",
                sample_id=sample_id,
                time_s=time_s,
                value=stress_value,
                score=score,
            )
            for time_s, stress_value, score in sorted(
                scored, key=lambda item: item[2], reverse=True
            )[:STRESS_TOP_N * 3]
        ]

    return collapse_peaks(raw, STRESS_COLLAPSE_WINDOW_S, STRESS_TOP_N)


def detect_stress_peaks_in_manual_window(
    sample_id: str,
    times: List[float],
    stress: List[float],
    left_s: float,
    right_s: float,
) -> List[Peak]:
    """
    Wykrywa piki Stress wyłącznie w ręcznie zaznaczonym przedziale.

    Przedział jest podawany w surowym czasie mechaniki. W GUI zaznaczenie
    odbywa się na mapie Stress przeliczonej na oś AE, dlatego caller musi
    zamienić:
        raw_time = ae_axis_time + offset_AE_to_mech_s
    """
    left_s, right_s = sorted((float(left_s), float(right_s)))

    return detect_stress_peaks(
        sample_id=sample_id,
        times=times,
        stress=stress,
        search_start_s=left_s,
        search_end_s=right_s,
    )


def detect_ae_peaks(
    sample_id: str,
    times: List[float],
    signal: List[float],
    auxiliary_values: List[float],
    source_kind: str,
    search_start_s: float,
    search_end_s: Optional[float],
) -> List[Peak]:
    """
    Wykrywa piki z właściwego sygnału:

      events              -> Events No.
      cumulative_energy   -> d(CumEn)/dt

    Dla CumEn nie używamy samego poziomu skumulowanej energii, bo byłby
    monotoniczny. Piki pochodnej odpowiadają rzeczywistym skokom energii.
    """
    values_in_window = [
        value
        for time_s, value in zip(times, signal)
        if in_window(time_s, search_start_s, search_end_s) and value > 0
    ]

    if not values_in_window:
        return []

    threshold = max(
        1e-12,
        robust_threshold(
            values_in_window,
            z=6.0,
            q=0.90,
        ),
    )

    raw = []
    peak_source = (
        "ae_energy_rate"
        if source_kind == "cumulative_energy"
        else "ae_events"
    )

    for time_s, value, auxiliary in zip(
        times,
        signal,
        auxiliary_values,
    ):
        if not in_window(time_s, search_start_s, search_end_s):
            continue
        if value <= 0:
            continue

        if source_kind == "events":
            score = value + math.log10(max(auxiliary, 1.0)) * 0.05
        else:
            # Sam d(CumEn)/dt jest fizycznym sygnałem pikowym.
            score = value

        if value >= threshold:
            raw.append(
                Peak(
                    source=peak_source,
                    sample_id=sample_id,
                    time_s=time_s,
                    value=value,
                    score=score,
                )
            )

    if len(raw) < 5:
        raw = [
            Peak(
                source=peak_source,
                sample_id=sample_id,
                time_s=time_s,
                value=value,
                score=(
                    value + math.log10(max(auxiliary, 1.0)) * 0.05
                    if source_kind == "events"
                    else value
                ),
            )
            for time_s, value, auxiliary in zip(
                times,
                signal,
                auxiliary_values,
            )
            if in_window(time_s, search_start_s, search_end_s) and value > 0
        ]

    raw = sorted(
        raw,
        key=lambda peak: peak.score,
        reverse=True,
    )[:AE_TOP_N * 3]

    return collapse_peaks(raw, AE_COLLAPSE_WINDOW_S, AE_TOP_N)


def detect_ae_peaks_in_manual_window(
    sample_id: str,
    times: List[float],
    signal: List[float],
    auxiliary_values: List[float],
    source_kind: str,
    left_s: float,
    right_s: float,
) -> List[Peak]:
    """
    Wykrywa piki WYŁĄCZNIE w przedziale zaznaczonym przez użytkownika.

    To ma pierwszeństwo nad rankingiem globalnym. W szczególności pakiet
    synchronizacyjny o umiarkowanej amplitudzie nie odpada tylko dlatego,
    że później istnieją dużo większe piki materiałowe.

    events:
      score = Events No. + niewielka korekta energią.

    cumulative_energy:
      score = d(CumEn)/dt, czyli sygnał pikowy odzyskany z CumEn.
    """
    left_s, right_s = sorted((float(left_s), float(right_s)))

    peak_source = (
        "ae_energy_rate"
        if source_kind == "cumulative_energy"
        else "ae_events"
    )

    raw = []

    for time_s, value, auxiliary in zip(
        times,
        signal,
        auxiliary_values,
    ):
        if not (left_s <= time_s <= right_s):
            continue
        if value <= 0:
            continue

        score = (
            value + math.log10(max(auxiliary, 1.0)) * 0.05
            if source_kind == "events"
            else value
        )

        raw.append(
            Peak(
                source=peak_source,
                sample_id=sample_id,
                time_s=time_s,
                value=value,
                score=score,
            )
        )

    if not raw:
        return []

    # W ręcznym obszarze zachowujemy większy limit niż w automacie globalnym.
    raw = sorted(
        raw,
        key=lambda peak: peak.score,
        reverse=True,
    )[:300]

    return collapse_peaks(
        raw,
        AE_COLLAPSE_WINDOW_S,
        60,
    )


def make_clusters(peaks: List[Peak], max_gap_s: float) -> List[List[Peak]]:
    """
    Z każdego zbioru pików tworzy sekwencje 2- i 3-pikowe o małych odstępach.
    To odwzorowuje maksymalnie trzy uderzenia synchronizacyjne.
    """
    peaks = sorted(peaks, key=lambda peak: peak.time_s)
    clusters = []

    for start in range(len(peaks)):
        sequence = [peaks[start]]

        for end in range(start + 1, len(peaks)):
            if peaks[end].time_s - sequence[-1].time_s > max_gap_s:
                break

            sequence.append(peaks[end])

            if 2 <= len(sequence) <= 3:
                clusters.append(list(sequence))

            if len(sequence) == 3:
                break

    clusters = sorted(
        clusters,
        key=lambda cluster: (
            len(cluster),
            sum(peak.score for peak in cluster),
        ),
        reverse=True,
    )

    return clusters[:60]


def subsequences(cluster: List[Peak], length: int) -> List[List[Peak]]:
    if length == len(cluster):
        return [cluster]

    if length == 1:
        return [[peak] for peak in cluster]

    if length == 2 and len(cluster) == 3:
        return [
            [cluster[0], cluster[1]],
            [cluster[0], cluster[2]],
            [cluster[1], cluster[2]],
        ]

    return []


def signature(peaks: List[Peak]) -> List[float]:
    return [
        peaks[index].time_s - peaks[index - 1].time_s
        for index in range(1, len(peaks))
    ]


def classify_confidence(
    matched_peak_count: int,
    residual_rms_s: float,
    pattern_rms_s: float,
) -> str:
    if (
        matched_peak_count >= 3
        and residual_rms_s <= 0.25
        and pattern_rms_s <= 0.35
    ):
        return "WYSOKA"

    if (
        matched_peak_count >= 2
        and residual_rms_s <= 0.35
        and pattern_rms_s <= 0.50
    ):
        return "SREDNIA"

    if matched_peak_count >= 2:
        return "NISKA"

    return "BARDZO_NISKA"


def match_candidates(
    sample_id: str,
    stress_peaks_raw_time: List[Peak],
    ae_peaks: List[Peak],
    max_gap_s: float,
) -> List[SyncCandidate]:
    """
    Definicja offsetu:
        czas_mechaniki = czas_AE + offset_AE_to_mech

    Do wykresu używamy później:
        Time specimen = Time specimen raw - offset_AE_to_mech
    """
    stress_clusters = make_clusters(stress_peaks_raw_time, max_gap_s)
    ae_clusters = make_clusters(ae_peaks, max_gap_s)

    # Fallback gdy automat nie zbudował sekwencji 2–3 pików.
    if not stress_clusters:
        stress_clusters = [
            [peak]
            for peak in sorted(
                stress_peaks_raw_time,
                key=lambda peak: peak.score,
                reverse=True,
            )[:10]
        ]

    if not ae_clusters:
        ae_clusters = [
            [peak]
            for peak in sorted(
                ae_peaks,
                key=lambda peak: peak.score,
                reverse=True,
            )[:10]
        ]

    candidates = []

    for stress_cluster in stress_clusters:
        for ae_cluster in ae_clusters:
            maximum_length = min(len(stress_cluster), len(ae_cluster), 3)

            for length in range(maximum_length, 0, -1):
                for stress_sequence in subsequences(stress_cluster, length):
                    for ae_sequence in subsequences(ae_cluster, length):
                        stress_times = [
                            peak.time_s for peak in stress_sequence
                        ]
                        ae_times = [peak.time_s for peak in ae_sequence]

                        offsets = [
                            stress_time - ae_time
                            for stress_time, ae_time in zip(
                                stress_times,
                                ae_times,
                            )
                        ]
                        offset = median(offsets)

                        residuals = [
                            (ae_time + offset) - stress_time
                            for stress_time, ae_time in zip(
                                stress_times,
                                ae_times,
                            )
                        ]
                        residual_rms = rms(residuals)

                        if length >= 2:
                            stress_signature = signature(stress_sequence)
                            ae_signature = signature(ae_sequence)
                            pattern_rms = rms(
                                [
                                    stress_gap - ae_gap
                                    for stress_gap, ae_gap in zip(
                                        stress_signature,
                                        ae_signature,
                                    )
                                ]
                            )
                        else:
                            pattern_rms = 999.0

                        # Mniejsze score = lepsze dopasowanie.
                        missing_penalty = (3 - length) * 0.75
                        tiny_rank_penalty = (
                            1.0
                            / max(
                                1.0,
                                sum(peak.score for peak in stress_sequence),
                            )
                            + 1.0
                            / max(
                                1.0,
                                sum(peak.score for peak in ae_sequence),
                            )
                        )
                        score = (
                            residual_rms
                            + 0.70 * pattern_rms
                            + missing_penalty
                            + tiny_rank_penalty
                        )

                        candidates.append(
                            SyncCandidate(
                                sample_id=sample_id,
                                candidate_rank=0,
                                confidence=classify_confidence(
                                    length,
                                    residual_rms,
                                    pattern_rms,
                                ),
                                score=score,
                                offset_ae_to_mech_s=offset,
                                matched_peak_count=length,
                                stress_peaks=list(stress_sequence),
                                ae_peaks=list(ae_sequence),
                                residuals_s=residuals,
                                pattern_rms_s=pattern_rms,
                            )
                        )

                # Dla tej pary klastrów preferujemy najdłuższą możliwą sekwencję.
                break

    # Usuwamy prawie identyczne propozycje.
    #
    # WAŻNE: kandydatów NIE porządkujemy wyłącznie po błędzie dopasowania
    # odstępów. Trzy późne piki materiałowe mogą przypadkiem mieć niemal
    # identyczny rytm jak trzy uderzenia synchronizacyjne. W takim układzie
    # czysta metryka numeryczna podnosi błędny pakiet ponad prawdziwe,
    # wcześniejsze uderzenia.
    #
    # Program jest narzędziem zatwierdzania przez człowieka, dlatego po
    # deduplikacji pokazuje pakiety przede wszystkim w kolejności czasu AE:
    # najpierw wczesne sekwencje 1–3 pików, następnie późniejsze. W obrębie
    # podobnego czasu preferuje mniejszy błąd dopasowania.
    candidates = sorted(
        candidates,
        key=lambda candidate: (
            min(peak.time_s for peak in candidate.ae_peaks),
            candidate.score,
        ),
    )

    unique = []
    seen = set()

    for candidate in candidates:
        key = (
            round(candidate.offset_ae_to_mech_s, 3),
            tuple(round(peak.time_s, 3) for peak in candidate.stress_peaks),
            tuple(round(peak.time_s, 3) for peak in candidate.ae_peaks),
        )
        if key in seen:
            continue

        seen.add(key)
        unique.append(candidate)

        if len(unique) >= MAX_CANDIDATES_PER_SAMPLE:
            break

    for rank, candidate in enumerate(unique, start=1):
        candidate.candidate_rank = rank

    return unique


# ====================== PRZYGOTOWANIE I ZAPIS DECYZJI ========================

def candidate_to_approved_dict(
    candidate: SyncCandidate,
    post_sync_margin_s: float,
) -> dict:
    """
    Czas AE pozostaje natywny.
    Czas mechaniki będzie później przesunięty o:
      mechanical_time_offset_s = -offset_ae_to_mech_s
    """
    last_ae_peak_s = max(peak.time_s for peak in candidate.ae_peaks)
    last_mech_peak_s = max(peak.time_s for peak in candidate.stress_peaks)

    return {
        "status": "APPROVED",
        "approved_at": now_text(),
        "candidate_rank": candidate.candidate_rank,
        "confidence": candidate.confidence,
        "score": candidate.score,
        "matched_peak_count": candidate.matched_peak_count,
        "offset_ae_to_mech_s": candidate.offset_ae_to_mech_s,
        "mechanical_time_offset_s": -candidate.offset_ae_to_mech_s,
        "post_sync_margin_s": post_sync_margin_s,
        "ae_zero_time_s": last_ae_peak_s + post_sync_margin_s,
        "mech_zero_time_s": last_mech_peak_s + post_sync_margin_s,
        "stress_peak_times_s": [peak.time_s for peak in candidate.stress_peaks],
        "ae_peak_times_s": [peak.time_s for peak in candidate.ae_peaks],
        "residuals_s": candidate.residuals_s,
        "pattern_rms_s": candidate.pattern_rms_s,
        "interpretation": {
            "time_ae": "Nie zmieniaj; jest referencją dla AE i TIFF.",
            "time_specimen": (
                "Time specimen = Time specimen raw - offset_ae_to_mech_s"
            ),
            "cumulative_ea_counts": (
                "Rozpocznij sumę eventsNo. od ae_zero_time_s; przed tym "
                "czasem cumulative EA counts = 0."
            ),
        },
    }


def empty_approval_file(root: Path, settings: dict) -> dict:
    return {
        "format_version": 1,
        "created_at": now_text(),
        "updated_at": now_text(),
        "root": str(root),
        "settings": settings,
        "samples": {},
    }


def load_approvals(path: Path, root: Path, settings: dict) -> dict:
    if not path.is_file():
        return empty_approval_file(root, settings)

    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        backup = path.with_suffix(".broken.json")
        try:
            path.replace(backup)
        except OSError:
            pass
        return empty_approval_file(root, settings)

    if not isinstance(data, dict):
        return empty_approval_file(root, settings)

    data.setdefault("format_version", 1)
    data.setdefault("created_at", now_text())
    data.setdefault("root", str(root))
    data.setdefault("settings", settings)
    data.setdefault("samples", {})
    return data


def save_approvals(path: Path, approvals: dict) -> None:
    approvals["updated_at"] = now_text()
    atomic_write_json(path, approvals)


def decision_to_row(sample_id: str, decision: dict) -> DecisionRecord:
    status = decision.get("status", "")
    is_approved = status == "APPROVED"

    return DecisionRecord(
        sample_id=sample_id,
        status=status,
        decision_at=decision.get(
            "approved_at",
            decision.get("skipped_at", ""),
        ),
        candidate_rank=str(decision.get("candidate_rank", "")),
        confidence=str(decision.get("confidence", "")),
        offset_ae_to_mech_s=(
            "{0:.9f}".format(decision["offset_ae_to_mech_s"])
            if is_approved else ""
        ),
        mechanical_time_offset_s=(
            "{0:+.9f}".format(decision["mechanical_time_offset_s"])
            if is_approved else ""
        ),
        ae_zero_time_s=(
            "{0:.6f}".format(decision["ae_zero_time_s"])
            if is_approved else ""
        ),
        mech_zero_time_s=(
            "{0:.6f}".format(decision["mech_zero_time_s"])
            if is_approved else ""
        ),
        post_sync_margin_s=(
            "{0:.3f}".format(decision["post_sync_margin_s"])
            if is_approved else ""
        ),
        matched_peak_count=str(decision.get("matched_peak_count", "")),
        stress_peak_times_s=(
            " | ".join(
                "{0:.6f}".format(value)
                for value in decision.get("stress_peak_times_s", [])
            )
        ),
        ae_peak_times_s=(
            " | ".join(
                "{0:.6f}".format(value)
                for value in decision.get("ae_peak_times_s", [])
            )
        ),
        message=str(decision.get("message", "")),
    )


def write_decision_csv(path: Path, approvals: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    fields = list(DecisionRecord.__dataclass_fields__.keys())
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter=";")
        writer.writeheader()

        for sample_id in sorted(
            approvals.get("samples", {}),
            key=lambda value: value.casefold(),
        ):
            writer.writerow(
                asdict(
                    decision_to_row(
                        sample_id,
                        approvals["samples"][sample_id],
                    )
                )
            )


def candidate_to_csv_row(candidate: SyncCandidate) -> dict:
    return {
        "sample_id": candidate.sample_id,
        "candidate_rank": candidate.candidate_rank,
        "confidence": candidate.confidence,
        "score": candidate.score,
        "offset_ae_to_mech_s": candidate.offset_ae_to_mech_s,
        "mechanical_time_offset_s": -candidate.offset_ae_to_mech_s,
        "matched_peak_count": candidate.matched_peak_count,
        "stress_peak_times_s": format_times(candidate.stress_peaks),
        "ae_peak_times_s": format_times(candidate.ae_peaks),
        "residuals_s": format_values(candidate.residuals_s),
        "pattern_rms_s": candidate.pattern_rms_s,
    }


def write_candidates_csv(path: Path, candidates: List[SyncCandidate]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    fields = [
        "sample_id",
        "candidate_rank",
        "confidence",
        "score",
        "offset_ae_to_mech_s",
        "mechanical_time_offset_s",
        "matched_peak_count",
        "stress_peak_times_s",
        "ae_peak_times_s",
        "residuals_s",
        "pattern_rms_s",
    ]

    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter=";")
        writer.writeheader()

        for candidate in candidates:
            writer.writerow(candidate_to_csv_row(candidate))


# ============================= WIDOK MATPLOTLIB ==============================

def downsample(
    xs: List[float],
    ys: List[float],
    max_points: int,
) -> Tuple[List[float], List[float]]:
    if len(xs) <= max_points:
        return xs, ys

    stride = max(1, int(math.ceil(len(xs) / float(max_points))))
    return xs[::stride], ys[::stride]


def visible_window(
    center_s: float,
    window_s: float,
    data_start_s: float,
    data_end_s: float,
) -> Tuple[float, float]:
    """
    Stara się utrzymać pełne okno (domyślnie 50 s).
    Przy początku lub końcu testu przesuwa zakres, zamiast go ucinać.
    """
    half_window = window_s / 2.0
    start_s = center_s - half_window
    end_s = center_s + half_window

    if start_s < data_start_s:
        end_s += data_start_s - start_s
        start_s = data_start_s

    if end_s > data_end_s:
        start_s -= end_s - data_end_s
        end_s = data_end_s

    start_s = max(data_start_s, start_s)
    end_s = min(data_end_s, end_s)
    return start_s, end_s


def values_in_window(
    times: List[float],
    values: List[float],
    start_s: float,
    end_s: float,
) -> Tuple[List[float], List[float]]:
    xs = []
    ys = []

    for time_s, value in zip(times, values):
        if start_s <= time_s <= end_s:
            xs.append(time_s)
            ys.append(value)

    return xs, ys


def set_reasonable_ylim(axis, values: List[float], lower_floor: Optional[float] = None) -> None:
    if not values:
        return

    low = quantile(values, 0.01)
    high = quantile(values, 0.99)

    if lower_floor is not None:
        low = min(lower_floor, low)

    if high <= low:
        high = low + 1.0

    margin = (high - low) * 0.12
    axis.set_ylim(low - margin, high + margin)


def set_full_positive_ylim(
    axis,
    values: List[float],
    required_values: Optional[List[float]] = None,
) -> None:
    """
    Skala dla panelu AE.

    Nie używa percentyli. Drugi panel służy do wyboru strzałów
    synchronizacyjnych, więc ostre piki nie mogą wypadać poza oś Y.
    """
    all_values = [
        float(value)
        for value in values
        if value is not None and value >= 0
    ]

    if required_values:
        all_values.extend(
            float(value)
            for value in required_values
            if value is not None and value >= 0
        )

    if not all_values:
        axis.set_ylim(0.0, 1.0)
        return

    high = max(all_values)

    if high <= 0:
        axis.set_ylim(0.0, 1.0)
        return

    axis.set_ylim(0.0, high * 1.12)


def find_largest_post_sync_event(
    ae_times: List[float],
    ae_events: List[float],
    ae_start_s: float,
) -> Optional[Tuple[float, float]]:
    candidates = [
        (time_s, event_count)
        for time_s, event_count in zip(ae_times, ae_events)
        if time_s >= ae_start_s
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[1])


class SyncReviewWindow:
    """
    Jedno interaktywne okno dla jednej próbki.
    Nie przechowuje decyzji globalnie — zwraca ją do pętli głównej.
    """

    def __init__(
        self,
        sample_id: str,
        candidates: List[SyncCandidate],
        stress_peaks: List[Peak],
        ae_peaks: List[Peak],
        mech_time_raw: List[float],
        stress: List[float],
        ae_time: List[float],
        ae_events: List[float],
        ae_auxiliary_values: List[float],
        ae_source_kind: str,
        ae_signal_label: str,
        ae_overview_label: str,
        max_sync_gap_s: float,
        review_window_s: float,
        default_margin_s: float,
        save_preview_path: Path,
    ):
        self.sample_id = sample_id
        self.original_candidates = list(candidates)
        self.candidates = list(candidates)
        self.stress_peaks = list(stress_peaks)
        self.ae_peaks = list(ae_peaks)
        self.mech_time_raw = mech_time_raw
        self.stress = stress
        self.ae_time = ae_time
        # W CumEn ae_events zawiera d(CumEn)/dt; nazwa pozostaje dla
        # kompatybilności wewnętrznej z panelem sygnału AE.
        self.ae_events = ae_events
        self.ae_auxiliary_values = ae_auxiliary_values
        self.ae_source_kind = ae_source_kind
        self.ae_signal_label = ae_signal_label
        self.ae_overview_label = ae_overview_label
        self.max_sync_gap_s = max_sync_gap_s
        self.review_window_s = review_window_s
        self.default_margin_s = default_margin_s
        self.margin_s = default_margin_s
        self.save_preview_path = save_preview_path

        self.index = 0
        self.decision = None
        self.figure = None
        self.ax_stress_overview = None
        self.ax_stress = None
        self.ax_ae = None
        self.ax_overview = None
        self.text_box = None

        # Stan ręcznego wyboru pakietów synchronizacyjnych.
        self.manual_ae_window = None
        self.manual_ae_peaks = None
        self.manual_stress_raw_window = None
        self.manual_stress_peaks = None
        self.manual_message = ""
        self.ae_span_selector = None
        self.stress_span_selector = None

        # Przewijanie tylko widoku w czasie; nie zmienia kandydatów ani offsetu.
        self.detail_view_override = None
        self.current_view_left = None
        self.current_view_right = None

        # Przewijanie pionowe układu okna. To tylko kosmetyka widoku.
        self.vertical_slider = None
        self.vertical_scroll_axes = []
        self.vertical_scroll_base_positions = {}

    @property
    def candidate(self) -> SyncCandidate:
        return self.candidates[self.index]

    def aligned_mechanical_time(self) -> List[float]:
        """
        Przenosi mechanikę na Time AE.
        offset_AE_to_mech = t_mech - t_AE,
        zatem t_AE = t_mech - offset_AE_to_mech.
        """
        offset = self.candidate.offset_ae_to_mech_s
        return [time_s - offset for time_s in self.mech_time_raw]

    def candidate_center_on_ae_axis(self) -> float:
        ae_peak_times = [peak.time_s for peak in self.candidate.ae_peaks]
        return (min(ae_peak_times) + max(ae_peak_times)) / 2.0

    def candidate_ae_zero_time(self) -> float:
        return max(peak.time_s for peak in self.candidate.ae_peaks) + self.margin_s

    def candidate_mech_zero_time_on_ae_axis(self) -> float:
        raw_mech_last = max(
            peak.time_s for peak in self.candidate.stress_peaks
        )
        return (
            raw_mech_last
            - self.candidate.offset_ae_to_mech_s
            + self.margin_s
        )

    def decide_approve(self, event=None) -> None:
        self.decision = "APPROVE"
        self.save_preview_path.parent.mkdir(parents=True, exist_ok=True)
        self.figure.savefig(self.save_preview_path, dpi=170, bbox_inches="tight")
        plt.close(self.figure)

    def decide_skip(self, event=None) -> None:
        self.decision = "SKIP"
        plt.close(self.figure)

    def decide_quit(self, event=None) -> None:
        self.decision = "QUIT"
        plt.close(self.figure)

    def next_candidate(self, event=None) -> None:
        self.index = (self.index + 1) % len(self.candidates)
        self.margin_s = self.default_margin_s
        self.detail_view_override = None
        self.draw()

    def previous_candidate(self, event=None) -> None:
        self.index = (self.index - 1) % len(self.candidates)
        self.margin_s = self.default_margin_s
        self.detail_view_override = None
        self.draw()

    def margin_up(self, event=None) -> None:
        self.margin_s = min(5.0, self.margin_s + 0.10)
        self.draw()

    def margin_down(self, event=None) -> None:
        self.margin_s = max(0.0, self.margin_s - 0.10)
        self.draw()

    def margin_reset(self, event=None) -> None:
        self.margin_s = self.default_margin_s
        self.draw()

    def on_key(self, event) -> None:
        key = (event.key or "").lower()

        if key in ("y", "enter", "return"):
            self.decide_approve()
        elif key in ("n", "right"):
            self.next_candidate()
        elif key in ("p", "left"):
            self.previous_candidate()
        elif key in ("up", "+", "="):
            self.margin_up()
        elif key in ("down", "-", "_"):
            self.margin_down()
        elif key == "r":
            self.margin_reset()
        elif key == "x":
            self.reset_manual_ae_window()
        elif key == "s":
            self.decide_skip()
        elif key in ("q", "escape"):
            self.decide_quit()

    def on_close(self, event) -> None:
        # Zamknięcie krzyżykiem jest traktowane jako bezpieczne zakończenie,
        # a nie automatyczne pominięcie próbki.
        if self.decision is None:
            self.decision = "QUIT"

    def _make_span_selector(self, axis, callback):
        try:
            return SpanSelector(
                axis,
                callback,
                "horizontal",
                useblit=False,
                interactive=True,
                drag_from_anywhere=True,
            )
        except TypeError:
            return SpanSelector(
                axis,
                callback,
                "horizontal",
                useblit=False,
                interactive=True,
            )

    def _refresh_span_selectors(self) -> None:
        """
        Axes są czyszczone przy każdym draw(), dlatego SpanSelector musi być
        przyłączony ponownie do świeżych paneli.

        V16:
          - panel 1 / górna mapa Stress NIE zaznacza; działa jak mapa AE,
            czyli klik/kółko przesuwa okno szczegółowe,
          - panel 2 / szczegółowy Stress zaznacza pakiet mechaniczny,
          - panel 3 / szczegółowy AE zaznacza pakiet akustyczny,
          - panel 4 / mapa AE przesuwa okno szczegółowe.
        """
        for selector_name in (
            "ae_span_selector",
            "stress_detail_span_selector",
            # nazwy ze starszych V14/V15 — czyszczone dla zgodności
            "stress_overview_span_selector",
            "stress_span_selector",
        ):
            selector = getattr(self, selector_name, None)
            try:
                if selector is not None:
                    selector.disconnect_events()
            except Exception:
                pass
            setattr(self, selector_name, None)

        self.stress_detail_span_selector = self._make_span_selector(
            self.ax_stress,
            self.on_stress_span,
        )
        self.ae_span_selector = self._make_span_selector(
            self.ax_ae,
            self.on_ae_span,
        )

    def _manual_stress_window_on_ae_axis(self, candidate=None):
        if self.manual_stress_raw_window is None:
            return None

        if candidate is None:
            candidate = self.candidate

        left_raw, right_raw = self.manual_stress_raw_window
        offset = candidate.offset_ae_to_mech_s

        return (
            left_raw - offset,
            right_raw - offset,
        )

    def _rebuild_candidates_from_manual_peaks(self, reason: str) -> bool:
        stress_peaks = (
            self.manual_stress_peaks
            if self.manual_stress_peaks is not None
            else self.stress_peaks
        )
        ae_peaks = (
            self.manual_ae_peaks
            if self.manual_ae_peaks is not None
            else self.ae_peaks
        )

        local_candidates = match_candidates(
            sample_id=self.sample_id,
            stress_peaks_raw_time=stress_peaks,
            ae_peaks=ae_peaks,
            max_gap_s=self.max_sync_gap_s,
        )

        if not local_candidates:
            self.manual_message = (
                "{0} Nie utworzono kandydata z aktualnych ręcznych "
                "zaznaczeń Stress/AE. Poszerz obszar albo naciśnij X."
            ).format(reason)
            self.draw()
            return False

        self.candidates = local_candidates
        self.index = 0
        self.margin_s = self.default_margin_s
        self.detail_view_override = None
        self.manual_message = reason
        self.draw()
        return True


    def on_ae_span(self, xmin, xmax) -> None:
        """
        Przeciągnięcie po panelu AE określa, z którego fragmentu mają zostać
        wybrane piki strzałów akustycznych.
        """
        left, right = sorted((float(xmin), float(xmax)))

        if right - left < 0.15:
            self.manual_message = (
                "Zaznaczenie AE jest zbyt wąskie. Obejmij cały pakiet "
                "strzałów, nie pojedynczy punkt."
            )
            self.draw()
            return

        local_ae_peaks = detect_ae_peaks_in_manual_window(
            sample_id=self.sample_id,
            times=self.ae_time,
            signal=self.ae_events,
            auxiliary_values=self.ae_auxiliary_values,
            source_kind=self.ae_source_kind,
            left_s=left,
            right_s=right,
        )

        if not local_ae_peaks:
            self.manual_message = (
                "W zaznaczonym zakresie AE {0:.3f}–{1:.3f} s nie znaleziono "
                "pików. Poszerz zaznaczenie albo wybierz inny fragment."
            ).format(left, right)
            self.draw()
            return

        previous_window = self.manual_ae_window
        previous_peaks = self.manual_ae_peaks

        self.manual_ae_window = (left, right)
        self.manual_ae_peaks = local_ae_peaks

        reason = (
            "RĘCZNY OBSZAR AE: {0:.3f}–{1:.3f} s. "
            "Piki AE są pobierane wyłącznie z tego zakresu. "
            "Panel 2 Stress może dodatkowo ograniczyć piki mechaniczne. "
            "N/P zmienia kandydata; X przywraca automat."
        ).format(left, right)

        if not self._rebuild_candidates_from_manual_peaks(reason):
            self.manual_ae_window = previous_window
            self.manual_ae_peaks = previous_peaks

    def on_stress_span(self, xmin, xmax) -> None:
        """
        Przeciągnięcie po panelu 2 Stress ogranicza obszar wyszukiwania
        mechanicznych uderzeń synchronizacyjnych.

        Zaznaczenie odbywa się na osi AE, bo mapa Stress jest już narysowana
        po aktualnym przesunięciu kandydata. Do detekcji pików wracamy do
        surowej osi specimen:
            raw_time = ae_axis_time + offset_AE_to_mech_s
        """
        left_ae, right_ae = sorted((float(xmin), float(xmax)))

        if right_ae - left_ae < 0.15:
            self.manual_message = (
                "Zaznaczenie Stress jest zbyt wąskie. Obejmij cały pakiet "
                "uderzeń mechanicznych, nie pojedynczy punkt."
            )
            self.draw()
            return

        offset = self.candidate.offset_ae_to_mech_s
        left_raw = left_ae + offset
        right_raw = right_ae + offset

        local_stress_peaks = detect_stress_peaks_in_manual_window(
            sample_id=self.sample_id,
            times=self.mech_time_raw,
            stress=self.stress,
            left_s=left_raw,
            right_s=right_raw,
        )

        if not local_stress_peaks:
            self.manual_message = (
                "W zaznaczonym zakresie Stress {0:.3f}–{1:.3f} s "
                "na osi AE nie znaleziono pików mechanicznych. "
                "Poszerz zaznaczenie albo wybierz inny fragment."
            ).format(left_ae, right_ae)
            self.draw()
            return

        previous_window = self.manual_stress_raw_window
        previous_peaks = self.manual_stress_peaks

        self.manual_stress_raw_window = (left_raw, right_raw)
        self.manual_stress_peaks = local_stress_peaks

        reason = (
            "RĘCZNY OBSZAR STRESS: {0:.3f}–{1:.3f} s na osi AE "
            "(raw specimen {2:.3f}–{3:.3f} s). "
            "Piki Stress są pobierane wyłącznie z tego zakresu. "
            "Panel AE może dodatkowo ograniczyć piki akustyczne. "
            "N/P zmienia kandydata; X przywraca automat."
        ).format(left_ae, right_ae, left_raw, right_raw)

        if not self._rebuild_candidates_from_manual_peaks(reason):
            self.manual_stress_raw_window = previous_window
            self.manual_stress_peaks = previous_peaks


    def reset_manual_ae_window(self, event=None) -> None:
        self.manual_ae_window = None
        self.manual_ae_peaks = None
        self.manual_stress_raw_window = None
        self.manual_stress_peaks = None
        self.detail_view_override = None
        self.candidates = list(self.original_candidates)
        self.index = 0
        self.margin_s = self.default_margin_s
        self.manual_message = (
            "Powrót do pełnej automatycznej listy kandydatów."
        )
        self.draw()

    def _time_domain(self, aligned_mech_time):
        return (
            min(self.ae_time[0], aligned_mech_time[0]),
            max(self.ae_time[-1], aligned_mech_time[-1]),
        )

    def _set_detail_view_centered(self, center_s, width_s) -> None:
        aligned_mech_time = self.aligned_mechanical_time()
        domain_left, domain_right = self._time_domain(aligned_mech_time)

        width_s = max(
            0.5,
            min(float(width_s), domain_right - domain_left),
        )
        left = float(center_s) - width_s / 2.0
        right = float(center_s) + width_s / 2.0

        if left < domain_left:
            right += domain_left - left
            left = domain_left

        if right > domain_right:
            left -= right - domain_right
            right = domain_right

        self.detail_view_override = (
            max(domain_left, left),
            min(domain_right, right),
        )

    def on_overview_click(self, event) -> None:
        """
        Lewy klik na panelu 1 albo panelu 4 przesuwa tylko widok szczegółowy.
        Nie zmienia zaznaczeń, kandydatów ani offsetu.

        Panel 1 = mapa Stress.
        Panel 4 = mapa AE.
        """
        if event.inaxes not in (self.ax_stress_overview, self.ax_overview):
            return
        if event.xdata is None:
            return
        if getattr(event, "button", None) != 1:
            return
        if (
            self.current_view_left is None
            or self.current_view_right is None
        ):
            return

        width_s = self.current_view_right - self.current_view_left
        self._set_detail_view_centered(event.xdata, width_s)

        source_label = (
            "mapą Stress"
            if event.inaxes is self.ax_stress_overview
            else "mapą AE"
        )
        self.manual_message = (
            "Widok przesunięty {0} do {1:.3f} s. "
            "To nie zmienia wybranych strzałów."
        ).format(source_label, event.xdata)
        self.draw()

    def on_overview_scroll(self, event) -> None:
        """
        Kółko myszy nad panelem 1 albo panelem 4 przewija wyłącznie widok
        szczegółowy.

        Panel 1 = mapa Stress.
        Panel 4 = mapa AE.
        """
        if event.inaxes not in (self.ax_stress_overview, self.ax_overview):
            return
        if (
            self.current_view_left is None
            or self.current_view_right is None
        ):
            return

        width_s = self.current_view_right - self.current_view_left
        center_s = (
            self.current_view_left + self.current_view_right
        ) / 2.0
        step_s = max(0.5, width_s * 0.45)
        direction = (
            -1.0
            if getattr(event, "button", "") == "up"
            else 1.0
        )

        self._set_detail_view_centered(
            center_s + direction * step_s,
            width_s,
        )
        source_label = (
            "mapą Stress"
            if event.inaxes is self.ax_stress_overview
            else "mapą AE"
        )
        self.manual_message = (
            "Widok przewinięty {0}: kółko ↑ = wcześniej, kółko ↓ = później."
        ).format(source_label)
        self.draw()

    def _register_vertical_scroll_axes(self, axes) -> None:
        self.vertical_scroll_axes = list(axes)
        self.vertical_scroll_base_positions = {
            axis: axis.get_position().frozen()
            for axis in self.vertical_scroll_axes
        }

    def on_vertical_scroll_slider(self, value) -> None:
        """
        Suwak po prawej przesuwa cały pakiet wykresów/instrukcji góra-dół.

        Nie zmienia danych, kandydatów, offsetu ani zaznaczeń. To tylko sposób
        na pracę w niższym oknie, gdy dochodzi dodatkowa mapa Stress.
        """
        if not self.vertical_scroll_base_positions:
            return

        # value=0.5 to pozycja neutralna; zakres daje ok. 22% wysokości figury.
        offset = (float(value) - 0.5) * 0.22

        for axis, base in self.vertical_scroll_base_positions.items():
            axis.set_position([
                base.x0,
                base.y0 + offset,
                base.width,
                base.height,
            ])

        self.figure.canvas.draw_idle()

    def draw(self) -> None:
        candidate = self.candidate
        aligned_mech_time = self.aligned_mechanical_time()
        ae_zero = self.candidate_ae_zero_time()
        mech_zero_on_ae = self.candidate_mech_zero_time_on_ae_axis()

        data_start, data_end = self._time_domain(aligned_mech_time)

        if self.detail_view_override is not None:
            window_start, window_end = self.detail_view_override
        elif self.manual_ae_window is not None:
            manual_left, manual_right = self.manual_ae_window
            padding_s = max(
                4.0,
                (manual_right - manual_left) * 1.5,
            )
            window_start = max(data_start, manual_left - padding_s)
            window_end = min(data_end, manual_right + padding_s)
        else:
            window_start, window_end = visible_window(
                center_s=self.candidate_center_on_ae_axis(),
                window_s=self.review_window_s,
                data_start_s=data_start,
                data_end_s=data_end,
            )

        self.current_view_left = window_start
        self.current_view_right = window_end

        self.ax_stress_overview.clear()
        self.ax_stress.clear()
        self.ax_ae.clear()
        self.ax_overview.clear()

        # --- Górna mapa: pełny Stress na osi AE -------------------------
        stress_overview_x, stress_overview_y = downsample(
            aligned_mech_time,
            self.stress,
            max_points=5000,
        )
        self.ax_stress_overview.plot(
            stress_overview_x,
            stress_overview_y,
            linewidth=0.75,
            label="Mapa Stress [MPa]",
        )
        self.ax_stress_overview.axvspan(
            window_start,
            window_end,
            alpha=0.20,
            label="aktualne okno kontroli",
        )
        manual_stress_ae_window = self._manual_stress_window_on_ae_axis(
            candidate
        )
        if manual_stress_ae_window is not None:
            stress_left, stress_right = manual_stress_ae_window
            self.ax_stress_overview.axvspan(
                stress_left,
                stress_right,
                alpha=0.18,
                label="ręczny obszar Stress",
            )
        self.ax_stress_overview.axvline(
            mech_zero_on_ae,
            linestyle=":",
            linewidth=1.2,
            alpha=0.95,
            label="T0 Stress",
        )
        self.ax_stress_overview.set_ylabel("Stress mapa")
        self.ax_stress_overview.grid(True, alpha=0.25)
        self.ax_stress_overview.set_xlim(data_start, data_end)
        set_reasonable_ylim(self.ax_stress_overview, stress_overview_y)
        self.ax_stress_overview.legend(loc="upper right", fontsize=8)

        # --- Panel 1: Stress na osi AE ----------------------------------
        stress_x, stress_y = values_in_window(
            aligned_mech_time,
            self.stress,
            window_start,
            window_end,
        )
        stress_x, stress_y = downsample(stress_x, stress_y, max_points=5000)

        self.ax_stress.plot(
            stress_x,
            stress_y,
            linewidth=0.9,
            label="Stress [MPa] — Time specimen na osi AE",
        )
        self.ax_stress.set_ylabel("Stress [MPa]")
        self.ax_stress.grid(True, alpha=0.25)
        self.ax_stress.set_xlim(window_start, window_end)
        set_reasonable_ylim(self.ax_stress, stress_y)

        manual_stress_ae_window = self._manual_stress_window_on_ae_axis(
            candidate
        )
        if manual_stress_ae_window is not None:
            stress_left, stress_right = manual_stress_ae_window
            self.ax_stress.axvspan(
                stress_left,
                stress_right,
                alpha=0.18,
                label="ręczny obszar Stress",
            )

        # Zaznaczamy piki Stress aktualnego kandydata widoczne
        # w tym fragmencie, a piki aktualnego kandydata grubszą linią.
        for peak in self.candidates[self.index].stress_peaks:
            aligned_peak_time = peak.time_s - candidate.offset_ae_to_mech_s
            self.ax_stress.axvline(
                aligned_peak_time,
                linestyle="--",
                linewidth=1.6,
                alpha=0.85,
            )
            self.ax_stress.text(
                aligned_peak_time,
                self.ax_stress.get_ylim()[1],
                " S{0}".format(
                    candidate.stress_peaks.index(peak) + 1
                ),
                va="top",
                ha="left",
                fontsize=9,
            )

        self.ax_stress.axvline(
            mech_zero_on_ae,
            linestyle=":",
            linewidth=1.4,
            alpha=0.95,
            label="start po sync",
        )
        self.ax_stress.legend(loc="upper right", fontsize=8)

        # --- Panel 2: Events No. na NATYWNEJ osi AE ---------------------
        ae_x, ae_y = values_in_window(
            self.ae_time,
            self.ae_events,
            window_start,
            window_end,
        )

        self.ax_ae.step(
            ae_x,
            ae_y,
            where="mid",
            linewidth=0.9,
            label=self.ae_signal_label,
        )
        self.ax_ae.set_ylabel(self.ae_signal_label)
        self.ax_ae.grid(True, alpha=0.25)
        self.ax_ae.set_xlim(window_start, window_end)
        set_full_positive_ylim(
            self.ax_ae,
            ae_y,
            required_values=[
                peak.value
                for peak in candidate.ae_peaks
            ],
        )

        if self.manual_ae_window is not None:
            manual_left, manual_right = self.manual_ae_window
            self.ax_ae.axvspan(
                manual_left,
                manual_right,
                alpha=0.18,
                label="ręczny obszar pików",
            )

        for peak in candidate.ae_peaks:
            self.ax_ae.axvline(
                peak.time_s,
                linestyle="--",
                linewidth=1.6,
                alpha=0.85,
            )
            self.ax_ae.text(
                peak.time_s,
                self.ax_ae.get_ylim()[1],
                " AE{0}".format(
                    candidate.ae_peaks.index(peak) + 1
                ),
                va="top",
                ha="left",
                fontsize=9,
            )

        self.ax_ae.axvline(
            ae_zero,
            linestyle=":",
            linewidth=1.4,
            alpha=0.95,
            label="start liczenia cumulative EA",
        )
        self.ax_ae.legend(loc="upper right", fontsize=8)

        # --- Panel 3: pełny przebieg, kontekst późnego zerwania ----------
        overview_y = [math.log10(value + 1.0) for value in self.ae_events]
        overview_x, overview_y = downsample(
            self.ae_time,
            overview_y,
            max_points=5000,
        )

        self.ax_overview.plot(
            overview_x,
            overview_y,
            linewidth=0.75,
            label=self.ae_overview_label,
        )
        self.ax_overview.axvspan(
            window_start,
            window_end,
            alpha=0.20,
            label="aktualne okno kontroli",
        )
        if self.manual_ae_window is not None:
            manual_left, manual_right = self.manual_ae_window
            self.ax_overview.axvspan(
                manual_left,
                manual_right,
                alpha=0.18,
                label="ręczny obszar pików",
            )
        self.ax_overview.axvline(
            ae_zero,
            linestyle=":",
            linewidth=1.2,
            alpha=0.95,
            label="T0 cumulative EA",
        )

        largest_post_sync = find_largest_post_sync_event(
            self.ae_time,
            self.ae_events,
            ae_zero,
        )
        if largest_post_sync is not None:
            largest_time, largest_value = largest_post_sync
            self.ax_overview.axvline(
                largest_time,
                linestyle="--",
                linewidth=1.0,
                alpha=0.65,
            )
            self.ax_overview.annotate(
                "max sygnału AE po T0\n{0:.1f} s; {1:g}".format(
                    largest_time,
                    largest_value,
                ),
                xy=(largest_time, math.log10(largest_value + 1.0)),
                xytext=(5, 4),
                textcoords="offset points",
                fontsize=8,
                rotation=90,
                va="bottom",
            )

        self.ax_overview.set_xlabel("Time AE [s] — natywna oś AE / TIFF")
        self.ax_overview.set_ylabel(self.ae_overview_label)
        self.ax_overview.grid(True, alpha=0.25)
        self.ax_overview.set_xlim(self.ae_time[0], self.ae_time[-1])
        self.ax_overview.legend(loc="upper right", fontsize=8)

        # --- Informacja i instrukcja -------------------------------------
        text = (
            "{sample} | kandydat {rank}/{total} | dopasowanie numeryczne: {confidence}\n"
            "Kolejność kandydatów: od najwcześniejszych pakietów AE; "
            "ostateczną decyzję podejmujesz wzrokowo.\n"
            "offset AE→mech = {offset:+.6f} s  →  "
            "Time specimen = raw {mech_offset:+.6f} s\n"
            "piki Stress (raw): {stress_times}\n"
            "piki AE: {ae_times}\n"
            "residua: {residuals} s | margin: {margin:.1f} s | "
            "Cumulative EA od: {ae_zero:.3f} s\n"
            "Ręczny obszar Stress: {manual_stress_window}\n"
            "Ręczny obszar pików AE: {manual_window}\n"
            "PRZECIĄGNIJ po górnej mapie Stress pakiet uderzeń mechanicznych. "
            "PRZECIĄGNIJ po panelu AE prawidłowy pakiet strzałów akustycznych. "
            "Kandydaci zostaną ograniczeni do zaznaczeń. "
            "Panel AE ma pełną skalę Y bez obcinania pików. "
            "Panel 1/4: klik/kółko tylko przesuwa widok. "
            "Prawy suwak: przewijanie okna góra/dół.\n"
            "Y/Enter: zatwierdź | N/→: następny | P/←: poprzedni | "
            "X: automat | ↑/↓: margin ±0,1 s | R: reset margin | "
            "S: pomiń | Q: zakończ\n"
            "{manual_message}"
        ).format(
            sample=self.sample_id,
            rank=candidate.candidate_rank,
            total=len(self.candidates),
            confidence=candidate.confidence,
            offset=candidate.offset_ae_to_mech_s,
            mech_offset=-candidate.offset_ae_to_mech_s,
            stress_times=format_times(candidate.stress_peaks),
            ae_times=format_times(candidate.ae_peaks),
            residuals=format_values(candidate.residuals_s),
            margin=self.margin_s,
            ae_zero=ae_zero,
            manual_stress_window=(
                "raw {0:.3f}–{1:.3f} s".format(
                    *self.manual_stress_raw_window
                )
                if self.manual_stress_raw_window is not None
                else "brak — automat globalny"
            ),
            manual_window=(
                "{0:.3f}–{1:.3f} s".format(*self.manual_ae_window)
                if self.manual_ae_window is not None
                else "brak — automat globalny"
            ),
            manual_message=self.manual_message,
        )

        self.text_box.set_text(text)
        self.figure.suptitle(
            "Synchronizacja mechanika ↔ AE — sprawdź pakiet uderzeń synchronizacyjnych",
            fontsize=14,
            fontweight="bold",
        )
        self._refresh_span_selectors()
        self.figure.canvas.draw_idle()

    def show(self) -> str:
        self.figure = plt.figure(figsize=(15.2, 12.2))
        grid = self.figure.add_gridspec(
            nrows=5,
            ncols=1,
            height_ratios=[1.25, 2.7, 2.5, 1.55, 1.05],
            hspace=0.38,
        )

        self.ax_stress_overview = self.figure.add_subplot(grid[0, 0])
        self.ax_stress = self.figure.add_subplot(grid[1, 0])
        self.ax_ae = self.figure.add_subplot(
            grid[2, 0],
            sharex=self.ax_stress,
        )
        self.ax_overview = self.figure.add_subplot(grid[3, 0])
        text_axis = self.figure.add_subplot(grid[4, 0])
        text_axis.axis("off")
        self.text_box = text_axis.text(
            0.01,
            0.96,
            "",
            va="top",
            ha="left",
            fontsize=8.8,
            family="monospace",
        )

        self.figure.subplots_adjust(
            left=0.075,
            right=0.935,
            top=0.93,
            bottom=0.072,
        )

        # Suwak pionowy — przewijanie układu okna góra/dół.
        slider_axis = self.figure.add_axes([0.965, 0.12, 0.016, 0.74])
        try:
            self.vertical_slider = Slider(
                slider_axis,
                "Y",
                0.0,
                1.0,
                valinit=0.5,
                orientation="vertical",
            )
        except TypeError:
            # Starszy Matplotlib bez parametru orientation.
            self.vertical_slider = Slider(
                slider_axis,
                "Y",
                0.0,
                1.0,
                valinit=0.5,
            )
        self.vertical_slider.on_changed(self.on_vertical_scroll_slider)

        self._register_vertical_scroll_axes([
            self.ax_stress_overview,
            self.ax_stress,
            self.ax_ae,
            self.ax_overview,
            text_axis,
        ])

        # Przyciski: na dole figury, pod panelem tekstowym.
        button_y = 0.012
        button_height = 0.038
        button_specs = [
            ("← Poprzedni", 0.08, self.previous_candidate),
            ("Następny →", 0.23, self.next_candidate),
            ("✓ TAK", 0.36, self.decide_approve),
            ("Automat (X)", 0.50, self.reset_manual_ae_window),
            ("Pomiń", 0.65, self.decide_skip),
            ("Zakończ", 0.78, self.decide_quit),
        ]
        self._buttons = []

        for label, x, callback in button_specs:
            axis = self.figure.add_axes([x, button_y, 0.11, button_height])
            button = Button(axis, label)
            button.on_clicked(callback)
            self._buttons.append(button)

        self.figure.canvas.mpl_connect("key_press_event", self.on_key)
        self.figure.canvas.mpl_connect(
            "button_press_event",
            self.on_overview_click,
        )
        self.figure.canvas.mpl_connect(
            "scroll_event",
            self.on_overview_scroll,
        )
        self.figure.canvas.mpl_connect("close_event", self.on_close)

        self.draw()
        plt.show(block=True)

        return self.decision or "QUIT"


# ================================ PRZETWARZANIE ==============================

def get_sample_data(
    sample_id: str,
    sample_index: Dict[str, Path],
    geometry_manifest: Dict[str, dict],
    search_start_s: float,
    search_end_s: Optional[float],
    max_gap_s: float,
) -> Tuple[
    List[float],
    List[float],
    AEData,
    List[Peak],
    List[Peak],
    List[SyncCandidate],
]:
    sample_dir = sample_index.get(sample_id)
    if sample_dir is None:
        raise RuntimeError(
            "Brak katalogu dla sample_id: {0}".format(sample_id)
        )

    geometry = geometry_for_sample(sample_id, geometry_manifest)

    area_mm2 = to_float(geometry.get("cross_section_before_mm2"))
    if area_mm2 is None or area_mm2 <= 0:
        raise RuntimeError(
            "Brak poprawnego A0 w manifeście dla {0}.".format(sample_id)
        )

    specimen_path = find_one_specimen(sample_dir)
    ae_path = find_one_ae_txt(sample_dir)

    mech_time_raw, stress = parse_specimen(specimen_path, area_mm2)
    ae_data = parse_ae_txt(ae_path)

    stress_peaks = detect_stress_peaks(
        sample_id=sample_id,
        times=mech_time_raw,
        stress=stress,
        search_start_s=search_start_s,
        search_end_s=search_end_s,
    )
    ae_peaks = detect_ae_peaks(
        sample_id=sample_id,
        times=ae_data.times,
        signal=ae_data.peak_signal,
        auxiliary_values=ae_data.auxiliary_values,
        source_kind=ae_data.source_kind,
        search_start_s=search_start_s,
        search_end_s=search_end_s,
    )

    candidates = match_candidates(
        sample_id=sample_id,
        stress_peaks_raw_time=stress_peaks,
        ae_peaks=ae_peaks,
        max_gap_s=max_gap_s,
    )

    if not candidates:
        raise RuntimeError(
            "Nie zbudowano ani jednego kandydata synchronizacji. "
            "Uruchom ponownie z większym --search-end lub sprawdź dane."
        )

    return mech_time_raw, stress, ae_data, stress_peaks, ae_peaks, candidates


def make_skipped_decision(message: str) -> dict:
    return {
        "status": "SKIPPED",
        "skipped_at": now_text(),
        "message": message,
    }


def process_one_sample(
    root: Path,
    output_dir: Path,
    sample_id: str,
    sample_index: Dict[str, Path],
    geometry_manifest: Dict[str, dict],
    approvals: dict,
    settings: dict,
) -> str:
    print("\n" + "=" * 78)
    print("PRÓBKA: {0}".format(sample_id))
    print("=" * 78)

    try:
        (
            mech_time_raw,
            stress,
            ae_data,
            stress_peaks,
            ae_peaks,
            candidates,
        ) = get_sample_data(
            sample_id=sample_id,
            sample_index=sample_index,
            geometry_manifest=geometry_manifest,
            search_start_s=settings["search_start_s"],
            search_end_s=settings["search_end_s"],
            max_gap_s=settings["max_sync_gap_s"],
        )
    except Exception as exc:
        message = "{0}: {1}".format(type(exc).__name__, exc)
        approvals["samples"][sample_id] = make_skipped_decision(message)
        print("[POMINIĘTA Z BŁĘDEM] {0}".format(message))
        return "CONTINUE"

    # Dokładamy kandydatów z bieżącej próbki do pełnego CSV po zakończeniu.
    all_candidates_path = output_dir / "sync_kandydaci_do_zatwierdzenia.csv"
    current_rows = []

    if all_candidates_path.is_file():
        try:
            with all_candidates_path.open("r", encoding="utf-8-sig", newline="") as handle:
                current_rows = list(csv.DictReader(handle, delimiter=";"))
        except OSError:
            current_rows = []

    # Usuwamy stare wpisy tej próbki i zapisujemy świeże.
    current_rows = [
        row for row in current_rows
        if row.get("sample_id", "").casefold() != sample_id.casefold()
    ]
    current_rows.extend(candidate_to_csv_row(candidate) for candidate in candidates)

    candidate_fields = [
        "sample_id",
        "candidate_rank",
        "confidence",
        "score",
        "offset_ae_to_mech_s",
        "mechanical_time_offset_s",
        "matched_peak_count",
        "stress_peak_times_s",
        "ae_peak_times_s",
        "residuals_s",
        "pattern_rms_s",
    ]
    with all_candidates_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=candidate_fields, delimiter=";")
        writer.writeheader()
        writer.writerows(current_rows)

    preview_path = output_dir / ("potwierdzenie_" + sample_id + ".png")

    review = SyncReviewWindow(
        sample_id=sample_id,
        candidates=candidates,
        stress_peaks=stress_peaks,
        ae_peaks=ae_peaks,
        mech_time_raw=mech_time_raw,
        stress=stress,
        ae_time=ae_data.times,
        ae_events=ae_data.peak_signal,
        ae_auxiliary_values=ae_data.auxiliary_values,
        ae_source_kind=ae_data.source_kind,
        ae_signal_label=ae_data.signal_label,
        ae_overview_label=ae_data.overview_label,
        max_sync_gap_s=settings["max_sync_gap_s"],
        review_window_s=settings["view_window_s"],
        default_margin_s=settings["post_sync_margin_s"],
        save_preview_path=preview_path,
    )

    decision = review.show()

    if decision == "APPROVE":
        approved = candidate_to_approved_dict(
            candidate=review.candidate,
            post_sync_margin_s=review.margin_s,
        )
        approved["ae_sync_source"] = ae_data.source_kind
        approved["ae_sync_signal_label"] = ae_data.signal_label
        approved["ae_file"] = ae_data.ae_path
        approved["manual_ae_window_s"] = (
            list(review.manual_ae_window)
            if review.manual_ae_window is not None
            else None
        )
        approved["manual_stress_window_raw_s"] = (
            list(review.manual_stress_raw_window)
            if review.manual_stress_raw_window is not None
            else None
        )
        manual_stress_ae_window = review._manual_stress_window_on_ae_axis(
            review.candidate
        )
        approved["manual_stress_window_ae_axis_s"] = (
            list(manual_stress_ae_window)
            if manual_stress_ae_window is not None
            else None
        )
        approvals["samples"][sample_id] = approved

        print("[ZATWIERDZONA] {0}".format(sample_id))
        print(
            "  offset AE→mech: {0:+.9f} s".format(
                approved["offset_ae_to_mech_s"]
            )
        )
        print(
            "  Time specimen offset: {0:+.9f} s".format(
                approved["mechanical_time_offset_s"]
            )
        )
        print(
            "  Cumulative EA od: {0:.3f} s AE".format(
                approved["ae_zero_time_s"]
            )
        )
        print("  Zapis podglądu: {0}".format(preview_path))
        return "CONTINUE"

    if decision == "SKIP":
        approvals["samples"][sample_id] = make_skipped_decision(
            "Pominięto ręcznie w oknie zatwierdzania."
        )
        print("[POMINIĘTA] {0}".format(sample_id))
        return "CONTINUE"

    print("[ZAKOŃCZONO PRZEGLĄD] na próbce {0}".format(sample_id))
    return "QUIT"


# ================================== MAIN =====================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Interaktywne zatwierdzanie synchronizacji mechanika ↔ AE."
    )
    parser.add_argument(
        "--root",
        default=".",
        help="Folder Pomiary. Domyślnie bieżący folder.",
    )
    parser.add_argument(
        "--sample",
        default="",
        help="Jedna próbka, np. 1HLP316LLN.",
    )
    parser.add_argument(
        "--redo",
        action="store_true",
        help="Pokaż także próby już wcześniej zatwierdzone/pominięte.",
    )
    parser.add_argument(
        "--search-start",
        type=float,
        default=DEFAULT_SEARCH_START_S,
        help="Początek okna wykrywania pików synchronizacji [s].",
    )
    parser.add_argument(
        "--search-end",
        default=str(DEFAULT_SEARCH_END_S),
        help=(
            "Koniec okna wykrywania pików [s]; wpisz None, aby szukać "
            "w całym przebiegu."
        ),
    )
    parser.add_argument(
        "--sync-gap",
        type=float,
        default=DEFAULT_SYNC_MAX_GAP_S,
        help="Maksymalny odstęp między pikami sekwencji synchronizacyjnej [s].",
    )
    parser.add_argument(
        "--post-margin",
        type=float,
        default=DEFAULT_POST_SYNC_MARGIN_S,
        help="Domyślny margines po ostatnim piku synchronizacji [s].",
    )
    parser.add_argument(
        "--window",
        type=float,
        default=DEFAULT_VIEW_WINDOW_S,
        help="Szerokość głównego okna kontroli [s]. Domyślnie 50.",
    )
    args = parser.parse_args()

    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        print("BŁĄD: folder nie istnieje: {0}".format(root))
        return 2

    try:
        search_end = (
            None
            if str(args.search_end).casefold() == "none"
            else float(args.search_end)
        )
    except ValueError:
        print("BŁĄD: --search-end musi być liczbą lub None.")
        return 3

    if args.window <= 0:
        print("BŁĄD: --window musi być większe od 0.")
        return 4

    if args.sync_gap <= 0:
        print("BŁĄD: --sync-gap musi być większe od 0.")
        return 5

    output_dir = root / "Output" / "Synchronizacja"
    output_dir.mkdir(parents=True, exist_ok=True)

    settings = {
        "search_start_s": args.search_start,
        "search_end_s": search_end,
        "max_sync_gap_s": args.sync_gap,
        "post_sync_margin_s": args.post_margin,
        "view_window_s": args.window,
    }

    approvals_path = output_dir / "sync_parametry_zatwierdzone.json"
    decisions_path = output_dir / "sync_decyzje.csv"

    approvals = load_approvals(
        path=approvals_path,
        root=root,
        settings=settings,
    )

    try:
        sample_index = build_sample_index(root)
    except Exception as exc:
        print("BŁĄD: {0}".format(exc))
        return 7

    try:
        geometry_manifest = build_geometry_map(sample_index)
    except Exception as exc:
        print("BŁĄD GEOMETRII (pipeline_lokalny_common.py): {0}".format(exc))
        return 6

    if args.sample:
        try:
            requested_samples = [
                resolve_requested_sample_id(args.sample, sample_index)
            ]
        except Exception as exc:
            print("BŁĄD: {0}".format(exc))
            return 7
    else:
        requested_samples = list(sample_index.keys())

    if not requested_samples:
        print("BŁĄD: nie znaleziono folderów zawierających AE.")
        return 7

    samples_to_review = []
    for sample_id in requested_samples:
        existing = approvals.get("samples", {}).get(sample_id, {})
        status = existing.get("status", "")

        if not args.redo and status in ("APPROVED", "SKIPPED"):
            print(
                "[POMIJAM WCZEŚNIEJSZĄ DECYZJĘ] {0}: {1}".format(
                    sample_id,
                    status,
                )
            )
            continue

        samples_to_review.append(sample_id)

    if not samples_to_review:
        print(
            "Nie ma nowych próbek do przejrzenia. "
            "Użyj --redo, aby pokazać zatwierdzone ponownie."
        )
        return 0

    print("=" * 78)
    print("INTERAKTYWNE ZATWIERDZANIE SYNCHRONIZACJI")
    print("Folder: {0}".format(root))
    print("Foldery prób do przejrzenia: {0}".format(", ".join(samples_to_review)))
    print("Główne okno: {0:.1f} s".format(args.window))
    print(
        "Piki wyszukiwane w czasie: {0}–{1} s".format(
            args.search_start,
            "koniec pliku" if search_end is None else search_end,
        )
    )
    print("=" * 78)

    for sample_id in samples_to_review:
        result = process_one_sample(
            root=root,
            output_dir=output_dir,
            sample_id=sample_id,
            sample_index=sample_index,
            geometry_manifest=geometry_manifest,
            approvals=approvals,
            settings=settings,
        )

        # Zapis po KAŻDEJ decyzji, żeby nie utracić wcześniej zatwierdzonych
        # prób przy przerwaniu programu lub zamknięciu okna.
        save_approvals(approvals_path, approvals)
        write_decision_csv(decisions_path, approvals)

        if result == "QUIT":
            break

    approved_count = sum(
        item.get("status") == "APPROVED"
        for item in approvals.get("samples", {}).values()
    )
    skipped_count = sum(
        item.get("status") == "SKIPPED"
        for item in approvals.get("samples", {}).values()
    )

    print("\n" + "=" * 78)
    print("ZAPISANO DECYZJE")
    print("  zatwierdzone: {0}".format(approved_count))
    print("  pominięte:    {0}".format(skipped_count))
    print("  JSON: {0}".format(approvals_path))
    print("  CSV:  {0}".format(decisions_path))
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
