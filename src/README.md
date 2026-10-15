Developer video pipeline. Entry point: `python src/main.py --repo <github-url-or-path>`.

`devvideo.clone` checks out the repo, `devvideo.evidence` retrieves README and API cards, `devvideo.script` writes a five-beat cited script, `devvideo.tts` reads that script aloud, and `devvideo.render` draws the cards and muxes the speech into the mp4.
