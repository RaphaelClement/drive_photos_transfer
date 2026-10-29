# Transfert & Déduplication Qualité Max (Google Drives A, B, C + Amazon Photos ➔ Google Drive D)

Ce projet permet de transférer toutes vos photos et vidéos prises entre le **8 septembre 2025** et le **15 juillet 2026** depuis **Google Drive A, B, C** et **Amazon Photos** vers un dossier unique sur le **Google Drive D**, en ne gardant systématiquement que **la version la moins compressée (fichier le plus lourd en octets)**.

---

## Pourquoi cette architecture évite 100 % des conflits dans le Drive D

Au lieu de copier successivement A vers D, puis B vers D, puis C vers D (ce qui force Google Drive à créer des `IMG_1234 (1).jpg` ou à uploader des gigaoctets de photos compressées qui seront supprimées ensuite), le script applique le principe **"Scan & Elect" (Scanner puis Élire)** :

1. **Scan sans téléchargement** :
   - Sur les **Drives A, B et C** (montés dans l'Explorateur Windows), `os.scandir()` lit le nom, la taille exacte en octets et la date de modification **sans jamais ouvrir le fichier** (Google Drive for Desktop ne télécharge donc pas les fichiers pendant le scan).
   - Sur **Amazon Photos**, la librairie `amazon-photos` interroge le catalogue à distance et récupère les noms, tailles en octets et dates sans télécharger les photos.
2. **Séparation Photos / Vidéos** :
   - Chaque fichier est identifié par `(nom_de_base_en_minuscule, catégorie)`.
   - Ainsi, `IMG_1234.HEIC` (2.5 Mo) et sa version compressée `IMG_1234.jpg` (800 Ko) sont comparées ensemble (la `.HEIC` gagne), tandis qu'une vidéo Live Photo `IMG_1234.MOV` est traitée dans la catégorie `"video"` et n'écrase jamais la photo.
3. **Copie unique et atomique vers le Drive D** :
   - Seul le fichier gagnant est copié (ou téléchargé depuis Amazon Photos) vers le Drive D.
   - Si vous relancez le script plus tard, il vérifie ce qui est déjà dans D : si le fichier est déjà présent en qualité optimale, il est ignoré ; si une version plus lourde est trouvée, l'ancienne est supprimée de D *avant* d'écrire la nouvelle.

---

## Installation (sur le PC où sont connectés les comptes)

1. Copiez ce dossier `drive_photos_transfer` sur le PC cible.
2. Installez les dépendances Python :
   ```powershell
   pip install -r requirements.txt
   ```

---

## Configuration (`config.json`)

Ouvrez `config.json` et adaptez les chemins et cookies :

```json
{
  "date_start": "2025-09-08",
  "date_end": "2026-07-15",
  "drive_sources": {
    "Drive_A": "G:\\Mon Drive\\Photos",
    "Drive_B": "H:\\Mon Drive\\Photos",
    "Drive_C": "I:\\Mon Drive\\Photos"
  },
  "drive_destination": "J:\\Mon Drive\\Photos_2025_2026",
  "amazon_photos": {
    "enabled": true,
    "tld": "fr",
    "cookies": {
      "session-id": "VOTRE_SESSION_ID",
      "ubid-acbfr": "VOTRE_UBID_ACBFR",
      "at-acbfr": "VOTRE_AT_ACBFR"
    }
  },
  "report_csv": "rapport_transfert.csv"
}
```

### Comment récupérer les 3 cookies Amazon Photos (`amazon.fr`) en 30 secondes :
1. Connectez-vous sur [https://www.amazon.fr/photos](https://www.amazon.fr/photos) depuis votre navigateur (Chrome, Edge, Brave ou Firefox).
2. Appuyez sur **F12** pour ouvrir les outils de développement.
3. Allez dans l'onglet **Application** (sur Chrome/Edge) ou **Stockage** (sur Firefox) ➔ **Cookies** ➔ `https://www.amazon.fr`.
4. Copiez la colonne **Valeur (Value)** des 3 cookies suivants dans `config.json` :
   - `session-id`
   - `ubid-acbfr`
   - `at-acbfr`

---

## Utilisation

### Étape 1 : Lancer une simulation (`--dry-run`)
Ne copie et ne modifie **aucun** fichier, mais scanne A, B, C et Amazon Photos et génère le tableau Excel/CSV `rapport_transfert.csv` montrant quel fichier gagne et quels doublons sont écartés :

```powershell
python transfer_photos.py --dry-run
```

Ouvrez `rapport_transfert.csv` (avec Excel) pour vérifier les décisions du script.

### Étape 2 : Lancer le vrai transfert vers le Drive D
Une fois satisfait du rapport de simulation, lancez le transfert réel :

```powershell
python transfer_photos.py
```

---

## Script 2 : Rechercher précisément des photos par leur nom et les copier dans `Téléchargements` (`find_and_download.py`)

La barre de recherche du site web Amazon Photos retrouve très mal les noms de fichiers exacts. Si certaines photos ont rencontré une erreur ou si vous voulez récupérer une liste précise de photos dans leur meilleure qualité vers le dossier **Téléchargements** du PC :

### Option A — Mode interactif (le plus simple : collez 1 nom ou vos 40 noms d'un coup)
```powershell
python find_and_download.py
```
*(Collez vos noms un par ligne puis appuyez deux fois sur Entrée, ou tapez simplement `erreurs` pour qu'il lise tout seul les lignes `ERROR` de `rapport_transfert.csv` !)*

### Option B — Récupérer automatiquement toutes les erreurs de `rapport_transfert.csv`
```powershell
python find_and_download.py --from-report
```

### Option C — Depuis un fichier texte ou directement en ligne de commande
```powershell
python find_and_download.py IMG_1234.JPG IMG_5678.HEIC
# ou depuis un fichier texte (1 nom par ligne) :
python find_and_download.py --file liste_photos.txt
```
*(Ajoutez `--amazon-only` si vous voulez chercher uniquement sur Amazon Photos sans vérifier les Drives A, B, C).*

