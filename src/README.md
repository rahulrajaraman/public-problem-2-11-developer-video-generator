Developer video pipeline. Entry point: `python src/main.py --repo <github-url-or-path>`.

Four roles in `devvideo.roles`: the retriever writes `evidence.json`, the scriptwriter drafts a storyboard (`claude -p` when `CLAUDE_KEY` is set), the reviewer accepts or sends it back once, and the producer renders speech and `developer-video.mp4` only after acceptance.
