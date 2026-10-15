# Developer video generator

## Disclaimer 
I took the help of cursor to develop this workflow. 

## Workflow

The way I attempted to solve this problem was using a mostly fixed workflow and a templated video (or slides) generation for the purpose of this interview since time is of essence. But if I were to do this properly, I would want to generate enough evidences and titles to properly generate enough use cases to use, and then render them all into videos. 

The steps of the workflow is mentioned below in the same order.
1. Repo Ingestion -  Clones the repo to the working directory. 
2. Grounding - This step as required by the problem statement, generates evidences of code snippets, readme snippets that are relevant to the prefixed template. This step produces `evidence.json` files which contains the necessary snippets that are shown during rendering. 
3.  LLM Based Content - Uses the evidence files to write the content for the video using an LLM. This is essentially going to be the body of each prefixed slide. A bit of prompt engineering is in works here, to provide the context and the evidence to the LLM. Since the titles are prefixed and evidence is given, we do not need strict gaurdrails here, however it will be needed if we choose to improve our system to adaptive templates based planning.
4. There is a reviewer component here that validates the script to see if the citations are used correctly.
5. The final renderer writes the narration.txt and converts it to audio using a lightweight TTS model called en-US-GuyNeural, and renders the templates of the frame accordingly. 


## Functions and Roles
Four roles pass files to each other. The producer does not start until the reviewer accepts the script.

1. **Retriever.** `--repo` accepts `https://github.com/owner/name`. GitHub inputs are shallow-cloned. The repo is never executed. README, docs, benchmarks, fenced examples, and public Python signatures become evidence cards with file and line numbers. The pack is `evidence.json`.
2. **Scriptwriter.** Reads that pack and drafts five beats: title, problem, APIs, how to use it, results. If `CLAUDE_KEY` is set, the draft comes from `claude -p`. That variable is a Claude login token (`CLAUDE_CODE_OAUTH_TOKEN`), not an Anthropic API key. Otherwise an OpenAI-compatible chat model or Ollama is used when configured. `--llm off` skips both and fills the beats from the evidence cards directly.
**Note** I used local Ollama to test it locally, and used cursor to include support for Claude token as mentioned in the repo. 

3. **Reviewer.** Accepts the draft, or sends the problems back to the scriptwriter for one revision. A beat must cite evidence ids from the pack. A number or percentage in the headline or narration must appear in those cards. The video stays under 60 seconds. With `--llm auto`, a second rejection falls back to the evidence-backed draft.
4. **Producer.** Reads the accepted storyboard aloud with edge-tts, draws each beat, and muxes `developer-video.mp4`. The on-screen code is copied from the evidence rows.

`narration.txt` is the timed read-aloud script. `script.json` records which generator wrote the draft and how many review attempts it took.

Speech uses en-US-GuyNeural, or `DEVVIDEO_TTS_VOICE`. `--tts auto` keeps a silent video if the speech service cannot be reached. `--tts off` skips speech.

## Run

```bash
pip install -r requirements.txt
python src/main.py --repo https://github.com/owner/name --out artifacts/name
python src/main.py --repo examples/frame_index --out artifacts/frame_index --llm off
```

`examples/frame_index` is a tiny video-retrieval stand-in so the pipeline runs without network. Point `--repo` at a real video-understanding or retrieval checkout for a full video. ffmpeg is required.

Set `CLAUDE_KEY` to draft with `claude -p`. Set `DEVVIDEO_LLM_BASE_URL` and `DEVVIDEO_LLM_MODEL` for a non-OpenAI chat endpoint. `--llm on` stops if neither Claude nor a chat endpoint is available. `--llm auto` falls back to the evidence-backed draft. `--llm off` does not call a model.

## Limits

Retrieval is README, docs, benchmarks, and Python signatures. It does not follow call graphs or run the project, so "how to use it" shows the repo's own quick start and `main` entry. Repos without a measured result get an on-screen card and a narration that says no figure was retrieved. Speech needs network access to the edge-tts service.
