from pathlib import Path


def read_under(root, supplied):
    return (Path(root) / supplied).read_text()
