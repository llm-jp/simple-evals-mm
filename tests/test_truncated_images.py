"""A truncated image file must fail to load, not be silently padded (PIL's LOAD_TRUNCATED_IMAGES must stay off)."""
import io

import pytest
from PIL import Image, ImageFile


def test_samplers_leave_truncated_image_loading_off(tmp_path):
    import simple_evals_mm.sampler.sglang_sampler  # noqa: F401  (used to switch it on at import)

    assert ImageFile.LOAD_TRUNCATED_IMAGES is False
    buf = io.BytesIO()
    Image.new("RGB", (256, 256), "red").save(buf, format="JPEG")
    path = tmp_path / "cut.jpg"
    path.write_bytes(buf.getvalue()[: len(buf.getvalue()) // 2])
    with pytest.raises(OSError, match="truncated"):
        Image.open(path).convert("RGB")


@pytest.mark.parametrize("module,cls", [("docvqa", "DocVQAEval"), ("infovqa", "InfoVQAEval"),
                                        ("okvqa", "OKVQAEval"), ("textvqa", "TextVQAEval")])
def test_vqa_tasks_fail_on_a_broken_image(tmp_path, monkeypatch, module, cls):
    """These tasks used to print and skip an unreadable image, dropping the question from the score."""
    import importlib
    import json

    from tests.helpers import EchoSampler, FixedJudge

    buf = io.BytesIO()
    Image.new("RGB", (64, 64), "red").save(buf, format="JPEG")
    cut = tmp_path / "cut.jpg"
    cut.write_bytes(buf.getvalue()[: len(buf.getvalue()) // 2])
    for name in ("data/okvqa/mscoco_val2014_annotations.json", "data/textvqa/textvqa_val_annotations.json"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(json.dumps({"annotations": [{"question_id": 1, "answers": [{"answer": "x"}]}]}))
    monkeypatch.chdir(tmp_path)
    task = getattr(importlib.import_module(f"simple_evals_mm.tasks.{module}"), cls).__new__(
        getattr(importlib.import_module(f"simple_evals_mm.tasks.{module}"), cls))
    task.grader_model, task.num_threads = FixedJudge(), 1
    for image, err in ((str(tmp_path / "missing.jpg"), FileNotFoundError), (str(cut), OSError)):
        task.examples = [{"image": image, "question": "q?", "question_id": 1, "answer": ["x"]}]
        with pytest.raises(err):
            task(EchoSampler())
