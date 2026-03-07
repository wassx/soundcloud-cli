# sc — SoundCloud CLI

A terminal tool for searching, streaming, downloading, and inspecting SoundCloud content.

```
 ♪  BICEP | GLUE // CLIP — BICEP
 L │▁▂▄▆▇█▇▆▅▄▃▄▅▆▇█▇▅▃▂│
 R │▂▃▅▇█▇▆▅▄▃▂▃▄▆▇█▆▄▂▁│

   0:23  q/Space — stop
```

## Features

- **Search** tracks, users, and playlists with a rich table view
- **Interactive selection** — pick a track by number and stream it instantly after search
- **Animated VU meter** — L/R bar animation with elapsed timer while playing
- **Stop control** — press `q`, `Space`, or `Esc` to stop playback at any time
- **Download** tracks and playlists via yt-dlp (`mp3`, `m4a`, `opus`, `flac`)
- **Inspect** any track, user, or playlist URL with `sc info`
- No login required — uses the public SoundCloud API

## Requirements

| Dependency | Purpose |
|---|---|
| Python ≥ 3.10 | Runtime |
| `mpv` *(recommended)* | Audio playback |
| `ffplay` or `vlc` | Alternative players |
| `ffmpeg` | Required by yt-dlp for format conversion |

Install a player on Debian/Ubuntu:
```bash
sudo apt install mpv          # recommended
# or: sudo apt install ffmpeg
```

## Installation

```bash
git clone https://github.com/yourname/soundcloud-cli
cd soundcloud-cli

python3 -m venv .venv
source .venv/bin/activate

pip install -e .
```

The `sc` command is now available inside the virtual environment.  
To activate it in future sessions:
```bash
source /path/to/soundcloud-cli/.venv/bin/activate
```

## Usage

### Search and play interactively

```bash
sc search "bicep glue"
```

Displays a results table, then prompts:
```
Enter a track number to play [1–10], or press Enter / q to quit.
  Play> 2
```

Pick a number → track streams with the VU meter.  
After it finishes or you stop it, the prompt returns so you can pick another.

```bash
sc search "four tet" --type users       # search users
sc search "boiler room" --type playlists --limit 5
sc search "bicep" --list                # print results only, no prompt
```

### Stream the top result immediately

```bash
sc stream "bicep glue"
```

### Play a specific URL

```bash
sc play https://soundcloud.com/feelmybicep/bicep-glue-clip
```

### Playback controls

| Key | Action |
|---|---|
| `q` / `Q` | Stop |
| `Space` | Stop |
| `Esc` / `Ctrl-C` | Stop |

### Show details about a URL

```bash
sc info https://soundcloud.com/feelmybicep/bicep-glue-clip
sc info https://soundcloud.com/feelmybicep
```

### Download

```bash
sc download https://soundcloud.com/feelmybicep/bicep-glue-clip
sc download https://soundcloud.com/feelmybicep/bicep-glue-clip --format opus
sc download https://soundcloud.com/feelmybicep/bicep-glue-clip -o ~/Music
```

Supported formats: `mp3` (default), `m4a`, `opus`, `flac`, `best` (no re-encode).  
Playlists and sets are supported.

## Project layout

```
sc_cli/
├── __init__.py
├── api.py      ← SoundCloud API v2 client (auto-scrapes & caches client_id)
├── player.py   ← subprocess player + VU meter + keypress stop
└── main.py     ← Click CLI commands
pyproject.toml
```

## Notes

- The SoundCloud `client_id` is scraped automatically from their public JS bundle and cached at `~/.cache/sc_cli/client_id` for 24 hours. It refreshes itself on expiry or on a 401/403 response.
- Playback is unofficial/streaming only — tracks that are not publicly streamable will not work.
- Downloads go through yt-dlp; ffmpeg must be installed for format conversion.
