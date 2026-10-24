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
