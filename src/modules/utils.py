from pathlib import Path
import logging
import yaml

def load_config(config_path):
    """指定したYAML設定ファイルを読み込み、設定データを返す。

    ファイルがなければエラーにする。
    """
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"config file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg

def load_primitives(primitive_names=None):
    """基本操作のJavaScriptファイルを、指定名または対象フォルダーの全件から読み込み、コード文字列の一覧を返す。"""
    primitives_dir = Path("primitives")
    if primitive_names is None:
        suffix = ".js"
        primitive_names = [
            path.name[:-len(suffix)]
            for path in primitives_dir.iterdir()
            if path.name.endswith(suffix)
        ]
    primitives = [
        (primitives_dir / f"{primitive_name}.js").read_text(encoding="utf-8")
        for primitive_name in primitive_names
    ]
    return primitives

def read_files(tree, base="."):
    """辞書やリストをたどり、文字列が基準フォルダー内の実在ファイルを指していれば内容に置き換える。

    ファイルでない文字列やその他の値は保持する。
    """
    base = Path(base)

    if isinstance(tree, dict):
        return {k: read_files(v, base) for k, v in tree.items()}
    elif isinstance(tree, list):
        return [read_files(v, base) for v in tree]
    elif isinstance(tree, str):
        p = base / tree
        return p.read_text(encoding="utf-8") if p.is_file() else tree
    else:
        return tree

def make_file_logger(
    name: str,
    log_path: str | Path,
    *,
    level: int = logging.INFO,
) -> logging.Logger:
    """ファイルと端末へ出力するロガーを用意する。

    既存ハンドラーがあれば再利用し、file_only付きのログはファイルだけへ出す。
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)
    if logger.handlers:
        return logger
    logger.propagate = False
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(level)
    fh.setFormatter(formatter)
    logger.addHandler(fh)
    sh = logging.StreamHandler()
    sh.addFilter(lambda record: not getattr(record, "file_only", False))
    sh.setLevel(level)
    sh.setFormatter(formatter)
    logger.addHandler(sh)
    return logger