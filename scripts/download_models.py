"""One-time online model preparation; PDF conversion itself runs offline."""

import os

from files_to_feishu.config import Settings


def main():
    os.environ.pop("HF_HUB_OFFLINE", None)
    os.environ.pop("TRANSFORMERS_OFFLINE", None)
    from docling.utils.model_downloader import download_models

    destination = Settings().docling_artifacts_path.resolve()
    download_models(
        output_dir=destination,
        progress=True,
        with_layout=True,
        with_tableformer=True,
        with_code_formula=False,
        with_picture_classifier=False,
        with_rapidocr=False,
    )
    (destination / ".ready").write_text("docling layout + tableformer\n", encoding="utf-8")
    print(f"本地模型已准备：{destination}")


if __name__ == "__main__":
    main()
