from pathlib import Path


def sample_text():
    return (Path(__file__).parent.parent.parent.parent / "sample_data" / "pool.csv").read_text(encoding="utf-8-sig")
