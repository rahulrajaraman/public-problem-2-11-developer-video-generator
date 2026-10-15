# Frame Index

Frame Index is a small retrieval helper for video understanding.
It turns a clip's duration into sampled frame times, then ranks those frames for a text query.

## The problem

Developers building a video retrieval engine still need a readable index before they plug in a real embedding model.
Frame Index is that stand-in: it samples frame timestamps and scores them by token overlap so the search API can be tried without a model.

## API

`index_video(path, duration_s, fps=1.0)` returns an `Index` of sampled frame times.
`search(index, query, k=5)` returns up to `k` `Hit` values, each with a timestamp and a score.

## Usage

```python
from frame_index import index_video, search

index = index_video("clip.mp4", duration_s=12.0, fps=1.0)
hits = search(index, "person opening a door", k=5)
```

## Results

`index_video` with `duration_s=12.0` and `fps=1.0` records 12 timestamps, one per second.
`search` returns at most 5 hits, sorted by token-overlap score.
