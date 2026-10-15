# Developer video generator

A one-minute developer video from a GitHub repository. The video explains the problem the repo addresses, the functions a caller uses, how a call moves through the code, and the results the repo actually reports. Every code block and quote on screen is a retrieved slice of that repo.

## Pipeline

1. **Checkout.** `--repo` accepts `https://github.com/owner/name`, `owner/name`, or a local directory. GitHub inputs are shallow-cloned. The repo is never executed.
2. **Retrieve.** README, `docs/`, and `benchmarks/` are split into sections, fenced examples, and notes. Public Python functions and classes become signature cards (name, docstring, public methods) with file and line numbers. Cards are ranked toward problem statements, usage, exported APIs, and benchmark tables, then kept as a small evidence pack (`evidence.json`).
3. **Script.** Five beats, in order: title, problem, APIs, how to use it, results. An OpenAI-compatible chat model writes the narration when `DEVVIDEO_LLM_API_KEY` or `OPENAI_API_KEY` is set, or when Ollama is running. The model may only cite evidence ids. A validator rejects unknown ids and any number or percentage that does not appear verbatim in the cited cards, and retries once. Without a model, a grounded composer writes the same shape from those cards. Either way, durations are fit under 60 seconds. `narration.txt` is the timed read-aloud script.
4. **Speech.** edge-tts (en-US-GuyNeural, or `DEVVIDEO_TTS_VOICE`) reads each beat. Paths and units are softened for speech (`engine.py` becomes "engine dot py", `ms` becomes "milliseconds"). The clip is padded to the beat length, so the line starts with the frame and the rest of the beat is quiet for reading the evidence. `--tts auto` keeps the silent video if the service cannot be reached. `--tts off` skips this step.
5. **Render.** Each beat is a frame: spoken line on top, a **RETRIEVED** panel underneath that copies the cited file lines. API beats stack the retrieved signatures. ffmpeg muxes the speech track into `developer-video.mp4`.

The picture does not trust the narration for code. The panel is drawn from the evidence rows, so a model cannot invent a function body onto the screen.

## Run

```bash
pip install -r requirements.txt
python src/main.py --repo https://github.com/owner/name --out artifacts/name
python src/main.py --repo examples/frame_index --out artifacts/frame_index --llm off
```

`examples/frame_index` is a tiny video-retrieval stand-in so the pipeline runs without network. Point `--repo` at a real video-understanding or retrieval checkout for a full video. ffmpeg is required.

Set `DEVVIDEO_LLM_BASE_URL` and `DEVVIDEO_LLM_MODEL` for a non-OpenAI endpoint. `--llm on` fails closed if no endpoint is configured. `--llm auto` falls back to the grounded composer.

## Limits

Retrieval is README, docs, benchmarks, and Python signatures. It does not follow call graphs or run the project, so "how to use it" shows the repo's own quick start and `main` entry. Repos without a measured result get an on-screen card and a narration that says no figure was retrieved. Speech needs network access to the edge-tts service.
