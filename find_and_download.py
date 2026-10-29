#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Script de recherche précise par nom et de récupération dans le dossier "Téléchargements" du PC.

Contourne la barre de recherche limitée d'Amazon Photos en récupérant l'index des noeuds
via l'API `amazon-photos` (ainsi que les Drives sources locaux si disponibles) pour retrouver
exactement les fichiers demandés dans leur MEILLEURE qualité (poids maximal en octets),
puis les télécharge/copie dans le dossier "Téléchargements" (Downloads) de Windows.

Modes d'utilisation :
  1. Mode interactif (coller 1 ou 40 noms d'un coup) :
       python find_and_download.py
  2. Depuis un fichier texte (1 nom par ligne) :
       python find_and_download.py --file erreurs.txt
  3. Automatiquement depuis les erreurs de rapport_transfert.csv :
       python find_and_download.py --from-report
  4. Directement en argument :
       python find_and_download.py IMG_1234.JPG IMG_5678.HEIC
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(iterable, **kwargs):
        return iterable


PHOTO_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp",
    ".bmp", ".tiff", ".tif", ".raw", ".dng", ".cr2", ".nef", ".arw", ".gif"
}

VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".m4v", ".3gp", ".webm", ".mts"
}


@dataclass
class FoundMedia:
    source_name: str
    filename: str
    base_name_lower: str
    ext_lower: str
    category: str
    size_bytes: int
    local_path: Optional[Path] = None
    amazon_node_id: Optional[str] = None


def classify_extension(ext: str) -> str:
    ext_lower = ext.lower()
    if ext_lower in PHOTO_EXTENSIONS:
        return "photo"
    if ext_lower in VIDEO_EXTENSIONS:
        return "video"
    return "other"


def get_windows_downloads_folder() -> Path:
    """
    Détecte le vrai dossier 'Téléchargements' de Windows (même s'il a été déplacé sur un autre disque),
    avec repli automatique sur C:\\Users\\<Nom>\\Downloads.
    """
    if os.name == "nt":
        try:
            import winreg
            sub_key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            downloads_guid = "{374DE290-123F-4565-9164-39C4925E467B}"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub_key) as key:
                val, _ = winreg.QueryValueEx(key, downloads_guid)
                expanded = os.path.expandvars(val)
                p = Path(expanded)
                if p.exists():
                    return p
        except Exception:
            pass

    fallback = Path.home() / "Downloads"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def normalize_query_names(raw_names: List[str]) -> List[str]:
    """Nettoie la liste des noms fournis par l'utilisateur (supprime guillemets, chemins éventuels, espaces)."""
    cleaned: List[str] = []
    seen = set()
    for raw in raw_names:
        # Supporte les séparations par point-virgule ou virgule si plusieurs noms sur une ligne
        parts = [raw] if (";" not in raw) else raw.split(";")
        for part in parts:
            item = part.strip().strip('"').strip("'")
            if not item:
                continue
            # Si l'utilisateur a collé un chemin complet, ne garder que le nom du fichier
            item = os.path.basename(item)
            if item.lower() not in seen:
                seen.add(item.lower())
                cleaned.append(item)
    return cleaned


def load_names_from_report(report_path: Path) -> List[str]:
    """Extrait automatiquement tous les fichiers ayant le statut ERROR dans rapport_transfert.csv."""
    if not report_path.exists():
        print(f"[ERREUR] Le fichier rapport {report_path} est introuvable.")
        sys.exit(1)

    error_files: List[str] = []
    with report_path.open("r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            action = (row.get("Action") or "").strip().upper()
            filename = (row.get("Fichier_Gagnant") or "").strip()
            if action == "ERROR" and filename:
                error_files.append(filename)

    print(f"[RAPPORT] {len(error_files)} fichier(s) en statut ERROR trouvés dans {report_path}.")
    return error_files


def prompt_interactive_names(default_report: Path) -> List[str]:
    print("=" * 70)
    print(" RECHERCHE PRÉCISE & TÉLÉCHARGEMENT (AMAZON PHOTOS + DRIVES)")
    print("=" * 70)
    if default_report.exists():
        print("Astuce : Tapez 'erreurs' pour récupérer automatiquement les photos en ERREUR")
        print(f"         depuis {default_report}.\n")

    print("Collez le nom précis de la photo (ou collez vos ~40 noms d'un coup, un par ligne).")
    print("Appuyez deux fois sur ENTRÉE (ligne vide) quand vous avez terminé :\n")

    lines: List[str] = []
    while True:
        try:
            line = input("> ").strip()
        except EOFError:
            break

        if not line:
            if lines:
                break
            continue

        if line.lower() in {"erreurs", "erreur", "error", "errors"} and not lines:
            return load_names_from_report(default_report)

        lines.append(line)

    return normalize_query_names(lines)


def build_needed_sets(target_names: List[str]):
    """Prépare les ensembles de noms complets et noms de base recherchés pour un filtrage instantané."""
    exact_names_lower = set()
    base_names_lower = set()
    for name in target_names:
        exact_names_lower.add(name.lower())
        stem, _ = os.path.splitext(name)
        base_names_lower.add(stem.lower())
    return exact_names_lower, base_names_lower


def search_amazon_photos(
    ap_config: dict,
    target_names: List[str],
    date_start_str: Optional[str],
    date_end_str: Optional[str],
) -> tuple[List[FoundMedia], Optional[object]]:
    """
    Recherche les fichiers demandés dans Amazon Photos sans passer par le moteur de recherche Web cassé :
    1. Cherche d'abord dans le catalogue de la plage de dates (config.json) — très rapide.
    2. S'il manque encore certaines photos demandées, étend automatiquement la recherche à TOUT Amazon Photos.
    """
    if not ap_config.get("enabled", False):
        print("[INFO] Amazon Photos est désactivé dans config.json.")
        return [], None

    try:
        from amazon_photos import AmazonPhotos
    except ImportError:
        print("[ERREUR] La librairie 'amazon-photos' n'est pas installée (pip install -r requirements.txt).")
        sys.exit(1)

    cookies = ap_config.get("cookies", {})
    if any("REMPLACER" in str(v) for v in cookies.values()):
        print("[ERREUR] Veuillez renseigner vos cookies Amazon Photos dans config.json.")
        sys.exit(1)

    print("\n[AMAZON PHOTOS] Connexion au compte Amazon Photos...")
    ap_client = AmazonPhotos(
        cookies=cookies,
        tld=ap_config.get("tld", "fr"),
    )

    exact_needed, base_needed = build_needed_sets(target_names)
    found: List[FoundMedia] = []
    seen_node_ids = set()

    def parse_dataframe(df_nodes) -> None:
        if df_nodes is None or len(df_nodes) == 0:
            return
        records = df_nodes.to_dict(orient="records") if hasattr(df_nodes, "to_dict") else list(df_nodes)
        for row in records:
            node_id = row.get("id")
            filename = row.get("name")
            if not node_id or not filename or str(node_id) in seen_node_ids:
                continue

            fname_lower = filename.lower()
            stem, ext = os.path.splitext(filename)
            stem_lower = stem.lower()

            if fname_lower in exact_needed or stem_lower in base_needed:
                size_bytes = row.get("size")
                content_props = row.get("contentProperties") if isinstance(row.get("contentProperties"), dict) else {}
                if size_bytes is None:
                    size_bytes = content_props.get("size", 0)
                size_bytes = int(size_bytes or 0)

                seen_node_ids.add(str(node_id))
                found.append(
                    FoundMedia(
                        source_name="Amazon_Photos",
                        filename=filename,
                        base_name_lower=stem_lower,
                        ext_lower=ext.lower(),
                        category=classify_extension(ext),
                        size_bytes=size_bytes,
                        amazon_node_id=str(node_id),
                    )
                )

    # Passe 1 : Recherche rapide sur la période configurée dans config.json
    if date_start_str and date_end_str:
        d_start = (dt.date.fromisoformat(date_start_str) - dt.timedelta(days=5)).isoformat()
        d_end = (dt.date.fromisoformat(date_end_str) + dt.timedelta(days=5)).isoformat()
        print(f"[AMAZON PHOTOS] Recherche dans la période {d_start} -> {d_end} ...")
        df_period = ap_client.query(
            f"type:(PHOTOS OR VIDEOS) AND contentDate:[{d_start}T00:00:00Z TO {d_end}T23:59:59Z]"
        )
        parse_dataframe(df_period)

    # Vérifier si toutes les photos ont été trouvées sur Amazon Photos
    found_bases = {m.base_name_lower for m in found}
    found_exacts = {m.filename.lower() for m in found}
    still_missing = [
        t for t in target_names
        if t.lower() not in found_exacts and os.path.splitext(t)[0].lower() not in found_bases
    ]

    # Passe 2 : S'il en manque, on scanne l'intégralité du catalogue Amazon Photos sans filtre de date !
    if still_missing:
        print(
            f"[AMAZON PHOTOS] {len(still_missing)} photo(s) hors période ou sans date EXIF : "
            "recherche élargie sur TOUT le compte Amazon Photos..."
        )
        df_all = ap_client.query("type:(PHOTOS OR VIDEOS)")
        parse_dataframe(df_all)

    print(f"  -> {len(found)} correspondance(s) trouvée(s) sur Amazon Photos.")
    return found, ap_client


def search_local_drives(drive_sources: Dict[str, str], target_names: List[str]) -> List[FoundMedia]:
    """
    Cherche également les noms demandés dans les Google Drives montés (A, B, C) s'ils sont accessibles,
    afin de comparer la taille et toujours garantir la meilleure qualité.
    """
    exact_needed, base_needed = build_needed_sets(target_names)
    found: List[FoundMedia] = []

    for source_name, folder_str in drive_sources.items():
        root = Path(folder_str)
        if not root.exists():
            continue

        print(f"[SCAN LOCAL] Vérification de {source_name} ({root}) ...")
        stack = [root]
        while stack:
            curr = stack.pop()
            try:
                with os.scandir(curr) as entries:
                    for entry in entries:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(Path(entry.path))
                            elif entry.is_file(follow_symlinks=False):
                                fname_lower = entry.name.lower()
                                stem, ext = os.path.splitext(entry.name)
                                stem_lower = stem.lower()
                                if fname_lower in exact_needed or stem_lower in base_needed:
                                    size_bytes = entry.stat(follow_symlinks=False).st_size
                                    found.append(
                                        FoundMedia(
                                            source_name=source_name,
                                            filename=entry.name,
                                            base_name_lower=stem_lower,
                                            ext_lower=ext.lower(),
                                            category=classify_extension(ext),
                                            size_bytes=size_bytes,
                                            local_path=Path(entry.path),
                                        )
                                    )
                        except OSError:
                            pass
            except OSError:
                pass

    return found


def select_best_match(query_name: str, pool: List[FoundMedia]) -> Optional[FoundMedia]:
    """
    Sélectionne la meilleure version (taille max en octets) pour un nom recherché :
    - Si l'utilisateur a précisé une extension (ex: IMG_1234.JPG), filtre sur la même catégorie
      (photo vs vidéo) pour ne jamais confondre un .MOV Live Photo avec une photo .HEIC/.JPG,
      et prend le fichier le plus lourd (ex: s'il existe un IMG_1234.HEIC plus lourd sur Amazon, il gagne !).
    - Si l'utilisateur n'a pas mis d'extension (ex: IMG_1234), privilégie les photos puis prend la plus lourde.
    """
    q_stem, q_ext = os.path.splitext(query_name)
    q_stem_lower = q_stem.lower()
    q_cat = classify_extension(q_ext) if q_ext else None

    # Candidats ayant exactement le même nom de base
    candidates = [m for m in pool if m.base_name_lower == q_stem_lower]
    if not candidates:
        return None

    if q_cat and q_cat != "other":
        same_cat = [m for m in candidates if m.category == q_cat]
        if same_cat:
            candidates = same_cat
    elif not q_ext:
        # Si aucune extension fournie, préférer une photo à une vidéo Live Photo du même nom
        photos_only = [m for m in candidates if m.category == "photo"]
        if photos_only:
            candidates = photos_only

    # Tri par poids décroissant (meilleure qualité d'abord)
    candidates.sort(
        key=lambda m: (m.size_bytes, 1 if m.local_path is not None else 0),
        reverse=True,
    )
    return candidates[0]


def download_with_retries(
    winner: FoundMedia,
    dest_dir: Path,
    ap_client: Optional[object],
    max_retries: int = 3,
) -> Path:
    """
    Copie ou télécharge le fichier gagnant vers le dossier Téléchargements avec tentatives multiples
    (évite les micro-coupures ou erreurs temporaires d'Amazon Photos) et conserve le vrai nom du fichier.
    """
    final_path = dest_dir / winner.filename

    for attempt in range(1, max_retries + 1):
        try:
            if winner.local_path is not None:
                shutil.copy2(winner.local_path, final_path)
                return final_path

            if winner.amazon_node_id is not None and ap_client is not None:
                with tempfile.TemporaryDirectory() as tmp_dir:
                    ap_client.download([winner.amazon_node_id], out=tmp_dir)
                    downloaded_files = [p for p in Path(tmp_dir).iterdir() if p.is_file()]
                    if not downloaded_files:
                        raise FileNotFoundError(
                            f"Aucun fichier reçu depuis Amazon Photos pour node_id={winner.amazon_node_id}"
                        )
                    # On déplace en forçant le vrai nom original (winner.filename)
                    if final_path.exists():
                        final_path.unlink()
                    shutil.move(str(downloaded_files[0]), str(final_path))
                    return final_path

            raise RuntimeError("Source invalide pour le téléchargement.")

        except Exception as exc:
            if attempt < max_retries:
                wait_sec = 2 * attempt
                print(f"\n  [Tentative {attempt}/{max_retries} échouée pour {winner.filename}] {exc} -> Nouvel essai dans {wait_sec}s...")
                time.sleep(wait_sec)
            else:
                raise

    return final_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Recherche précise par nom (Amazon Photos + Drives) et copie dans le dossier Téléchargements du PC."
    )
    parser.add_argument(
        "names",
        nargs="*",
        help="Un ou plusieurs noms précis de photos à chercher (ex: IMG_1234.JPG IMG_5678.HEIC)",
    )
    parser.add_argument(
        "--file", "-f",
        type=Path,
        help="Fichier texte contenant la liste des noms de photos (1 nom par ligne)",
    )
    parser.add_argument(
        "--from-report",
        action="store_true",
        help="Récupère automatiquement toutes les photos en statut ERROR depuis rapport_transfert.csv",
    )
    parser.add_argument(
        "--amazon-only",
        action="store_true",
        help="Chercher uniquement sur Amazon Photos (ignorer les Drives A, B, C locaux)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Dossier de destination personnalisé (par défaut : dossier 'Téléchargements' du PC)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.json"),
        help="Chemin du fichier config.json (défaut: config.json)",
    )
    args = parser.parse_args()

    if not args.config.exists():
        print(f"[ERREUR] Fichier {args.config} introuvable.")
        sys.exit(1)

    with args.config.open("r", encoding="utf-8") as f:
        config = json.load(f)

    default_report = Path(config.get("report_csv", "rapport_transfert.csv"))

    # 1. Collecte des noms de photos à rechercher
    raw_names: List[str] = []
    if args.from_report:
        raw_names.extend(load_names_from_report(default_report))
    if args.file:
        if not args.file.exists():
            print(f"[ERREUR] Fichier liste introuvable : {args.file}")
            sys.exit(1)
        raw_names.extend(args.file.read_text(encoding="utf-8").splitlines())
    if args.names:
        raw_names.extend(args.names)

    target_names = normalize_query_names(raw_names)
    if not target_names:
        target_names = prompt_interactive_names(default_report)

    if not target_names:
        print("Aucun nom de photo fourni. Arrêt.")
        return

    dest_dir = args.out if args.out is not None else get_windows_downloads_folder()
    dest_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n[CIBLE] {len(target_names)} photo(s)/vidéo(s) à rechercher.")
    print(f"[DESTINATION] Dossier Téléchargements : {dest_dir}\n")

    # 2. Recherche sur Amazon Photos (+ Drives locaux sauf si --amazon-only)
    pool: List[FoundMedia] = []
    ap_found, ap_client = search_amazon_photos(
        ap_config=config.get("amazon_photos", {}),
        target_names=target_names,
        date_start_str=config.get("date_start"),
        date_end_str=config.get("date_end"),
    )
    pool.extend(ap_found)

    if not args.amazon_only:
        local_found = search_local_drives(config.get("drive_sources", {}), target_names)
        pool.extend(local_found)

    # 3. Sélection de la meilleure qualité et téléchargement dans "Téléchargements"
    ok_list = []
    not_found_list = []
    failed_list = []

    print(f"\n[TÉLÉCHARGEMENT] Copie vers {dest_dir} ...")
    for q_name in tqdm(target_names, desc="Récupération", unit="fichier"):
        winner = select_best_match(q_name, pool)
        if winner is None:
            not_found_list.append(q_name)
            continue

        try:
            saved_path = download_with_retries(winner, dest_dir, ap_client, max_retries=3)
            ok_list.append((q_name, winner, saved_path))
        except Exception as exc:
            failed_list.append((q_name, str(exc)))

    # 4. Affichage du bilan clair
    print("\n" + "=" * 70)
    print(f"BILAN DE LA RÉCUPÉRATION (vers {dest_dir}) :")
    print(f"  - Trouvées et téléchargées : {len(ok_list)} / {len(target_names)}")
    for q_name, w, saved_path in ok_list:
        mo = w.size_bytes / (1024 * 1024)
        print(f"      [OK] {w.filename} ({mo:.2f} Mo depuis {w.source_name})")

    if not_found_list:
        print(f"\n  - Introuvables ({len(not_found_list)}) :")
        for name in not_found_list:
            print(f"      [?] {name}")

    if failed_list:
        print(f"\n  - Erreurs de téléchargement ({len(failed_list)}) :")
        for name, err in failed_list:
            print(f"      [!] {name} -> {err}")
    print("=" * 70)


if __name__ == "__main__":
    main()
