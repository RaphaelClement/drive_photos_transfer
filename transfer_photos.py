#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Script de tri, déduplication par qualité (poids max) et transfert de photos/vidéos
depuis Google Drive A, B, C et Amazon Photos vers Google Drive D.

Architecture "Scan & Elect" :
1. Scanne les métadonnées de A, B, C (via l'Explorateur Windows sans ouvrir les fichiers)
   et d'Amazon Photos (via l'API amazon-photos par cookies) sur la période cible.
2. Regroupe les fichiers par (nom_de_base_minuscule, categorie_media ["photo" | "video"]).
3. Élit pour chaque groupe l'unique fichier "gagnant" (le plus lourd en octets = moins compressé).
4. Vérifie l'état actuel du Drive D et ne copie/télécharge que le gagnant une seule fois,
   garantissant ZÉRO conflit de nom à un instant T dans le Drive D.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(iterable, **kwargs):
        return iterable


# Extensions classées par catégorie pour qu'une vidéo Live Photo (.MOV)
# n'écrase jamais la photo (.HEIC/.JPG) portant le même nom de base.
PHOTO_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp",
    ".bmp", ".tiff", ".tif", ".raw", ".dng", ".cr2", ".nef", ".arw", ".gif"
}

VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".m4v", ".3gp", ".webm", ".mts"
}

# Expression régulière pour extraire une date YYYYMMDD ou YYYY-MM-DD depuis le nom du fichier
# Exemples reconnus : IMG_20250908_142210.jpg, PXL_20250908_..., 2025-09-08_..., IMG-20250908-WA0001.jpg
FILENAME_DATE_REGEX = re.compile(
    r"(?:^|[^0-9])(20\d{2})[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])(?:[^0-9]|$)"
)


@dataclass
class MediaCandidate:
    source_name: str          # Ex: "Drive_A", "Drive_B", "Drive_C", "Amazon_Photos"
    filename: str             # Nom complet avec extension, ex: "IMG_1234.HEIC"
    base_name_key: str        # Nom sans extension en minuscule, ex: "img_1234"
    extension: str            # Extension en minuscule, ex: ".heic"
    category: str             # "photo" ou "video"
    size_bytes: int           # Taille exacte en octets
    capture_date: dt.date     # Date retenue (nom du fichier ou mtime/contentDate)
    date_source: str          # "filename", "mtime", ou "amazon_metadata"
    local_path: Optional[Path] = None     # Chemin Windows si Drive A, B, C
    amazon_node_id: Optional[str] = None  # ID du noeud si Amazon Photos
    mtime_timestamp: Optional[float] = None


@dataclass
class ElectionResult:
    key: Tuple[str, str]  # (base_name_key, category)
    winner: MediaCandidate
    losers: List[MediaCandidate] = field(default_factory=list)
    action: str = "PENDING"
    notes: str = ""


def classify_extension(ext: str) -> Optional[str]:
    ext_lower = ext.lower()
    if ext_lower in PHOTO_EXTENSIONS:
        return "photo"
    if ext_lower in VIDEO_EXTENSIONS:
        return "video"
    return None


def extract_date_from_filename(filename: str) -> Optional[dt.date]:
    """Tente d'extraire une date valide depuis le nom du fichier."""
    stem = os.path.splitext(filename)[0]
    for match in FILENAME_DATE_REGEX.finditer(stem):
        year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
        try:
            return dt.date(year, month, day)
        except ValueError:
            continue
    return None


def scan_local_drive(
    source_name: str,
    root_folder: Path,
    date_start: dt.date,
    date_end: dt.date,
) -> List[MediaCandidate]:
    """
    Parcourt récursivement un dossier Google Drive monté dans Windows.
    Utilise os.scandir() qui lit les entrées du répertoire Windows (taille + mtime)
    SANS ouvrir ni télécharger le contenu des fichiers depuis le cloud.
    """
    candidates: List[MediaCandidate] = []
    if not root_folder.exists():
        print(f"[ATTENTION] Le dossier source {source_name} ({root_folder}) est introuvable. Ignoré.")
        return candidates

    print(f"[SCAN] Exploration de {source_name} : {root_folder} ...")
    stack = [root_folder]

    while stack:
        current_dir = stack.pop()
        try:
            with os.scandir(current_dir) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            base_name, ext = os.path.splitext(entry.name)
                            category = classify_extension(ext)
                            if not category:
                                continue

                            stat_info = entry.stat(follow_symlinks=False)
                            size_bytes = stat_info.st_size
                            mtime_ts = stat_info.st_mtime

                            # 1. Priorité à la date dans le nom du fichier
                            file_date = extract_date_from_filename(entry.name)
                            if file_date is not None:
                                date_source = "filename"
                            else:
                                # 2. Fallback sur la date de modification Windows
                                file_date = dt.date.fromtimestamp(mtime_ts)
                                date_source = "mtime"

                            if date_start <= file_date <= date_end:
                                candidates.append(
                                    MediaCandidate(
                                        source_name=source_name,
                                        filename=entry.name,
                                        base_name_key=base_name.lower(),
                                        extension=ext.lower(),
                                        category=category,
                                        size_bytes=size_bytes,
                                        capture_date=file_date,
                                        date_source=date_source,
                                        local_path=Path(entry.path),
                                        mtime_timestamp=mtime_ts,
                                    )
                                )
                    except OSError as err:
                        print(f"  [Erreur lecture entrée] {entry.path}: {err}")
        except OSError as err:
            print(f"  [Erreur accès dossier] {current_dir}: {err}")

    print(f"  -> {len(candidates)} médias trouvés dans la période sur {source_name}.")
    return candidates


def parse_iso_date(iso_str: Optional[str]) -> Optional[dt.date]:
    if not iso_str:
        return None
    try:
        return dt.date.fromisoformat(iso_str[:10])
    except ValueError:
        return None


def scan_amazon_photos(
    ap_config: dict,
    date_start: dt.date,
    date_end: dt.date,
) -> Tuple[List[MediaCandidate], Optional[object]]:
    """
    Interroge Amazon Photos à distance via la librairie `amazon-photos`
    pour récupérer les métadonnées (nom, taille en octets, ID, date) sans télécharger les fichiers.
    """
    if not ap_config.get("enabled", False):
        print("[SCAN] Amazon Photos est désactivé dans config.json.")
        return [], None

    try:
        from amazon_photos import AmazonPhotos
    except ImportError:
        print(
            "[ERREUR] La librairie 'amazon-photos' n'est pas installée.\n"
            "Exécutez : pip install -r requirements.txt"
        )
        sys.exit(1)

    cookies = ap_config.get("cookies", {})
    if any("REMPLACER" in str(v) for v in cookies.values()):
        print(
            "[ERREUR] Vous devez renseigner vos cookies Amazon Photos dans config.json\n"
            "(session-id, ubid-acbfr, at-acbfr) ou passer enabled à false."
        )
        sys.exit(1)

    print("[SCAN] Connexion à Amazon Photos et récupération du catalogue à distance...")
    ap_client = AmazonPhotos(
        cookies=cookies,
        tld=ap_config.get("tld", "fr"),
    )

    # On élargit légèrement la requête serveur de +/- 2 jours pour éviter les écarts de fuseau horaire,
    # puis on applique notre filtre strict (date du nom en priorité, sinon contentDate/createdDate).
    query_start = (date_start - dt.timedelta(days=2)).isoformat()
    query_end = (date_end + dt.timedelta(days=2)).isoformat()

    # Récupère les noeuds PHOTO et VIDEO
    df_nodes = ap_client.query(
        f"type:(PHOTOS OR VIDEOS) AND contentDate:[{query_start}T00:00:00Z TO {query_end}T23:59:59Z]"
    )

    candidates: List[MediaCandidate] = []
    if df_nodes is None or len(df_nodes) == 0:
        print("  -> 0 média trouvé sur Amazon Photos pour cette plage de dates.")
        return candidates, ap_client

    records = df_nodes.to_dict(orient="records") if hasattr(df_nodes, "to_dict") else list(df_nodes)

    for row in records:
        filename = row.get("name")
        node_id = row.get("id")
        if not filename or not node_id:
            continue

        base_name, ext = os.path.splitext(filename)
        category = classify_extension(ext)
        if not category:
            continue

        # Extraction de la taille en octets (peut être à plat ou dans contentProperties)
        size_bytes = row.get("size")
        content_props = row.get("contentProperties") if isinstance(row.get("contentProperties"), dict) else {}
        if size_bytes is None:
            size_bytes = content_props.get("size", 0)
        size_bytes = int(size_bytes or 0)

        # 1. Priorité à la date dans le nom du fichier
        file_date = extract_date_from_filename(filename)
        date_source = "filename"

        # 2. Sinon date de prise de vue Amazon (contentDate) ou création (createdDate)
        if file_date is None:
            raw_date = (
                row.get("contentDate")
                or content_props.get("contentDate")
                or row.get("createdDate")
            )
            file_date = parse_iso_date(str(raw_date) if raw_date else None)
            date_source = "amazon_metadata"

        if file_date is None:
            continue

        if date_start <= file_date <= date_end:
            candidates.append(
                MediaCandidate(
                    source_name="Amazon_Photos",
                    filename=filename,
                    base_name_key=base_name.lower(),
                    extension=ext.lower(),
                    category=category,
                    size_bytes=size_bytes,
                    capture_date=file_date,
                    date_source=date_source,
                    amazon_node_id=str(node_id),
                )
            )

    print(f"  -> {len(candidates)} médias trouvés dans la période sur Amazon Photos.")
    return candidates, ap_client


def elect_winners(all_candidates: List[MediaCandidate]) -> List[ElectionResult]:
    """
    Regroupe tous les candidats par (base_name_key, category) et élit le fichier le plus lourd.
    En cas d'égalité parfaite de taille en octets entre un Drive local et Amazon Photos,
    privilégie le fichier déjà présent sur un Drive monté (évite un téléchargement web inutile).
    """
    groups: Dict[Tuple[str, str], List[MediaCandidate]] = {}
    for cand in all_candidates:
        key = (cand.base_name_key, cand.category)
        groups.setdefault(key, []).append(cand)

    results: List[ElectionResult] = []
    for key, cand_list in sorted(groups.items(), key=lambda x: x[0]):
        # Tri décroissant par taille en octets ; à taille égale, préfère local_path non-nul
        sorted_cands = sorted(
            cand_list,
            key=lambda c: (c.size_bytes, 1 if c.local_path is not None else 0),
            reverse=True,
        )
        winner = sorted_cands[0]
        losers = sorted_cands[1:]
        results.append(ElectionResult(key=key, winner=winner, losers=losers))

    return results


def index_existing_destination(dest_folder: Path) -> Dict[Tuple[str, str], List[Tuple[Path, int]]]:
    """
    Parcourt RÉCURSIVEMENT le dossier cible sur le Drive D (y compris tous ses sous-dossiers)
    pour connaître toutes les photos/vidéos déjà présentes sur le Drive D.
    Retourne un dictionnaire :
      (base_name_key, category) -> Liste de (chemin_existant, taille_octets) triée du plus lourd au plus léger.
    """
    existing: Dict[Tuple[str, str], List[Tuple[Path, int]]] = {}
    if not dest_folder.exists():
        return existing

    print(f"[SCAN] Indexation récursive des fichiers déjà présents dans le Drive D ({dest_folder}) ...")
    stack = [dest_folder]
    total_found = 0

    while stack:
        current_dir = stack.pop()
        try:
            with os.scandir(current_dir) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            if entry.name.startswith(".tmp_"):
                                continue
                            base_name, ext = os.path.splitext(entry.name)
                            category = classify_extension(ext)
                            if not category:
                                continue
                            size_bytes = entry.stat(follow_symlinks=False).st_size
                            key = (base_name.lower(), category)
                            existing.setdefault(key, []).append((Path(entry.path), size_bytes))
                            total_found += 1
                    except OSError as err:
                        print(f"  [Erreur lecture entrée Drive D] {entry.path}: {err}")
        except OSError as err:
            print(f"  [Erreur accès sous-dossier Drive D] {current_dir}: {err}")

    # Tri décroissant par taille pour chaque clé afin que le meilleur exemplaire déjà dans D soit en position 0
    for key in existing:
        existing[key].sort(key=lambda item: item[1], reverse=True)

    print(f"  -> {total_found} médias déjà présents détectés dans le Drive D (sous-dossiers inclus).")
    return existing


def execute_transfers(
    elections: List[ElectionResult],
    dest_folder: Path,
    ap_client: Optional[object],
    dry_run: bool,
) -> None:
    """
    Copie ou télécharge uniquement les gagnants vers le Drive D, sans jamais créer de conflit de nom,
    en tenant compte des fichiers déjà présents dans les sous-dossiers du Drive D.
    """
    if not dry_run:
        dest_folder.mkdir(parents=True, exist_ok=True)

    existing_in_d = index_existing_destination(dest_folder)

    copied_count = 0
    replaced_count = 0
    skipped_count = 0
    error_count = 0

    mode_label = "SIMULATION (DRY-RUN)" if dry_run else "TRANSFERT RÉEL"
    print(f"\n[{mode_label}] Traitement de {len(elections)} médias uniques vers {dest_folder} ...")

    for item in tqdm(elections, desc="Progression", unit="média"):
        winner = item.winner
        existing_list = existing_in_d.get(item.key, [])

        # Vérifie si ce média (même nom de base + même catégorie) existe déjà dans D (ou un sous-dossier de D)
        if existing_list:
            best_existing_path, best_existing_size = existing_list[0]
            if best_existing_size >= winner.size_bytes:
                item.action = "SKIP_ALREADY_IN_D"
                rel_loc = best_existing_path.relative_to(dest_folder) if best_existing_path.is_relative_to(dest_folder) else best_existing_path
                item.notes = f"Déjà présent dans D ({rel_loc}) avec taille >= ({best_existing_size} octets)"
                skipped_count += 1
                continue
            # Si une version moins bonne existait dans un sous-dossier de D, on la remplacera au même endroit !
            target_dir = best_existing_path.parent
        else:
            # Nouveau fichier absent de D : copié à la racine de dest_folder
            target_dir = dest_folder

        target_path = target_dir / winner.filename

        if dry_run:
            if existing_list:
                best_existing_path, best_existing_size = existing_list[0]
                rel_loc = best_existing_path.relative_to(dest_folder) if best_existing_path.is_relative_to(dest_folder) else best_existing_path
                item.action = "DRY_RUN_WOULD_REPLACE"
                item.notes = (
                    f"Remplacerait {rel_loc} ({best_existing_size} octets) "
                    f"par {winner.filename} ({winner.size_bytes} octets) depuis {winner.source_name}"
                )
                replaced_count += 1
            else:
                item.action = "DRY_RUN_WOULD_COPY"
                item.notes = f"Copierait depuis {winner.source_name} ({winner.size_bytes} octets)"
                copied_count += 1
            continue

        # Mode réel : si une ou plusieurs versions plus légères existent déjà dans D (et ses sous-dossiers),
        # on les supprime d'abord pour qu'il n'y ait JAMAIS deux fichiers du même nom dans D.
        try:
            for old_path, _ in existing_list:
                if old_path.exists():
                    old_path.unlink()

            if winner.local_path is not None:
                # Copie atomique via un fichier temporaire dans le dossier cible
                temp_target = target_dir / f".tmp_{winner.filename}"
                shutil.copy2(winner.local_path, temp_target)
                os.replace(temp_target, target_path)
            elif winner.amazon_node_id is not None and ap_client is not None:
                # Téléchargement depuis Amazon Photos dans un dossier temporaire local puis déplacement vers D
                with tempfile.TemporaryDirectory() as tmp_dir:
                    ap_client.download([winner.amazon_node_id], out=tmp_dir)
                    downloaded_files = list(Path(tmp_dir).iterdir())
                    if not downloaded_files:
                        raise FileNotFoundError(
                            f"Échec du téléchargement Amazon Photos pour {winner.filename} (node {winner.amazon_node_id})"
                        )
                    shutil.move(str(downloaded_files[0]), str(target_path))

            if existing_list:
                best_existing_path, best_existing_size = existing_list[0]
                rel_loc = best_existing_path.relative_to(dest_folder) if best_existing_path.is_relative_to(dest_folder) else best_existing_path
                item.action = "REPLACED_IN_D"
                item.notes = f"Ancienne version ({rel_loc}, {best_existing_size} octets) supprimée puis remplacée au même endroit"
                replaced_count += 1
            else:
                item.action = "COPIED_TO_D"
                item.notes = "Copié avec succès"
                copied_count += 1

        except Exception as exc:
            item.action = "ERROR"
            item.notes = str(exc)
            error_count += 1
            print(f"\n[ERREUR] Impossible de transférer {winner.filename}: {exc}")

    print("\n" + "=" * 65)
    print(f"RÉSUMÉ ({mode_label}) :")
    print(f"  - Médias uniques identifiés : {len(elections)}")
    print(f"  - Nouveaux fichiers copiés  : {copied_count}")
    print(f"  - Fichiers améliorés dans D : {replaced_count}")
    print(f"  - Déjà optimaux dans D      : {skipped_count}")
    print(f"  - Erreurs                   : {error_count}")
    print("=" * 65)


def write_csv_report(elections: List[ElectionResult], report_path: Path) -> None:
    """Génère un rapport CSV complet listant chaque photo élue et les doublons écartés."""
    with report_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow([
            "Nom_de_base",
            "Categorie",
            "Fichier_Gagnant",
            "Source_Gagnante",
            "Taille_Gagnante_Octets",
            "Taille_Gagnante_Mo",
            "Date_Retenue",
            "Origine_Date",
            "Nb_Doublons_Ecartes",
            "Details_Doublons_Ecartes",
            "Action",
            "Notes",
        ])

        for item in elections:
            w = item.winner
            losers_desc = " | ".join(
                f"{l.source_name}:{l.filename} ({l.size_bytes} octets)"
                for l in item.losers
            )
            writer.writerow([
                item.key[0],
                item.key[1],
                w.filename,
                w.source_name,
                w.size_bytes,
                f"{w.size_bytes / (1024 * 1024):.2f}",
                w.capture_date.isoformat(),
                w.date_source,
                len(item.losers),
                losers_desc,
                item.action,
                item.notes,
            ])

    print(f"[RAPPORT] Rapport CSV détaillé enregistré dans : {report_path.resolve()}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Tri et transfert sans doublon (qualité max) de Google Drive A, B, C + Amazon Photos vers Drive D."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.json"),
        help="Chemin vers le fichier de configuration JSON (défaut: config.json)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Mode simulation : scanne, compare et génère le rapport CSV sans copier aucun fichier.",
    )
    args = parser.parse_args()

    if not args.config.exists():
        print(f"[ERREUR] Fichier de configuration introuvable : {args.config}")
        sys.exit(1)

    with args.config.open("r", encoding="utf-8") as f:
        config = json.load(f)

    date_start = dt.date.fromisoformat(config["date_start"])
    date_end = dt.date.fromisoformat(config["date_end"])
    print(f"Période filtrée : du {date_start.isoformat()} au {date_end.isoformat()} inclus.\n")

    all_candidates: List[MediaCandidate] = []

    # 1. Scan des Google Drives montés (A, B, C)
    drive_sources: Dict[str, str] = config.get("drive_sources", {})
    for source_name, folder_str in drive_sources.items():
        all_candidates.extend(
            scan_local_drive(
                source_name=source_name,
                root_folder=Path(folder_str),
                date_start=date_start,
                date_end=date_end,
            )
        )

    # 2. Scan d'Amazon Photos à distance
    ap_candidates, ap_client = scan_amazon_photos(
        ap_config=config.get("amazon_photos", {}),
        date_start=date_start,
        date_end=date_end,
    )
    all_candidates.extend(ap_candidates)

    if not all_candidates:
        print("\nAucun média trouvé sur la période spécifiée. Arrêt.")
        return

    # 3. Élection de la meilleure qualité par nom de base et catégorie (photo vs vidéo)
    elections = elect_winners(all_candidates)
    total_duplicates = sum(len(e.losers) for e in elections)
    print(
        f"\n[ÉLECTION] {len(all_candidates)} fichiers analysés -> "
        f"{len(elections)} médias uniques retenus ({total_duplicates} doublons/versions compressées écartés)."
    )

    # 4. Copie (ou simulation) vers le Drive D
    dest_folder = Path(config["drive_destination"])
    execute_transfers(
        elections=elections,
        dest_folder=dest_folder,
        ap_client=ap_client,
        dry_run=args.dry_run,
    )

    # 5. Écriture du rapport CSV
    report_csv = Path(config.get("report_csv", "rapport_transfert.csv"))
    write_csv_report(elections, report_csv)


if __name__ == "__main__":
    main()
