#!/usr/bin/env python3
"""cdrescue - squeeze every readable byte out of a scratched CD/DVD/BD.

Pipeline (each step uses the best tool installed, falls back to built-ins):
  1. image the disc: GNU ddrescue if present, then the built-in multi-pass
     reader (fast pass skipping damaged areas, sector-by-sector trim,
     slow-speed retries in alternating directions); ddrescue-compatible map
     so runs can be stopped and resumed
  2. audio CDs: rip tracks with cdparanoia / cdda2wav / icedax
  3. extract files from the image with 7z / bsdtar / xorriso / dvdbackup
  4. DVD-Video: save raw MPEG sectors (VOB data) found anywhere on the disc,
     so video survives even when the filesystem is unreadable
  5. carve other files (jpg/png/gif/pdf/zip) with foremost/photorec and a
     built-in sector-aligned carver
  6. report: bad sector ranges, which files they hit, sha256, report.json
Nothing is joined or repaired: it only recovers data.
Standard library only.
"""
import argparse
import fcntl
import glob
import hashlib
import json
import mmap
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time

SECTOR = 2048
CLUSTER = 32          # sectors per read in the fast pass (64 KiB = 2 DVD ECC blocks)
MAX_SKIP = 4096       # max sectors jumped after an error in the fast pass

# linux/cdrom.h
CDROM_SELECT_SPEED = 0x5322
CDROM_DRIVE_STATUS = 0x5326
CDROM_DISC_STATUS = 0x5327
CDS_DISC_OK = 4
CDS_AUDIO, CDS_MIXED = 100, 105
DISC_NAMES = {0: "no info", 1: "no disc", 2: "tray open", 3: "drive not ready",
              4: "disc ok", 100: "audio", 101: "data mode 1", 102: "data mode 2",
              103: "XA mode 2 form 1", 104: "XA mode 2 form 2", 105: "mixed audio+data"}

TOOLS = ["ddrescue", "cdparanoia", "cdda2wav", "icedax", "7z", "bsdtar", "xorriso",
         "dvdbackup", "foremost", "photorec"]

MPEG_PACK = b"\x00\x00\x01\xba"


def log(msg=""):
    print(msg, flush=True)
    if log.file:
        log.file.write(msg + "\n")
        log.file.flush()


log.file = None


def run(cmd, **kw):
    log("$ " + shlex.join(cmd))
    try:
        return subprocess.run(cmd, **kw).returncode
    except OSError as e:
        log(f"  failed to start: {e}")
        return -1


# ---------------------------------------------------------------- source

class Source:
    """Random-access reader over an optical drive or an image file."""

    def __init__(self, path):
        self.path = path
        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        self.is_drive = stat.S_ISBLK(os.fstat(self.fd).st_mode)
        self.size = os.lseek(self.fd, 0, os.SEEK_END)

    def read(self, offset, size):
        return os.pread(self.fd, size, offset)

    def drop_cache(self):
        # so a retry hits the disc again instead of the kernel's cached error
        try:
            os.posix_fadvise(self.fd, 0, 0, os.POSIX_FADV_DONTNEED)
        except OSError:
            pass

    def set_speed(self, x):
        # slower spin = more reliable reads on scratched discs; 0 = drive max
        if self.is_drive:
            try:
                fcntl.ioctl(self.fd, CDROM_SELECT_SPEED, x)
            except OSError:
                pass

    def close(self):
        os.close(self.fd)


def find_drives():
    return sorted(glob.glob("/dev/sr[0-9]*"))


def wait_for_disc(dev, timeout):
    fd = os.open(dev, os.O_RDONLY | os.O_NONBLOCK)
    try:
        end = time.monotonic() + timeout
        while True:
            try:
                st = fcntl.ioctl(fd, CDROM_DRIVE_STATUS, 0)
            except OSError:
                st = CDS_DISC_OK  # not a cdrom ioctl device; just try to read it
            if st == CDS_DISC_OK or time.monotonic() > end:
                break
            log(f"waiting for disc ({DISC_NAMES.get(st, st)})...")
            time.sleep(2)
        try:
            return fcntl.ioctl(fd, CDROM_DISC_STATUS, 0)
        except OSError:
            return 0
    finally:
        os.close(fd)


# ---------------------------------------------------------------- map file (GNU ddrescue format)

def load_map(path, nsec):
    st = bytearray(b"?" * nsec)
    if not os.path.exists(path):
        return st
    with open(path) as f:
        for line in f:
            p = line.split()
            if line.startswith("#") or len(p) != 3 or not p[1].startswith("0x"):
                continue
            pos, size, s = int(p[0], 16), int(p[1], 16), p[2]
            a, b = pos // SECTOR, min(nsec, -(-(pos + size) // SECTOR))
            s = s if s in "+-*" else ("*" if s == "/" else "?")
            st[a:b] = s.encode() * (b - a)
    return st


def save_map(path, st, total):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write("# Rescue Logfile. Created by cdrescue\n"
                "# current_pos  current_status  current_pass\n"
                "0x00000000     +               1\n"
                "#      pos        size  status\n")
        for a, b, s in runs(st):
            pos = a * SECTOR
            f.write(f"0x{pos:08X}  0x{min(b * SECTOR, total) - pos:08X}  {chr(s)}\n")
    os.replace(tmp, path)


def runs(st):
    """Yield (first, end, status) for each run of equal status."""
    i, n = 0, len(st)
    while i < n:
        j = i + 1
        while j < n and st[j] == st[i]:
            j += 1
        yield i, j, st[i]
        i = j


# ---------------------------------------------------------------- built-in rescue

def rescue(src, img_path, map_path, retries=2, quick=False):
    nsec = -(-src.size // SECTOR)
    st = load_map(map_path, nsec)
    fd = os.open(img_path, os.O_RDWR | os.O_CREAT, 0o644)
    os.ftruncate(fd, src.size)
    last = [time.monotonic()]

    def read(first, count):
        off = first * SECTOR
        size = min(count * SECTOR, src.size - off)
        try:
            data = src.read(off, size)
        except OSError:
            return False
        if len(data) != size:
            return False
        os.pwrite(fd, data, off)
        st[first:first + count] = b"+" * count
        return True

    def tick(name, pos):
        if time.monotonic() - last[0] > 3:
            last[0] = time.monotonic()
            save_map(map_path, st, src.size)
            log(f"  {name}: {100 * pos // max(nsec, 1)}%  good {st.count(b'+')}"
                f"  bad {st.count(b'-') + st.count(b'*')}  of {nsec} sectors")

    try:
        # big reads; the first time jump ahead after errors to grab the easy data first
        for name, skipping in (("fast pass", True), ("fill pass", False)):
            i, skip = 0, 0
            while i < nsec:
                n = min(CLUSTER, nsec - i)
                if b"?" in st[i:i + n]:
                    if read(i, n):
                        skip = 0
                    else:
                        st[i:i + n] = st[i:i + n].replace(b"?", b"*")
                        if skipping:
                            skip = min(max(skip * 2, CLUSTER), MAX_SKIP)
                            i += skip
                    tick(name, i)
                i += n
        if not quick:
            # split failed clusters into single sectors
            for i in [i for i in range(nsec) if st[i] == ord("*")]:
                if not read(i, 1):
                    st[i] = ord("-")
                tick("trim pass", i)
            # retries at lowest speed, alternating direction
            src.set_speed(1)
            for attempt in range(retries):
                bad = [i for i in range(nsec) if st[i] in b"-*"]
                if not bad:
                    break
                if attempt % 2:
                    bad.reverse()
                src.drop_cache()
                for k, i in enumerate(bad):
                    read(i, 1)
                    tick(f"retry {attempt + 1}/{retries}", k * nsec // len(bad))
            src.set_speed(0)
    finally:
        save_map(map_path, st, src.size)
        os.close(fd)
    return st


def ddrescue(src, img, mapf, retries):
    run(["ddrescue", "-b", str(SECTOR), "-n", src, img, mapf])
    run(["ddrescue", "-b", str(SECTOR), "-r", str(retries)]
        + (["-d"] if src.startswith("/dev/") else []) + [src, img, mapf])


def bad_ranges(st):
    return [(a, b) for a, b, s in runs(st) if s != ord("+")]


# ---------------------------------------------------------------- audio

def rip_audio(dev, outdir):
    os.makedirs(outdir, exist_ok=True)
    if shutil.which("cdparanoia"):
        run(["cdparanoia", "-Q", "-d", dev])
        if run(["cdparanoia", "-B", "-d", dev], cwd=outdir) == 0:
            return "cdparanoia"
    for tool in ("cdda2wav", "icedax"):
        if shutil.which(tool) and run([tool, "-B", f"dev={dev}"], cwd=outdir) == 0:
            return tool
    log("no audio ripper worked (install cdparanoia)")
    return None


# ---------------------------------------------------------------- file extraction

EXTRACTORS = [
    ("7z", lambda img, d: ["7z", "x", "-y", f"-o{d}", img]),
    ("bsdtar", lambda img, d: ["bsdtar", "-xf", img, "-C", d]),
    ("xorriso", lambda img, d: ["xorriso", "-osirrox", "on", "-indev", img, "-extract", "/", d]),
    ("dvdbackup", lambda img, d: ["dvdbackup", "-M", "-r", "b", "-i", img, "-o", d]),
]


def extract(img, outdir, damaged, dev=None):
    """Run extractors; with a damaged image try all of them, each may salvage different files."""
    done = []
    for name, cmd in EXTRACTORS:
        if not shutil.which(name):
            continue
        d = os.path.join(outdir, name)
        os.makedirs(d, exist_ok=True)
        # dvdbackup decrypts CSS through libdvdcss, which needs the real drive
        rc = run(cmd(dev if name == "dvdbackup" and dev else img, d), stdin=subprocess.DEVNULL)
        if os.listdir(d):
            done.append(name)
        if rc == 0 and not damaged:
            break
    return done


def file_extents(img):
    """[(path, first_lba, blocks)] from the ISO9660 tree, via xorriso."""
    if not shutil.which("xorriso"):
        return []
    out = subprocess.run(["xorriso", "-indev", img, "-find", "/", "-exec", "report_lba"],
                         capture_output=True, text=True).stdout
    return [(m.group(3), int(m.group(1)), int(m.group(2))) for m in re.finditer(
        r"File data lba:\s*\d+\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*\d+\s*,\s*'(.*)'", out)]


def damaged_files(extents, st):
    hits = []
    for path, lba, blocks in extents:
        bad = sum(1 for s in st[lba:lba + blocks] if s != ord("+"))
        if bad:
            hits.append((path, bad, blocks))
    return hits


# ---------------------------------------------------------------- DVD-Video raw sectors

def carve_mpeg(img, outdir, gap=256, min_size=1 << 20):
    """Save runs of sectors that start with an MPEG-PS pack header.

    Every DVD-Video VOB sector is a 2048-byte MPEG-PS pack, so this finds the
    video even if the filesystem is destroyed. Unreadable (zeroed) sectors are
    dropped; a new file starts after `gap` sectors without packs.
    """
    os.makedirs(outdir, exist_ok=True)
    streams, out, last = [], None, -gap - 1
    with open(img, "rb") as f:
        lba = 0
        while block := f.read(SECTOR * 512):
            for k in range(0, len(block), SECTOR):
                if block[k:k + 4] == MPEG_PACK:
                    if lba - last > gap:
                        if out:
                            out.close()
                        streams.append(os.path.join(outdir, f"lba_{lba:08d}.vob"))
                        out = open(streams[-1], "wb")
                    out.write(block[k:k + SECTOR])
                    last = lba
                lba += 1
    if out:
        out.close()
    kept = []
    for s in streams:
        if os.path.getsize(s) < min_size:
            os.remove(s)
        else:
            kept.append(s)
    return kept


# ---------------------------------------------------------------- generic carving

# name, header regex, footer, bytes after footer, max size
SIGS = [
    ("jpg", rb"\xff\xd8\xff", b"\xff\xd9", 0, 64 << 20),
    ("png", rb"\x89PNG\r\n\x1a\n", b"IEND\xaeB`\x82", 0, 64 << 20),
    ("gif", rb"GIF8[79]a", b"\x00;", 0, 32 << 20),
    ("pdf", rb"%PDF-", b"%%EOF", 0, 512 << 20),
    ("zip", rb"PK\x03\x04", b"PK\x05\x06", 18, 1 << 30),
]


def carve(img, outdir):
    """Carve files that start on a sector boundary (true for every file on ISO9660/UDF)."""
    os.makedirs(outdir, exist_ok=True)
    if os.path.getsize(img) == 0:
        return []
    pat = re.compile(b"|".join(b"(?P<%s>%s)" % (n.encode(), h) for n, h, *_ in SIGS))
    sig = {n: (f, extra, mx) for n, _, f, extra, mx in SIGS}
    found = []
    with open(img, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        starts = [(m.start(), m.lastgroup) for m in pat.finditer(mm) if m.start() % SECTOR == 0]
        for k, (s, kind) in enumerate(starts):
            footer, extra, mx = sig[kind]
            limit = min(starts[k + 1][0] if k + 1 < len(starts) else len(mm), s + mx)
            # last footer before the next file: skips footers of embedded thumbnails
            e = mm.rfind(footer, s + 4, limit)
            if e < 0:
                continue
            path = os.path.join(outdir, f"{s // SECTOR:08d}.{kind}")
            with open(path, "wb") as o:
                o.write(mm[s:min(e + len(footer) + extra, len(mm))])
            found.append(path)
    return found


def external_carvers(img, outdir):
    used = []
    if shutil.which("foremost"):
        if run(["foremost", "-q", "-i", img, "-o", os.path.join(outdir, "foremost")]) == 0:
            used.append("foremost")
    if shutil.which("photorec"):
        d = os.path.join(outdir, "photorec")
        os.makedirs(d, exist_ok=True)
        if run(["photorec", "/log", "/d", d + "/", "/cmd", img,
                "partition_none,fileopt,everything,enable,search"], stdin=subprocess.DEVNULL) == 0:
            used.append("photorec")
    return used


# ---------------------------------------------------------------- main

def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(1 << 20):
            h.update(b)
    return h.hexdigest()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Recover as much data as possible from a CD/DVD/BD.")
    ap.add_argument("source", nargs="?", help="drive (/dev/sr0) or disc image; default: first drive found")
    ap.add_argument("-o", "--out", help="output folder (default: cdrescue-YYYYmmdd-HHMMSS); "
                                        "reuse it to resume an interrupted rescue")
    ap.add_argument("-r", "--retries", type=int, default=2, help="retry passes over bad sectors (default 2)")
    ap.add_argument("--quick", action="store_true", help="no sector-by-sector trim/retry: fastest, loses more")
    ap.add_argument("--wait", type=int, default=30, help="seconds to wait for a disc in the drive")
    ap.add_argument("--no-ddrescue", action="store_true", help="use only the built-in reader")
    ap.add_argument("--no-audio", action="store_true")
    ap.add_argument("--no-extract", action="store_true")
    ap.add_argument("--no-video", action="store_true", help="skip raw VOB sector recovery")
    ap.add_argument("--no-carve", action="store_true")
    a = ap.parse_args(argv)

    out = os.path.abspath(a.out or time.strftime("cdrescue-%Y%m%d-%H%M%S"))
    os.makedirs(out, exist_ok=True)
    log.file = open(os.path.join(out, "cdrescue.log"), "a")
    try:
        return _main(a, out)
    finally:
        log.file.close()
        log.file = None


def _main(a, out):
    report = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "out": out}
    have = {t: bool(shutil.which(t)) for t in TOOLS}
    report["tools"] = have
    log("tools: " + "  ".join(f"{t}{'' if ok else '(missing)'}" for t, ok in have.items()))
    if not have["ddrescue"]:
        log("tip: install GNU ddrescue for the best imaging (pacman -S ddrescue / apt install gddrescue)")

    src_path = a.source
    if not src_path:
        drives = find_drives()
        if not drives:
            log("no optical drive found (/dev/sr*). Connect a USB CD/DVD drive, "
                "or pass an image file: cdrescue.py disc.iso")
            return 2
        src_path = drives[0]
    src_path = os.path.abspath(src_path)
    if not os.path.exists(src_path):
        log(f"{src_path}: not found")
        return 2
    is_drive = stat.S_ISBLK(os.stat(src_path).st_mode)
    report["source"] = src_path

    disc = wait_for_disc(src_path, a.wait) if is_drive else 0
    report["disc_type"] = DISC_NAMES.get(disc, disc)
    log(f"source: {src_path} ({'drive' if is_drive else 'image'}, {report['disc_type']})")

    if is_drive and disc in (CDS_AUDIO, CDS_MIXED) and not a.no_audio:
        report["audio_ripper"] = rip_audio(src_path, os.path.join(out, "audio"))
    if disc == CDS_AUDIO:
        return finish(out, report)

    if is_drive:
        img, mapf = os.path.join(out, "disc.iso"), os.path.join(out, "disc.map")
        if have["ddrescue"] and not a.no_ddrescue:
            ddrescue(src_path, img, mapf, a.retries)
        src = Source(src_path)
        try:
            log("built-in rescue (resumes from disc.map)")
            st = rescue(src, img, mapf, a.retries, a.quick)
        finally:
            src.close()
    else:
        img = src_path
        st = bytearray(b"+" * -(-os.path.getsize(img) // SECTOR))

    bad = bad_ranges(st)
    report.update(image=img, sectors=len(st), good_sectors=st.count(b"+"),
                  bad_sectors=len(st) - st.count(b"+"),
                  bad_ranges=[[x, y - 1] for x, y in bad])
    log(f"image: {report['good_sectors']}/{len(st)} sectors read, {report['bad_sectors']} unreadable")
    if bad:
        with open(os.path.join(out, "bad_sectors.txt"), "w") as f:
            f.writelines(f"{x}-{y - 1}\n" for x, y in bad)
    report["sha256"] = sha256(img)

    hits = damaged_files(file_extents(img), st) if bad else []
    report["damaged_files"] = [{"path": p, "bad_sectors": n, "sectors": t} for p, n, t in hits]
    if hits:
        with open(os.path.join(out, "damaged_files.txt"), "w") as f:
            f.writelines(f"{p}\t{n}/{t} sectors missing\n" for p, n, t in hits)
        log(f"{len(hits)} files contain unreadable sectors (see damaged_files.txt)")

    if not a.no_extract:
        report["extracted_with"] = extract(img, os.path.join(out, "files"), bool(bad),
                                           src_path if is_drive else None)
    if not a.no_video:
        report["vob_streams"] = carve_mpeg(img, os.path.join(out, "vob_sectors"))
    if not a.no_carve:
        cdir = os.path.join(out, "carved")
        report["carved"] = len(carve(img, cdir))
        report["carvers"] = ["builtin"] + external_carvers(img, cdir)
    return finish(out, report)


def finish(out, report):
    report["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(os.path.join(out, "report.json"), "w") as f:
        json.dump(report, f, indent=2)
    log(f"done -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
