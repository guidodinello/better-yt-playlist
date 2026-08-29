# better-yt-playlist

Fetch YouTube playlist videos and analyze/cluster by theme.

## Install

```bash
uv pip install -e .
```

## Usage

```bash
better-yt-playlist fetch --help
better-yt-playlist analyze --help
```

## Development

```bash
# Install dev dependencies
uv sync --dev

# Run pre-commit
pre-commit run --all-files

# Run tests (when added)
pytest
```

## Commands

| Command | Description |
|---------|-------------|
| `fetch` | Fetch videos from a YouTube playlist |
| `analyze` | Analyze/cluster videos by theme |
