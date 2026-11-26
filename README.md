# cdrescue

Recupera **il massimo possibile** da un CD/DVD/Blu-ray graffiato o danneggiato.
Un solo script Python (solo libreria standard) che usa ogni strumento installato
e, quando manca, ripiega sui propri metodi interni.

*English: one-file Python tool that images a scratched optical disc (ddrescue +
built-in multi-pass reader with resumable ddrescue-compatible map), rips audio,
extracts files with every extractor available, saves raw DVD-Video (VOB)
sectors even without a filesystem, carves files, and reports which files hit
bad sectors.*

## Guida passo passo (Linux Mint)

1. Collega il lettore CD/DVD (va bene anche uno esterno USB) e inserisci il disco.
   Se Mint apre una finestra o un lettore video, chiudili.
2. Apri il **Terminale** (menu → Terminale, oppure `Ctrl+Alt+T`).
3. Installa gli strumenti, una volta sola (chiede la password):
   ```bash
   sudo apt update && sudo apt install -y git python3 gddrescue p7zip-full libarchive-tools xorriso cdparanoia dvdbackup foremost testdisk
   ```
4. Scarica il programma:
   ```bash
   git clone https://github.com/GiacomoCunardi2005/cd-rescue && cd cd-rescue
   ```
5. Avvia il recupero (la cartella `recupero` verrà creata qui):
   ```bash
   sudo python3 cdrescue.py -o ~/recupero
   ```
   Su un disco graffiato può volerci **molto** tempo (anche ore): lascialo
   lavorare. Se devi fermarlo premi `Ctrl+C`; per riprendere rilancia **lo
   stesso comando**, e ripartirà da dove era arrivato.
6. Alla fine apri la cartella `recupero` nella tua home:
   - `files/` → i file del disco (per un DVD video: `files/7z/VIDEO_TS/...`)
   - `vob_sectors/` → video recuperato direttamente dai settori
   - `damaged_files.txt` → elenco dei file con pezzi mancanti
   - `disc.iso` + `disc.map` → copia completa del disco: **conservale**, servono
     per ricostruire i pezzi mancanti o per riprovare con un altro lettore.
7. (Facoltativo) Per migliorare il risultato, inserisci lo stesso disco in un
   **altro lettore** e rilancia il comando del punto 5 con la stessa cartella
   `-o ~/recupero`: vengono riletti solo i settori che mancano.

## Cosa fa

1. **Immagine del disco** → `disc.iso` + `disc.map`
   - GNU `ddrescue` se installato (il migliore), poi il lettore interno:
     passata veloce che salta le zone rovinate, passata di riempimento,
     rilettura settore per settore, nuovi tentativi alla **velocità minima**
     e in **direzione alternata**, svuotando la cache del kernel.
   - La mappa è nel formato di ddrescue: puoi **interrompere (Ctrl+C) e
     riprendere** rilanciando con la stessa cartella `-o`, anche con un
     **altro lettore** (lettori diversi leggono zone diverse).
2. **CD audio** → `audio/` con `cdparanoia` (o `cdda2wav`/`icedax`).
3. **Estrazione file** → `files/<strumento>/` con `7z`, `bsdtar`, `xorriso`,
   `dvdbackup`. Se ci sono settori illeggibili li prova **tutti**, perché
   ognuno può salvare file diversi.
4. **DVD-Video (VIDEO_TS)** → `vob_sectors/lba_XXXXXXXX.vob`: ogni settore di un
   VOB è un pacchetto MPEG, quindi il video viene recuperato **anche se il
   filesystem è distrutto**. I settori illeggibili vengono semplicemente
   saltati. Nessuna unione/conversione: solo dati recuperati.
5. **Carving** → `carved/`: jpg, png, gif, pdf, zip (docx/xlsx/odt) allineati al
   settore, più `foremost` e `photorec` se installati.
6. **Report**:
   - `bad_sectors.txt`: intervalli di settori non letti
   - `damaged_files.txt`: quali file contengono buchi (e quanti settori)
   - `report.json`, `cdrescue.log`, sha256 dell'immagine

I settori non leggibili restano a zero nell'immagine: i file vengono comunque
estratti, con dei piccoli buchi, e si possono ricostruire in seguito.

## Installazione (Linux Mint / Ubuntu)

```bash
sudo apt install python3 gddrescue p7zip-full libarchive-tools xorriso cdparanoia dvdbackup foremost testdisk
```

Sono tutti facoltativi: lo script funziona anche solo con Python 3.8+.
Per i **DVD commerciali criptati (CSS)** serve anche libdvdcss:

```bash
sudo apt install libdvd-pkg && sudo dpkg-reconfigure libdvd-pkg
```

Scarica lo script:

```bash
git clone https://github.com/GiacomoCunardi2005/cd-rescue && cd cd-rescue
```

## Uso

```bash
python3 cdrescue.py                       # primo lettore trovato (/dev/sr0)
python3 cdrescue.py /dev/sr1 -o recupero  # lettore e cartella scelti
python3 cdrescue.py -o recupero           # stessa cartella = riprende da dove era rimasto
python3 cdrescue.py immagine.iso          # lavora su un'immagine già fatta
python3 cdrescue.py --quick               # solo passate veloci: più rapido, perde di più
python3 cdrescue.py -r 5                  # più tentativi sui settori rovinati
```

Se dice *permission denied* su `/dev/sr0`, lancialo con `sudo`.

Opzioni: `--no-ddrescue`, `--no-audio`, `--no-extract`, `--no-video`,
`--no-carve`, `--wait SECONDI` (attesa che il disco sia pronto).

## Consigli per dischi graffiati

- Pulisci il disco dal centro verso l'esterno (mai in cerchio) con un panno morbido.
- Prova lettori diversi riusando la stessa cartella `-o`: la mappa accumula
  i settori letti da ognuno.
- I lettori DVD esterni USB economici spesso leggono meglio di quelli dei portatili.
- Più `-r` = più tentativi, ma il disco gira di più: prima fai una passata normale.

## Test

```bash
python3 -m unittest discover -s tests
```

I test simulano un disco con settori morti e settori instabili, e creano una
vera ISO con `VIDEO_TS` (serve `xorriso`).

## Licenza

MIT
