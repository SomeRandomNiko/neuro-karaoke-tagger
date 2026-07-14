# neuro-karaoke-tagger

Keep the [Neuro Karaoke Archive](https://drive.google.com/drive/folders/1B1VaWp-mCKk15_7XpFnImsTdBJPOGx7a) up to date in [Navidrome](https://www.navidrome.org/) with zero manual effort.

This is a containerized, incremental pipeline that reads raw MP3s from the archive, parses the embedded JSON metadata, and outputs Navidrome-ready files with clean ID3v2.4 tags. Pair it with a Google Drive sync tool and a scheduler to have your library update itself automatically.

## How It Works

1. **Scans** `/input` for folders matching the archive naming convention (`DISC N - Album Name (dates)`)
2. **Compares** each file against a local SQLite state database to detect new or modified files
3. **Copies** changed files to `/output`, strips all existing tags, and writes fresh ID3v2.4 tags parsed from the JSON in the `COMM` frame
4. **Copies** cover art (`cover.*`, `folder.*`, `front.*`) to the output folder for Navidrome to pick up
5. **Purges** files from `/output` that no longer exist in `/input`

### Tag Mapping

| ID3 Frame | Source |
|-----------|--------|
| `TIT2` (Title) | JSON `Title` |
| `TPE1` (Artist) | JSON `Artist` + `CoverArtist`, split by `" & "` and `", "`, deduplicated |
| `TPE2` (Album Artist) | `QueenPB & Vedal987` |
| `TALB` (Album) | Parent folder name (with `DISC N -` prefix and date range stripped) |
| `TRCK` (Track) | JSON `Track` (supports `"N/M"` and bare integer) |
| `TPOS` (Disc) | JSON `Discnumber` |
| `TDRC` (Date) | JSON `Date` |

## Docker Setup

The recommended deployment uses three containers: Navidrome (music server), Ofelia (scheduler), and this tagger.

### What is Ofelia?

[Ofelia](https://github.com/mcuadros/ofelia) is a Docker-native job scheduler. Instead of configuring cron inside containers, Ofelia runs as a sidecar and triggers jobs on a schedule by spinning up containers or executing commands. All configuration lives in Docker labels.

### Docker Compose (Navidrome + Ofelia + Tagger)

```yaml
services:
  navidrome:
    image: deluan/navidrome:latest
    ports:
      - "4533:4533"
    volumes:
      - /path/to/navidrome-data:/data
      - /path/to/music-library:/music:ro

  ofelia:
    image: mcuadros/ofelia:latest
    command: daemon --docker
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    labels:
      # Run every day at 3 AM
      ofelia.job-run.neuro-karaoke-tagger.schedule: "0 0 3 * * *"
      ofelia.job-run.neuro-karaoke-tagger.image: ghcr.io/somerandomniko/neuro-karaoke-tagger:latest
      ofelia.job-run.neuro-karaoke-tagger.volume: >-
        [
          "/path/to/neuro-karaoke-archive-v3:/input:ro",
          "/path/to/music-library:/output:rw",
          "/path/to/neuro-karaoke-tagger-data:/data:rw"
        ]
      ofelia.job-run.neuro-karaoke-tagger.no-overlap: "true"
```

> **Note:** The tagger's `/output` and Navidrome's `/music` must point to the same host directory so Navidrome picks up the processed files.

### Docker Run (standalone)

```sh
docker run --rm \
  -v /path/to/neuro-karaoke-archive-v3:/input:ro \
  -v /path/to/music-library:/output \
  -v /path/to/neuro-karaoke-tagger-data:/data \
  ghcr.io/somerandomniko/neuro-karaoke-tagger:latest
```

### Force Reprocess

To ignore the state database and reprocess every file (e.g., after a tagger update):

```sh
docker run --rm \
  -v /path/to/neuro-karaoke-archive-v3:/input:ro \
  -v /path/to/music-library:/output \
  -v /path/to/neuro-karaoke-tagger-data:/data \
  ghcr.io/somerandomniko/neuro-karaoke-tagger:latest --force
```

## Volume Mounts

| Path | Mode | Purpose |
|------|------|---------|
| `/input` | Read-only | Raw MP3s from the Neuro Karaoke Archive (synced from Google Drive) |
| `/output` | Read-write | Processed MP3s (Navidrome's media library) |
| `/data` | Read-write | SQLite state database (`state_cache.db`) |

## Input Directory Structure

```
/input/
  DISC 1 - Humble Beginnings (2023-01-03 - 2023-05-17)/
    001. The Weeknd - Blinding Lights (Neuro.v1).mp3
    002. a-ha - Take On Me (Neuro.v1).mp3
    cover.png
  DISC 9 - Regularly Scheduled Program (2026-06-24 - Present)/
    001. Madeon - You're On (Neuro.v3).mp3
    ...
```

Folders that don't match the expected naming pattern are skipped with a warning.

## Credits

This project relies on the **Neuro Karaoke Archive v3** maintained by the community:

- [Google Drive archive](https://drive.google.com/drive/folders/1B1VaWp-mCKk15_7XpFnImsTdBJPOGx7a)
- [Discord channel](https://discord.com/channels/574720535888396288/1337588612845539349)
- [Metadata repository](https://github.com/Nyss777/Neuro-Karaoke-Archive-Metadata)

## Disclaimer

This project is **not affiliated with or endorsed by** VedalAI or the Neuro Karaoke Archive. It is an independent tool built for personal use.

The code is largely AI-generated because I'm not very familiar with Python, but it was the best tool for the job here thanks to the `mutagen` library for ID3 tag manipulation.

## Contributing

Suggestions, bug reports, and pull requests are welcome! Feel free to open an issue or PR if you have ideas for improvements.
