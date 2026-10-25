# Plushie pictures and sounds

The leaderboard (`leaderboard.py`) serves this folder at `/plushies/`. Each
folder is a namesake: a badge whose name contains the folder's name (ignoring
case) uses its files, and the longest match wins, so `Octocat` uses `octocat/`
rather than `cat/` or `octo/`. Every other badge uses `default/`, as does any
badge whose folder lacks a file.

```
plushies/
  default/   icon.svg  throw.wav  drop.wav   <- used for anything missing below
  copilot/   icon.svg  throw.wav             <- no drop.wav, so default/drop.wav
  bear/      icon.svg                        <- default sounds
```

| File | Used for |
| --- | --- |
| `icon.svg` | Next to the badge's bar and on its card. `.png`, `.webp`, `.gif` and `.jpg` also work. |
| `throw.wav` | Played once each time the plushie leaves the ground, including for a flight too short to see. It plays to the end, even after landing. |
| `drop.wav` | Played once when the badge reports a drop. |

Sounds may also be `.ogg`, `.mp3`, `.m4a` or `.webm`; if a folder has more than
one, the first in that order wins (WAV first). To add a plushie, add a folder
and reload the page; the server lists the folders on each page load
(`/plushies/manifest.json`), so it needs no restart.

## Making sounds

The page fetches and decodes every sound once, when sound is turned on, and
plays it from memory, so a throw or drop starts within a few milliseconds.
For the lowest latency:

- **16-bit PCM WAV, mono, 48 kHz.** It needs no decoding, and unlike MP3 and
  AAC it has no encoder padding: the sound starts on its first sample. Keep
  files short; size doesn't affect latency once loaded.
- **No silence at the start.** Trim to the first sample of sound.
- **Keep `throw` short.** About 0.5–1 s; it plays once per throw.
- **Normalise to about -1 dBFS.** The page sets the overall volume. Badges
  sharing a `default/` sound are pitched a little apart by their id; their
  own sounds play as recorded.

## Credits

- `copilot/icon.svg`, `mona/icon.svg`, `octocat/icon.svg`: Primer Octicons
  `copilot-24` and `mark-github-24` (MIT,
  [primer/octicons](https://github.com/primer/octicons)).
- `duck/icon.svg`: drawn for this project.
- The other icons, including `default/icon.svg` (teddy bear) and
  `squirrel/icon.svg`: [Twemoji](https://github.com/jdecked/twemoji) 15.1
  (graphics CC-BY 4.0, © Twitter, Inc. and other contributors).
- All sounds: synthesised for this project (the leaderboard's earlier Web
  Audio sounds, rendered to WAV).
