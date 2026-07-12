#!/usr/bin/env python3
"""Neuro Karaoke Tagger - Incremental MP3 metadata pipeline for Navidrome."""

import json
import logging
import os
import re
import shutil
import sqlite3

from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1, TPE2, TALB, TRCK, TPOS, TDRC, APIC

INPUT_DIR = "/input"
OUTPUT_DIR = "/output"
DB_PATH = "/data/state_cache.db"
ALBUM_ARTIST = "QueenPB & Vedal987"
FOLDER_RE = re.compile(
    r"^DISC \d+ - (.+) \(\d{4}-\d{2}-\d{2} - (?:\d{4}-\d{2}-\d{2}|Present)\)$"
)

log = logging.getLogger("tagger")


# ---------------------------------------------------------------------------
# Database layer
# ---------------------------------------------------------------------------

def init_db(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS files ("
        "  relative_path TEXT PRIMARY KEY,"
        "  mtime REAL NOT NULL,"
        "  size INTEGER NOT NULL"
        ")"
    )
    conn.commit()


def get_record(conn, rel_path):
    return conn.execute(
        "SELECT mtime, size FROM files WHERE relative_path = ?", (rel_path,)
    ).fetchone()


def upsert_record(conn, rel_path, mtime, size):
    conn.execute(
        "INSERT OR REPLACE INTO files (relative_path, mtime, size) VALUES (?, ?, ?)",
        (rel_path, mtime, size),
    )
    conn.commit()


def delete_record(conn, rel_path):
    conn.execute("DELETE FROM files WHERE relative_path = ?", (rel_path,))
    conn.commit()


def get_all_paths(conn):
    return {r[0] for r in conn.execute("SELECT relative_path FROM files").fetchall()}


# ---------------------------------------------------------------------------
# Folder name parser
# ---------------------------------------------------------------------------

def parse_album_name(folder_name):
    m = FOLDER_RE.match(folder_name)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# COMM JSON metadata extraction
# ---------------------------------------------------------------------------

def extract_metadata(filepath):
    try:
        audio = MP3(filepath)
    except Exception as e:
        log.warning("Failed to read MP3 %s: %s", filepath, e)
        return None

    if audio.tags is None:
        log.warning("No ID3 tags in %s", filepath)
        return None

    # Find COMM frame with language code "ved"
    json_str = None
    for key in audio.tags:
        if key.startswith("COMM:"):
            frame = audio.tags[key]
            if frame.lang == "ved":
                json_str = frame.text[0] if isinstance(frame.text, list) else frame.text
                break

    if json_str is None:
        log.warning("No COMM frame with lang 'ved' in %s", filepath)
        return None

    try:
        data = json.loads(json_str)
    except (json.JSONDecodeError, TypeError) as e:
        log.warning("Invalid JSON in COMM frame of %s: %s", filepath, e)
        return None

    required = ("Title", "Artist", "CoverArtist", "Date", "Track", "Discnumber")
    missing = [f for f in required if f not in data]
    if missing:
        log.warning("Missing fields %s in %s", missing, filepath)
        return None

    # Parse track number (handles both "4/189" and bare int 30)
    track_raw = data["Track"]
    if isinstance(track_raw, str) and "/" in track_raw:
        parts = track_raw.split("/", 1)
        track_num = int(parts[0])
        track_total = int(parts[1])
    else:
        track_num = int(track_raw)
        track_total = None

    # Split and deduplicate artists, preserving order
    artists = data["Artist"].split(" & ")
    cover_artists = data["CoverArtist"].split(" & ")
    all_artists = list(dict.fromkeys(artists + cover_artists))

    return {
        "title": data["Title"],
        "artists": all_artists,
        "date": data["Date"],
        "track_num": track_num,
        "track_total": track_total,
        "disc_number": data["Discnumber"],
    }


# ---------------------------------------------------------------------------
# ID3v2.4 tag writer
# ---------------------------------------------------------------------------

def apply_tags(filepath, metadata, album_name, cover_bytes):
    # Strip all existing tags from the file
    audio = MP3(filepath)
    audio.delete()

    # Build a fresh ID3v2.4 tag set
    tags = ID3()
    tags.add(TIT2(encoding=3, text=[metadata["title"]]))
    tags.add(TPE1(encoding=3, text=metadata["artists"]))
    tags.add(TPE2(encoding=3, text=[ALBUM_ARTIST]))
    tags.add(TALB(encoding=3, text=[album_name]))

    track_str = str(metadata["track_num"])
    if metadata["track_total"] is not None:
        track_str = f"{metadata['track_num']}/{metadata['track_total']}"
    tags.add(TRCK(encoding=3, text=[track_str]))

    tags.add(TPOS(encoding=3, text=[str(metadata["disc_number"])]))
    tags.add(TDRC(encoding=3, text=[metadata["date"]]))

    if cover_bytes:
        tags.add(APIC(
            encoding=3,
            mime="image/png",
            type=3,
            desc="Front Cover",
            data=cover_bytes,
        ))

    tags.save(filepath, v2_version=4)


# ---------------------------------------------------------------------------
# Sync engine
# ---------------------------------------------------------------------------

def sync():
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    stats = {"processed": 0, "skipped": 0, "deleted": 0, "errors": 0}
    seen_paths = set()
    folders_with_writes = set()

    # --- Step A: scan & validate folders ---
    try:
        entries = sorted(os.listdir(INPUT_DIR))
    except OSError as e:
        log.error("Cannot list input directory: %s", e)
        conn.close()
        return stats

    album_map = {}
    for entry in entries:
        if not os.path.isdir(os.path.join(INPUT_DIR, entry)):
            continue
        album_name = parse_album_name(entry)
        if album_name is None:
            log.warning("Skipping folder with unrecognized name: %s", entry)
            continue
        album_map[entry] = album_name

    # --- Step B: process files ---
    for folder_name, album_name in album_map.items():
        input_folder = os.path.join(INPUT_DIR, folder_name)
        output_folder = os.path.join(OUTPUT_DIR, folder_name)

        # Read cover art once per folder
        cover_path = os.path.join(input_folder, "cover.png")
        cover_bytes = None
        if os.path.isfile(cover_path):
            with open(cover_path, "rb") as f:
                cover_bytes = f.read()

        for fname in sorted(os.listdir(input_folder)):
            if not fname.lower().endswith(".mp3"):
                continue

            rel_path = os.path.join(folder_name, fname)
            input_path = os.path.join(input_folder, fname)
            output_path = os.path.join(output_folder, fname)
            seen_paths.add(rel_path)

            st = os.stat(input_path)
            record = get_record(conn, rel_path)
            if record and record[0] == st.st_mtime and record[1] == st.st_size:
                stats["skipped"] += 1
                continue

            # Copy raw file to output
            try:
                os.makedirs(output_folder, exist_ok=True)
                shutil.copy2(input_path, output_path)
            except OSError as e:
                log.warning("Failed to copy %s: %s", rel_path, e)
                stats["errors"] += 1
                continue

            # Extract metadata from the output copy
            metadata = extract_metadata(output_path)
            if metadata is None:
                log.warning("No metadata for %s, copied without tags", rel_path)
                stats["errors"] += 1
                upsert_record(conn, rel_path, st.st_mtime, st.st_size)
                folders_with_writes.add(folder_name)
                continue

            # Apply clean ID3v2.4 tags
            try:
                apply_tags(output_path, metadata, album_name, cover_bytes)
            except Exception as e:
                log.warning("Failed to apply tags to %s: %s", rel_path, e)
                stats["errors"] += 1
                upsert_record(conn, rel_path, st.st_mtime, st.st_size)
                folders_with_writes.add(folder_name)
                continue

            upsert_record(conn, rel_path, st.st_mtime, st.st_size)
            folders_with_writes.add(folder_name)
            stats["processed"] += 1
            log.info("Processed: %s", rel_path)

    # --- Step C: copy cover.png for folders that had writes ---
    for folder_name in folders_with_writes:
        src = os.path.join(INPUT_DIR, folder_name, "cover.png")
        dst = os.path.join(OUTPUT_DIR, folder_name, "cover.png")
        if os.path.isfile(src):
            try:
                shutil.copy2(src, dst)
                log.info("Copied cover art: %s", folder_name)
            except OSError as e:
                log.warning("Failed to copy cover.png for %s: %s", folder_name, e)

    # --- Step D: purge deletions ---
    orphans = get_all_paths(conn) - seen_paths
    for rel_path in orphans:
        output_path = os.path.join(OUTPUT_DIR, rel_path)
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
                log.info("Deleted: %s", rel_path)
            except OSError as e:
                log.warning("Failed to delete %s: %s", rel_path, e)
        delete_record(conn, rel_path)
        stats["deleted"] += 1

    # Clean up empty directories in output
    for dirpath, dirnames, filenames in os.walk(OUTPUT_DIR, topdown=False):
        if dirpath != OUTPUT_DIR and not dirnames and not filenames:
            try:
                os.rmdir(dirpath)
                log.info("Removed empty directory: %s", dirpath)
            except OSError:
                pass

    conn.close()
    return stats


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    log.info("Starting neuro-karaoke-tagger")
    stats = sync()
    log.info(
        "Done: %d processed, %d skipped, %d deleted, %d errors",
        stats["processed"],
        stats["skipped"],
        stats["deleted"],
        stats["errors"],
    )


if __name__ == "__main__":
    main()
