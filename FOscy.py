from __future__ import annotations

"""
FOscy_v20.py

Batchowe filtrowanie spektrogramów AE w folderze serii.

STRUKTURA WEJŚCIOWA
===================
<nazwa serii>/
├── FOscy.py
├── f451310/
│   ├── specimen.dat
│   ├── AE/
│   │   ├── *.wav
│   │   └── oscy1/
│   │       └── segment_*.npy
│   └── f451310/
└── f451311/
    └── AE/oscy1/segment_*.npy

STRUKTURA WYNIKOWA
==================
<nazwa serii>/
├── f451310 - filtered/
│   ├── specimen.dat
│   ├── AE/
│       ├── *.wav
│       ├── Ff451310_4000.txt
│       ├── CumEn_4000.txt
│       └── oscy1/
│           ├── filtered_segments/
│           │   └── segment_XX_filtered.npy
│           ├── background_by_freq.npy
│           ├── background_by_freq.csv
│           ├── background_by_freq_individual.npy
│           ├── background_by_freq_individual.csv
│           ├── background_by_freq_series_upper.npy
│           ├── background_by_freq_series_upper.csv
│           ├── counts_vs_time_0p01s.csv
│           ├── spectral_excess_energy_vs_time_0p01s.csv
│           ├── PSD.tiff         (wizualizacja, max-pool po czasie)
│           └── params.json
│   └── f451310 - filtered/
└── FOscy_series_upper_envelope_mask.csv

Najważniejsza zasada filtrowania:

1. Dla każdego segmentu próbki wyznaczana jest maska segmentu:
       segment_mask_i,s(f) = minimum po czasie z pomocniczo wygładzonego PSD.

2. Maska indywidualna próbki jest DOLNĄ OBWIEDNIĄ masek jej segmentów:
       individual_mask_i(f) = min_s(segment_mask_i,s(f))

3. Dla całej serii wyznaczana jest WSPÓLNA GÓRNA OBWIEDNIA masek próbek:
       series_mask(f) = max_i(individual_mask_i(f))

   Jest to maksimum NUMERYCZNE w dB. Np. max(-20, -3) = -3.

4. Każda próbka jest filtrowana względem tej samej maski serii:
       filtered_psd(f, t) = original_psd(f, t) - series_mask(f)

W skrócie, dla każdej częstotliwości f:
       series_mask(f) = max_i min_s min_t(smoothed_psd_i,s(f, t))

Dzięki temu każda próbka wnosi swoją najniższą maskę z segmentów,
a następnie cały folder zbiorczy używa najwyższej z tych masek.

Skrypt pracuje maksymalnie na czterech procesach równolegle:
- Pass 1: indywidualne maski próbek,
- Pass 2: filtrowanie, zliczenia i TIFF-y.

Ważne: surowe segmenty *.npy NIE są kopiowane do folderu
"<próbka> - filtered". Są czytane z folderu źródłowego, a folder
wynikowy AE/oscy1 zawiera wyłącznie rezultaty filtrowania.

DODATKOWE KRZYWE ENERGII
=========================
Dla każdego segmentu jest tworzona jedna skumulowana krzywa energii po
filtracji wspólną maską serii:

- CumEn_4000.txt: energia dodatnia ponad samą wspólną maską.

Krzywa jest liczona w liniowej skali mocy PSD i całkowana po
częstotliwości oraz czasie. Nie jest tworzony wariant progowany 6 dB.
"""

# Ograniczenie wątków wewnętrznych bibliotek numerycznych.
# Musi być ustawione PRZED importem numpy/pandas, zwłaszcza przy Windows spawn.
import os
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")

from concurrent.futures import ProcessPoolExecutor, as_completed
import argparse
from pathlib import Path
from typing import Any
import gc
import hashlib
import json
import math
import re
import shutil
import traceback

import numpy as np
import pandas as pd
import tifffile


# =============================================================================
# KONFIGURACJA
# =============================================================================

# Maksymalna liczba PRÓBEK przetwarzanych równolegle.
MAX_WORKERS = 4

# Czas trwania jednego segment_*.npy.
FILE_DURATION_S = 25.0

# Rozdzielczość pliku counts_vs_time_0p01s.csv oraz F<nazwa>_4000.txt.
COUNT_BIN_S = 0.01

# Zakres częstotliwości zapisanych w oscy1/*.npy.
# Ustawione na 125 kHz zgodnie z wcześniejszymi wykresami.
FREQ_MIN_HZ = 0.0
FREQ_MAX_HZ = 125_000.0

# Parametry pomocniczego wygładzania używanego WYŁĄCZNIE do wyznaczenia masek.
MEDIAN_TIME_S = 2.0
MEDIAN_FREQ_HZ = 2_000.0

# Piksel jest liczony, gdy:
# filtered_psd > THRESHOLD_DB
THRESHOLD_DB = 6.0

# Nazwy folderów.
AE_DIR_NAME = "AE"
OSCY_SOURCE_DIR_NAME = "oscy1"
FILTERED_SAMPLE_SUFFIX = " - filtered"
FILTERED_SEGMENTS_DIR_NAME = "filtered_segments"

# Wznawianie pracy po błędzie / przerwaniu.
# True = istniejące foldery "<próbka> - filtered" są sprawdzane, a kompletne
# wyniki są pomijane. Wyniki niepełne lub niezgodne są budowane od nowa tylko
# dla danej próbki; pozostałe próbki nie są ponownie filtrowane.
RESUME_EXISTING_FILTERED_RESULTS = True
GENERATED_COPY_SENTINEL_NAME = ".foscy_generated_copy.json"
RESUME_STATE_NAME = ".foscy_resume_state.json"
RESUME_ALGORITHM_ID = "min_time_min_segments_max_samples_v1"

# Nazwy wyników numerycznych.
COUNTS_CSV_NAME = "counts_vs_time_0p01s.csv"
BACKGROUND_NPY_NAME = "background_by_freq.npy"
BACKGROUND_CSV_NAME = "background_by_freq.csv"
BACKGROUND_INDIVIDUAL_NPY_NAME = "background_by_freq_individual.npy"
BACKGROUND_INDIVIDUAL_CSV_NAME = "background_by_freq_individual.csv"
BACKGROUND_SERIES_UPPER_NPY_NAME = "background_by_freq_series_upper.npy"
BACKGROUND_SERIES_UPPER_CSV_NAME = "background_by_freq_series_upper.csv"
PARAMS_JSON_NAME = "params.json"

# Dodatkowa krzywa energii spektralnej. Nazwa zawiera pełną nazwę próbki,
# aby plik pozostał jednoznaczny po skopiowaniu poza własny folder AE.
ENERGY_CSV_NAME = "spectral_excess_energy_vs_time_0p01s.csv"
ENERGY_REPORT_ALGORITHM_ID = "spectral_excess_energy_common_mask_only_v2"
OBSOLETE_CUM_EN_6DB_NAME = "CumEn6dB_4000.txt"

# Zgodnie z interpretacją PSD: PSD_dB = 10 * log10(PSD_linear).
# Jeżeli upstream generowałby dB amplitudowe, wartość należałoby zmienić na 20,
# ale dla PSD poprawny jest mianownik 10.
PSD_DB_FACTOR = 10.0

# Wspólny wynik dla całej serii, zapisywany w katalogu, w którym leży FOscy.py.
SERIES_MASK_NPY_NAME = "FOscy_series_upper_envelope_mask.npy"
SERIES_MASK_CSV_NAME = "FOscy_series_upper_envelope_mask.csv"

# TIFF końcowy: krótkie nazwy celowo ograniczają długość pełnej ścieżki
# w Windows. Plik leży we własnym folderze próbki, więc nie ma kolizji.
# Używamy wyłącznie rozszerzenia .tiff.
LINEAR_TIFF_NAME = "PSD.tiff"
OBSOLETE_LOG_TIFF_NAME = "PSD_log.tiff"

# Limit dotyczy KAŻDEGO TIFF-a osobno i opiera się na nieskompresowanym RGB
# (3 kanały uint8). Skrypt automatycznie dobiera całkowity współczynnik
# redukcji osi czasu, aby pojedynczy TIFF nie przekraczał około tej wartości
# na 1000 s pomiaru. Dla obecnych danych (257 × ok. 1953 ramek/s) wyjdzie
# współczynnik 2: ok. 0.75 GB / 1000 s na pojedynczy TIFF.
TIFF_TARGET_BYTES_PER_1000_S = 1_000_000_000

# Downsampling dotyczy WYŁĄCZNIE obrazów TIFF. Jeden piksel TIFF po czasie
# dostaje maksimum z kolejnych ramek filtered PSD. Dzięki temu krótkie,
# silne zdarzenia nie są uśredniane ani zacierane. Dane dokładne, liczniki
# oraz filtered_segments/*.npy pozostają w pełnej rozdzielczości.
TIFF_TIME_POOLING = "max"

# Wielkość kafelków TIFF. Oba wymiary muszą być wielokrotnością 16.
TIFF_TILE_HEIGHT = 16
TIFF_TILE_WIDTH = 4096

# Oś częstotliwości w obrazie: True = wysokie częstotliwości na górze.
FLIP_VERTICAL_FOR_TIFF = True

# Zakres i paleta obrazów TIFF.
# Dane filtered_psd pozostają float32 w *.npy; poniższe ustawienia dotyczą
# WYŁĄCZNIE wizualizacji. Skala jest zakotwiczona w progu detekcji:
#   0 dB  -> niemal czarne tło,
#   6 dB  -> pierwszy wyraźny kolor (cyjan),
#   >6 dB -> zielony / żółty / pomarańcz / magenta.
# Dzięki temu tło po odjęciu maski nie wygląda jak "gorący" sygnał.
COLOR_MIN_DB = -6.0
COLOR_MAX_DB = 25.0

# Dawne parametry PSD_log.tiff zostają wyłącznie w podpisach kompatybilności
# cache'u z wcześniejszymi wersjami. Aktualna wersja nie generuje PSD_log.tiff.
LOG_LOW_SOFTENING_DB = 1.0
LOG_HIGH_SOFTENING_DB = 3.0

# Węzły palety pod tło dla czarnego/granatowego wykresu DIAdem:
# biały -> zielony -> ciemny żółty -> pomarańczowy -> czerwony -> bordowy.
# Bez czerni, granatu i cyjanu, żeby krzywe nałożone w DIAdem pozostawały czytelne.
COLOR_STOPS_DB = np.array(
    [
        -6.0,
        -3.0,
        0.0,
        2.0,
        4.0,
        6.0,
        8.0,
        10.0,
        12.0,
        15.0,
        18.0,
        21.0,
        25.0,
    ],
    dtype=np.float32,
)

COLOR_STOPS_RGB = np.array(
    [
        [252, 252, 250],  # -6 dB  prawie białe
        [245, 245, 240],  # -3 dB  bardzo jasne tło
        [236, 239, 228],  #  0 dB  złamane jasne tło
        [210, 232, 190],  #  2 dB  bardzo jasna zieleń
        [164, 214, 120],  #  4 dB  zieleń
        [110, 186, 72],   #  6 dB  mocniejsza zieleń
        [175, 176, 52],   #  8 dB  oliwkowo-ciemny żółty
        [213, 177, 34],   # 10 dB  ciemny żółty
        [232, 145, 26],   # 12 dB  pomarańcz
        [225, 101, 24],   # 15 dB  mocniejszy pomarańcz
        [210, 54, 36],    # 18 dB  czerwony
        [178, 28, 36],    # 21 dB  ciemniejsza czerwień
        [122, 24, 38],    # 25 dB  bordowy
    ],
    dtype=np.uint8,
)
LUT_SIZE = 4096

# Parametry palety FOscy_v13, wyłącznie do rozpoznania poprawnego cache PASS 1
# utworzonego poprzednią wersją. Pozwala v14 odtworzyć same TIFF-y bez
# przeliczania median i masek indywidualnych.
V13_LEGACY_COLOR_MIN_DB = -10.0
V13_LEGACY_COLOR_MAX_DB = 25.0
V13_LEGACY_LOG_DISPLAY_MAX_DB = 25.0
V13_LEGACY_COLOR_STOPS_DB = np.array(
    [-10.0, -7.5, -5.0, -2.5, 0.0, 2.5, 5.0, 7.5, 10.0, 12.5,
     15.0, 17.5, 20.0, 22.5, 25.0],
    dtype=np.float32,
)
V13_LEGACY_COLOR_STOPS_RGB = np.array(
    [
        [128, 128, 128], [58, 142, 185], [0, 220, 242], [35, 205, 83],
        [214, 235, 16], [255, 211, 0], [255, 156, 0], [255, 82, 48],
        [255, 93, 121], [255, 8, 238], [234, 18, 137], [239, 6, 6],
        [161, 18, 18], [118, 0, 0], [20, 20, 20],
    ],
    dtype=np.uint8,
)


# =============================================================================
# FUNKCJE OGÓLNE
# =============================================================================

def natural_key(path_obj: Path) -> list[Any]:
    """Sortowanie naturalne: segment_2.npy przed segment_10.npy."""
    return [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"(\d+)", path_obj.name)
    ]


def close_memmap(array: Any) -> None:
    """Bezpiecznie zamyka mmap utworzony przez np.load(..., mmap_mode='r')."""
    mmap_obj = getattr(array, "_mmap", None)
    if mmap_obj is not None:
        mmap_obj.close()


def odd_window_from_physical(window_size: float, step_size: float) -> int:
    """Zamienia rozmiar okna w s/Hz na nieparzystą liczbę binów."""
    if step_size <= 0:
        raise ValueError("Krok osi musi być dodatni.")

    bins = max(1, int(round(window_size / step_size)))
    if bins % 2 == 0:
        bins += 1
    return bins


def make_foscy_filename(sample_name: str) -> str:
    """
    Nazwa jest literalnie tworzona jako:
        F + nazwa_folderu_próbki + _4000.txt

    Przykład:
        f451310 -> Ff451310_4000.txt
    """
    sample_name = sample_name.strip()
    if not sample_name:
        raise ValueError("Nazwa próbki jest pusta.")
    return f"F{sample_name}_4000.txt"


def make_cum_en_filename(sample_name: str) -> str:
    """Nazwa skumulowanej energii ponad wspólną maską dla całego pasma."""
    if not sample_name.strip():
        raise ValueError("Nazwa próbki jest pusta.")
    return "CumEn_4000.txt"


def format_khz_for_filename(value_khz: float) -> str:
    """Zamienia częstotliwość w kHz na bezpieczny fragment nazwy pliku."""
    value = float(value_khz)
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return (f"{value:.6g}".replace(".", "p").replace("-", "m"))


def energy_band_file_label(band_khz: tuple[float, float]) -> str:
    low_khz, high_khz = band_khz
    return f"{format_khz_for_filename(low_khz)}-{format_khz_for_filename(high_khz)}kHz"


def energy_band_column_label(band_khz: tuple[float, float]) -> str:
    return f"CumEn_{energy_band_file_label(band_khz).replace('-', '_')}"


def make_cum_en_band_filename(band_khz: tuple[float, float]) -> str:
    """Nazwa skumulowanej energii dla wybranego pasma częstotliwości."""
    return f"CumEn_{energy_band_file_label(band_khz)}_4000.txt"


def parse_single_energy_band_khz(raw_values: list[str]) -> tuple[float, float]:
    """Parsuje --cum-en-band-khz w formie '50-125' albo '50 125'."""
    if len(raw_values) == 1:
        text = raw_values[0].strip().lower()
        text = text.replace("khz", "").replace(" ", "").replace(",", ".")
        if "-" in text:
            low_text, high_text = text.split("-", 1)
        elif ":" in text:
            low_text, high_text = text.split(":", 1)
        else:
            raise argparse.ArgumentTypeError(
                "Zakres pasma podaj jako 50-125 albo jako dwa argumenty: 50 125."
            )
    elif len(raw_values) == 2:
        low_text = raw_values[0].replace(",", ".")
        high_text = raw_values[1].replace(",", ".")
    else:
        raise argparse.ArgumentTypeError(
            "Jeden zakres częstotliwości wymaga formy 50-125 albo dwóch liczb: 50 125."
        )

    try:
        low_khz = float(low_text)
        high_khz = float(high_text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Nie można odczytać zakresu częstotliwości: {' '.join(raw_values)}"
        ) from exc

    if not math.isfinite(low_khz) or not math.isfinite(high_khz):
        raise argparse.ArgumentTypeError("Zakres częstotliwości musi być skończony.")
    if low_khz < 0 or high_khz < 0:
        raise argparse.ArgumentTypeError("Zakres częstotliwości nie może być ujemny.")
    if low_khz >= high_khz:
        raise argparse.ArgumentTypeError("Dolna granica pasma musi być mniejsza od górnej.")

    max_config_khz = FREQ_MAX_HZ / 1000.0
    min_config_khz = FREQ_MIN_HZ / 1000.0
    if low_khz < min_config_khz or high_khz > max_config_khz:
        raise argparse.ArgumentTypeError(
            f"Zakres {low_khz:g}-{high_khz:g} kHz wychodzi poza oś spektrogramu "
            f"{min_config_khz:g}-{max_config_khz:g} kHz."
        )
    return (low_khz, high_khz)


def parse_cli_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Batchowe filtrowanie spektrogramów FOscy oraz raporty CumEn. "
            "Opcjonalnie generuje dodatkowe pliki CumEn dla wybranych pasm kHz."
        )
    )
    parser.add_argument(
        "--cum-en-band-khz",
        "--cum-en-range-khz",
        "--energy-band-khz",
        action="append",
        nargs="+",
        default=[],
        metavar=("LOW", "HIGH"),
        help=(
            "Dodatkowe pasmo do skumulowanej energii, np. "
            "--cum-en-band-khz 50-125 albo --cum-en-band-khz 50 125. "
            "Opcję można powtórzyć dla kilku pasm."
        ),
    )
    args = parser.parse_args()
    args.cum_en_bands_khz = [
        parse_single_energy_band_khz(values)
        for values in args.cum_en_band_khz
    ]
    # Usunięcie duplikatów z zachowaniem kolejności.
    seen: set[tuple[float, float]] = set()
    unique: list[tuple[float, float]] = []
    for band in args.cum_en_bands_khz:
        key = (round(band[0], 9), round(band[1], 9))
        if key not in seen:
            seen.add(key)
            unique.append(band)
    args.cum_en_bands_khz = unique
    return args


def normalised_energy_bands_payload(
    energy_bands_khz: list[tuple[float, float]] | None,
) -> list[dict[str, float]]:
    return [
        {"low_khz": float(low), "high_khz": float(high)}
        for low, high in (energy_bands_khz or [])
    ]


def energy_report_settings_signature(
    energy_bands_khz: list[tuple[float, float]] | None = None,
) -> str:
    """Podpis ustawień wpływających na dodatkowe krzywe energii."""
    return json_sha256(
        {
            "algorithm": ENERGY_REPORT_ALGORITHM_ID,
            "count_bin_s": COUNT_BIN_S,
            "file_duration_s": FILE_DURATION_S,
            "freq_min_hz": FREQ_MIN_HZ,
            "freq_max_hz": FREQ_MAX_HZ,
            "psd_db_factor": PSD_DB_FACTOR,
            "requested_energy_bands_khz": normalised_energy_bands_payload(energy_bands_khz),
            "energy_mask_definition": (
                "sum(max(10**(original_psd_db/10)-10**(mask_db/10),0)*df*dt)"
            ),
            "band_energy_definition": (
                "same energy definition, but summed only over frequency bins inside "
                "the requested inclusive kHz band"
            ),
        }
    )

def warn_if_windows_path_is_long(path: Path, *, label: str) -> None:
    """Wypisuje ostrzeżenie dla ścieżek zbliżających się do limitu Win32."""
    if os.name != "nt":
        return

    length = len(str(path.resolve()))
    if length >= 240:
        print(
            f"[OSTRZEŻENIE] Długa ścieżka Windows ({length} znaków) dla {label}:\n"
            f"  {path}\n"
            "Jeżeli zapis zakończy się WinError 206, przenieś folder serii bliżej "
            "korzenia dysku, np. D:\\AE\\seria.",
            flush=True,
        )


def assert_tiff_tile_config() -> None:
    """Sprawdza wymagania TIFF dla kafelków i temporalnego downsamplingu."""
    if TIFF_TILE_HEIGHT <= 0 or TIFF_TILE_WIDTH <= 0:
        raise ValueError("Wymiary kafelków TIFF muszą być dodatnie.")
    if TIFF_TILE_HEIGHT % 16 != 0 or TIFF_TILE_WIDTH % 16 != 0:
        raise ValueError(
            "TIFF_TILE_HEIGHT oraz TIFF_TILE_WIDTH muszą być wielokrotnością 16."
        )
    if TIFF_TARGET_BYTES_PER_1000_S <= 0:
        raise ValueError("TIFF_TARGET_BYTES_PER_1000_S musi być dodatnie.")
    if TIFF_TIME_POOLING != "max":
        raise ValueError("Obsługiwany jest wyłącznie TIFF_TIME_POOLING = 'max'.")


def tiff_time_pool_factor(
    *,
    n_freq: int,
    total_time_frames: int,
    n_segments: int,
) -> int:
    """
    Dobiera całkowity współczynnik redukcji osi czasu dla JEDNEGO RGB TIFF-a.

    Używamy rzeczywistej liczby ramek i nominalnego czasu segmentów, ponieważ
    ostatni segment może mieć krótszą liczbę ramek, ale w pipeline pozostaje
    segmentem 25-sekundowym.
    """
    if n_freq <= 0 or total_time_frames <= 0 or n_segments <= 0:
        raise ValueError("Nieprawidłowe wymiary do wyznaczenia downsamplingu TIFF.")

    nominal_duration_s = n_segments * FILE_DURATION_S
    frames_per_second = total_time_frames / nominal_duration_s
    estimated_bytes_per_1000_s = n_freq * frames_per_second * 1000.0 * 3.0

    return max(
        1,
        int(math.ceil(estimated_bytes_per_1000_s / TIFF_TARGET_BYTES_PER_1000_S)),
    )


def tiff_output_time_pixels(total_time_frames: int, time_pool_factor: int) -> int:
    """Liczba pikseli osi czasu po grupowaniu kolejnych ramek w bloki."""
    if total_time_frames <= 0 or time_pool_factor <= 0:
        raise ValueError("Nieprawidłowe parametry osi czasu TIFF.")
    return int(math.ceil(total_time_frames / time_pool_factor))


# =============================================================================
# ODKRYWANIE PRÓBEK I BUDOWANIE FOLDERÓW WYNIKOWYCH
# =============================================================================

def find_source_samples(series_root: Path) -> list[Path]:
    """
    Zwraca tylko bezpośrednie foldery próbek o strukturze:
        <próbka>/AE/oscy1/*.npy

    Foldery " - filtered" są pomijane.
    """
    samples: list[Path] = []

    for child in sorted(series_root.iterdir(), key=natural_key):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if child.name.endswith(FILTERED_SAMPLE_SUFFIX):
            continue

        raw_oscy_dir = child / AE_DIR_NAME / OSCY_SOURCE_DIR_NAME
        if raw_oscy_dir.is_dir() and any(raw_oscy_dir.glob("*.npy")):
            samples.append(child)

    return samples


def copy_ignore_function(current_dir: str, names: list[str]) -> set[str]:
    """
    Przy kopiowaniu próbki do " - filtered" nie kopiuje:
    - surowego AE/oscy1,
    - starych wyników oscy1-filtered,
    - cache Python.

    Dzięki temu wynikowe AE/oscy1 zawiera wyłącznie nowe dane przefiltrowane.
    """
    current_path = Path(current_dir)
    ignored: set[str] = set()

    for name in names:
        if name in {"__pycache__", "oscy1-filtered"}:
            ignored.add(name)

    if current_path.name == AE_DIR_NAME and OSCY_SOURCE_DIR_NAME in names:
        ignored.add(OSCY_SOURCE_DIR_NAME)

    return ignored


def write_generated_copy_sentinel(target_sample_dir: Path, source_sample_dir: Path) -> None:
    """Zapisuje znacznik pozwalający bezpiecznie odtworzyć kopię przy kolejnym runie."""
    payload = {
        "generated_by": "FOscy.py",
        "source_sample_dir": str(source_sample_dir),
        "target_sample_dir": str(target_sample_dir),
        "raw_oscy_not_copied": True,
    }
    (target_sample_dir / GENERATED_COPY_SENTINEL_NAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )



def inherit_filtered_sample_dirnames(
    target_sample_dir: Path,
    source_sample_name: str,
) -> int:
    """
    Dziedziczy przyrostek `` - filtered`` wyłącznie dla folderu próbki
    leżącego bezpośrednio obok ``AE``.

    Jeżeli kopia ma układ::

        f451310 - filtered/
        ├── AE/
        └── f451310/

    to ostatni katalog zostaje przemianowany na::

        f451310 - filtered/
        ├── AE/
        └── f451310 - filtered/

    Katalogi wewnątrz ``AE`` nie są przez tę funkcję przeglądane ani
    modyfikowane. Gdy poprawna nazwa już istnieje, zawartość jest scalana,
    a stary katalog usuwany.
    """
    target_name = target_sample_dir.name
    if source_sample_name == target_name:
        return 0

    old_dir = target_sample_dir / source_sample_name
    if not old_dir.exists():
        return 0
    if not old_dir.is_dir():
        raise RuntimeError(
            "Nie mogę odziedziczyć nazwy folderu, ponieważ bezpośrednio "
            "w folderze próbki istnieje plik zamiast katalogu:\n"
            f"  {old_dir}"
        )

    new_dir = target_sample_dir / target_name
    if new_dir.exists():
        if not new_dir.is_dir():
            raise RuntimeError(
                "Nie mogę odziedziczyć nazwy folderu, ponieważ pod ścieżką "
                "docelową istnieje plik:\n"
                f"  źródło: {old_dir}\n"
                f"  cel:    {new_dir}"
            )
        shutil.copytree(old_dir, new_dir, dirs_exist_ok=True)
        shutil.rmtree(old_dir)
    else:
        old_dir.rename(new_dir)

    return 1


def prepare_filtered_copy(source_sample_dir: Path) -> Path:
    """
    Zapewnia folder "<próbka> - filtered" bez surowego AE/oscy1.

    W kopii tylko bezpośredni folder próbki, leżący obok ``AE`` i nazwany
    dokładnie jak próbka źródłowa, dziedziczy przyrostek `` - filtered``.

    Nowy folder jest kopiowany pełnie (z pominięciem raw oscy1). Istniejący
    folder wygenerowany przez FOscy nie jest usuwany: uzupełniamy w nim jedynie
    pliki źródłowe inne niż raw oscy1, a następnie mechanizm wznawiania decyduje,
    czy wyniki filtrowania są kompletne i aktualne.
    """
    target_sample_dir = source_sample_dir.with_name(
        f"{source_sample_dir.name}{FILTERED_SAMPLE_SUFFIX}"
    )

    if not target_sample_dir.exists():
        shutil.copytree(
            source_sample_dir,
            target_sample_dir,
            ignore=copy_ignore_function,
        )
        (target_sample_dir / AE_DIR_NAME / OSCY_SOURCE_DIR_NAME).mkdir(
            parents=True,
            exist_ok=True,
        )
        write_generated_copy_sentinel(target_sample_dir, source_sample_dir)
        inherited_count = inherit_filtered_sample_dirnames(
            target_sample_dir,
            source_sample_dir.name,
        )
        print(f"[{source_sample_dir.name}] folder wynikowy: UTWORZONY", flush=True)
        if inherited_count:
            print(
                f"[{source_sample_dir.name}] dziedziczenie '- filtered': "
                f"zmieniono nazwę folderu próbki obok AE",
                flush=True,
            )
        return target_sample_dir

    sentinel_path = target_sample_dir / GENERATED_COPY_SENTINEL_NAME
    if not sentinel_path.exists():
        raise RuntimeError(
            "Istnieje folder o nazwie wyglądającej na wynikową, ale bez znacznika "
            "bezpieczeństwa. Nie modyfikuję go automatycznie:\n"
            f"  {target_sample_dir}\n"
            f"Brakuje: {GENERATED_COPY_SENTINEL_NAME}\n"
            "Usuń/zmień nazwę tego folderu albo dodaj znacznik tylko wtedy, gdy "
            "masz pewność, że został utworzony przez FOscy."
        )

    # Aktualizujemy specimen.dat, WAV-y i inne pliki pomocnicze źródła, ale nie
    # dotykamy AE/oscy1 w źródle ani wyników już leżących w celu.
    shutil.copytree(
        source_sample_dir,
        target_sample_dir,
        dirs_exist_ok=True,
        ignore=copy_ignore_function,
    )
    (target_sample_dir / AE_DIR_NAME / OSCY_SOURCE_DIR_NAME).mkdir(
        parents=True,
        exist_ok=True,
    )
    inherited_count = inherit_filtered_sample_dirnames(
        target_sample_dir,
        source_sample_dir.name,
    )
    print(f"[{source_sample_dir.name}] folder wynikowy: ISTNIEJE — sprawdzam/wznawiam", flush=True)
    if inherited_count:
        print(
            f"[{source_sample_dir.name}] dziedziczenie '- filtered': "
            f"zmieniono nazwę folderu próbki obok AE",
            flush=True,
        )
    return target_sample_dir


def remove_obsolete_log_tiff(target_sample_dir: Path) -> bool:
    """Usuwa dawny PSD_log.tiff, bo aktualna wersja generuje wyłącznie PSD.tiff."""
    obsolete_path = (
        target_sample_dir
        / AE_DIR_NAME
        / OSCY_SOURCE_DIR_NAME
        / OBSOLETE_LOG_TIFF_NAME
    )
    if obsolete_path.exists():
        obsolete_path.unlink()
        return True
    return False


# =============================================================================
# WZNOWIENIE PRACY I WALIDACJA WYNIKÓW
# =============================================================================

def json_sha256(payload: Any) -> str:
    """Stabilny SHA-256 dla danych JSON-serializowalnych."""
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def array_sha256(array: np.ndarray) -> str:
    """SHA-256 wartości tablicy float32, niezależny od ścieżki pliku."""
    contiguous = np.ascontiguousarray(np.asarray(array, dtype=np.float32))
    return hashlib.sha256(contiguous.tobytes()).hexdigest()


def processing_settings_signature() -> str:
    """Podpis wszystkich ustawień wpływających na maskę, dane lub TIFF-y."""
    payload = {
        "algorithm": RESUME_ALGORITHM_ID,
        "file_duration_s": FILE_DURATION_S,
        "count_bin_s": COUNT_BIN_S,
        "freq_min_hz": FREQ_MIN_HZ,
        "freq_max_hz": FREQ_MAX_HZ,
        "median_time_s": MEDIAN_TIME_S,
        "median_freq_hz": MEDIAN_FREQ_HZ,
        "threshold_db": THRESHOLD_DB,
        "linear_tiff_name": LINEAR_TIFF_NAME,
        "log_tiff_name": OBSOLETE_LOG_TIFF_NAME,
        "tiff_tile_height": TIFF_TILE_HEIGHT,
        "tiff_tile_width": TIFF_TILE_WIDTH,
        "flip_vertical_for_tiff": FLIP_VERTICAL_FOR_TIFF,
        "color_min_db": COLOR_MIN_DB,
        "color_max_db": COLOR_MAX_DB,
        "log_low_softening_db": LOG_LOW_SOFTENING_DB,
        "log_high_softening_db": LOG_HIGH_SOFTENING_DB,
        "color_stops_db": COLOR_STOPS_DB.astype(float).tolist(),
        "color_stops_rgb": COLOR_STOPS_RGB.astype(int).tolist(),
    }
    return json_sha256(payload)


def mask_settings_signature() -> str:
    """Podpis ustawień wpływających wyłącznie na PASS 1 / maski."""
    payload = {
        "algorithm": RESUME_ALGORITHM_ID,
        "file_duration_s": FILE_DURATION_S,
        "freq_min_hz": FREQ_MIN_HZ,
        "freq_max_hz": FREQ_MAX_HZ,
        "median_time_s": MEDIAN_TIME_S,
        "median_freq_hz": MEDIAN_FREQ_HZ,
    }
    return json_sha256(payload)


def v13_legacy_full_settings_signature() -> str:
    """Pełny podpis FOscy_v13 używany tylko do przejęcia jego cache PASS 1."""
    payload = {
        "algorithm": RESUME_ALGORITHM_ID,
        "file_duration_s": FILE_DURATION_S,
        "count_bin_s": COUNT_BIN_S,
        "freq_min_hz": FREQ_MIN_HZ,
        "freq_max_hz": FREQ_MAX_HZ,
        "median_time_s": MEDIAN_TIME_S,
        "median_freq_hz": MEDIAN_FREQ_HZ,
        "threshold_db": THRESHOLD_DB,
        "linear_tiff_name": LINEAR_TIFF_NAME,
        "log_tiff_name": OBSOLETE_LOG_TIFF_NAME,
        "tiff_tile_height": TIFF_TILE_HEIGHT,
        "tiff_tile_width": TIFF_TILE_WIDTH,
        "flip_vertical_for_tiff": FLIP_VERTICAL_FOR_TIFF,
        "color_min_db": V13_LEGACY_COLOR_MIN_DB,
        "color_max_db": V13_LEGACY_COLOR_MAX_DB,
        "log_display_max_db": V13_LEGACY_LOG_DISPLAY_MAX_DB,
        "color_stops_db": V13_LEGACY_COLOR_STOPS_DB.astype(float).tolist(),
        "color_stops_rgb": V13_LEGACY_COLOR_STOPS_RGB.astype(int).tolist(),
    }
    return json_sha256(payload)


def source_descriptor(source_sample_dir: Path) -> dict[str, Any]:
    """Opis wejścia używany do bezpiecznego wznowienia pracy."""
    input_oscy_dir = source_sample_dir / AE_DIR_NAME / OSCY_SOURCE_DIR_NAME
    shapes, n_freq, total_time_frames = read_and_validate_segment_shapes(input_oscy_dir)

    segment_records: list[dict[str, Any]] = []
    for path, freq_bins, time_bins in shapes:
        stat = path.stat()
        segment_records.append(
            {
                "name": path.name,
                "bytes": int(stat.st_size),
                "mtime_ns": int(stat.st_mtime_ns),
                "shape": [int(freq_bins), int(time_bins)],
            }
        )

    signature_payload = {
        "source_sample_name": source_sample_dir.name,
        "segments": segment_records,
    }
    return {
        "source_signature": json_sha256(signature_payload),
        "n_freq": int(n_freq),
        "total_time_frames": int(total_time_frames),
        "segment_records": segment_records,
        "segment_names": [record["name"] for record in segment_records],
        "newest_source_mtime_ns": max(record["mtime_ns"] for record in segment_records),
    }


def resume_state_path(target_sample_dir: Path) -> Path:
    return target_sample_dir / AE_DIR_NAME / OSCY_SOURCE_DIR_NAME / RESUME_STATE_NAME


def load_json_safely(path: Path) -> dict[str, Any] | None:
    try:
        with open(path, "r", encoding="utf-8") as file:
            payload = json.load(file)
        return payload if isinstance(payload, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Zapis atomowy: przerwany zapis nie może udawać kompletnego stanu."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f"{path.name}.tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary_path, path)


def make_resume_state_base(
    *,
    source_sample_dir: Path,
    descriptor: dict[str, Any],
    settings_signature: str,
    individual_mask_sha256: str,
) -> dict[str, Any]:
    return {
        "algorithm": RESUME_ALGORITHM_ID,
        "source_sample_name": source_sample_dir.name,
        "source_signature": descriptor["source_signature"],
        "settings_signature": settings_signature,
        "mask_settings_signature": mask_settings_signature(),
        "n_frequency_bins": descriptor["n_freq"],
        "total_time_frames": descriptor["total_time_frames"],
        "individual_mask_sha256": individual_mask_sha256,
    }


def resume_state_matches_base(
    state: dict[str, Any] | None,
    *,
    source_sample_dir: Path,
    descriptor: dict[str, Any],
    settings_signature: str,
) -> bool:
    if not state:
        return False
    return (
        state.get("algorithm") == RESUME_ALGORITHM_ID
        and state.get("source_sample_name") == source_sample_dir.name
        and state.get("source_signature") == descriptor["source_signature"]
        and state.get("settings_signature") == settings_signature
        and state.get("n_frequency_bins") == descriptor["n_freq"]
        and state.get("total_time_frames") == descriptor["total_time_frames"]
    )


def write_mask_ready_state(
    *,
    source_sample_dir: Path,
    target_sample_dir: Path,
    descriptor: dict[str, Any],
    settings_signature: str,
    individual_mask: np.ndarray,
) -> None:
    """Zapisuje bezpieczny cache PASS 1, zanim zacznie się kosztowny PASS 2."""
    output_dir = target_sample_dir / AE_DIR_NAME / OSCY_SOURCE_DIR_NAME
    output_dir.mkdir(parents=True, exist_ok=True)

    freq_axis_hz = np.linspace(
        FREQ_MIN_HZ,
        FREQ_MAX_HZ,
        descriptor["n_freq"],
        dtype=np.float64,
    )
    np.save(output_dir / BACKGROUND_INDIVIDUAL_NPY_NAME, individual_mask)
    save_background_csv(
        output_dir / BACKGROUND_INDIVIDUAL_CSV_NAME,
        freq_axis_hz,
        individual_mask,
    )

    state = make_resume_state_base(
        source_sample_dir=source_sample_dir,
        descriptor=descriptor,
        settings_signature=settings_signature,
        individual_mask_sha256=array_sha256(individual_mask),
    )
    state["stage"] = "mask_ready"
    atomic_write_json(resume_state_path(target_sample_dir), state)


def load_reusable_individual_mask(
    *,
    source_sample_dir: Path,
    target_sample_dir: Path,
    descriptor: dict[str, Any],
    settings_signature: str,
) -> np.ndarray | None:
    """Odczytuje cache PASS 1 niezależnie od zmian czysto wizualnych TIFF-a.

    Dla v14 zgodność maski sprawdza osobny ``mask_settings_signature``. Dla
    istniejących cache FOscy_v13 dopuszczamy jego dokładny dawny pełny podpis,
    aby zmiana palety odtworzyła tylko PASS 2, a nie mediany/maski.
    """
    if not RESUME_EXISTING_FILTERED_RESULTS:
        return None

    state = load_json_safely(resume_state_path(target_sample_dir))
    if not state:
        return None

    basic_match = (
        state.get("algorithm") == RESUME_ALGORITHM_ID
        and state.get("source_sample_name") == source_sample_dir.name
        and state.get("source_signature") == descriptor["source_signature"]
        and state.get("n_frequency_bins") == descriptor["n_freq"]
        and state.get("total_time_frames") == descriptor["total_time_frames"]
        and state.get("stage") in {"mask_ready", "complete"}
    )
    if not basic_match:
        return None

    current_mask_signature = mask_settings_signature()
    is_v14_or_newer_mask = (
        state.get("mask_settings_signature") == current_mask_signature
    )
    is_compatible_v13_mask = (
        state.get("settings_signature") == v13_legacy_full_settings_signature()
    )
    if not (is_v14_or_newer_mask or is_compatible_v13_mask):
        return None

    mask_path = (
        target_sample_dir / AE_DIR_NAME / OSCY_SOURCE_DIR_NAME
        / BACKGROUND_INDIVIDUAL_NPY_NAME
    )
    try:
        mask = np.asarray(np.load(mask_path), dtype=np.float32)
    except (OSError, ValueError):
        return None

    if mask.ndim != 1 or len(mask) != descriptor["n_freq"]:
        return None
    if state.get("individual_mask_sha256") != array_sha256(mask):
        return None
    return mask


def expected_filtered_segment_names(descriptor: dict[str, Any]) -> list[str]:
    return [f"{Path(name).stem}_filtered.npy" for name in descriptor["segment_names"]]


def validate_array_file(
    path: Path,
    expected: np.ndarray,
) -> str | None:
    if not path.is_file():
        return f"brak {path.name}"
    try:
        actual = np.asarray(np.load(path), dtype=np.float32)
    except Exception as exc:  # noqa: BLE001
        return f"nie można odczytać {path.name}: {type(exc).__name__}"
    if actual.shape != expected.shape:
        return f"zły rozmiar {path.name}: {actual.shape}, oczekiwano {expected.shape}"
    if not np.array_equal(actual, expected):
        return f"zawartość {path.name} nie odpowiada aktualnej masce"
    return None


def validate_filtered_segment_file(path: Path, expected_shape: tuple[int, int]) -> str | None:
    if not path.is_file():
        return f"brak {path.name}"
    try:
        segment = np.load(path, mmap_mode="r")
        try:
            if tuple(segment.shape) != expected_shape:
                return (
                    f"zły rozmiar {path.name}: {tuple(segment.shape)}, "
                    f"oczekiwano {expected_shape}"
                )
            if segment.dtype != np.float32:
                return f"zły dtype {path.name}: {segment.dtype}, oczekiwano float32"
        finally:
            close_memmap(segment)
    except Exception as exc:  # noqa: BLE001
        return f"nie można odczytać {path.name}: {type(exc).__name__}"
    return None


def count_data_rows(path: Path) -> int:
    with open(path, "r", encoding="utf-8", errors="replace") as file:
        next(file, None)
        return sum(1 for _ in file)


def validate_tiff_file(path: Path, expected_shape: tuple[int, int, int]) -> str | None:
    if not path.is_file() or path.stat().st_size == 0:
        return f"brak albo pusty {path.name}"
    try:
        with tifffile.TiffFile(path) as tif:
            page = tif.pages[0]
            if tuple(page.shape) != expected_shape:
                return (
                    f"zły rozmiar {path.name}: {tuple(page.shape)}, "
                    f"oczekiwano {expected_shape}"
                )
            if not page.is_tiled:
                return f"{path.name} nie jest TIFF-em kafelkowanym"
    except Exception as exc:  # noqa: BLE001
        return f"nie można odczytać {path.name}: {type(exc).__name__}"
    return None


def validate_params_file(
    path: Path,
    *,
    descriptor: dict[str, Any],
) -> str | None:
    params = load_json_safely(path)
    if params is None:
        return f"brak albo uszkodzony {path.name}"

    checks = {
        "file_duration_s": FILE_DURATION_S,
        "count_bin_s": COUNT_BIN_S,
        "freq_min_hz": FREQ_MIN_HZ,
        "freq_max_hz": FREQ_MAX_HZ,
        "median_time_s": MEDIAN_TIME_S,
        "median_freq_hz": MEDIAN_FREQ_HZ,
        "threshold_db": THRESHOLD_DB,
        "n_input_files": len(descriptor["segment_names"]),
        "n_frequency_bins": descriptor["n_freq"],
        "total_time_frames": descriptor["total_time_frames"],
    }
    for key, expected in checks.items():
        if params.get(key) != expected:
            return f"params.json: niezgodne pole {key}"
    return None


def inspect_existing_result(
    *,
    source_sample_dir: Path,
    target_sample_dir: Path,
    descriptor: dict[str, Any],
    settings_signature: str,
    individual_mask: np.ndarray,
    series_upper_mask: np.ndarray,
    series_mask_sha256: str,
    energy_bands_khz: list[tuple[float, float]],
    energy_report_signature: str,
) -> tuple[str, list[str]]:
    """
    Klasyfikuje wynik jako:
      - complete: pełny i zweryfikowany bieżącym stanem;
      - adoptable_legacy: pełny wynik v10/legacy, możliwy do bezpiecznego adopcji;
      - incomplete: brak/niezgodność co najmniej jednego wymaganego elementu.
    """
    if not RESUME_EXISTING_FILTERED_RESULTS:
        return "incomplete", ["wznawianie wyłączone w konfiguracji"]

    output_ae_dir = target_sample_dir / AE_DIR_NAME
    output_dir = output_ae_dir / OSCY_SOURCE_DIR_NAME
    filtered_dir = output_dir / FILTERED_SEGMENTS_DIR_NAME
    reasons: list[str] = []

    sentinel_path = target_sample_dir / GENERATED_COPY_SENTINEL_NAME
    if not sentinel_path.is_file():
        reasons.append(f"brak {GENERATED_COPY_SENTINEL_NAME}")

    # Surowe segmenty nie mogą pojawić się bezpośrednio w wyniku AE/oscy1.
    for raw_name in descriptor["segment_names"]:
        if (output_dir / raw_name).exists():
            reasons.append(f"skopiowano surowy segment do wyniku: {raw_name}")
            break

    array_checks = [
        (output_dir / BACKGROUND_NPY_NAME, series_upper_mask),
        (output_dir / BACKGROUND_INDIVIDUAL_NPY_NAME, individual_mask),
        (output_dir / BACKGROUND_SERIES_UPPER_NPY_NAME, series_upper_mask),
    ]
    for path, expected in array_checks:
        error = validate_array_file(path, expected)
        if error:
            reasons.append(error)

    required_csv = [
        output_dir / BACKGROUND_CSV_NAME,
        output_dir / BACKGROUND_INDIVIDUAL_CSV_NAME,
        output_dir / BACKGROUND_SERIES_UPPER_CSV_NAME,
        output_dir / COUNTS_CSV_NAME,
    ]
    for path in required_csv:
        if not path.is_file() or path.stat().st_size == 0:
            reasons.append(f"brak albo pusty {path.name}")

    expected_shapes = {
        f"{Path(record['name']).stem}_filtered.npy": tuple(record["shape"])
        for record in descriptor["segment_records"]
    }
    for filtered_name, expected_shape in expected_shapes.items():
        error = validate_filtered_segment_file(filtered_dir / filtered_name, expected_shape)
        if error:
            reasons.append(error)

    expected_rows = len(descriptor["segment_names"]) * int(round(FILE_DURATION_S / COUNT_BIN_S))
    counts_path = output_dir / COUNTS_CSV_NAME
    if counts_path.is_file() and counts_path.stat().st_size > 0:
        try:
            if count_data_rows(counts_path) != expected_rows:
                reasons.append(
                    f"{COUNTS_CSV_NAME}: zła liczba wierszy, oczekiwano {expected_rows}"
                )
        except OSError:
            reasons.append(f"nie można odczytać {COUNTS_CSV_NAME}")

    foscy_path = output_ae_dir / make_foscy_filename(source_sample_dir.name)
    if not foscy_path.is_file() or foscy_path.stat().st_size == 0:
        reasons.append(f"brak albo pusty {foscy_path.name}")
    else:
        try:
            with open(foscy_path, "r", encoding="utf-8", errors="replace") as file:
                header = file.readline().strip()
            if header != "time[s]  eventsNo.  EAenergy [arb.units]":
                reasons.append(f"nieprawidłowy nagłówek {foscy_path.name}")
            elif count_data_rows(foscy_path) != expected_rows:
                reasons.append(f"{foscy_path.name}: zła liczba wierszy")
        except OSError:
            reasons.append(f"nie można odczytać {foscy_path.name}")

    # Dodatkowe krzywe energii. Wersje FOscy_v14 i wcześniejsze ich nie
    # miały, dlatego ich brak kwalifikuje wyłącznie do szybkiego uzupełnienia
    # raportów, a nie do ponownego filtrowania / generowania TIFF-ów.
    energy_report_reasons: list[str] = []
    energy_csv_path = output_dir / ENERGY_CSV_NAME
    if not energy_csv_path.is_file() or energy_csv_path.stat().st_size == 0:
        energy_report_reasons.append(f"brak albo pusty {ENERGY_CSV_NAME}")
    else:
        try:
            if count_data_rows(energy_csv_path) != expected_rows:
                energy_report_reasons.append(
                    f"{ENERGY_CSV_NAME}: zła liczba wierszy"
                )
        except OSError:
            energy_report_reasons.append(f"nie można odczytać {ENERGY_CSV_NAME}")

    expected_energy_headers = {
        make_cum_en_filename(source_sample_dir.name): "time[s]  CumEn [arb.units]",
    }
    for band_khz in energy_bands_khz:
        expected_energy_headers[make_cum_en_band_filename(band_khz)] = (
            f"time[s]  {energy_band_column_label(band_khz)} [arb.units]"
        )
    for filename, expected_header in expected_energy_headers.items():
        path = output_ae_dir / filename
        if not path.is_file() or path.stat().st_size == 0:
            energy_report_reasons.append(f"brak albo pusty {filename}")
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as file:
                header = file.readline().strip()
            if header != expected_header:
                energy_report_reasons.append(f"nieprawidłowy nagłówek {filename}")
            elif count_data_rows(path) != expected_rows:
                energy_report_reasons.append(f"{filename}: zła liczba wierszy")
        except OSError:
            energy_report_reasons.append(f"nie można odczytać {filename}")

    time_pool_factor = tiff_time_pool_factor(
        n_freq=descriptor["n_freq"],
        total_time_frames=descriptor["total_time_frames"],
        n_segments=len(descriptor["segment_names"]),
    )
    tiff_shape = (
        descriptor["n_freq"],
        tiff_output_time_pixels(descriptor["total_time_frames"], time_pool_factor),
        3,
    )
    for path in [output_dir / LINEAR_TIFF_NAME]:
        error = validate_tiff_file(path, tiff_shape)
        if error:
            reasons.append(error)

    error = validate_params_file(output_dir / PARAMS_JSON_NAME, descriptor=descriptor)
    if error:
        reasons.append(error)

    if reasons:
        return "incomplete", reasons

    state = load_json_safely(resume_state_path(target_sample_dir))
    base_matches = resume_state_matches_base(
        state,
        source_sample_dir=source_sample_dir,
        descriptor=descriptor,
        settings_signature=settings_signature,
    )
    expected_individual_sha = array_sha256(individual_mask)
    if (
        base_matches
        and state is not None
        and state.get("stage") == "complete"
        and state.get("individual_mask_sha256") == expected_individual_sha
        and state.get("series_mask_sha256") == series_mask_sha256
    ):
        if (
            not energy_report_reasons
            and state.get("energy_report_signature") == energy_report_signature
        ):
            return "complete", []
        return "energy_reports_missing", energy_report_reasons or [
            "brak aktualnego podpisu raportów energii"
        ]

    # Adopcja pełnego wyniku sprzed wprowadzenia pliku resume state. Jest
    # bezpieczna wyłącznie wtedy, gdy params.json powstał nie wcześniej niż
    # wszystkie segmenty źródłowe i wszystkie artefakty odpowiadają bieżącym
    # maskom, parametrom oraz rozmiarom.
    params_path = output_dir / PARAMS_JSON_NAME
    if params_path.stat().st_mtime_ns >= descriptor["newest_source_mtime_ns"]:
        return "adoptable_legacy", ["pełny wynik legacy — zapiszę stan wznowienia"]

    return "incomplete", [
        "brak zgodnego stanu wznowienia, a params.json jest starszy od danych źródłowych"
    ]


def clear_stale_pass2_artifacts(
    *,
    output_ae_dir: Path,
    output_dir: Path,
    sample_name: str,
) -> None:
    """Usuwa wyłącznie artefakty filtrowania danej próbki przed ponownym PASS 2."""
    # WAŻNE: NIE usuwamy cache PASS 1:
    # - background_by_freq_individual.npy,
    # - background_by_freq_individual.csv,
    # - .foscy_resume_state.json ze stage == "mask_ready".
    #
    # Dzięki temu, gdy PASS 2 przerwie się podczas TIFF-a, następne uruchomienie
    # odczyta gotową maskę indywidualną i wznowi dokładnie od PASS 2.
    paths_to_remove = [
        output_dir / FILTERED_SEGMENTS_DIR_NAME,
        output_dir / BACKGROUND_NPY_NAME,
        output_dir / BACKGROUND_CSV_NAME,
        output_dir / BACKGROUND_SERIES_UPPER_NPY_NAME,
        output_dir / BACKGROUND_SERIES_UPPER_CSV_NAME,
        output_dir / COUNTS_CSV_NAME,
        output_dir / ENERGY_CSV_NAME,
        output_dir / PARAMS_JSON_NAME,
        output_dir / LINEAR_TIFF_NAME,
        output_dir / OBSOLETE_LOG_TIFF_NAME,
        output_ae_dir / make_foscy_filename(sample_name),
        output_ae_dir / make_cum_en_filename(sample_name),
        output_ae_dir / OBSOLETE_CUM_EN_6DB_NAME,
    ]
    for path in paths_to_remove:
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()

    # Usuwamy też stare warianty pasmowe CumEn_XX-YYkHz_4000.txt,
    # żeby PASS 2 nie mieszał plików z poprzednich wywołań.
    for path in output_ae_dir.glob("CumEn*_4000.txt"):
        if path.is_file():
            path.unlink()


# =============================================================================
# SEGMENTY, MASKI I ZLICZENIA
# =============================================================================

def read_and_validate_segment_shapes(
    input_oscy_dir: Path,
) -> tuple[list[tuple[Path, int, int]], int, int]:
    """Odczytuje kształty segmentów i kontroluje liczbę binów częstotliwości."""
    files = sorted(input_oscy_dir.glob("*.npy"), key=natural_key)
    if not files:
        raise FileNotFoundError(f"Nie znaleziono segmentów .npy w: {input_oscy_dir}")

    shapes: list[tuple[Path, int, int]] = []
    n_freq: int | None = None
    total_time_frames = 0

    for path in files:
        segment = np.load(path, mmap_mode="r")
        try:
            if segment.ndim != 2:
                raise ValueError(
                    f"{path.name}: oczekiwano tablicy 2D, otrzymano {segment.shape}"
                )

            freq_bins, time_bins = segment.shape
            if n_freq is None:
                n_freq = freq_bins
            elif freq_bins != n_freq:
                raise ValueError(
                    "Niezgodna liczba binów częstotliwości:\n"
                    f"  {path.name}: {freq_bins}\n"
                    f"  oczekiwano: {n_freq}"
                )

            shapes.append((path, freq_bins, time_bins))
            total_time_frames += time_bins
        finally:
            close_memmap(segment)

    if n_freq is None:
        raise RuntimeError("Nie udało się ustalić liczby binów częstotliwości.")

    return shapes, n_freq, total_time_frames


def rolling_median_time_then_frequency(
    spectrum_f_t: np.ndarray,
    median_time_bins: int,
    median_freq_bins: int,
) -> np.ndarray:
    """
    Pomocniczy obraz do estymacji maski:
    1. mediana krocząca po czasie,
    2. mediana krocząca po częstotliwości.

    Ten obraz NIE staje się wynikiem filtrowania.
    """
    time_smoothed = (
        pd.DataFrame(spectrum_f_t.T)
        .rolling(
            window=median_time_bins,
            center=True,
            min_periods=1,
        )
        .median()
        .to_numpy(dtype=np.float32)
        .T
    )

    background_smoothed = (
        pd.DataFrame(time_smoothed)
        .rolling(
            window=median_freq_bins,
            center=True,
            min_periods=1,
        )
        .median()
        .to_numpy(dtype=np.float32)
    )

    del time_smoothed
    gc.collect()
    return background_smoothed


def calculate_individual_mask_worker(source_sample_dir_str: str) -> dict[str, Any]:
    """
    PASS 1 dla pojedynczej próbki.

    Dla każdego segmentu: local_mask(f) = minimum po czasie pomocniczo
    wygładzonego PSD.

    Wynik individual_mask(f) to DOLNA OBWIEDNIA masek segmentów tej próbki:
        individual_mask(f) = minimum po segmentach local_mask(f).
    """
    source_sample_dir = Path(source_sample_dir_str)
    sample_name = source_sample_dir.name
    input_oscy_dir = source_sample_dir / AE_DIR_NAME / OSCY_SOURCE_DIR_NAME

    try:
        shapes, n_freq, total_time_frames = read_and_validate_segment_shapes(input_oscy_dir)

        if n_freq < 2:
            raise ValueError("Potrzebne są co najmniej dwa biny częstotliwości.")

        freq_axis_hz = np.linspace(
            FREQ_MIN_HZ,
            FREQ_MAX_HZ,
            n_freq,
            dtype=np.float64,
        )
        df_hz = float(np.median(np.diff(freq_axis_hz)))

        print(f"[{sample_name}] PASS 1/2: maska indywidualna ({len(shapes)} segmentów)", flush=True)

        individual_mask: np.ndarray | None = None

        for index, (path, _, time_bins) in enumerate(shapes, start=1):
            frame_dt_s = FILE_DURATION_S / time_bins
            median_time_bins = odd_window_from_physical(MEDIAN_TIME_S, frame_dt_s)
            median_freq_bins = odd_window_from_physical(MEDIAN_FREQ_HZ, df_hz)

            print(
                f"[{sample_name}]   {index}/{len(shapes)} {path.name} | "
                f"mediana {median_freq_bins} freq × {median_time_bins} czas",
                flush=True,
            )

            spectrum = np.load(path).astype(np.float32, copy=False)
            try:
                smoothed = rolling_median_time_then_frequency(
                    spectrum_f_t=spectrum,
                    median_time_bins=median_time_bins,
                    median_freq_bins=median_freq_bins,
                )
                local_mask = np.min(smoothed, axis=1)

                if individual_mask is None:
                    individual_mask = local_mask.astype(np.float32, copy=True)
                else:
                    # Dolna obwiednia masek segmentów tej samej próbki.
                    # W dB: min(-30, -3) = -30.
                    np.minimum(individual_mask, local_mask, out=individual_mask)

                del smoothed
                del local_mask
            finally:
                del spectrum
                gc.collect()

        if individual_mask is None:
            raise RuntimeError("Nie udało się wyznaczyć maski indywidualnej.")

        return {
            "ok": True,
            "sample": sample_name,
            "source_sample_dir": str(source_sample_dir),
            "n_freq": n_freq,
            "total_time_frames": total_time_frames,
            "freq_axis_hz": freq_axis_hz,
            "individual_mask": individual_mask,
        }

    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "sample": sample_name,
            "source_sample_dir": str(source_sample_dir),
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }


def aggregate_frame_counts_to_time_bins(
    frame_counts: np.ndarray,
    file_duration_s: float,
    output_bin_s: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Sumuje zliczenia pikseli z ramek do binów czasu, np. 0.01 s."""
    frame_counts = np.asarray(frame_counts, dtype=np.int64)
    n_frames = len(frame_counts)
    if n_frames == 0:
        raise ValueError("Nie można agregować pustego przebiegu zliczeń.")

    n_output_bins = int(round(file_duration_s / output_bin_s))
    frame_dt_s = file_duration_s / n_frames

    frame_times_s = np.arange(n_frames, dtype=np.float64) * frame_dt_s
    output_indices = np.floor(frame_times_s / output_bin_s).astype(np.int64)
    output_indices = np.clip(output_indices, 0, n_output_bins - 1)

    pixel_count = np.bincount(
        output_indices,
        weights=frame_counts.astype(np.float64),
        minlength=n_output_bins,
    ).astype(np.int64)

    time_s_left_edge = np.arange(n_output_bins, dtype=np.float64) * output_bin_s
    return time_s_left_edge, pixel_count


def aggregate_frame_values_to_time_bins(
    frame_values: np.ndarray,
    file_duration_s: float,
    output_bin_s: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Sumuje ciągłą wielkość ramek PSD do binów czasu, np. 0.01 s."""
    frame_values = np.asarray(frame_values, dtype=np.float64)
    n_frames = len(frame_values)
    if n_frames == 0:
        raise ValueError("Nie można agregować pustego przebiegu wartości.")

    n_output_bins = int(round(file_duration_s / output_bin_s))
    frame_dt_s = file_duration_s / n_frames
    frame_times_s = np.arange(n_frames, dtype=np.float64) * frame_dt_s
    output_indices = np.floor(frame_times_s / output_bin_s).astype(np.int64)
    output_indices = np.clip(output_indices, 0, n_output_bins - 1)

    values = np.bincount(
        output_indices,
        weights=frame_values,
        minlength=n_output_bins,
    ).astype(np.float64)
    time_s_left_edge = np.arange(n_output_bins, dtype=np.float64) * output_bin_s
    return time_s_left_edge, values


def frequency_indices_for_band_khz(
    *,
    n_freq: int,
    band_khz: tuple[float, float],
) -> np.ndarray:
    """Zwraca indeksy binów częstotliwości mieszczących się w paśmie kHz."""
    if n_freq < 2:
        raise ValueError("Do wyboru pasma potrzeba co najmniej dwóch binów częstotliwości.")
    low_khz, high_khz = band_khz
    freq_axis_khz = np.linspace(
        FREQ_MIN_HZ / 1000.0,
        FREQ_MAX_HZ / 1000.0,
        n_freq,
        dtype=np.float64,
    )
    indices = np.flatnonzero((freq_axis_khz >= low_khz) & (freq_axis_khz <= high_khz))
    if len(indices) == 0:
        raise ValueError(
            f"Pasmo {low_khz:g}-{high_khz:g} kHz nie obejmuje żadnego binu spektrogramu."
        )
    return indices.astype(np.int64, copy=False)


def calculate_frame_spectral_excess_energies(
    *,
    filtered_psd_db: np.ndarray,
    series_upper_mask_db: np.ndarray,
    df_hz: float,
    frame_dt_s: float,
    freq_indices: np.ndarray | None = None,
) -> np.ndarray:
    """
    Wyznacza energię spektralną każdej ramki po filtracji wspólną maską.

    ``filtered_psd_db`` = original_psd_db - series_upper_mask_db.
    Energię liczymy w skali liniowej PSD, bo dB nie można sumować.

    E_mask(t) = df * dt * Σ_f max(PSD_linear - mask_linear, 0)

    Gdy ``freq_indices`` nie jest puste, suma obejmuje wyłącznie wskazane
    biny częstotliwości, np. odpowiadające zakresowi 50-125 kHz.
    Wariant progowany 6 dB nie jest liczony.
    """
    filtered = np.asarray(filtered_psd_db, dtype=np.float32)
    mask = np.asarray(series_upper_mask_db, dtype=np.float32)
    if filtered.ndim != 2:
        raise ValueError("filtered_psd_db musi mieć układ [częstotliwość, czas].")
    if mask.ndim != 1 or len(mask) != filtered.shape[0]:
        raise ValueError("Maska częstotliwościowa nie pasuje do filtered_psd_db.")
    if df_hz <= 0 or frame_dt_s <= 0:
        raise ValueError("df_hz i frame_dt_s muszą być dodatnie.")

    if freq_indices is not None:
        indices = np.asarray(freq_indices, dtype=np.int64)
        if indices.ndim != 1 or len(indices) == 0:
            raise ValueError("freq_indices musi być niepustą tablicą 1D.")
        filtered = filtered[indices, :]
        mask = mask[indices]

    mask_linear = np.power(
        np.float32(10.0),
        mask / np.float32(PSD_DB_FACTOR),
    ).astype(np.float32, copy=False)

    relative_linear = np.power(
        np.float32(10.0),
        filtered / np.float32(PSD_DB_FACTOR),
    ).astype(np.float32, copy=False)

    # Nad samą maską: max(10^(filtered/10) - 1, 0) * 10^(mask/10).
    np.subtract(relative_linear, np.float32(1.0), out=relative_linear)
    np.maximum(relative_linear, np.float32(0.0), out=relative_linear)
    energy_above_mask = (
        np.sum(
            relative_linear * mask_linear[:, None],
            axis=0,
            dtype=np.float64,
        )
        * float(df_hz)
        * float(frame_dt_s)
    )

    return np.asarray(energy_above_mask, dtype=np.float64)

def format_energy_for_txt(value: float) -> str:
    """Format liczby przyjazny dla regionalnego importu w DIAdem: przecinek + E."""
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("Energia zawiera wartość niebędącą liczbą skończoną.")
    if value == 0.0:
        return "0"
    return f"{value:.12E}".replace(".", ",")


def write_cumulative_energy_file(
    *,
    output_path: Path,
    time_s_left_edge: np.ndarray,
    cumulative_energy: np.ndarray,
    column_name: str,
) -> None:
    """Zapisuje dwukolumnowy, kumulatywny plik TXT do bezpośredniego wykresu."""
    time_s_left_edge = np.asarray(time_s_left_edge, dtype=np.float64)
    cumulative_energy = np.asarray(cumulative_energy, dtype=np.float64)
    if len(time_s_left_edge) != len(cumulative_energy):
        raise ValueError("Czas i kumulatywna energia muszą mieć tę samą długość.")

    time_s_right_edge = time_s_left_edge + COUNT_BIN_S
    with open(output_path, "w", encoding="utf-8", newline="\n") as file:
        file.write(f"time[s]  {column_name} [arb.units]\n")
        for time_value, energy_value in zip(time_s_right_edge, cumulative_energy):
            time_text = f"{time_value:.2f}".replace(".", ",")
            file.write(f"{time_text} {format_energy_for_txt(float(energy_value))}\n")


def write_foscy_count_file(
    output_path: Path,
    time_s_left_edge: np.ndarray,
    pixel_count: np.ndarray,
) -> None:
    """
    Zapisuje wyłącznie FORMAT FOscy, z krokiem 0.01 s:

        time[s]  eventsNo.  EAenergy [arb.units]
        0,01 0 0
        0,02 14 0

    eventsNo. = pixel_count.
    EAenergy pozostaje zerowe, bo pipeline nie wyznacza natywnej energii hitów AE.
    """
    time_s_left_edge = np.asarray(time_s_left_edge, dtype=np.float64)
    pixel_count = np.asarray(pixel_count, dtype=np.int64)

    if len(time_s_left_edge) != len(pixel_count):
        raise ValueError("time_s_left_edge i pixel_count muszą mieć tę samą długość.")

    time_s_right_edge = time_s_left_edge + COUNT_BIN_S

    with open(output_path, "w", encoding="utf-8", newline="\n") as file:
        file.write("time[s]  eventsNo.  EAenergy [arb.units]\n")
        for time_value, count_value in zip(time_s_right_edge, pixel_count):
            time_text = f"{time_value:.2f}".replace(".", ",")
            file.write(f"{time_text} {int(count_value)} 0\n")


def save_background_csv(
    output_path: Path,
    freq_axis_hz: np.ndarray,
    background_db: np.ndarray,
    envelope_source_sample: np.ndarray | None = None,
) -> None:
    """Zapisuje maskę częstotliwościową do CSV."""
    data: dict[str, Any] = {
        "freq_hz": np.asarray(freq_axis_hz, dtype=np.float64),
        "background_db": np.asarray(background_db, dtype=np.float32),
    }
    if envelope_source_sample is not None:
        data["envelope_source_sample"] = envelope_source_sample

    pd.DataFrame(data).to_csv(output_path, index=False)


# =============================================================================
# PALETA I TIFF
# =============================================================================

def build_colour_lut() -> np.ndarray:
    """Tworzy RGB LUT dla palety używanej w obrazach TIFF."""
    values = np.linspace(COLOR_MIN_DB, COLOR_MAX_DB, LUT_SIZE, dtype=np.float32)
    lut = np.empty((LUT_SIZE, 3), dtype=np.uint8)

    for channel in range(3):
        lut[:, channel] = np.round(
            np.interp(values, COLOR_STOPS_DB, COLOR_STOPS_RGB[:, channel])
        ).astype(np.uint8)

    return lut


RGB_LUT = build_colour_lut()


def colourize_display_values(display_values: np.ndarray) -> np.ndarray:
    """Mapuje wartości wizualizacji na RGB przez LUT."""
    values = np.asarray(display_values, dtype=np.float32)
    values = np.nan_to_num(
        values,
        nan=COLOR_MIN_DB,
        posinf=COLOR_MAX_DB,
        neginf=COLOR_MIN_DB,
    )
    values = np.clip(values, COLOR_MIN_DB, COLOR_MAX_DB)

    lut_indices = np.round(
        (values - COLOR_MIN_DB)
        * (LUT_SIZE - 1)
        / (COLOR_MAX_DB - COLOR_MIN_DB)
    ).astype(np.int32)
    lut_indices = np.clip(lut_indices, 0, LUT_SIZE - 1)

    return RGB_LUT[lut_indices]


def map_block_to_display_values(block: np.ndarray, mode: str) -> np.ndarray:
    """Tworzy wartości wejściowe palety dla obrazu PSD.tiff."""
    if mode != "linear":
        raise ValueError("Aktualna wersja zapisuje wyłącznie PSD.tiff, bez PSD_log.tiff.")
    return np.asarray(block, dtype=np.float32)

def write_tiled_colour_tiff(
    *,
    output_path: Path,
    filtered_segment_paths: list[Path],
    segment_widths: list[int],
    n_freq: int,
    total_time_frames: int,
    time_pool_factor: int,
    mode: str,
    sample_name: str,
) -> None:
    """
    Zapisuje kafelkowany RGB TIFF/BigTIFF bez ładowania całości do RAM.

    Aktualna wersja zapisuje tylko PSD.tiff. Oś czasu TIFF jest redukowana
    przez pooling maksimum. Wartość jednego piksela obrazu dla danej
    częstotliwości i bloku czasu jest maksimum filtered PSD z kolejnych
    `time_pool_factor` ramek. To redukuje objętość pliku, ale nie zmienia
    `filtered_segments/*.npy` ani zliczeń.
    """
    assert_tiff_tile_config()
    warn_if_windows_path_is_long(output_path, label=f"TIFF {mode}")

    if len(filtered_segment_paths) != len(segment_widths):
        raise ValueError("filtered_segment_paths i segment_widths muszą mieć tę samą długość.")
    if time_pool_factor <= 0:
        raise ValueError("time_pool_factor musi być dodatni.")

    output_time_pixels = tiff_output_time_pixels(total_time_frames, time_pool_factor)

    if output_path.exists():
        output_path.unlink()

    # 3 kanały uint8, plus niewielki zapas na strukturę TIFF.
    estimated_raw_bytes = n_freq * output_time_pixels * 3
    use_bigtiff = estimated_raw_bytes >= 3_800_000_000

    segment_bounds: list[tuple[int, int, Path]] = []
    global_start = 0
    for segment_path, width in zip(filtered_segment_paths, segment_widths):
        global_end = global_start + width
        segment_bounds.append((global_start, global_end, segment_path))
        global_start = global_end

    if global_start != total_time_frames:
        raise RuntimeError("Niezgodna liczba ramek przy składaniu TIFF-a.")

    description = json.dumps(
        {
            "axes": "YXS",
            "y_axis": "frequency_bin",
            "x_axis": "time_pool_pixel",
            "sample_axis": "RGB",
            "sample": sample_name,
            "source": "original PSD minus shared series upper-envelope mask",
            "display_mode": mode,
            "linear_tiff": mode == "linear",
            "time_pooling": TIFF_TIME_POOLING,
            "time_pool_factor_frames": time_pool_factor,
            "source_time_frames": total_time_frames,
            "output_time_pixels": output_time_pixels,
            "display_palette_min_db": COLOR_MIN_DB,
            "display_palette_max_db": COLOR_MAX_DB,
            "display_threshold_db": THRESHOLD_DB,
            "note": (
                "TIFF is a temporally max-pooled visualization only. Exact "
                "float32 values remain in filtered_segments/*.npy."
            ),
        },
        # TIFF Description (tag 270) może zawierać wyłącznie 7-bitowy ASCII.
        ensure_ascii=True,
    )
    description.encode("ascii")

    def tile_generator():
        current_path: Path | None = None
        current_segment: Any = None

        def load_segment(segment_path: Path):
            nonlocal current_path, current_segment
            if current_path == segment_path:
                return current_segment

            if current_segment is not None:
                close_memmap(current_segment)

            current_path = segment_path
            current_segment = np.load(segment_path, mmap_mode="r")
            return current_segment

        try:
            for out_y0 in range(0, n_freq, TIFF_TILE_HEIGHT):
                valid_height = min(TIFF_TILE_HEIGHT, n_freq - out_y0)

                if FLIP_VERTICAL_FOR_TIFF:
                    source_y0 = n_freq - (out_y0 + valid_height)
                    source_y1 = n_freq - out_y0
                else:
                    source_y0 = out_y0
                    source_y1 = out_y0 + valid_height

                for out_x0 in range(0, output_time_pixels, TIFF_TILE_WIDTH):
                    valid_width = min(TIFF_TILE_WIDTH, output_time_pixels - out_x0)
                    source_x0 = out_x0 * time_pool_factor
                    source_x1 = min(
                        (out_x0 + valid_width) * time_pool_factor,
                        total_time_frames,
                    )
                    source_width = source_x1 - source_x0
                    expected_source_width = valid_width * time_pool_factor

                    # Jeden bufor obejmuje ciągły fragment czasu źródłowego,
                    # również wtedy, gdy kafelek przechodzi przez granicę segmentu.
                    source_block = np.full(
                        (valid_height, expected_source_width),
                        -np.inf,
                        dtype=np.float32,
                    )

                    for seg_start, seg_end, segment_path in segment_bounds:
                        if seg_end <= source_x0:
                            continue
                        if seg_start >= source_x1:
                            break

                        segment = load_segment(segment_path)
                        overlap_start = max(source_x0, seg_start)
                        overlap_end = min(source_x1, seg_end)

                        local_x0 = overlap_start - seg_start
                        local_x1 = overlap_end - seg_start
                        block_x0 = overlap_start - source_x0
                        block_x1 = overlap_end - source_x0

                        block = segment[source_y0:source_y1, local_x0:local_x1]
                        if FLIP_VERTICAL_FOR_TIFF:
                            block = block[::-1, :]

                        source_block[:, block_x0:block_x1] = block
                        del block

                    # Ostatni blok może mieć mniej ramek źródłowych; brakujące
                    # pozycje pozostają -inf i nie podbijają maksimum.
                    pooled = np.max(
                        source_block.reshape(
                            valid_height,
                            valid_width,
                            time_pool_factor,
                        ),
                        axis=2,
                    )

                    tile = np.zeros(
                        (TIFF_TILE_HEIGHT, TIFF_TILE_WIDTH, 3),
                        dtype=np.uint8,
                    )
                    display_values = map_block_to_display_values(pooled, mode)
                    tile[:valid_height, :valid_width, :] = colourize_display_values(
                        display_values
                    )

                    del source_block
                    del pooled
                    del display_values
                    yield tile
        finally:
            if current_segment is not None:
                close_memmap(current_segment)

    estimated_gb_per_1000_s = (
        n_freq
        * (output_time_pixels / (len(segment_widths) * FILE_DURATION_S))
        * 1000.0
        * 3.0
        / 1_000_000_000
    )
    print(
        f"[{sample_name}] TIFF {mode}: {output_path.name} | "
        f"{n_freq} × {output_time_pixels} px | "
        f"max-pool ×{time_pool_factor} | "
        f"~{estimated_gb_per_1000_s:.2f} GB/1000 s | "
        f"{'BigTIFF' if use_bigtiff else 'TIFF'}",
        flush=True,
    )

    with tifffile.TiffWriter(output_path, bigtiff=use_bigtiff, byteorder="<") as tif:
        tif.write(
            data=tile_generator(),
            shape=(n_freq, output_time_pixels, 3),
            dtype=np.uint8,
            tile=(TIFF_TILE_HEIGHT, TIFF_TILE_WIDTH),
            photometric="rgb",
            planarconfig="contig",
            metadata=None,
            description=description,
        )

    # Lekka walidacja struktury pliku bez wczytywania obrazu.
    with tifffile.TiffFile(output_path) as tif:
        page = tif.pages[0]
        expected_shape = (n_freq, output_time_pixels, 3)
        if tuple(page.shape) != expected_shape:
            raise RuntimeError(
                f"TIFF ma niepoprawny shape {page.shape}; oczekiwano {expected_shape}."
            )
        if not page.is_tiled:
            raise RuntimeError("TIFF nie został zapisany kafelkowo.")


# =============================================================================
# RAPORTY ENERGII Z ISTNIEJĄCYCH filtered_segments (bez ponownej filtracji)
# =============================================================================

def generate_energy_reports_from_filtered_worker(task: dict[str, Any]) -> dict[str, Any]:
    """
    Uzupełnia CumEn z istniejących ``filtered_segments``.

    Ta ścieżka ponownie używa już zapisanych residuali po wspólnej masce i NIE
    przelicza median, maski serii, segmentów filtrowanych ani TIFF-ów.
    """
    source_sample_dir = Path(task["source_sample_dir"])
    target_sample_dir = Path(task["target_sample_dir"])
    sample_name = source_sample_dir.name

    try:
        input_oscy_dir = source_sample_dir / AE_DIR_NAME / OSCY_SOURCE_DIR_NAME
        output_ae_dir = target_sample_dir / AE_DIR_NAME
        output_dir = output_ae_dir / OSCY_SOURCE_DIR_NAME
        filtered_segments_dir = output_dir / FILTERED_SEGMENTS_DIR_NAME
        energy_bands_khz = [tuple(band) for band in task.get("energy_bands_khz", [])]
        energy_report_signature = task["energy_report_signature"]

        shapes, n_freq, _ = read_and_validate_segment_shapes(input_oscy_dir)
        series_upper_mask = np.asarray(task["series_upper_mask"], dtype=np.float32)
        if series_upper_mask.ndim != 1 or len(series_upper_mask) != n_freq:
            raise ValueError("Wspólna maska serii nie pasuje do wynikowych segmentów.")
        if n_freq < 2:
            raise ValueError("Do całkowania PSD potrzeba co najmniej dwóch binów częstotliwości.")

        df_hz = float((FREQ_MAX_HZ - FREQ_MIN_HZ) / (n_freq - 1))
        band_indices_by_label = {
            energy_band_file_label(band): frequency_indices_for_band_khz(
                n_freq=n_freq,
                band_khz=band,
            )
            for band in energy_bands_khz
        }
        energy_tables: list[pd.DataFrame] = []

        print(
            f"[{sample_name}] raport energii: wykorzystuję istniejące filtered_segments...",
            flush=True,
        )
        if energy_bands_khz:
            print(
                f"[{sample_name}]   dodatkowe pasma CumEn: "
                + ", ".join(energy_band_file_label(band) for band in energy_bands_khz),
                flush=True,
            )

        for segment_index, (raw_path, _, time_bins) in enumerate(shapes):
            filtered_path = filtered_segments_dir / f"{raw_path.stem}_filtered.npy"
            print(
                f"[{sample_name}]   energia {segment_index + 1}/{len(shapes)} "
                f"{filtered_path.name}",
                flush=True,
            )
            filtered = np.load(filtered_path, mmap_mode="r")
            try:
                expected_shape = (n_freq, time_bins)
                if tuple(filtered.shape) != expected_shape:
                    raise ValueError(
                        f"Nieprawidłowy shape {filtered_path.name}: "
                        f"{tuple(filtered.shape)}, oczekiwano {expected_shape}."
                    )

                frame_dt_s = FILE_DURATION_S / time_bins
                frame_energy_above_mask = calculate_frame_spectral_excess_energies(
                    filtered_psd_db=filtered,
                    series_upper_mask_db=series_upper_mask,
                    df_hz=df_hz,
                    frame_dt_s=frame_dt_s,
                )
                local_time_s, energy_above_mask = aggregate_frame_values_to_time_bins(
                    frame_values=frame_energy_above_mask,
                    file_duration_s=FILE_DURATION_S,
                    output_bin_s=COUNT_BIN_S,
                )
                row_data: dict[str, Any] = {
                    "time_s": local_time_s + segment_index * FILE_DURATION_S,
                    "energy_above_common_mask": energy_above_mask,
                    "segment_index": segment_index,
                    "source_file": raw_path.name,
                }

                for band in energy_bands_khz:
                    label = energy_band_file_label(band)
                    frame_energy_band = calculate_frame_spectral_excess_energies(
                        filtered_psd_db=filtered,
                        series_upper_mask_db=series_upper_mask,
                        df_hz=df_hz,
                        frame_dt_s=frame_dt_s,
                        freq_indices=band_indices_by_label[label],
                    )
                    _, energy_band = aggregate_frame_values_to_time_bins(
                        frame_values=frame_energy_band,
                        file_duration_s=FILE_DURATION_S,
                        output_bin_s=COUNT_BIN_S,
                    )
                    row_data[f"energy_above_common_mask_{label}"] = energy_band
                    del frame_energy_band
                    del energy_band

                energy_tables.append(pd.DataFrame(row_data))
                del frame_energy_above_mask
                del energy_above_mask
            finally:
                close_memmap(filtered)
                del filtered
                gc.collect()

        energy_df = pd.concat(energy_tables, ignore_index=True)
        energy_df["cumulative_energy_above_common_mask"] = (
            energy_df["energy_above_common_mask"].cumsum()
        )
        band_output_files: list[dict[str, Any]] = []
        for band in energy_bands_khz:
            label = energy_band_file_label(band)
            energy_col = f"energy_above_common_mask_{label}"
            cumulative_col = f"cumulative_energy_above_common_mask_{label}"
            energy_df[cumulative_col] = energy_df[energy_col].cumsum()

        energy_csv_path = output_dir / ENERGY_CSV_NAME
        energy_df.to_csv(energy_csv_path, index=False)
        cum_en_output_path = output_ae_dir / make_cum_en_filename(sample_name)
        write_cumulative_energy_file(
            output_path=cum_en_output_path,
            time_s_left_edge=energy_df["time_s"].to_numpy(),
            cumulative_energy=energy_df[
                "cumulative_energy_above_common_mask"
            ].to_numpy(),
            column_name="CumEn",
        )

        for band in energy_bands_khz:
            label = energy_band_file_label(band)
            cumulative_col = f"cumulative_energy_above_common_mask_{label}"
            band_path = output_ae_dir / make_cum_en_band_filename(band)
            write_cumulative_energy_file(
                output_path=band_path,
                time_s_left_edge=energy_df["time_s"].to_numpy(),
                cumulative_energy=energy_df[cumulative_col].to_numpy(),
                column_name=energy_band_column_label(band),
            )
            band_output_files.append(
                {
                    "low_khz": float(band[0]),
                    "high_khz": float(band[1]),
                    "file": str(band_path),
                    "total_energy": float(energy_df[cumulative_col].iloc[-1]),
                }
            )

        obsolete_6db_path = output_ae_dir / OBSOLETE_CUM_EN_6DB_NAME
        if obsolete_6db_path.exists():
            obsolete_6db_path.unlink()

        total_energy_above_mask = float(
            energy_df["cumulative_energy_above_common_mask"].iloc[-1]
        )

        params_path = output_dir / PARAMS_JSON_NAME
        params = load_json_safely(params_path)
        if params is None:
            raise ValueError(f"Nie można uzupełnić raportów: uszkodzony {PARAMS_JSON_NAME}.")
        for obsolete_key in [
            "energy_above_common_mask_plus_6db_definition",
            "cum_en_6db_file",
            "total_energy_above_common_mask_plus_6db",
        ]:
            params.pop(obsolete_key, None)
        params.update(
            {
                "energy_report_algorithm": ENERGY_REPORT_ALGORITHM_ID,
                "energy_report_signature": energy_report_signature,
                "psd_db_to_linear_definition": "psd_linear = 10**(psd_db / 10)",
                "energy_above_common_mask_definition": (
                    "E_mask(bin) = sum_{frames,freq}(max(10**(original_psd_db/10) "
                    "- 10**(series_mask_db/10), 0) * df_hz * dt_s)"
                ),
                "energy_band_definition": (
                    "E_band(bin) = ta sama definicja energii, ale suma jest "
                    "ograniczona do binów częstotliwości w zadanym paśmie kHz."
                ),
                "energy_frequency_bands_khz": normalised_energy_bands_payload(energy_bands_khz),
                "energy_threshold_note": "Nie generowano wariantu CumEn6dB; energia jest liczona względem samej wspólnej maski.",
                "energy_csv": str(energy_csv_path),
                "cum_en_file": str(cum_en_output_path),
                "cum_en_band_files": band_output_files,
                "total_energy_above_common_mask": total_energy_above_mask,
            }
        )
        atomic_write_json(params_path, params)

        state = load_json_safely(resume_state_path(target_sample_dir))
        if state is None:
            state = make_resume_state_base(
                source_sample_dir=source_sample_dir,
                descriptor=task["source_descriptor"],
                settings_signature=task["settings_signature"],
                individual_mask_sha256=task["individual_mask_sha256"],
            )
        state.update(
            {
                "stage": "complete",
                "series_mask_sha256": task["series_mask_sha256"],
                "series_mask_file": BACKGROUND_SERIES_UPPER_NPY_NAME,
                "energy_report_signature": energy_report_signature,
                "energy_frequency_bands_khz": normalised_energy_bands_payload(energy_bands_khz),
            }
        )
        required_outputs = list(state.get("required_outputs", []))
        required_outputs = [
            filename for filename in required_outputs
            if filename != OBSOLETE_CUM_EN_6DB_NAME
        ]
        for filename in [ENERGY_CSV_NAME, make_cum_en_filename(sample_name)]:
            if filename not in required_outputs:
                required_outputs.append(filename)
        for band in energy_bands_khz:
            filename = make_cum_en_band_filename(band)
            if filename not in required_outputs:
                required_outputs.append(filename)
        state["required_outputs"] = required_outputs
        atomic_write_json(resume_state_path(target_sample_dir), state)

        del energy_df
        del energy_tables
        gc.collect()

        return {
            "ok": True,
            "sample": sample_name,
            "cum_en_file": str(cum_en_output_path),
            "cum_en_band_files": band_output_files,
            "total_energy_above_mask": total_energy_above_mask,
        }

    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "sample": sample_name,
            "source_sample_dir": str(source_sample_dir),
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }


# =============================================================================
# PASS 2: FILTROWANIE PRÓBKI WSPÓLNĄ MASKĄ SERII
# =============================================================================

def process_one_sample_worker(task: dict[str, Any]) -> dict[str, Any]:
    """
    PASS 2 dla jednej próbki: używa wspólnej górnej obwiedni maski serii.
    """
    source_sample_dir = Path(task["source_sample_dir"])
    target_sample_dir = Path(task["target_sample_dir"])
    sample_name = source_sample_dir.name

    try:
        input_oscy_dir = source_sample_dir / AE_DIR_NAME / OSCY_SOURCE_DIR_NAME
        output_ae_dir = target_sample_dir / AE_DIR_NAME
        output_dir = output_ae_dir / OSCY_SOURCE_DIR_NAME
        filtered_segments_dir = output_dir / FILTERED_SEGMENTS_DIR_NAME
        energy_bands_khz = [tuple(band) for band in task.get("energy_bands_khz", [])]
        energy_report_signature = task["energy_report_signature"]

        output_dir.mkdir(parents=True, exist_ok=True)
        # Próbka uznana za niepełną jest liczona od początku PASS 2. Dzięki temu
        # nie mieszają się fragmenty pochodzące z różnych masek serii.
        clear_stale_pass2_artifacts(
            output_ae_dir=output_ae_dir,
            output_dir=output_dir,
            sample_name=sample_name,
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        filtered_segments_dir.mkdir(parents=True, exist_ok=True)

        shapes, n_freq, total_time_frames = read_and_validate_segment_shapes(input_oscy_dir)
        individual_mask = np.asarray(task["individual_mask"], dtype=np.float32)
        series_upper_mask = np.asarray(task["series_upper_mask"], dtype=np.float32)
        envelope_source_sample = np.asarray(task["envelope_source_sample"], dtype=object)

        if len(individual_mask) != n_freq or len(series_upper_mask) != n_freq:
            raise ValueError(
                "Rozmiar maski nie pasuje do liczby binów częstotliwości "
                f"dla próbki {sample_name}."
            )

        if len(envelope_source_sample) != n_freq:
            raise ValueError("Nieprawidłowa długość envelope_source_sample.")

        freq_axis_hz = np.linspace(
            FREQ_MIN_HZ,
            FREQ_MAX_HZ,
            n_freq,
            dtype=np.float64,
        )

        print(f"[{sample_name}] PASS 2/2: filtracja wspólną górną obwiednią...", flush=True)

        # background_by_freq.* jest faktycznie używaną maską wspólną.
        np.save(output_dir / BACKGROUND_NPY_NAME, series_upper_mask)
        save_background_csv(
            output_dir / BACKGROUND_CSV_NAME,
            freq_axis_hz,
            series_upper_mask,
            envelope_source_sample,
        )

        # Dwa dodatkowe pliki zachowują pełną ścieżkę audytu.
        np.save(output_dir / BACKGROUND_INDIVIDUAL_NPY_NAME, individual_mask)
        save_background_csv(
            output_dir / BACKGROUND_INDIVIDUAL_CSV_NAME,
            freq_axis_hz,
            individual_mask,
        )

        np.save(output_dir / BACKGROUND_SERIES_UPPER_NPY_NAME, series_upper_mask)
        save_background_csv(
            output_dir / BACKGROUND_SERIES_UPPER_CSV_NAME,
            freq_axis_hz,
            series_upper_mask,
            envelope_source_sample,
        )

        count_tables: list[pd.DataFrame] = []
        energy_tables: list[pd.DataFrame] = []
        filtered_segment_paths: list[Path] = []
        segment_widths: list[int] = []

        if n_freq < 2:
            raise ValueError("Do całkowania PSD potrzeba co najmniej dwóch binów częstotliwości.")
        df_hz = float((FREQ_MAX_HZ - FREQ_MIN_HZ) / (n_freq - 1))
        band_indices_by_label = {
            energy_band_file_label(band): frequency_indices_for_band_khz(
                n_freq=n_freq,
                band_khz=band,
            )
            for band in energy_bands_khz
        }
        if energy_bands_khz:
            print(
                f"[{sample_name}] dodatkowe pasma CumEn: "
                + ", ".join(energy_band_file_label(band) for band in energy_bands_khz),
                flush=True,
            )

        for segment_index, (path, _, time_bins) in enumerate(shapes):
            print(
                f"[{sample_name}]   {segment_index + 1}/{len(shapes)} {path.name}",
                flush=True,
            )

            spectrum = np.load(path).astype(np.float32, copy=False)
            try:
                # Ten sam wspólny baseline dla wszystkich próbek w serii.
                filtered = spectrum - series_upper_mask[:, None]

                filtered_path = filtered_segments_dir / f"{path.stem}_filtered.npy"
                np.save(filtered_path, filtered.astype(np.float32, copy=False))
                filtered_segment_paths.append(filtered_path)
                segment_widths.append(time_bins)

                frame_counts = np.sum(
                    filtered > THRESHOLD_DB,
                    axis=0,
                    dtype=np.int32,
                )

                local_time_s, pixel_count = aggregate_frame_counts_to_time_bins(
                    frame_counts=frame_counts,
                    file_duration_s=FILE_DURATION_S,
                    output_bin_s=COUNT_BIN_S,
                )

                global_time_s = local_time_s + segment_index * FILE_DURATION_S
                count_tables.append(
                    pd.DataFrame(
                        {
                            "time_s": global_time_s,
                            "pixel_count": pixel_count,
                            "segment_index": segment_index,
                            "source_file": path.name,
                        }
                    )
                )

                frame_dt_s = FILE_DURATION_S / time_bins
                frame_energy_above_mask = calculate_frame_spectral_excess_energies(
                    filtered_psd_db=filtered,
                    series_upper_mask_db=series_upper_mask,
                    df_hz=df_hz,
                    frame_dt_s=frame_dt_s,
                )
                energy_local_time_s, energy_above_mask = aggregate_frame_values_to_time_bins(
                    frame_values=frame_energy_above_mask,
                    file_duration_s=FILE_DURATION_S,
                    output_bin_s=COUNT_BIN_S,
                )
                row_data: dict[str, Any] = {
                    "time_s": energy_local_time_s + segment_index * FILE_DURATION_S,
                    "energy_above_common_mask": energy_above_mask,
                    "segment_index": segment_index,
                    "source_file": path.name,
                }

                for band in energy_bands_khz:
                    label = energy_band_file_label(band)
                    frame_energy_band = calculate_frame_spectral_excess_energies(
                        filtered_psd_db=filtered,
                        series_upper_mask_db=series_upper_mask,
                        df_hz=df_hz,
                        frame_dt_s=frame_dt_s,
                        freq_indices=band_indices_by_label[label],
                    )
                    _, energy_band = aggregate_frame_values_to_time_bins(
                        frame_values=frame_energy_band,
                        file_duration_s=FILE_DURATION_S,
                        output_bin_s=COUNT_BIN_S,
                    )
                    row_data[f"energy_above_common_mask_{label}"] = energy_band
                    del frame_energy_band
                    del energy_band

                energy_tables.append(pd.DataFrame(row_data))

                del frame_energy_above_mask
                del energy_above_mask
                del filtered
                del frame_counts
            finally:
                del spectrum
                gc.collect()

        counts_df = pd.concat(count_tables, ignore_index=True)
        counts_csv_path = output_dir / COUNTS_CSV_NAME
        counts_df.to_csv(counts_csv_path, index=False)

        energy_df = pd.concat(energy_tables, ignore_index=True)
        energy_df["cumulative_energy_above_common_mask"] = (
            energy_df["energy_above_common_mask"].cumsum()
        )
        band_output_files: list[dict[str, Any]] = []
        for band in energy_bands_khz:
            label = energy_band_file_label(band)
            energy_col = f"energy_above_common_mask_{label}"
            cumulative_col = f"cumulative_energy_above_common_mask_{label}"
            energy_df[cumulative_col] = energy_df[energy_col].cumsum()

        energy_csv_path = output_dir / ENERGY_CSV_NAME
        energy_df.to_csv(energy_csv_path, index=False)

        cum_en_output_path = output_ae_dir / make_cum_en_filename(sample_name)
        write_cumulative_energy_file(
            output_path=cum_en_output_path,
            time_s_left_edge=energy_df["time_s"].to_numpy(),
            cumulative_energy=energy_df[
                "cumulative_energy_above_common_mask"
            ].to_numpy(),
            column_name="CumEn",
        )
        for band in energy_bands_khz:
            label = energy_band_file_label(band)
            cumulative_col = f"cumulative_energy_above_common_mask_{label}"
            band_path = output_ae_dir / make_cum_en_band_filename(band)
            write_cumulative_energy_file(
                output_path=band_path,
                time_s_left_edge=energy_df["time_s"].to_numpy(),
                cumulative_energy=energy_df[cumulative_col].to_numpy(),
                column_name=energy_band_column_label(band),
            )
            band_output_files.append(
                {
                    "low_khz": float(band[0]),
                    "high_khz": float(band[1]),
                    "file": str(band_path),
                    "total_energy": float(energy_df[cumulative_col].iloc[-1]),
                }
            )

        foscy_output_path = output_ae_dir / make_foscy_filename(sample_name)
        write_foscy_count_file(
            output_path=foscy_output_path,
            time_s_left_edge=counts_df["time_s"].to_numpy(),
            pixel_count=counts_df["pixel_count"].to_numpy(),
        )

        # Ostateczny obraz PSD.tiff w formacie .tiff. Czas jest redukowany tylko
        # w wizualizacji przez pooling maksimum, aby ograniczyć rozmiar plików.
        linear_tiff_path = output_dir / LINEAR_TIFF_NAME
        time_pool_factor = tiff_time_pool_factor(
            n_freq=n_freq,
            total_time_frames=total_time_frames,
            n_segments=len(segment_widths),
        )
        output_time_pixels = tiff_output_time_pixels(
            total_time_frames,
            time_pool_factor,
        )

        write_tiled_colour_tiff(
            output_path=linear_tiff_path,
            filtered_segment_paths=filtered_segment_paths,
            segment_widths=segment_widths,
            n_freq=n_freq,
            total_time_frames=total_time_frames,
            time_pool_factor=time_pool_factor,
            mode="linear",
            sample_name=sample_name,
        )

        params = {
            "source_sample_dir": str(source_sample_dir),
            "filtered_sample_dir": str(target_sample_dir),
            "input_oscy_dir": str(input_oscy_dir),
            "output_dir": str(output_dir),
            "file_duration_s": FILE_DURATION_S,
            "count_bin_s": COUNT_BIN_S,
            "freq_min_hz": FREQ_MIN_HZ,
            "freq_max_hz": FREQ_MAX_HZ,
            "median_time_s": MEDIAN_TIME_S,
            "median_freq_hz": MEDIAN_FREQ_HZ,
            "threshold_db": THRESHOLD_DB,
            "max_workers": MAX_WORKERS,
            "resume_algorithm": RESUME_ALGORITHM_ID,
            "source_signature": task["source_descriptor"]["source_signature"],
            "settings_signature": task["settings_signature"],
            "series_mask_sha256": task["series_mask_sha256"],
            "n_input_files": len(shapes),
            "n_frequency_bins": int(n_freq),
            "total_time_frames": int(total_time_frames),
            "individual_mask_file": str(output_dir / BACKGROUND_INDIVIDUAL_CSV_NAME),
            "series_upper_mask_file": str(output_dir / BACKGROUND_SERIES_UPPER_CSV_NAME),
            "effective_mask_file": str(output_dir / BACKGROUND_CSV_NAME),
            "filter_definition": (
                "filtered_psd(f, t) = original_psd(f, t) "
                "- series_upper_envelope_mask(f)"
            ),
            "individual_mask_definition": (
                "Dla każdego segmentu: minimum po czasie pomocniczo "
                "wygładzonego PSD. Dla próbki: minimum numeryczne "
                "tych masek po segmentach (dolna obwiednia segmentów)."
            ),
            "series_upper_envelope_definition": (
                "Dla każdej częstotliwości: maksimum numeryczne masek "
                "indywidualnych po wszystkich próbkach serii."
            ),
            "count_definition": (
                "pixel_count = suma pikseli filtered_psd > threshold_db "
                "w kolejnych oknach 0.01 s."
            ),
            "energy_report_algorithm": ENERGY_REPORT_ALGORITHM_ID,
            "energy_report_signature": energy_report_signature,
            "psd_db_to_linear_definition": "psd_linear = 10**(psd_db / 10)",
            "energy_above_common_mask_definition": (
                "E_mask(bin) = sum_{frames,freq}(max(10**(original_psd_db/10) "
                "- 10**(series_mask_db/10), 0) * df_hz * dt_s)"
            ),
            "energy_band_definition": (
                "E_band(bin) = ta sama definicja energii, ale suma jest "
                "ograniczona do binów częstotliwości w zadanym paśmie kHz."
            ),
            "energy_frequency_bands_khz": normalised_energy_bands_payload(energy_bands_khz),
            "energy_threshold_note": "Nie generowano wariantu CumEn6dB; energia jest liczona względem samej wspólnej maski.",
            "energy_csv": str(energy_csv_path),
            "cum_en_file": str(cum_en_output_path),
            "cum_en_band_files": band_output_files,
            "total_energy_above_common_mask": float(
                energy_df["cumulative_energy_above_common_mask"].iloc[-1]
            ),
            "foscy_file": str(foscy_output_path),
            "foscy_format": (
                "time[s] eventsNo. EAenergy [arb.units]; "
                "czas jako prawy brzeg okna; EAenergy = 0"
            ),
            "linear_tiff": str(linear_tiff_path),
            "obsolete_log_tiff_removed": OBSOLETE_LOG_TIFF_NAME,
            "tiff_time_pooling": TIFF_TIME_POOLING,
            "tiff_time_pool_factor_frames": int(time_pool_factor),
            "tiff_source_time_frames": int(total_time_frames),
            "tiff_output_time_pixels": int(output_time_pixels),
            "tiff_target_bytes_per_1000_s_per_file": int(TIFF_TARGET_BYTES_PER_1000_S),
            "tiff_note": (
                "PSD.tiff jest wizualizacją z temporalnym max-poolingiem; "
                "pełne dane float32 pozostają w filtered_segments/*.npy."
            ),
        }
        (output_dir / PARAMS_JSON_NAME).write_text(
            json.dumps(params, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        completion_state = make_resume_state_base(
            source_sample_dir=source_sample_dir,
            descriptor=task["source_descriptor"],
            settings_signature=task["settings_signature"],
            individual_mask_sha256=task["individual_mask_sha256"],
        )
        completion_state.update(
            {
                "stage": "complete",
                "series_mask_sha256": task["series_mask_sha256"],
                "series_mask_file": BACKGROUND_SERIES_UPPER_NPY_NAME,
                "required_filtered_segments": [path.name for path in filtered_segment_paths],
                "required_outputs": [
                    COUNTS_CSV_NAME,
                    ENERGY_CSV_NAME,
                    BACKGROUND_NPY_NAME,
                    BACKGROUND_CSV_NAME,
                    BACKGROUND_INDIVIDUAL_NPY_NAME,
                    BACKGROUND_INDIVIDUAL_CSV_NAME,
                    BACKGROUND_SERIES_UPPER_NPY_NAME,
                    BACKGROUND_SERIES_UPPER_CSV_NAME,
                    PARAMS_JSON_NAME,
                    LINEAR_TIFF_NAME,
                    make_foscy_filename(sample_name),
                    make_cum_en_filename(sample_name),
                    *[make_cum_en_band_filename(band) for band in energy_bands_khz],
                ],
                "energy_report_signature": energy_report_signature,
                "energy_frequency_bands_khz": normalised_energy_bands_payload(energy_bands_khz),
            }
        )
        atomic_write_json(resume_state_path(target_sample_dir), completion_state)

        total_pixel_count = int(counts_df["pixel_count"].sum())
        total_energy_above_mask = float(
            energy_df["cumulative_energy_above_common_mask"].iloc[-1]
        )
        del counts_df
        del energy_df
        del count_tables
        del energy_tables
        gc.collect()

        return {
            "ok": True,
            "sample": sample_name,
            "target": str(target_sample_dir),
            "foscy_file": str(foscy_output_path),
            "counts_csv": str(counts_csv_path),
            "linear_tiff": str(linear_tiff_path),
            "obsolete_log_tiff_removed": OBSOLETE_LOG_TIFF_NAME,
            "cum_en_file": str(cum_en_output_path),
            "cum_en_band_files": band_output_files,
            "total_pixel_count": total_pixel_count,
            "total_energy_above_mask": total_energy_above_mask,
        }

    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "sample": sample_name,
            "source_sample_dir": str(source_sample_dir),
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }


# =============================================================================
# PROGRAM GŁÓWNY
# =============================================================================

def main() -> None:
    args = parse_cli_args()
    energy_bands_khz = args.cum_en_bands_khz
    energy_report_signature = energy_report_settings_signature(energy_bands_khz)

    series_root = Path(__file__).resolve().parent
    assert_tiff_tile_config()
    settings_signature = processing_settings_signature()

    print("=" * 86)
    print("FOscy_v20.py — wspólna maska serii, PSD.tiff i kumulatywne energie PSD")
    print(f"Folder serii: {series_root}")
    print(f"Maksymalna liczba workerów: {MAX_WORKERS}")
    print(f"Wznawianie istniejących wyników: {RESUME_EXISTING_FILTERED_RESULTS}")
    if energy_bands_khz:
        print(
            "Dodatkowe pasma CumEn: "
            + ", ".join(energy_band_file_label(band) for band in energy_bands_khz)
        )
    else:
        print("Dodatkowe pasma CumEn: brak")
    print(
        "Limit pojedynczego TIFF-a: "
        f"~{TIFF_TARGET_BYTES_PER_1000_S / 1_000_000_000:.2f} GB / 1000 s "
        f"(pooling czasu: {TIFF_TIME_POOLING})"
    )
    print("=" * 86)

    source_samples = find_source_samples(series_root)
    if not source_samples:
        raise FileNotFoundError(
            "Nie znaleziono żadnej próbki o strukturze:\n"
            "  <nazwa próbki>/AE/oscy1/*.npy\n\n"
            f"Sprawdzony folder: {series_root}"
        )

    print("Znalezione próbki:")
    for sample_dir in source_samples:
        print(f"  - {sample_dir.name}")

    workers = min(MAX_WORKERS, len(source_samples))

    # Przygotowanie / synchronizacja folderów wynikowych. Foldery istniejące
    # zostają zachowane; nie usuwamy kompletnych rezultatów.
    target_by_source: dict[str, Path] = {}
    descriptor_by_source: dict[str, dict[str, Any]] = {}
    print("\nKontrola folderów '* - filtered'...")
    for source_sample_dir in source_samples:
        source_key = str(source_sample_dir)
        target_by_source[source_key] = prepare_filtered_copy(source_sample_dir)
        if remove_obsolete_log_tiff(target_by_source[source_key]):
            print(
                f"[{source_sample_dir.name}] usunięto dawny {OBSOLETE_LOG_TIFF_NAME}",
                flush=True,
            )
        descriptor_by_source[source_key] = source_descriptor(source_sample_dir)

    # -------------------------------------------------------------------------
    # PASS 1 — odczyt gotowych masek, a brakujące/nieaktualne tylko obliczamy.
    # -------------------------------------------------------------------------
    print("\nPASS 1/2 — maski indywidualne: cache lub obliczenie...")
    individual_mask_by_source: dict[str, np.ndarray] = {}
    sources_needing_mask: list[Path] = []

    for source_sample_dir in source_samples:
        source_key = str(source_sample_dir)
        cached_mask = load_reusable_individual_mask(
            source_sample_dir=source_sample_dir,
            target_sample_dir=target_by_source[source_key],
            descriptor=descriptor_by_source[source_key],
            settings_signature=settings_signature,
        )
        if cached_mask is not None:
            individual_mask_by_source[source_key] = cached_mask
            print(f"[{source_sample_dir.name}] PASS 1/2: maska z cache — GOTOWA", flush=True)
        else:
            sources_needing_mask.append(source_sample_dir)
            print(f"[{source_sample_dir.name}] PASS 1/2: brak aktualnej maski — liczę", flush=True)

    mask_results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    if sources_needing_mask:
        mask_workers = min(MAX_WORKERS, len(sources_needing_mask))
        with ProcessPoolExecutor(max_workers=mask_workers) as executor:
            future_to_source = {
                executor.submit(calculate_individual_mask_worker, str(sample_dir)): sample_dir
                for sample_dir in sources_needing_mask
            }
            for future in as_completed(future_to_source):
                result = future.result()
                if result["ok"]:
                    mask_results.append(result)
                    source_key = result["source_sample_dir"]
                    mask = np.asarray(result["individual_mask"], dtype=np.float32)
                    individual_mask_by_source[source_key] = mask
                    write_mask_ready_state(
                        source_sample_dir=Path(source_key),
                        target_sample_dir=target_by_source[source_key],
                        descriptor=descriptor_by_source[source_key],
                        settings_signature=settings_signature,
                        individual_mask=mask,
                    )
                    print(f"[{result['sample']}] maska indywidualna: GOTOWA", flush=True)
                else:
                    failures.append(result)
                    print(f"[{result['sample']}] maska indywidualna: BŁĄD", flush=True)
                    print(f"  {result['error']}", flush=True)

    if failures:
        print("\nNie można utworzyć wspólnej maski, ponieważ PASS 1 zakończył się błędami:")
        for failure in failures:
            print(f"  - {failure['sample']}: {failure['error']}")
        raise SystemExit(1)

    # Kontrola zgodności masek niezależnie od tego, czy pochodzą z cache.
    n_freq_values = {descriptor_by_source[str(sample)]["n_freq"] for sample in source_samples}
    if len(n_freq_values) != 1:
        detail = ", ".join(
            f"{sample.name}={descriptor_by_source[str(sample)]['n_freq']}"
            for sample in source_samples
        )
        raise ValueError(
            "Próbki mają różną liczbę binów częstotliwości; "
            "nie można utworzyć wspólnej maski. "
            f"{detail}"
        )

    n_freq = next(iter(n_freq_values))
    sample_order = [sample.name for sample in source_samples]
    individual_masks = np.stack(
        [individual_mask_by_source[str(sample)] for sample in source_samples],
        axis=0,
    )
    if individual_masks.shape != (len(source_samples), n_freq):
        raise RuntimeError("Nieprawidłowy kształt zestawu masek indywidualnych.")

    # MAKSIMUM po próbkach = wspólna górna obwiednia folderu/serii.
    envelope_indices = np.argmax(individual_masks, axis=0)
    series_upper_mask = np.max(individual_masks, axis=0).astype(np.float32)
    series_mask_sha256 = array_sha256(series_upper_mask)
    envelope_source_sample = np.asarray(
        [sample_order[index] for index in envelope_indices],
        dtype=object,
    )

    freq_axis_hz = np.linspace(
        FREQ_MIN_HZ,
        FREQ_MAX_HZ,
        n_freq,
        dtype=np.float64,
    )
    series_mask_npy_path = series_root / SERIES_MASK_NPY_NAME
    series_mask_csv_path = series_root / SERIES_MASK_CSV_NAME
    np.save(series_mask_npy_path, series_upper_mask)
    save_background_csv(
        series_mask_csv_path,
        freq_axis_hz,
        series_upper_mask,
        envelope_source_sample,
    )

    print("\nWspólna maska serii: GOTOWA")
    print(
        "  Definicja: series_mask(f) = max_i[min_s[min_t(smoothed_psd_i,s(f, t))]]."
    )

    # -------------------------------------------------------------------------
    # Kontrola kompletności per próbka. Tylko brakujące/niezgodne wykonają PASS 2.
    # -------------------------------------------------------------------------
    print("\nKontrola kompletności wyników...")
    tasks: list[dict[str, Any]] = []
    energy_report_tasks: list[dict[str, Any]] = []
    already_complete: list[str] = []
    adopted_legacy: list[str] = []

    for source_sample_dir in source_samples:
        source_key = str(source_sample_dir)
        target_sample_dir = target_by_source[source_key]
        descriptor = descriptor_by_source[source_key]
        individual_mask = individual_mask_by_source[source_key]

        status, reasons = inspect_existing_result(
            source_sample_dir=source_sample_dir,
            target_sample_dir=target_sample_dir,
            descriptor=descriptor,
            settings_signature=settings_signature,
            individual_mask=individual_mask,
            series_upper_mask=series_upper_mask,
            series_mask_sha256=series_mask_sha256,
            energy_bands_khz=energy_bands_khz,
            energy_report_signature=energy_report_signature,
        )

        if status == "complete":
            already_complete.append(source_sample_dir.name)
            print(f"[{source_sample_dir.name}] wynik kompletny — POMIJAM", flush=True)
            continue

        if status == "energy_reports_missing":
            print(
                f"[{source_sample_dir.name}] filtrowanie i TIFF-y kompletne — "
                "generuję tylko CumEn",
                flush=True,
            )
            for reason in reasons[:4]:
                print(f"  - {reason}", flush=True)
            energy_report_tasks.append(
                {
                    "source_sample_dir": source_key,
                    "target_sample_dir": str(target_sample_dir),
                    "individual_mask": individual_mask,
                    "series_upper_mask": series_upper_mask,
                    "source_descriptor": descriptor,
                    "settings_signature": settings_signature,
                    "individual_mask_sha256": array_sha256(individual_mask),
                    "series_mask_sha256": series_mask_sha256,
                    "energy_bands_khz": energy_bands_khz,
                    "energy_report_signature": energy_report_signature,
                }
            )
            continue

        if status == "adoptable_legacy":
            completion_state = make_resume_state_base(
                source_sample_dir=source_sample_dir,
                descriptor=descriptor,
                settings_signature=settings_signature,
                individual_mask_sha256=array_sha256(individual_mask),
            )
            completion_state.update(
                {
                    "stage": "complete",
                    "series_mask_sha256": series_mask_sha256,
                    "series_mask_file": BACKGROUND_SERIES_UPPER_NPY_NAME,
                    "adopted_from_legacy_output": True,
                }
            )
            atomic_write_json(resume_state_path(target_sample_dir), completion_state)
            adopted_legacy.append(source_sample_dir.name)
            print(
                f"[{source_sample_dir.name}] wynik legacy kompletny — ZAADOPTOWANY; "
                "generuję CumEn",
                flush=True,
            )
            energy_report_tasks.append(
                {
                    "source_sample_dir": source_key,
                    "target_sample_dir": str(target_sample_dir),
                    "individual_mask": individual_mask,
                    "series_upper_mask": series_upper_mask,
                    "source_descriptor": descriptor,
                    "settings_signature": settings_signature,
                    "individual_mask_sha256": array_sha256(individual_mask),
                    "series_mask_sha256": series_mask_sha256,
                    "energy_bands_khz": energy_bands_khz,
                    "energy_report_signature": energy_report_signature,
                }
            )
            continue

        print(f"[{source_sample_dir.name}] wynik niepełny/nieaktualny — WZNOWIENIE OD PASS 2", flush=True)
        for reason in reasons[:6]:
            print(f"  - {reason}", flush=True)
        if len(reasons) > 6:
            print(f"  - ... oraz {len(reasons) - 6} kolejnych problemów", flush=True)

        tasks.append(
            {
                "source_sample_dir": source_key,
                "target_sample_dir": str(target_sample_dir),
                "individual_mask": individual_mask,
                "series_upper_mask": series_upper_mask,
                "envelope_source_sample": envelope_source_sample,
                "source_descriptor": descriptor,
                "settings_signature": settings_signature,
                "individual_mask_sha256": array_sha256(individual_mask),
                "series_mask_sha256": series_mask_sha256,
                "energy_bands_khz": energy_bands_khz,
                "energy_report_signature": energy_report_signature,
            }
        )

    # -------------------------------------------------------------------------
    # RAPORTY ENERGII — dla kompletnych wyników v14 tylko z filtered_segments.
    # -------------------------------------------------------------------------
    energy_reports_completed: list[dict[str, Any]] = []
    energy_report_failures: list[dict[str, Any]] = []
    if energy_report_tasks:
        print("\nRaporty energii — bez ponownego filtrowania i bez TIFF-ów...")
        energy_workers = min(MAX_WORKERS, len(energy_report_tasks))
        with ProcessPoolExecutor(max_workers=energy_workers) as executor:
            future_to_task = {
                executor.submit(generate_energy_reports_from_filtered_worker, task): task
                for task in energy_report_tasks
            }
            for future in as_completed(future_to_task):
                result = future.result()
                if result["ok"]:
                    energy_reports_completed.append(result)
                    print(f"[{result['sample']}] CumEn GOTOWE", flush=True)
                    print(f"  CumEn:     {result['cum_en_file']}", flush=True)
                    print(
                        "  ΣE mask: "
                        f"{result['total_energy_above_mask']:.6E}",
                        flush=True,
                    )
                else:
                    energy_report_failures.append(result)
                    print(f"[{result['sample']}] BŁĄD RAPORTÓW ENERGII", flush=True)
                    print(f"  {result['error']}", flush=True)
    else:
        print("\nRaporty energii — brak pracy: wszystkie są kompletne.")

    # -------------------------------------------------------------------------
    # PASS 2 — tylko próbki wymagające obliczenia; równolegle maks. 4 procesy.
    # -------------------------------------------------------------------------
    completed: list[dict[str, Any]] = []
    process_failures: list[dict[str, Any]] = []
    if tasks:
        print("\nPASS 2/2 — filtracja, zliczenia i TIFF-y tylko dla wyników niepełnych...")
        pass2_workers = min(MAX_WORKERS, len(tasks))
        with ProcessPoolExecutor(max_workers=pass2_workers) as executor:
            future_to_task = {
                executor.submit(process_one_sample_worker, task): task
                for task in tasks
            }
            for future in as_completed(future_to_task):
                result = future.result()
                if result["ok"]:
                    completed.append(result)
                    print(f"[{result['sample']}] GOTOWE", flush=True)
                    print(f"  FOscy: {result['foscy_file']}", flush=True)
                    print(f"  TIFF:  {result['linear_tiff']}", flush=True)
                    print(f"  CumEn: {result['cum_en_file']}", flush=True)
                    print(f"  Σ pixel_count: {result['total_pixel_count']}", flush=True)
                    print(
                        "  ΣE mask: "
                        f"{result['total_energy_above_mask']:.6E}",
                        flush=True,
                    )
                else:
                    process_failures.append(result)
                    print(f"[{result['sample']}] BŁĄD", flush=True)
                    print(f"  {result['error']}", flush=True)
    else:
        print("\nPASS 2/2 — brak pracy: wszystkie wyniki są kompletne.")

    print("\n" + "=" * 86)
    print(f"Pomijane kompletne: {len(already_complete)}")
    print(f"Zaadoptowane kompletne legacy: {len(adopted_legacy)}")
    print(
        f"Raporty energii uzupełnione bez PASS 2: "
        f"{len(energy_reports_completed)}/{len(energy_report_tasks)} próbek."
    )
    print(f"Przetworzone teraz pełnym PASS 2: {len(completed)}/{len(tasks)} próbek.")
    print(f"Wspólna maska NPY: {series_mask_npy_path.name}")
    print(f"Wspólna maska CSV: {series_mask_csv_path.name}")

    if energy_report_failures or process_failures:
        if energy_report_failures:
            print("\nBłędy raportów energii:")
            for failure in energy_report_failures:
                print(f"  - {failure['sample']}: {failure['error']}")
        if process_failures:
            print("\nBłędy PASS 2:")
            for failure in process_failures:
                print(f"  - {failure['sample']}: {failure['error']}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
